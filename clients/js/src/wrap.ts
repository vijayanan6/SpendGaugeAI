/**
 * wrap.ts — patches messages.create and messages.stream on an Anthropic
 * client so every call reports itself automatically (docs/DESIGN.md §8a).
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
 * - tools_used comes from `tool_use`-type content blocks; web_search_requests
 *   from `usage.server_tool_use`, a separate field — not one place.
 * - Every report is wrapped in try/catch — reporting usage must never be
 *   able to break the caller's real request.
 */
import type { SpendGaugeAIClient, UsageLogParams } from "./client.js";

interface UsageLike {
  input_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
  output_tokens?: number;
  server_tool_use?: { web_search_requests?: number } | null;
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
    .filter((b): b is ContentBlockLike & { name: string } => b.type === "tool_use" && typeof b.name === "string")
    .map((b) => b.name);
}

function extractWebSearchRequests(message: MessageLike): number {
  return message.usage?.server_tool_use?.web_search_requests ?? 0;
}

function reportParams(message: MessageLike, fallbackModel: string): UsageLogParams {
  return {
    model: message.model ?? fallbackModel,
    toolsUsed: extractToolsUsed(message),
    webSearchRequests: extractWebSearchRequests(message),
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
        } else if (event.type === "content_block_start" && event.content_block?.type === "tool_use" && event.content_block.name) {
          toolsUsed.push(event.content_block.name);
        } else if (event.type === "message_delta" && event.usage) {
          outputTokens = event.usage.output_tokens ?? outputTokens;
          webSearchRequests = event.usage.server_tool_use?.web_search_requests ?? webSearchRequests;
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

// Minimal structural shape wrap() needs. Avoids a hard runtime dependency on
// @anthropic-ai/sdk (it's a peerDependency — apps bring their own version);
// TypeScript structurally matches a real Anthropic/AsyncAnthropic-style client.
export interface WrappableAnthropicClient {
  messages: {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    create: (...args: any[]) => Promise<any>;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    stream: (...args: any[]) => any;
  };
}

export function wrap<T extends WrappableAnthropicClient>(anthropicClient: T, spendgauge: SpendGaugeAIClient): T {
  const messages = anthropicClient.messages;
  const originalCreate = messages.create.bind(messages);
  const originalStream = messages.stream.bind(messages);

  messages.create = (async (...args: unknown[]) => {
    const params = args[0] as { model?: string; stream?: boolean } | undefined;
    const requestedModel = params?.model ?? "unknown";
    const response = await originalCreate(...args);
    if (params?.stream) {
      return wrapRawStream(response as AsyncIterable<RawStreamEventLike>, spendgauge, requestedModel);
    }
    await reportSafely(spendgauge, response, requestedModel);
    return response;
  }) as typeof messages.create;

  messages.stream = ((...args: unknown[]) => {
    const stream = originalStream(...args);
    const requestedModel = (args[0] as { model?: string } | undefined)?.model ?? "unknown";
    Promise.resolve(stream.finalMessage())
      .then((finalMessage: MessageLike) => reportSafely(spendgauge, finalMessage, requestedModel))
      .catch(() => {
        // Stream errored/aborted before completion — nothing to report.
      });
    return stream;
  }) as typeof messages.stream;

  return anthropicClient;
}
