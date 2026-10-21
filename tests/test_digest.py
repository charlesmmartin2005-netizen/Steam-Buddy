import time
from datetime import date

from steam_buddy.digest import (
    Digest,
    NewsPost,
    Revisit,
    build_digest,
    clean_text,
    is_update,
    recent_posts,
    revisit_candidates,
    to_markdown,
)
from steam_buddy.profile import GameSummary, TasteProfile
from steam_buddy.steam_client import NewsItem, RecentGame, SteamAPIError

NOW = time.time()
DAY = 86400


def news(title, days_ago, official=True):
    return NewsItem(
        gid=title, title=title, url=f"https://example.com/{len(title)}", author="", contents=f"About {title}.",
        feedlabel="Community Announcements" if official else "PC Gamer", date=int(NOW - days_ago * DAY),
        feedname="steam_community_announcements" if official else "pcgamer", feed_type=1 if official else 0,
    )


def profile(**kw):
    base = dict(
        total_games=0, played_games=0, total_hours=0, top_tags=[], most_played=[], bounced=[], bounced_tags=[],
        active=[], completed=[], abandoned=[], stopped_endless=[], unplayed=[], recent_games=[], rising_tags=[],
        session_lean={}, excluded_software=[], notes=[],
    )
    return TasteProfile(**{**base, **kw})


class NewsClient:
    def __init__(self, news=None, recent=None):
        self.news = news or {}
        self.recent = recent or []

    def get_news(self, appid, count=5, *, refresh=False):
        if appid not in self.news:
            raise SteamAPIError("news timed out")
        return self.news[appid][:count]

    def get_recently_played(self, count=0, *, refresh=False):
        return self.recent


def pick(appid, name, **kw):
    return {
        "appid": appid, "name": name, "price": "$9.99", "discount_percent": 0, "positive_pct": 95.0,
        "reviews": 1200, "early_access": False, "session_style": "short runs", "fits_your_tags": ["Rogue-like"],
        "because_you_played": [{"game": "Rogue Runner", "hours": 200.0, "shared_tags": ["Rogue-like"]}], **kw,
    }


class FakeTools:
    """build_digest only needs these parts of SteamTools."""

    def __init__(self, client, prof, picks=(), gems=(), backlog=None):
        self.client, self._profile = client, prof
        self.picks, self.gems, self.backlog = list(picks), list(gems), backlog

    def profile(self):
        return self._profile

    def recommend_games(self, max_results=None):
        if isinstance(self.picks, Exception):
            raise self.picks
        return {"games": self.picks[:max_results]}

    def find_hidden_gems(self, max_results=None):
        return {"games": self.gems[:max_results]}

    def best_backlog_game(self):
        return self.backlog


def test_clean_text_strips_markup():
    raw = "[h1]Patch 1.2[/h1]<p>Fixed &amp; improved [url=https://x]stuff[/url]</p>"
    assert clean_text(raw) == "Patch 1.2: Fixed & improved stuff"
    assert clean_text("word " * 200, limit=20).endswith("…")


def test_clean_text_keeps_words_apart_and_drops_media():
    raw = ('[img]{STEAM_CLAN_IMAGE}/1/a.png[/img][h3]FIXES![/h3][list][*]Magnet works[/list]'
           '[p]Now on Play Store[/p][p]Adapted for mobile[/p][previewyoutube=abc;full][/previewyoutube]'
           '[img src="{STEAM_CLAN_IMAGE}/2/b.png"][/img][h2]Thanks[/h2]')
    assert clean_text(raw) == "FIXES! Magnet works Now on Play Store Adapted for mobile Thanks"


def test_sale_ads_and_press_are_not_updates():
    assert is_update(news("Patch 1.2: new biome", 1))
    assert not is_update(news("40% Off Sale Ending Soon!", 1))
    assert not is_update(news("Free Weekend starts now", 1))
    assert not is_update(news("Patch 1.2 reviewed", 1, official=False))
    for promo in ("SIFU Mobile is OUT NOW!", "Devstream #198", "Vote for us at the Golden Joystick Awards"):
        assert not is_update(news(promo, 1)), promo


def test_recent_posts_put_developer_updates_first():
    client = NewsClient({1: [
        news("Review: still great", 1, official=False),
        news("Update 2", 3),
        news("Summer Sale", 4),
        news("Обзор обновления", 5, official=False),
        news("Old patch", 20),
    ]})
    posts = recent_posts(client, 1, since=NOW - 14 * DAY, limit=3)
    assert [p.title for p in posts] == ["Update 2", "Review: still great"]
    assert [p.official for p in posts] == [True, False]
    assert posts[0].summary == "About Update 2."


def test_recent_posts_skip_press_about_the_same_story():
    client = NewsClient({1: [
        news("Caves of Qud: Diaspora is coming December 9", 1, official=False),
        news("Caves of Qud: Diaspora is coming December 9th", 2),
        news("Monumentally expansive roguelike gets an expansion", 2, official=False),
    ]})
    posts = recent_posts(client, 1, since=NOW - 14 * DAY, limit=3)
    assert [(p.title, p.official) for p in posts] == [
        ("Caves of Qud: Diaspora is coming December 9th", True),
        ("Monumentally expansive roguelike gets an expansion", False),
    ]


def test_revisit_prefers_games_with_updates_since_you_stopped():
    prof = profile(
        abandoned=[GameSummary(1, "Dropped RPG", 50, 200), GameSummary(6, "Short One", 2, 300)],
        stopped_endless=[GameSummary(2, "Old Shooter", 100, 120)],
        most_played=[
            GameSummary(3, "Daily Game", 300, 10),  # still playing
            GameSummary(1, "Dropped RPG", 50, 200),  # already listed as abandoned
            GameSummary(4, "Resting Game", 20, 400),
            GameSummary(5, "Finished Game", 80, 300),
        ],
        completed=[GameSummary(5, "Finished Game", 80, 300)],
    )
    client = NewsClient({
        1: [news("Big expansion", 100), news("Launch patch", 300)],
        2: [news("Season 3", 150)],  # before you stopped
        # 4 has no news: the request fails, and the game is still listed
    })
    out = revisit_candidates(client, prof, NOW)
    assert [r.name for r in out] == ["Dropped RPG", "Old Shooter", "Resting Game"]
    assert [p.title for p in out[0].updates] == ["Big expansion"]
    assert not out[0].more_updates
    assert out[0].why_stopped == "likely abandoned"
    assert out[1].updates == [] and out[1].why_stopped == "open-ended game you stopped"


def test_revisit_flags_when_every_post_is_newer():
    prof = profile(abandoned=[GameSummary(1, "Dropped RPG", 50, 200)])
    out = revisit_candidates(NewsClient({1: [news("Patch 9", 10), news("Patch 8", 30)]}), prof, NOW)
    assert out[0].more_updates


def test_digest_sections():
    client = NewsClient(
        news={1: [news("Patch 1.2", 2), news("Ancient news", 40)], 2: [news("Big expansion", 50), news("Launch", 300)]},
        recent=[RecentGame(1, "Rogue Runner", 600, 12000), RecentGame(9, "Some Tool", 900, 900)],
    )
    prof = profile(abandoned=[GameSummary(2, "Dropped RPG", 50, 200)], excluded_software=["Some Tool"])
    tools = FakeTools(client, prof, picks=[pick(100, "Big Hit"), pick(101, "Pick*Two")],
                      gems=[pick(101, "Pick*Two"), pick(102, "Small Gem"), pick(103, "Other Gem"), pick(104, "Third")])
    d = build_digest(tools, now=NOW)
    assert [g["name"] for g in d.gems] == ["Small Gem", "Other Gem"]  # a top pick isn't repeated
    assert [e["game"] for e in d.news] == ["Rogue Runner"]  # software is left out
    assert [p.title for p in d.news[0]["posts"]] == ["Patch 1.2"]
    assert d.revisit.name == "Dropped RPG" and d.backlog is None

    md = to_markdown(d)
    for heading in ("## Top picks for you", "## Hidden gems", "## News for what you're playing",
                    "## Worth going back to"):
        assert heading in md
    assert "1. **Big Hit** ($9.99, 95% positive of 1,200 reviews)" in md
    assert "Pick\\*Two" in md  # Markdown characters in names are escaped
    assert "Like Rogue Runner (200h): Rogue-like. Suits short runs." in md
    assert "### Rogue Runner (10h in the last two weeks)" in md
    assert "1 developer update since you stopped" in md


def test_digest_suggests_a_backlog_game_when_nothing_you_stopped_has_updates():
    client = NewsClient(news={2: [news("Old patch", 300)]})
    prof = profile(abandoned=[GameSummary(2, "Dropped RPG", 50, 200)])
    backlog = {"appid": 8, "name": "Backlog Epic", "hours": 0, "fits_your_tags": ["RPG"], "because_you_played": []}
    d = build_digest(FakeTools(client, prof, backlog=backlog), now=NOW)
    assert d.revisit is None and d.backlog == backlog
    md = to_markdown(d)
    assert "## From your backlog" in md
    assert "**Backlog Epic** (never played). Fits your RPG games." in md

    # With no backlog either, the best game you stopped is still shown.
    d = build_digest(FakeTools(client, prof), now=NOW)
    assert d.revisit.name == "Dropped RPG"
    assert "No developer updates since then" in to_markdown(d)


def test_a_failing_section_leaves_a_note_instead_of_failing_the_digest():
    tools = FakeTools(NewsClient(), profile(notes=["Achievements are private."]))
    tools.picks = SteamAPIError("SteamSpy is down")
    d = build_digest(tools, now=NOW)
    assert d.picks == []
    assert d.notes == ["Couldn't load recommendations: SteamSpy is down", "Achievements are private."]
    md = to_markdown(d)
    assert "No picks this week." in md
    assert "You haven't played anything in the last two weeks." in md
    assert "_Couldn't load recommendations: SteamSpy is down_\n\n_Achievements are private._" in md  # one paragraph each


def test_markdown_dates_and_more_updates():
    r = Revisit(1, "Dropped RPG", 50.5, 200, "likely abandoned",
                [NewsPost("Patch 9", "2026-10-01", "https://x/9", "", True)], more_updates=True)
    md = to_markdown(Digest(date(2026, 10, 21), [], [], [], r, None))
    assert "_Wednesday, October 21, 2026_" in md
    assert "**Dropped RPG**: 50.5h, last played 200 days ago (likely abandoned). 1+ developer updates" in md
    assert "- Oct 1: [Patch 9](https://x/9)" in md
