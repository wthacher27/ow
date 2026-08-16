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
    "ilios", "lijiang tower", "nepal", "oasis", "samoa", "antarctic peninsula",
    # Escort
    "dorado", "havana", "junkertown", "rialto", "route 66",
    "watchpoint: gibraltar", "watchpoint gibraltar", "shambali monastery", "paraíso", "paraiso",
    # Hybrid
    "blizzard world", "eichenwalde", "hollywood", "king's row", "kings row",
    "midtown", "numbani",
    # Push
    "colosseo", "esperança", "esperanca", "new queen street", "runasapi", "hanaoka",
    # Flashpoint
    "new junk city", "suravasa",
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


def _ocr(img: Image.Image, region=None) -> str:
    if region:
        img = img.crop(region)
    processed = _preprocess(img)
    cfg = "--oem 3 --psm 3"
    return pytesseract.image_to_string(processed, config=cfg)


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


def parse_history_rows(text: str) -> list[dict]:
    """Parse OCR text from the History/Replays screen into match rows.

    Each row normally shows a map name and a result on the same line; if OCR
    splits a row across two lines (icon/spacing artifacts), fall back to a
    2-line window before giving up on that line.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    rows = []
    i = 0
    while i < len(lines):
        found_map = _find_map(lines[i])
        found_result = _find_result(lines[i])
        consumed = 1
        if not (found_map and found_result) and i + 1 < len(lines):
            window = lines[i] + " " + lines[i + 1]
            fm = _find_map(window)
            fr = _find_result(window)
            if fm and fr:
                found_map, found_result, consumed = fm, fr, 2
        if found_map and found_result:
            rows.append({"map": found_map, "result": found_result})
            i += consumed
        else:
            i += 1
    return rows


async def scan_history_screen(battletag: str, monitor_index: int | None = None) -> list[dict]:
    """One-shot scan of the currently displayed History/Replays screen.

    Returns parsed rows (most recent first, as shown on screen), each tagged
    with 'likely_duplicate' if that many (map, result) pairs are already in
    the game log - a best-effort dedup hint since OCR can't read timestamps
    reliably, not a guarantee.
    """
    img = capture_full(monitor_index)
    text = _ocr(img)
    rows = parse_history_rows(text)

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
