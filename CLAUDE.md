# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Current status

**v1 is done and verified end-to-end, not just "implemented."** Backend, both client SDKs,
Docker packaging, and `pip install git+https://github.com/vijayanan6/SpendGaugeAI.git` have all
been run for real (not just read/reviewed) and confirmed working — see the git log for the
packaging bugs that surfaced and got fixed doing that (`93b6393`, `020ce0f`). `docs/DESIGN.md` is
still the spec and `docs/mockup.html` remains the approved visual reference — read DESIGN.md in
full before changing behavior, and if implementation forces a real change to the design, update
that file rather than letting it silently drift out of date.

Both official client SDKs exist, are tested, and have their own docs: Python
(`src/spendgaugeai/client.py`) and JS/TS (`clients/js/` — the one place Node/npm legitimately
exists in this repo, scoped to that subfolder's own build; see `clients/js/README.md`).
`docs/mockup.html`'s CSS was ported into `static/src/input.css` as-is rather than reinterpreted
into Tailwind utility classes (Tailwind's role here is the standalone-binary build tool +
preflight reset, not a rewrite of the hand-tuned CSS) — Vijay has frontend design changes to
apply on top of this once he's ready, so don't assume the current visual pass is final.

The MCP Learning Project dogfooding wire-up (§10) is done and actually working — that project's
`src/backend/api.py` has a best-effort `SpendGaugeAIClient.alog()` call alongside its existing
local `usage_log()`, gated on `SPENDGAUGEAI_URL`/`SPENDGAUGEAI_API_KEY` plus the `spendgaugeai`
package being installed, committed there as `e49c23d` and pushed to `origin/main`; its API key
was found mismatched and fixed this session, verified live (`200 {"logged":true}`) rather than
just assumed correct. That project's own local `/usage` dashboard is unaffected.

**Only genuinely open items:** the real PyPI/npm publish — manual steps Vijay runs when ready,
exact commands in `docs/PUBLISHING.md` — and Vijay's own pending frontend redesign pass (not
started).

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
│   ├── PUBLISHING.md           # exact PyPI/npm publish commands — the runbook for the one
│   │                              manual step every other doc defers to Vijay
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
│   ├── README.md                # to this subfolder's own build; does not touch the server's
│   ├── LICENSE                  # Node-free runtime/build story. README + LICENSE are this
│   ├── src/                     # package's own copies — npm's registry page reads from here,
│   └── tsup.config.ts           # not the repo root.
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
- **`wrap()` has eight specific, resolved edges — implement all eight, don't ship a naive
  version.** The first four came from design review; edges 5–6 were only found by wiring a real
  app to a real `tool_runner`-based agentic loop and a real production server — every prior test
  used fake Python clients built from plain `async def` functions, which happened to mask both;
  edges 7–8 were only found by real Pragya dogfooding traffic (an Advisor-tool multi-model call
  for edge 7, 1-hour cache TTL writes for edge 8). (1)
  patch `messages.create` **and** `messages.stream`, on **both** `Anthropic` and `AsyncAnthropic`
  — patching only the sync client silently covers nothing for an async-only app; (2) streaming
  reports from the final accumulated message inside a `finally`, forwarding every event unchanged
  to the caller — without this, streaming calls (the majority pattern) go silently unreported;
  (3) `session_id` propagates via a `contextvars.ContextVar` (`client.spendgauge_session(...)`),
  never a mutable attribute on the client object — a shared long-lived client under concurrent
  requests would otherwise clobber sessions together; (4) `tools_used` comes from **both**
  `tool_use`- and `server_tool_use`-type content blocks (the latter covers server-side tools like
  Advisor/`web_fetch`/`code_execution` that never surface as plain `tool_use`, and was originally
  missed — see edge 7), `web_search_requests` comes from `response.usage.server_tool_use` — two
  different places, not one; (5) **patching `client.messages` alone does NOT cover
  `tool_runner`** — `client.beta.messages` is a genuinely separate resource object in the real SDK
  (`client.messages is client.beta.messages` → `False`, confirmed live), and `tool_runner` lives
  exclusively on `.beta`; `wrap()` must patch both resources, and must also patch `.parse()`
  alongside `.create()`/`.stream()` on each — `tool_runner`'s non-streaming mode calls `.parse()`,
  never `.create()`; (6) **sync/async branch selection can't use
  `inspect.iscoroutinefunction(original_create)` directly** — the real SDK wraps its `async def`
  methods in an internal sync dispatcher that makes this report `False` even though normal callers
  can `await` it fine; use `inspect.iscoroutinefunction(inspect.unwrap(original_create))` instead,
  or the sync branch runs against a real async client and reports on an **unawaited coroutine**
  (all-zero tokens/cost, real API call still works — which is exactly why this was invisible until
  tested against a real client); (7) **a response can carry `response.usage.iterations`** — a
  per-sub-inference breakdown present when a server-side agentic loop invoked a second,
  differently-priced model mid-response (the Advisor tool's motivating case: executor on one
  model, advisor consultation on another). Pricing the whole response at the flat top-level
  `model` silently undercounts these calls; `wrap()` must extract `iterations` alongside
  `tools_used`/`web_search_requests` and forward it on `/usage/log` so the server can sum
  per-iteration cost instead (§4 of `docs/DESIGN.md`) — absent/empty for the overwhelming
  majority of calls, so this is additive, not a behavior change for normal traffic; (8) **1-hour
  cache TTL writes cost 2x input, not 1.25x** — real Anthropic pricing has two cache-write tiers
  (1.25x input for the default 5-minute breakpoint, 2x for an explicit
  `cache_control: {..., "ttl": "1h"}` breakpoint), but this project's pricing only ever knew about
  the 5-minute rate, silently undercounting every 1h-tier write. Found investigating a real ~$0.29
  gap between this project's own tracked spend and the actual Anthropic console balance — Pragya
  caches its system prompt and tool schema at 1h TTL, and that mispricing accounted for roughly
  55% of the gap. The real API breaks the flat `cache_creation_input_tokens` down by tier on
  `usage.cache_creation.ephemeral_1h_input_tokens`/`.ephemeral_5m_input_tokens`; `wrap()` extracts
  the 1h figure and forwards it as `cache_write_1h_tokens` alongside the existing flat count, and
  the server splits the two rates instead of flat-pricing every write at the 5-minute rate —
  absent/`0` for the overwhelming majority of calls (no 1h TTL breakpoint used), so this is
  additive too. See §8a of `docs/DESIGN.md` for the exact extraction code and full story behind
  edges 5–8.

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
