"""A local web app: the digest as a dashboard, a news feed, pick cards and the chat.

    python -m steam_buddy.main --web

Serves one page on 127.0.0.1 only. Game art loads in the browser straight from
Steam's image servers; everything else comes from the same digest and tools the
CLI uses, so the home screen is free (no Claude call) and only chat messages
reach Claude.

The server is single-threaded on purpose: the cache is one SQLite connection,
and one person using it locally never needs two requests at once.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .cache import Cache
from .config import Config
from .digest import Digest, build_digest
from .steam_client import ProfilePrivateError, SteamAPIError, SteamClient
from .tools import SteamTools

PAGE = Path(__file__).with_name("web_ui.html")
DEFAULT_PORT = 8765
HOME_TTL = 15 * 60  # seconds the home screen is reused before it's rebuilt
MAX_BODY = 8_000  # bytes; a chat message, nothing more

ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
ART = ASSET_BASE + "steam/apps/{appid}/{file}"
GET_ITEMS = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
# Steam's GetItems names each asset; newer games keep theirs under hashed paths the classic names miss.
# Some games list only the double-size library images, so those are the second choice.
ASSETS = {"header": ("header",), "capsule": ("library_capsule", "library_capsule_2x"),
          "hero": ("library_hero", "library_hero_2x")}
CLASSIC = {"header": "header.jpg", "capsule": "library_600x900.jpg", "hero": "library_hero.jpg"}

ArtLookup = Callable[[int], dict[str, str]]


def art(appid: int, assets: dict[str, Any] | None = None) -> dict[str, str]:
    """Image URLs for a game: GetItems' names where it gave them, else Steam's classic file names.

    Not every game has every image; the page falls back to the header, then to a plain tile.
    """
    assets = assets or {}
    fmt = assets.get("asset_url_format")
    out = {}
    for kind, keys in ASSETS.items():
        name = next((assets[k] for k in keys if assets.get(k)), None)
        out[kind] = ASSET_BASE + fmt.replace("${FILENAME}", name) if fmt and name else ART.format(
            appid=appid, file=CLASSIC[kind])
    return out


class ArtFinder:
    """Looks up each game's art names once per run. No key needed; a failure falls back to the classic names."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=5, headers={"User-Agent": "steam-buddy/0.1"})
        self._seen: dict[int, dict[str, str]] = {}

    def __call__(self, appid: int) -> dict[str, str]:
        if appid not in self._seen:
            assets: dict[str, Any] = {}
            query = {"ids": [{"appid": appid}], "context": {"language": "english", "country_code": "US"},
                     "data_request": {"include_assets": True}}
            try:
                r = self._http.get(GET_ITEMS, params={"input_json": json.dumps(query)})
                assets = r.json()["response"]["store_items"][0].get("assets") or {}
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                pass
            self._seen[appid] = art(appid, assets)
        return self._seen[appid]


def _game(g: dict[str, Any], find_art: ArtLookup, owned: dict[str, int]) -> dict[str, Any]:
    # "Because you played" names your games; give each one its art too when it's one we know.
    because = [{**b, "art": find_art(owned[b["game"]])} if b.get("game") in owned else b
               for b in g.get("because_you_played") or []]
    return {**g, "because_you_played": because, "art": find_art(g["appid"]),
            "store_url": f"https://store.steampowered.com/app/{g['appid']}/"}


def owned_appids(games: list[Any]) -> dict[str, int]:
    """Name → appid for every game you own, so any game a recommendation cites gets its art."""
    out: dict[str, int] = {}
    for g in games:
        out.setdefault(g.name, g.appid)
    return out


def home_payload(d: Digest, wishlist: dict[str, Any] | None, find_art: ArtLookup = art,
                 owned: dict[str, int] | None = None) -> dict[str, Any]:
    """Everything the home, feed and picks screens show, as JSON-ready data."""
    owned = owned or {}
    playing = [
        {"appid": n["appid"], "name": n["game"], "hours_2_weeks": n["hours_2_weeks"], "art": find_art(n["appid"]),
         "posts": [asdict(p) for p in n["posts"]], "has_news": bool(n["posts"]),
         "play_url": f"steam://run/{n['appid']}"}
        for n in d.news
    ]
    revisit = None
    if d.revisit:
        r = d.revisit
        revisit = {**asdict(r), "art": find_art(r.appid), "play_url": f"steam://run/{r.appid}"}
    backlog = None
    if d.backlog:
        backlog = {**_game(d.backlog, find_art, owned), "play_url": f"steam://run/{d.backlog['appid']}"}
    items = [_game(i, find_art, owned) for i in (wishlist or {}).get("items", [])]
    return {
        "day": d.day.isoformat(),
        "playing": playing,
        "picks": [_game(g, find_art, owned) for g in d.picks],
        "gems": [_game(g, find_art, owned) for g in d.gems],
        "revisit": revisit,
        "backlog": backlog,
        "wishlist": items,
        "notes": d.notes,
    }


def allowed_host(host: str | None, port: int) -> bool:
    """Only answer requests addressed to this machine, so another site can't reach the server by DNS tricks."""
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def token_ok(sent: str | None, token: str) -> bool:
    """The page carries a token made at launch; other sites can't read the page, so they can't send it."""
    return bool(sent) and hmac.compare_digest(sent, token)


class App:
    """The state behind the server: Steam access, the cached home screen and a chat agent made on first use."""

    def __init__(self, cfg: Config, tools: SteamTools, find_art: ArtLookup = art) -> None:
        self.cfg = cfg
        self.tools = tools
        self.find_art = find_art
        self.token = secrets.token_urlsafe(24)
        self._home: dict[str, Any] | None = None
        self._home_at = 0.0
        self._agent: Any = None
        self._used: list[str] = []

    def home(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or self._home is None or time.time() - self._home_at > HOME_TTL:
            d = build_digest(self.tools)
            try:
                wishlist = self.tools.get_wishlist()
            except (SteamAPIError, ValueError):
                wishlist = None
            try:
                owned = owned_appids(self.tools.client.get_owned_games())
            except SteamAPIError:
                owned = {}
            self._home, self._home_at = home_payload(d, wishlist, self.find_art, owned), time.time()
        return self._home

    def chat(self, question: str) -> dict[str, Any]:
        from .agent import AgentError  # the Anthropic SDK is slow to import; the home screen doesn't need it

        if not self.cfg.anthropic_api_key:
            return {"text": "Chat needs ANTHROPIC_API_KEY in your .env.", "status": "error", "tools": []}
        if question.strip().lower() == "/reset":
            if self._agent:
                self._agent.reset()
            return {"text": "Started a new conversation.", "status": "ok", "tools": []}
        self._used = []
        try:
            reply = self._chat_agent().ask(question)
        except AgentError as e:
            return {"text": str(e), "status": "error", "tools": self._used}
        return {"text": reply.text, "status": reply.status, "tools": self._used,
                "fallback_model": reply.fallback_model}

    def _chat_agent(self) -> Any:
        if self._agent is None:
            from .agent import Agent
            from .main import TOOL_LABELS

            def run(name: str, args: dict[str, Any]) -> tuple[str, bool]:
                self._used.append(TOOL_LABELS.get(name, name))
                return self.tools.run(name, args)

            self._agent = Agent.from_config(self.cfg, run)
        return self._agent


def _handler(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SteamBuddy"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
            pass  # request lines would clutter the terminal

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Another site can't show the app in a frame and trick a click on it.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, api: bool) -> bool:
            if not allowed_host(self.headers.get("Host"), port):
                self._json(403, {"error": "This server only answers on this computer."})
                return False
            if api and not token_ok(self.headers.get("X-Buddy-Token"), app.token):
                self._json(403, {"error": "Missing or wrong token. Reload the page."})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - the base class's name
            url = urlsplit(self.path)
            if not self._guard(api=url.path.startswith("/api/")):
                return
            if url.path in ("/", "/index.html"):
                page = PAGE.read_text(encoding="utf-8").replace("__BUDDY_TOKEN__", app.token)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/home":
                try:
                    self._json(200, app.home(refresh="refresh=1" in url.query))
                except ProfilePrivateError as e:
                    self._json(409, {"error": str(e)})
                except SteamAPIError as e:
                    self._json(502, {"error": f"Couldn't reach Steam: {e}"})
            else:
                self._json(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard(api=True):
                return
            if urlsplit(self.path).path != "/api/chat":
                self._json(404, {"error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            # Read the body even when refusing it: replying with it unread resets the connection on Windows.
            body = self.rfile.read(length) if 0 < length <= 64 * MAX_BODY else b""
            # A JSON body can't be sent by a plain form on another site, so this also stops cross-site posts.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(415, {"error": "Send JSON."})
                return
            if not 0 < length <= MAX_BODY:
                self._json(413, {"error": "Message too long."})
                return
            try:
                question = str(json.loads(body).get("question", "")).strip()
            except (ValueError, AttributeError):
                self._json(400, {"error": "Bad request."})
                return
            if not question:
                self._json(400, {"error": "Ask something."})
                return
            self._json(200, app.chat(question))

    return Handler


def _edge() -> str | None:
    found = shutil.which("msedge")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (p := Path(base, "Microsoft", "Edge", "Application", "msedge.exe")).exists():
            return str(p)
    return None


def open_window(url: str) -> None:
    """Edge's app mode gives the page a window of its own; any other browser gets a normal tab."""
    edge = _edge()
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def serve(cfg: Config, *, port: int = DEFAULT_PORT, open_browser: bool = True, refresh: bool = False,
          clear_account_data: Any = None) -> int:
    """Run the web app until Ctrl+C."""
    from rich.console import Console

    console = Console()
    with Cache(cfg.cache_path) as cache, SteamClient.from_config(cfg, cache=cache) as steam:
        if refresh and clear_account_data:
            clear_account_data(cache)
        app = App(cfg, SteamTools(steam), ArtFinder())
        try:
            server = HTTPServer(("127.0.0.1", port), _handler(app, port))
        except OSError:
            console.print(f"[bold red]Port {port} is busy. Try --port {port + 1}.[/]")
            return 1
        url = f"http://127.0.0.1:{port}/"
        console.print(f"Steam Buddy is running at [cyan]{url}[/]  (Ctrl+C to stop)")
        console.print("[dim]The first load can take a minute while your library's tags are fetched.[/]")
        if open_browser:
            threading.Timer(0.5, open_window, (url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\nStopped.")
        finally:
            server.server_close()
    return 0
