import pytest

from steam_buddy.config import DEFAULT_MODEL, ConfigError, Secret, load_config

GOOD_KEY = "0123456789ABCDEF0123456789ABCDEF"
GOOD_ID = "76561197960287930"
ENV_VARS = ["STEAM_API_KEY", "STEAM_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "STEAM_BUDDY_CACHE", "STEAM_COUNTRY"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    env_file = tmp_path / ".env"

    def write(**values):
        env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        return env_file

    return write


def test_valid_config(env):
    cfg = load_config(env(STEAM_API_KEY=GOOD_KEY, STEAM_ID=GOOD_ID))
    assert cfg.steam_id == GOOD_ID
    assert cfg.steam_api_key.reveal() == GOOD_KEY
    assert cfg.anthropic_model == DEFAULT_MODEL
    assert cfg.anthropic_api_key is None


def test_key_never_appears_in_repr(env):
    cfg = load_config(env(STEAM_API_KEY=GOOD_KEY, STEAM_ID=GOOD_ID, ANTHROPIC_API_KEY="sk-ant-secret"))
    for text in (repr(cfg), str(cfg), repr(cfg.steam_api_key), f"{cfg.steam_api_key}"):
        assert GOOD_KEY not in text
        assert "sk-ant-secret" not in text


def test_missing_values_reported_together(env):
    with pytest.raises(ConfigError) as exc:
        load_config(env(), require_anthropic=True)
    assert len(exc.value.problems) == 3


def test_bad_values_rejected_without_echoing_key(env):
    with pytest.raises(ConfigError) as exc:
        load_config(env(STEAM_API_KEY="not-a-real-key", STEAM_ID="gabelogannewell"))
    message = str(exc.value)
    assert "not-a-real-key" not in message
    assert "SteamID64" in message


def test_secret_is_falsy_when_empty():
    assert not Secret("")
    assert Secret("x")
