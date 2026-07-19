"""
alerts.py — Discord alert checks (docs/DESIGN.md §7).

Ported from the MCP Learning Project's `_run_alert_checks` (api.py), triggered
from POST /usage/log instead of a chat endpoint. Same six alert types, same
independent per-check cooldown discipline so nothing spams Discord and one
failing check (e.g. a DB error) never skips the others or breaks the caller's
real POST /usage/log response — every check is wrapped and swallowed.
"""
import logging
from datetime import date, datetime, timedelta

import httpx

from .database import (
    clear_alert_cooldown,
    clear_warning_cooldown,
    credit_status,
    daily_digest,
    mark_alert_sent,
    mark_digest_sent,
    mark_pricing_warning_sent,
    mark_spike_alert_sent,
    mark_warning_sent,
    mark_web_search_budget_alert_sent,
    pricing_warnings_pending,
    total_cost_for_date,
    trailing_daily_average,
    web_search_cost_for_date,
)

logger = logging.getLogger(__name__)

ALERT_COOLDOWN = timedelta(hours=24)  # low-balance tiers: min gap between repeat alerts
WEB_SEARCH_DAILY_BUDGET = 1.00        # per-tool budget alert threshold
SPIKE_MULTIPLIER = 3.0                # today's spend vs trailing average
SPIKE_MIN_ABSOLUTE = 1.00             # floor so a near-zero average can't trigger noise


def _webhook_url() -> str | None:
    import os
    return os.environ.get("DISCORD_WEBHOOK_URL")


async def _send_discord(message: str) -> bool:
    """POST a message to the Discord webhook. Never raises — a failed alert
    must never break the caller's real POST /usage/log response."""
    url = _webhook_url()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"content": message})
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"[alert] Discord webhook failed: {e}")
        return False


async def _maybe_send_low_credit_alert() -> None:
    """Two-tier alert as remaining balance drops: warning_threshold (default
    $5) then alert_threshold (default $1, the "critical" tier). Each tier has
    its own cooldown so it won't spam Discord while balance stays low; each
    cooldown clears as soon as balance recovers back above that tier's
    threshold, so the next drop alerts immediately instead of waiting out a
    stale window. Critical takes priority over a redundant warning.
    """
    cfg = credit_status()
    starting_balance = cfg.get("starting_balance") or 0
    if starting_balance <= 0:
        return  # no balance configured — nothing to alert on

    alert_threshold = cfg.get("alert_threshold") or 1.0
    warning_threshold = cfg.get("warning_threshold") or 5.0
    remaining = max(starting_balance - (cfg.get("period_cost_usd") or 0), 0)

    if remaining <= alert_threshold:
        if cfg.get("last_warning_sent_at"):
            clear_warning_cooldown()
        last_sent = cfg.get("last_alert_sent_at")
        if last_sent and (datetime.now() - datetime.fromisoformat(last_sent)) < ALERT_COOLDOWN:
            return
        message = (
            f"🔴 **SpendGaugeAI — CRITICAL low credit**\n"
            f"Remaining: **${remaining:.2f}** (critical threshold: ${alert_threshold:.2f})\n"
            f"Starting balance: ${starting_balance:.2f}"
        )
        if await _send_discord(message):
            mark_alert_sent()
        return

    if cfg.get("last_alert_sent_at"):
        clear_alert_cooldown()

    if remaining <= warning_threshold:
        last_warned = cfg.get("last_warning_sent_at")
        if last_warned and (datetime.now() - datetime.fromisoformat(last_warned)) < ALERT_COOLDOWN:
            return
        message = (
            f"🟡 **SpendGaugeAI — low credit warning**\n"
            f"Remaining: **${remaining:.2f}** (warning threshold: ${warning_threshold:.2f})\n"
            f"Starting balance: ${starting_balance:.2f}"
        )
        if await _send_discord(message):
            mark_warning_sent()
        return

    if cfg.get("last_warning_sent_at"):
        clear_warning_cooldown()


async def _maybe_send_spend_spike_alert() -> None:
    """Alert when today's spend is unusually high vs. the trailing 7-day daily
    average — catches a runaway loop or bug causing spend, not just the low
    balance that results from it. Capped at once per day. A minimum absolute
    floor avoids false positives when the trailing average is near-zero."""
    today_str = date.today().isoformat()
    cfg = credit_status()
    if cfg.get("last_spike_alert_date") == today_str:
        return

    today_cost = total_cost_for_date(today_str)
    if today_cost < SPIKE_MIN_ABSOLUTE:
        return

    avg = trailing_daily_average(today_str, days=7)
    if avg <= 0 or today_cost < avg * SPIKE_MULTIPLIER:
        return

    message = (
        f"📈 **SpendGaugeAI — spend spike detected**\n"
        f"Today so far: **${today_cost:.2f}** vs. 7-day average **${avg:.2f}**/day "
        f"({today_cost / avg:.1f}x)\n"
        f"Worth checking for a runaway loop or unexpected tool usage."
    )
    if await _send_discord(message):
        mark_spike_alert_sent(today_str)


async def _maybe_send_daily_digest() -> None:
    """Recap of yesterday's usage on the first request of each new day. No
    background scheduler — this app isn't guaranteed to be running at any
    fixed wall-clock time, so the digest piggybacks on real traffic."""
    today_str = date.today().isoformat()
    cfg = credit_status()
    if cfg.get("last_digest_sent_date") == today_str:
        return

    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    d = daily_digest(yesterday_str)
    top_tools = ", ".join(f"{t['tool_name']} ({t['calls']})" for t in d["top_tools"]) or "none"
    message = (
        f"📋 **SpendGaugeAI — daily digest** ({yesterday_str})\n"
        f"Spend: **${d['cost_usd']:.2f}** · Requests: {d['requests']} · "
        f"Tokens: {d['input_tokens'] + d['output_tokens']:,}\n"
        f"Top tools: {top_tools}"
    )
    starting_balance = cfg.get("starting_balance") or 0
    if starting_balance > 0:
        remaining = max(starting_balance - (cfg.get("period_cost_usd") or 0), 0)
        message += f"\nAvailable credit: **${remaining:.2f}**"
    if await _send_discord(message):
        mark_digest_sent(today_str)


async def _maybe_send_web_search_budget_alert() -> None:
    """Alert if web_search alone (the one tool with a real $/use fee) exceeds
    WEB_SEARCH_DAILY_BUDGET today. Capped at once per day."""
    today_str = date.today().isoformat()
    cfg = credit_status()
    if cfg.get("last_web_search_budget_alert_date") == today_str:
        return

    cost = web_search_cost_for_date(today_str)
    if cost < WEB_SEARCH_DAILY_BUDGET:
        return

    message = (
        f"🔎 **SpendGaugeAI — web_search budget exceeded**\n"
        f"web_search cost today: **${cost:.2f}** (budget: ${WEB_SEARCH_DAILY_BUDGET:.2f})\n"
        f"At $0.01/search, that's {round(cost / 0.01)} searches so far today."
    )
    if await _send_discord(message):
        mark_web_search_budget_alert_sent(today_str)


async def _maybe_send_pricing_warning_alert() -> None:
    """Alert for any model that hit _PRICING's fallback (an unrecognized model
    reported without a real pricing entry). One-time per model, not a daily
    cooldown — a config gap, not a spend threshold."""
    pending = pricing_warnings_pending()
    if not pending:
        return

    models_list = ", ".join(f"`{m}`" for m in pending)
    message = (
        f"⚠️ **SpendGaugeAI — missing pricing data**\n"
        f"No `_PRICING` entry for: {models_list}\n"
        f"Costs for these are being estimated using Sonnet's rate as a fallback — "
        f"add a real entry in `database.py`'s `_PRICING` dict."
    )
    if await _send_discord(message):
        for model in pending:
            mark_pricing_warning_sent(model)


async def run_alert_checks() -> None:
    """Run all Discord alert checks after a request logs usage. Each check is
    isolated — one failing must not skip the others or ever break the
    caller's real POST /usage/log response."""
    if not _webhook_url():
        return
    for check in (
        _maybe_send_low_credit_alert,
        _maybe_send_spend_spike_alert,
        _maybe_send_daily_digest,
        _maybe_send_web_search_budget_alert,
        _maybe_send_pricing_warning_alert,
    ):
        try:
            await check()
        except Exception as e:
            logger.warning(f"[alert] {check.__name__} failed: {e}")
