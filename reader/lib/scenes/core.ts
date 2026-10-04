/**
 * The browser scene engine's shared vocabulary.
 *
 * A scene is a pure function of time: `render(plan, t)`. Each archetype builds a
 * *plan* from its validated parameters — the same parameters the Python template
 * would hand to Manim — and the plan's timeline mirrors that template's
 * `estimate_duration` beat for beat. That is what keeps the captions (fitted to the
 * template's runtime by the pipeline) in step with what is on screen.
 *
 * Pure functions of `t` make three things free: scrubbing, reduced motion (render
 * the final frame), and determinism (the same params always draw the same scene).
 */

/** Minimum seconds per beat and the hold after a reveal — arcvisual/templates/base.py. */
export const MIN_BEAT_S = 0.8;
export const REVEAL_HOLD_S = 1.5;

/** Scene canvas coordinates. Everything is laid out in this box and scaled by SVG. */
export const VIEW_W = 960;
export const VIEW_H = 540;

/** The page palette, reused so an animation reads as part of the page. */
export const PALETTE = {
  ink: "#151313",
  inkSoft: "#4a4643",
  muted: "#8a8480",
  line: "#e6e3dc",
  paper: "#ffffff",
  coral: "#ff5734",
  lavender: "#be94f5",
  lavenderSoft: "#ece1fd",
  mustard: "#fccc42",
  mustardSoft: "#fff3cc",
  violet: "#7a4fd6",
  amber: "#c98a00",
} as const;

/** Line colours for data series, chosen to stay legible on white. */
export const SERIES_COLORS = [PALETTE.coral, PALETTE.violet, PALETTE.amber, PALETTE.inkSoft];

export interface Segment {
  start: number;
  dur: number;
}

export function clamp(v: number, lo = 0, hi = 1): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** 0 before the segment, 1 after it, linear in between. */
export function progress(t: number, seg: Segment | null | undefined): number {
  if (!seg) return 0;
  if (seg.dur <= 0) return t >= seg.start ? 1 : 0;
  return clamp((t - seg.start) / seg.dur);
}

export const ease = {
  inOutCubic: (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
  outCubic: (x: number) => 1 - Math.pow(1 - x, 3),
  inOutSine: (x: number) => -(Math.cos(Math.PI * x) - 1) / 2,
  /** A single up-and-back pulse, for "indicate". 0 → 1 → 0. */
  pulse: (x: number) => Math.sin(Math.PI * clamp(x)),
};

/**
 * Builds a timeline by appending segments end to end, the way a Manim body plays
 * one `play()` after another.
 */
export class Clock {
  t = 0;

  /** Reserve `dur` seconds and return the segment. */
  play(dur: number): Segment {
    const seg = { start: this.t, dur };
    this.t += dur;
    return seg;
  }

  wait(dur: number): void {
    this.t += dur;
  }
}

/** A tick step giving roughly 5–8 ticks, rounded to something human. */
export function niceStep(lo: number, hi: number): number {
  const span = hi - lo;
  if (span <= 0 || !Number.isFinite(span)) return 1;
  const raw = span / 6;
  const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw))));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (raw <= mag * mult) return mag * mult;
  }
  return mag * 10;
}

export function formatNumber(v: number): string {
  if (!Number.isFinite(v)) return "";
  if (Number.isInteger(v)) return String(v);
  const abs = Math.abs(v);
  if (abs >= 1000 || abs < 0.001) return v.toExponential(1);
  return String(Number(v.toPrecision(3)));
}

/** Split a label into at most `maxLines` lines of roughly `width` characters. */
export function wrapLabel(text: string, width: number, maxLines = 2): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    const next = current ? `${current} ${word}` : word;
    if (next.length > width && current) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  }
  if (current) lines.push(current);
  if (lines.length <= maxLines) return lines;
  const kept = lines.slice(0, maxLines);
  kept[maxLines - 1] = `${kept[maxLines - 1].replace(/[.,;:]?$/, "")}…`;
  return kept;
}

export interface ScenePlan<D> {
  /** Seconds the scene plays for, from its own timeline. */
  duration: number;
  data: D;
}

/** What every archetype renderer receives. */
export interface SceneProps<D> {
  plan: ScenePlan<D>;
  t: number;
  /** The paper's own LaTeX macros, so notation copied from it typesets. */
  macros?: Record<string, string>;
}

export function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function asString(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

export function asStringList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
