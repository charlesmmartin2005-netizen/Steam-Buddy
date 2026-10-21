"""Print your taste profile.

    python scripts/print_profile.py            uses cached data where fresh
    python scripts/print_profile.py --refresh  re-fetch everything
    python scripts/print_profile.py --json     machine-readable output

The first run fetches tags and achievements for every game you've played
(SteamSpy allows one request per second), so expect a minute or two.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.progress import Progress  # noqa: E402
from rich.table import Table  # noqa: E402

from steam_buddy.cache import Cache  # noqa: E402
from steam_buddy.config import ConfigError, load_config  # noqa: E402
from steam_buddy.profile import TasteProfile, build_profile, gather  # noqa: E402
from steam_buddy.steam_client import ProfilePrivateError, SteamAPIError, SteamClient  # noqa: E402

console = Console()


def _ago(days: int | None) -> str:
    if days is None:
        return "never"
    if days == 0:
        return "today"
    if days < 60:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def _names(games, limit: int = 8) -> str:
    shown = ", ".join(escape(g.name) for g in games[:limit])
    more = len(games) - limit
    return shown + (f" and {more} more" if more > 0 else "") if games else "none"


def render(p: TasteProfile) -> None:
    console.rule("[bold]Your taste profile")
    console.print(
        f"{p.total_games} games, {p.played_games} played, {p.total_hours:,.0f} hours total.\n"
    )

    table = Table(
        title="What you play most",
        title_justify="left",
        caption="Weight: playtime-weighted tag strength (top = 100). Hours: all hours in games with the tag.",
        caption_justify="left",
    )
    table.add_column("Tag")
    table.add_column("Weight", justify="right")
    table.add_column("Hours", justify="right")
    table.add_column("Games", justify="right")
    table.add_column("Mostly from", style="dim")
    best = p.top_tags[0].score if p.top_tags else 1
    for t in p.top_tags[:12]:
        table.add_row(escape(t.tag), f"{100 * t.score / best:.0f}", f"{t.hours:,.0f}", str(t.games), escape(", ".join(t.top_games)))
    console.print(table)

    s = p.session_lean
    console.print(
        f"\n[bold]Session style:[/] {s['lean']} "
        f"({s['short_pct']}% of weighted play in short-run genres, {s['long_pct']}% in long-session genres)."
    )
    console.print(f"[dim]{s['note']}[/]")

    console.print("\n[bold]Recently[/]")
    console.print(f"  Playing: {_names(p.recent_games)}")
    if p.rising_tags:
        console.print(f"  Trending up for you: {escape(', '.join(p.rising_tags))}")

    console.print("\n[bold]Still playing[/]")
    for g in p.active[:8]:
        console.print(f"  {escape(g.name)}: {g.hours:,.1f}h, last played {_ago(g.last_played_days_ago)}")

    console.print("\n[bold]Likely finished[/] (70%+ achievements)")
    for g in p.completed[:8]:
        console.print(f"  {escape(g.name)}: {g.achievement_pct:.0f}% achievements, {g.hours:,.1f}h")
    if not p.completed:
        console.print("  none found")

    console.print("\n[bold]Likely abandoned[/] (under 30% achievements, untouched for 90+ days)")
    for g in p.abandoned[:8]:
        console.print(
            f"  {escape(g.name)}: {g.achievement_pct:.0f}% achievements, {g.hours:,.1f}h, "
            f"last played {_ago(g.last_played_days_ago)}"
        )
    if not p.abandoned:
        console.print("  none found")

    console.print("\n[bold]Bounced off[/] (played under an hour)")
    console.print(f"  {_names(p.bounced, 12)}")
    if p.bounced_tags:
        console.print(f"  Common among them: {escape(', '.join(p.bounced_tags))}")

    console.print(f"\n[bold]Never played:[/] {_names(p.unplayed, 12)}")
    if p.excluded_software:
        console.print(f"[dim]Left out as software: {escape(', '.join(p.excluded_software))}[/]")
    for note in p.notes:
        console.print(f"[dim]Note: {escape(note)}[/]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print your Steam taste profile.")
    parser.add_argument("--refresh", action="store_true", help="re-fetch instead of using the cache")
    parser.add_argument("--json", action="store_true", help="print the profile as JSON")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        console.print("[bold red]Setup isn't complete:[/]")
        for problem in e.problems:
            console.print(f"  • {problem}")
        return 1

    with Cache(cfg.cache_path) as cache, SteamClient.from_config(cfg, cache=cache) as steam:
        try:
            with Progress(console=console, transient=True, disable=args.json) as bar:
                task = bar.add_task("Loading your library", total=None)

                def progress(stage: str, i: int, n: int) -> None:
                    bar.update(task, description=f"Fetching {stage}", completed=i, total=n)

                data = gather(steam, refresh=args.refresh, progress=progress)
        except ProfilePrivateError as e:
            console.print(f"[bold yellow]{e}[/]")
            return 2
        except SteamAPIError as e:
            console.print(f"[bold red]Steam request failed:[/] {e}")
            return 1

    profile = build_profile(data)
    if args.json:
        print(json.dumps(profile.to_dict(), indent=2))
    else:
        render(profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
