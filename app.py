#!/usr/bin/env python3
import asyncio
import click
import httpx
from rich.console import Console
from rich.table import Table

from database import init_db, add_player, remove_player
from tracker import track_all_players, auto_track
from api import search_players
from display import (
    show_players, show_ranks, show_overview,
    show_heroes, show_hero_trend, show_rank_trend,
)

console = Console()


def run(coro):
    return asyncio.run(coro)


@click.group()
def cli():
    """Overwatch 2 Stat Tracker — track your stats over time."""
    run(init_db())


@cli.command()
@click.argument("name")
def search(name):
    """Search for a player by name to find their player ID."""
    async def _search():
        async with httpx.AsyncClient(timeout=30) as client:
            results = await search_players(client, name)

        if not results:
            console.print(f"[yellow]No players found matching '{name}'[/yellow]")
            return

        table = Table(title=f"Search Results for '{name}'")
        table.add_column("#", style="dim")
        table.add_column("Name", style="cyan")
        table.add_column("Player ID (use this with 'add')", style="green")

        for i, p in enumerate(results[:15], 1):
            table.add_row(str(i), p.get("name", "?"), p.get("player_id", "?"))

        console.print(table)
        console.print("\n[dim]Use the Player ID with the 'add' command.[/dim]")

    run(_search())


@cli.command()
@click.argument("player_id")
def add(player_id):
    """Add a player to track. Use the player ID from 'search'."""
    async def _add():
        pid = await add_player(player_id)
        console.print(f"[green]Added {player_id} (id={pid})[/green]")
    run(_add())


@cli.command()
@click.argument("player_id")
def remove(player_id):
    """Remove a player and all their data."""
    async def _remove():
        ok = await remove_player(player_id)
        if ok:
            console.print(f"[green]Removed {player_id} and all associated data.[/green]")
        else:
            console.print(f"[red]Player {player_id} not found.[/red]")
    run(_remove())


@cli.command()
def players():
    """List all tracked players."""
    run(show_players())


@cli.command()
@click.argument("player_ids", nargs=-1)
def track(player_ids):
    """Fetch and save current stats for players. Tracks all if none specified."""
    async def _track():
        tags = list(player_ids) if player_ids else None
        results = await track_all_players(tags)
        if results:
            console.print(f"\n[green]Saved snapshots for {len(results)} player(s).[/green]")
    run(_track())


@cli.command()
@click.option("--interval", "-i", default=30, help="Minutes between snapshots.")
def autotrack(interval):
    """Continuously track all players at an interval."""
    try:
        run(auto_track(interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Auto-tracking stopped.[/yellow]")


@cli.command()
@click.argument("player_id")
def overview(player_id):
    """Show player overview with current ranks."""
    run(show_overview(player_id))


@cli.command()
@click.argument("player_id")
@click.option("--mode", "-m", default="competitive", type=click.Choice(["competitive", "quickplay"]))
@click.option("--limit", "-n", default=10, help="Number of heroes to show.")
def heroes(player_id, mode, limit):
    """Show top heroes for a player."""
    run(show_heroes(player_id, mode, limit))


@cli.command()
@click.argument("player_id")
def ranks(player_id):
    """Show rank history for a player."""
    run(show_ranks(player_id))


@cli.command()
@click.argument("player_id")
@click.argument("role", type=click.Choice(["tank", "damage", "support"]))
def ranktrend(player_id, role):
    """Show rank trend over time for a specific role."""
    run(show_rank_trend(player_id, role))


@cli.command()
@click.argument("player_id")
@click.argument("hero")
@click.option("--mode", "-m", default="competitive", type=click.Choice(["competitive", "quickplay"]))
def herotrend(player_id, hero, mode):
    """Show stat trend for a specific hero over time."""
    run(show_hero_trend(player_id, hero, mode))


if __name__ == "__main__":
    cli()
