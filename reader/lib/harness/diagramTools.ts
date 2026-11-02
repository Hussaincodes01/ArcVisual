/**
 * The tools a model draws a `concept_diagram` with, and the checks behind them.
 *
 * One-shot generation returns a whole diagram as JSON and learns it was wrong only
 * from Gate 1, a full round trip later. Here every tool call is checked as it is made
 * and answers with concrete problems ("q and k share cell (0, 0)"), so the model
 * fixes them in the same conversation.
 *
 * The tools are deliberately coarse — `draw` sets every element and arrow at once,
 * `script` every step — because each turn on the free endpoint costs seconds: a
 * diagram should take two or three turns, not twenty.
 *
 * Runtime is measured with `planConceptDiagram`, the very function the browser uses
 * to animate the scene, so "fits the runtime cap" means exactly what the reader
 * will play. The Python template validates the result again before it is stored.
 */

import { planConceptDiagram } from "../scenes/plans";
import type { ToolDef } from "./openrouter";

export const KINDS = ["block", "op", "tokens", "stack", "grid", "bars", "text", "container"] as const;
export const TONES = ["neutral", "accent", "input", "output", "muted"] as const;
const ITEM_KINDS = new Set(["tokens", "stack", "grid", "bars"]);
const ID = /^[A-Za-z0-9_]+$/;
const MAX_COLUMNS = 8;
const MAX_ROWS = 6;
/** Same ceiling as concept_diagram.MAX_RUNTIME_S. */
export const MAX_RUNTIME_S = 70;

export interface DiagramElement {
  id: string;
  kind: string;
  label: string;
  column: number;
  row: number;
  col_span?: number;
  row_span?: number;
  cells?: string[];
  size?: number;
  values?: number[];
  tone?: string;
  note?: string;
}

export interface DiagramConnection {
  src: string;
  dst: string;
  label?: string | null;
  dashed?: boolean;
}

export interface DiagramStep {
  caption: string;
  show?: string[];
  focus?: string[];
  flow?: string[];
  marks?: { target: string; item: number; col?: number }[];
  relabel?: { id: string; label: string }[];
}

export interface DiagramState {
  title: string | null;
  elements: DiagramElement[];
  connections: DiagramConnection[];
  steps: DiagramStep[];
}

export interface ToolReply {
  ok: boolean;
  problems: string[];
  /** Advisory only; nothing here blocks `finish`. */
  notes: string[];
}

export function emptyState(): DiagramState {
  return { title: null, elements: [], connections: [], steps: [] };
}

const elementSchema = {
  type: "object",
  properties: {
    id: { type: "string", description: "Letters, digits and _ only." },
    kind: { type: "string", enum: [...KINDS] },
    label: { type: "string", description: "Short; Unicode maths (QKᵀ, √dₖ), never LaTeX." },
    column: { type: "integer", minimum: 0, maximum: MAX_COLUMNS - 1 },
    row: { type: "integer", minimum: 0, maximum: MAX_ROWS - 1 },
    col_span: { type: "integer", minimum: 1, maximum: MAX_COLUMNS },
    row_span: { type: "integer", minimum: 1, maximum: MAX_ROWS },
    cells: { type: "array", items: { type: "string" }, description: "Item labels (tokens/stack/bars) or grid row/column labels." },
    size: { type: "integer", minimum: 0, maximum: 8, description: "Item count, or N for an N×N grid." },
    values: { type: "array", items: { type: "number" }, description: "bars: one height each. grid: N×N, row-major. Only numbers the paper gives." },
    tone: { type: "string", enum: [...TONES] },
    note: { type: "string", description: "A short second line, e.g. a shape like n × d." },
  },
  required: ["id", "kind", "label", "column", "row"],
};

export const TOOLS: ToolDef[] = [
  {
    type: "function",
    function: {
      name: "draw",
      description:
        "Set the diagram's elements and arrows, replacing any drawn before. Returns the problems to fix " +
        "(unknown ids, two elements in one cell, ...). Call it again with the whole corrected diagram.",
      parameters: {
        type: "object",
        properties: {
          title: { type: "string", description: "Optional heading, under 60 characters." },
          elements: { type: "array", items: elementSchema, minItems: 2, maxItems: 16 },
          connections: {
            type: "array",
            maxItems: 20,
            items: {
              type: "object",
              properties: {
                src: { type: "string" },
                dst: { type: "string" },
                label: { type: "string" },
                dashed: { type: "boolean" },
              },
              required: ["src", "dst"],
            },
          },
        },
        required: ["elements", "connections"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "script",
      description:
        "Set the animation steps, replacing any set before. Each step is one beat: a caption plus what appears, " +
        "what is in focus, where a pulse flows, which cells light up, which labels change. Returns problems to fix.",
      parameters: {
        type: "object",
        properties: {
          steps: {
            type: "array",
            minItems: 1,
            maxItems: 8,
            items: {
              type: "object",
              properties: {
                caption: { type: "string", description: "One plain sentence, under 90 characters." },
                show: { type: "array", items: { type: "string" } },
                focus: { type: "array", items: { type: "string" } },
                flow: { type: "array", items: { type: "string" }, description: "A path of element ids, in order." },
                marks: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: { target: { type: "string" }, item: { type: "integer" }, col: { type: "integer" } },
                    required: ["target", "item"],
                  },
                },
                relabel: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: { id: { type: "string" }, label: { type: "string" } },
                    required: ["id", "label"],
                  },
                },
              },
              required: ["caption"],
            },
          },
        },
        required: ["steps"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "finish",
      description: "Submit the diagram. Refused, with the reasons, while draw or script still report problems.",
      parameters: { type: "object", properties: {} },
    },
  },
];

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

export function checkDrawing(state: DiagramState): string[] {
  const problems: string[] = [];
  const { elements, connections } = state;
  if (elements.length < 2) problems.push("draw at least 2 elements");
  if (elements.length > 16) problems.push(`${elements.length} elements; at most 16`);
  const ids = new Set<string>();
  const cells = new Map<string, string>();
  for (const e of elements) {
    if (!e.id || !ID.test(e.id)) problems.push(`id ${JSON.stringify(e.id)}: use letters, digits and _ only`);
    if (ids.has(e.id)) problems.push(`duplicate id ${e.id}`);
    ids.add(e.id);
    if (!(KINDS as readonly string[]).includes(e.kind)) problems.push(`${e.id}: unknown kind ${JSON.stringify(e.kind)}`);
    if (!Number.isInteger(e.column) || e.column < 0 || e.column >= MAX_COLUMNS) problems.push(`${e.id}: column must be 0-7`);
    if (!Number.isInteger(e.row) || e.row < 0 || e.row >= MAX_ROWS) problems.push(`${e.id}: row must be 0-5`);
    if (e.kind === "container") continue;
    const cs = Math.max(1, e.col_span ?? 1);
    const rs = Math.max(1, e.row_span ?? 1);
    for (let c = e.column; c < Math.min(e.column + cs, MAX_COLUMNS); c++) {
      for (let r = e.row; r < Math.min(e.row + rs, MAX_ROWS); r++) {
        const key = `${c},${r}`;
        const other = cells.get(key);
        if (other && other !== e.id) problems.push(`${e.id} and ${other} share grid cell (${c}, ${r}); move one`);
        cells.set(key, e.id);
      }
    }
  }
  const seen = new Set<string>();
  for (const c of connections) {
    for (const end of [c.src, c.dst]) if (!ids.has(end)) problems.push(`arrow ${c.src}->${c.dst}: no element ${JSON.stringify(end)}`);
    if (c.src === c.dst) problems.push(`arrow ${c.src}->${c.dst} points at itself`);
    const key = `${c.src}>${c.dst}`;
    if (seen.has(key)) problems.push(`arrow ${key} drawn twice`);
    seen.add(key);
  }
  return [...new Set(problems)];
}

export function checkScript(state: DiagramState): { problems: string[]; notes: string[] } {
  const problems: string[] = [];
  const notes: string[] = [];
  const byId = new Map(state.elements.map((e) => [e.id, e]));
  if (!state.steps.length) problems.push("script at least 1 step");
  if (state.steps.length > 8) problems.push(`${state.steps.length} steps; at most 8`);
  state.steps.forEach((s, i) => {
    const n = i + 1;
    if (!s.caption?.trim()) problems.push(`step ${n}: needs a caption`);
    else if (s.caption.length > 90) notes.push(`step ${n}: caption is ${s.caption.length} characters and will be shortened to 90`);
    const named = [...asArray<string>(s.show), ...asArray<string>(s.focus), ...asArray<string>(s.flow)];
    for (const r of asArray<{ id: string }>(s.relabel)) named.push(r.id);
    for (const id of named) if (!byId.has(id)) problems.push(`step ${n}: no element ${JSON.stringify(id)}`);
    for (const m of asArray<{ target: string; item: number; col?: number }>(s.marks)) {
      const t = byId.get(m.target);
      if (!t) problems.push(`step ${n}: mark on unknown element ${JSON.stringify(m.target)}`);
      else if (!ITEM_KINDS.has(t.kind)) problems.push(`step ${n}: ${m.target} is a ${t.kind}; marks go on tokens, stack, grid or bars`);
    }
  });
  const shown = new Set(state.steps.flatMap((s) => asArray<string>(s.show)));
  const never = state.elements.filter((e) => !shown.has(e.id)).map((e) => e.id);
  if (never.length) notes.push(`never shown, so they appear in the first step that uses them (or the first step): ${never.join(", ")}`);
  if (!problems.length && state.elements.length) {
    const plan = planConceptDiagram(toParams(state));
    if (plan && plan.duration > MAX_RUNTIME_S) {
      problems.push(
        `the script runs ${plan.duration.toFixed(0)}s; keep it under ${MAX_RUNTIME_S}s (shorter flow paths, fewer steps)`,
      );
    } else if (plan) {
      notes.push(`runs ${plan.duration.toFixed(1)}s`);
    }
  }
  return { problems: [...new Set(problems)], notes };
}

/** The `concept_diagram` params the Python template validates. */
export function toParams(state: DiagramState): Record<string, unknown> {
  return {
    ...(state.title ? { title: state.title } : {}),
    elements: state.elements,
    connections: state.connections,
    steps: state.steps,
  };
}

/** Apply one tool call to the state and answer it. `done` is set by an accepted finish. */
export function runTool(
  state: DiagramState,
  name: string,
  rawArgs: string,
): { reply: ToolReply; done: boolean } {
  let args: Record<string, unknown>;
  try {
    args = rawArgs.trim() ? (JSON.parse(rawArgs) as Record<string, unknown>) : {};
  } catch {
    return { reply: { ok: false, problems: [`arguments to ${name} were not valid JSON`], notes: [] }, done: false };
  }
  if (name === "draw") {
    state.elements = asArray<DiagramElement>(args.elements);
    state.connections = asArray<DiagramConnection>(args.connections);
    if (typeof args.title === "string") state.title = args.title.slice(0, 60) || null;
    const problems = checkDrawing(state);
    const notes = state.steps.length ? checkScript(state).problems.map((p) => `the current script: ${p}`) : [];
    return { reply: { ok: !problems.length, problems, notes }, done: false };
  }
  if (name === "script") {
    state.steps = asArray<DiagramStep>(args.steps);
    const drawing = checkDrawing(state);
    if (drawing.length) {
      return { reply: { ok: false, problems: ["draw a valid diagram first", ...drawing], notes: [] }, done: false };
    }
    const { problems, notes } = checkScript(state);
    return { reply: { ok: !problems.length, problems, notes }, done: false };
  }
  if (name === "finish") {
    const problems = [...checkDrawing(state), ...(state.steps.length ? checkScript(state).problems : ["script the steps first"])];
    return { reply: { ok: !problems.length, problems, notes: [] }, done: !problems.length };
  }
  return { reply: { ok: false, problems: [`no tool named ${JSON.stringify(name)}; use draw, script or finish`], notes: [] }, done: false };
}
