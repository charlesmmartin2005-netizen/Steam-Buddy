"""Command-line entry point.

    python -m steam_buddy.main            interactive chat
    python -m steam_buddy.main --refresh  chat, re-fetching your account data first
    python -m steam_buddy.main --check    validate .env
    python -m steam_buddy.main --digest   weekly digest (no Claude call, so it's free)
    python -m steam_buddy.main --digest --out digests/{date}.md   save it instead of printing
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

from .cache import Cache
from .config import Config, ConfigError, load_config
from .digest import build_digest, to_markdown
from .steam_client import ProfilePrivateError, SteamAPIError, SteamClient
from .tools import SteamTools

console = Console()

# What the chat shows while each tool runs.
TOOL_LABELS = {
    "get_library_summary": "Looking at your library",
    "get_taste_profile": "Reading your taste profile",
    "get_recently_played": "Checking what you've played lately",
    "get_game_details": "Looking up a game",
    "search_store": "Searching the store",
    "recommend_games": "Scoring games against your taste",
    "find_similar_games": "Finding similar games",
    "find_hidden_gems": "Digging for hidden gems",
    "get_game_news": "Reading the news",
    "get_recent_news": "Reading news for what you've been playing",
    "get_games_to_revisit": "Checking what's changed in games you stopped",
    "get_wishlist": "Checking your wishlist and prices",
    "get_backlog": "Going through your backlog",
}

# Account data that --refresh re-fetches. Store data (tags, details) stays cached for its week.
ACCOUNT_KINDS = ("owned_games", "recent_games", "achievements", "wishlist", "player_summary", "price", "news")

HELP = """Ask anything about your games, for example:
  what should I play tonight?
  what in my backlog fits a 2-hour session?
  what have I abandoned that's worth going back to?
  what's on sale from my wishlist?
  find me hidden gems like my favourite roguelikes
  what should I buy next for under $20?
  what's new in the games I've been playing?

Commands: /reset starts a new conversation, /help shows this, /quit exits."""


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
    console.print(f"  Model:         {cfg.anthropic_model} (effort {cfg.anthropic_effort})")
    console.print(f"  Cache:         {cfg.cache_path}")
    return 0


class _Output:
    """Streams Claude's reply, keeping tool notices on lines of their own."""

    def __init__(self) -> None:
        self.started = False
        self.mid_line = False

    def new_turn(self) -> None:
        self.started = self.mid_line = False

    def text(self, chunk: str) -> None:
        if not self.started:
            console.print("[bold green]buddy>[/] ", end="")
            self.started = True
        console.print(chunk, end="", markup=False, highlight=False, soft_wrap=True)
        self.mid_line = not chunk.endswith("\n")

    def break_line(self) -> None:
        if self.mid_line:
            console.print()
            self.mid_line = False

    def notice(self, markup: str) -> None:
        self.break_line()
        console.print(markup, highlight=False)

    def end_turn(self) -> None:
        console.print("\n" if self.mid_line else "")


def _tool_runner(tools: SteamTools, out: _Output):
    def run(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        label = TOOL_LABELS.get(name, name)
        out.break_line()  # the spinner needs a line of its own
        with console.status(f"[dim]{label}…[/]"):
            result, is_error = tools.run(name, args)
        out.notice(f"[dim]· {label}{' (failed)' if is_error else ''}[/]")
        return result, is_error

    return run


def chat(cfg: Config, *, refresh: bool = False) -> int:
    from .agent import Agent, AgentError  # the Anthropic SDK takes seconds to import; --digest doesn't need it

    with Cache(cfg.cache_path) as cache, SteamClient.from_config(cfg, cache=cache) as steam:
        if refresh:
            _clear_account_data(cache)
        tools = SteamTools(steam)
        out = _Output()
        try:
            agent = Agent.from_config(
                cfg,
                _tool_runner(tools, out),
                on_text=out.text,
                on_fallback=lambda m: out.notice(f"[dim](continuing on {m})[/]"),
            )
        except AgentError as e:
            console.print(f"[bold red]{e}[/]")
            return 1

        console.rule("[bold]Steam Buddy")
        console.print(HELP + "\n", highlight=False)
        console.print("[dim]The first question can take a minute while your library's tags are fetched; "
                      "after that it's cached.[/]\n")
        while True:
            try:
                question = console.input("[bold cyan]you>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            if not question:
                continue
            if question.lower() in ("/quit", "/exit", "/q", "quit", "exit"):
                return 0
            if question.lower() == "/reset":
                agent.reset()
                console.print("[dim]Started a new conversation.[/]\n")
                continue
            if question.lower() == "/help":
                console.print(HELP + "\n", highlight=False)
                continue
            if question.startswith("/"):
                console.print("[dim]Unknown command. Try /help.[/]\n")
                continue

            out.new_turn()
            try:
                reply = agent.ask(question)
            except KeyboardInterrupt:
                out.notice("[dim](stopped; that question was dropped)[/]\n")
                continue
            except AgentError as e:
                out.notice(f"[bold red]{e}[/]\n")
                continue
            if reply.status != "ok":
                out.notice(f"[yellow]{reply.text}[/]")
            elif reply.fallback_model:
                out.notice(f"[dim](answered by {reply.fallback_model})[/]")
            out.end_turn()


def _clear_account_data(cache: Cache) -> None:
    for kind in ACCOUNT_KINDS:
        cache.clear(kind)


def digest(cfg: Config, *, refresh: bool = False, out: str | None = None) -> int:
    """Build the weekly digest and print it, or save it as Markdown when `out` is given."""
    with Cache(cfg.cache_path) as cache, SteamClient.from_config(cfg, cache=cache) as steam:
        if refresh:
            _clear_account_data(cache)
        try:
            with console.status("[dim]Building your digest (the first run can take a couple of minutes "
                                "while tags are fetched)…[/]"):
                d = build_digest(SteamTools(steam))
        except ProfilePrivateError as e:
            console.print(f"[bold red]{e}[/]")
            return 1
        except SteamAPIError as e:
            console.print(f"[bold red]Couldn't build the digest: {e}[/]")
            return 1
    text = to_markdown(d)
    if out:
        path = Path(out.replace("{date}", d.day.isoformat()))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.print(f"Saved your digest to [cyan]{path}[/]")
    else:
        from rich.markdown import Markdown

        console.print(Markdown(text))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steam-buddy", description="Your personal Steam gaming agent.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate your .env setup")
    group.add_argument("--digest", action="store_true", help="print your weekly digest (no Claude call)")
    parser.add_argument("--out", metavar="FILE", help="with --digest: save it as Markdown instead; "
                        "{date} in the name becomes today's date")
    parser.add_argument("--refresh", action="store_true", help="re-fetch your library, wishlist and prices first")
    args = parser.parse_args(argv)
    if args.out and not args.digest:
        parser.error("--out only works with --digest")
    # Keep library loggers quiet; the client never logs keys, but stray debug output clutters the chat.
    logging.basicConfig(level=logging.WARNING)

    if args.check:
        return check_setup()
    try:
        cfg = load_config(require_anthropic=not args.digest)
    except ConfigError as e:
        console.print("[bold red]Setup isn't complete yet:[/]")
        for problem in e.problems:
            console.print(f"  • {problem}")
        return 1
    if args.digest:
        return digest(cfg, refresh=args.refresh, out=args.out)
    return chat(cfg, refresh=args.refresh)


if __name__ == "__main__":
    sys.exit(main())
