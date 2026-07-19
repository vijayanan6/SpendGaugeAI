/**
 * client.ts — SpendGaugeAIClient (docs/DESIGN.md §8a).
 *
 * Best-effort HTTP client for POST /usage/log. Every method is designed to
 * never throw — reporting usage must never be able to break the app using it.
 *
 * session_id/project flow through AsyncLocalStorage — Node's async-context-local
 * primitive, the equivalent of Python's contextvars.ContextVar — never a mutable
 * instance field, so a shared long-lived client handling concurrent requests
 * can't have one request's session bleed into another's.
 */
import { AsyncLocalStorage } from "node:async_hooks";

export interface UsageLogParams {
  model: string;
  inputTokens?: number;
  cacheWriteTokens?: number;
  cacheReadTokens?: number;
  outputTokens?: number;
  webSearchRequests?: number;
  toolsUsed?: string[];
  sessionId?: string;
  project?: string;
}

interface SessionContext {
  sessionId: string;
  project?: string;
}

const sessionStorage = new AsyncLocalStorage<SessionContext>();

export interface SpendGaugeAIClientOptions {
  baseUrl: string;
  apiKey: string;
  project?: string;
  timeoutMs?: number;
}

export class SpendGaugeAIClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  readonly project: string;
  private readonly timeoutMs: number;

  constructor(options: SpendGaugeAIClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.project = options.project ?? "default";
    this.timeoutMs = options.timeoutMs ?? 2000;
  }

  private buildPayload(params: UsageLogParams): Record<string, unknown> {
    const ctx = sessionStorage.getStore();
    return {
      project: params.project ?? ctx?.project ?? this.project,
      session_id: params.sessionId ?? ctx?.sessionId,
      model: params.model,
      input_tokens: params.inputTokens ?? 0,
      cache_write_tokens: params.cacheWriteTokens ?? 0,
      cache_read_tokens: params.cacheReadTokens ?? 0,
      output_tokens: params.outputTokens ?? 0,
      web_search_requests: params.webSearchRequests ?? 0,
      tools_used: params.toolsUsed ?? [],
    };
  }

  /** Best-effort, async. Never throws. */
  async log(params: UsageLogParams): Promise<void> {
    const payload = this.buildPayload(params);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      await fetch(`${this.baseUrl}/usage/log`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
    } catch {
      // Silent by design — see module docstring.
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Scope session_id/project to the current async context for the duration of `fn`:
   *
   *   await client.spendgaugeSession({ sessionId, project: "my-app" }, async () => {
   *     const response = await anthropic.messages.create(...);
   *   });
   *
   * No context set -> each call falls back to a fresh server-generated UUID
   * (the server default — see docs/DESIGN.md §4).
   */
  spendgaugeSession<T>(options: { sessionId?: string; project?: string }, fn: () => T): T {
    const ctx: SessionContext = {
      sessionId: options.sessionId ?? crypto.randomUUID(),
      project: options.project,
    };
    return sessionStorage.run(ctx, fn);
  }
}
