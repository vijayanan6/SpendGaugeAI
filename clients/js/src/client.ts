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

/**
 * One sub-inference entry from `usage.iterations` — present on beta
 * responses that went through a server-side agentic loop (e.g. the Advisor
 * tool, which runs a second, separately-priced model mid-response). `model`
 * is undefined/null for iteration types with no model of their own (e.g.
 * "compaction") — the server falls back to the top-level `model` for those.
 */
export interface IterationUsageParam {
  type?: string;
  model?: string | null;
  inputTokens?: number;
  cacheWriteTokens?: number;
  cacheReadTokens?: number;
  outputTokens?: number;
}

export interface UsageLogParams {
  model: string;
  inputTokens?: number;
  cacheWriteTokens?: number;
  cacheReadTokens?: number;
  outputTokens?: number;
  webSearchRequests?: number;
  toolsUsed?: string[];
  iterations?: IterationUsageParam[];
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

/**
 * Raised by wrap()'s enforcement guard (`{ enforce: true }`) when GET
 * /usage/budget confirms the configured global spend cap has been exceeded.
 * The one deliberate exception to "reporting must never break the app" —
 * enforcement is opt-in and meant to block. Any check failure (timeout,
 * network error, non-2xx) fails open instead of throwing — see
 * SpendGaugeAIClient.checkBudget().
 */
export class BudgetExceededError extends Error {
  constructor(message = "SpendGaugeAI: global spend cap exceeded — call blocked") {
    super(message);
    this.name = "BudgetExceededError";
  }
}

export interface CheckBudgetOptions {
  /** Defaults to the account-wide cap (undefined), not this client's
   * configured `project` — enforcement is deliberately global-only. Pass a
   * project explicitly to check that project's own spend against the global
   * cap instead. */
  project?: string;
  /** Cache TTL in seconds. Default 5. */
  cacheSeconds?: number;
}

/**
 * Full response shape of GET /usage/budget. `nearLimit` reuses the server's
 * existing `warning_threshold` (the same dollar amount that already triggers
 * the Discord low-credit warning) — "near your limit" means the same thing
 * everywhere. `nearLimit` is only ever true when `exceeded` is false (they're
 * mutually exclusive by construction on the server).
 */
export interface BudgetStatus {
  startingBalance: number;
  remainingUsd: number;
  exceeded: boolean;
  nearLimit: boolean;
}

interface BudgetCacheEntry {
  checkedAt: number;
  project?: string;
  status: BudgetStatus;
}

export class SpendGaugeAIClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  readonly project: string;
  private readonly timeoutMs: number;
  private budgetCache: BudgetCacheEntry | null = null;

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
      iterations: params.iterations?.map((it) => ({
        type: it.type,
        model: it.model ?? null,
        input_tokens: it.inputTokens ?? 0,
        cache_write_tokens: it.cacheWriteTokens ?? 0,
        cache_read_tokens: it.cacheReadTokens ?? 0,
        output_tokens: it.outputTokens ?? 0,
      })),
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

  private cachedStatus(project: string | undefined, cacheSeconds: number): BudgetStatus | null {
    const cache = this.budgetCache;
    if (!cache || cache.project !== project || Date.now() - cache.checkedAt >= cacheSeconds * 1000) {
      return null;
    }
    return cache.status;
  }

  /**
   * Best-effort fetch of GET /usage/budget — the full status
   * (`startingBalance`, `remainingUsd`, `exceeded`, `nearLimit`). Returns
   * null on any failure (timeout, network error, non-2xx) — never throws.
   * Cached per-project for `cacheSeconds`; the same cached response backs
   * both checkBudget()'s hard-block decision and wrap()'s `downgradeModel`
   * decision.
   */
  async getBudgetStatus(options: CheckBudgetOptions = {}): Promise<BudgetStatus | null> {
    const { project, cacheSeconds = 5 } = options;
    const cached = this.cachedStatus(project, cacheSeconds);
    if (cached !== null) return cached;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const url = new URL(`${this.baseUrl}/usage/budget`);
      if (project) url.searchParams.set("project", project);
      const resp = await fetch(url, {
        headers: { Authorization: `Bearer ${this.apiKey}` },
        signal: controller.signal,
      });
      if (!resp.ok) return null;
      const data = (await resp.json()) as {
        starting_balance?: number;
        remaining_usd?: number;
        exceeded?: boolean;
        near_limit?: boolean;
      };
      const status: BudgetStatus = {
        startingBalance: data.starting_balance ?? 0,
        remainingUsd: data.remaining_usd ?? 0,
        exceeded: Boolean(data.exceeded),
        nearLimit: Boolean(data.near_limit),
      };
      this.budgetCache = { checkedAt: Date.now(), project, status };
      return status;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Best-effort check of GET /usage/budget's `exceeded` field — the global
   * spend cap (docs/DESIGN.md hard spend-cap enforcement). Returns
   * true/false on a real answer, null on any failure — never throws. Thin
   * wrapper over getBudgetStatus(); same caching/semantics as before.
   */
  async checkBudget(options: CheckBudgetOptions = {}): Promise<boolean | null> {
    const status = await this.getBudgetStatus(options);
    return status === null ? null : status.exceeded;
  }

  /**
   * Synchronous peek at the cached getBudgetStatus() result — used
   * internally by wrap()'s `messages.stream()` guard, which can't await an
   * async check without breaking the synchronous EventEmitter-return
   * contract the Anthropic SDK relies on for `.stream(...).on(...)`
   * chaining. A cold or stale cache returns null — "proceed"/"no downgrade",
   * matching the fail-open default everywhere else in this SDK — not a
   * network call itself.
   */
  peekCachedBudgetStatus(options: CheckBudgetOptions = {}): BudgetStatus | null {
    return this.cachedStatus(options.project, options.cacheSeconds ?? 5);
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
