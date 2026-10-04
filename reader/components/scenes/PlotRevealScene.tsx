"use client";

import { useId } from "react";

/**
 * `plot_reveal` in the browser: a results chart revealed rather than presented.
 *
 * Mirrors arcvisual/templates/plot_reveal.py: axes first, then each series drawn
 * left to right, its callout, the legend, and the takeaway last. Series are explicit
 * point lists — the template never evaluates a model-supplied formula, and neither
 * does this.
 */

import {
  PALETTE,
  SERIES_COLORS,
  VIEW_H,
  VIEW_W,
  clamp,
  ease,
  formatNumber,
  niceStep,
  progress,
  type SceneProps,
} from "../../lib/scenes/core";
import type { PlotRevealData } from "../../lib/scenes/plans";
import { plainText } from "../../lib/tex";

function ticks(lo: number, hi: number, log: boolean): number[] {
  if (log) {
    const out: number[] = [];
    for (let e = Math.ceil(Math.log10(lo)); e <= Math.floor(Math.log10(hi)); e++) out.push(Math.pow(10, e));
    return out;
  }
  const step = niceStep(lo, hi);
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(Number(v.toPrecision(12)));
  return out.slice(0, 12);
}

export default function PlotRevealScene({ plan, t, macros }: SceneProps<PlotRevealData>) {
  // SVG cannot hold KaTeX: axis titles, series names and the takeaway are shown as
  // readable Unicode math rather than TeX source.
  const show = (text: string) => plainText(text, macros);
  const d = plan.data;
  // Unique per scene: several charts can share a page, and SVG ids are global.
  const clipId = `plot-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const left = 128;
  const right = VIEW_W - 40;
  const top = 34;
  const bottom = d.takeaway ? 380 : 436;
  const pw = right - left;
  const ph = bottom - top;

  const sx = (x: number) => left + ((x - d.xRange[0]) / (d.xRange[1] - d.xRange[0])) * pw;
  const sy = (y: number) => {
    if (d.logY) {
      const [a, b] = [Math.log10(d.yRange[0]), Math.log10(d.yRange[1])];
      return bottom - ((Math.log10(y) - a) / (b - a)) * ph;
    }
    return bottom - ((y - d.yRange[0]) / (d.yRange[1] - d.yRange[0])) * ph;
  };

  const a = ease.outCubic(progress(t, d.axes));
  const xt = ticks(d.xRange[0], d.xRange[1], false);
  const yt = ticks(d.yRange[0], d.yRange[1], d.logY);

  return (
    <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} className="absolute inset-0 h-full w-full" role="img" aria-label={`Chart of ${show(d.yLabel)} against ${show(d.xLabel)}`}>
      <defs>
        <clipPath id={clipId}>
          <rect x={left - 8} y={top - 12} width={pw + 16} height={ph + 20} />
        </clipPath>
      </defs>

      {/* Grid and ticks fade in with the axes. */}
      <g opacity={a}>
        {yt.map((v) => (
          <g key={`y${v}`}>
            <line x1={left} x2={right} y1={sy(v)} y2={sy(v)} stroke={PALETTE.line} strokeWidth={1.5} />
            <text x={left - 14} y={sy(v) + 7} textAnchor="end" fontSize={22} fill={PALETTE.muted}>
              {formatNumber(v)}
            </text>
          </g>
        ))}
        {xt.map((v) => (
          <text key={`x${v}`} x={sx(v)} y={bottom + 32} textAnchor="middle" fontSize={22} fill={PALETTE.muted}>
            {formatNumber(v)}
          </text>
        ))}
        <text x={(left + right) / 2} y={bottom + 70} textAnchor="middle" fontSize={25} fontWeight={500} fill={PALETTE.ink}>
          {show(d.xLabel)}
        </text>
        <text
          transform={`translate(34 ${(top + bottom) / 2}) rotate(-90)`}
          textAnchor="middle"
          fontSize={25}
          fontWeight={500}
          fill={PALETTE.ink}
        >
          {show(d.yLabel)}
        </text>
      </g>
      <line x1={left} y1={bottom} x2={left + pw * a} y2={bottom} stroke={PALETTE.ink} strokeWidth={2.5} strokeLinecap="round" />
      <line x1={left} y1={bottom} x2={left} y2={bottom - ph * a} stroke={PALETTE.ink} strokeWidth={2.5} strokeLinecap="round" />

      <g clipPath={`url(#${clipId})`}>
        {d.series.map((s, i) => {
          const color = SERIES_COLORS[i % SERIES_COLORS.length];
          const p = ease.inOutSine(progress(t, d.curves[i]));
          if (p <= 0) return null;
          const pts = s.points.map(([x, y]) => [sx(x), sy(y)] as [number, number]);
          const lengths = [0];
          for (let j = 1; j < pts.length; j++) {
            lengths.push(lengths[j - 1] + Math.hypot(pts[j][0] - pts[j - 1][0], pts[j][1] - pts[j - 1][1]));
          }
          const total = lengths[lengths.length - 1] || 1;
          const path = pts.map(([x, y], j) => `${j ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
          return (
            <g key={i}>
              {s.dashed ? (
                <path
                  d={path}
                  fill="none"
                  stroke={color}
                  strokeWidth={4}
                  strokeDasharray="12 10"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  opacity={p}
                />
              ) : (
                <path
                  d={path}
                  fill="none"
                  stroke={color}
                  strokeWidth={4}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeDasharray={total}
                  strokeDashoffset={total * (1 - p)}
                />
              )}
              {pts.length <= 24
                ? pts.map(([x, y], j) =>
                    lengths[j] / total <= p + 1e-6 ? (
                      <circle key={j} cx={x} cy={y} r={5.5} fill={PALETTE.paper} stroke={color} strokeWidth={3} />
                    ) : null,
                  )
                : null}
            </g>
          );
        })}
      </g>

      {/* Callouts: the value the reader should look at, with its label. */}
      {d.series.map((s, i) => {
        const seg = d.callouts[i];
        if (!seg || s.annotateAt === null) return null;
        const q = ease.outCubic(progress(t, seg));
        if (q <= 0) return null;
        const [px, py] = s.points[s.annotateAt];
        const cx = sx(px);
        const cy = sy(py);
        const label = `${show(s.label)}: ${formatNumber(py)}`;
        const w = Math.min(420, label.length * 13 + 32);
        const bx = clamp(cx + 16, left, right - w);
        const by = clamp(cy - 62, top, bottom - 46);
        const color = SERIES_COLORS[i % SERIES_COLORS.length];
        return (
          <g key={`c${i}`} opacity={q}>
            <circle cx={cx} cy={cy} r={9 * (0.5 + 0.5 * q)} fill={color} stroke={PALETTE.ink} strokeWidth={2} />
            <rect x={bx} y={by} width={w} height={42} rx={21} fill={PALETTE.paper} stroke={PALETTE.ink} strokeWidth={2} />
            <text x={bx + w / 2} y={by + 28} textAnchor="middle" fontSize={22} fontWeight={500} fill={PALETTE.ink}>
              {label}
            </text>
          </g>
        );
      })}

      {d.legend ? (
        <g opacity={progress(t, d.legend)}>
          {d.series.map((s, i) => {
            const y = top + 4 + i * 36;
            return (
              <g key={`l${i}`} transform={`translate(${right - 250} ${y})`}>
                <rect x={-12} y={-6} width={262} height={34} rx={17} fill={PALETTE.paper} opacity={0.92} />
                <line x1={0} x2={28} y1={10} y2={10} stroke={SERIES_COLORS[i % SERIES_COLORS.length]} strokeWidth={4} strokeLinecap="round" strokeDasharray={s.dashed ? "7 6" : undefined} />
                <text x={38} y={18} fontSize={21} fill={PALETTE.ink}>
                  {show(s.label).length > 22 ? `${show(s.label).slice(0, 21)}…` : show(s.label)}
                </text>
              </g>
            );
          })}
        </g>
      ) : null}

      {d.takeaway && d.takeawaySeg ? (() => {
        const q = ease.outCubic(progress(t, d.takeawaySeg));
        const full = show(d.takeaway);
        const text = full.length > 80 ? `${full.slice(0, 79)}…` : full;
        const w = Math.min(VIEW_W - 40, text.length * 13 + 52);
        return (
          <g opacity={q} transform={`translate(0 ${(1 - q) * 10})`}>
            <rect x={(VIEW_W - w) / 2} y={474} width={w} height={50} rx={25} fill={PALETTE.mustard} stroke={PALETTE.ink} strokeWidth={2} />
            <text x={VIEW_W / 2} y={507} textAnchor="middle" fontSize={23} fontWeight={500} fill={PALETTE.ink}>
              {text}
            </text>
          </g>
        );
      })() : null}
    </svg>
  );
}
