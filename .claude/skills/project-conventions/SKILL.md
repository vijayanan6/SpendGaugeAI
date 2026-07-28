---
name: project-conventions
description: Pre-commit self-check for SpendGaugeAI's hard, easy-to-regress rules — no Node outside clients/js, committed build artifacts must stay in sync, auth precedence must stay exclusive, wrap()'s six resolved edge cases. Use before finishing a change that touches static/src/input.css, auth.py, client.py, clients/js/, or that adds a new dependency/framework.
user-invocable: false
---

# Project Conventions Self-Check

CLAUDE.md already documents the full rationale for these — this skill is a compact checklist for
catching a regression *before* finishing a change, not a replacement for reading CLAUDE.md. Run
through whichever items are relevant to what just changed; skip the rest.

## If you touched `static/src/input.css`
- [ ] Did you also re-run `scripts/build-css.sh` and commit the regenerated
      `src/spendgaugeai/static/app.css` in the same commit? (There's a hook for this —
      `.claude/hooks/rebuild_css.py` — but double-check it actually ran.)

## If you touched `src/spendgaugeai/auth.py` or route auth wiring in `app.py`
- [ ] Is `SPENDGAUGEAI_API_KEY` precedence still *exclusive* (env wins outright, never "env OR
      persisted")?
- [ ] Does every non-`/health` route still carry an auth dependency?
- [ ] Are all credential comparisons still via `secrets.compare_digest`, never `==`?
- [ ] Consider invoking the `security-reviewer` subagent for anything non-trivial here.

## If you touched `src/spendgaugeai/client.py` or `clients/js/src/` (the `wrap()` implementations)
- [ ] Sync **and** async variants patched, on **both** the plain client and its `.beta` resource?
- [ ] Streaming reports from the final accumulated message in a `finally`, forwarding events
      unchanged?
- [ ] `session_id` propagates via `contextvars.ContextVar` (Python) / equivalent isolation (JS),
      never a mutable attribute on a shared client?
- [ ] `tools_used` from `tool_use` content blocks, `web_search_requests` from
      `response.usage.server_tool_use` — not conflated?
- [ ] `.parse()` patched alongside `.create()`/`.stream()` (needed for `tool_runner`'s
      non-streaming path)?
- [ ] Sync/async dispatch checked via `inspect.iscoroutinefunction(inspect.unwrap(original))`, not
      a direct check that the real SDK's dispatcher wrapper defeats?

## If you're adding a new dependency, framework, or build step anywhere in the repo
- [ ] Node/npm stays confined to `clients/js/` — nothing added to the server side introduces a
      Node runtime or build-time dependency.
- [ ] No React/Vite/SPA routing reintroduced on the frontend — it's Jinja2 + Alpine.js + Tailwind
      (standalone CLI) by deliberate, twice-reconsidered decision.

## If you touched `docs/DESIGN.md` itself
- [ ] Does the change reflect an actual, deliberate design decision (and ideally note *why*), not
      just implementation drift being back-filled into the spec?
