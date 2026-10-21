import pytest

from steam_buddy.cache import DAY, HOUR, Cache


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cache(clock):
    with Cache(":memory:", clock=clock) as c:
        yield c


def test_round_trip(cache):
    cache.set("owned_games", "me", [{"appid": 1}])
    assert cache.get("owned_games", "me") == [{"appid": 1}]


def test_ttls_per_kind(cache, clock):
    cache.set("news", "620", ["n"])
    cache.set("owned_games", "me", ["g"])
    cache.set("app_details", "620", {"name": "Portal 2"})

    clock.now += HOUR + 1
    assert cache.get("news", "620") is None
    assert cache.get("owned_games", "me") == ["g"]

    clock.now += DAY
    assert cache.get("owned_games", "me") is None
    assert cache.get("app_details", "620") == {"name": "Portal 2"}

    clock.now += 7 * DAY
    assert cache.get("app_details", "620") is None
    assert cache.get("app_details", "620", allow_stale=True) == {"name": "Portal 2"}


def test_get_or_fetch_uses_cache_until_expired(cache, clock):
    calls = []

    def fetch():
        calls.append(1)
        return len(calls)

    assert cache.get_or_fetch("news", "a", fetch) == 1
    assert cache.get_or_fetch("news", "a", fetch) == 1
    clock.now += HOUR + 1
    assert cache.get_or_fetch("news", "a", fetch) == 2
    assert cache.get_or_fetch("news", "a", fetch, refresh=True) == 3


def test_cached_none_is_a_hit(cache):
    calls = []

    def fetch():
        calls.append(1)
        return None

    cache.get_or_fetch("app_details", "delisted", fetch)
    cache.get_or_fetch("app_details", "delisted", fetch)
    assert len(calls) == 1


def test_stale_value_returned_when_fetch_fails(cache, clock):
    cache.set("news", "a", ["old"])
    clock.now += HOUR + 1

    def boom():
        raise RuntimeError("network down")

    assert cache.get_or_fetch("news", "a", boom) == ["old"]
    with pytest.raises(RuntimeError):
        cache.get_or_fetch("news", "never-cached", boom)


def test_unknown_kind_rejected(cache):
    with pytest.raises(ValueError):
        cache.set("nonsense", "k", 1)


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "cache.sqlite3"
    with Cache(path) as c:
        c.set("news", "a", 1)
    assert path.exists()
