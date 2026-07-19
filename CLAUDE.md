# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Current status

**Implementation in progress — v1 backend + a first-pass frontend exist and work end-to-end.**
`docs/DESIGN.md` is the spec and `docs/mockup.html` remains the approved visual reference — still
read DESIGN.md in full before changing behavior, and if implementation forces a real change to
the design, update that file rather than letting it silently drift out of date.

Both official client SDKs exist and are tested: Python (`src/spendgaugeai/client.py`) and
JS/TS (`clients/js/` — the one place Node/npm legitimately exists in this repo, scoped to that
subfolder's own build). `docs/mockup.html`'s CSS was ported into `static/src/input.css` as-is
rather than reinterpreted into Tailwind utility classes (Tailwind's role here is the
standalone-binary build tool + preflight reset, not a rewrite of the hand-tuned CSS) — Vijay has
frontend design changes to apply on top of this once he's ready, so don't assume the current
visual pass is final.

The MCP Learning Project dogfooding wire-up (§10) is done — that project's `src/backend/api.py`
has a best-effort `SpendGaugeAIClient.alog()` call alongside its existing local `usage_log()`,
gated on `SPENDGAUGEAI_URL`/`SPENDGAUGEAI_API_KEY` plus the `spendgaugeai` package being
installed, committed there as `e49c23d` and pushed to `origin/main`. That project's own local
`/usage` dashboard is unaffected. Not yet done: a real PyPI/npm publish (manual steps Vijay runs
when ready, per docs/DESIGN.md §9).

## What this project is

SpendGaugeAI is **AI FinOps for self-hosted developers** — a budget control center for Claude
API spend, not a usage-log viewer. That distinction is a first-class design principle (§1 of
`docs/DESIGN.md`), not marketing framing: every feature and every piece of UI copy gets held
against "does this help set/enforce a budget policy" vs. "does this just display what
happened." Don't describe this project as a "cost tracker" or "dashboard" in new docs/copy —
those words describe the earlier, weaker framing this was deliberately corrected away from.

Standalone, self-hosted, pip-installable — extracted from the MCP Learning Project's proven
`/usage` dashboard (`c:\Users\vijay\OneDrive\Desktop\Claude Workspace\MCP Project\src\backend\database.py`
/ `api.py`). It is a **separate product in a separate repo**, not a subfolder of that project.
Full rationale, architecture, data model, API contract, and design tokens are in
`docs/DESIGN.md` — this file won't duplicate that content, only point to it.

## Planned folder structure

```
SpendGaugeAI/
├── pyproject.toml            # hatchling backend, console_scripts entry point, includes
│                                compiled static/ assets as package data
├── README.md
├── LICENSE                   # MIT
├── .env.example
├── Dockerfile                 # single-stage (python:3.12-slim) — one RUN step compiles
│                                Tailwind CSS via the standalone binary, no Node anywhere
├── docker-compose.yml
├── docs/
│   ├── DESIGN.md               # the spec — read before implementing
│   └── mockup.html             # approved visual/interaction reference — unlike a React plan,
│                                  this one's HTML/CSS/JS carry over closely, split into Jinja2
│                                  partials + Alpine components rather than reinterpreted
├── src/spendgaugeai/
│   ├── __init__.py            # exports SpendGaugeAIClient
│   ├── cli.py                 # `spendgaugeai serve` — argparse + uvicorn.run
│   ├── app.py                 # FastAPI app + routes; Jinja2Templates + StaticFiles mount
│   ├── database.py            # usage/credit/alert logic, ported from the source project
│   ├── alerts.py               # Discord alert checks, ported from the source project's api.py
│   ├── auth.py                 # API-key dependency for POST /usage/log and POST /usage/credit
│   ├── client.py               # SpendGaugeAIClient — wrap() and .log(), both fail-safe
│   ├── templates/              # Jinja2: base.html (shared nav/header/theme toggle) + one
│   │                              template per page (usage.html now, more as pages are added)
│   └── static/                 # src/input.css (Tailwind source), compiled app.css (committed
│                                  build output — see below), vendored alpine.min.js, chart JS —
│                                  do not hand-edit app.css, it's generated
├── clients/js/                # spendgaugeai-client npm package — the ONE place Node/npm
│   ├── package.json            # legitimately exists in this repo (§8a of DESIGN.md). Scoped
│   ├── src/                    # to this subfolder's own build; does not touch the server's
│   └── tsup.config.ts          # Node-free runtime/build story.
└── tests/
    └── test_app.py
```

## Planned commands (once scaffolded)

```bash
./scripts/build-css.sh                          # downloads the Tailwind standalone binary if
                                                    needed, compiles src/spendgaugeai/static/app.css
pip install -e .
spendgaugeai serve [--host 0.0.0.0] [--port 8000] [--db-path ./data/spendgaugeai.db]
docker compose up --build                        # single RUN step compiles CSS, no Node stage
pytest tests/                                     # backend
# Playwright E2E against the running app covers the frontend — no separate JS test framework
```

## Key design decisions (see docs/DESIGN.md for full reasoning)

- **Cost computed server-side**, not by the reporting client. Clients send raw token counts;
  SpendGaugeAI owns `_PRICING` and computes `estimated_cost_usd` itself — one place to fix pricing
  drift instead of one per integration.
- **Everything requires a credential — no open routes except `/health`.** This was tightened
  from an earlier revision that left `/usage`/`/usage/data` open; corrected on explicit
  requirement. Two schemes sharing **one secret**: `Authorization: Bearer <SPENDGAUGEAI_API_KEY>`
  for machine calls (`POST /usage/log`, `POST /usage/credit`), **HTTP Basic Auth** (fixed
  username `spendgaugeai`, the API key as the password) for browser access (`GET /usage`,
  `GET /usage/data`, `GET /static/*`) — Basic because it's the one scheme browsers handle
  natively with no custom login page needed. Don't build a login page/session/cookie system;
  don't drop auth from any route without being asked — both the scheme choice and the "gate
  everything" requirement were deliberate, not left unconsidered.
- **Env-var key precedence is exclusive, not a fallback pair.** Once `SPENDGAUGEAI_API_KEY` is
  set via the environment, it is the *only* valid key — do not also accept the persisted
  `server_config` value as an alternative match. Implementing this as "matches env-var-key OR
  persisted-key" would leave the original auto-generated key (which got printed to a startup
  log — Docker logs, CI output, a scrollback somewhere) permanently valid even after someone
  deliberately rotated to their own key. This is a real security requirement, not a style
  preference.
- **The API key must never be silently unset.** If `SPENDGAUGEAI_API_KEY` isn't in the
  environment, generate one on first boot, persist it in `server_config` (§3 of DESIGN.md), and
  print it once at startup. An env var always wins over the persisted one. There must never be a
  code path where a missing key means "accept unauthenticated writes."
- **SQLite runs in WAL mode** (`PRAGMA journal_mode=WAL`, set once in `init_db()`) — not
  optional. Multiple apps reporting concurrently is the actual product use case, unlike the
  source project where only one process ever wrote to the file.
- **`POST /usage/log` has real input guardrails**: Pydantic length caps on `project`/
  `session_id`/`tools_used`, a max request body size, and a simple in-memory per-key rate limit.
  No new external dependency (no Redis) — stay proportionate to "one process, no external
  services."
- **No `notes`/`sessions` tables.** Those were specific to the source project's chat app, not to
  cost tracking. Don't reintroduce them here.
- **`static/app.css` is committed, not gitignored.** Reversed from the original design (which
  treated it as a pure build artifact, generated fresh before packaging) once `pip install
  git+https://github.com/...` needed to work — pip builds straight from a raw clone with no
  chance to run `scripts/build-css.sh` first, and hatchling's `force-include` errors outright if
  the file isn't there. Same treatment as the already-committed `alpine.min.js`/chart JS: after
  editing `static/src/input.css`, re-run `./scripts/build-css.sh` and commit the regenerated
  `app.css` in the same commit as the source change — don't let it drift out of sync.
- **Frontend is Jinja2 + Alpine.js + Tailwind (standalone CLI) — not React, not vanilla HTML
  with no reactivity either.** The stack went through two revisions: plain HTML/CSS/JS →
  React (for "competitive UI") → Alpine.js/Jinja2 (once 2-3 more planned pages made clear this
  is a multi-page *server-rendered* admin app, not a SPA — React's real value doesn't apply, and
  Alpine.js delivers the same visual/interaction quality with zero npm dependency surface).
  **Do not reintroduce React/Vite/npm/`node_modules` without being asked** — this was a
  deliberate, twice-reconsidered call, not an unexamined default. There must be **no Node
  anywhere** — not runtime, not build time, not dev time. The Tailwind CLI is a downloaded
  standalone binary, never the npm package.
- **`docs/mockup.html` is the primary implementation source, not just a reference.** Unlike the
  React plan, its HTML/CSS/JS carry over closely — split into `templates/base.html` +
  page-specific Jinja2 templates, with its inline `<script>` refactored into Alpine `x-data`
  components rather than reinterpreted through a different framework's idioms. Its hand-built
  SVG charts get vendored directly, not replaced by a charting library. Design tokens (the
  indigo/amber palette) port into the Tailwind CSS source input unchanged — don't reinvent them.
- **Multi-page navigation is real page loads, not client-side routing.** Each page (`/usage` now,
  2-3 more later) is its own FastAPI route rendering its own Jinja2 template — same pattern the
  source project already uses for `/`, `/usage`, `/logs`. No router, no shared client-side app
  state across pages needed for this to work well.
- **Language-independent integration is a first-class goal, not a Python-first-with-HTTP-as-
  fallback afterthought.** The `POST /usage/log` HTTP contract *is* the product interface.
  Python (`spendgaugeai`) and JS/TS (`spendgaugeai-client`, `clients/js/`) both get official SDKs
  in v1 — this was corrected during design review specifically because treating one as primary
  and the other as a deferred fast-follow contradicted the "any AI app" goal. Both SDKs must
  offer `wrap()` (auto-report by wrapping an app's existing Anthropic client, the recommended
  path) and `.log()` (manual, for per-call control) — see §8a of `docs/DESIGN.md`. Both must
  fail silently with a short timeout; reporting usage must never be able to break the app using
  it. Any other language gets documented raw-HTTP/`curl` examples, not a promise of a future SDK.
- **`session_id` is optional on `POST /usage/log`**, server-generates a UUID if omitted. Found
  in review: the original schema had it `NOT NULL` with no default, which blocked even the
  simplest integration (a script with no session concept) from sending its first request.
- **`wrap()` has four specific, resolved edges — implement all four, don't ship a naive version:**
  (1) patch `messages.create` **and** `messages.stream`, on **both** `Anthropic` and
  `AsyncAnthropic` — patching only the sync client silently covers nothing for the source
  project's dogfooding target, which is async-only; (2) streaming reports from the final
  accumulated message inside a `finally`, forwarding every event unchanged to the caller —
  without this, streaming calls (the majority pattern) go silently unreported; (3) `session_id`
  propagates via a `contextvars.ContextVar` (`client.spendgauge_session(...)`), never a mutable
  attribute on the client object — a shared long-lived client under concurrent requests would
  otherwise clobber sessions together; (4) `tools_used` comes from `tool_use`-type content
  blocks, `web_search_requests` comes from `response.usage.server_tool_use` — two different
  places, not one; see §8a of `docs/DESIGN.md` for the exact extraction code. Patching at the
  `messages.create`/`messages.stream` level (not a higher convenience wrapper) means
  `tool_runner`'s internal loop is covered automatically — don't special-case it.

## Relationship to the MCP Learning Project

SpendGaugeAI is dogfooded by that project (see `docs/DESIGN.md` §10) once v1 works: a best-effort
`SpendGaugeAIClient` call gets added alongside its existing local usage logging, gated by optional
env vars, never able to break a real chat response if SpendGaugeAI is unreachable. That project's
own local `/usage` dashboard is unaffected — this only adds a second, independent reporting path.
Do not modify anything in `MCP Project/` as part of building SpendGaugeAI itself; the dogfooding
wire-up is a distinct, later step against that project's `api.py`.

## Git / commits

Separate git repo (`git init` already run in this folder). Follow the same discipline as the
source project: create commits only when explicitly asked, never with `--no-verify`, and check
`git status` for anything unexpected before staging.
