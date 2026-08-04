# SpendGaugeAI — Design Document

Status: **design approved, implementation not started**. This document is the reference for
implementation — read it before writing code, and update it if implementation forces a real
design change (don't let it silently drift stale).

## Tech stack — finalized

| Layer | Choice | Why (detail in the section noted) |
|---|---|---|
| Backend language/framework | Python + FastAPI | Proven pattern, direct port of the source project's own stack |
| Database | SQLite, **WAL mode** | Single file, no external service, WAL handles concurrent reporting apps (§3) |
| ASGI server | uvicorn | Standard FastAPI pairing |
| HTTP client (for `SpendGaugeAIClient` + Discord webhook) | httpx | Already proven in the source project |
| Validation | Pydantic (via FastAPI) | Request models + the length/size guardrails in §4 |
| CLI | stdlib `argparse` | No CLI-framework dependency for one command (`spendgaugeai serve`) |
| Backend build | `hatchling` (`pyproject.toml`) | `pip install spendgaugeai`, `[project.scripts]` entry point (§9) |
| Templating | Jinja2 (via FastAPI's `Jinja2Templates`) | Shared `base.html` layout (nav/header/theme toggle) across all pages once multi-page growth (§1) starts — a template engine, not a framework; already a standard FastAPI-ecosystem dependency |
| Frontend interactivity | Alpine.js (single vendored file, ~15KB) | Real reactive bindings/transitions for the config panel, theme, filters — chosen over React after a second design pass: same visual/interaction quality for a multi-page admin dashboard, none of the build-pipeline cost (§8) |
| Styling | Tailwind CSS, **standalone CLI** (no npm) | Same utility classes as the approved `mockup.html`, ported directly — not reinterpreted through JSX, zero drift risk (§8) |
| Charts | The mockup's own hand-built SVG (reused directly) | Already built to real mark-spec standards (grid, area+line, hover tooltip, emphasized endpoint) — no charting library needed |
| Frontend state/data | Alpine `x-data` per page + `fetch()` polling, no bundler, no router | Multi-page via real FastAPI routes (same pattern the source project already uses for `/`, `/usage`, `/logs`) — no SPA router needed |
| Packaging: Docker | **Single-stage** `Dockerfile` (`python:3.12-slim`) | No Node anywhere — the Tailwind standalone CLI is a small downloaded binary, not an npm/node_modules tree (§9) |
| Packaging: pip | Compiled CSS + vendored JS included as package data | `pip install` ships everything needed; no Node at install time, dev time, or runtime (§9) |
| Auth | One shared secret, two schemes: Bearer (machine calls) + HTTP Basic (browser) | Tightened from an earlier revision — gates *every* route except `/health`, not just writes (§5) |
| Icons | Heroicons (MIT, Tailwind Labs), vendored inline `<svg>` | Natural pairing with Tailwind; `currentColor`-driven so it needs zero extra theme wiring for light/dark (§8) |
| Optional integration | Discord webhook (raw HTTP, no SDK) | No-ops cleanly if unset, same pattern as the source project (§7) |
| Client SDK: Python | `spendgaugeai` package — `SpendGaugeAIClient`, `wrap()` | Same install as the server; `wrap()` auto-reports, `.log()` for manual control (§8a) |
| Client SDK: JS/TS | `spendgaugeai-client` npm package, zero runtime deps | Separate, independently-versioned package a *JS/TS app* installs — does not affect the server's Node-free story (§8a) |
| Any other language | Raw `POST /usage/log` (JSON + Bearer token) + documented `curl` examples | No SDK required by design — the HTTP contract is the actual product interface (§8a) |
| Testing | pytest (backend) + Playwright E2E (frontend) + the JS client's own small test suite | Same testing discipline already proven on the source project |

**Deliberately excluded, and why:** Postgres/MySQL (SQLite+WAL is enough at this scale) · Redis
(the in-memory rate limiter in §4 doesn't need it at single-process scale) · React/Vite/
shadcn/ui/Recharts *for the server's dashboard* (reconsidered after the initial stack pick —
real machinery for a one-page app that's now growing to 3-4 *server-rendered* pages, not a
client-routed SPA; Alpine.js + Jinja2 gives the same visual/interaction quality with none of the
npm dependency surface) · Redux/Zustand/a client router (no cross-page client state complex
enough to justify either) · a generated OpenAPI/TS client for the dashboard's own internal
fetch (real future work, low risk at v1 scale — separate from the JS *client SDK*, which does
ship in v1 for a different reason: integration, not internal fetching) · **Node/npm in the
server**, anywhere, at any stage — not even at build time, since the Tailwind CLI ships as a
standalone binary. Node *is* used, deliberately and narrowly, inside `clients/js/`'s own build
for the JS/TS client SDK — that's a different package with a different audience (JS app
developers integrating *with* SpendGaugeAI), not a walk-back of the server decision.

Runtime *and build-time* dependency footprint for someone **running** SpendGaugeAI:
**Python + SQLite only**, regardless of what language the apps reporting to it are written in.
Someone **integrating** a JS/TS app additionally needs Node only if they choose the
`spendgaugeai-client` npm package over raw `fetch()` calls — never to run the server itself.

## 1. Problem & goals

Anyone self-hosting a Claude API app has no lightweight way to *control* spend — not just watch
it — without either (a) manually checking console.anthropic.com, or (b) adopting a hosted
observability platform (Langfuse, Helicone, Portkey) built around logs and traces, where cost is
a side effect of tracing rather than the product's center of gravity. Those platforms are good
at what they do, but none of them target "I want a budget policy and a control panel on my own
machine, zero account, docker run and done."

**Positioning, made explicit (a real correction found in review, not a cosmetic rename):**
SpendGaugeAI is **AI FinOps for self-hosted developers**, not a usage-log viewer. The
distinction matters beyond marketing copy — it's the test every future feature and every pixel
of UI gets held against: does this help someone *set and enforce a budget policy* (FinOps), or
does it just *display what happened* (logging)? The engine already leans the right way — credit
balance, alert thresholds, burn rate, runway forecasting are financial control primitives, not
passive accounting — but this was found to be under-stated in the project's own prior framing
("cost tracker," "dashboard") in a way the actual instrument-panel/gauge UI concept (§8) had
already outgrown. Docs corrected; **the UI must read as a financial control center, not a
settings page** — see the explicit principle in the Goals list below.

SpendGaugeAI fills that gap: a small, self-hosted, pip-installable service that any Claude API
app reports token usage to over HTTP, with a real budget-control panel, credit/burn-rate
tracking, and optional Discord alerts enforcing the policy you set. The engine (accounting math,
credit tracking, alert logic) is not new — it's a proven extraction from the MCP Learning
Project's `/usage` dashboard, which has already caught real billing bugs against real invoices
(see that project's `INSIGHTS.md` #33, #37).

**Goals for v1:**
- A client app can report usage with one HTTP call and see it reflected in the budget control
  panel within seconds.
- Runs with zero external account — `pip install spendgaugeai && spendgaugeai serve`, or Docker.
- Cost math lives in one place (server-side), so pricing corrections happen once, not per-client.
- **The UI reads as a financial control center, not a settings page or a log viewer.** Lead with
  status and policy (the hero gauge, burn rate, runway, budget threshold), not raw event lists.
  Configuration (starting balance, alert threshold) is *setting policy*, and should be worded
  and weighted like a financial control, not a generic form — this governs copy choices (§8's
  "Configure" panel labels) as much as layout, and applies to every future page (§1's planned
  2-3 additions), not just `/usage`.
- **Language-independent integration.** The audience is "anyone building a Claude API app," not
  "anyone building one in Python." The HTTP contract (§4) is the actual product interface;
  Python and JS/TS get official SDKs in v1 (§8a), any other language integrates against the
  same documented raw HTTP contract with no SDK required.

**Known future growth, factored into the tech stack now:** 2-3 more pages beyond `/usage` are
planned (not `/logs` — that stays out of scope per the non-goals below; exact pages TBD). This
shaped the frontend stack decision in §8 — a multi-page *server-rendered* admin app, not a
single view, but also not something needing a client-side router or cross-page app state.

**Explicit non-goals for v1** (revisit later, not forgotten):
- Multi-tenant auth (per-project API keys, user accounts) — v1 is single-operator,
  single shared secret.
- The MCP Learning Project's `/logs` log-viewer / conversations tab — that's app-specific
  (parses that app's own log file and session table), not a generic cost-tracking feature.
- A real PyPI publish — building it pip-installable is in scope; actually running
  `twine upload` is Vijay's call to make when he's ready, not an automatic step here.
- HTMX / server-rendered partials — the existing poll-and-redraw JS approach doesn't need it.

## 2. Architecture

```
┌───────────────────┐       POST /usage/log        ┌──────────────────────────┐
│  Your app          │ ──── {model, tokens, tools} ─▶│  SpendGaugeAI            │
│  (any Claude app)  │      Authorization: Bearer key│  FastAPI + SQLite        │
└───────────────────┘                                │                          │
                                                       │  GET  /usage            │──▶ dashboard (browser)
                                                       │  GET  /usage/data       │──▶ JSON
                                                       │  POST /usage/credit     │
                                                       │  GET  /health           │
                                                       └────────────┬─────────────┘
                                                                    │ (optional, no-op if unset)
                                                                    ▼
                                                              Discord webhook
```

One process, one SQLite file, no external services required. Discord alerting is the only
optional integration, and follows the same "never break the real response, no-op if unset"
pattern already proven in the source project (`_lf_finish`, `_run_alert_checks`).

## 3. Data model

Ported near-verbatim from the MCP Learning Project's `database.py` — that file already keeps
this logic cleanly separate from its own app-specific `notes`/`sessions` tables, confirmed by
reading it before extraction.

```sql
CREATE TABLE usage_logs (
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
);

CREATE TABLE credit_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    starting_balance    REAL NOT NULL DEFAULT 0,
    alert_threshold     REAL NOT NULL DEFAULT 1.0,
    warning_threshold   REAL NOT NULL DEFAULT 5.0,
    period_start        TEXT,
    prev_period_start   TEXT, prev_period_end TEXT,
    prev_period_cost_usd REAL NOT NULL DEFAULT 0, prev_period_days INTEGER NOT NULL DEFAULT 0,
    last_alert_sent_at TEXT, last_warning_sent_at TEXT,
    last_spike_alert_date TEXT, last_digest_sent_date TEXT, last_web_search_budget_alert_date TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE pricing_warnings (
    model         TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    alert_sent_at TEXT
);

-- New vs. the source project — see §5 for why this exists.
CREATE TABLE server_config (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    api_key       TEXT NOT NULL,
    generated_at  TEXT NOT NULL
);
```

No `notes` or `sessions` tables — those were MCP-app-specific and aren't part of a cost tracker.

**Concurrency — new consideration vs. the source project.** The source project's `database.py`
has exactly one writer: its own FastAPI process, talking to itself. SpendGaugeAI's actual pitch is
multiple apps reporting over HTTP, possibly concurrently — a materially different write
pattern. SQLite's default rollback-journal mode single-writer-locks the whole file, which will
surface as real `database is locked` errors under concurrent `POST /usage/log` calls from more
than one reporting app. Fix, staying inside "one SQLite file, no external services": enable
**WAL mode** (`PRAGMA journal_mode=WAL`) once, in `init_db()`. WAL allows concurrent readers
alongside a single writer and is the standard fix for exactly this access pattern — no new
service, no Postgres migration, still a single file on disk. This is not optional polish; skip
it and the product's own core use case (multiple apps reporting to one instance) is the first
thing that breaks under real concurrent load.

## 4. API contract

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | none | Docker liveness check — no data in the response, gating it would break orchestration for no benefit |
| `/usage/log` | POST | **Bearer** (API key) | Ingest one usage record |
| `/usage/credit` | POST | **Bearer** (API key) | Save starting balance / alert thresholds / trigger reset |
| `/usage/budget` | GET | **Bearer** (API key) | Global spend-cap status — `{starting_balance, remaining_usd, exceeded, near_limit}` (§8b, §8c); polled by the SDKs' enforcement/downgrade guards, not the dashboard |
| `/usage` | GET | **Basic** | Dashboard (server-rendered Jinja2 template) |
| `/usage/data` | GET | **Basic** | Aggregated JSON (`?project=` optional filter) |
| `/static/*` | GET | **Basic** | Compiled CSS, vendored Alpine.js, chart JS (static files) |

Superseded from an earlier revision: read routes were originally left open, on the reasoning
that self-hosted + your own network boundary was trust enough. Corrected after explicit
requirement — nothing should be reachable, read or write, without a credential once this is
running. See §5 for the two-scheme mechanism (Bearer for machine calls, Basic for the browser)
and why one shared secret serves both rather than requiring two separate credentials.

**`POST /usage/log`** — the one generalization this project adds over the source project's
design, and the actual integration surface for "any AI app" (§8a). Request body — only `model`
and the token counts are truly required, everything else has a sensible default so the
simplest possible integration (a one-off script with no concept of "session") isn't forced to
invent one just to send its first request:

```json
{
  "project": "my-app",
  "session_id": "abc123",
  "model": "claude-sonnet-4-6",
  "input_tokens": 1200,
  "cache_write_tokens": 0,
  "cache_read_tokens": 300,
  "output_tokens": 450,
  "web_search_requests": 0,
  "tools_used": ["search_docs"],
  "iterations": null
}
```

`project` defaults to `"default"` (already true — §3). `session_id` is now **optional**: if
omitted, the server generates a UUID server-side, so a caller with no natural session concept
can send `{"model": ..., "input_tokens": ..., "output_tokens": ...}` and nothing else. This was
a real gap found in review — as written, `session_id` was `NOT NULL` with no default, meaning
even the minimal integration case was blocked on inventing a session ID first.

**`iterations`** (added after launch, optional, `null`/absent for the overwhelming majority of
calls) — a per-sub-inference breakdown, present when a response went through a server-side
agentic loop that used more than one model. The motivating case: Claude's Advisor tool runs a
second, separately-priced model mid-response (e.g. the executor is `claude-haiku-4-5` but the
advisor consultation ran on `claude-opus-4-8`); the real Anthropic API exposes this via
`message.usage.iterations`, and pricing the *whole* response at the one top-level `model` silently
undercounts every such call. Shape — one entry per sub-inference:

```json
{
  "iterations": [
    {"type": "message", "model": "claude-haiku-4-5", "input_tokens": 900, "output_tokens": 300, "cache_write_tokens": 0, "cache_read_tokens": 0},
    {"type": "advisor_message", "model": "claude-opus-4-8", "input_tokens": 300, "output_tokens": 200, "cache_write_tokens": 0, "cache_read_tokens": 0}
  ]
}
```

`model` is `null` for an entry with no model of its own (the `"compaction"` iteration type) — the
server falls back to the request's top-level `model` for that entry. `type` is informational only,
never read for cost calculation.

Server computes `estimated_cost_usd` from `_PRICING` + the request's raw token counts (see §6),
stores the row, and runs the alert checks (§7). Response: `{"logged": true, "cost_usd": 0.0091}`.

**Cost-calculation branch**: when `iterations` is present and non-empty, cost is the **sum** of
each entry's own per-model cost (§6's `_estimate_cost()`, called once per entry) instead of the
flat single-model calculation. When `iterations` is absent/empty — every call before this field
existed, and every call that doesn't involve a multi-model sub-inference — cost is computed exactly
as before, unchanged. The raw `iterations` payload is also persisted (nullable JSON column on
`usage_logs`, same encoding as `tools_used`) rather than discarded after the cost sum, so a future
investigation (or a per-iteration-model dashboard view) can still see *why* a given row's cost is
what it is. Note this is distinct from `GET /usage/data`'s existing `by_model` breakdown below,
which groups by the one flat top-level `model` column only — it does not yet break out
iteration-level models; that remains a documented future enhancement, not part of this change.

**Ingestion guardrails** (missing from the first pass — staying proportionate to the product:
no external rate-limiter service, no new dependency, just bounded input):
- Pydantic field constraints on the request model: `project`/`session_id` capped at a sane
  length (e.g. 200 chars), `tools_used` capped at a sane array length (e.g. 50 entries) —
  rejects a malformed or runaway client with a normal 422, not a slowly-growing SQLite file.
- A max request body size (Starlette middleware, a few lines, no dependency) — protects against
  a buggy retry loop or oversized payload before it's even parsed.
- A simple **in-memory** per-key sliding-window limit (a dict + timestamps, no Redis) on
  `POST /usage/log` — generous enough not to bother normal traffic, there only to blunt a
  runaway client. This is the one place "in-memory, resets on restart" is actually fine: worst
  case a restart briefly re-opens the window, which is a non-issue for a single self-hosted
  instance.
- `project`, `session_id`, and each `tools_used` entry are free text from the client and later
  rendered on the dashboard. Jinja2's `Environment` autoescapes by default (FastAPI's
  `Jinja2Templates` inherits this), which covers the common case, but this is worth stating
  explicitly rather than leaving it implicit — it's the same bug class (unsanitized
  client-supplied text persisted and later re-displayed) as a real issue the source project
  already had and fixed (`attachment.filename`, GitHub issue #2 there). The length caps above
  double as a blunt mitigation even before considering output-encoding.

**`GET /usage/data`** response shape — this is the contract the dashboard template's Alpine
components fetch against, so it's specified here rather than left to inference from the
mockup's mock data:

```json
{
  "totals": {
    "total_requests": 1842, "total_input": 1240000, "total_cache_write": 95000,
    "total_cache_read": 610000, "total_output": 380000, "total_web_searches": 47,
    "total_cost_usd": 14.7823, "first_request": "2026-07-01T09:12:00", "last_request": "2026-07-17T14:02:00"
  },
  "by_model":   [{"model": "claude-sonnet-4-6", "requests": 1290, "cost_usd": 12.41}],
  "by_day":     [{"day": "2026-07-17", "requests": 151, "cost_usd": 1.51}],
  "by_session": [{"session_id": "abc123", "requests": 86, "cost_usd": 1.982, "first_at": "...", "last_at": "..."}],
  "by_tool":    [{"tool_name": "search_docs", "calls": 312, "cost_usd": 2.114, "avg_cost_usd": 0.0068}],
  "by_project": [{"project": "my-app", "requests": 1510, "cost_usd": 6.942, "last_at": "..."}],
  "credit": {
    "starting_balance": 25.0, "alert_threshold": 5.0, "warning_threshold": 5.0,
    "period_cost_usd": 8.2823, "period_active_days": 8,
    "prev_period_cost_usd": 0, "prev_period_days": 0, "prev_period_end": null
  }
}
```

Any field name change on the Python side is a breaking change to the dashboard's Alpine
components — flag it in review rather than letting the two drift silently. (A generated
OpenAPI/TS-types step is real future work, not required for v1: one page, one endpoint, low
enough risk to catch by eye for now — separate from the JS/TS *client SDK* types in §8a, which
cover the ingestion contract other apps integrate against, a different and higher-stakes
surface than this one internal dashboard fetch.)

## 5. Auth model

**Everything requires a credential — this was tightened from an earlier revision.** The original
design left `/usage` and `/usage/data` open, reasoning that self-hosted + your own network
boundary was trust enough. Corrected on explicit requirement: nothing should be reachable, read
or write, without a credential once the app is actually running. Two schemes, one shared secret:

- **Machine calls** (`POST /usage/log`, `POST /usage/credit`) — `Authorization: Bearer
  <SPENDGAUGEAI_API_KEY>`, unchanged from the original design. This is what every SDK (§8a) and
  raw-HTTP integration sends.
- **Browser access** (`GET /usage`, `GET /usage/data`, `GET /static/*`) — **HTTP Basic Auth**,
  the one credential scheme browsers handle natively (a built-in username/password prompt) with
  zero custom login page, session, or cookie infrastructure to build. Username is a fixed,
  non-secret constant (`spendgaugeai`) — required by the Basic Auth scheme, not itself a secret;
  the password *is* `SPENDGAUGEAI_API_KEY`.
- **One secret serves both roles**, not two separately-managed credentials. Simpler to set up
  and reason about, and matches how this product is actually used — one operator, one instance.

**Honest limitation, not glossed over:** Basic Auth sends the credential base64-encoded, which is
reversible, not encrypted. It stops anyone who can merely *reach* the port without a credential;
it does not stop an attacker already sniffing plaintext traffic on the network path. That's a TLS
problem, and same as the network-boundary assumption below, it's your own reverse proxy's job to
terminate TLS if that threat model matters to your deployment — this app's own dev server was
never going to implement TLS itself, and doing so would be real scope creep against "one process,
no external services."

**The key itself is never allowed to be silently unset.** A missing `SPENDGAUGEAI_API_KEY` must
not mean "accept unauthenticated access" — that's the exact silent-fallback shape that let the
source project's Haiku pricing drift go unnoticed for weeks. But refusing to start over a
missing env var also fights the "docker run and done" pitch for someone who just wants to try
it. Resolution: on first boot, if `SPENDGAUGEAI_API_KEY` isn't set in the environment, generate a
random key, persist it in the `server_config` table (§3), and print it once to the startup
log with a clear "set SPENDGAUGEAI_API_KEY to pin this across restarts" note.

**Precedence — made explicit, not left ambiguous.** A key set via the environment is the *only*
valid key while it's set — the persisted one is not also checked as a fallback. This matters:
without stating it explicitly, a careless implementation could check "matches env-var-key OR
persisted-key," which would leave the original auto-generated key — the one that got printed to
a startup log that might live in Docker logs, CI output, or a terminal scrollback somewhere —
permanently valid even after you deliberately set your own key. That would be a real, silent
leak surface hiding behind text that sounded like it had already been fixed. Either way (env var
or persisted), there is always exactly one real key in effect — never an open-access default,
and never two simultaneously-valid ones.

**Not addressed here, noted for a later pass (§11):** no key-rotation command exists yet — today,
rotating means setting a new `SPENDGAUGEAI_API_KEY` yourself; the old persisted value simply
stops mattering once you do, per the precedence rule above. No file-permission hardening is
specified on the SQLite file itself (e.g. `chmod 600`) — low priority for a single-user local
tool, but cheap enough to be worth doing when this is implemented.

## 6. Cost calculation — server-side, not client-side

The client sends **raw token counts only** — no dollar figures. SpendGaugeAI owns the `_PRICING`
table and computes cost itself. This was a deliberate choice over having each client compute
and report its own cost:

- **One place to fix pricing drift.** The source project's own `INSIGHTS.md` documents a real
  bug (Haiku pricing rates stuck at pre-4.5 values, undercounting ~20% of requests for weeks)
  that was only caught by manually comparing against the real bill. Centralizing pricing means
  that class of bug gets fixed once, and every connected app benefits immediately — not once
  per integration.
- **Trade-off, accepted knowingly:** SpendGaugeAI's own `_PRICING` table can go stale the exact
  same way. Same discipline applies — re-verify against console.anthropic.com whenever Anthropic
  changes rates, or whenever a new model starts appearing in `pricing_warnings`.

## 7. Alerting

Ported from the source project's `_run_alert_checks` (`api.py`) into a dedicated `alerts.py`,
triggered from `POST /usage/log` instead of from a chat endpoint. Same six alert types, same
cooldown discipline (each independently gated so nothing spams Discord):

warning/critical low balance (two-tier, clears on recovery) · spend spike (≥3× trailing 7-day
average and ≥$1 absolute) · web_search daily budget · daily digest (fires on first request of a
new calendar day, not a fixed time — this only runs when something calls it, so piggybacking on
real traffic guarantees it eventually fires) · one-time pricing-warning (via the
`pricing_warnings` table, not a print statement — the whole reason that table exists).
`DISCORD_WEBHOOK_URL` is optional; every check no-ops cleanly if unset.

## 8. Frontend design

**Approved visual reference:** `docs/mockup.html` (static, real data shape, no backend) — the
signed-off design: palette, type, layout, the "instrument panel" concept. Unlike the React plan
this superseded, this file's markup and JS *are* close to what ships — it gets split into
Jinja2 partials and its inline `<script>` gets lightly refactored into Alpine `x-data`
components, not reinterpreted through a different framework's idioms.

**Concept — "instrument panel."** A hero band styled like a meter face (radial gauge for
remaining balance, ledger-serif numerals for the big figures) rather than the flat progress bar
the source dashboard uses, followed by a control-panel grid of stat tiles and ledger-style
tables (hairline rows, tabular numerals, right-aligned figures).

**Verified, not assumed — Alpine.js vs. React side-by-side.** Before locking Alpine.js in, built
two live prototypes of the same three interactions (theme toggle, animated config-panel
disclosure, chart hover tooltip) — one in Alpine.js, one in real React 18 with hooks — using
identical CSS/design tokens so the only variable was the framework. Both were reviewed side by
side and judged visually and behaviorally equivalent. The one real, honest difference found: the
config-panel fade transition is a single Alpine attribute (`x-transition:enter.duration.200ms`)
versus ~15 lines of hand-rolled enter/exit timing in plain React (no transition primitive exists
without a library) — the *end result* was the same, but Alpine reached it with meaningfully less
code. Runtime weight was also measured directly, not estimated: React 18 + ReactDOM 18
(production, minified) alone is ~142KB before any app code, versus Alpine.js at ~44KB for the
same three interactions. This is the evidence behind "no compromise" in §Tech stack — a
comparison, not an assertion.

### Stack — Alpine.js + Jinja2 + Tailwind CLI, zero Node at any stage

The mockup was hand-authored vanilla HTML/CSS/JS. That was briefly upgraded to React for a more
"competitive" UI, then reconsidered once multi-page growth (§1: 2-3 more pages planned) was
factored in — a full SPA framework's real value (cross-page client state, no-reload navigation)
doesn't apply to a set of independent, server-rendered admin pages, and the build-pipeline cost
(npm, `node_modules`, a JS test framework, a second dependency-audit surface) wasn't worth
paying for a benefit this product doesn't need. Alpine.js delivers the same visual and
interaction quality — reactive bindings, transitions, computed state — without any of that:

- **Jinja2** (`FastAPI`'s built-in `Jinja2Templates`) — a `base.html` layout holding the shared
  nav/header/theme-toggle, extended by each page (`usage.html` now, more as pages are added).
  Solves the actual thing multi-page growth needs (consistent chrome across pages) without a
  client router.
- **Alpine.js** — a single vendored file (~15KB, downloaded once and committed, not loaded from
  a CDN — self-hosted means the dashboard must work without outbound internet access), providing
  `x-data`/`x-show`/`x-transition` for the config disclosure panel, theme handling, and the
  project filter. No build step; it's just a `<script src="/static/alpine.min.js">` tag.
- **Tailwind CSS, standalone CLI** — the same utility classes as `mockup.html`, compiled by
  Tailwind's official standalone binary (not the npm package — a downloaded executable, no
  `node_modules`, no `package-lock.json`). One command (`tailwindcss -i input.css -o app.css
  --minify`) run during the Docker build and in local dev; never something an end user runs.
- **Charts** — the mockup's own hand-built SVG (daily-spend line+area, model-split donut),
  vendored as-is into a small JS file. Already built to real mark-spec standards (grid lines,
  hover tooltip, emphasized endpoint) — no charting library needed.
- **Data layer** — `fetch('/usage/data')` on a `setInterval`, pausing on `visibilitychange` and
  refreshing immediately on return, exactly the pattern already proven in the source project.
  Ported near-verbatim, not rewritten into a different idiom. No extra auth-handling code
  needed for this fetch: `/usage/data` requires Basic Auth (§5) same as the page that loads it,
  and browsers automatically resend cached Basic Auth credentials for same-origin requests
  within the same protection realm — the polling `fetch()` inherits the credential the browser
  already has from loading `/usage` itself.
- **Packaging:** `Dockerfile` is **single-stage** (`python:3.12-slim`) — the Tailwind binary is
  downloaded and run in one `RUN` step, no separate Node stage needed at all. The pip wheel
  includes the compiled CSS + vendored JS as package data, same as the React plan would have,
  just without ever needing Node to produce them.
- **Honest trade-off, updated:** the real cost given up is instant client-side navigation
  between pages — each page load is a real HTTP request, like the source project's own `/`,
  `/usage`, `/logs`. For an admin dashboard checked periodically, not a consumer app navigated
  constantly, that's the right trade for a self-hosted, zero-Node, easier-to-audit stack.

**What's reused vs. rebuilt from the source dashboard:** more than the React plan reused — the
mockup's HTML structure, CSS, and chart-rendering JS carry over close to directly, split into
Jinja2 partials and lightly wired to Alpine's reactivity model instead of raw
`document.getElementById` calls. The polling/visibility-aware data-fetching behavior is ported
unchanged.

**Color** (validated with the `dataviz` skill's palette checker — categorical separation,
colorblind-safety, and contrast all pass in both themes):

| Token | Light | Dark | Use |
|---|---|---|---|
| `--primary` (indigo) | `#6355D6` | `#7C6EF0` | Brand accent, primary series (Sonnet), focus ring |
| `--secondary` (amber) | `#B5791A` | `#C77F1F` | Secondary series (Haiku), complementary accent |
| `--good` | `#2F7A50` | `#3E8F63` | Positive status |
| `--warning` | `#C2811A` | `#C2811A` | Warning status — always paired with icon + label |
| `--critical` | `#C13B3B` | `#DC3535` | Critical status |
| `--bg` | `#F5F4FA` | `#171521` | Page background (cool violet-tinted, not warm cream or neon-on-black) |
| `--surface` | `#FBFAFE` | `#211D33` | Card background |

Full token set (surfaces, borders, text, soft-tint variants) is in `mockup.html`'s `:root`
block — that file is the canonical copy, this table is a summary for quick reference.

**Type:**
- Display (`--font-display`): `ui-serif, "Iowan Old Style", "Palatino Linotype", "Book Antiqua",
  Georgia, serif` — ledger-style serif numerals for the hero balance figure and donut center
  values. A system stack was chosen deliberately over an embedded webfont: Artifacts block font
  CDNs, and a data-URI-embedded face risked a silent fallback for no real visual gain at this
  stage — worth revisiting with a real embedded face (e.g. Fraunces) once this is a shipped
  product, not a mockup.
- Body (`--font-body`): native system-ui stack — deliberately not Inter/Space Grotesk (flagged
  in the `artifact-design` skill as a defaulted "safe" choice), appropriate for a technical tool.
- Data (`--font-mono`): `ui-monospace` stack + `tabular-nums` wherever digits line up in columns.

**Iconography** (added in a later mockup pass — Vijay wanted the dashboard to read as a
polished product, not text-and-numbers-only): **Heroicons** (MIT, Tailwind Labs' own icon set —
a natural pairing since Tailwind is already the CSS choice), vendored as plain inline `<svg>`
markup, not an icon font or an npm/CDN dependency — same "vendor exactly what's needed" pattern
already used for Alpine.js itself. Every icon uses `stroke="currentColor"`, so it inherits
color from its CSS context automatically and needs zero extra theme wiring for light/dark.
Applied to: all stat tiles, all section headers, and the two hero readouts most worth a quick
visual anchor (Burn rate, Est. runway) — deliberately *not* applied to every readout or to the
tool-table rows, which already use categorical color-coded swatches (§ dataviz color table) and
would be visually noisy with icons added on top. A small custom logo mark was added alongside
this — not a Heroicon, a hand-drawn glyph reusing the *exact same construction* as the hero
gauge (track arc + colored fill arc, same 67% fill level) at a much smaller scale, so the brand
mark and the product read as one visual system rather than a wordmark bolted onto unrelated UI.

**Balance configuration** (the piece missed in the first mockup pass): a collapsible
`<details>` panel behind a "Configure" toggle in the hero, holding the same controls as the
source dashboard's always-visible form — starting balance, alert threshold, the reset-spend-
tracking checkbox (with its confirm-before-reset framing preserved), and the previous-period
note. Collapsed by default so the polished gauge isn't cluttered by raw inputs on every load,
since these are rarely-touched settings.

**Copy in this panel is policy language, not a generic settings form** — the concrete
application of §1's "control center, not a settings page" principle. "Starting balance" reads
as a budget being set, not a number being entered; the reset checkbox's existing
confirm-before-reset framing already does this correctly (it explains a consequence, not just a
toggle) — that same standard extends to every label in the panel, not just the one that already
had it. Small wording difference, same principle as the hero gauge itself: the whole page should
feel like operating a financial instrument, not filling out a form that happens to be about
money.

## 8a. Client integration — language-independent by design

**The product is the HTTP API, not any SDK.** `POST /usage/log` (§4) is plain JSON over HTTP
with a Bearer token — every SDK below is a thin, optional convenience wrapper around that same
contract. An AI app in a language with no official SDK integrates exactly as fully as one that
has one; it just writes the HTTP call by hand instead of importing a package. This is a
deliberate correction from the first version of this section, which implicitly treated the
Python client as the primary integration path and everything else as an afterthought — that's
backwards for a product whose stated audience is "anyone building a Claude API app," not
"anyone building one in Python."

**Two integration patterns, both offered by every official SDK:**
- **`wrap(client, ...)`** — wraps an app's existing Anthropic SDK client object once, at
  construction time. Every call the app makes through the wrapped client reports itself
  automatically; nothing else in the app's code changes. This is the primary, recommended path
  — "plug in once, forget about it" — matching how comparable tools (Langfuse, Sentry SDKs)
  handle this, and it removes the real failure mode of the alternative (a manual call forgotten
  at some call site as an app grows).
- **`.log(...)`** — manual, explicit reporting for apps that want per-call control (custom
  `project`/`session_id` tagging, not using the Anthropic SDK's client object directly, or
  reporting from a context `wrap()` can't see into). Stays available in every SDK; not the
  default-recommended path, but not deprecated either.

**`wrap()`'s six real edges — the first four found in design review, the last two only found by
wiring a real app to a real production server, not assumed from a first design pass:**

1. **Patch at the shared choke point — which turned out to be *two* objects, not one.** `wrap()`
   patches `messages.create` and `messages.stream` directly on the client object — on *both*
   `Anthropic` and `AsyncAnthropic` (four patch points total, not one; the source project's
   dogfooding target uses `AsyncAnthropic` exclusively, so sync-only support would silently cover
   nothing there). The original version of this section claimed patching `client.messages` alone
   was enough because `client.beta.messages.tool_runner` "calls the same underlying method
   internally" — **that claim was wrong, and shipped for a week before a live integration
   surfaced it.** `client.messages` and `client.beta.messages` are genuinely separate resource
   objects in the real SDK (`client.messages is client.beta.messages` → `False`, confirmed live in
   both the Python and TS SDKs), and `tool_runner`'s internal agentic loop calls exclusively
   through `self._client.beta.messages...` — never the stable resource. `wrap()` now patches
   **both** `anthropic_client.messages` and, if present, `anthropic_client.beta.messages` (same
   patch function, applied twice) — real `Anthropic`/`AsyncAnthropic` clients always have `.beta`.
   Test doubles with no `.beta` attribute keep working unchanged — only patched when present.
2. **Streaming reports from the final accumulated message, in a `finally` block.** Non-streaming
   `.create()` returns a `Message` with `.usage` directly — report immediately. Streaming
   (`stream=True`, or `.stream()`) only has usage once the stream completes, so `wrap()` returns
   a pass-through wrapper that forwards every event to the caller unchanged, and reports from the
   final accumulated message once the stream is exhausted — in a `finally`, so it fires whether
   the caller consumes the whole stream, breaks early, or the stream errors. This mirrors the
   "report at every exit point" discipline `_lf_finish()` already uses in the source project.
   Left unfixed, this would have been a *silent* gap — no error, just missing dashboard data for
   the majority pattern in real chat apps.
3. **`session_id` flows through a `contextvars.ContextVar`, not a mutable client attribute.** A
   shared long-lived client handling concurrent requests (again, exactly the source project's own
   pattern) can't safely store per-request session state as an instance attribute — concurrent
   requests would clobber each other's value. `contextvars.ContextVar` is asyncio-task-local,
   which is the actual requirement here:
   ```python
   with client.spendgauge_session(session_id=session_id, project="my-app"):
       response = await client.messages.create(...)
   ```
   No context set → each call falls back to a fresh server-generated UUID (§4's default),
   correctly modeling "no session concept" rather than silently colliding requests together.
4. **`tools_used` must collect two different content-block types, and `web_search_requests` comes
   from a third place entirely.** Client-side tools are `tool_use`-type content blocks;
   *server-side* tools (advisor, web_search, web_fetch, code_execution, bash_code_execution,
   text_editor_code_execution, tool_search_tool_regex, tool_search_tool_bm25) arrive as
   `server_tool_use`-type blocks instead — a real bug found post-launch (both SDKs originally
   only checked `block.type == "tool_use"`, so every server-side tool except web_search was
   invisible in `tools_used`; only web_search's *count*, tracked separately below, was ever
   captured). `web_search_requests` itself is a separate structured field on `.usage`, not a
   content block at all:
   ```python
   tools_used = [b.name for b in response.content if b.type in ("tool_use", "server_tool_use")]
   web_search_requests = getattr(getattr(response.usage, "server_tool_use", None), "web_search_requests", 0) or 0
   ```
   Same logic runs against the final message whether it came from a plain call or an exhausted
   stream — only how the final message is obtained differs between the two paths.
4a. **`iterations` — per-sub-inference model breakdown, for accurate multi-model cost.** Also
   found post-launch: when a response involves a server-side sub-inference that runs a *different*
   model than the main response (the Advisor tool is the motivating case — it consults a second,
   separately-priced model mid-response), the real breakdown lives in `message.usage.iterations`,
   a list where entries of type `"message"`/`"advisor_message"`/`"fallback_message"` each carry
   their own `model` field (`"compaction"` entries carry none). Neither SDK read this field
   originally, so the *entire* response — including any pricier sub-inference tokens — was priced
   at the one top-level model, silently undercounting cost. Extraction (mirrors `_extract_usage()`'s
   existing `cache_creation_input_tokens` → `cache_write_tokens` field-naming convention):
   ```python
   def _extract_iterations(message):
       iterations = getattr(getattr(message, "usage", None), "iterations", None)
       if not iterations:
           return None
       return [{
           "type": getattr(it, "type", None), "model": getattr(it, "model", None),
           "input_tokens": getattr(it, "input_tokens", 0) or 0,
           "cache_write_tokens": getattr(it, "cache_creation_input_tokens", 0) or 0,
           "cache_read_tokens": getattr(it, "cache_read_input_tokens", 0) or 0,
           "output_tokens": getattr(it, "output_tokens", 0) or 0,
       } for it in iterations]
   ```
   Absent/`None` for the overwhelming majority of calls (no server-side sub-inference involved),
   in which case the server prices the call exactly as it always has — see §4's cost-calculation
   branch for the server-side half of this fix.
5. **`tool_runner`'s non-streaming variant calls a *third* method, `.parse()` — not `.create()`
   at all.** Fixing edge 1 (patching `.beta.messages`) still wasn't enough: `BetaAsyncToolRunner`
   /`BetaToolRunner` (what a bare `tool_runner(...)` call returns when `stream` isn't passed —
   the common case) calls `self._client.beta.messages.parse(...)` internally, confirmed by
   reading the SDK's own source. `.create()` was never invoked at all for this path. `wrap()` now
   patches `.parse()` too, if present, alongside `.create()`/`.stream()` — reported the same
   simple way as non-streaming `.create()`, since `.parse()` never returns a raw stream (its own
   `stream` kwarg only affects the request body, not the transport call, which the SDK always
   makes non-streaming internally).
6. **Sync-vs-async detection via `inspect.iscoroutinefunction()` silently lies about the real
   SDK.** `wrap()` branches its patch logic on `inspect.iscoroutinefunction(original_create)` to
   decide whether to `await` — this reports **`False`** for a real `AsyncAnthropic` client's
   `.create()`/`.parse()`, confirmed live, because the SDK wraps the true `async def` in an
   internal sync dispatcher (still `functools.wraps`-decorated, so `await client.create(...)`
   works completely normally for real callers — the dispatcher just returns the inner coroutine
   object for the caller to await). Every unit test in this repo used a fake client built from a
   plain `async def` function directly, which *correctly* reports `True` — so this stayed
   invisible until `wrap()` was tested against a real SDK client instead of a test double, for the
   first time, wiring up a real app. The consequence was silent and specific: `wrap()` took its
   *sync* patch branch even for a real `AsyncAnthropic` client, so `response = original_create(...)`
   (no `await`) captured the **unawaited coroutine object itself**, not the real message — every
   report fired with all-zero tokens/cost, while the real Anthropic call kept working perfectly
   (the outer caller still awaits whatever `patched_create`'s sync body returns). Fixed with
   `inspect.iscoroutinefunction(inspect.unwrap(original_create))` — `inspect.unwrap()` follows the
   dispatcher's `__wrapped__` chain to the real function; test doubles with no `__wrapped__`
   unwrap to themselves, so existing tests needed no changes, only a new one to lock this in.

**Both patterns, in every official SDK, share a hard guarantee:** a short request timeout, every
exception caught internally, never raised into the caller's app. Reporting usage must never be
able to break the actual AI app doing real work — this was previously just a convention the
dogfooding integration happened to follow by hand; it's now a documented requirement of the SDK
layer itself, so every integration gets it automatically.

**Official SDKs shipped in v1 (both, per review — the audience for this product spans
ecosystems, and treating one as v1 and the other as a fast-follow would contradict the
language-independence goal):**
- **Python — `spendgaugeai` package** (same package as the server, importable as
  `from spendgaugeai import SpendGaugeAIClient, wrap`). No extra install for anyone already
  running the server; a standalone thin install for a separate app that only needs the client.
- **JS/TS — `spendgaugeai-client` npm package** (name verified available on the npm registry).
  Client-only — a small `fetch`-based wrapper (`wrap()`, `.log()`), zero runtime dependencies.
  **This does not reintroduce Node into the server.** The server (§9) remains Node-free at
  every stage; this is a separate, independently-versioned package that a *JS/TS app* installs
  into *its own* project to talk to a SpendGaugeAI server over HTTP — the two are unrelated
  except for sharing the same wire contract. Lives in `clients/js/` (its own `package.json`,
  TypeScript source, a `tsup`/`tsc` build step) — Node/npm usage is scoped to that one
  subfolder's own build, not the product's runtime story.

**Any other language** (Go, Ruby, Rust, whatever an AI app happens to be written in): the raw
`POST /usage/log` contract (§4) plus copy-paste `curl` and generic-HTTP examples in the README
are sufficient — no SDK required, by design. An official SDK for a third language is real,
welcome future work (community-contributable, given the protocol is simple and stable), not
something this project needs to pre-build speculatively.

## 8b. Hard spend-cap enforcement — blocking a call before it bills you

Alerting (§7) is notification, not enforcement — it tells you spend is high after the fact but
never stops a call. This section closes that gap: an **opt-in, client-SDK-side pre-call gate**
that can refuse to make an Anthropic call at all once the configured budget is exhausted. Not a
proxy — SpendGaugeAI never sits in the request path to Anthropic (consistent with "own your
data, zero traffic redirection"), it can only let the SDK decide not to place the call.

**Scope: global cap only, fail-open.** Reuses the existing single-row `credit_config
.starting_balance` as the ceiling — no per-project caps yet (a clean follow-on once this
mechanism is proven). If SpendGaugeAI is unreachable, the guarded call proceeds — a
monitoring-sidecar outage must never take down the real app, the same "reporting must never
break the app" guarantee §8a establishes everywhere else, extended here to "and neither can
enforcement, when it can't get a definitive answer."

**The one deliberately-breaking exception in either SDK.** Every other `wrap()`/`.log()` path is
fail-safe by design (§8a) — this is the sole part of the SDK that's *allowed* to raise into the
caller's app, and only when the cap is confirmed (not merely suspected) exceeded. It must be
unmistakably opt-in (`enforce: true`), off by default, and raise a typed exception the caller can
catch: `SpendGaugeAIBudgetExceededError` (Python), `BudgetExceededError` (TS).

**No new DB schema, no dashboard changes.** `database.budget_status(project=None) -> dict`
returns `{starting_balance, remaining_usd, exceeded, near_limit}` — `exceeded` is always `False`
when `starting_balance <= 0` (unconfigured means no enforcement, the same convention §7's
low-credit alert already used); `near_limit` is documented in §8c, which reuses this same field.
`alerts.py`'s low-credit/digest checks now call this helper instead of duplicating the
remaining-balance math a second time. `GET /usage/budget` (§4) is a thin `run_in_threadpool`
wrapper around it, Bearer-only (this is a machine-facing check called by the SDK's
enforcement/downgrade guards, not the browser dashboard, so it doesn't need
`require_bearer_or_basic` the way `/usage/credit` does) with an optional `?project=` filter — the
cap itself stays global, but you can check one project's own spend against it.

**Client-side opt-in only.** `wrap(client, ..., enforce=True)` (Python) /
`wrap(client, spendgauge, { enforce: true })` (TS, `wrap()`'s new optional 3rd param) is what
turns this on. `check_budget()`/`acheck_budget()` (Python) and `checkBudget()` (TS) are also
public methods on the client, not just `wrap()`-internals, so a `.log()`-only integrator can gate
its own call site without adopting `wrap()`. All three: return `True`/`False` on a real answer,
`None`/`null` on any failure (timeout, network error, non-2xx) — never raise/throw. `wrap()`'s
guard only blocks on a definitive `True`; `None` and `False` both mean "proceed." Both are now
thin wrappers over `get_budget_status()`/`getBudgetStatus()` (§8c) — the full status dict that
also backs the downgrade decision, so one cached HTTP response serves both concerns.

**Short TTL cache (default 5s, param named `cache_seconds`/`cacheSeconds`) on the check result**,
keyed per-project, to avoid doubling request volume against the shared rate limiter (§4) for an
app making many Claude calls per minute — a few seconds of staleness on a budget *ceiling* is an
acceptable trade against a network hop on every guarded call. (Named `enforce_cache_seconds`/
`enforceCacheSeconds` originally; renamed once §8c gave the same cache a second purpose — safe to
rename pre-publish, nothing external depended on the old name.)

**Guarded at all four of §8a's choke points in Python** (sync/async `create`, sync/async
`stream`, including the raw `stream=True` path) — but the two languages diverge on *where
exactly* the check runs for the `.stream()` path, for a real API-shape reason:

- Python's `messages.stream(...)` returns a context manager that doesn't touch the network until
  `__enter__` — so the guard lives in `_SyncStreamWrapper`/`_AsyncStreamWrapper`'s
  `__enter__`/`__aenter__`, genuinely blocking before the real call, with a full async
  `check_budget()`/`acheck_budget()` round-trip.
- The JS/TS Anthropic SDK's `messages.stream(...)` returns its `MessageStream` **synchronously**,
  by design, so callers can chain `.on(...)` before the network call resolves — wrapping it in a
  Promise to await a real check would break that contract for every caller, not just guarded
  ones. So the TS guard only ever consults `checkBudget()`'s cache **synchronously**
  (`peekCachedBudgetStatus()`) and, on a cold cache, fails open while kicking off a background
  refresh. `messages.create(...)` in TS has no such constraint (it's already `Promise`-returning)
  and gets the full awaited check like Python. Net effect: in a sustained `.stream()`-only TS
  workload the cap is still enforced, just not necessarily on the very first call right after
  it's crossed — a real, documented, narrower guarantee than Python's, not a silent gap.

## 8c. Budget-aware model downgrade — graceful degradation before the hard block

§8b's `enforce` is a binary cliff: the app works normally right up until the cap, then every call
fails. The more differentiated, more honest-to-"control layer, not just a fence" move is a softer
middle tier: once spend is *close* to the cap but not yet over it, automatically route calls to a
cheaper model instead of refusing them — the app keeps working, just cheaper, right up until the
real limit, where `enforce` (if also on) still takes over.

**Reuses `credit_config.warning_threshold` as the trigger — no new schema, no new dashboard
field.** The same dollar amount that already triggers the Discord low-credit warning (§7) now
means the same thing everywhere: a Discord ping *and*, for `wrap(downgrade_model=...)` callers,
the SDK starting to be frugal. `database.budget_status()` (§8b) computes one more field:

```python
near_limit = starting_balance > 0 and not exceeded and remaining_usd <= warning_threshold
```

`near_limit` is `False` whenever `exceeded` is `True` — mutually exclusive by construction, so
`wrap()`'s policy check can always evaluate `exceeded` first with no separate priority logic
needed elsewhere.

**New `wrap()` param, both languages:** `downgrade_model: str | None = None` (Python) /
`downgradeModel?: string` (TS, `WrapOptions`) — independent of `enforce`; either can be on without
the other.

**Where the swap happens.** `messages.create` (both languages, both sync/async, including the raw
`stream: true` path) fetches the full status once via `get_budget_status()`/`getBudgetStatus()`
before the real call — the same choke point `enforce` already uses — and swaps `model` in place if
`near_limit`. In Python, `kwargs` is already a fresh dict scoped to that one call, so mutating it
directly is safe; in TS, `args[0]` is the *caller's own* object reference, so the swap
shallow-copies (`{ ...params, model: downgradeModel }`) rather than mutating it in place — silently
changing a caller's own object out from under them would be a surprising side effect their code
didn't ask for.

**`messages.stream` needs a synchronous peek in *both* languages — a real architectural
constraint, not a style choice.** Once `original_stream(model=X, ...)` is called, `X` is baked
into the returned stream/manager object with no supported way to change it after the fact, so the
swap decision must happen *before* that call, inside `patched_stream` itself — which must stay a
plain synchronous function in both languages (constructing a stream/manager object is synchronous
by SDK design in both Python and TS, the same reason §8b's TS `enforce` guard already needed a
peek). This is where Python and TS diverge from each other for `enforce` (§8b) but *agree* for
`downgrade_model`:

- TS reuses (renamed) `peekCachedBudgetStatus()` for both the `enforce` peek and the new downgrade
  peek. Cold/stale cache → no swap (fail open), plus a background refresh kicked off exactly like
  `enforce`'s cold-cache path already does.
- Python gets a **new** `peek_cached_budget_status()` — Python's `enforce` guard for `.stream()`
  never needed a peek (it lives inside `_SyncStreamWrapper.__enter__`/
  `_AsyncStreamWrapper.__aenter__`, which run *after* the manager already exists, so a real
  blocking/awaited check there is fine — it just delays entering the `with`/`async with` block, not
  the manager's already-fixed `model`). Downgrade is different: it must know the answer *before*
  the manager is constructed. Deliberately **no** background-refresh-on-cold-cache for Python —
  `asyncio.ensure_future` fire-and-forget tasks have real lifecycle sharp edges ("Task was
  destroyed but it is pending" if unreferenced) that JS's fire-and-forget promises don't share. A
  cold cache here just means no downgrade for that one call — self-healing, since `.create()` calls
  already populate the same shared cache, and the default TTL is only 5s. `enforce`'s existing
  Python `.stream()` behavior is completely untouched by this — still a real, fresh,
  awaited/blocking check, no regression.

**Cost attribution needs zero extra work.** Both languages already report the model from the
*response* (`message.model` / `getattr(message, "model", ...)`), not from what was originally
requested — Anthropic's response always echoes back whichever model actually served the call. A
downgraded call is therefore logged and cost-attributed correctly automatically.

**Scope boundary:** only `model` gets swapped — not `max_tokens`, temperature, or anything else.
No model-tier mapping table (`sonnet → haiku` auto-inference); the caller names their exact
fallback model explicitly, the same reasoning `_PRICING` already needs hand-maintenance for — a
second thing that goes stale the moment Anthropic ships a new model isn't worth building.

## 9. Packaging

- `pyproject.toml`, `hatchling` backend. Deps: `fastapi`, `uvicorn[standard]`, `httpx`,
  `python-dotenv`, `jinja2`. `[project.scripts] spendgaugeai = "spendgaugeai.cli:main"`. Package data
  includes the compiled `static/app.css` and vendored `alpine.min.js`/chart JS — small,
  version-controllable files, not a `dist/` folder from an npm build. `app.css` is **committed**,
  not gitignored: `pip install git+https://github.com/...` builds straight from a raw clone with
  no chance to run a pre-packaging build step, so the compiled CSS has to already be in the repo
  for that install path to work at all (verified against a real clone — it 404s on the force-
  include otherwise). Regenerate with `./scripts/build-css.sh` and commit the diff whenever
  `static/src/input.css` changes, same maintenance model as the already-committed `alpine.min.js`.
- `Dockerfile`: **single-stage** (`python:3.12-slim`) — one `RUN` step recompiles `static/app.css`
  via the Tailwind standalone binary (idempotent over the committed copy — keeps the image build
  self-verifying rather than trusting a stale commit), then the Python package installs normally.
  No Node/npm anywhere in the image or the build process. `docker-compose.yml` bind-mounts
  `./data:/data` for the SQLite file — deliberately a bind mount, not a Docker-managed named
  volume: a named volume lives in Docker's own hidden storage, so switching between `pip install`
  and `docker compose up` would silently mean two separate, un-synced databases (found and fixed
  after exactly that confusion — verified the bind mount makes both paths read/write the identical
  file). `restart: unless-stopped` on the service, so it survives crashes and daemon restarts —
  note this does **not** cover a full Docker Desktop restart/reinstall on Windows, which tears
  down and rebuilds the WSL2 VM rather than triggering the container runtime's own restart-policy
  path; `docker compose up -d` after one of those is a manual step, not automatic.
- `.env.example`: `SPENDGAUGEAI_API_KEY`, `DISCORD_WEBHOOK_URL` (optional), `PORT`.
- PyPI publish is a manual step Vijay runs when ready — not automated here. No `node_modules`
  state to get stale or out of sync either way. Exact commands (build, `twine upload`, `npm
  publish`, and the fresh-install verification step for each): `docs/PUBLISHING.md`.
- **`clients/js/`** — the JS/TS client SDK (§8a), a self-contained package with its own
  `package.json`, TypeScript source, and build step (`tsup` or `tsc`). This is the one place
  Node/npm legitimately exists in the repo — scoped to this subfolder's own dev/build process,
  publishing `spendgaugeai-client` to npm independently of the server's release cycle. Publishing
  it is a manual step Vijay runs when ready, same posture as the PyPI publish above.

## 10. Dogfooding

Once v1 is verified working end-to-end, the MCP Learning Project becomes the first real client:
a best-effort `SpendGaugeAIClient(...).log(...)` call added alongside its existing local
`usage_log()` call, gated by optional `SPENDGAUGEAI_URL`/`SPENDGAUGEAI_API_KEY` env vars (no-op if
unset), wrapped so a SpendGaugeAI failure can never break a real chat response. The MCP project's
own `/usage` dashboard keeps working completely unchanged — this only adds a second, real
reporting path.

## 11. Open questions for a later revision

Resolved during design review (kept here as a record, not re-open): `/usage/credit` now
requires the API key (§4, §5) · SQLite concurrency addressed via WAL mode (§3) · ingestion
guardrails — body size, field-length caps, in-memory rate limit (§4) · missing-API-key fail-safe
via generate-and-persist-once instead of silent open-write (§5) · `/usage/data` response shape
now specified (§4) · static-asset serving routes decided (§4) · `wrap()`'s four edges — async
client support, streaming, session-scoping via `contextvars`, and correct `tool_use`/
`server_tool_use` extraction — all specified in §8a rather than left as an implicit gap in the
flagship integration path · read routes now require Basic Auth, closing the "reads stay open"
gap from the original design (§4, §5) · env-var-vs-persisted-key precedence made unambiguous —
the persisted key is dead the moment an env var is set, not a silent fallback (§5) · SQLite file
permissions now hardened to owner-only `0600` on the db file and its `-wal`/`-shm` siblings, via
`_harden_db_permissions()` called at the end of `init_db()` — best-effort (Windows can't express
real POSIX bits), verified `-rw-------` inside a Linux container.

Still genuinely open:
- Key rotation has no dedicated command — today it's "set a new `SPENDGAUGEAI_API_KEY`
  yourself"; real UX (a `/rotate` endpoint or CLI command) is future work, not v1.
- Per-project API keys (multi-operator safety) — v1-deferred, not designed yet. The single
  shared key is deliberately proportionate to "one operator, one instance"; revisit if/when
  multiple people sharing one instance becomes a real use case.
- Embedded display webfont for a shipped (non-mockup) release — currently a system serif stack.
- The in-memory rate limiter (§4) resets on restart and doesn't survive multi-process deployment
  (e.g. behind a load balancer) — a non-issue for the single-process v1 target, worth revisiting
  only if SpendGaugeAI ever runs as more than one process.
