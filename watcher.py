"""
Watch a player's career stats and auto-detect match results by comparing
consecutive snapshots from the OverFast API.

The API scrapes Blizzard's career profile and can lag 5-15 min after
a game ends, so a 2-min poll interval is fine without hammering the server.
"""

import asyncio
import httpx
from datetime import datetime

from api import fetch_stats_summary, fetch_player_stats, extract_hero_stats
from database import add_player, save_snapshot, save_ranks, save_game_log, get_players
from api import fetch_player_summary, extract_ranks

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


def _hero_totals(stats_data: dict) -> dict[str, dict]:
    """Return {hero: {games_played, games_won, time_played}} from /stats response."""
    result = {}
    for hero_name, categories in stats_data.items():
        if hero_name == "all-heroes" or not isinstance(categories, list):
            continue
        gp = gw = tp = 0
        for cat in categories:
            if cat.get("category") == "game":
                for s in cat.get("stats", []):
                    if s["key"] == "games_played":
                        gp = s["value"]
                    elif s["key"] == "games_won":
                        gw = s["value"]
                    elif s["key"] == "time_played":
                        tp = s["value"]
        result[hero_name] = {"games_played": gp, "games_won": gw, "time_played": tp}
    return result


def _sum_games(hero_totals: dict[str, dict]) -> tuple[int, int]:
    """Sum total games_played and games_won across all heroes."""
    gp = sum(v["games_played"] for v in hero_totals.values())
    gw = sum(v["games_won"] for v in hero_totals.values())
    return gp, gw


def detect_matches(
    prev_comp: dict, curr_comp: dict,
    prev_qp: dict, curr_qp: dict,
) -> list[dict]:
    """
    Compare before/after stats and return a list of detected match dicts.
    Each match: {mode, result, hero, games_played_delta, kd_delta}
    """
    matches = []

    for mode, prev, curr in [("competitive", prev_comp, curr_comp), ("quickplay", prev_qp, curr_qp)]:
        if not prev or not curr:
            continue

        prev_totals = _hero_totals(prev)
        curr_totals = _hero_totals(curr)

        prev_gp, prev_gw = _sum_games(prev_totals)
        curr_gp, curr_gw = _sum_games(curr_totals)

        games_delta = curr_gp - prev_gp
        wins_delta = curr_gw - prev_gw
        if games_delta <= 0:
            continue

        # Which hero was played? Largest time_played increase.
        hero_deltas = []
        all_heroes = set(prev_totals) | set(curr_totals)
        for hero in all_heroes:
            prev_tp = prev_totals.get(hero, {}).get("time_played", 0)
            curr_tp = curr_totals.get(hero, {}).get("time_played", 0)
            prev_hgp = prev_totals.get(hero, {}).get("games_played", 0)
            curr_hgp = curr_totals.get(hero, {}).get("games_played", 0)
            if curr_tp > prev_tp or curr_hgp > prev_hgp:
                hero_deltas.append((hero, curr_tp - prev_tp, curr_hgp - prev_hgp))

        hero_deltas.sort(key=lambda x: (x[2], x[1]), reverse=True)
        top_hero = hero_deltas[0][0] if hero_deltas else None

        for i in range(games_delta):
            # For multiple games in one delta, only the last one's result
            # is reliably known. Distribute wins first.
            if i < wins_delta:
                result = "win"
            elif i < games_delta - max(0, games_delta - wins_delta - (games_delta - wins_delta)):
                result = "draw"
            else:
                result = "loss"

        # Single summary entry for the whole delta batch
        losses_delta = games_delta - wins_delta
        if games_delta == 1:
            result = "win" if wins_delta == 1 else "loss"
        else:
            result = f"{wins_delta}W / {losses_delta}L"

        matches.append({
            "mode": mode,
            "games_delta": games_delta,
            "wins_delta": wins_delta,
            "losses_delta": losses_delta,
            "hero": top_hero,
            "result": "win" if wins_delta == games_delta else ("loss" if wins_delta == 0 else "mixed"),
            "result_label": result,
        })

    return matches


async def _log_matches(player_id: int, matches: list[dict]):
    for m in matches:
        if m["games_delta"] == 1:
            result = "win" if m["wins_delta"] == 1 else "loss"
            await save_game_log(player_id, "unknown", result, m["hero"], m["mode"], [], None)
        else:
            # Log each win/loss separately as best we can
            for _ in range(m["wins_delta"]):
                await save_game_log(player_id, "unknown", "win", m["hero"], m["mode"], [], None)
            for _ in range(m["losses_delta"]):
                await save_game_log(player_id, "unknown", "loss", m["hero"], m["mode"], [], None)


def _display_match(battletag: str, match: dict, timestamp: str):
    mode_color = "cyan" if match["mode"] == "competitive" else "blue"
    result_color = "green" if match["result"] == "win" else "red" if match["result"] == "loss" else "yellow"
    hero = (match["hero"] or "unknown").replace("-", " ").title()

    lines = [
        f"[{mode_color}]{match['mode'].upper()}[/{mode_color}]  "
        f"[{result_color}]{match['result_label']}[/{result_color}]  "
        f"Hero: [bold]{hero}[/bold]",
        f"[dim]{timestamp} — map logged as 'unknown', update with log-game[/dim]",
    ]
    console.print(Panel("\n".join(lines), title=f"[bold]Match Detected — {battletag}[/bold]"))


async def watch_player(
    battletag: str,
    interval_seconds: int = 120,
    mode: str = "both",
):
    console.print(
        f"[bold cyan]Watching {battletag} every {interval_seconds}s. "
        f"Press Ctrl+C to stop.[/bold cyan]"
    )
    console.print("[dim]Stats update 5-15 min after a match ends.[/dim]\n")

    players = await get_players()
    player_row = next((p for p in players if p["battletag"] == battletag), None)
    if not player_row:
        console.print(f"[red]Player {battletag} not found. Add them first.[/red]")
        return

    player_id = player_row["id"]

    async with httpx.AsyncClient(timeout=60) as client:
        # Baseline
        console.print("[dim]Fetching baseline stats...[/dim]")
        prev_comp = await fetch_player_stats(client, battletag, "competitive") or {}
        prev_qp = await fetch_player_stats(client, battletag, "quickplay") or {}
        console.print("[green]Baseline saved. Watching for new matches...[/green]\n")

        while True:
            await asyncio.sleep(interval_seconds)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            try:
                curr_comp = await fetch_player_stats(client, battletag, "competitive") or {}
                curr_qp = await fetch_player_stats(client, battletag, "quickplay") or {}

                matches = detect_matches(prev_comp, curr_comp, prev_qp, curr_qp)

                if matches:
                    # Full snapshot
                    summary = await fetch_player_summary(client, battletag)
                    if summary:
                        pid = await add_player(battletag)
                        snap_id = await save_snapshot(pid, summary, curr_comp, curr_qp)
                        ranks = extract_ranks(summary)
                        if ranks:
                            await save_ranks(pid, ranks)

                    await _log_matches(player_id, matches)

                    for m in matches:
                        _display_match(battletag, m, now)

                    prev_comp = curr_comp
                    prev_qp = curr_qp
                else:
                    console.print(f"[dim]{now} — no new matches[/dim]")

            except Exception as e:
                console.print(f"[yellow]{now} — poll error: {e}[/yellow]")


async def watch_all(interval_seconds: int = 120):
    players = await get_players()
    if not players:
        console.print("[yellow]No players tracked. Add one first.[/yellow]")
        return

    console.print(
        f"[bold cyan]Watching {len(players)} player(s) every {interval_seconds}s. "
        f"Ctrl+C to stop.[/bold cyan]\n"
    )

    async with httpx.AsyncClient(timeout=60) as client:
        baselines: dict[str, tuple[dict, dict]] = {}
        for p in players:
            tag = p["battletag"]
            comp = await fetch_player_stats(client, tag, "competitive") or {}
            qp = await fetch_player_stats(client, tag, "quickplay") or {}
            baselines[tag] = (comp, qp)
            console.print(f"[dim]Baseline: {tag}[/dim]")

        console.print("[green]All baselines ready. Watching...[/green]\n")

        while True:
            await asyncio.sleep(interval_seconds)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            any_match = False

            tasks = {
                p["battletag"]: (
                    fetch_player_stats(client, p["battletag"], "competitive"),
                    fetch_player_stats(client, p["battletag"], "quickplay"),
                )
                for p in players
            }

            for tag, (comp_coro, qp_coro) in tasks.items():
                try:
                    curr_comp, curr_qp = await asyncio.gather(comp_coro, qp_coro)
                    curr_comp = curr_comp or {}
                    curr_qp = curr_qp or {}
                    prev_comp, prev_qp = baselines[tag]

                    matches = detect_matches(prev_comp, curr_comp, prev_qp, curr_qp)
                    if matches:
                        any_match = True
                        player_row = next((p for p in players if p["battletag"] == tag), None)
                        if player_row:
                            await _log_matches(player_row["id"], matches)
                        for m in matches:
                            _display_match(tag, m, now)
                        baselines[tag] = (curr_comp, curr_qp)

                except Exception as e:
                    console.print(f"[yellow]{now} — {tag} error: {e}[/yellow]")

            if not any_match:
                console.print(f"[dim]{now} — no new matches[/dim]")
