"use client";

/**
 * The loading mark: an arc that draws itself, in the same palette the Manim templates
 * render with. Used wherever the pipeline is working and there is nothing yet to show.
 *
 * Three deliberate constraints:
 *
 * 1. **Pure SVG + CSS, no JS animation loop.** A React state tick per frame would
 *    compete with the video decoding happening in the same page. `stroke-dasharray`
 *    animation runs on the compositor and costs nothing.
 * 2. **`prefers-reduced-motion` gets a static arc**, not a hidden one. Removing the
 *    indicator entirely would leave a reduced-motion reader with no signal that work
 *    is in progress — worse than a still image.
 * 3. **It never spins forever silently.** Every caller pairs it with text saying what
 *    is happening; the plan is explicit that ArcVisual is asynchronous by design and
 *    that hiding the wait behind a spinner produces a bad product.
 */

interface Props {
  size?: number;
  /** 0–1. Omit for indeterminate. A known fraction should always be shown as one. */
  progress?: number;
  label?: string;
  className?: string;
}

const STROKE = 3;

export default function ArcLoader({
  size = 40,
  progress,
  label,
  className = "",
}: Props) {
  const radius = (size - STROKE) / 2;
  const circumference = 2 * Math.PI * radius;
  const determinate = typeof progress === "number";
  const clamped = determinate ? Math.min(1, Math.max(0, progress)) : 0;

  return (
    <span
      className={`inline-flex items-center gap-3 ${className}`}
      role="status"
      aria-live="polite"
      aria-label={label ?? (determinate ? `${Math.round(clamped * 100)}%` : "working")}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className={determinate ? "" : "arc-spin"}
        aria-hidden="true"
      >
        {/* Track */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-border)"
          strokeWidth={STROKE}
        />
        {/* Arc */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={
            determinate ? circumference * (1 - clamped) : circumference * 0.72
          }
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          className={determinate ? "arc-progress" : ""}
        />
      </svg>
      {label ? <span className="text-sm text-muted">{label}</span> : null}
    </span>
  );
}

/**
 * Three dots that settle in sequence. For inline "still working" text where a full
 * arc would be too heavy — a caption line, a table cell.
 */
export function ArcDots({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 ${className}`} aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="arc-dot size-1.5 rounded-full bg-accent"
          style={{ animationDelay: `${i * 160}ms` }}
        />
      ))}
    </span>
  );
}
