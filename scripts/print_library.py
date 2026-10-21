"""Print your Steam library sorted by playtime, to confirm the client works.

    python scripts/print_library.py              top 50 games by playtime
    python scripts/print_library.py --limit 0    every game
    python scripts/print_library.py --refresh    ignore the cache and re-fetch
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Allow running from a checkout without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.table import Table  # noqa: E402

from steam_buddy.cache import Cache  # noqa: E402
from steam_buddy.config import ConfigError, load_config  # noqa: E402
from steam_buddy.steam_client import (  # noqa: E402
    ProfilePrivateError,
    SteamAPIError,
    SteamClient,
    library_warnings,
)

console = Console()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print your Steam library sorted by playtime.")
    parser.add_argument("--limit", type=int, default=50, help="how many games to show (0 = all)")
    parser.add_argument("--refresh", action="store_true", help="re-fetch instead of using the cache")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        console.print("[bold red]Setup isn't complete:[/]")
        for p in e.problems:
            console.print(f"  • {p}")
        return 1

    with Cache(cfg.cache_path) as cache, SteamClient.from_config(cfg, cache=cache) as steam:
        try:
            player = steam.get_player_summary(refresh=args.refresh)
            games = steam.get_owned_games(refresh=args.refresh)
            recent = steam.get_recently_played(refresh=args.refresh)
        except ProfilePrivateError as e:
            console.print(f"[bold yellow]{e}[/]")
            return 2
        except SteamAPIError as e:
            console.print(f"[bold red]Steam request failed:[/] {e}")
            return 1

    if player.get("communityvisibilitystate") != 3:
        console.print("[yellow]Note: your profile isn't fully public, so some data may be missing.[/]")
    for warning in library_warnings(games):
        console.print(f"[bold yellow]{warning}[/]\n")

    games.sort(key=lambda g: g.playtime_forever, reverse=True)
    shown = games if args.limit == 0 else games[: args.limit]

    name = player.get("personaname")
    table = Table(title=f"{escape(name)}'s library, by playtime" if name else "Your library, by playtime")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Game")
    table.add_column("Hours", justify="right")
    table.add_column("Last 2 weeks", justify="right")
    table.add_column("Last played", justify="right", style="dim")
    for i, g in enumerate(shown, 1):
        last = datetime.fromtimestamp(g.last_played).strftime("%Y-%m-%d") if g.last_played else "never"
        recent_hours = f"{g.playtime_2weeks / 60:.1f}" if g.playtime_2weeks else ""
        table.add_row(str(i), escape(g.name), f"{g.hours:,.1f}", recent_hours, last)
    console.print(table)

    total_hours = sum(g.hours for g in games)
    unplayed = sum(1 for g in games if g.playtime_forever == 0)
    console.print(
        f"{len(games)} games, {total_hours:,.0f} hours total, "
        f"{unplayed} never played, {len(recent)} played in the last two weeks."
    )
    if args.limit and len(games) > args.limit:
        console.print(f"[dim]Showing the top {args.limit}. Use --limit 0 to see all.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
