"use client";

/**
 * The interactive half of the hero demo: a timer, and nothing else.
 *
 * Receives finished KaTeX HTML from the server component, so this island carries no
 * typesetting library. Its whole job is deciding which pre-rendered step is on screen.
 *
 * The pacing constants come in as props from one place, so the demo stays in step with
 * the style contract in `arcvisual/templates/base.py`. If the pacing floor changes
 * there, it changes here.
 */

import { useEffect, useRef, useState } from "react";

// Required wherever KaTeX markup is mounted, not just where KaTeX is called.
// KaTeX emits two layers — a visually-hidden MathML one for screen readers and the
// visual HTML one — and it is this stylesheet that hides the first. Without it both
// paint, and the equation renders twice, overlapping. The article route imports it
// separately; the landing page needs its own because it renders KaTeX too.
import "katex/dist/katex.min.css";

export interface RenderedStep {
  /** KaTeX output, typeset at build time. Empty when typesetting failed. */
  html: string;
  latex: string;
  caption: string;
}

interface Props {
  steps: RenderedStep[];
  beatMs: number;
  holdMs: number;
}

export default function TransformDemoClient({ steps, beatMs, holdMs }: Props) {
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // Reduced motion gets the finished derivation, not a frozen first frame — the last
  // step is the one carrying the actual result.
  useEffect(() => {
    if (reduced) setIndex(steps.length - 1);
  }, [reduced, steps.length]);

  useEffect(() => {
    if (reduced || paused) return;
    const last = index === steps.length - 1;
    timer.current = window.setTimeout(
      () => setIndex((i) => (i + 1) % steps.length),
      last ? holdMs * 2 : beatMs + holdMs
    );
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [index, paused, reduced, steps.length, beatMs, holdMs]);

  const step = steps[index];

  return (
    <figure
      className="rounded-lg border border-border bg-canvas p-6 sm:p-8"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      tabIndex={0}
      aria-label="A worked example: scaled dot-product attention, derived one step at a time"
    >
      <div className="flex min-h-[7.5rem] items-center justify-center">
        {step.html ? (
          <div
            key={index}
            className="demo-equation scroll-x w-full text-center"
            // Server-typeset KaTeX from a constant in this repo — never user input.
            dangerouslySetInnerHTML={{ __html: step.html }}
          />
        ) : (
          <code className="text-sm text-muted">{step.latex}</code>
        )}
      </div>

      <p
        key={`cap-${index}`}
        className="demo-caption mt-6 min-h-[3rem] text-center text-sm text-muted"
      >
        {step.caption}
      </p>

      {/* Beat markers, mirroring the storyboard's own beat list. */}
      <div className="mt-5 flex items-center justify-center gap-2">
        {steps.map((_, i) => (
          <button
            key={i}
            type="button"
            onClick={() => setIndex(i)}
            className={`h-1 rounded-full transition-all duration-500 ${
              i === index ? "w-8 bg-accent" : "w-4 bg-border hover:bg-muted"
            }`}
            aria-label={`Step ${i + 1} of ${steps.length}`}
            aria-current={i === index ? "step" : undefined}
          />
        ))}
      </div>

      <figcaption className="mt-6 border-t border-border pt-4 text-xs text-muted">
        <span className="text-fg">The claim this visual makes:</span> the scaling factor
        is what keeps the softmax out of its saturated region.
        {!reduced ? (
          <span className="ml-1 opacity-70">
            {paused ? " (paused)" : " (hover to pause)"}
          </span>
        ) : null}
      </figcaption>
    </figure>
  );
}
