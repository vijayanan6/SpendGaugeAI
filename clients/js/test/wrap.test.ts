import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SpendGaugeAIClient } from "../src/client.js";
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
});
