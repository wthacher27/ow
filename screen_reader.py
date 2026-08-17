"""
Overwatch 2 Screen Reader
Watches the game screen, detects loading screens and match results via OCR,
and auto-logs them to the tracker database.

Windows requirements (pip install):
    mss Pillow pytesseract

Tesseract OCR engine (required):
    https://github.com/UB-Mannheim/tesseract/wiki
    Default path: C:\\Program Files\\Tesseract-OCR\\tesseract.exe
"""

import asyncio
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    import mss
    import mss.tools
    from PIL import Image, ImageEnhance, ImageFilter
    import pytesseract
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Run: pip install mss Pillow pytesseract")
    sys.exit(1)

from database import init_db, get_players, save_game_log, get_recent_games

# ─── CONFIG ─────────────────────────────────────────────────────────────────
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POLL_INTERVAL  = 3    # seconds between screenshots
CONFIRM_FRAMES = 2    # consecutive confirmations before acting
COOLDOWN_SEC   = 45   # ignore screen after logging a match
MONITOR_INDEX  = 1    # 1 = primary monitor; change if OW is on a different screen
# ─────────────────────────────────────────────────────────────────────────────

if sys.platform == "win32":
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# All current OW2 maps (lowercase for matching)
KNOWN_MAPS = {
    # Control
    "ilios", "lijiang tower", "nepal", "oasis", "samoa", "antarctic peninsula", "busan",
    # Escort
    "dorado", "havana", "junkertown", "rialto", "route 66", "circuit royal",
    "watchpoint: gibraltar", "watchpoint gibraltar", "shambali monastery", "paraíso", "paraiso",
    # Hybrid
    "blizzard world", "eichenwalde", "hollywood", "king's row", "kings row",
    "midtown", "numbani",
    # Push
    "colosseo", "esperança", "esperanca", "new queen street", "runasapi", "hanaoka",
    # Flashpoint
    "new junk city", "suravasa", "aatlis",
    # Clash
    "throne of anubis",
    # Deathmatch / Workshop
    "chateau guillard", "petra", "kanezaka", "black forest", "necropolis",
    "castillo", "adlersbrunn", "malevento",
}

# Normalize map names for display (handle OCR variants)
MAP_ALIASES = {
    "kings row":            "King's Row",
    "king's row":           "King's Row",
    "watchpoint gibraltar": "Watchpoint: Gibraltar",
    "paraiso":              "Paraíso",
    "paraíso":              "Paraíso",
    "esperanca":            "Esperança",
    "esperança":            "Esperança",
}


def _normalize_map(raw: str) -> str:
    key = raw.strip().lower()
    return MAP_ALIASES.get(key, raw.strip().title())


def _preprocess(img: Image.Image, scale: float = 2.0) -> Image.Image:
    w, h = img.size
    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    img = img.convert("L")                        # grayscale
    img = ImageEnhance.Contrast(img).enhance(2.5)
    img = img.point(lambda p: 255 if p > 140 else 0)  # threshold
    return img


_TESS_CONFIG = "--oem 3 --psm 3"


def _ocr(img: Image.Image, region=None, config: str = _TESS_CONFIG) -> str:
    if region:
        img = img.crop(region)
    processed = _preprocess(img)
    return pytesseract.image_to_string(processed, config=config)


_BADGE_COLORS = {"win": (25, 211, 25), "loss": (209, 39, 39), "draw": (152, 152, 152)}
_BADGE_COLOR_TOL = 20


def _classify_badge_color(rgb) -> str | None:
    r, g, b = rgb[:3]
    for name, (tr, tg, tb) in _BADGE_COLORS.items():
        if abs(r - tr) <= _BADGE_COLOR_TOL and abs(g - tg) <= _BADGE_COLOR_TOL and abs(b - tb) <= _BADGE_COLOR_TOL:
            return name
    return None


def _find_badge_bands(img: Image.Image) -> list[tuple[int, int, str]]:
    """Scan the badge column top-to-bottom for contiguous vertical bands of a
    single result color; each band marks one match row and its result.

    The History screen renders WIN/LOSS/DRAW as a solid-color pill badge in a
    stylized font OCR can't read reliably (see `_classify_badge_color`), but
    the fill color is a clean, unambiguous signal - and since every row has
    exactly one badge, the bands double as row boundaries for map-name OCR.
    """
    w, h = img.size
    xs = list(range(int(w * 0.86), w, 4))
    px = img.load()
    bands = []
    band_color = None
    band_start = 0
    for y in range(h):
        votes: Counter = Counter(c for x in xs if (c := _classify_badge_color(px[x, y])))
        top = votes.most_common(1)
        color = top[0][0] if top and top[0][1] >= 3 else None
        if color != band_color:
            if band_color is not None and y - band_start >= 15:
                bands.append((band_start, y, band_color))
            band_color = color
            band_start = y
    if band_color is not None and h - band_start >= 15:
        bands.append((band_start, h, band_color))
    return bands


def _find_map(text: str) -> str | None:
    t = text.lower()
    # Longest match first so "watchpoint: gibraltar" beats "gibraltar"
    for name in sorted(KNOWN_MAPS, key=len, reverse=True):
        if name in t:
            raw = text[t.index(name): t.index(name) + len(name)]
            return _normalize_map(raw)
    return None


def _find_result(text: str) -> str | None:
    t = text.upper()
    if "VICTORY" in t or re.search(r"\bWIN\b", t):
        return "win"
    if "DEFEAT" in t or re.search(r"\bLOSS\b", t) or re.search(r"\bLOSE\b", t):
        return "loss"
    if "DRAW" in t:
        return "draw"
    return None


def _is_play_of_game(text: str) -> bool:
    return "PLAY OF THE GAME" in text.upper() or "POTG" in text.upper()


class ScreenReader:
    def __init__(self, battletag: str):
        self.battletag = battletag
        self.player_id: int | None = None

        self._map_candidate: str | None = None
        self._map_hits = 0
        self._result_candidate: str | None = None
        self._result_hits = 0
        self._last_logged = 0.0
        self._in_potg = False

    async def _resolve_player(self):
        players = await get_players()
        row = next((p for p in players if p["battletag"] == self.battletag), None)
        if not row:
            print(f"[ERROR] Player {self.battletag!r} not found. Add them first with 'add'.")
            sys.exit(1)
        self.player_id = row["id"]
        print(f"[OK] Watching for {self.battletag} (id={self.player_id})")

    def _capture(self) -> Image.Image:
        with mss.mss() as sct:
            mon = sct.monitors[MONITOR_INDEX]
            raw = sct.grab(mon)
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def _capture_region(self, top_frac: float, bot_frac: float) -> Image.Image:
        with mss.mss() as sct:
            mon = sct.monitors[MONITOR_INDEX]
            h = mon["height"]
            region = {
                "top":    mon["top"] + int(h * top_frac),
                "left":   mon["left"],
                "width":  mon["width"],
                "height": int(h * (bot_frac - top_frac)),
                "mon":    MONITOR_INDEX,
            }
            raw = sct.grab(region)
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    async def _log_match(self, map_name: str, result: str):
        if time.time() - self._last_logged < COOLDOWN_SEC:
            return
        gid = await save_game_log(self.player_id, map_name, result, None, "competitive", [], None)
        self._last_logged = time.time()
        ts = datetime.now().strftime("%H:%M:%S")
        color = "WIN" if result == "win" else "LOSS" if result == "loss" else "DRAW"
        print(f"[{ts}] ✓ Logged game #{gid}: {color} on {map_name}")
        # Reset state
        self._map_candidate = None
        self._map_hits = 0
        self._result_candidate = None
        self._result_hits = 0

    async def tick(self):
        # Loading screen: top 40% of screen has the map name
        loading_img = self._capture_region(0.0, 0.40)
        loading_text = _ocr(loading_img)

        # Result screen: top 30% has VICTORY / DEFEAT
        result_img = self._capture_region(0.10, 0.40)
        result_text = _ocr(result_img)

        # Skip if we're on play-of-the-game (false positive for result)
        full_text = loading_text + " " + result_text
        if _is_play_of_game(full_text):
            self._in_potg = True
            return
        if self._in_potg:
            # wait until POTG is gone
            self._in_potg = False
            return

        # ── Map detection ──────────────────────────────────────────────────
        found_map = _find_map(loading_text)
        if found_map:
            if found_map == self._map_candidate:
                self._map_hits += 1
            else:
                self._map_candidate = found_map
                self._map_hits = 1
        else:
            if self._map_hits > 0:
                self._map_hits = max(0, self._map_hits - 1)
            if self._map_hits == 0:
                self._map_candidate = None

        # ── Result detection ───────────────────────────────────────────────
        found_result = _find_result(result_text)
        if found_result:
            if found_result == self._result_candidate:
                self._result_hits += 1
            else:
                self._result_candidate = found_result
                self._result_hits = 1
        else:
            self._result_hits = max(0, self._result_hits - 1)
            if self._result_hits == 0:
                self._result_candidate = None

        # ── Log when both confirmed ────────────────────────────────────────
        if (
            self._map_candidate
            and self._map_hits >= CONFIRM_FRAMES
            and self._result_candidate
            and self._result_hits >= CONFIRM_FRAMES
        ):
            await self._log_match(self._map_candidate, self._result_candidate)

        # Debug line (muted — uncomment to troubleshoot)
        # ts = datetime.now().strftime("%H:%M:%S")
        # print(f"[{ts}] map={self._map_candidate}({self._map_hits}) result={self._result_candidate}({self._result_hits})")

    async def run(self):
        await init_db()
        await self._resolve_player()
        print(f"[screen_reader] Polling every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n")

        while True:
            try:
                await self.tick()
            except Exception as e:
                print(f"[WARN] tick error: {e}")
            await asyncio.sleep(POLL_INTERVAL)


def capture_full(monitor_index: int | None = None) -> Image.Image:
    """Grab the whole monitor - used for the History/Replays list screen,
    which shows many matches at once rather than one loading/result screen."""
    with mss.mss() as sct:
        mon = sct.monitors[monitor_index or MONITOR_INDEX]
        raw = sct.grab(mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


_ROW_TESS_CONFIG = "--oem 3 --psm 6"


def parse_history_rows(img: Image.Image) -> list[dict]:
    """Parse the History/Replays screen into match rows.

    Running OCR on the whole screen in one shot lets Tesseract's automatic
    page segmentation (`--psm 3`) get confused by the busy layout and drop
    rows unpredictably. Instead, find each row's bounds from its badge (see
    `_find_badge_bands`) and OCR just that row's narrow map-name crop.
    """
    rows = []
    pad = 6
    x0, x1 = int(img.width * 0.22), int(img.width * 0.36)
    for y0, y1, result in _find_badge_bands(img):
        row_crop = img.crop((x0, max(0, y0 - pad), x1, min(img.height, y1 + pad)))
        text = _ocr(row_crop, config=_ROW_TESS_CONFIG)
        found_map = _find_map(text)
        if found_map:
            rows.append({"map": found_map, "result": result})
    return rows


async def scan_history_screen(battletag: str, monitor_index: int | None = None) -> list[dict]:
    """One-shot scan of the currently displayed History/Replays screen.

    Returns parsed rows (most recent first, as shown on screen), each tagged
    with 'likely_duplicate' if that many (map, result) pairs are already in
    the game log - a best-effort dedup hint since OCR can't read timestamps
    reliably, not a guarantee.
    """
    img = capture_full(monitor_index)
    rows = parse_history_rows(img)

    existing = await get_recent_games(battletag, limit=100)
    existing_counts = Counter((r["map"], r["result"]) for r in existing)
    seen_counts: Counter = Counter()
    for row in rows:
        key = (row["map"], row["result"])
        seen_counts[key] += 1
        row["likely_duplicate"] = seen_counts[key] <= existing_counts.get(key, 0)

    return rows


async def _main(battletag: str):
    reader = ScreenReader(battletag)
    await reader.run()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python screen_reader.py YourName-1234")
        sys.exit(1)
    try:
        asyncio.run(_main(sys.argv[1]))
    except KeyboardInterrupt:
        print("\n[screen_reader] Stopped.")
