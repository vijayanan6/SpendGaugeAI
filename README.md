# SpendGaugeAI

**AI FinOps for self-hosted Claude API developers.** Not a usage-log viewer — a financial
control center for your Claude spend: set a budget, watch burn rate and runway in real time,
and get alerted before you run out. No account, no SDK wrapping — your app POSTs token usage
after each call, SpendGaugeAI computes cost, enforces the budget policy you set, and optionally
pings Discord when something needs attention.

> **Status: v1 implementation working end-to-end.** Backend (auth, ingestion, credit tracking,
> Discord alerts), both official client SDKs (Python and JS/TS), a first-pass Jinja2/Alpine.js
> dashboard, Docker packaging, and tests are all in place and verified — see
> [`docs/DESIGN.md`](docs/DESIGN.md) for the spec and [`docs/mockup.html`](docs/mockup.html) for
> the approved visual reference. The dashboard's visual pass is still expected to change.

## Why

Langfuse, Helicone, and Portkey already do hosted multi-provider LLM observability well, for
free at small scale — but they're built around *logs and traces*, not spend *control*. Their
cost views are a side effect of tracing, not the product's center of gravity. SpendGaugeAI
inverts that: cost, budget, and burn rate *are* the product, and it's for the narrower case
where you also don't want to hand a hosted service your prompts, costs, or an account signup
just to manage your own budget. `docker run` (or `pip install`), point your app at it, done.
Your data — and your spend policy — stay on your machine.

The accounting engine underneath isn't new — it's extracted from a cost control panel that's
already caught real billing bugs against real Anthropic invoices in production use.

## How it will work

```
your app  ──POST /usage/log──▶  SpendGaugeAI  ──▶  dashboard at /usage
                                     │
                                     └──▶  Discord alerts (optional)
```

**The HTTP API is the actual product interface** — `POST /usage/log`, JSON, a Bearer token.
Every SDK below is an optional convenience wrapper around that same contract, not a requirement.
SpendGaugeAI is built for any Claude API app, not just Python ones.

**Python** — wrap your existing Anthropic client once, everything after that reports itself:

```python
from spendgaugeai import wrap
import anthropic

client = wrap(anthropic.Anthropic(), base_url="http://localhost:8000", api_key="...", project="my-app")
response = client.messages.create(...)   # reports automatically, no other code changes
```

**TypeScript/JS** — same pattern:

```ts
import { wrap } from "spendgaugeai-client";
import Anthropic from "@anthropic-ai/sdk";

const client = wrap(new Anthropic(), { baseUrl: "http://localhost:8000", apiKey: "...", project: "my-app" });
const response = await client.messages.create({ ... });   // reports automatically
```

**Any other language** — no SDK needed, just the raw contract:

```bash
curl -X POST http://localhost:8000/usage/log \
  -H "Authorization: Bearer $SPENDGAUGEAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","input_tokens":1200,"output_tokens":450}'
```

`model` and the token counts are the only required fields — `project`/`session_id` default
sensibly (§4 of `DESIGN.md`) so the simplest possible integration doesn't need to invent a
session concept just to send its first request. Both official SDKs also offer a manual `.log()`
method for apps that want more control than auto-wrapping gives them (see
[`docs/DESIGN.md` §8a](docs/DESIGN.md) for the full integration design). SpendGaugeAI computes
the cost server-side (see [§6](docs/DESIGN.md#6-cost-calculation--server-side-not-client-side)
for why) and it shows up on the dashboard immediately.

## Quickstart (once built)

```bash
pip install spendgaugeai
spendgaugeai serve
```

First run prints a generated credential once:
```
[spendgaugeai] Generated API key: sk_live_xxxxxxxxxxxx
[spendgaugeai] Set SPENDGAUGEAI_API_KEY to pin this across restarts.
```
Open `http://localhost:8000/usage` — your browser will prompt for a username/password (native
Basic Auth, no login page). Username is `spendgaugeai`, password is the key printed above. Use
that same key as the Bearer token when configuring `wrap()`/`.log()` in your app. Nothing —
dashboard included — is reachable without it (see [`docs/DESIGN.md` §5](docs/DESIGN.md) for why
even a self-hosted, localhost-only tool needs this).

or with Docker:

```bash
docker compose up
```

No Node.js required to **run** SpendGaugeAI — for either path, or even for building the server
from source. The dashboard is server-rendered (Jinja2 + Alpine.js), styled with Tailwind's
standalone CLI (a downloaded binary, not an npm package); there's no JavaScript build pipeline
for the server at all. Node is only relevant if you're **integrating a JS/TS app** and choose
the `spendgaugeai-client` npm package over a raw `fetch()` call — that's a separate, optional
package for API consumers, unrelated to running the server itself.

## Developing the dashboard

The dashboard lives in `src/spendgaugeai/templates/` (Jinja2) and `src/spendgaugeai/static/`
(Tailwind CSS source, vendored Alpine.js, chart JS). Compiling the CSS is one command:

```bash
./scripts/build-css.sh   # downloads the Tailwind standalone binary if needed,
                          # writes src/spendgaugeai/static/app.css
```

`docs/mockup.html` is both the approved visual reference *and* close to the actual
implementation source — its HTML/CSS/JS get split into templates and lightly wired to Alpine's
reactivity rather than rebuilt from scratch in a different framework.

## What's in v1

- Budget policy: set a starting balance and alert threshold, track burn rate, forecast
  "estimated runway" — the control layer, not just a number
- Token/cost accounting per model, per project, per session, per tool — the ledger underneath
  the policy, not the headline feature
- Discord alerts: low balance (two-tier), spend spikes, daily digest, stale-pricing warnings —
  policy enforcement, not just notifications
- A single shared API key gates the two mutating endpoints (`POST /usage/log`, `POST /usage/credit`)
- Official Python (`spendgaugeai`) and TypeScript/JS (`spendgaugeai-client`) SDKs, both with an
  auto-reporting `wrap()` and a manual `.log()` — any other language integrates via the
  documented raw HTTP contract, no SDK required

**Not in v1** (see `docs/DESIGN.md` §1 for the full list and why): multi-tenant auth / per-project
keys, a hosted PyPI release, and the log-viewer/conversations features that are specific to the
project this was extracted from rather than general to cost tracking.

## Design

Full design doc: [`docs/DESIGN.md`](docs/DESIGN.md). Approved visual reference:
[`docs/mockup.html`](docs/mockup.html).

## License

[MIT](LICENSE)
