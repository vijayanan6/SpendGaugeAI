"""Tests for alerts.py's threshold handling — isolated from the Discord HTTP
call, which is monkeypatched out."""
import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp()
os.environ["SPENDGAUGEAI_DB_PATH"] = str(Path(_tmp_dir) / "test.db")
os.environ["SPENDGAUGEAI_API_KEY"] = "test-key-12345"
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test"

import pytest

from spendgaugeai import alerts, database


@pytest.fixture(autouse=True)
def clean_db():
    database.init_db()
    with database.get_connection() as conn:
        conn.execute("DELETE FROM usage_logs")
        conn.execute("DELETE FROM credit_config")
        conn.commit()
    yield


@pytest.mark.asyncio
async def test_explicit_zero_alert_threshold_is_not_overridden(monkeypatch):
    # Regression: `cfg.get("alert_threshold") or 1.0` silently replaced an
    # explicitly-configured 0 with the 1.0 default, since `0 or 1.0` is 1.0.
    database.credit_set(starting_balance=10.0, alert_threshold=0.0, warning_threshold=0.0)

    sent_messages = []

    async def fake_send_discord(message: str) -> bool:
        sent_messages.append(message)
        return True

    monkeypatch.setattr(alerts, "_send_discord", fake_send_discord)

    # Remaining balance is 10.0 (no usage logged) — well above the real
    # threshold of 0, so no alert should fire at all if 0 is honored.
    await alerts._maybe_send_low_credit_alert()
    assert sent_messages == []


@pytest.mark.asyncio
async def test_zero_alert_threshold_fires_only_at_zero_remaining(monkeypatch):
    database.credit_set(starting_balance=1.0, alert_threshold=0.0, warning_threshold=0.0)
    database.usage_log(
        session_id="s1", model="claude-sonnet-4-6",
        input_tokens=1_000_000, cache_write=0, cache_read=0, output_tokens=0,
    )  # spends well past the $1 starting balance, so remaining == 0

    sent_messages = []

    async def fake_send_discord(message: str) -> bool:
        sent_messages.append(message)
        return True

    monkeypatch.setattr(alerts, "_send_discord", fake_send_discord)

    await alerts._maybe_send_low_credit_alert()
    assert len(sent_messages) == 1
    assert "critical threshold: $0.00" in sent_messages[0]
