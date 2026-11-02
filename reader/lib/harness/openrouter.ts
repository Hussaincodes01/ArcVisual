/**
 * A small OpenRouter chat client for tool-calling agents.
 *
 * Three properties carried over from the Python provider, each learned in production:
 *
 * - **Every call has a wall-clock limit.** `AbortSignal.timeout` covers the whole
 *   request, body included. A slow call ends with an error the agent can act on,
 *   instead of the platform killing the function mid-call.
 * - **A model chain, not a model.** A `:free` endpoint that is spent for the day
 *   (429 "free-models-per-day"), withdrawn (404 "No endpoints found") or too slow
 *   hands over to the next model.
 * - **Reasoning is capped.** On the free Nemotron endpoint, measured side by side, a
 *   diagram took 66s with unbounded reasoning and 15s with an 800-token cap.
 *
 * `reasoning_details` from each reply is kept on the assistant message, so the next
 * turn continues the same line of reasoning instead of starting over.
 */

export interface ToolDef {
  type: "function";
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

export interface ToolCall {
  id: string;
  type?: "function";
  function: { name: string; arguments: string };
}

export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  /** Opaque; passed back unmodified so the model continues its reasoning. */
  reasoning_details?: unknown;
}

export interface Usage {
  prompt: number;
  completion: number;
  reasoning: number;
}

export interface ChatResult {
  message: Message;
  model: string;
  usage: Usage;
}

export interface ClientOptions {
  apiKey: string;
  baseUrl?: string;
  /** Preference order. Spent or withdrawn models are skipped for the rest of the run. */
  models: string[];
  /** Reasoning tokens per call; 0 lets the model reason without a cap. */
  reasoningMaxTokens: number;
  maxTokens?: number;
  /**
   * Ceiling on ONE call, within the overall deadline. Without it a slow free model
   * waits out the entire budget and leaves the paid fallback no time to answer.
   */
  callTimeoutMs?: number;
  /** No call starts with less than this left. */
  minCallMs?: number;
  /** Injected in tests. */
  fetch?: typeof fetch;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/** Below this many milliseconds left, no call starts: it could not finish. */
export const MIN_CALL_MS = 8_000;
export const CALL_TIMEOUT_MS = 90_000;

export class OutOfTime extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OutOfTime";
  }
}

export class NoModelLeft extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NoModelLeft";
  }
}

/** Errors that mean the MODEL is unusable now, as opposed to this request. */
function modelGone(status: number, body: string, model: string): boolean {
  if (status === 404) return /no endpoints found|model_not_found|not a valid model/i.test(body);
  if (status === 400) return /model_decommissioned|not a valid model/i.test(body);
  if (status === 429) return model.endsWith(":free") && /per[- ]day/i.test(body);
  return false;
}

export class Chat {
  private readonly unavailable = new Set<string>();
  private readonly fetch: typeof fetch;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly minCallMs: number;

  constructor(private readonly opts: ClientOptions) {
    this.fetch = opts.fetch ?? fetch;
    this.now = opts.now ?? Date.now;
    this.sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.minCallMs = opts.minCallMs ?? MIN_CALL_MS;
  }

  /** The models still worth trying, in order. */
  models(): string[] {
    const live = this.opts.models.filter((m) => !this.unavailable.has(m));
    return live.length ? live : [];
  }

  async send(messages: Message[], tools: ToolDef[], deadline: number): Promise<ChatResult> {
    let lastError = "no model configured";
    for (const model of this.models()) {
      const budget = deadline - this.now();
      if (budget < this.minCallMs) {
        throw new OutOfTime(`only ${Math.max(0, Math.round(budget / 1000))}s left; no call started`);
      }
      let retriedRateLimit = false;
      for (;;) {
        const result = await this.once(model, messages, tools, deadline);
        if (result.kind === "ok") return result.value;
        if (result.kind === "rate-limited" && !retriedRateLimit && deadline - this.now() > this.minCallMs + 3_000) {
          // A per-minute limit clears in seconds; one short wait is worth it.
          retriedRateLimit = true;
          await this.sleep(3_000);
          continue;
        }
        lastError = result.error;
        if (result.kind === "fatal") throw new Error(result.error);
        // "gone", "too-slow" or a second rate limit: this model is done for this run.
        this.unavailable.add(model);
        break;
      }
    }
    throw new NoModelLeft(`every model in the chain failed; last: ${lastError}`);
  }

  private async once(
    model: string,
    messages: Message[],
    tools: ToolDef[],
    deadline: number,
  ): Promise<
    | { kind: "ok"; value: ChatResult }
    | { kind: "gone" | "too-slow" | "rate-limited" | "fatal"; error: string }
  > {
    const cap = this.opts.reasoningMaxTokens;
    const body = {
      model,
      messages,
      tools,
      tool_choice: "auto",
      max_tokens: this.opts.maxTokens ?? 8192,
      reasoning: cap > 0 ? { max_tokens: cap } : { enabled: true },
    };
    const budget = Math.min(deadline - this.now(), this.opts.callTimeoutMs ?? CALL_TIMEOUT_MS);
    let res: Response;
    try {
      res = await this.fetch(`${this.opts.baseUrl ?? "https://openrouter.ai/api/v1"}/chat/completions`, {
        method: "POST",
        headers: { Authorization: `Bearer ${this.opts.apiKey}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(Math.max(1, budget)),
      });
    } catch (err) {
      const name = (err as Error)?.name ?? "";
      if (name === "TimeoutError" || name === "AbortError") {
        if (model.endsWith(":free")) return { kind: "too-slow", error: `${model}: no answer within ${Math.round(budget / 1000)}s` };
        throw new OutOfTime(`${model}: no answer within ${Math.round(budget / 1000)}s`);
      }
      return { kind: "fatal", error: `${model}: ${String(err)}` };
    }
    let text: string;
    try {
      text = await res.text();
    } catch (err) {
      const name = (err as Error)?.name ?? "";
      if (name === "TimeoutError" || name === "AbortError") {
        if (model.endsWith(":free")) return { kind: "too-slow", error: `${model}: reply cut off at the deadline` };
        throw new OutOfTime(`${model}: reply cut off at the deadline`);
      }
      return { kind: "fatal", error: `${model}: ${String(err)}` };
    }
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(text) as Record<string, unknown>;
    } catch {
      /* non-JSON error page; handled by status below */
    }
    // OpenRouter can report an upstream error inside a 200.
    const inner = parsed.error as { code?: unknown; message?: unknown } | undefined;
    const status = res.ok && inner ? (typeof inner.code === "number" ? inner.code : 502) : res.status;
    if (status === 200) {
      const choice = (parsed.choices as { message?: Message }[] | undefined)?.[0];
      if (!choice?.message) return { kind: "fatal", error: `${model}: reply had no message` };
      const u = (parsed.usage ?? {}) as Record<string, unknown>;
      const details = (u.completion_tokens_details ?? {}) as Record<string, unknown>;
      return {
        kind: "ok",
        value: {
          message: { ...choice.message, role: "assistant" },
          model,
          usage: {
            prompt: Number(u.prompt_tokens ?? 0),
            completion: Number(u.completion_tokens ?? 0),
            reasoning: Number(details.reasoning_tokens ?? 0),
          },
        },
      };
    }
    const detail = `${model}: ${status} ${text.slice(0, 200)}`;
    if (modelGone(status, text, model)) return { kind: "gone", error: detail };
    if (status === 429) return { kind: "rate-limited", error: detail };
    if (status >= 500) return { kind: "gone", error: detail };
    return { kind: "fatal", error: detail };
  }
}

export function addUsage(a: Usage, b: Usage): Usage {
  return { prompt: a.prompt + b.prompt, completion: a.completion + b.completion, reasoning: a.reasoning + b.reasoning };
}
