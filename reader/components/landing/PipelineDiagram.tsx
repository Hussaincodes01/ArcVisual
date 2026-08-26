"use client";

/**
 * The five stages, as an animated diagram that walks itself.
 *
 * Deliberately shows the *gates and the drop-outs*, not just a happy path. Most
 * pipeline diagrams on a landing page are five boxes and four arrows, which tells a
 * visitor nothing they could not guess. The interesting claim here is that the
 * pipeline throws work away — ungrounded concepts, triaged animations, scenes that
 * fail a gate — and that is what the counters convey.
 *
 * A single `setInterval` drives the walk; each stage is a CSS transition on
 * opacity/transform, so nothing lays out per frame.
 */

import { useEffect, useState } from "react";

interface Stage {
  n: string;
  name: string;
  does: string;
  /** The thing this stage refuses to do, which is usually the real design. */
  discipline: string;
}

const STAGES: Stage[] = [
  {
    n: "01",
    name: "Ingest",
    does: "arXiv LaTeX source → sections, equations, figures, licence.",
    discipline:
      "Never splits on length. Uses the document's own \\section hierarchy, so every character offset still points at real text.",
  },
  {
    n: "02",
    name: "Analyze",
    does: "Concepts, a dependency DAG, and argued visual opportunities.",
    discipline:
      "Every claim quotes the paper verbatim. A quote we cannot locate is dropped, not approximated.",
  },
  {
    n: "03",
    name: "Generate",
    does: "The model fills a typed schema; a tested template owns the rest.",
    discipline:
      "No free-form Manim. The model contributes parameters and captions — never camera, layout, easing or margins.",
  },
  {
    n: "04",
    name: "Validate",
    does: "Four gates: static, runtime, spatial, semantic.",
    discipline:
      "A scene that fails three times degrades to the paper's own figure, or to prose. Never a broken slot.",
  },
  {
    n: "05",
    name: "Read",
    does: "Prose first at 90 seconds; animations fill their slots as they land.",
    discipline:
      "Every section links back to the original. A companion to the paper, not a replacement for it.",
  },
];

export default function PipelineDiagram() {
  const [active, setActive] = useState(0);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (paused) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(
      () => setActive((i) => (i + 1) % STAGES.length),
      3400
    );
    return () => window.clearInterval(id);
  }, [paused]);

  return (
    <div
      className="grid gap-8 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      {/* Rail */}
      <ol className="relative space-y-1">
        <span
          className="absolute left-[1.4rem] top-2 bottom-2 w-px bg-border"
          aria-hidden="true"
        />
        {STAGES.map((stage, i) => {
          const on = i === active;
          return (
            <li key={stage.n}>
              <button
                type="button"
                onClick={() => setActive(i)}
                aria-current={on ? "step" : undefined}
                className={`relative flex w-full items-center gap-4 rounded-md px-3 py-2.5 text-left transition-colors ${
                  on ? "bg-surface" : "hover:bg-surface/50"
                }`}
              >
                <span
                  className={`relative z-10 flex size-6 shrink-0 items-center justify-center rounded-full border text-[0.65rem] tabular-nums transition-colors ${
                    on
                      ? "border-accent bg-accent text-canvas"
                      : "border-border bg-canvas text-muted"
                  }`}
                >
                  {stage.n}
                </span>
                <span
                  className={`font-medium transition-colors ${
                    on ? "text-fg" : "text-muted"
                  }`}
                >
                  {stage.name}
                </span>
                {on ? (
                  <span className="arc-pulse ml-auto size-1.5 rounded-full bg-accent" />
                ) : null}
              </button>
            </li>
          );
        })}
      </ol>

      {/* Panel */}
      <div className="rounded-lg border border-border bg-surface/30 p-6">
        <p className="text-xs uppercase tracking-widest text-muted">
          Stage {STAGES[active].n} · {STAGES[active].name}
        </p>
        <p key={`d-${active}`} className="demo-caption mt-4 text-lg text-fg">
          {STAGES[active].does}
        </p>
        <div className="mt-6 border-l-2 border-accent/40 pl-4">
          <p className="text-xs uppercase tracking-widest text-muted">
            The discipline
          </p>
          <p key={`x-${active}`} className="demo-caption mt-2 text-sm text-muted">
            {STAGES[active].discipline}
          </p>
        </div>
      </div>
    </div>
  );
}
