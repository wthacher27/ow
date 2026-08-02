import asyncio
import httpx
from datetime import datetime

from api import fetch_player_summary, fetch_player_stats, extract_ranks, extract_hero_stats
from database import (
    add_player, save_snapshot, save_ranks, save_hero_stats, get_players,
)

from rich.console import Console

console = Console()


async def track_player(client: httpx.AsyncClient, battletag: str) -> dict | None:
    summary = await fetch_player_summary(client, battletag)
    if not summary:
        console.print(f"  [red]Could not fetch summary for {battletag}[/red]")
        return None

    comp_stats = await fetch_player_stats(client, battletag, "competitive")
    qp_stats = await fetch_player_stats(client, battletag, "quickplay")

    player_id = await add_player(battletag)

    snapshot_id = await save_snapshot(
        player_id,
        summary=summary,
        competitive=comp_stats or {},
        quickplay=qp_stats or {},
    )

    ranks = extract_ranks(summary)
    if ranks:
        await save_ranks(player_id, ranks)

    if comp_stats:
        heroes = extract_hero_stats(comp_stats)
        if heroes:
            await save_hero_stats(snapshot_id, "competitive", heroes)

    if qp_stats:
        heroes = extract_hero_stats(qp_stats)
        if heroes:
            await save_hero_stats(snapshot_id, "quickplay", heroes)

    return {
        "battletag": battletag,
        "username": summary.get("username", battletag),
        "title": summary.get("title"),
        "endorsement": summary.get("endorsement", {}).get("level"),
        "ranks": ranks,
        "comp_heroes": len(extract_hero_stats(comp_stats)) if comp_stats else 0,
        "qp_heroes": len(extract_hero_stats(qp_stats)) if qp_stats else 0,
    }


async def track_all_players(battletags: list[str] | None = None) -> list[dict]:
    if battletags is None:
        players = await get_players()
        battletags = [p["battletag"] for p in players]

    if not battletags:
        console.print("[yellow]No players to track. Add players first.[/yellow]")
        return []

    console.print(f"[cyan]Tracking {len(battletags)} player(s) concurrently...[/cyan]")

    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        tasks = [track_player(client, tag) for tag in battletags]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for tag, result in zip(battletags, completed):
            if isinstance(result, Exception):
                console.print(f"  [red]Error tracking {tag}: {result}[/red]")
            elif result:
                results.append(result)
                console.print(f"  [green]✓ {result['username']}[/green]")
            else:
                console.print(f"  [yellow]⚠ No data for {tag}[/yellow]")

    return results


async def auto_track(interval_minutes: int = 30):
    console.print(f"[bold cyan]Auto-tracking every {interval_minutes} minutes. Press Ctrl+C to stop.[/bold cyan]")
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.print(f"\n[dim]── Snapshot at {now} ──[/dim]")
        await track_all_players()
        await asyncio.sleep(interval_minutes * 60)
