"""Command-line entry point.

    python -m steam_buddy.main --check    validate .env (Phase 1)
    python -m steam_buddy.main            interactive chat (Phase 4)
    python -m steam_buddy.main --digest   weekly summary (Phase 6)
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from .config import ConfigError, load_config

console = Console()


def check_setup() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        console.print("[bold red]Setup isn't complete yet:[/]")
        for problem in e.problems:
            console.print(f"  • {problem}")
        console.print("\nCopy [cyan].env.example[/] to [cyan].env[/] and fill it in.")
        return 1

    console.print("[bold green]Config looks good.[/]")
    console.print(f"  SteamID64:     {cfg.steam_id}")
    console.print("  Steam key:     set (hidden)")
    console.print(f"  Anthropic key: {'set (hidden)' if cfg.anthropic_api_key else '[yellow]not set (needed from Phase 4)[/]'}")
    console.print(f"  Model:         {cfg.anthropic_model}")
    console.print(f"  Cache:         {cfg.cache_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steam-buddy", description="Your personal Steam gaming agent.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate your .env setup")
    group.add_argument("--digest", action="store_true", help="print a weekly summary")
    args = parser.parse_args(argv)

    if args.check:
        return check_setup()
    if args.digest:
        console.print("[yellow]The weekly digest arrives in Phase 6.[/]")
        return 0
    console.print("[yellow]Chat mode arrives in Phase 4. For now, run with --check.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
