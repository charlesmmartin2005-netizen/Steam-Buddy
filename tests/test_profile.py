import json
from pathlib import Path

import pytest

from steam_buddy.profile import DAY, PRIVATE_STOP_AFTER, LibraryData, build_profile, gather
from steam_buddy.steam_client import AchievementProgress, OwnedGame, ProfilePrivateError, SteamAPIError, TagInfo

FIXTURE = Path(__file__).parent / "fixtures" / "fake_library.json"


def load_fixture() -> LibraryData:
    raw = json.loads(FIXTURE.read_text())
    now = raw["now"]
    games, tags, achievements = [], {}, {}
    for g in raw["games"]:
        games.append(
            OwnedGame(
                appid=g["appid"],
                name=g["name"],
                playtime_forever=g["playtime_forever"],
                playtime_2weeks=g["playtime_2weeks"],
                last_played=None if g["days_ago"] is None else int(now - g["days_ago"] * DAY),
            )
        )
        tags[g["appid"]] = TagInfo(g["appid"], g["tags"], "steamspy")
        a = g["achievements"]
        achievements[g["appid"]] = AchievementProgress(g["appid"], **a) if a else None
    return LibraryData(games=games, tags=tags, achievements=achievements, now=now)


@pytest.fixture
def profile():
    return build_profile(load_fixture())


def names(games):
    return {g.name for g in games}


def test_counts_exclude_software(profile):
    assert profile.excluded_software == ["Wallpaper Thing"]
    assert profile.total_games == 9
    assert profile.played_games == 8
    assert profile.total_hours == pytest.approx(391.5, abs=0.1)


def test_top_tag_is_weighted_by_playtime(profile):
    top = profile.top_tags[0]
    assert top.tag == "Roguelike"
    assert top.hours == 300
    assert top.games == 2
    assert top.top_games[0] == "Rogue Runner"


def test_non_descriptive_and_software_tags_left_out(profile):
    tags = {t.tag for t in profile.top_tags}
    assert "Indie" not in tags
    assert "Utilities" not in tags


def test_bounced_games_excludes_just_started_and_unplayed(profile):
    assert names(profile.bounced) == {"Arena Shooter", "Drop Zone Royale"}
    assert "FPS" in profile.bounced_tags and "Shooter" in profile.bounced_tags
    # Bounced games don't count toward what you like.
    assert "FPS" not in {t.tag for t in profile.top_tags}


def test_unplayed(profile):
    assert names(profile.unplayed) == {"Backlog Epic"}


def test_status_classification(profile):
    assert names(profile.active) == {"Rogue Runner", "Fresh Puzzler", "Tactics Lite"}
    assert names(profile.completed) == {"Deck Delver"}
    assert names(profile.abandoned) == {"Grand Open RPG"}
    # No achievement data means no verdict, and the profile says so.
    assert "Wild Survival" not in names(profile.completed + profile.abandoned)
    assert any("No achievement data for 2 games" in n for n in profile.notes)


def test_recent_trends(profile):
    assert names(profile.recent_games) == {"Rogue Runner", "Fresh Puzzler", "Tactics Lite"}
    assert profile.recent_games[0].name == "Fresh Puzzler"  # most recent first
    assert profile.rising_tags
    assert "RPG" not in profile.rising_tags and "Open World" not in profile.rising_tags


def test_session_lean(profile):
    s = profile.session_lean
    assert s["lean"] == "short runs"
    assert s["short_pct"] > s["long_pct"]
    assert "Steam doesn't report" in s["note"]


def test_one_huge_game_does_not_drown_out_the_rest():
    now = 1_800_000_000
    games = [
        OwnedGame(1, "Forever MMO", 60_000, 0, now - DAY),  # 1,000 hours
        OwnedGame(2, "Puzzle A", 1_200, 0, now - DAY),
        OwnedGame(3, "Puzzle B", 1_200, 0, now - DAY),
        OwnedGame(4, "Puzzle C", 1_200, 0, now - DAY),
    ]
    tags = {1: TagInfo(1, {"MMORPG": 10}, "steamspy")} | {
        i: TagInfo(i, {"Puzzle": 10}, "steamspy") for i in (2, 3, 4)
    }
    p = build_profile(LibraryData(games, tags, {}, now))
    scores = {t.tag: t.score for t in p.top_tags}
    assert scores["MMORPG"] > scores["Puzzle"]  # still on top
    assert scores["Puzzle"] / scores["MMORPG"] > 0.4  # but not drowned out


def test_profile_is_json_serialisable(profile):
    json.dumps(profile.to_dict())


def test_empty_library():
    p = build_profile(LibraryData([], {}, {}, 1_800_000_000))
    assert p.total_games == 0
    assert p.top_tags == []
    assert p.session_lean["lean"] == "unknown"


class FakeClient:
    def __init__(self, data: LibraryData, fail_achievements=()):
        self.data = data
        self.fail = set(fail_achievements)
        self.tag_calls = []

    def get_owned_games(self, refresh=False):
        return self.data.games

    def get_app_tags(self, appid, refresh=False):
        self.tag_calls.append(appid)
        return self.data.tags[appid]

    def get_achievements(self, appid, refresh=False):
        if appid in self.fail:
            raise SteamAPIError("boom")
        return self.data.achievements.get(appid)


def test_gather_skips_unplayed_and_notes_failures():
    data = load_fixture()
    client = FakeClient(data, fail_achievements={1})
    seen = []
    gathered = gather(client, now=data.now, progress=lambda stage, i, n: seen.append(stage))
    assert 8 not in client.tag_calls  # never played: no need to fetch tags
    assert gathered.achievements[1] is None
    assert any("achievements for 1 game." in n and "boom" in n for n in gathered.notes)
    assert {"tags", "achievements"} <= set(seen)


def test_tag_spelling_variants_are_merged_and_matched():
    now = 1_800_000_000
    games = [
        OwnedGame(1, "Roguey", 6_000, 0, now - DAY),
        OwnedGame(2, "Other Roguey", 3_000, 0, now - DAY),
    ]
    tags = {
        # SteamSpy's spellings: hyphenated, HTML-escaped, trailing space
        1: TagInfo(1, {"Rogue-like": 50, "Single-player": 40, "Point &amp; Click": 10, "Dystopian ": 5}, "steamspy"),
        2: TagInfo(2, {"Roguelike": 30, "Multi-player": 20, "Multiplayer": 25}, "steamspy"),
    }
    p = build_profile(LibraryData(games, tags, {}, now))
    names_ = [t.tag for t in p.top_tags]
    assert names_.count("Rogue-like") + names_.count("Roguelike") == 1
    roguelike = next(t for t in p.top_tags if t.tag in ("Rogue-like", "Roguelike"))
    assert roguelike.games == 2
    assert "Single-player" not in names_  # non-descriptive, despite the hyphen
    assert "Point & Click" in names_ and "Dystopian" in names_
    assert sum(1 for n in names_ if n.replace("-", "") == "Multiplayer") == 1
    assert p.session_lean["lean"] == "short runs"  # "Rogue-like" counts as a short-run genre


class PrivateAchievementsClient(FakeClient):
    def __init__(self, data):
        super().__init__(data)
        self.ach_calls = 0

    def get_achievements(self, appid, refresh=False):
        self.ach_calls += 1
        raise ProfilePrivateError()


def test_gather_explains_private_achievements_and_stops_asking():
    data = load_fixture()
    client = PrivateAchievementsClient(data)
    gathered = gather(client, now=data.now)
    assert client.ach_calls == PRIVATE_STOP_AFTER
    assert all(v is None for v in gathered.achievements.values())
    note = next(n for n in gathered.notes if "aren't public" in n)
    assert "Game details" in note and "steamcommunity.com/my/edit/settings" in note
    assert not any("Couldn't read" in n for n in gathered.notes)
    # One clear explanation, not a second vaguer note about missing data.
    assert not any("no achievement data" in n for n in build_profile(gathered).notes)


def test_few_achievements_and_live_service_games_get_no_verdict():
    now = 1_800_000_000
    old = now - 200 * DAY
    games = [
        OwnedGame(1, "One-Achievement Shooter", 100, 0, old),
        OwnedGame(2, "Hero Brawl Online", 7_500, 0, old),
        OwnedGame(3, "Story Game", 600, 0, old),
    ]
    tags = {
        1: TagInfo(1, {"FPS": 10}, "steamspy"),
        2: TagInfo(2, {"Hero Shooter": 10, "PvP": 8}, "steamspy"),
        3: TagInfo(3, {"Story Rich": 10}, "steamspy"),
    }
    achievements = {
        1: AchievementProgress(1, unlocked=1, total=1),
        2: AchievementProgress(2, unlocked=5, total=40),
        3: AchievementProgress(3, unlocked=1, total=20),
    }
    p = build_profile(LibraryData(games, tags, achievements, now))
    assert names(p.completed) == set()  # 1/1 achievements isn't "finished"
    assert names(p.abandoned) == {"Story Game"}
    assert names(p.stopped_endless) == {"Hero Brawl Online"}
    assert not any(" 1 games" in n for n in p.notes)


def test_bounced_tags_leave_out_tags_you_play_a_lot():
    now = 1_800_000_000
    old = now - 200 * DAY
    games = [
        OwnedGame(1, "Big Strategy", 30_000, 0, old),
        OwnedGame(2, "Dull Strategy", 20, 0, old),
        OwnedGame(3, "Spooky Strategy", 30, 0, old),
    ]
    tags = {
        1: TagInfo(1, {"Strategy": 10}, "steamspy"),
        2: TagInfo(2, {"Strategy": 10, "Horror": 5}, "steamspy"),
        3: TagInfo(3, {"Strategy": 10, "Horror": 5}, "steamspy"),
    }
    p = build_profile(LibraryData(games, tags, {}, now))
    assert p.bounced_tags == ["Horror"]
