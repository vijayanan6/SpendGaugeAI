import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SpendGaugeAIClient } from "../src/client.js";

describe("SpendGaugeAIClient.log", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the expected payload shape with Bearer auth", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000/", apiKey: "key123", project: "my-app" });
    await client.log({ model: "claude-sonnet-4-6", inputTokens: 10, outputTokens: 5 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://localhost:8000/usage/log");
    expect(init.headers.Authorization).toBe("Bearer key123");
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      project: "my-app",
      model: "claude-sonnet-4-6",
      input_tokens: 10,
      output_tokens: 5,
      cache_write_tokens: 0,
      cache_read_tokens: 0,
      web_search_requests: 0,
      tools_used: [],
    });
  });

  it("maps iterations to the snake_case wire shape when supplied", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await client.log({
      model: "claude-haiku-4-5",
      iterations: [
        { type: "message", model: "claude-haiku-4-5", inputTokens: 900, outputTokens: 500 },
        { type: "advisor_message", model: "claude-opus-4-8", inputTokens: 300, outputTokens: 200 },
      ],
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.iterations).toEqual([
      { type: "message", model: "claude-haiku-4-5", input_tokens: 900, cache_write_tokens: 0, cache_read_tokens: 0, output_tokens: 500 },
      { type: "advisor_message", model: "claude-opus-4-8", input_tokens: 300, cache_write_tokens: 0, cache_read_tokens: 0, output_tokens: 200 },
    ]);
  });

  it("omits the iterations key entirely (not null) when not supplied — regression guard", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await client.log({ model: "claude-sonnet-4-6" });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body).not.toHaveProperty("iterations");
  });

  it("never throws even when the request fails", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await expect(client.log({ model: "claude-sonnet-4-6" })).resolves.toBeUndefined();
  });

  it("propagates session_id/project from spendgaugeSession() to log() calls made inside it", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123", project: "default" });
    await client.spendgaugeSession({ sessionId: "sess-1", project: "scoped-app" }, async () => {
      await client.log({ model: "claude-sonnet-4-6" });
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.session_id).toBe("sess-1");
    expect(body.project).toBe("scoped-app");
  });

  it("does not leak session context across concurrent independent calls", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    const bodies: any[] = [];
    fetchMock.mockImplementation(async (_url: string, init: any) => {
      bodies.push(JSON.parse(init.body));
      return new Response("{}", { status: 200 });
    });

    await Promise.all([
      client.spendgaugeSession({ sessionId: "a" }, () => client.log({ model: "m" })),
      client.spendgaugeSession({ sessionId: "b" }, () => client.log({ model: "m" })),
    ]);

    const sessionIds = bodies.map((b) => b.session_id).sort();
    expect(sessionIds).toEqual(["a", "b"]);
  });

  it("falls back to a fresh generated session_id when none is passed to spendgaugeSession", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await client.spendgaugeSession({}, async () => {
      await client.log({ model: "claude-sonnet-4-6" });
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(typeof body.session_id).toBe("string");
    expect(body.session_id.length).toBeGreaterThan(0);
  });
});

describe("SpendGaugeAIClient.checkBudget", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns true when the server reports the cap exceeded", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ exceeded: true }), { status: 200 }));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });

    await expect(client.checkBudget()).resolves.toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url.toString()).toBe("http://localhost:8000/usage/budget");
    expect(init.headers.Authorization).toBe("Bearer key123");
  });

  it("includes project as a query param when given, omits it otherwise", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ exceeded: false }), { status: 200 }));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });

    await client.checkBudget({ project: "demo-app" });
    expect(fetchMock.mock.calls[0][0].toString()).toBe("http://localhost:8000/usage/budget?project=demo-app");
  });

  it("returns null (fails open) on network failure", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await expect(client.checkBudget()).resolves.toBeNull();
  });

  it("returns null (fails open) on a non-2xx response", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 401 }));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await expect(client.checkBudget()).resolves.toBeNull();
  });

  it("caches the result within cacheSeconds instead of refetching", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ exceeded: false }), { status: 200 }));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });

    await client.checkBudget({ cacheSeconds: 60 });
    await client.checkBudget({ cacheSeconds: 60 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("peekCachedBudgetStatus reflects a warm cache and returns null when cold", async () => {
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    expect(client.peekCachedBudgetStatus()).toBeNull();

    fetchMock.mockResolvedValue(new Response(JSON.stringify({ exceeded: true, near_limit: false }), { status: 200 }));
    await client.checkBudget({ cacheSeconds: 60 });
    expect(client.peekCachedBudgetStatus({ cacheSeconds: 60 })).toEqual({
      startingBalance: 0,
      remainingUsd: 0,
      exceeded: true,
      nearLimit: false,
    });
  });
});

describe("SpendGaugeAIClient.getBudgetStatus", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("maps the full server response, including near_limit -> nearLimit", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ starting_balance: 10, remaining_usd: 2, exceeded: false, near_limit: true }),
        { status: 200 },
      ),
    );
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });

    await expect(client.getBudgetStatus()).resolves.toEqual({
      startingBalance: 10,
      remainingUsd: 2,
      exceeded: false,
      nearLimit: true,
    });
  });

  it("returns null (fails open) on network failure", async () => {
    fetchMock.mockRejectedValue(new Error("network down"));
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });
    await expect(client.getBudgetStatus()).resolves.toBeNull();
  });

  it("shares its cache with checkBudget (one cached response backs both)", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ exceeded: false, near_limit: true }), { status: 200 }),
    );
    const client = new SpendGaugeAIClient({ baseUrl: "http://localhost:8000", apiKey: "key123" });

    await client.checkBudget({ cacheSeconds: 60 });
    await client.getBudgetStatus({ cacheSeconds: 60 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
