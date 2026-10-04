"use client";

/**
 * One visual slot in an article, in whichever state it is in.
 *
 * - **Building**: a skeleton the exact size of the stage, showing the scene's claim
 *   already — it is known long before the visual is ready, and it is the argument
 *   the visual will make.
 * - **Passed, client**: drawn live from its parameters.
 * - **Passed, video**: a rendered MP4 (deployments with a render worker).
 * - **Degraded**: never a broken slot. Say the idea is carried by the text, and
 *   point at the paper.
 */

import { mediaUrl } from "../lib/api";
import { isClientScene, type Scene } from "../lib/types";
import { ArcDots } from "./ArcLoader";
import SceneCard from "./SceneCard";

interface Props {
  scene: Scene;
  sectionHeading: string;
  mediaBase: string;
  originUrl: string;
  macros?: Record<string, string>;
}

const BUILDING_LABEL: Record<string, string> = {
  transform_chain: "Writing out the derivation",
  plot_reveal: "Plotting the result",
  architecture_flow: "Laying out the diagram",
};

export default function SceneSlot({ scene, sectionHeading, mediaBase, originUrl, macros }: Props) {
  const source = `From the section “${sectionHeading}”`;

  if (scene.state === "pending" || scene.state === "generating" || scene.state === "validating") {
    return (
      <figure className="rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-3 sm:p-4" aria-busy="true">
        <div className="scene-stage outlined shimmer flex items-center justify-center">
          <p className="flex items-center gap-2 rounded-full border-[1.5px] border-ink bg-paper px-4 py-2 text-sm">
            {BUILDING_LABEL[scene.spec.archetype] ?? "Drawing this visual"}
            <ArcDots />
          </p>
        </div>
        <figcaption className="mt-4 border-t-[1.5px] border-dashed border-line pt-3">
          <p className="text-[0.95rem] font-medium leading-snug">{scene.spec.claim}</p>
          <p className="mt-1.5 text-xs text-muted">{source}</p>
        </figcaption>
      </figure>
    );
  }

  if (isClientScene(scene)) {
    return (
      <SceneCard
        archetype={scene.spec.archetype}
        params={scene.spec.params}
        beats={scene.spec.beats}
        claim={scene.spec.claim}
        source={source}
        macros={macros}
      />
    );
  }

  if (scene.state === "passed" && scene.artifact && scene.artifact.bytes > 0) {
    const mp4 = mediaUrl(mediaBase, scene.artifact.mp4_key);
    const webm = mediaUrl(mediaBase, scene.artifact.webm_key);
    const poster = mediaUrl(mediaBase, scene.artifact.poster_key);
    return (
      <figure className="rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-3 sm:p-4">
        <video className="w-full rounded-[var(--radius-tile)] border-[1.5px] border-ink" poster={poster || undefined} controls muted playsInline preload="metadata">
          {webm ? <source src={webm} type="video/webm" /> : null}
          {mp4 ? <source src={mp4} type="video/mp4" /> : null}
        </video>
        <figcaption className="mt-4 border-t-[1.5px] border-dashed border-line pt-3">
          <p className="text-[0.95rem] font-medium leading-snug">{scene.spec.claim}</p>
          <p className="mt-1.5 text-xs text-muted">{source}</p>
        </figcaption>
      </figure>
    );
  }

  return (
    <figure className="rounded-[var(--radius-card)] border-[1.5px] border-dashed border-ink bg-wash p-5">
      <p className="font-medium">This idea is explained in the text instead.</p>
      <p className="mt-2 text-sm leading-relaxed text-ink-soft">{scene.spec.claim}</p>
      <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
        <a href={originUrl} target="_blank" rel="noreferrer" className="pill bg-paper hover:bg-mustard-soft">
          See the paper&rsquo;s own figure
        </a>
        {scene.degraded_reason ? (
          <details className="text-xs text-muted">
            <summary className="cursor-pointer">Why</summary>
            <p className="mt-1 max-w-sm">
              The visual did not pass our checks, so we left it out rather than show something misleading.
            </p>
          </details>
        ) : null}
      </div>
    </figure>
  );
}
