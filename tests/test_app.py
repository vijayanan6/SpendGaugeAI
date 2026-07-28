"""End-to-end tests for the FastAPI app: auth gating on every route (docs/
DESIGN.md §5), the ingestion contract, guardrails, and the /usage/data shape
(§4). Env vars are set before importing spendgaugeai.app since database.py
and auth.py resolve them at import/startup time."""
import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp()
os.environ["SPENDGAUGEAI_DB_PATH"] = str(Path(_tmp_dir) / "test.db")
os.environ["SPENDGAUGEAI_API_KEY"] = "test-key-12345"

import pytest
from fastapi.testclient import TestClient

from spendgaugeai import database
from spendgaugeai.app import app

BASIC = ("spendgaugeai", "test-key-12345")
BEARER = {"Authorization": "Bearer test-key-12345"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    database.init_db()
    with database.get_connection() as conn:
        conn.execute("DELETE FROM usage_logs")
        conn.execute("DELETE FROM credit_config")
        conn.execute("DELETE FROM pricing_warnings")
        conn.commit()
    yield


def test_health_requires_no_auth(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_usage_log_requires_bearer(client):
    res = client.post("/usage/log", json={"model": "claude-sonnet-4-6", "input_tokens": 100})
    assert res.status_code == 401


def test_usage_log_rejects_wrong_bearer(client):
    res = client.post(
        "/usage/log", json={"model": "claude-sonnet-4-6"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert res.status_code == 401


def test_usage_log_success_computes_cost(client):
    res = client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 1000},
        headers=BEARER,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["logged"] is True
    assert body["cost_usd"] == pytest.approx(0.003 + 0.015, rel=1e-6)


def test_usage_log_defaults_project_and_session(client):
    res = client.post("/usage/log", json={"model": "claude-sonnet-4-6"}, headers=BEARER)
    assert res.status_code == 200


def test_usage_log_rejects_oversized_project(client):
    res = client.post(
        "/usage/log", json={"model": "claude-sonnet-4-6", "project": "x" * 201},
        headers=BEARER,
    )
    assert res.status_code == 422


def test_usage_log_rejects_too_many_tools(client):
    res = client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "tools_used": [f"tool{i}" for i in range(51)]},
        headers=BEARER,
    )
    assert res.status_code == 422


def test_pricing_fallback_for_unknown_model(client):
    res = client.post(
        "/usage/log",
        json={"model": "some-future-model", "input_tokens": 1000, "output_tokens": 1000},
        headers=BEARER,
    )
    assert res.status_code == 200
    assert res.json()["cost_usd"] == pytest.approx(0.003 + 0.015, rel=1e-6)


def test_usage_data_requires_basic_auth(client):
    res = client.get("/usage/data")
    assert res.status_code == 401


def test_usage_data_with_project_filter_and_tools(client):
    # Regression test: filtering by project used to 500 in the by_tool query
    # (a WHERE clause built via string .replace() injected two `?`
    # placeholders while only one param was bound).
    client.post(
        "/usage/log",
        json={
            "project": "demo-app", "model": "claude-sonnet-4-6",
            "input_tokens": 100, "output_tokens": 50, "tools_used": ["search_docs"],
        },
        headers=BEARER,
    )
    res = client.get("/usage/data", params={"project": "demo-app"}, auth=BASIC)
    assert res.status_code == 200
    data = res.json()
    assert data["by_tool"][0]["tool_name"] == "search_docs"
    assert data["by_project"][0]["project"] == "demo-app"


def test_usage_data_credit_stays_global_regardless_of_project_filter(client):
    # Regression: the "remaining balance" gauge used to scope period_cost_usd
    # to the ?project= filter, so a project with barely any spend of its own
    # looked like it still had the *entire* starting balance available, even
    # after other projects had already spent most of the real shared pool.
    # The budget is one shared pool (§8b) — credit must always reflect true
    # global reality, no matter which project's other stats you're viewing.
    client.post("/usage/credit", json={"starting_balance": 10.0}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"project": "big-spender", "model": "claude-sonnet-4-6", "input_tokens": 1_000_000, "output_tokens": 1_000_000},
        headers=BEARER,
    )
    client.post(
        "/usage/log",
        json={"project": "tiny-project", "model": "claude-haiku-4-5", "input_tokens": 10, "output_tokens": 10},
        headers=BEARER,
    )
    unfiltered = client.get("/usage/data", auth=BASIC).json()["credit"]
    filtered = client.get("/usage/data", params={"project": "tiny-project"}, auth=BASIC).json()["credit"]
    assert filtered["period_cost_usd"] == unfiltered["period_cost_usd"]
    assert filtered["period_cost_usd"] > 1.0  # dominated by big-spender's real cost, not tiny-project's own ~$0


def test_usage_data_shape(client):
    client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 500, "output_tokens": 200},
        headers=BEARER,
    )
    res = client.get("/usage/data", auth=BASIC)
    assert res.status_code == 200
    data = res.json()
    for key in ("totals", "by_model", "by_day", "by_session", "by_tool", "by_project", "credit"):
        assert key in data
    assert data["totals"]["total_requests"] == 1


def test_usage_page_requires_basic_auth(client):
    res = client.get("/usage")
    assert res.status_code == 401


def test_usage_page_renders(client):
    res = client.get("/usage", auth=BASIC)
    assert res.status_code == 200
    assert "SpendGaugeAI" in res.text


def test_usage_credit_requires_bearer(client):
    res = client.post("/usage/credit", json={"starting_balance": 10})
    assert res.status_code == 401


def test_usage_credit_set_reflected_in_data(client):
    res = client.post(
        "/usage/credit", json={"starting_balance": 25.0, "alert_threshold": 5.0}, headers=BEARER,
    )
    assert res.status_code == 200
    data = client.get("/usage/data", auth=BASIC).json()
    assert data["credit"]["starting_balance"] == 25.0
    assert data["credit"]["alert_threshold"] == 5.0


def test_static_requires_basic_auth(client):
    res = client.get("/static/dashboard.js")
    assert res.status_code == 401


def test_static_serves_existing_file(client):
    res = client.get("/static/dashboard.js", auth=BASIC)
    assert res.status_code == 200


def test_static_404_for_missing_file(client):
    res = client.get("/static/does-not-exist.js", auth=BASIC)
    assert res.status_code == 404


def test_docs_routes_are_disabled(client):
    # Regression: FastAPI's auto-registered docs previously bypassed auth
    # entirely (no dependency covers app-internal routes).
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_usage_credit_accepts_basic_auth_from_dashboard(client):
    # Regression: the dashboard's Save button only ever has a cached Basic
    # credential (from loading /usage), never a Bearer token — it must work.
    res = client.post("/usage/credit", json={"starting_balance": 30.0}, auth=BASIC)
    assert res.status_code == 200
    data = client.get("/usage/data", auth=BASIC).json()
    assert data["credit"]["starting_balance"] == 30.0


def test_usage_credit_rejects_wrong_basic_auth(client):
    res = client.post(
        "/usage/credit", json={"starting_balance": 10}, auth=("spendgaugeai", "wrong-key"),
    )
    assert res.status_code == 401


def test_max_body_size_rejects_invalid_content_length_header(client):
    res = client.post(
        "/usage/log",
        content=b'{"model": "claude-sonnet-4-6"}',
        headers={**BEARER, "Content-Type": "application/json", "Content-Length": "not-a-number"},
    )
    assert res.status_code == 400


def test_usage_budget_requires_bearer(client):
    res = client.get("/usage/budget")
    assert res.status_code == 401


def test_usage_budget_rejects_basic_auth(client):
    # Unlike /usage/credit, this is a machine-facing route (checked by the
    # SDK's enforcement guard, not the browser dashboard) — Bearer-only.
    res = client.get("/usage/budget", auth=BASIC)
    assert res.status_code == 401


def test_usage_budget_unconfigured_never_exceeded(client):
    res = client.get("/usage/budget", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body == {"starting_balance": 0.0, "remaining_usd": 0.0, "exceeded": False, "near_limit": False}


def test_usage_budget_not_exceeded_under_cap(client):
    client.post("/usage/credit", json={"starting_balance": 10.0}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 1000},
        headers=BEARER,
    )
    res = client.get("/usage/budget", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["exceeded"] is False
    assert body["near_limit"] is False
    assert body["starting_balance"] == 10.0
    assert body["remaining_usd"] == pytest.approx(10.0 - (0.003 + 0.015), rel=1e-6)


def test_usage_budget_exceeded_over_cap(client):
    client.post("/usage/credit", json={"starting_balance": 0.01}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 1_000_000, "output_tokens": 1_000_000},
        headers=BEARER,
    )
    res = client.get("/usage/budget", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["exceeded"] is True
    assert body["remaining_usd"] == 0.0
    # exceeded and near_limit are mutually exclusive by construction.
    assert body["near_limit"] is False


def test_usage_budget_project_filter_scopes_spend(client):
    client.post("/usage/credit", json={"starting_balance": 10.0}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"project": "other-app", "model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 1000},
        headers=BEARER,
    )
    res = client.get("/usage/budget", params={"project": "does-not-exist"}, headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["exceeded"] is False
    assert body["remaining_usd"] == 10.0


def test_usage_budget_near_limit_within_warning_threshold(client):
    # warning_threshold defaults to 5.0 — spend down to $3 remaining out of a
    # $10 cap should trip near_limit without tripping exceeded.
    client.post("/usage/credit", json={"starting_balance": 10.0}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 2_333_334, "output_tokens": 0},
        headers=BEARER,
    )
    res = client.get("/usage/budget", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    assert body["remaining_usd"] < 5.0
    assert body["exceeded"] is False
    assert body["near_limit"] is True


def test_usage_budget_near_limit_respects_custom_warning_threshold(client):
    client.post("/usage/credit", json={"starting_balance": 10.0, "warning_threshold": 1.0}, headers=BEARER)
    client.post(
        "/usage/log",
        json={"model": "claude-sonnet-4-6", "input_tokens": 1000, "output_tokens": 1000},
        headers=BEARER,
    )
    res = client.get("/usage/budget", headers=BEARER)
    assert res.status_code == 200
    body = res.json()
    # Only ~$0.018 spent out of $10 — far from both the default $5 warning
    # and this custom $1 one.
    assert body["near_limit"] is False


def test_max_body_size_rejects_oversized_chunked_body_without_content_length(client):
    # Regression: the size check previously only looked at the Content-Length
    # header, so a chunked-encoded request (no Content-Length header at all)
    # bypassed the 64KB cap entirely. A generator body makes httpx send
    # Transfer-Encoding: chunked with no Content-Length, exercising that path.
    from spendgaugeai.app import MAX_BODY_SIZE

    def body_stream():
        chunk = b"x" * 8192
        total = 0
        while total < MAX_BODY_SIZE + 8192:
            total += len(chunk)
            yield chunk

    res = client.post("/usage/log", content=body_stream(), headers=BEARER)
    assert res.status_code == 413
