"use client";

/**
 * The placeholder that stands in for an animation slot while it renders.
 *
 * The design constraint that matters: **it occupies the exact space the video will**,
 * via a 16:9 aspect box. A skeleton that collapses when the real video arrives makes
 * the page jump under a reader mid-sentence, and in a scroll-driven article a reflow
 * is the one jank nobody forgives.
 *
 * It also shows the scene's *claim* while it waits. The claim is already known — it
 * came out of Analyze long before the render finished — so a reader gets the argument
 * the animation will make even before the animation exists. That is the difference
 * between a placeholder and a spinner.
 */

import ArcLoader from "./ArcLoader";

interface Props {
  claim: string;
  archetype?: string;
  /** 0–1 across the whole job, for the arc. Omit for indeterminate. */
  progress?: number;
  stage?: string;
}

const ARCHETYPE_LABELS: Record<string, string> = {
  transform_chain: "deriving the equation, step by step",
  plot_reveal: "drawing the curve",
  architecture_flow: "assembling the diagram",
};

export default function SceneSkeleton({ claim, archetype, progress, stage }: Props) {
  const what = archetype ? ARCHETYPE_LABELS[archetype] : undefined;

  return (
    <figure className="rounded-lg border border-border bg-surface/40 p-3">
      {/* 16:9, so the real video lands in exactly this box and nothing reflows. */}
      <div className="relative aspect-video w-full overflow-hidden rounded-md border border-border bg-canvas">
        <div className="shimmer absolute inset-0" aria-hidden="true" />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
          <ArcLoader size={44} progress={progress} />
          <p className="text-sm text-muted">
            {stage ?? (what ? `Rendering — ${what}` : "Rendering this animation")}
          </p>
        </div>
      </div>

      {/* The claim is known now; show it rather than making the reader wait for it. */}
      <figcaption className="mt-3 border-t border-border pt-2 text-sm text-fg">
        {claim}
      </figcaption>
    </figure>
  );
}

/** Prose placeholder, for the brief window before the article text is served. */
export function ProseSkeleton({ lines = 5 }: { lines?: number }) {
  // Varied widths: uniform bars read as a loading bug rather than as text.
  const widths = ["92%", "97%", "85%", "94%", "70%", "89%", "96%"];
  return (
    <div className="prose-arc space-y-3" aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="shimmer h-4 rounded"
          style={{ width: widths[i % widths.length] }}
        />
      ))}
    </div>
  );
}
