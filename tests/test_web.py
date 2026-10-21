import json
import threading
from datetime import date
from http.server import HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from steam_buddy.digest import Digest, NewsPost, Revisit
from steam_buddy.web import App, ArtFinder, _handler, allowed_host, art, home_payload, owned_appids, token_ok


def pick(appid, name):
    return {"appid": appid, "name": name, "price": "$9.99", "discount_percent": 0, "positive_pct": 95.0,
            "reviews": 1200, "early_access": False, "match": 50.0, "fits_your_tags": ["Rogue-like"],
            "because_you_played": [{"game": "Rogue Runner", "hours": 200.0, "shared_tags": ["Rogue-like"]}]}


def post(title, day="2026-10-18"):
    return NewsPost(title, day, "https://example.com/p", "What changed.", official=True, big_update=True)


def digest(**kw):
    base = dict(
        day=date(2026, 10, 21),
        picks=[pick(1, "Pick One")],
        gems=[pick(2, "Gem Two")],
        news=[{"appid": 3, "game": "Playing Now", "hours_2_weeks": 12.5, "posts": [post("Update 1.0")]},
              {"appid": 4, "game": "Quiet Game", "hours_2_weeks": 1.0, "posts": []}],
        revisit=Revisit(5, "Old Favourite", 130.0, 265, "open-ended game you stopped", [post("New AI")], True, 3.8),
        backlog=None,
    )
    return Digest(**{**base, **kw})


# -- art -----------------------------------------------------------------------


def test_art_uses_classic_names_without_assets():
    a = art(105600)
    assert a["header"].endswith("/steam/apps/105600/header.jpg")
    assert a["capsule"].endswith("/steam/apps/105600/library_600x900.jpg")
    assert a["hero"].endswith("/steam/apps/105600/library_hero.jpg")


def test_art_uses_getitems_names_and_falls_back_per_image():
    assets = {"asset_url_format": "steam/apps/7/abc123/${FILENAME}", "header": "header.jpg",
              "library_capsule": "library_capsule.jpg"}  # no hero listed
    a = art(7, assets)
    assert a["capsule"] == "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/7/abc123/library_capsule.jpg"
    assert a["hero"].endswith("/steam/apps/7/library_hero.jpg")


def _transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_art_finder_reads_getitems_once_per_game():
    calls = []

    def handler(request):
        calls.append(request.url)
        body = {"response": {"store_items": [{"assets": {"asset_url_format": "steam/apps/9/h/${FILENAME}",
                                                          "library_capsule": "cap.jpg"}}]}}
        return httpx.Response(200, json=body)

    find = ArtFinder(_transport(handler))
    assert find(9)["capsule"].endswith("/steam/apps/9/h/cap.jpg")
    find(9)
    assert len(calls) == 1


@pytest.mark.parametrize("response", [httpx.Response(500, text="oops"), httpx.Response(200, json={"response": {}})])
def test_art_finder_falls_back_when_getitems_fails(response):
    find = ArtFinder(_transport(lambda request: response))
    assert find(9) == art(9)


# -- payload -------------------------------------------------------------------


def test_home_payload_shapes():
    data = home_payload(digest(), {"items": [{"appid": 6, "name": "Wish", "price": "$35.00", "on_sale": False}]})
    json.dumps(data)  # it all goes out as JSON
    assert data["day"] == "2026-10-21"
    assert [g["name"] for g in data["playing"]] == ["Playing Now", "Quiet Game"]
    assert data["playing"][0]["has_news"] and not data["playing"][1]["has_news"]
    assert data["playing"][0]["posts"][0]["title"] == "Update 1.0"
    assert data["playing"][0]["play_url"] == "steam://run/3"
    assert data["picks"][0]["store_url"] == "https://store.steampowered.com/app/1/"
    assert set(data["gems"][0]["art"]) == {"header", "capsule", "hero"}
    assert data["revisit"]["name"] == "Old Favourite" and data["revisit"]["updates"][0]["title"] == "New AI"
    assert data["wishlist"][0]["art"]["header"].endswith("/6/header.jpg")
    assert data["backlog"] is None


def test_because_you_played_gets_art_for_owned_games():
    data = home_payload(digest(), None, owned={"Rogue Runner": 42})
    because = data["picks"][0]["because_you_played"][0]
    assert because["game"] == "Rogue Runner" and because["art"]["capsule"].endswith("/42/library_600x900.jpg")
    assert "art" not in home_payload(digest(), None)["picks"][0]["because_you_played"][0]


def test_owned_appids_covers_the_whole_library():
    g = lambda appid, name: SimpleNamespace(appid=appid, name=name)
    assert owned_appids([g(1, "A"), g(2, "B"), g(1, "A")]) == {"A": 1, "B": 2}


def test_home_cites_art_for_an_owned_game_the_profile_doesnt_list(monkeypatch):
    # Risk of Rain 2 sits mid-library: not most played, not recent, not finished or abandoned.
    owned = [SimpleNamespace(appid=632360, name="Risk of Rain 2"), SimpleNamespace(appid=42, name="Rogue Runner")]
    d = digest(picks=[{**pick(1, "Pick One"), "because_you_played": [
        {"game": "Risk of Rain 2", "hours": 39.2, "shared_tags": ["Action Roguelike"]}]}])
    client = SimpleNamespace(get_owned_games=lambda: owned)
    tools = SimpleNamespace(client=client, get_wishlist=lambda: {"items": []})
    app = App(SimpleNamespace(anthropic_api_key=None), tools)
    monkeypatch.setattr("steam_buddy.web.build_digest", lambda t: d)
    data = app.home()
    because = data["picks"][0]["because_you_played"][0]
    assert because["art"]["capsule"].endswith("/632360/library_600x900.jpg")


def test_home_payload_handles_empty_sections():
    data = home_payload(digest(picks=[], gems=[], news=[], revisit=None,
                               backlog={"appid": 8, "name": "Never Played", "hours": 0}), None)
    assert data["picks"] == data["gems"] == data["playing"] == data["wishlist"] == []
    assert data["revisit"] is None
    assert data["backlog"]["play_url"] == "steam://run/8"


# -- guards --------------------------------------------------------------------


def test_allowed_host():
    assert allowed_host("127.0.0.1:8765", 8765)
    assert allowed_host("localhost:8765", 8765)
    assert not allowed_host("evil.example:8765", 8765)
    assert not allowed_host("127.0.0.1:9999", 8765)
    assert not allowed_host(None, 8765)


def test_token_ok():
    assert token_ok("abc", "abc")
    assert not token_ok("abd", "abc")
    assert not token_ok(None, "abc")
    assert not token_ok("", "abc")


def test_chat_without_anthropic_key_says_so():
    app = App(SimpleNamespace(anthropic_api_key=None), tools=None)
    reply = app.chat("what should I play?")
    assert reply["status"] == "error" and "ANTHROPIC_API_KEY" in reply["text"]


# -- the server ----------------------------------------------------------------


class FakeApp:
    token = "secret-token"

    def __init__(self):
        self.questions = []

    def home(self, refresh=False):
        return {"day": "2026-10-21", "refreshed": refresh}

    def chat(self, question):
        self.questions.append(question)
        return {"text": f"echo {question}", "status": "ok", "tools": []}


@pytest.fixture
def server():
    app = FakeApp()
    srv = HTTPServer(("127.0.0.1", 0), lambda *a: None)
    port = srv.server_port
    srv.RequestHandlerClass = _handler(app, port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        yield app, client, port
    srv.shutdown()
    srv.server_close()


def test_page_carries_the_token(server):
    app, client, _ = server
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert app.token in r.text and "__BUDDY_TOKEN__" not in r.text


def test_api_needs_the_token(server):
    app, client, _ = server
    assert client.get("/api/home").status_code == 403
    assert client.get("/api/home", headers={"X-Buddy-Token": "wrong"}).status_code == 403
    r = client.get("/api/home?refresh=1", headers={"X-Buddy-Token": app.token})
    assert r.status_code == 200 and r.json() == {"day": "2026-10-21", "refreshed": True}


def test_other_hosts_are_refused(server):
    app, client, port = server
    r = client.get("/api/home", headers={"Host": f"evil.example:{port}", "X-Buddy-Token": app.token})
    assert r.status_code == 403
    assert client.get("/", headers={"Host": f"evil.example:{port}"}).status_code == 403


def test_chat_endpoint(server):
    app, client, _ = server
    ok = {"X-Buddy-Token": app.token, "Content-Type": "application/json"}
    r = client.post("/api/chat", headers=ok, content=json.dumps({"question": "hi"}))
    assert r.status_code == 200 and r.json()["text"] == "echo hi"
    # A form post from another site can't set a JSON content type or the token.
    assert client.post("/api/chat", headers={"X-Buddy-Token": app.token}, data={"question": "hi"}).status_code == 415
    assert client.post("/api/chat", headers={"Content-Type": "application/json"},
                       content=json.dumps({"question": "hi"})).status_code == 403
    assert client.post("/api/chat", headers=ok, content=json.dumps({"question": " "})).status_code == 400
    assert client.post("/api/chat", headers=ok, content="not json").status_code == 400
    assert client.post("/api/chat", headers=ok, content=json.dumps({"question": "x" * 9000})).status_code == 413
    assert app.questions == ["hi"]
    assert "access-control-allow-origin" not in r.headers


def test_unknown_paths(server):
    app, client, _ = server
    assert client.get("/api/nope", headers={"X-Buddy-Token": app.token}).status_code == 404
    assert client.get("/secret.txt").status_code == 404
