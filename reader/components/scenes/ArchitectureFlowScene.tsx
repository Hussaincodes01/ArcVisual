"use client";

/**
 * `architecture_flow` in the browser: a model diagram with data moving through it.
 *
 * Mirrors arcvisual/templates/architecture_flow.py. The model gives each node a
 * column and a row *index*; layout is computed here, never supplied — grid indices
 * cannot express off-frame drift or overlapping boxes. Blocks appear in reveal
 * order, an edge is drawn only once both its ends are on screen, and a pulse walks
 * the data path at the end: the sequence a static figure cannot show.
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
} from "../../lib/scenes/core";
import type { ArchitectureFlowData, Node } from "../../lib/scenes/plans";
import { plainText } from "../../lib/tex";

interface Box {
  cx: number;
  cy: number;
  w: number;
  h: number;
}

function layout(nodes: Node[]): Record<string, Box> {
  const cols = [...new Set(nodes.map((n) => n.column))].sort((a, b) => a - b);
  const rows = [...new Set(nodes.map((n) => n.row))].sort((a, b) => a - b);
  const padX = 34;
  const padY = 40;
  const colGap = (VIEW_W - 2 * padX) / cols.length;
  const rowGap = (VIEW_H - 2 * padY) / rows.length;
  const w = colGap * 0.78;
  const h = Math.min(rowGap * 0.66, Math.max(w * 0.56, 84), 170);
  const out: Record<string, Box> = {};
  for (const n of nodes) {
    out[n.id] = {
      cx: padX + colGap * (cols.indexOf(n.column) + 0.5),
      cy: padY + rowGap * (rows.indexOf(n.row) + 0.5),
      w,
      h,
    };
  }
  return out;
}

/** Where the line from a box's centre towards (tx, ty) leaves the box. */
function exitPoint(b: Box, tx: number, ty: number, buff: number): [number, number] {
  const dx = tx - b.cx;
  const dy = ty - b.cy;
  if (dx === 0 && dy === 0) return [b.cx, b.cy];
  const sx = dx === 0 ? Infinity : (b.w / 2 + buff) / Math.abs(dx);
  const sy = dy === 0 ? Infinity : (b.h / 2 + buff) / Math.abs(dy);
  const s = Math.min(sx, sy);
  return [b.cx + dx * s, b.cy + dy * s];
}

export default function ArchitectureFlowScene({ plan, t, macros }: SceneProps<ArchitectureFlowData>) {
  // SVG cannot hold KaTeX, so labels are converted to readable Unicode math:
  // a label typed as `∇_φ L` becomes ∇ᵩ L, never source.
  const show = (text: string) => plainText(text, macros);
  const d = plan.data;
  const markerId = `arrow-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const boxes = layout(d.nodes);

  // Where the pulse is, and which node it is passing through.
  let pulse: { x: number; y: number; r: number; o: number } | null = null;
  let litNode: string | null = null;
  if (d.trace.length >= 2 && d.pulseIn && d.pulseOut && t >= d.pulseIn.start && t <= d.pulseOut.start + d.pulseOut.dur) {
    const first = boxes[d.trace[0]];
    let x = first.cx;
    let y = first.cy;
    litNode = d.trace[0];
    d.hops.forEach((seg, i) => {
      if (t >= seg.start) {
        const a = boxes[d.trace[i]];
        const b = boxes[d.trace[i + 1]];
        const p = ease.inOutSine(progress(t, seg));
        x = a.cx + (b.cx - a.cx) * p;
        y = a.cy + (b.cy - a.cy) * p;
        litNode = p > 0.5 ? d.trace[i + 1] : d.trace[i];
      }
    });
    const pin = progress(t, d.pulseIn);
    const pout = progress(t, d.pulseOut);
    pulse = { x, y, r: 13 * (0.4 + 0.6 * pin) * (1 + 0.6 * pout), o: pin * (1 - pout) };
  }

  // Lexend's medium glyphs average ~0.62em; sizing at a narrower guess let labels
  // spill past their boxes on six-across diagrams.
  const fontFor = (b: Box, label: string) => {
    for (const size of [34, 30, 27, 24, 21, 19, 17, 15]) {
      const perLine = Math.max(4, Math.floor((b.w * 0.84) / (size * 0.62)));
      const lines = wrapLabel(label, perLine, 2);
      if (lines.length * size * 1.2 <= b.h * 0.82 && lines.every((l) => l.length <= perLine)) return { size, lines };
    }
    const size = 14;
    return { size, lines: wrapLabel(label, Math.max(4, Math.floor((b.w * 0.84) / (size * 0.62))), 2) };
  };

  return (
    <svg
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      className="absolute inset-0 h-full w-full"
      role="img"
      aria-label={`Diagram: ${d.nodes.map((n) => show(n.label)).join(", ")}`}
    >
      <defs>
        <marker id={markerId} viewBox="0 0 12 12" refX="10" refY="6" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
          <path d="M1 1 L11 6 L1 11 z" fill={PALETTE.ink} />
        </marker>
      </defs>

      {d.edges.map((e) => {
        const key = `${e.src}>${e.dst}`;
        const seg = d.edgeSeg[key];
        const p = ease.inOutCubic(progress(t, seg));
        if (p <= 0) return null;
        const a = boxes[e.src];
        const b = boxes[e.dst];
        const [x1, y1] = exitPoint(a, b.cx, b.cy, 8);
        const [x2, y2] = exitPoint(b, a.cx, a.cy, 12);
        const mx = (x1 + x2) / 2;
        const my = (y1 + y2) / 2;
        const shown = e.label ? show(e.label) : null;
        const label = shown && shown.length > 24 ? `${shown.slice(0, 23)}…` : shown;
        return (
          <g key={key}>
            <line
              x1={x1}
              y1={y1}
              x2={x1 + (x2 - x1) * (e.dashed ? 1 : p)}
              y2={y1 + (y2 - y1) * (e.dashed ? 1 : p)}
              stroke={PALETTE.ink}
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeDasharray={e.dashed ? "9 8" : undefined}
              opacity={e.dashed ? p : 1}
              markerEnd={p > 0.92 ? `url(#${markerId})` : undefined}
            />
            {label ? (
              <g opacity={clamp((p - 0.5) * 2)}>
                <rect
                  x={mx - (label.length * 11 + 24) / 2}
                  y={my - 38}
                  width={label.length * 11 + 24}
                  height={32}
                  rx={16}
                  fill={PALETTE.mustardSoft}
                  stroke={PALETTE.ink}
                  strokeWidth={1.5}
                />
                <text x={mx} y={my - 15} textAnchor="middle" fontSize={19} fill={PALETTE.ink}>
                  {label}
                </text>
              </g>
            ) : null}
          </g>
        );
      })}

      {d.nodes.map((n) => {
        const b = boxes[n.id];
        const q = ease.outCubic(progress(t, d.nodeSeg[n.id]));
        if (q <= 0) return null;
        const { size, lines } = fontFor(b, show(n.label));
        const lit = litNode === n.id;
        return (
          <g key={n.id} opacity={q} transform={`translate(${(1 - q) * -18} 0)`}>
            <rect
              x={b.cx - b.w / 2}
              y={b.cy - b.h / 2}
              width={b.w}
              height={b.h}
              rx={18}
              fill={n.emphasis ? PALETTE.lavender : PALETTE.paper}
              stroke={lit ? PALETTE.coral : PALETTE.ink}
              strokeWidth={lit ? 4 : 2.5}
            />
            {lines.map((line, i) => (
              <text
                key={i}
                x={b.cx}
                y={b.cy + (i - (lines.length - 1) / 2) * size * 1.2 + size * 0.35}
                textAnchor="middle"
                fontSize={size}
                fontWeight={n.emphasis ? 600 : 500}
                fill={PALETTE.ink}
              >
                {line}
              </text>
            ))}
          </g>
        );
      })}

      {pulse && pulse.o > 0 ? (
        <g opacity={pulse.o}>
          <circle cx={pulse.x} cy={pulse.y} r={pulse.r * 2.2} fill={PALETTE.coral} opacity={0.18} />
          <circle cx={pulse.x} cy={pulse.y} r={pulse.r} fill={PALETTE.coral} stroke={PALETTE.ink} strokeWidth={2} />
        </g>
      ) : null}
    </svg>
  );
}
