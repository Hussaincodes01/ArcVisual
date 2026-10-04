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

