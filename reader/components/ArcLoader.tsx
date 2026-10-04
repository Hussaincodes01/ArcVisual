"use client";

/**
 * The working indicators. Pure SVG and CSS (no JS animation loop), and under
 * reduced motion they hold still rather than vanish — a reader who asked for less
 * motion still needs to know that work is happening. Every caller pairs one with
 * text saying what is happening.
 */

interface Props {
  size?: number;
  /** 0–1. Omit for indeterminate. A known fraction should always be shown as one. */
  progress?: number;
  label?: string;
  className?: string;
}

const STROKE = 4;

export default function ArcLoader({ size = 44, progress, label, className = "" }: Props) {
  const radius = (size - STROKE) / 2;
  const circumference = 2 * Math.PI * radius;
  const determinate = typeof progress === "number";
  const clamped = determinate ? Math.min(1, Math.max(0, progress)) : 0.28;
  return (
    <span
      className={`inline-flex ${className}`}
      role="status"
      aria-label={label ?? (determinate ? `${Math.round(clamped * 100)}% done` : "Working")}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="#fff" stroke="#e6e3dc" strokeWidth={STROKE} />
        <g className={determinate ? "" : "arc-spin"} style={{ transformOrigin: "50% 50%" }}>
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="#ff5734"
            strokeWidth={STROKE}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - clamped)}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ transition: determinate ? "stroke-dashoffset 600ms ease" : undefined }}
          />
        </g>
      </svg>
    </span>
  );
}

export function ArcDots({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 ${className}`} aria-hidden="true">
      <span className="arc-dot block size-1.5 rounded-full bg-coral" />
      <span className="arc-dot block size-1.5 rounded-full bg-coral" />
      <span className="arc-dot block size-1.5 rounded-full bg-coral" />
    </span>
  );
}
