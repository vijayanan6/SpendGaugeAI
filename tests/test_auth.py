"""Direct tests of the key-resolution precedence rule (docs/DESIGN.md §5): an
env-var key is exclusive, not a fallback pair with the persisted key. Each
test reloads spendgaugeai.database / spendgaugeai.auth against a fresh temp
DB so the module-level "active key" cache doesn't leak between tests."""
import importlib

import pytest


@pytest.fixture
def fresh_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("SPENDGAUGEAI_DB_PATH", str(tmp_path / "auth_test.db"))
    monkeypatch.delenv("SPENDGAUGEAI_API_KEY", raising=False)

    import spendgaugeai.database as database
    importlib.reload(database)
    import spendgaugeai.auth as auth
    importlib.reload(auth)

    database.init_db()
    return auth, database


def test_generates_and_persists_key_when_unset(fresh_auth):
    auth, database = fresh_auth
    key, source = auth.resolve_api_key()
    assert source == "generated"
    assert key
    persisted = database.server_config_get()
    assert persisted["api_key"] == key


def test_persisted_key_reused_across_restarts(fresh_auth):
    auth, database = fresh_auth
    key1, _ = auth.resolve_api_key()

    importlib.reload(auth)  # simulate a process restart with the same DB
    key2, source = auth.resolve_api_key()

    assert source == "persisted"
    assert key1 == key2


def test_env_var_takes_exclusive_precedence_over_persisted(monkeypatch, fresh_auth):
    auth, database = fresh_auth
    generated_key, _ = auth.resolve_api_key()  # first boot: no env var, generates + persists

    monkeypatch.setenv("SPENDGAUGEAI_API_KEY", "my-own-rotated-key")
    importlib.reload(auth)
    active_key, source = auth.resolve_api_key()

    assert source == "env"
    assert active_key == "my-own-rotated-key"
    # The old generated key (printed to a startup log somewhere) must no
    # longer be a valid credential once an env var is set — this is the
    # exact "OR" vs. exclusive-precedence bug the design doc calls out.
    assert active_key != generated_key
