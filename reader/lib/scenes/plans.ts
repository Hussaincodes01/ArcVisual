/**
 * Scene plans: parameters -> timeline. No React, no DOM, no KaTeX — so the server
 * can compute them too (the landing page fits its demo captions to these
 * timelines at build time) and the client renderers stay pure functions of time.
 *
 * Each plan mirrors its Python template's `estimate_duration` beat for beat.
 */

import {
  Clock,
  MIN_BEAT_S,
  REVEAL_HOLD_S,
  asString,
  asStringList,
  isRecord,
  type ScenePlan,
  type Segment,
} from "./core";

// -- transform_chain ------------------------------------------------------- //

/** Same cap the template applies: beyond six, steps blur together. */
const MAX_STEPS = 6;

export interface TransformChainData {
  title: string | null;
  steps: string[];
  highlights: string[];
  titleSeg: Segment | null;
  appear: Segment[];
  indicate: (Segment | null)[];
}

export function planTransformChain(params: Record<string, unknown>): ScenePlan<TransformChainData> | null {
  let steps = asStringList(params.steps).filter((s) => s.trim());
  if (steps.length < 1) return null;
  if (steps.length > MAX_STEPS) steps = [...steps.slice(0, MAX_STEPS - 1), steps[steps.length - 1]];
  const highlights = asStringList(params.highlight);
  const captions = asStringList(params.captions).slice(0, steps.length);
  const title = asString(params.title) || null;
  const hl = (i: number) => (highlights[i] ?? "").trim();

  const c = new Clock();
  const titleSeg = title ? c.play(MIN_BEAT_S) : null;
  if (captions.length) c.wait(MIN_BEAT_S);
  const appear: Segment[] = [c.play(Math.max(1.2, MIN_BEAT_S))];
  const indicate: (Segment | null)[] = [hl(0) ? c.play(1.0) : null];
  c.wait(REVEAL_HOLD_S);
  for (let i = 1; i < steps.length; i++) {
    if (captions.length && i < captions.length) c.wait(MIN_BEAT_S);
    appear.push(c.play(1.4));
    indicate.push(hl(i) ? c.play(1.0) : null);
    c.wait(REVEAL_HOLD_S);
  }
  return {
    duration: c.t,
    data: { title, steps, highlights: steps.map((_, i) => hl(i)), titleSeg, appear, indicate },
  };
}


// -- plot_reveal ----------------------------------------------------------- //

export interface Series {
  label: string;
  points: [number, number][];
  dashed: boolean;
  annotateAt: number | null;
}

export interface PlotRevealData {
  xLabel: string;
  yLabel: string;
  series: Series[];
  xRange: [number, number];
  yRange: [number, number];
  logY: boolean;
  takeaway: string | null;
  axes: Segment;
  curves: Segment[];
  callouts: (Segment | null)[];
  legend: Segment | null;
  takeawaySeg: Segment | null;
}

function padded(lo: number, hi: number, frac = 0.08): [number, number] {
  if (hi === lo) return [lo - 1, hi + 1];
  const pad = (hi - lo) * frac;
  return [lo - pad, hi + pad];
}

function asRange(v: unknown): [number, number] | null {
  if (!Array.isArray(v) || v.length !== 2) return null;
  const [a, b] = v.map(Number);
  return Number.isFinite(a) && Number.isFinite(b) && b > a ? [a, b] : null;
}

function parseSeries(raw: unknown): Series | null {
  if (!isRecord(raw)) return null;
  const pts = Array.isArray(raw.points)
    ? raw.points
        .map((p) => (Array.isArray(p) && p.length >= 2 ? ([Number(p[0]), Number(p[1])] as [number, number]) : null))
        .filter((p): p is [number, number] => p !== null && Number.isFinite(p[0]) && Number.isFinite(p[1]))
    : [];
  if (pts.length < 2) return null;
  const at = typeof raw.annotate_at === "number" && raw.annotate_at >= 0 && raw.annotate_at < pts.length ? raw.annotate_at : null;
  return { label: asString(raw.label, "series"), points: pts, dashed: raw.dashed === true, annotateAt: at };
}

export function planPlotReveal(params: Record<string, unknown>): ScenePlan<PlotRevealData> | null {
  const series = (Array.isArray(params.series) ? params.series : []).map(parseSeries).filter((s): s is Series => s !== null).slice(0, 4);
  if (!series.length) return null;
  const logY = params.log_y === true && series.every((s) => s.points.every(([, y]) => y > 0));
  const xs = series.flatMap((s) => s.points.map(([x]) => x));
  const ys = series.flatMap((s) => s.points.map(([, y]) => y));
  const xRange = asRange(params.x_range) ?? padded(Math.min(...xs), Math.max(...xs));
  let yRange = asRange(params.y_range) ?? padded(Math.min(...ys), Math.max(...ys));
  if (logY) {
    const lo = Math.min(...ys);
    const hi = Math.max(...ys);
    yRange = [Math.pow(10, Math.floor(Math.log10(lo))), Math.pow(10, Math.ceil(Math.log10(hi)))];
    if (yRange[0] === yRange[1]) yRange = [yRange[0] / 10, yRange[1] * 10];
  }
  const captions = asStringList(params.captions);
  const takeaway = asString(params.takeaway) || null;

  const c = new Clock();
  if (captions.length) c.wait(MIN_BEAT_S);
  const axes = c.play(1.2);
  const curves: Segment[] = [];
  const callouts: (Segment | null)[] = [];
  series.forEach((s, i) => {
    if (captions.length && i + 1 < captions.length) c.wait(MIN_BEAT_S);
    curves.push(c.play(1.6));
    if (s.annotateAt !== null) {
      callouts.push(c.play(MIN_BEAT_S));
      c.wait(REVEAL_HOLD_S);
    } else {
      callouts.push(null);
    }
  });
  const legend = series.length > 1 ? c.play(MIN_BEAT_S) : null;
  let takeawaySeg: Segment | null = null;
  if (takeaway) {
    takeawaySeg = c.play(Math.max(1.0, MIN_BEAT_S));
    c.wait(REVEAL_HOLD_S);
  }
  return {
    duration: c.t,
    data: {
      xLabel: asString(params.x_label),
      yLabel: asString(params.y_label),
      series,
      xRange,
      yRange,
      logY,
      takeaway,
      axes,
      curves,
      callouts,
      legend,
      takeawaySeg,
    },
  };
}


// -- architecture_flow ----------------------------------------------------- //

export interface Node {
  id: string;
  label: string;
  column: number;
  row: number;
  emphasis: boolean;
}

export interface Edge {
  src: string;
  dst: string;
  label: string | null;
  dashed: boolean;
}

export interface ArchitectureFlowData {
  nodes: Node[];
  edges: Edge[];
  /** When each node starts to appear, and over how long. */
  nodeSeg: Record<string, Segment>;
  /** When each edge is drawn (key `src>dst`). */
  edgeSeg: Record<string, Segment>;
  trace: string[];
  pulseIn: Segment | null;
  hops: Segment[];
  pulseOut: Segment | null;
}

function parseNode(raw: unknown): Node | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  return {
    id,
    label: asString(raw.label, id),
    column: Math.max(0, Math.round(Number(raw.column) || 0)),
    row: Math.max(0, Math.round(Number(raw.row) || 0)),
    emphasis: raw.emphasis === true,
  };
}

function parseEdge(raw: unknown, ids: Set<string>): Edge | null {
  if (!isRecord(raw)) return null;
  const src = asString(raw.src);
  const dst = asString(raw.dst);
  if (!ids.has(src) || !ids.has(dst) || src === dst) return null;
  return { src, dst, label: asString(raw.label) || null, dashed: raw.dashed === true };
}

export function planArchitectureFlow(params: Record<string, unknown>): ScenePlan<ArchitectureFlowData> | null {
  const nodes = (Array.isArray(params.nodes) ? params.nodes : [])
    .map(parseNode)
    .filter((n): n is Node => n !== null)
    .slice(0, 12);
  if (nodes.length < 1) return null;
  const ids = new Set(nodes.map((n) => n.id));
  const edges = (Array.isArray(params.edges) ? params.edges : [])
    .map((e) => parseEdge(e, ids))
    .filter((e): e is Edge => e !== null);
  const captions = asStringList(params.captions);

  // Reveal groups: the model's stages, else column by column.
  let groups: string[][] = Array.isArray(params.stages)
    ? params.stages.map((g) => asStringList(g).filter((id) => ids.has(id))).filter((g) => g.length)
    : [];
  if (!groups.length) {
    const byCol = new Map<number, string[]>();
    [...nodes].sort((a, b) => a.column - b.column || a.row - b.row).forEach((n) => {
      byCol.set(n.column, [...(byCol.get(n.column) ?? []), n.id]);
    });
    groups = [...byCol.keys()].sort((a, b) => a - b).map((c) => byCol.get(c)!);
  }
  // A node the stages forgot still has to appear somewhere: with the last group.
  const staged = new Set(groups.flat());
  const missing = nodes.filter((n) => !staged.has(n.id)).map((n) => n.id);
  if (missing.length) groups.push(missing);

  const c = new Clock();
  const nodeSeg: Record<string, Segment> = {};
  const edgeSeg: Record<string, Segment> = {};
  const revealed = new Set<string>();
  let remaining = edges.map((e) => `${e.src}>${e.dst}`);

  groups.forEach((group, gi) => {
    if (gi < captions.length) c.wait(MIN_BEAT_S);
    const fresh = group.filter((id) => !revealed.has(id));
    if (fresh.length) {
      const seg = c.play(Math.max(MIN_BEAT_S, 0.5 + 0.22 * fresh.length));
      // lag_ratio 0.18, as the template's AnimationGroup.
      const each = seg.dur / (1 + 0.18 * (fresh.length - 1));
      fresh.forEach((id, j) => {
        nodeSeg[id] = { start: seg.start + j * each * 0.18, dur: each };
      });
    }
    group.forEach((id) => revealed.add(id));
    const ready = remaining.filter((k) => {
      const [s, d] = k.split(">");
      return revealed.has(s) && revealed.has(d);
    });
    if (ready.length) {
      const seg = c.play(Math.max(MIN_BEAT_S, 0.4 + 0.15 * ready.length));
      ready.forEach((k) => {
        edgeSeg[k] = seg;
      });
      remaining = remaining.filter((k) => !ready.includes(k));
    }
    c.wait(0.9);
  });

  const trace = asStringList(params.trace_path).filter((id) => ids.has(id));
  let pulseIn: Segment | null = null;
  let pulseOut: Segment | null = null;
  const hops: Segment[] = [];
  if (trace.length >= 2) {
    if (captions.length > groups.length) c.wait(MIN_BEAT_S);
    pulseIn = c.play(MIN_BEAT_S);
    for (let i = 1; i < trace.length; i++) hops.push(c.play(0.9));
    pulseOut = c.play(MIN_BEAT_S);
    c.wait(REVEAL_HOLD_S);
  }
  return {
    duration: c.t,
    data: { nodes, edges, nodeSeg, edgeSeg, trace: trace.length >= 2 ? trace : [], pulseIn, hops, pulseOut },
  };
}



// -- concept_diagram ------------------------------------------------------- //
//
// Mirrors arcvisual/templates/concept_diagram.py: `step_segments` and
// `estimate_duration`, phase for phase. The pipeline stores params already
// normalised by that file's validator (every element revealed by some step, values
// on a 0-1 scale, item counts resolved), so this only re-derives what it must and
// guards against hand-written params that skipped the validator.

/** Seconds — same constants as concept_diagram.py. */
const STEP_HOLD_S = 1.2;
const FOCUS_S = 1.0;
const MARK_S = 1.0;
const HOP_S = MIN_BEAT_S;
const MAX_ITEMS = 8;

export type DiagramKind = "block" | "op" | "tokens" | "stack" | "grid" | "bars" | "text" | "container";
export type DiagramTone = "neutral" | "accent" | "input" | "output" | "muted";

const KINDS: readonly string[] = ["block", "op", "tokens", "stack", "grid", "bars", "text", "container"];
const TONES: readonly string[] = ["neutral", "accent", "input", "output", "muted"];
const ITEM_KINDS: readonly string[] = ["tokens", "stack", "grid", "bars"];

export interface DiagramElement {
  id: string;
  kind: DiagramKind;
  label: string;
  column: number;
  row: number;
  colSpan: number;
  rowSpan: number;
  cells: string[];
  /** Items drawn (tokens/stack/bars), or N for an N×N grid. 0 for other kinds. */
  size: number;
  /** 0-1. bars: one per bar. grid: N×N, row-major. Empty = unspecified. */
  values: number[];
  tone: DiagramTone;
  note: string;
}

export interface DiagramConnection {
  key: string;
  src: string;
  dst: string;
  label: string | null;
  dashed: boolean;
}

export interface DiagramMark {
  target: string;
  /** Item index, or grid row. */
  item: number;
  col: number;
}

export interface DiagramStep {
  caption: string;
  /** Whole step, from its caption beat to the end of its hold. */
  span: Segment;
  show: string[];
  reveal: Segment | null;
  /** When each newly shown element fades in (lag 0.18, as the template's LaggedStart). */
  elementSeg: Record<string, Segment>;
  edges: string[];
  connect: Segment | null;
  relabel: { id: string; label: string }[];
  relabelSeg: Segment | null;
  focus: string[];
  focusSeg: Segment | null;
  marks: DiagramMark[];
  marksSeg: Segment | null;
  flow: string[];
  flowIn: Segment | null;
  hops: Segment[];
  flowOut: Segment | null;
}

export interface ConceptDiagramData {
  title: string | null;
  titleSeg: Segment | null;
  elements: DiagramElement[];
  connections: DiagramConnection[];
  steps: DiagramStep[];
}

const intIn = (v: unknown, lo: number, hi: number, dflt: number) => {
  const n = Math.round(Number(v));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
};

function parseElement(raw: unknown): DiagramElement | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  const kind = (KINDS.includes(asString(raw.kind)) ? raw.kind : "block") as DiagramKind;
  const column = intIn(raw.column, 0, 7, 0);
  const row = intIn(raw.row, 0, 5, 0);
  const cells = asStringList(raw.cells).slice(0, MAX_ITEMS);
  const rawValues = Array.isArray(raw.values) ? raw.values.map(Number).filter(Number.isFinite) : [];
  let size = 0;
  if (ITEM_KINDS.includes(kind)) {
    size = intIn(raw.size, 0, MAX_ITEMS, 0) || cells.length || (kind === "bars" ? rawValues.length : 0) || 3;
    size = Math.max(kind === "grid" ? 2 : 1, Math.min(size, MAX_ITEMS));
  }
  let values: number[] = [];
  if ((kind === "bars" || kind === "grid") && rawValues.length) {
    const want = kind === "grid" ? size * size : size;
    const vals = rawValues.slice(0, want).map(Math.abs);
    while (vals.length < want) vals.push(0);
    const top = Math.max(...vals) || 1;
    values = vals.map((v) => v / top);
  }
  return {
    id,
    kind,
    label: asString(raw.label),
    column,
    row,
    colSpan: intIn(raw.col_span, 1, 8 - column, 1),
    rowSpan: intIn(raw.row_span, 1, 6 - row, 1),
    cells: ITEM_KINDS.includes(kind) ? cells : [],
    size,
    values,
    tone: (TONES.includes(asString(raw.tone)) ? raw.tone : "neutral") as DiagramTone,
    note: asString(raw.note),
  };
}

function within(container: DiagramElement, e: DiagramElement): boolean {
  return (
    e.column >= container.column &&
    e.column < container.column + container.colSpan &&
    e.row >= container.row &&
    e.row < container.row + container.rowSpan
  );
}

export function planConceptDiagram(params: Record<string, unknown>): ScenePlan<ConceptDiagramData> | null {
  const elements = (Array.isArray(params.elements) ? params.elements : [])
    .map(parseElement)
    .filter((e): e is DiagramElement => e !== null)
    .slice(0, 16);
  if (elements.length < 1) return null;
  const byId = new Map(elements.map((e) => [e.id, e]));
  const known = (id: string) => byId.has(id);

  const seen = new Set<string>();
  const connections: DiagramConnection[] = [];
  for (const raw of Array.isArray(params.connections) ? params.connections : []) {
    if (!isRecord(raw)) continue;
    const src = asString(raw.src);
    const dst = asString(raw.dst);
    const key = `${src}>${dst}`;
    if (!known(src) || !known(dst) || src === dst || seen.has(key)) continue;
    seen.add(key);
    connections.push({ key, src, dst, label: asString(raw.label) || null, dashed: raw.dashed === true });
  }

  interface RawStep {
    caption: string;
    show: string[];
    focus: string[];
    flow: string[];
    marks: DiagramMark[];
    relabel: { id: string; label: string }[];
  }
  const rawSteps: RawStep[] = (Array.isArray(params.steps) ? params.steps : [])
    .filter(isRecord)
    .slice(0, 8)
    .map((s) => {
      const marks = (Array.isArray(s.marks) ? s.marks : [])
        .filter(isRecord)
        .map((m) => ({ target: asString(m.target), item: intIn(m.item, 0, 99, 0), col: intIn(m.col, 0, 99, 0) }))
        .filter((m) => {
          const t = byId.get(m.target);
          if (!t || !ITEM_KINDS.includes(t.kind) || m.item >= t.size) return false;
          return t.kind !== "grid" || m.col < t.size;
        });
      let focus = asStringList(s.focus).filter(known);
      let flow = asStringList(s.flow).filter(known);
      if (flow.length === 1) {
        focus = [...new Set([...focus, ...flow])];
        flow = [];
      }
      return {
        caption: asString(s.caption),
        show: asStringList(s.show).filter(known),
        focus,
        flow,
        marks,
        relabel: (Array.isArray(s.relabel) ? s.relabel : [])
          .filter(isRecord)
          .map((r) => ({ id: asString(r.id), label: asString(r.label) }))
          .filter((r) => known(r.id)),
      };
    });
  if (!rawSteps.length) rawSteps.push({ caption: "", show: [], focus: [], flow: [], marks: [], relabel: [] });

  // Every element appears no later than the first step that uses it; containers
  // with their first child (concept_diagram._reveal_everything).
  const shownAt = new Map<string, number>();
  rawSteps.forEach((s, i) => {
    for (const id of [...s.show, ...s.focus, ...s.flow, ...s.marks.map((m) => m.target), ...s.relabel.map((r) => r.id)]) {
      if (!shownAt.has(id)) shownAt.set(id, i);
    }
  });
  for (const e of elements) {
    if (e.kind === "container") {
      const inside = elements
        .filter((o) => o.kind !== "container" && shownAt.has(o.id) && within(e, o))
        .map((o) => shownAt.get(o.id)!);
      if (inside.length) shownAt.set(e.id, Math.min(shownAt.get(e.id) ?? rawSteps.length, ...inside));
    }
    if (!shownAt.has(e.id)) shownAt.set(e.id, 0);
  }

  const title = asString(params.title) || null;
  const c = new Clock();
  const titleSeg = title ? c.play(MIN_BEAT_S) : null;
  const shown = new Set<string>();
  let remaining = connections.map((e) => e.key);

  const steps: DiagramStep[] = rawSteps.map((s, i) => {
    const start = c.t;
    c.play(MIN_BEAT_S); // caption
    const show = elements.filter((e) => shownAt.get(e.id) === i).map((e) => e.id);
    const fresh = show.filter((id) => !shown.has(id));
    show.forEach((id) => shown.add(id));
    const elementSeg: Record<string, Segment> = {};
    let reveal: Segment | null = null;
    if (fresh.length) {
      reveal = c.play(Math.max(MIN_BEAT_S, 0.5 + 0.2 * fresh.length));
      const each = reveal.dur / (1 + 0.18 * (fresh.length - 1));
      fresh.forEach((id, j) => {
        elementSeg[id] = { start: reveal!.start + j * each * 0.18, dur: each };
      });
    }
    const edges = remaining.filter((k) => {
      const [a, b] = k.split(">");
      return shown.has(a) && shown.has(b);
    });
    remaining = remaining.filter((k) => !edges.includes(k));
    const connect = edges.length ? c.play(Math.max(MIN_BEAT_S, 0.4 + 0.15 * edges.length)) : null;
    const relabelSeg = s.relabel.length ? c.play(MIN_BEAT_S) : null;
    const focusSeg = s.focus.length ? c.play(FOCUS_S) : null;
    const marksSeg = s.marks.length ? c.play(MARK_S) : null;
    let flowIn: Segment | null = null;
    let flowOut: Segment | null = null;
    const hops: Segment[] = [];
    if (s.flow.length >= 2) {
      flowIn = c.play(HOP_S);
      for (let h = 1; h < s.flow.length; h++) hops.push(c.play(HOP_S));
      flowOut = c.play(HOP_S);
    }
    c.wait(STEP_HOLD_S);
    return {
      caption: s.caption,
      span: { start, dur: c.t - start },
      show,
      reveal,
      elementSeg,
      edges,
      connect,
      relabel: s.relabel,
      relabelSeg,
      focus: s.focus,
      focusSeg,
      marks: s.marks,
      marksSeg,
      flow: s.flow.length >= 2 ? s.flow : [],
      flowIn,
      hops,
      flowOut,
    };
  });
  c.wait(REVEAL_HOLD_S);
  return { duration: c.t, data: { title, titleSeg, elements, connections, steps } };
}
