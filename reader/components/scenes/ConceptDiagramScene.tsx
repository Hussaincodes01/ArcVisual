"use client";

/**
 * `concept_diagram` in the browser: the model's drawing of an idea, animated step by
 * step.
 *
 * Mirrors arcvisual/templates/concept_diagram.py. The model places elements on a grid
 * of column/row indices and scripts steps; layout is computed here, never supplied.
 * Each step reveals what it names, draws the arrows whose ends are both on screen,
 * then — in this order — swaps labels, pulls focus (everything else dims), lights up
 * cells, and sends a pulse along a path. A pure function of `t`, like every scene.
 */

import { useId } from "react";
import {
  PALETTE,
  VIEW_H,
  VIEW_W,
  clamp,
  ease,
  progress,
  wrapLabel,
  type SceneProps,
  type Segment,
} from "../../lib/scenes/core";
import type { ConceptDiagramData, DiagramElement, DiagramStep } from "../../lib/scenes/plans";
import { plainText } from "../../lib/tex";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const cx = (r: Rect) => r.x + r.w / 2;
const cy = (r: Rect) => r.y + r.h / 2;

const MINT = "#dcf2e3";
const MINT_INK = "#2f8a55";

/** Fill and stroke per tone, from the page palette so a scene reads as part of it. */
const TONE: Record<string, { fill: string; stroke: string; item: string }> = {
  neutral: { fill: PALETTE.paper, stroke: PALETTE.ink, item: PALETTE.ink },
  accent: { fill: PALETTE.lavender, stroke: PALETTE.ink, item: PALETTE.violet },
  input: { fill: PALETTE.mustardSoft, stroke: PALETTE.ink, item: PALETTE.amber },
  output: { fill: MINT, stroke: PALETTE.ink, item: MINT_INK },
  muted: { fill: PALETTE.paper, stroke: PALETTE.muted, item: PALETTE.muted },
};

/** Lexend's medium glyphs average ~0.6em wide. */
const GLYPH = 0.6;

function fitText(text: string, maxW: number, maxH: number, sizes: number[], maxLines = 2) {
  for (const size of sizes) {
    const perLine = Math.max(3, Math.floor(maxW / (size * GLYPH)));
    // Wrapped without a line cap first: wrapLabel elides to fit `maxLines`, and an
    // elided candidate would otherwise pass and stop the search at a size where the
    // whole text did not fit.
    const lines = wrapLabel(text, perLine, 99);
    if (lines.length <= maxLines && lines.length * size * 1.18 <= maxH && lines.every((l) => l.length <= perLine)) {
      return { size, lines };
    }
  }
  const size = sizes[sizes.length - 1];
  return { size, lines: wrapLabel(text, Math.max(3, Math.floor(maxW / (size * GLYPH))), maxLines) };
}

// -- layout ------------------------------------------------------------------ //

interface Geometry {
  /** The drawn shape's bounds: what arrows attach to. */
  box: Rect;
  /** Item rectangles (tokens, vectors, grid cells, bars), for marks. */
  items: Rect[];
  /** Where the element's own label goes. */
  labelAt: Rect;
  /** Grid header band, if any. */
  extra?: { colHeads: Rect[]; rowHeads: Rect[] };
}

function cellArea(d: ConceptDiagramData, top: number): (e: DiagramElement) => Rect {
  const c0 = Math.min(...d.elements.map((e) => e.column));
  const r0 = Math.min(...d.elements.map((e) => e.row));
  const cols = Math.max(...d.elements.map((e) => e.column + e.colSpan)) - c0;
  const rows = Math.max(...d.elements.map((e) => e.row + e.rowSpan)) - r0;
  const padX = 22;
  const bottom = 18;
  const cw = (VIEW_W - 2 * padX) / cols;
  const ch = (VIEW_H - top - bottom) / rows;
  return (e) => ({ x: padX + (e.column - c0) * cw, y: top + (e.row - r0) * ch, w: e.colSpan * cw, h: e.rowSpan * ch });
}

function geometry(e: DiagramElement, cell: Rect): Geometry {
  const g = Math.min(cell.w, cell.h) * 0.08;
  const area: Rect = { x: cell.x + g, y: cell.y + g, w: cell.w - 2 * g, h: cell.h - 2 * g };
  const labelH = 26;

  switch (e.kind) {
    case "container": {
      const box = { x: cell.x + 4, y: cell.y + 4, w: cell.w - 8, h: cell.h - 8 };
      return { box, items: [], labelAt: { x: box.x + 14, y: box.y - 13, w: box.w * 0.7, h: 26 } };
    }
    case "op": {
      const short = e.label.length <= 3;
      const h = Math.min(area.h * 0.55, short ? 92 : 60, area.w * 0.8);
      const w = short ? h : Math.min(area.w * 0.95, Math.max(h * 1.6, e.label.length * 13 + 34));
      const box = { x: cx(area) - w / 2, y: cy(area) - h / 2, w, h };
      return { box, items: [], labelAt: box };
    }
    case "text": {
      return { box: area, items: [], labelAt: area };
    }
    case "tokens":
    case "stack": {
      const n = Math.max(1, e.size);
      const gap = e.kind === "tokens" ? 7 : 6;
      const bodyH = area.h - labelH;
      const iw = Math.min((area.w - gap * (n - 1)) / n, e.kind === "tokens" ? 120 : 46);
      const ih = Math.min(bodyH * (e.kind === "tokens" ? 0.5 : 0.82), e.kind === "tokens" ? 52 : 132);
      const totalW = n * iw + (n - 1) * gap;
      const x0 = cx(area) - totalW / 2;
      const y0 = area.y + (bodyH - ih) / 2;
      const items = Array.from({ length: n }, (_, i) => ({ x: x0 + i * (iw + gap), y: y0, w: iw, h: ih }));
      const box = { x: x0, y: y0, w: totalW, h: ih };
      return { box, items, labelAt: { x: cell.x, y: y0 + ih + 4, w: cell.w, h: labelH } };
    }
    case "grid": {
      const n = Math.max(2, e.size);
      const heads = e.cells.length > 0;
      const head = heads ? 24 : 0;
      const side = Math.max(40, Math.min(area.w - head, area.h - labelH - head));
      const step = side / n;
      const gx = cx(area) - (side + head) / 2 + head;
      const gy = area.y + (area.h - labelH - side - head) / 2 + head;
      const items: Rect[] = [];
      for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) items.push({ x: gx + c * step, y: gy + r * step, w: step, h: step });
      const colHeads = heads ? Array.from({ length: n }, (_, c) => ({ x: gx + c * step, y: gy - head, w: step, h: head })) : [];
      const rowHeads = heads ? Array.from({ length: n }, (_, r) => ({ x: gx - head - 4, y: gy + r * step, w: head, h: step })) : [];
      const box = { x: gx, y: gy, w: side, h: side };
      return { box, items, labelAt: { x: cell.x, y: gy + side + 4, w: cell.w, h: labelH }, extra: { colHeads, rowHeads } };
    }
    case "bars": {
      const n = Math.max(1, e.size);
      const names = e.cells.length ? 22 : 0;
      const bodyH = area.h - labelH - names;
      const bw = Math.min((area.w / n) * 0.62, 42);
      const gap = Math.min((area.w - n * bw) / Math.max(1, n - 1), bw * 0.6);
      const totalW = n * bw + (n - 1) * gap;
      const x0 = cx(area) - totalW / 2;
      const base = area.y + bodyH;
      const maxH = bodyH * 0.92;
      const vals = e.values.length ? e.values : Array(n).fill(0.6);
      const items = Array.from({ length: n }, (_, i) => {
        const h = Math.max(3, maxH * (vals[i] ?? 0));
        return { x: x0 + i * (bw + gap), y: base - h, w: bw, h };
      });
      const box = { x: x0, y: base - maxH, w: totalW, h: maxH };
      return { box, items, labelAt: { x: cell.x, y: base + names + 2, w: cell.w, h: labelH } };
    }
    default: {
      const h = Math.min(area.h, Math.max(e.note ? 86 : 66, area.w * 0.56), 150);
      const box = { x: area.x, y: cy(area) - h / 2, w: area.w, h };
      return { box, items: [], labelAt: box };
    }
  }
}

/** Where the line from a box's centre towards (tx, ty) leaves the box. */
function exitPoint(b: Rect, tx: number, ty: number, buff: number): [number, number] {
  const dx = tx - cx(b);
  const dy = ty - cy(b);
  if (dx === 0 && dy === 0) return [cx(b), cy(b)];
  const sx = dx === 0 ? Infinity : (b.w / 2 + buff) / Math.abs(dx);
  const sy = dy === 0 ? Infinity : (b.h / 2 + buff) / Math.abs(dy);
  const s = Math.min(sx, sy);
  return [cx(b) + dx * s, cy(b) + dy * s];
}

/** Whether the segment p→q passes through rectangle r (shrunk slightly). */
function crosses(p: [number, number], q: [number, number], r: Rect): boolean {
  const inset = 6;
  const x1 = r.x + inset;
  const x2 = r.x + r.w - inset;
  const y1 = r.y + inset;
  const y2 = r.y + r.h - inset;
  for (let i = 1; i < 20; i++) {
    const t = i / 20;
    const x = p[0] + (q[0] - p[0]) * t;
    const y = p[1] + (q[1] - p[1]) * t;
    if (x > x1 && x < x2 && y > y1 && y < y2) return true;
  }
  return false;
}

interface Route {
  d: string;
  /** Point at fraction s along the route. */
  at: (s: number) => [number, number];
  mid: [number, number];
}

/** A straight arrow, or a gentle arc when the straight one would cut through a shape. */
function route(a: Rect, b: Rect, obstacles: Rect[]): Route {
  const p = exitPoint(a, cx(b), cy(b), 7);
  const q = exitPoint(b, cx(a), cy(a), 11);
  if (!obstacles.some((o) => crosses(p, q, o))) {
    const at = (s: number): [number, number] => [p[0] + (q[0] - p[0]) * s, p[1] + (q[1] - p[1]) * s];
    return { d: `M${p[0]} ${p[1]} L${q[0]} ${q[1]}`, at, mid: at(0.5) };
  }
  const mx = (cx(a) + cx(b)) / 2;
  const my = (cy(a) + cy(b)) / 2;
  const len = Math.hypot(cx(b) - cx(a), cy(b) - cy(a)) || 1;
  const nx = -(cy(b) - cy(a)) / len;
  const ny = (cx(b) - cx(a)) / len;
  // Bow to whichever side is clear; above for a left-to-right row.
  const bow = Math.min(120, len * 0.35);
  const sides = [-1, 1].map((sgn) => [mx + nx * bow * sgn, my + ny * bow * sgn] as [number, number]);
  const ctrl = sides.find((c) => !obstacles.some((o) => crosses(c, c, o))) ?? sides[0];
  const p2 = exitPoint(a, ctrl[0], ctrl[1], 7);
  const q2 = exitPoint(b, ctrl[0], ctrl[1], 11);
  const at = (s: number): [number, number] => {
    const u = 1 - s;
    return [u * u * p2[0] + 2 * u * s * ctrl[0] + s * s * q2[0], u * u * p2[1] + 2 * u * s * ctrl[1] + s * s * q2[1]];
  };
  return { d: `M${p2[0]} ${p2[1]} Q${ctrl[0]} ${ctrl[1]} ${q2[0]} ${q2[1]}`, at, mid: at(0.5) };
}

// -- time helpers ------------------------------------------------------------ //

/** The step whose span contains t (the last one once the scene has ended). */
function currentStep(steps: DiagramStep[], t: number): DiagramStep | null {
  let cur: DiagramStep | null = null;
  for (const s of steps) if (t >= s.span.start) cur = s;
  return cur;
}

/** 0→1 as `seg` begins, holding at 1 until the step ends, then easing back out. */
function held(t: number, seg: Segment | null, step: DiagramStep): number {
  if (!seg || t < seg.start) return 0;
  const end = step.span.start + step.span.dur;
  const fadeOut = clamp((t - (end - 0.3)) / 0.3);
  return ease.outCubic(progress(t, { start: seg.start, dur: Math.min(0.35, seg.dur) })) * (1 - fadeOut);
}

export default function ConceptDiagramScene({ plan, t, macros }: SceneProps<ConceptDiagramData>) {
  const show = (text: string) => plainText(text, macros);
  const d = plan.data;
  const markerId = `cd-arrow-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const top = d.title ? 62 : 22;
  const area = cellArea(d, top);
  const geo: Record<string, Geometry> = {};
  for (const e of d.elements) geo[e.id] = geometry(e, area(e));

  // When each element starts appearing, and each edge is drawn.
  const elementSeg: Record<string, Segment> = {};
  const edgeSeg: Record<string, Segment> = {};
  for (const s of d.steps) {
    Object.assign(elementSeg, s.elementSeg);
    for (const k of s.edges) if (s.connect) edgeSeg[k] = s.connect;
  }

  const step = currentStep(d.steps, t);
  // Focus dims everything else; marks and the flow pulse light things up.
  const focusQ = step ? held(t, step.focusSeg, step) : 0;
  const focusPulse = step?.focusSeg ? ease.pulse(progress(t, step.focusSeg)) : 0;
  const focused = new Set(step?.focus ?? []);
  const marksQ = step ? held(t, step.marksSeg, step) : 0;
  const marked = new Map<string, Set<number>>();
  if (step && marksQ > 0) {
    for (const m of step.marks) {
      const e = d.elements.find((x) => x.id === m.target);
      if (!e) continue;
      const idx = e.kind === "grid" ? m.item * e.size + m.col : m.item;
      if (!marked.has(m.target)) marked.set(m.target, new Set());
      marked.get(m.target)!.add(idx);
    }
  }

  // Labels as of time t, crossfading through any relabel.
  const labelOf = (e: DiagramElement): { from: string; to: string; p: number } => {
    let label = e.label;
    for (const s of d.steps) {
      for (const r of s.relabel) {
        if (r.id !== e.id || !s.relabelSeg || t < s.relabelSeg.start) continue;
        const p = ease.inOutCubic(progress(t, s.relabelSeg));
        if (p < 1) return { from: label, to: r.label, p };
        label = r.label;
      }
    }
    return { from: label, to: label, p: 1 };
  };

  // Arrows avoid the shapes that are not their own ends.
  const solids = d.elements.filter((e) => e.kind !== "container" && e.kind !== "text");
  const routes: Record<string, Route> = {};
  for (const c of d.connections) {
    const obstacles = solids.filter((e) => e.id !== c.src && e.id !== c.dst).map((e) => geo[e.id].box);
    routes[c.key] = route(geo[c.src].box, geo[c.dst].box, obstacles);
  }

  // The flow pulse: along the arrow between consecutive stops when there is one.
  let pulse: { x: number; y: number; r: number; o: number } | null = null;
  let litNode: string | null = null;
  if (step && step.flow.length >= 2 && step.flowIn && step.flowOut && t >= step.flowIn.start && t <= step.flowOut.start + step.flowOut.dur) {
    const first = geo[step.flow[0]].box;
    let x = cx(first);
    let y = cy(first);
    litNode = step.flow[0];
    step.hops.forEach((seg, i) => {
      if (t < seg.start) return;
      const a = step.flow[i];
      const b = step.flow[i + 1];
      const p = ease.inOutSine(progress(t, seg));
      const fwd = routes[`${a}>${b}`];
      const back = routes[`${b}>${a}`];
      if (fwd) [x, y] = fwd.at(p);
      else if (back) [x, y] = back.at(1 - p);
      else {
        const A = geo[a].box;
        const B = geo[b].box;
        x = cx(A) + (cx(B) - cx(A)) * p;
        y = cy(A) + (cy(B) - cy(A)) * p;
      }
      litNode = p > 0.5 ? b : a;
    });
    const pin = progress(t, step.flowIn);
    const pout = progress(t, step.flowOut);
    pulse = { x, y, r: 11 * (0.4 + 0.6 * pin) * (1 + 0.6 * pout), o: pin * (1 - pout) };
  }

  const dimFor = (id: string) => (focusQ > 0 && !focused.has(id) ? 1 - 0.62 * focusQ : 1);

  const renderLabel = (text: string, r: Rect, sizes: number[], weight: number, fill: string, maxLines = 2, opacity = 1) => {
    const { size, lines } = fitText(text, r.w * 0.9, r.h * 0.9, sizes, maxLines);
    return lines.map((line, i) => (
      <text
        key={`${text}-${i}`}
        x={cx(r)}
        y={cy(r) + (i - (lines.length - 1) / 2) * size * 1.18 + size * 0.35}
        textAnchor="middle"
        fontSize={size}
        fontWeight={weight}
        fill={fill}
        opacity={opacity}
      >
        {line}
      </text>
    ));
  };

  const elementLabel = (e: DiagramElement, r: Rect, sizes: number[], weight: number, fill: string, maxLines = 2) => {
    const { from, to, p } = labelOf(e);
    if (p >= 1) return renderLabel(show(to), r, sizes, weight, fill, maxLines);
    return (
      <>
        <g transform={`translate(0 ${-8 * p})`}>{renderLabel(show(from), r, sizes, weight, fill, maxLines, 1 - p)}</g>
        <g transform={`translate(0 ${8 * (1 - p)})`}>{renderLabel(show(to), r, sizes, weight, PALETTE.coral, maxLines, p)}</g>
      </>
    );
  };

  const drawElement = (e: DiagramElement) => {
    const q = ease.outCubic(progress(t, elementSeg[e.id]));
    if (q <= 0) return null;
    const G = geo[e.id];
    const tone = TONE[e.tone] ?? TONE.neutral;
    const isFocused = focusQ > 0 && focused.has(e.id);
    const lit = litNode === e.id;
    const stroke = isFocused || lit ? PALETTE.coral : tone.stroke;
    const strokeW = isFocused || lit ? 4 : 2.5;
    const scale = (0.92 + 0.08 * q) * (isFocused ? 1 + 0.05 * focusPulse : 1);
    const ox = cx(G.box);
    const oy = cy(G.box);
    const marks = marked.get(e.id);
    // Items arrive one after another within the element's own reveal.
    const itemQ = (i: number, n: number) => ease.outCubic(clamp(q * 1.6 - (0.6 * i) / Math.max(1, n)));

    let body: React.ReactNode = null;
    switch (e.kind) {
      case "container": {
        const b = G.box;
        const { size, lines } = fitText(show(labelOf(e).to), b.w * 0.7, 24, [17, 15, 14], 1);
        const tabW = Math.min(b.w * 0.8, (lines[0]?.length ?? 4) * size * GLYPH + 26);
        body = (
          <>
            <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={18} fill={PALETTE.paper} fillOpacity={0.4} stroke={PALETTE.muted} strokeWidth={2} strokeDasharray="10 8" />
            <rect x={b.x + 14} y={b.y - 13} width={tabW} height={26} rx={13} fill={PALETTE.paper} stroke={PALETTE.muted} strokeWidth={1.5} />
            <text x={b.x + 14 + tabW / 2} y={b.y + size * 0.35} textAnchor="middle" fontSize={size} fontWeight={600} fill={PALETTE.inkSoft}>
              {lines[0]}
            </text>
          </>
        );
        break;
      }
      case "op": {
        const b = G.box;
        body = (
          <>
            <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={b.h / 2} fill={tone.fill} stroke={stroke} strokeWidth={strokeW} />
            {elementLabel(e, b, [30, 26, 22, 19, 17, 15], 600, PALETTE.ink, 1)}
          </>
        );
        break;
      }
      case "text": {
        const b = G.box;
        const noteH = e.note ? 24 : 0;
        body = (
          <>
            {elementLabel(e, { ...b, h: b.h - noteH }, [30, 26, 23, 20, 18, 16], 500, PALETTE.ink, 3)}
            {e.note ? renderLabel(show(e.note), { x: b.x, y: b.y + b.h - noteH - 4, w: b.w, h: noteH }, [16, 14], 400, PALETTE.muted, 1) : null}
          </>
        );
        break;
      }
      case "tokens":
      case "stack": {
        body = (
          <>
            {G.items.map((r, i) => {
              const iq = itemQ(i, G.items.length);
              const hot = marks?.has(i);
              return (
                <g key={i} opacity={iq} transform={`translate(0 ${(1 - iq) * 10})`}>
                  <rect
                    x={r.x}
                    y={r.y}
                    width={r.w}
                    height={r.h}
                    rx={e.kind === "tokens" ? r.h / 2.6 : 8}
                    fill={hot ? PALETTE.coral : tone.fill}
                    fillOpacity={hot ? 0.25 + 0.6 * marksQ : 1}
                    stroke={hot ? PALETTE.coral : stroke}
                    strokeWidth={hot ? 3.5 : strokeW * 0.8}
                  />
                  {e.kind === "stack"
                    ? [1, 2, 3].map((k) => (
                        <line key={k} x1={r.x + 6} x2={r.x + r.w - 6} y1={r.y + (r.h * k) / 4} y2={r.y + (r.h * k) / 4} stroke={tone.item} strokeOpacity={0.25} strokeWidth={1.5} />
                      ))
                    : null}
                  {e.cells[i] ? (
                    <g>
                      {e.kind === "stack" ? (
                        <rect x={r.x + 2} y={cy(r) - 13} width={r.w - 4} height={26} rx={6} fill={PALETTE.paper} fillOpacity={0.85} />
                      ) : null}
                      {renderLabel(show(e.cells[i]), r, [20, 18, 16, 14, 13], 500, PALETTE.ink, 1)}
                    </g>
                  ) : null}
                </g>
              );
            })}
            {renderLabel(show(labelOf(e).to), G.labelAt, [18, 16, 15, 14, 13, 12], 600, PALETTE.inkSoft, 1)}
          </>
        );
        break;
      }
      case "grid": {
        const n = e.size;
        body = (
          <>
            {G.items.map((r, i) => {
              const row = Math.floor(i / n);
              const iq = itemQ(row, n);
              const v = e.values[i] ?? 0;
              const hot = marks?.has(i);
              return (
                <rect
                  key={i}
                  x={r.x + 1.5}
                  y={r.y + 1.5}
                  width={r.w - 3}
                  height={r.h - 3}
                  rx={4}
                  fill={hot ? PALETTE.coral : PALETTE.violet}
                  fillOpacity={(hot ? 0.3 + 0.6 * marksQ : 0.06 + 0.72 * v) * iq}
                  stroke={hot ? PALETTE.coral : PALETTE.ink}
                  strokeOpacity={iq}
                  strokeWidth={hot ? 3.5 : 1.4}
                />
              );
            })}
            {G.extra?.colHeads.map((r, c) => (e.cells[c] ? <g key={`c${c}`}>{renderLabel(show(e.cells[c]), r, [15, 13, 12], 500, PALETTE.inkSoft, 1)}</g> : null))}
            {G.extra?.rowHeads.map((r, c) => (e.cells[c] ? <g key={`r${c}`}>{renderLabel(show(e.cells[c]), r, [15, 13, 12], 500, PALETTE.inkSoft, 1)}</g> : null))}
            <rect x={G.box.x} y={G.box.y} width={G.box.w} height={G.box.h} fill="none" stroke={stroke} strokeWidth={isFocused || lit ? 4 : 0} rx={4} />
            {renderLabel(show(labelOf(e).to), G.labelAt, [18, 16, 15, 14, 13, 12], 600, PALETTE.inkSoft, 1)}
          </>
        );
        break;
      }
      case "bars": {
        const base = G.box.y + G.box.h;
        body = (
          <>
            <line x1={G.box.x - 6} x2={G.box.x + G.box.w + 6} y1={base} y2={base} stroke={PALETTE.ink} strokeWidth={2} />
            {G.items.map((r, i) => {
              const iq = itemQ(i, G.items.length);
              const hot = marks?.has(i);
              const h = r.h * iq;
              return (
                <g key={i}>
                  <rect
                    x={r.x}
                    y={base - h}
                    width={r.w}
                    height={h}
                    rx={4}
                    fill={hot ? PALETTE.coral : tone.item}
                    fillOpacity={hot ? 0.5 + 0.4 * marksQ : 0.55}
                    stroke={hot ? PALETTE.coral : PALETTE.ink}
                    strokeWidth={hot ? 3 : 1.5}
                  />
                  {e.cells[i]
                    ? renderLabel(show(e.cells[i]), { x: r.x - 14, y: base + 2, w: r.w + 28, h: 22 }, [16, 14, 13], 500, PALETTE.inkSoft, 1)
                    : null}
                </g>
              );
            })}
            {renderLabel(show(labelOf(e).to), G.labelAt, [18, 16, 15, 14, 13, 12], 600, PALETTE.inkSoft, 1)}
          </>
        );
        break;
      }
      default: {
        const b = G.box;
        const noteH = e.note ? Math.min(34, b.h * 0.36) : 0;
        body = (
          <>
            <rect x={b.x} y={b.y} width={b.w} height={b.h} rx={16} fill={tone.fill} stroke={stroke} strokeWidth={strokeW} />
            {elementLabel(e, { x: b.x + 6, y: b.y + 4, w: b.w - 12, h: b.h - noteH - 8 }, [28, 25, 22, 20, 18, 16, 15], 600, PALETTE.ink, 2)}
            {e.note ? renderLabel(show(e.note), { x: b.x + 2, y: b.y + b.h - noteH - 4, w: b.w - 4, h: noteH }, [16, 14, 13, 12], 400, PALETTE.inkSoft, 2) : null}
          </>
        );
      }
    }

    return (
      <g
        key={e.id}
        opacity={q * dimFor(e.id)}
        transform={`translate(${ox} ${oy}) scale(${scale}) translate(${-ox} ${-oy})`}
      >
        {body}
      </g>
    );
  };

  const containers = d.elements.filter((e) => e.kind === "container");
  const others = d.elements.filter((e) => e.kind !== "container");

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      className="absolute inset-0 h-full w-full"
      role="img"
      aria-label={`Diagram: ${d.elements.map((e) => show(e.label)).join(", ")}`}
    >
      <defs>
        <marker id={markerId} viewBox="0 0 12 12" refX="10" refY="6" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
          <path d="M1 1 L11 6 L1 11 z" fill={PALETTE.ink} />
        </marker>
      </defs>

      {d.title ? (
        <text
          x={VIEW_W / 2}
          y={38}
          textAnchor="middle"
          fontSize={26}
          fontWeight={600}
          fill={PALETTE.ink}
          opacity={d.titleSeg ? progress(t, d.titleSeg) : 1}
        >
          {show(d.title)}
        </text>
      ) : null}

      {containers.map(drawElement)}

      {d.connections.map((c) => {
        const p = ease.inOutCubic(progress(t, edgeSeg[c.key]));
        if (p <= 0) return null;
        const r = routes[c.key];
        const dim = Math.min(dimFor(c.src), dimFor(c.dst));
        const label = c.label ? show(c.label) : null;
        const lw = label ? Math.min(220, label.length * 10.5 + 22) : 0;
        // The pill sits just above the arrow's midpoint, unless that would cover
        // one of the shapes it connects (a short arrow between big shapes): then it
        // rises above them.
        let ly = r.mid[1] - 32;
        if (label) {
          const pill = { x: r.mid[0] - lw / 2, y: ly, w: lw, h: 26 };
          for (const id of [c.src, c.dst]) {
            const b = geo[id].box;
            const overlaps = pill.x < b.x + b.w && pill.x + pill.w > b.x && pill.y < b.y + b.h && pill.y + pill.h > b.y;
            if (overlaps) ly = Math.min(ly, b.y - 30);
          }
          ly = Math.max(top - 6, ly);
        }
        return (
          <g key={c.key} opacity={dim}>
            <path
              d={r.d}
              fill="none"
              stroke={PALETTE.ink}
              strokeWidth={2.5}
              strokeLinecap="round"
              pathLength={1}
              strokeDasharray={c.dashed ? "0.025 0.02" : "1 1"}
              strokeDashoffset={c.dashed ? 0 : 1 - p}
              opacity={c.dashed ? p : 1}
              markerEnd={p > 0.92 ? `url(#${markerId})` : undefined}
            />
            {label ? (
              <g opacity={clamp((p - 0.5) * 2)}>
                <rect x={r.mid[0] - lw / 2} y={ly} width={lw} height={26} rx={13} fill={PALETTE.mustardSoft} stroke={PALETTE.ink} strokeWidth={1.3} />
                <text x={r.mid[0]} y={ly + 18.5} textAnchor="middle" fontSize={16} fill={PALETTE.ink}>
                  {label}
                </text>
              </g>
            ) : null}
          </g>
        );
      })}

      {others.map(drawElement)}

      {pulse && pulse.o > 0 ? (
        <g opacity={pulse.o}>
          <circle cx={pulse.x} cy={pulse.y} r={pulse.r * 2.2} fill={PALETTE.coral} opacity={0.18} />
          <circle cx={pulse.x} cy={pulse.y} r={pulse.r} fill={PALETTE.coral} stroke={PALETTE.ink} strokeWidth={2} />
        </g>
      ) : null}
    </svg>
  );
}
