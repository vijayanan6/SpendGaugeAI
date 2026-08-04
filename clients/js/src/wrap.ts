/**
 * wrap.ts — patches messages.create and messages.stream on an Anthropic
 * client so every call reports itself automatically (docs/DESIGN.md §8a).
 * Patches *two* separate resource objects: `client.messages` and, if
 * present, `client.beta.messages` — `client.beta.messages.toolRunner(...)`'s
 * internal agentic loop calls exclusively through the beta resource, so
 * patching only the stable one silently never covers toolRunner-based apps.
 *
 * Unlike the Python SDK, the JS Anthropic SDK has one client class (every
 * method already returns a Promise) — there's no sync/async split to patch
 * twice. The other three resolved edges still apply:
 *
 * - Streaming reports from the final accumulated message. `messages.stream()`
 *   returns a MessageStream that supports multiple independent consumers by
 *   design, so calling `.finalMessage()` here doesn't compete with however
 *   the caller iterates it — it resolves once the stream completes (or
 *   rejects if it errors/aborts) regardless of consumption pattern, the
 *   closest JS equivalent to Python's `finally`-block reporting.
 * - tools_used comes from `tool_use`- and `server_tool_use`-type content
 *   blocks (advisor, web_fetch, code_execution, etc. all arrive as the
 *   latter); web_search_requests from `usage.server_tool_use`, a separate
 *   field — not one place.
 * - `usage.iterations` (only present on beta responses that went through a
 *   server-side agentic loop, e.g. the Advisor tool running a second,
 *   separately-priced model mid-response) is forwarded to the server so cost
 *   can be summed per-iteration-model instead of priced entirely at the one
 *   top-level model — absent for the overwhelming majority of calls, in
 *   which case behavior is unchanged from before this field existed.
 * - Every report is wrapped in try/catch — reporting usage must never be
 *   able to break the caller's real request.
 */
import { BudgetExceededError, type SpendGaugeAIClient, type UsageLogParams, type IterationUsageParam } from "./client.js";

interface IterationUsageLike {
  type?: string;
  model?: string | null;
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  output_tokens?: number;
}

interface UsageLike {
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  output_tokens?: number;
  server_tool_use?: { web_search_requests?: number } | null;
  iterations?: IterationUsageLike[] | null;
}

interface ContentBlockLike {
  type: string;
  name?: string;
}

interface MessageLike {
  model?: string;
  usage?: UsageLike;
  content?: ContentBlockLike[];
}

interface RawStreamEventLike {
  type?: string;
  message?: MessageLike;
  content_block?: ContentBlockLike;
  usage?: UsageLike;
}

function extractToolsUsed(message: MessageLike): string[] {
  return (message.content ?? [])
    .filter((b): b is ContentBlockLike & { name: string } => (b.type === "tool_use" || b.type === "server_tool_use") && typeof b.name === "string")
    .map((b) => b.name);
}

function extractWebSearchRequests(message: MessageLike): number {
  return message.usage?.server_tool_use?.web_search_requests ?? 0;
}

function extractIterations(message: MessageLike): IterationUsageParam[] | undefined {
  const iterations = message.usage?.iterations;
  if (!iterations || iterations.length === 0) return undefined;
  return iterations.map((it) => ({
    type: it.type,
    model: it.model ?? null,
    inputTokens: it.input_tokens ?? 0,
    cacheWriteTokens: it.cache_creation_input_tokens ?? 0,
    cacheReadTokens: it.cache_read_input_tokens ?? 0,
    outputTokens: it.output_tokens ?? 0,
  }));
}

function reportParams(message: MessageLike, fallbackModel: string): UsageLogParams {
  return {
    model: message.model ?? fallbackModel,
    toolsUsed: extractToolsUsed(message),
    webSearchRequests: extractWebSearchRequests(message),
    iterations: extractIterations(message),
    inputTokens: message.usage?.input_tokens ?? 0,
    cacheWriteTokens: message.usage?.cache_creation_input_tokens ?? 0,
    cacheReadTokens: message.usage?.cache_read_input_tokens ?? 0,
    outputTokens: message.usage?.output_tokens ?? 0,
  };
}

async function reportSafely(spendgauge: SpendGaugeAIClient, message: MessageLike, fallbackModel: string): Promise<void> {
  try {
    await spendgauge.log(reportParams(message, fallbackModel));
  } catch {
    // Best-effort — never break the caller's real request.
  }
}

/**
 * Wraps the raw async-iterable of SSE events returned by
 * `messages.create({ stream: true, ... })` — the low-level streaming path,
 * distinct from `messages.stream()`. Forwards every event unchanged;
 * accumulates usage/tool info from the stream's own event types
 * (message_start, content_block_start, message_delta) and reports once
 * exhausted. Without this, `create({ stream: true })` silently reported zero
 * cost — usage/content only populate on the raw event objects this path
 * yields, never on a single top-level Message like a non-streaming response.
 */
function wrapRawStream(
  stream: AsyncIterable<RawStreamEventLike>,
  spendgauge: SpendGaugeAIClient,
  fallbackModel: string,
): AsyncIterable<RawStreamEventLike> {
  return {
    [Symbol.asyncIterator]() {
      const iterator = stream[Symbol.asyncIterator]();
      let model = fallbackModel;
      let inputTokens = 0;
      let cacheWriteTokens = 0;
      let cacheReadTokens = 0;
      let outputTokens = 0;
      let webSearchRequests = 0;
      const toolsUsed: string[] = [];
      let iterations: IterationUsageLike[] | null | undefined;
      let reported = false;

      const report = async () => {
        if (reported) return;
        reported = true;
        const accumulated: MessageLike = {
          model,
          usage: {
            input_tokens: inputTokens,
            cache_creation_input_tokens: cacheWriteTokens,
            cache_read_input_tokens: cacheReadTokens,
            output_tokens: outputTokens,
            server_tool_use: { web_search_requests: webSearchRequests },
            iterations,
          },
          content: toolsUsed.map((name) => ({ type: "tool_use", name })),
        };
        await reportSafely(spendgauge, accumulated, fallbackModel);
      };

      const absorb = (event: RawStreamEventLike) => {
        if (event.type === "message_start" && event.message) {
          model = event.message.model ?? model;
          inputTokens = event.message.usage?.input_tokens ?? inputTokens;
          cacheWriteTokens = event.message.usage?.cache_creation_input_tokens ?? cacheWriteTokens;
          cacheReadTokens = event.message.usage?.cache_read_input_tokens ?? cacheReadTokens;
        } else if (event.type === "content_block_start" && (event.content_block?.type === "tool_use" || event.content_block?.type === "server_tool_use") && event.content_block.name) {
          toolsUsed.push(event.content_block.name);
        } else if (event.type === "message_delta" && event.usage) {
          outputTokens = event.usage.output_tokens ?? outputTokens;
          webSearchRequests = event.usage.server_tool_use?.web_search_requests ?? webSearchRequests;
          iterations = event.usage.iterations ?? iterations;
        }
      };

      return {
        async next(): Promise<IteratorResult<RawStreamEventLike>> {
          const result = await iterator.next();
          if (result.done) {
            await report();
            return result;
          }
          absorb(result.value);
          return result;
        },
        async return(value?: unknown): Promise<IteratorResult<RawStreamEventLike>> {
          await report();
          if (iterator.return) return iterator.return(value) as Promise<IteratorResult<RawStreamEventLike>>;
          return { done: true, value } as IteratorResult<RawStreamEventLike>;
        },
        async throw(err?: unknown): Promise<IteratorResult<RawStreamEventLike>> {
          await report();
          if (iterator.throw) return iterator.throw(err) as Promise<IteratorResult<RawStreamEventLike>>;
          throw err;
        },
      };
    },
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
interface MessagesResourceLike {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  create: (...args: any[]) => Promise<any>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  stream: (...args: any[]) => any;
}

// Minimal structural shape wrap() needs. Avoids a hard runtime dependency on
// @anthropic-ai/sdk (it's a peerDependency — apps bring their own version);
// TypeScript structurally matches a real Anthropic client. `beta.messages` is
// optional here (older/minimal clients won't have it), but on a real
// Anthropic client it's a genuinely *separate* resource object from
// `.messages` — confirmed live (`client.messages !== client.beta.messages`)
// — and `client.beta.messages.toolRunner(...)`'s internal agentic loop calls
// exclusively through `.beta.messages`, never the stable resource. Patching
// only `.messages` (this project's original assumption) silently never
// covers toolRunner-based apps at all.
export interface WrappableAnthropicClient {
  messages: MessagesResourceLike;
  beta?: {
    messages: MessagesResourceLike;
  };
}

export interface WrapOptions {
  /**
   * Opt into hard spend-cap enforcement: checks the global budget cap
   * (SpendGaugeAIClient.checkBudget(), cached for `cacheSeconds`) before each
   * call and throws BudgetExceededError if it's confirmed exceeded. This is
   * the one part of wrap() that's allowed to break the caller's app —
   * everything else stays fail-safe. Off by default; fails open (proceeds)
   * on any check failure, same as checkBudget() itself.
   *
   * `messages.create(...)` (including the raw `stream: true` path) awaits a
   * real check before the request goes out. `messages.stream(...)` can't —
   * it returns its MessageStream synchronously by design, so callers can
   * chain `.on(...)` before the network call resolves — so its guard only
   * ever consults the cache synchronously (peekCachedBudgetStatus) and
   * opportunistically refreshes it in the background. In a sustained
   * `.stream()`-only workload the cap still gets enforced, just not
   * necessarily on the very first call after it's crossed.
   */
  enforce?: boolean;
  /**
   * Opt into graceful degradation: once spend is within the server's
   * `credit_config.warning_threshold` of the cap but not yet over it
   * (`nearLimit`), the outgoing `model` is swapped for `downgradeModel`
   * before the real call — the app keeps working, just cheaper, right up
   * until the real limit. Independent of `enforce`; either can be on
   * without the other. `exceeded` always wins over `nearLimit` when both
   * are checked (mutually exclusive by construction on the server). Cost
   * attribution needs no extra handling — usage is reported from the
   * *response*'s own `model` field (what Anthropic actually served), not
   * from what was requested. `messages.stream(...)` uses the same
   * synchronous cache peek as `enforce` does, for the same reason: the swap
   * must happen before `originalStream(...)` is called, and constructing
   * its return value is synchronous by SDK design.
   */
  downgradeModel?: string;
  /** Cache TTL in seconds for the enforcement/downgrade check. Default 5. */
  cacheSeconds?: number;
}

interface ResolvedWrapOptions {
  enforce: boolean;
  downgradeModel?: string;
  cacheSeconds: number;
}

function patchMessagesResource(messages: MessagesResourceLike, spendgauge: SpendGaugeAIClient, options: ResolvedWrapOptions): void {
  const { enforce, downgradeModel, cacheSeconds } = options;
  const originalCreate = messages.create.bind(messages);
  const originalStream = messages.stream.bind(messages);

  messages.create = (async (...args: unknown[]) => {
    let params = args[0] as { model?: string; stream?: boolean } | undefined;
    if (enforce || downgradeModel) {
      const status = await spendgauge.getBudgetStatus({ cacheSeconds });
      if (status) {
        if (enforce && status.exceeded) {
          throw new BudgetExceededError();
        }
        // Shallow-copy before overriding model — args[0] is the caller's own
        // object reference; mutating it in place would be a surprising,
        // caller-visible side effect their own code didn't expect.
        if (downgradeModel && status.nearLimit && params) {
          params = { ...params, model: downgradeModel };
          args = [params, ...args.slice(1)];
        }
      }
    }
    const requestedModel = params?.model ?? "unknown";
    const response = await originalCreate(...args);
    if (params?.stream) {
      return wrapRawStream(response as AsyncIterable<RawStreamEventLike>, spendgauge, requestedModel);
    }
    await reportSafely(spendgauge, response, requestedModel);
    return response;
  }) as typeof messages.create;

  messages.stream = ((...args: unknown[]) => {
    let params = args[0] as { model?: string } | undefined;
    if (enforce) {
      const cachedStatus = spendgauge.peekCachedBudgetStatus({ cacheSeconds });
      if (cachedStatus?.exceeded) {
        throw new BudgetExceededError();
      }
      // Can't await here without breaking messages.stream()'s synchronous
      // return contract — refresh the cache in the background instead.
      void spendgauge.getBudgetStatus({ cacheSeconds }).catch(() => {});
    }
    if (downgradeModel && params) {
      const cachedStatus = spendgauge.peekCachedBudgetStatus({ cacheSeconds });
      if (cachedStatus?.nearLimit) {
        // Shallow-copy — see the same note in messages.create above.
        params = { ...params, model: downgradeModel };
        args = [params, ...args.slice(1)];
      }
    }
    const stream = originalStream(...args);
    const requestedModel = params?.model ?? "unknown";
    Promise.resolve(stream.finalMessage())
      .then((finalMessage: MessageLike) => reportSafely(spendgauge, finalMessage, requestedModel))
      .catch(() => {
        // Stream errored/aborted before completion — nothing to report.
      });
    return stream;
  }) as typeof messages.stream;
}

export function wrap<T extends WrappableAnthropicClient>(
  anthropicClient: T,
  spendgauge: SpendGaugeAIClient,
  options: WrapOptions = {},
): T {
  const resolved = {
    enforce: options.enforce ?? false,
    downgradeModel: options.downgradeModel,
    cacheSeconds: options.cacheSeconds ?? 5,
  };

  patchMessagesResource(anthropicClient.messages, spendgauge, resolved);
  if (anthropicClient.beta?.messages) {
    patchMessagesResource(anthropicClient.beta.messages, spendgauge, resolved);
  }

  return anthropicClient;
}
