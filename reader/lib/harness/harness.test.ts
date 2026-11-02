import { describe, expect, it } from "vitest";
import { drawDiagram, type DiagramRequest } from "./diagramAgent";
import { checkDrawing, checkScript, emptyState, runTool, type DiagramState } from "./diagramTools";
import { Chat, NoModelLeft, OutOfTime, type Message } from "./openrouter";

// -- fixtures ----------------------------------------------------------------- //

const ATTENTION = {
  title: "Scaled dot-product attention",
  elements: [
    { id: "q", kind: "stack", label: "Queries Q", column: 0, row: 0, cells: ["q₁", "q₂", "q₃"] },
    { id: "k", kind: "stack", label: "Keys K", column: 0, row: 1, cells: ["k₁", "k₂", "k₃"] },
    { id: "dot", kind: "op", label: "QKᵀ", column: 1, row: 0, row_span: 2 },
    { id: "scores", kind: "grid", label: "scores", column: 2, row: 0, row_span: 2, size: 3 },
    { id: "sm", kind: "op", label: "softmax", column: 3, row: 0, row_span: 2 },
    { id: "out", kind: "block", label: "Output", column: 4, row: 0, row_span: 2 },
  ],
  connections: [
    { src: "q", dst: "dot" },
    { src: "k", dst: "dot" },
    { src: "dot", dst: "scores" },
    { src: "scores", dst: "sm" },
    { src: "sm", dst: "out" },
  ],
};
const STEPS = {
  steps: [
    { caption: "Every token brings a query and a key.", show: ["q", "k"] },
    { caption: "Each query meets every key.", show: ["dot", "scores"], flow: ["q", "dot", "scores"] },
    { caption: "Row 1 is how well q₁ matches each key.", marks: [{ target: "scores", item: 0, col: 0 }] },
    { caption: "Softmax turns scores into weights.", show: ["sm", "out"], focus: ["out"] },
  ],
};

const REQUEST: DiagramRequest = {
  title: "Attention Is All You Need",
  claim: "Each query is scored against every key; softmax turns the scores into weights.",
  concept: "Scaled dot-product attention",
  quote: "We compute the dot products of the query with all keys",
  section: "We compute the dot products of the query with all keys, divide each by sqrt(d_k).",
  equations: [],
};

function state(over: Partial<DiagramState> = {}): DiagramState {
  return { ...emptyState(), ...JSON.parse(JSON.stringify(ATTENTION)), ...STEPS, ...over };
}

let callId = 0;
function toolCall(name: string, args: unknown) {
  return { id: `call_${++callId}`, type: "function" as const, function: { name, arguments: JSON.stringify(args) } };
}

function reply(toolCalls: ReturnType<typeof toolCall>[], extra: Record<string, unknown> = {}) {
  return new Response(
    JSON.stringify({
      choices: [{ message: { role: "assistant", content: "", tool_calls: toolCalls, ...extra } }],
      usage: { prompt_tokens: 100, completion_tokens: 50, completion_tokens_details: { reasoning_tokens: 20 } },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

/** A fetch that answers from a script and records every request body. */
function scriptedFetch(answers: (Response | ((init: RequestInit) => Promise<Response>))[]) {
  const sent: { model: string; messages: Message[]; reasoning: unknown }[] = [];
  const fn = (async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    sent.push(body);
    const next = answers.shift();
    if (!next) throw new Error("no scripted answer left");
    return typeof next === "function" ? next(init) : next;
  }) as unknown as typeof fetch;
  return { fn, sent };
}

const NO_SLEEP = async () => {};

// -- tools ---------------------------------------------------------------------- //

describe("diagram tools", () => {
  it("accept a sound diagram and script", () => {
    expect(checkDrawing(state())).toEqual([]);
    const { problems, notes } = checkScript(state());
    expect(problems).toEqual([]);
    expect(notes.some((n) => n.startsWith("runs "))).toBe(true);
  });

  it("name the cells two elements share", () => {
    const s = state();
    s.elements[1].row = 0; // keys onto the queries' cell
    expect(checkDrawing(s)).toContain("k and q share grid cell (0, 0); move one");
  });

  it("let a container span what it groups", () => {
    const s = state();
    s.elements.push({ id: "head", kind: "container", label: "One head", column: 1, row: 0, col_span: 3, row_span: 2 });
    expect(checkDrawing(s)).toEqual([]);
  });

  it("refuse arrows and steps that name nothing", () => {
    const s = state({ connections: [{ src: "q", dst: "ghost" }] });
    expect(checkDrawing(s)).toContain('arrow q->ghost: no element "ghost"');
    const t = state({ steps: [{ caption: "Look.", focus: ["ghost"] }] });
    expect(checkScript(t).problems).toContain('step 1: no element "ghost"');
  });

  it("refuse marks on things without items", () => {
    const s = state({ steps: [{ caption: "Light it.", marks: [{ target: "dot", item: 0 }] }] });
    expect(checkScript(s).problems[0]).toMatch(/dot is a op; marks go on/);
  });

  it("measure runtime with the renderer's own timeline", () => {
    const long = Array.from({ length: 8 }, (_, i) => ({
      caption: `Step ${i}.`,
      focus: ["out"],
      flow: ["q", "dot", "scores", "sm", "out", "sm", "scores", "dot"],
      relabel: [{ id: "out", label: `v${i}` }],
    }));
    const { problems } = checkScript(state({ steps: long }));
    expect(problems.some((p) => p.includes("keep it under 70s"))).toBe(true);
  });

  it("accept a whole sound diagram in one call", () => {
    const s = emptyState();
    const { reply, done } = runTool(s, "submit_diagram", JSON.stringify({ ...ATTENTION, ...STEPS }));
    expect(reply.problems).toEqual([]);
    expect(done).toBe(true);
    expect(s.steps).toHaveLength(4);
  });

  it("refuse a diagram with every problem listed at once", () => {
    const broken = JSON.parse(JSON.stringify({ ...ATTENTION, ...STEPS }));
    broken.elements[1].row = 0;
    broken.connections.push({ src: "q", dst: "ghost" });
    const { reply, done } = runTool(emptyState(), "submit_diagram", JSON.stringify(broken));
    expect(done).toBe(false);
    expect(reply.problems).toEqual(
      expect.arrayContaining(["k and q share grid cell (0, 0); move one", 'arrow q->ghost: no element "ghost"']),
    );
  });

  it("answer an unknown tool or broken JSON with a problem, not an exception", () => {
    expect(runTool(emptyState(), "paint", "{}").reply.problems[0]).toMatch(/no tool named "paint"/);
    expect(runTool(emptyState(), "submit_diagram", "{not json").reply.problems[0]).toMatch(/not valid JSON/);
  });
});

// -- agent loop ----------------------------------------------------------------- //

describe("diagram agent", () => {
  it("accepts a sound first submission in one turn", async () => {
    const { fn } = scriptedFetch([reply([toolCall("submit_diagram", { ...ATTENTION, ...STEPS })])]);
    const result = await drawDiagram(REQUEST, {
      apiKey: "k",
      models: ["m"],
      reasoningMaxTokens: 800,
      deadline: Date.now() + 60_000,
      fetch: fn,
    });
    expect(result.ok).toBe(true);
    expect(result.turns).toBe(1);
  });

  it("fixes a reported problem in a second turn, carrying reasoning forward", async () => {
    const broken = JSON.parse(JSON.stringify(ATTENTION));
    broken.elements[1].row = 0;
    const { fn, sent } = scriptedFetch([
      reply([toolCall("submit_diagram", { ...broken, ...STEPS })], { reasoning_details: [{ type: "reasoning.text", text: "plan" }] }),
      reply([toolCall("submit_diagram", { ...ATTENTION, ...STEPS })]),
    ]);
    const result = await drawDiagram(REQUEST, {
      apiKey: "k",
      models: ["nvidia/nemotron-3.5-lightning:free"],
      reasoningMaxTokens: 800,
      deadline: Date.now() + 60_000,
      fetch: fn,
    });
    expect(result.ok).toBe(true);
    expect(result.turns).toBe(2);
    expect(result.captions).toEqual(STEPS.steps.map((s) => s.caption));
    expect((result.params?.elements as unknown[]).length).toBe(6);
    expect(result.usage).toEqual({ prompt: 200, completion: 100, reasoning: 40 });
    // The tool told the model exactly what was wrong ...
    const toolReplies = sent[1].messages.filter((m) => m.role === "tool").map((m) => JSON.parse(String(m.content)));
    expect(toolReplies[0].problems).toContain("k and q share grid cell (0, 0); move one");
    // ... and the model's reasoning went back to it unmodified.
    const assistant = sent[1].messages.find((m) => m.role === "assistant");
    expect(assistant?.reasoning_details).toEqual([{ type: "reasoning.text", text: "plan" }]);
    // Reasoning is capped on every call.
    expect(sent[0].reasoning).toEqual({ max_tokens: 800 });
  });

  it("nudges once when the model answers in prose, then gives up", async () => {
    const prose = () =>
      new Response(JSON.stringify({ choices: [{ message: { role: "assistant", content: "Here is a diagram..." } }] }), { status: 200 });
    const { fn } = scriptedFetch([prose(), prose()]);
    const result = await drawDiagram(REQUEST, {
      apiKey: "k",
      models: ["m"],
      reasoningMaxTokens: 800,
      deadline: Date.now() + 60_000,
      fetch: fn,
    });
    expect(result.ok).toBe(false);
    expect(result.notes).toContain("the model answered without calling a tool, twice");
  });
});

// -- client --------------------------------------------------------------------- //

describe("openrouter client", () => {
  const ok = () => reply([toolCall("submit_diagram", {})]);

  it("hands a spent free model over to the next in the chain", async () => {
    const { fn, sent } = scriptedFetch([
      new Response(JSON.stringify({ error: { code: 429, message: "Rate limit exceeded: free-models-per-day" } }), { status: 429 }),
      ok(),
    ]);
    const chat = new Chat({ apiKey: "k", models: ["a:free", "b"], reasoningMaxTokens: 800, fetch: fn, sleep: NO_SLEEP });
    const result = await chat.send([], [], Date.now() + 60_000);
    expect(result.model).toBe("b");
    expect(sent.map((s) => s.model)).toEqual(["a:free", "b"]);
    expect(chat.models()).toEqual(["b"]);
  });

  it("waits once on a per-minute limit and retries the same model", async () => {
    const { fn, sent } = scriptedFetch([new Response("slow down", { status: 429 }), ok()]);
    const chat = new Chat({ apiKey: "k", models: ["a:free"], reasoningMaxTokens: 0, fetch: fn, sleep: NO_SLEEP });
    const result = await chat.send([], [], Date.now() + 60_000);
    expect(result.model).toBe("a:free");
    expect(sent).toHaveLength(2);
    expect(sent[0].reasoning).toEqual({ enabled: true });
  });

  it("treats an error inside a 200 as that error", async () => {
    const { fn } = scriptedFetch([
      new Response(JSON.stringify({ error: { code: 404, message: "No endpoints found for a:free" } }), { status: 200 }),
      ok(),
    ]);
    const chat = new Chat({ apiKey: "k", models: ["a:free", "b"], reasoningMaxTokens: 800, fetch: fn, sleep: NO_SLEEP });
    expect((await chat.send([], [], Date.now() + 60_000)).model).toBe("b");
  });

  it("cuts a slow free model off and leaves the fallback time to answer", async () => {
    const hang = (init: RequestInit) =>
      new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () => reject(Object.assign(new Error("timed out"), { name: "TimeoutError" })));
      });
    const { fn, sent } = scriptedFetch([hang, ok()]);
    const chat = new Chat({
      apiKey: "k",
      models: ["slow:free", "fast"],
      reasoningMaxTokens: 800,
      fetch: fn,
      sleep: NO_SLEEP,
      callTimeoutMs: 200,
      minCallMs: 10,
    });
    const started = Date.now();
    const result = await chat.send([], [], Date.now() + 5_000);
    expect(result.model).toBe("fast");
    expect(sent.map((s) => s.model)).toEqual(["slow:free", "fast"]);
    expect(Date.now() - started).toBeLessThan(2_000);
  });

  it("gives up with OutOfTime when a paid model is too slow", async () => {
    const hang = (init: RequestInit) =>
      new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () => reject(Object.assign(new Error("timed out"), { name: "TimeoutError" })));
      });
    const { fn } = scriptedFetch([hang]);
    const chat = new Chat({ apiKey: "k", models: ["paid"], reasoningMaxTokens: 800, fetch: fn, callTimeoutMs: 100, minCallMs: 10 });
    await expect(chat.send([], [], Date.now() + 5_000)).rejects.toBeInstanceOf(OutOfTime);
  });

  it("starts no call when the deadline is too close", async () => {
    const { fn, sent } = scriptedFetch([ok()]);
    const chat = new Chat({ apiKey: "k", models: ["a"], reasoningMaxTokens: 800, fetch: fn });
    await expect(chat.send([], [], Date.now() + 1_000)).rejects.toBeInstanceOf(OutOfTime);
    expect(sent).toHaveLength(0);
  });

  it("reports when every model has failed", async () => {
    const gone = () => new Response(JSON.stringify({ error: { code: 404, message: "No endpoints found" } }), { status: 404 });
    const { fn } = scriptedFetch([gone(), gone()]);
    const chat = new Chat({ apiKey: "k", models: ["a:free", "b"], reasoningMaxTokens: 800, fetch: fn });
    await expect(chat.send([], [], Date.now() + 60_000)).rejects.toBeInstanceOf(NoModelLeft);
  });
});
