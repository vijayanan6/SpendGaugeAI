import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BudgetExceededError, SpendGaugeAIClient } from "../src/client.js";
import { wrap, type WrappableAnthropicClient } from "../src/wrap.js";

function fakeMessage(overrides: Record<string, unknown> = {}) {
  return {
    model: "claude-sonnet-4-6",
    content: [
      { type: "text", text: "hello" },
      { type: "tool_use", name: "search_docs", input: {} },
    ],
    usage: {
      input_tokens: 100,
      output_tokens: 50,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 20,
      server_tool_use: { web_search_requests: 2 },
    },
    ...overrides,
  };
}

describe("wrap()", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let spendgauge: SpendGaugeAIClient;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    spendgauge = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reports tools_used and web_search_requests from a non-streaming create() call", async () => {
    const fakeClient: WrappableAnthropicClient = {
      messages: {
        create: vi.fn().mockResolvedValue(fakeMessage()),
        stream: vi.fn(),
      },
    };
    const wrapped = wrap(fakeClient, spendgauge);

    const response = await wrapped.messages.create({ model: "claude-sonnet-4-6" });
    expect(response.content[0].text).toBe("hello"); // response forwarded unchanged

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.model).toBe("claude-sonnet-4-6");
    expect(body.tools_used).toEqual(["search_docs"]);
    expect(body.web_search_requests).toBe(2);
    expect(body.input_tokens).toBe(100);
    expect(body.output_tokens).toBe(50);
    expect(body.cache_read_tokens).toBe(20);
  });

  it("does not throw even if the underlying create() call fails", async () => {
    const fakeClient: WrappableAnthropicClient = {
      messages: {
        create: vi.fn().mockRejectedValue(new Error("api down")),
        stream: vi.fn(),
      },
    };
    const wrapped = wrap(fakeClient, spendgauge);
    await expect(wrapped.messages.create({ model: "claude-sonnet-4-6" })).rejects.toThrow("api down");
    // the real API error still propagates to the caller — only reporting is swallowed
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports from the final accumulated message once a stream completes, forwarding the stream unchanged", async () => {
    const finalMessage = fakeMessage({ model: "claude-haiku-4-5" });
    const fakeStream = {
      finalMessage: vi.fn().mockResolvedValue(finalMessage),
      [Symbol.asyncIterator]: async function* () {
        yield { type: "content_block_delta" };
      },
    };
    const fakeClient: WrappableAnthropicClient = {
      messages: {
        create: vi.fn(),
        stream: vi.fn().mockReturnValue(fakeStream),
      },
    };
    const wrapped = wrap(fakeClient, spendgauge);

    const stream = wrapped.messages.stream({ model: "claude-haiku-4-5" });
    expect(stream).toBe(fakeStream); // forwarded unchanged, not proxied

    // Caller consumes events as normal.
    const events = [];
    for await (const event of stream) events.push(event);
    expect(events).toHaveLength(1);

    // Reporting happens off finalMessage() independently of caller iteration —
    // wait a tick for that background promise chain to resolve.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.model).toBe("claude-haiku-4-5");
    expect(body.tools_used).toEqual(["search_docs"]);
  });

  it("reports real usage from create({ stream: true }) instead of silently logging zero", async () => {
    // Regression: create({ stream: true }) returns a raw async-iterable of
    // SSE events, not a populated Message — reading .usage/.content directly
    // off it (as the non-streaming path does) silently produced an all-zero
    // report with no error anywhere.
    const rawEvents = [
      {
        type: "message_start",
        message: {
          model: "claude-sonnet-4-6",
          usage: { input_tokens: 100, cache_creation_input_tokens: 0, cache_read_input_tokens: 20 },
        },
      },
      { type: "content_block_start", content_block: { type: "tool_use", name: "search_docs" } },
      { type: "message_delta", usage: { output_tokens: 50, server_tool_use: { web_search_requests: 2 } } },
      { type: "message_stop" },
    ];
    const fakeRawStream = {
      [Symbol.asyncIterator]: async function* () {
        for (const event of rawEvents) yield event;
      },
    };
    const fakeClient: WrappableAnthropicClient = {
      messages: {
        create: vi.fn().mockResolvedValue(fakeRawStream),
        stream: vi.fn(),
      },
    };
    const wrapped = wrap(fakeClient, spendgauge);

    const stream = await wrapped.messages.create({ model: "claude-sonnet-4-6", stream: true });
    const seen = [];
    for await (const event of stream) seen.push(event);
    expect(seen).toEqual(rawEvents); // every event forwarded unchanged

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.model).toBe("claude-sonnet-4-6");
    expect(body.input_tokens).toBe(100);
    expect(body.output_tokens).toBe(50);
    expect(body.cache_read_tokens).toBe(20);
    expect(body.web_search_requests).toBe(2);
    expect(body.tools_used).toEqual(["search_docs"]);
  });

  it("swallows a stream that errors before completion without reporting", async () => {
    const fakeStream = {
      finalMessage: vi.fn().mockRejectedValue(new Error("stream aborted")),
    };
    const fakeClient: WrappableAnthropicClient = {
      messages: {
        create: vi.fn(),
        stream: vi.fn().mockReturnValue(fakeStream),
      },
    };
    const wrapped = wrap(fakeClient, spendgauge);
    const stream = wrapped.messages.stream({ model: "claude-sonnet-4-6" });
    expect(stream).toBe(fakeStream);

    await vi.waitFor(() => expect(fakeStream.finalMessage).toHaveBeenCalled());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  describe("wrap(..., { enforce: true })", () => {
    function mockBudget(exceeded: boolean) {
      fetchMock.mockImplementation(async (url: string | URL) => {
        if (url.toString().includes("/usage/budget")) {
          return new Response(JSON.stringify({ exceeded }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      });
    }

    it("throws BudgetExceededError before create() reaches the Anthropic client when the cap is exceeded", async () => {
      mockBudget(true);
      const createFn = vi.fn().mockResolvedValue(fakeMessage());
      const fakeClient: WrappableAnthropicClient = { messages: { create: createFn, stream: vi.fn() } };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true });

      await expect(wrapped.messages.create({ model: "claude-sonnet-4-6" })).rejects.toThrow(BudgetExceededError);
      expect(createFn).not.toHaveBeenCalled();
    });

    it("throws BudgetExceededError before create({ stream: true }) reaches the Anthropic client", async () => {
      mockBudget(true);
      const createFn = vi.fn();
      const fakeClient: WrappableAnthropicClient = { messages: { create: createFn, stream: vi.fn() } };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true });

      await expect(wrapped.messages.create({ model: "claude-sonnet-4-6", stream: true })).rejects.toThrow(BudgetExceededError);
      expect(createFn).not.toHaveBeenCalled();
    });

    it("proceeds normally through create() when enforce is on but the cap is not exceeded", async () => {
      mockBudget(false);
      const createFn = vi.fn().mockResolvedValue(fakeMessage());
      const fakeClient: WrappableAnthropicClient = { messages: { create: createFn, stream: vi.fn() } };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true });

      const response = await wrapped.messages.create({ model: "claude-sonnet-4-6" });
      expect(response.content[0].text).toBe("hello");
      expect(createFn).toHaveBeenCalledTimes(1);
    });

    it("never checks the budget when enforce is left off (default)", async () => {
      const createFn = vi.fn().mockResolvedValue(fakeMessage());
      const fakeClient: WrappableAnthropicClient = { messages: { create: createFn, stream: vi.fn() } };
      const wrapped = wrap(fakeClient, spendgauge);

      await wrapped.messages.create({ model: "claude-sonnet-4-6" });
      const budgetCalls = fetchMock.mock.calls.filter(([url]) => url.toString().includes("/usage/budget"));
      expect(budgetCalls).toHaveLength(0);
    });

    it("throws synchronously from messages.stream() when a warm cache already shows exceeded", async () => {
      mockBudget(true);
      await spendgauge.checkBudget({ cacheSeconds: 60 }); // warm the cache first

      const streamFn = vi.fn();
      const fakeClient: WrappableAnthropicClient = { messages: { create: vi.fn(), stream: streamFn } };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true, enforceCacheSeconds: 60 });

      expect(() => wrapped.messages.stream({ model: "claude-sonnet-4-6" })).toThrow(BudgetExceededError);
      expect(streamFn).not.toHaveBeenCalled();
    });

    it("proceeds through messages.stream() on a cold cache (fails open) and refreshes it in the background", async () => {
      mockBudget(false);
      const finalMessage = fakeMessage();
      const fakeStream = {
        finalMessage: vi.fn().mockResolvedValue(finalMessage),
        [Symbol.asyncIterator]: async function* () {},
      };
      const streamFn = vi.fn().mockReturnValue(fakeStream);
      const fakeClient: WrappableAnthropicClient = { messages: { create: vi.fn(), stream: streamFn } };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true });

      const stream = wrapped.messages.stream({ model: "claude-sonnet-4-6" });
      expect(stream).toBe(fakeStream); // cold cache -> proceeds synchronously, fails open
      expect(streamFn).toHaveBeenCalledTimes(1);

      await vi.waitFor(() => {
        const budgetCalls = fetchMock.mock.calls.filter(([url]) => url.toString().includes("/usage/budget"));
        expect(budgetCalls.length).toBeGreaterThan(0);
      });
    });
  });

  // Regression: client.beta.messages.toolRunner(...) — the documented,
  // "automatically covered" way to use wrap() with an agentic tool loop —
  // calls exclusively through client.beta.messages.create/.stream
  // internally, a genuinely separate object from client.messages
  // (confirmed live against the real @anthropic-ai/sdk). wrap() previously
  // only ever patched the stable resource, so every toolRunner-based app
  // silently never reported anything and enforce never blocked anything
  // either — discovered live wiring a real app to a real production server.
  describe("wrap() also covers client.beta.messages", () => {
    it("reports from a beta.messages.create() call", async () => {
      const fakeClient: WrappableAnthropicClient = {
        messages: { create: vi.fn(), stream: vi.fn() },
        beta: { messages: { create: vi.fn().mockResolvedValue(fakeMessage()), stream: vi.fn() } },
      };
      const wrapped = wrap(fakeClient, spendgauge);

      const response = await wrapped.beta!.messages.create({ model: "claude-sonnet-4-6" });
      expect(response.content[0].text).toBe("hello");
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it("reports from stable and beta messages independently", async () => {
      const fakeClient: WrappableAnthropicClient = {
        messages: { create: vi.fn().mockResolvedValue(fakeMessage()), stream: vi.fn() },
        beta: { messages: { create: vi.fn().mockResolvedValue(fakeMessage()), stream: vi.fn() } },
      };
      const wrapped = wrap(fakeClient, spendgauge);

      await wrapped.messages.create({ model: "claude-sonnet-4-6" });
      await wrapped.beta!.messages.create({ model: "claude-sonnet-4-6" });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it("leaves a client with no .beta untouched and still working", async () => {
      const fakeClient: WrappableAnthropicClient = {
        messages: { create: vi.fn().mockResolvedValue(fakeMessage()), stream: vi.fn() },
      };
      const wrapped = wrap(fakeClient, spendgauge);
      expect(wrapped.beta).toBeUndefined();

      await wrapped.messages.create({ model: "claude-sonnet-4-6" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it("throws BudgetExceededError from beta.messages.create() when the cap is exceeded", async () => {
      fetchMock.mockImplementation(async (url: string | URL) => {
        if (url.toString().includes("/usage/budget")) {
          return new Response(JSON.stringify({ exceeded: true }), { status: 200 });
        }
        return new Response("{}", { status: 200 });
      });
      const betaCreate = vi.fn();
      const fakeClient: WrappableAnthropicClient = {
        messages: { create: vi.fn(), stream: vi.fn() },
        beta: { messages: { create: betaCreate, stream: vi.fn() } },
      };
      const wrapped = wrap(fakeClient, spendgauge, { enforce: true });

      await expect(wrapped.beta!.messages.create({ model: "claude-sonnet-4-6" })).rejects.toThrow(BudgetExceededError);
      expect(betaCreate).not.toHaveBeenCalled();
    });
  });
});
