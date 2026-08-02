import aiosqlite
import json
import os
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("OW_DB_PATH", Path(__file__).parent / "ow_stats.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    battletag TEXT UNIQUE NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    summary_json TEXT,
    competitive_json TEXT,
    quickplay_json TEXT
);

CREATE TABLE IF NOT EXISTS rank_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    role TEXT NOT NULL,
    division TEXT,
    tier INTEGER,
    rank_text TEXT
);

CREATE TABLE IF NOT EXISTS hero_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    mode TEXT NOT NULL,
    hero TEXT NOT NULL,
    time_played_seconds INTEGER DEFAULT 0,
    games_won INTEGER DEFAULT 0,
    eliminations INTEGER DEFAULT 0,
    deaths INTEGER DEFAULT 0,
    damage INTEGER DEFAULT 0,
    healing INTEGER DEFAULT 0,
    stats_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_player ON snapshots(player_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_rank_player ON rank_history(player_id);
CREATE INDEX IF NOT EXISTS idx_hero_snapshot ON hero_stats(snapshot_id);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def add_player(battletag: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO players (battletag) VALUES (?)", (battletag,)
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT id FROM players WHERE battletag = ?", (battletag,)
        )
        row = await cursor.fetchone()
        return row[0]


async def remove_player(battletag: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM players WHERE battletag = ?", (battletag,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        player_id = row[0]
        await db.execute(
            "DELETE FROM hero_stats WHERE snapshot_id IN (SELECT id FROM snapshots WHERE player_id = ?)",
            (player_id,),
        )
        await db.execute("DELETE FROM rank_history WHERE player_id = ?", (player_id,))
        await db.execute("DELETE FROM snapshots WHERE player_id = ?", (player_id,))
        await db.execute("DELETE FROM players WHERE id = ?", (player_id,))
        await db.commit()
        return True


async def get_players() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM players ORDER BY battletag")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def save_snapshot(player_id: int, summary: dict, competitive: dict, quickplay: dict) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO snapshots (player_id, summary_json, competitive_json, quickplay_json) VALUES (?, ?, ?, ?)",
            (player_id, json.dumps(summary), json.dumps(competitive), json.dumps(quickplay)),
        )
        snapshot_id = cursor.lastrowid
        await db.commit()
        return snapshot_id


async def save_ranks(player_id: int, ranks: list[dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        for r in ranks:
            await db.execute(
                "INSERT INTO rank_history (player_id, role, division, tier, rank_text) VALUES (?, ?, ?, ?, ?)",
                (player_id, r["role"], r.get("division"), r.get("tier"), r.get("rank_text")),
            )
        await db.commit()


async def save_hero_stats(snapshot_id: int, mode: str, heroes: list[dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        for h in heroes:
            await db.execute(
                """INSERT INTO hero_stats
                   (snapshot_id, mode, hero, time_played_seconds, games_won,
                    eliminations, deaths, damage, healing, stats_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, mode, h["hero"],
                    h.get("time_played_seconds", 0),
                    h.get("games_won", 0),
                    h.get("eliminations", 0),
                    h.get("deaths", 0),
                    h.get("damage", 0),
                    h.get("healing", 0),
                    json.dumps(h.get("extra", {})),
                ),
            )
        await db.commit()


async def get_snapshots(battletag: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT s.* FROM snapshots s
               JOIN players p ON s.player_id = p.id
               WHERE p.battletag = ?
               ORDER BY s.timestamp DESC LIMIT ?""",
            (battletag, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_rank_history(battletag: str, role: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if role:
            cursor = await db.execute(
                """SELECT rh.* FROM rank_history rh
                   JOIN players p ON rh.player_id = p.id
                   WHERE p.battletag = ? AND rh.role = ?
                   ORDER BY rh.timestamp DESC LIMIT ?""",
                (battletag, role, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT rh.* FROM rank_history rh
                   JOIN players p ON rh.player_id = p.id
                   WHERE p.battletag = ?
                   ORDER BY rh.timestamp DESC LIMIT ?""",
                (battletag, limit),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_hero_history(battletag: str, hero: str, mode: str = "competitive", limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT hs.*, s.timestamp FROM hero_stats hs
               JOIN snapshots s ON hs.snapshot_id = s.id
               JOIN players p ON s.player_id = p.id
               WHERE p.battletag = ? AND hs.hero = ? AND hs.mode = ?
               ORDER BY s.timestamp DESC LIMIT ?""",
            (battletag, hero, mode, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
