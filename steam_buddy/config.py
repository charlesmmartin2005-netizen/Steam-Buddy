"""Load and validate settings from the environment / .env file.

Secrets are wrapped in `Secret` so they never show up in reprs, tracebacks
or log lines by accident. Call `.reveal()` only at the point of use.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "steam-buddy" / "cache.sqlite3"

# SteamID64 for individual accounts: 17 digits, base 76561197960265728.
_STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
_STEAM_KEY_RE = re.compile(r"^[0-9A-Fa-f]{32}$")


class ConfigError(Exception):
    """Raised when required settings are missing or malformed."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class Secret:
    """A string that refuses to print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class Config:
    steam_api_key: Secret = field(repr=False)
    steam_id: str
    anthropic_api_key: Secret | None = field(repr=False)
    anthropic_model: str
    cache_path: Path
    country: str


def load_config(
    env_file: str | os.PathLike | None = None,
    *,
    require_anthropic: bool = False,
) -> Config:
    """Read settings from `env_file` (default: ./.env) and the process environment.

    Raises ConfigError listing every problem at once, so a fresh setup can be
    fixed in one pass. Error messages never include secret values.
    """
    load_dotenv(env_file or Path.cwd() / ".env", override=False)

    problems: list[str] = []

    steam_key = os.getenv("STEAM_API_KEY", "").strip()
    if not steam_key:
        problems.append("STEAM_API_KEY is not set (get one at https://steamcommunity.com/dev/apikey)")
    elif not _STEAM_KEY_RE.match(steam_key):
        problems.append("STEAM_API_KEY doesn't look like a Steam key (expected 32 hex characters)")

    steam_id = os.getenv("STEAM_ID", "").strip()
    if not steam_id:
        problems.append("STEAM_ID is not set (your 17-digit SteamID64; see README)")
    elif not _STEAM_ID_RE.match(steam_id):
        problems.append(
            f"STEAM_ID '{steam_id}' is not a SteamID64 (expected 17 digits starting with 7656119). "
            "If you have a custom profile URL name, see README for how to convert it."
        )

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if require_anthropic and not anthropic_key:
        problems.append("ANTHROPIC_API_KEY is not set (get one at https://console.anthropic.com/settings/keys)")

    if problems:
        raise ConfigError(problems)

    cache_env = os.getenv("STEAM_BUDDY_CACHE", "").strip()
    return Config(
        steam_api_key=Secret(steam_key),
        steam_id=steam_id,
        anthropic_api_key=Secret(anthropic_key) if anthropic_key else None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
        cache_path=Path(cache_env).expanduser() if cache_env else DEFAULT_CACHE_PATH,
        country=(os.getenv("STEAM_COUNTRY", "").strip() or "us").lower(),
    )
