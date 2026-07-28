# spendgaugeai-client

Official JS/TS client for [SpendGaugeAI](https://github.com/vijayanan6/SpendGaugeAI) — AI FinOps
for self-hosted developers. Point your existing Anthropic client at a running SpendGaugeAI
instance and it reports every call's usage automatically.

Not yet on npm — build it from a clone until the real publish happens:

```bash
git clone https://github.com/vijayanan6/SpendGaugeAI.git
cd SpendGaugeAI/clients/js && npm install && npm run build
npm pack   # produces spendgaugeai-client-<version>.tgz
cd /path/to/your-app && npm install /path/to/SpendGaugeAI/clients/js/spendgaugeai-client-*.tgz
```

(Once published: `npm install spendgaugeai-client`.)

## `wrap()` — recommended, auto-reporting

Wraps your existing `@anthropic-ai/sdk` client in place. `messages.create` and `messages.stream`
both report automatically after that — no other code changes, no per-call-site edits:

```ts
import { wrap, SpendGaugeAIClient } from "spendgaugeai-client";
import Anthropic from "@anthropic-ai/sdk";

const spendgauge = new SpendGaugeAIClient({
  baseUrl: "http://localhost:8000",
  apiKey: process.env.SPENDGAUGEAI_API_KEY!,
  project: "my-app",
});

const client = wrap(new Anthropic(), spendgauge);
const response = await client.messages.create({ ... }); // reports automatically
```

`apiKey` is the credential printed when you first run `spendgaugeai serve` (or set via
`SPENDGAUGEAI_API_KEY` — see the [main README](../../README.md#quickstart)).

`wrap()` mutates the client you pass it and returns the same object — since an app typically
constructs one Anthropic client and reuses it everywhere, every existing call site starts
reporting with zero further changes. Streaming reports once the stream completes rather than
per-chunk. Every report is wrapped in its own try/catch, so a SpendGaugeAI outage can never
break a real request.

## `.log()` — manual, for more control

```ts
await spendgauge.log({
  model: "claude-sonnet-4-6",
  inputTokens: 1200,
  outputTokens: 450,
});
```

`model` plus token counts are the only required fields — `project`/`sessionId` default
sensibly if omitted.

## Hard spend-cap enforcement — opt-in

Off by default. Once turned on, refuses to make the next Anthropic call at all when the global
budget (set on the dashboard or via `POST /usage/credit`) is exhausted — actual enforcement, not
just a Discord alert after the fact. Global cap only, fails open (proceeds) if SpendGaugeAI is
unreachable — see [`docs/DESIGN.md` §8b](../../docs/DESIGN.md) for the full design, including a
JS-specific caveat: `messages.stream(...)` returns synchronously by design (so you can chain
`.on(...)` before the request resolves), so its guard can only consult a short-lived cache
synchronously rather than awaiting a fresh check like `messages.create(...)` does.

```ts
import { BudgetExceededError, wrap } from "spendgaugeai-client";

const client = wrap(new Anthropic(), spendgauge, { enforce: true });
try {
  const response = await client.messages.create({ ... });
} catch (err) {
  if (err instanceof BudgetExceededError) {
    // blocked before it billed you
  }
}
```

Or check it yourself at a specific call site without adopting `wrap()`:

```ts
const exceeded = await spendgauge.checkBudget(); // true | false | null (null = check failed, fail open)
if (exceeded) {
  // skip the call, queue it, whatever fits your app
}
```

## Session scoping

`session_id`/`project` propagate through `AsyncLocalStorage` (Node's async-context-local
primitive — the equivalent of Python's `contextvars.ContextVar`), never a mutable field on the
client, so a shared long-lived client handling concurrent requests can't have one request's
session bleed into another's:

```ts
await spendgauge.spendgaugeSession({ sessionId: "abc123", project: "my-app" }, async () => {
  const response = await client.messages.create({ ... });
});
```

## Design

Full integration design (including the four resolved `wrap()` edge cases): see
[`docs/DESIGN.md` §8a](../../docs/DESIGN.md) in the main repo.

## License

[MIT](./LICENSE)
