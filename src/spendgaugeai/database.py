"""
database.py — SQLite persistence layer for usage/credit/alert accounting.

Ported from the MCP Learning Project's `database.py`, stripped of the
app-specific `notes`/`sessions` tables (out of scope here — see
docs/DESIGN.md §1) and adapted for this product's own concurrency profile:
multiple independent apps report over HTTP, possibly concurrently, so WAL
mode is enabled unconditionally in init_db() (§3 of DESIGN.md) — the source
project never needed this because it only ever had one writer (itself).
"""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("SPENDGAUGEAI_DB_PATH", "./data/spendgaugeai.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Open a connection with row_factory so rows behave like dicts."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                project             TEXT NOT NULL DEFAULT 'default',
                session_id          TEXT NOT NULL,
                model               TEXT NOT NULL,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                web_search_requests INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd  REAL NOT NULL DEFAULT 0,
                tools_used          TEXT NOT NULL DEFAULT '[]',
                created_at          TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_project ON usage_logs(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at ON usage_logs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_model ON usage_logs(model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_session_id ON usage_logs(session_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricing_warnings (
                model         TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                alert_sent_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_config (
                id                    INTEGER PRIMARY KEY CHECK (id = 1),
                starting_balance      REAL NOT NULL DEFAULT 0,
                alert_threshold       REAL NOT NULL DEFAULT 1.0,
                warning_threshold     REAL NOT NULL DEFAULT 5.0,
                period_start          TEXT,
                prev_period_start     TEXT,
                prev_period_end       TEXT,
                prev_period_cost_usd  REAL NOT NULL DEFAULT 0,
                prev_period_days      INTEGER NOT NULL DEFAULT 0,
                last_alert_sent_at    TEXT,
                last_warning_sent_at  TEXT,
                last_spike_alert_date TEXT,
                last_digest_sent_date TEXT,
                last_web_search_budget_alert_date TEXT,
                updated_at            TEXT NOT NULL
            )
        """)

        # New vs. the source project — see docs/DESIGN.md §5 for why this exists:
        # the persisted fallback API key, generated and printed once on first boot
        # if SPENDGAUGEAI_API_KEY isn't set in the environment.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS server_config (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                api_key      TEXT NOT NULL,
                generated_at TEXT NOT NULL
            )
        """)
        conn.commit()

    _harden_db_permissions()


def _harden_db_permissions() -> None:
    """Restrict the SQLite file (and its WAL/SHM siblings) to owner-only
    read/write. Best-effort — a chmod failure shouldn't block startup, and on
    Windows os.chmod can't express real POSIX permissions anyway (see
    docs/DESIGN.md §11)."""
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH.with_name(DB_PATH.name + suffix)
        try:
            path.chmod(0o600)
        except OSError:
            pass


# ── Pricing (USD per 1K tokens) ───────────────────────────────────────────────
# Manual snapshot — Anthropic's API has no endpoint that returns live pricing, so
# this must be re-verified by hand against console.anthropic.com/settings/billing
# whenever Anthropic changes rates or a new model starts appearing in
# pricing_warnings (see _estimate_cost()'s fallback below — it exists so an
# unpriced model gets flagged instead of silently mispriced, the exact bug class
# documented in the source project's INSIGHTS.md).
_PRICING = {
    "claude-haiku-4-5": {
        "input": 0.001, "cache_write": 0.00125, "cache_read": 0.0001, "output": 0.005
    },
    "claude-sonnet-4-6": {
        "input": 0.003, "cache_write": 0.00375, "cache_read": 0.0003, "output": 0.015
    },
}

# Anthropic server-side web search tool: $10 per 1,000 searches, billed per use
# regardless of result count — separate from token costs.
_WEB_SEARCH_COST_PER_USE = 0.01


def _estimate_cost(model: str, input_tokens: int, cache_write: int, cache_read: int, output_tokens: int, web_search_requests: int = 0) -> float:
    """Estimate cost in USD based on token counts, model pricing, and server-tool fees."""
    p = _PRICING.get(model)
    if p is None:
        print(f"[cost] WARNING: no _PRICING entry for model '{model}' — falling back to claude-sonnet-4-6 rates. Add a real entry in database.py._PRICING.")
        p = _PRICING["claude-sonnet-4-6"]
    return (
        input_tokens      / 1000 * p["input"] +
        cache_write       / 1000 * p["cache_write"] +
        cache_read        / 1000 * p["cache_read"] +
        output_tokens     / 1000 * p["output"] +
        web_search_requests * _WEB_SEARCH_COST_PER_USE
    )


# ── Usage Logs ────────────────────────────────────────────────────────────────

def usage_log(session_id: str, model: str, input_tokens: int, cache_write: int, cache_read: int, output_tokens: int, tools: list[str] | None = None, project: str = "default", web_search_requests: int = 0) -> float:
    """Save token usage for one request. Returns the estimated cost in USD."""
    cost = _estimate_cost(model, input_tokens, cache_write, cache_read, output_tokens, web_search_requests)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO usage_logs
              (project, session_id, model, input_tokens, cache_write_tokens, cache_read_tokens, output_tokens, web_search_requests, estimated_cost_usd, tools_used, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project, session_id, model, input_tokens, cache_write, cache_read, output_tokens, web_search_requests, cost, json.dumps(tools or []), datetime.now().isoformat()),
        )
        if model not in _PRICING:
            conn.execute(
                "INSERT OR IGNORE INTO pricing_warnings (model, first_seen_at) VALUES (?, ?)",
                (model, datetime.now().isoformat()),
            )
        conn.commit()
    return cost


def pricing_warnings_pending() -> list[str]:
    """Models that hit _PRICING's fallback and haven't had a Discord alert sent
    yet. One-time per model (not a daily cooldown) — stops once a real
    _PRICING entry is added for that model."""
    with get_connection() as conn:
        rows = conn.execute("SELECT model FROM pricing_warnings WHERE alert_sent_at IS NULL").fetchall()
    return [r["model"] for r in rows]


def mark_pricing_warning_sent(model: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE pricing_warnings SET alert_sent_at = ? WHERE model = ?", (datetime.now().isoformat(), model))
        conn.commit()


def usage_summary(project: str | None = None) -> dict:
    """Return aggregated usage stats. Pass a project name to filter to one project."""
    where  = "WHERE project = ?" if project else ""
    params = (project,)          if project else ()
    with get_connection() as conn:
        totals = conn.execute(f"""
            SELECT
                COUNT(*)                 AS total_requests,
                SUM(input_tokens)        AS total_input,
                SUM(cache_write_tokens)  AS total_cache_write,
                SUM(cache_read_tokens)   AS total_cache_read,
                SUM(output_tokens)       AS total_output,
                SUM(web_search_requests) AS total_web_searches,
                SUM(estimated_cost_usd)  AS total_cost_usd,
                MIN(created_at)          AS first_request,
                MAX(created_at)          AS last_request
            FROM usage_logs {where}
        """, params).fetchone()

        by_model = conn.execute(f"""
            SELECT model,
                   COUNT(*)                AS requests,
                   SUM(estimated_cost_usd) AS cost_usd
            FROM usage_logs {where}
            GROUP BY model
            ORDER BY cost_usd DESC
        """, params).fetchall()

        by_day = conn.execute(f"""
            SELECT DATE(created_at)        AS day,
                   COUNT(*)                AS requests,
                   SUM(estimated_cost_usd) AS cost_usd
            FROM usage_logs {where}
            GROUP BY day
            ORDER BY day DESC
            LIMIT 14
        """, params).fetchall()

        by_session = conn.execute(f"""
            SELECT session_id,
                   COUNT(*)                AS requests,
                   SUM(estimated_cost_usd) AS cost_usd,
                   MIN(created_at)         AS first_at,
                   MAX(created_at)         AS last_at
            FROM usage_logs {where}
            GROUP BY session_id
            ORDER BY cost_usd DESC
            LIMIT 10
        """, params).fetchall()

        # A turn/request that calls the same tool more than once must attribute
        # estimated_cost_usd to that request once per distinct tool it used, not
        # once per mention — see the source project's database.py for the real
        # bug this guards against (3x cost inflation from a 3-call repeated tool).
        by_tool = conn.execute(f"""
            SELECT
                tool_name,
                SUM(calls)    AS calls,
                SUM(cost_usd) AS cost_usd,
                AVG(cost_usd) AS avg_cost_usd
            FROM (
                SELECT
                    ul.id                AS log_id,
                    json_each.value      AS tool_name,
                    COUNT(*)             AS calls,
                    MAX(ul.estimated_cost_usd) AS cost_usd
                FROM usage_logs ul, json_each(ul.tools_used)
                {"WHERE ul.project = ?" if project else ""}
                GROUP BY ul.id, tool_name
            )
            GROUP BY tool_name
            ORDER BY calls DESC
        """, params).fetchall()

        by_project = conn.execute("""
            SELECT project,
                   COUNT(*)                AS requests,
                   SUM(estimated_cost_usd) AS cost_usd,
                   MAX(created_at)         AS last_at
            FROM usage_logs
            GROUP BY project
            ORDER BY cost_usd DESC
        """).fetchall()

    return {
        "totals":     dict(totals) if totals else {},
        "by_model":   [dict(r) for r in by_model],
        "by_day":     [dict(r) for r in by_day],
        "by_session": [dict(r) for r in by_session],
        "by_tool":    [dict(r) for r in by_tool],
        "by_project": [dict(r) for r in by_project],
    }


# ── Credit Config ─────────────────────────────────────────────────────────────

def credit_get() -> dict:
    """Return stored credit config or defaults."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM credit_config WHERE id = 1").fetchone()
        if row:
            return dict(row)
        return {
            "starting_balance": 0.0, "alert_threshold": 1.0, "warning_threshold": 5.0,
            "period_start": None, "prev_period_start": None, "prev_period_end": None,
            "prev_period_cost_usd": 0.0, "prev_period_days": 0,
        }


def _period_spend(conn, period_start: str | None, project: str | None) -> tuple[float, int]:
    """Sum cost and count distinct active days in usage_logs, optionally since period_start / for one project."""
    where_parts, params = [], []
    if period_start:
        where_parts.append("created_at >= ?")
        params.append(period_start)
    if project:
        where_parts.append("project = ?")
        params.append(project)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    row = conn.execute(f"""
        SELECT SUM(estimated_cost_usd) AS cost_usd, COUNT(DISTINCT DATE(created_at)) AS active_days
        FROM usage_logs {where}
    """, params).fetchone()
    return (row["cost_usd"] or 0.0), (row["active_days"] or 0)


def credit_status(project: str | None = None) -> dict:
    """Credit config plus spend/active-days for the *current* tracking period (since last reset, or all-time if never reset)."""
    cfg = credit_get()
    with get_connection() as conn:
        cost_usd, active_days = _period_spend(conn, cfg.get("period_start"), project)
    cfg["period_cost_usd"] = cost_usd
    cfg["period_active_days"] = active_days
    return cfg


def credit_set(starting_balance: float, alert_threshold: float = 1.0, reset: bool = False, warning_threshold: float | None = None) -> None:
    """Save or update the starting credit balance and alert thresholds.

    If reset=True, snapshot the outgoing period's spend/days into prev_period_*
    columns (global, not project-scoped — a real balance top-up applies to the
    whole account) and start a new period from now. Never touches usage_logs —
    historical charts are unaffected.
    """
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if reset:
            old_row = conn.execute("SELECT period_start FROM credit_config WHERE id = 1").fetchone()
            old_period_start = old_row["period_start"] if old_row else None
            prev_cost, prev_days = _period_spend(conn, old_period_start, project=None)
            conn.execute("""
                INSERT INTO credit_config
                  (id, starting_balance, alert_threshold, warning_threshold, period_start,
                   prev_period_start, prev_period_end, prev_period_cost_usd, prev_period_days, updated_at)
                VALUES (1, ?, ?, COALESCE(?, 5.0), ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    starting_balance = ?, alert_threshold = ?, warning_threshold = COALESCE(?, warning_threshold), period_start = ?,
                    prev_period_start = ?, prev_period_end = ?, prev_period_cost_usd = ?, prev_period_days = ?, updated_at = ?
            """, (
                starting_balance, alert_threshold, warning_threshold, now, old_period_start, now, prev_cost, prev_days, now,
                starting_balance, alert_threshold, warning_threshold, now, old_period_start, now, prev_cost, prev_days, now,
            ))
        else:
            conn.execute("""
                INSERT INTO credit_config (id, starting_balance, alert_threshold, warning_threshold, updated_at)
                VALUES (1, ?, ?, COALESCE(?, 5.0), ?)
                ON CONFLICT(id) DO UPDATE SET starting_balance = ?, alert_threshold = ?, warning_threshold = COALESCE(?, warning_threshold), updated_at = ?
            """, (starting_balance, alert_threshold, warning_threshold, now, starting_balance, alert_threshold, warning_threshold, now))
        conn.commit()


def mark_alert_sent() -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_alert_sent_at = ? WHERE id = 1", (datetime.now().isoformat(),))
        conn.commit()


def clear_alert_cooldown() -> None:
    """Reset the critical-tier cooldown once balance recovers above threshold, so
    the next drop alerts immediately instead of waiting out a stale window."""
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_alert_sent_at = NULL WHERE id = 1")
        conn.commit()


def mark_warning_sent() -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_warning_sent_at = ? WHERE id = 1", (datetime.now().isoformat(),))
        conn.commit()


def clear_warning_cooldown() -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_warning_sent_at = NULL WHERE id = 1")
        conn.commit()


def mark_spike_alert_sent(date_str: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_spike_alert_date = ? WHERE id = 1", (date_str,))
        conn.commit()


def mark_digest_sent(date_str: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_digest_sent_date = ? WHERE id = 1", (date_str,))
        conn.commit()


def mark_web_search_budget_alert_sent(date_str: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE credit_config SET last_web_search_budget_alert_date = ? WHERE id = 1", (date_str,))
        conn.commit()


# ── Alert query helpers ───────────────────────────────────────────────────────

def total_cost_for_date(date_str: str, project: str | None = None) -> float:
    """Sum estimated_cost_usd for one calendar date (YYYY-MM-DD), optionally one project."""
    where_parts, params = ["DATE(created_at) = ?"], [date_str]
    if project:
        where_parts.append("project = ?")
        params.append(project)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT SUM(estimated_cost_usd) AS cost_usd
            FROM usage_logs WHERE {" AND ".join(where_parts)}
        """, params).fetchone()
    return row["cost_usd"] or 0.0


def web_search_cost_for_date(date_str: str, project: str | None = None) -> float:
    """Sum web_search's flat per-use fee for one calendar date."""
    where_parts, params = ["DATE(created_at) = ?"], [date_str]
    if project:
        where_parts.append("project = ?")
        params.append(project)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT SUM(web_search_requests) AS requests
            FROM usage_logs WHERE {" AND ".join(where_parts)}
        """, params).fetchone()
    return (row["requests"] or 0) * _WEB_SEARCH_COST_PER_USE


def trailing_daily_average(before_date: str, days: int = 7, project: str | None = None) -> float:
    """Average daily spend over the `days` calendar dates strictly before `before_date`."""
    where_parts, params = ["DATE(created_at) < ?", "DATE(created_at) >= DATE(?, ?)"], [before_date, before_date, f"-{days} days"]
    if project:
        where_parts.append("project = ?")
        params.append(project)
    with get_connection() as conn:
        row = conn.execute(f"""
            SELECT SUM(estimated_cost_usd) AS cost_usd, COUNT(DISTINCT DATE(created_at)) AS active_days
            FROM usage_logs WHERE {" AND ".join(where_parts)}
        """, params).fetchone()
    active_days = row["active_days"] or 0
    return (row["cost_usd"] or 0.0) / active_days if active_days > 0 else 0.0


def daily_digest(date_str: str, project: str | None = None) -> dict:
    """Spend, tokens, request count, and top 3 tools for one calendar date — used
    by the daily Discord digest."""
    where_parts, params = ["DATE(created_at) = ?"], [date_str]
    if project:
        where_parts.append("project = ?")
        params.append(project)
    where = " AND ".join(where_parts)
    with get_connection() as conn:
        totals = conn.execute(f"""
            SELECT COUNT(*) AS requests, SUM(estimated_cost_usd) AS cost_usd,
                   SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
            FROM usage_logs WHERE {where}
        """, params).fetchone()
        top_tools = conn.execute(f"""
            SELECT json_each.value AS tool_name, COUNT(*) AS calls
            FROM usage_logs, json_each(usage_logs.tools_used)
            WHERE {where}
            GROUP BY json_each.value
            ORDER BY calls DESC
            LIMIT 3
        """, params).fetchall()
    return {
        "requests": totals["requests"] or 0,
        "cost_usd": totals["cost_usd"] or 0.0,
        "input_tokens": totals["input_tokens"] or 0,
        "output_tokens": totals["output_tokens"] or 0,
        "top_tools": [dict(r) for r in top_tools],
    }


# ── Server config (API key persistence — see docs/DESIGN.md §5) ───────────────

def server_config_get() -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM server_config WHERE id = 1").fetchone()
        return dict(row) if row else None


def server_config_set_api_key(api_key: str) -> None:
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO server_config (id, api_key, generated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET api_key = ?, generated_at = ?
        """, (api_key, datetime.now().isoformat(), api_key, datetime.now().isoformat()))
        conn.commit()
