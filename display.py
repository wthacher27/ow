import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from api import extract_hero_stats
from database import (
    get_snapshots, get_rank_history, get_hero_history, get_players,
    get_map_stats, get_teammate_stats, get_recent_games,
)

console = Console()

RANK_COLORS = {
    "bronze": "orange3",
    "silver": "grey70",
    "gold": "yellow",
    "platinum": "cyan",
    "diamond": "bright_cyan",
    "master": "bright_yellow",
    "grandmaster": "bright_white",
    "champion": "bright_magenta",
}


def rank_color(division: str | None) -> str:
    if not division:
        return "white"
    return RANK_COLORS.get(division.lower(), "white")


async def show_players():
    players = await get_players()
    if not players:
        console.print("[yellow]No players tracked yet. Use 'add' to add a player.[/yellow]")
        return

    table = Table(title="Tracked Players")
    table.add_column("BattleTag", style="cyan")
    table.add_column("Added", style="dim")

    for p in players:
        table.add_row(p["battletag"], p["added_at"])

    console.print(table)


async def show_ranks(battletag: str):
    history = await get_rank_history(battletag, limit=30)
    if not history:
        console.print(f"[yellow]No rank history for {battletag}[/yellow]")
        return

    table = Table(title=f"Rank History — {battletag}")
    table.add_column("Time", style="dim")
    table.add_column("Role", style="bold")
    table.add_column("Rank")

    for r in history:
        color = rank_color(r["division"])
        rank_str = r["rank_text"] or "Unranked"
        table.add_row(r["timestamp"], r["role"].capitalize(), f"[{color}]{rank_str}[/{color}]")

    console.print(table)


async def show_overview(battletag: str):
    snapshots = await get_snapshots(battletag, limit=1)
    if not snapshots:
        console.print(f"[yellow]No data for {battletag}. Run 'track' first.[/yellow]")
        return

    snap = snapshots[0]
    summary = json.loads(snap["summary_json"]) if snap["summary_json"] else {}

    username = summary.get("username", battletag)
    title = summary.get("title", "")
    endorsement = summary.get("endorsement", {}).get("level", "?")

    info_lines = [
        f"[bold cyan]{username}[/bold cyan]",
        f"Title: {title}" if title else "",
        f"Endorsement: {'★' * int(endorsement) if str(endorsement).isdigit() else endorsement}",
        f"Snapshot: {snap['timestamp']}",
    ]
    console.print(Panel("\n".join(line for line in info_lines if line), title="Player Overview"))

    competitive = summary.get("competitive") or {}
    platform_data = competitive.get("pc") or competitive.get("console") or {}
    if platform_data:
        rank_table = Table(title="Current Competitive Ranks")
        rank_table.add_column("Role", style="bold")
        rank_table.add_column("Rank")

        season = platform_data.get("season")
        if season:
            console.print(f"[dim]Season {season}[/dim]")

        for role in ("tank", "damage", "support"):
            data = platform_data.get(role)
            if data:
                div = data.get("division", "")
                tier = data.get("tier", "")
                color = rank_color(div)
                rank_table.add_row(
                    role.capitalize(),
                    f"[{color}]{div} {tier}[/{color}]",
                )

        console.print(rank_table)

    # Show top heroes from last snapshot
    comp_stats = json.loads(snap["competitive_json"]) if snap.get("competitive_json") else {}
    if comp_stats:
        heroes = extract_hero_stats(comp_stats)
        if heroes:
            heroes.sort(key=lambda h: h["time_played_seconds"], reverse=True)
            hero_table = Table(title="Most Played (Competitive)")
            hero_table.add_column("Hero", style="cyan")
            hero_table.add_column("Time", justify="right")
            hero_table.add_column("W/L", justify="right")
            for h in heroes[:5]:
                hrs = h["time_played_seconds"] // 3600
                mins = (h["time_played_seconds"] % 3600) // 60
                time_str = f"{hrs}h {mins}m" if hrs else f"{mins}m"
                wl = f"{h['games_won']}/{h.get('games_played', 0) - h['games_won']}"
                hero_table.add_row(h["hero"].replace("-", " ").title(), time_str, wl)
            console.print(hero_table)


async def show_heroes(battletag: str, mode: str = "competitive", limit: int = 10):
    snapshots = await get_snapshots(battletag, limit=1)
    if not snapshots:
        console.print(f"[yellow]No data for {battletag}[/yellow]")
        return

    snap = snapshots[0]
    key = f"{mode}_json"
    stats = json.loads(snap[key]) if snap.get(key) else {}

    heroes = extract_hero_stats(stats)
    if not heroes:
        console.print(f"[yellow]No {mode} hero stats for {battletag}[/yellow]")
        return

    heroes.sort(key=lambda h: h["time_played_seconds"], reverse=True)

    table = Table(title=f"Top Heroes ({mode.capitalize()}) — {battletag}")
    table.add_column("Hero", style="cyan")
    table.add_column("Time", justify="right")
    table.add_column("Games", justify="right")
    table.add_column("Wins", justify="right", style="green")
    table.add_column("Elims", justify="right")
    table.add_column("Deaths", justify="right")
    table.add_column("K/D", justify="right")
    table.add_column("Acc%", justify="right", style="yellow")
    table.add_column("Damage", justify="right", style="red")
    table.add_column("Healing", justify="right", style="green")

    total_elims = total_deaths = 0

    for hero in heroes[:limit]:
        hours = hero["time_played_seconds"] // 3600
        minutes = (hero["time_played_seconds"] % 3600) // 60
        time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"

        kd = f"{hero['eliminations'] / hero['deaths']:.2f}" if hero["deaths"] > 0 else "∞"
        acc = f"{hero['accuracy']}%" if hero.get("accuracy") is not None else "—"

        total_elims += hero["eliminations"]
        total_deaths += hero["deaths"]

        table.add_row(
            hero["hero"].replace("-", " ").title(),
            time_str,
            str(hero.get("games_played", 0)),
            str(hero["games_won"]),
            str(hero["eliminations"]),
            str(hero["deaths"]),
            kd,
            acc,
            f"{hero['damage']:,}",
            f"{hero['healing']:,}",
        )

    if len(heroes) > 1:
        avg_kd = f"{total_elims / total_deaths:.2f}" if total_deaths > 0 else "∞"
        table.add_section()
        table.add_row("[dim]Avg K/D[/dim]", "", "", "", "", "", f"[bold]{avg_kd}[/bold]", "", "", "")

    console.print(table)


async def show_hero_trend(battletag: str, hero: str, mode: str = "competitive"):
    history = await get_hero_history(battletag, hero, mode, limit=20)
    if not history:
        console.print(f"[yellow]No history for {hero} ({mode}) — {battletag}[/yellow]")
        return

    table = Table(title=f"{hero.replace('-', ' ').title()} Trend ({mode.capitalize()}) — {battletag}")
    table.add_column("Snapshot", style="dim")
    table.add_column("Wins", justify="right", style="green")
    table.add_column("Elims", justify="right")
    table.add_column("Deaths", justify="right")
    table.add_column("K/D", justify="right")
    table.add_column("Damage", justify="right", style="red")
    table.add_column("Healing", justify="right", style="green")

    for row in reversed(history):
        kd = f"{row['eliminations'] / row['deaths']:.2f}" if row["deaths"] > 0 else "∞"
        table.add_row(
            row["timestamp"],
            str(row["games_won"]),
            str(row["eliminations"]),
            str(row["deaths"]),
            kd,
            f"{row['damage']:,}",
            f"{row['healing']:,}",
        )

    console.print(table)


async def show_rank_trend(battletag: str, role: str):
    history = await get_rank_history(battletag, role=role, limit=30)
    if not history:
        console.print(f"[yellow]No rank history for {role} — {battletag}[/yellow]")
        return

    table = Table(title=f"{role.capitalize()} Rank Trend — {battletag}")
    table.add_column("Time", style="dim")
    table.add_column("Rank")

    prev_rank = None
    for r in reversed(history):
        color = rank_color(r["division"])
        rank_str = r["rank_text"] or "Unranked"
        arrow = ""
        if prev_rank and prev_rank != rank_str:
            arrow = " [green]▲[/green]" if _rank_value(r) > _rank_value_from_text(prev_rank) else " [red]▼[/red]"
        table.add_row(r["timestamp"], f"[{color}]{rank_str}[/{color}]{arrow}")
        prev_rank = rank_str

    console.print(table)


RANK_ORDER = ["bronze", "silver", "gold", "platinum", "diamond", "master", "grandmaster", "champion"]


def _rank_value(r: dict) -> int:
    div = (r.get("division") or "").lower()
    tier = r.get("tier") or 5
    div_val = RANK_ORDER.index(div) * 5 if div in RANK_ORDER else 0
    return div_val + (5 - tier)


async def show_map_stats(battletag: str, mode: str | None = None):
    rows = await get_map_stats(battletag, mode)
    if not rows:
        console.print(f"[yellow]No game logs for {battletag}. Use 'log-game' to record games.[/yellow]")
        return

    title = f"Map W/L — {battletag}"
    if mode:
        title += f" ({mode})"

    table = Table(title=title)
    table.add_column("Map", style="cyan")
    table.add_column("Games", justify="right")
    table.add_column("W", justify="right", style="green")
    table.add_column("L", justify="right", style="red")
    table.add_column("D", justify="right", style="dim")
    table.add_column("Win%", justify="right")

    for r in rows:
        wr = r["winrate"] or 0
        color = "green" if wr >= 55 else "red" if wr <= 45 else "yellow"
        table.add_row(
            r["map"],
            str(r["games"]),
            str(r["wins"]),
            str(r["losses"]),
            str(r["draws"]),
            f"[{color}]{wr}%[/{color}]",
        )

    console.print(table)


async def show_teammate_stats(battletag: str):
    rows = await get_teammate_stats(battletag)
    if not rows:
        console.print(f"[yellow]No teammate data for {battletag}. Use 'log-game --teammates' to record games.[/yellow]")
        return

    table = Table(title=f"Teammate W/L — {battletag}")
    table.add_column("Teammate", style="cyan")
    table.add_column("Games", justify="right")
    table.add_column("W", justify="right", style="green")
    table.add_column("L", justify="right", style="red")
    table.add_column("D", justify="right", style="dim")
    table.add_column("Win%", justify="right")

    for r in rows:
        wr = r["winrate"] or 0
        color = "green" if wr >= 55 else "red" if wr <= 45 else "yellow"
        table.add_row(
            r["teammate"],
            str(r["games"]),
            str(r["wins"]),
            str(r["losses"]),
            str(r["draws"]),
            f"[{color}]{wr}%[/{color}]",
        )

    console.print(table)


async def show_recent_games(battletag: str, limit: int = 20):
    rows = await get_recent_games(battletag, limit)
    if not rows:
        console.print(f"[yellow]No game logs for {battletag}.[/yellow]")
        return

    table = Table(title=f"Recent Games — {battletag}")
    table.add_column("Time", style="dim")
    table.add_column("Map", style="cyan")
    table.add_column("Result")
    table.add_column("Hero")
    table.add_column("Mode", style="dim")
    table.add_column("Teammates", style="dim")

    for r in rows:
        result = r["result"]
        color = "green" if result == "win" else "red" if result == "loss" else "yellow"
        table.add_row(
            r["timestamp"],
            r["map"],
            f"[{color}]{result.upper()}[/{color}]",
            (r["hero"] or "—").replace("-", " ").title(),
            r["mode"],
            r["teammates"] or "—",
        )

    console.print(table)


def _rank_value_from_text(text: str) -> int:
    parts = text.lower().split()
    if len(parts) == 2:
        div = parts[0]
        try:
            tier = int(parts[1])
        except ValueError:
            tier = 5
        div_val = RANK_ORDER.index(div) * 5 if div in RANK_ORDER else 0
        return div_val + (5 - tier)
    return 0
