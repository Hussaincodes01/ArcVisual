"use client";

/**
 * A live scene with its claim and provenance — the unit a reader actually sees.
 *
 * The claim is the caption: every scene has one, or the schema rejected it. The
 * provenance line says which part of the paper the visual argues from, because a
 * reader looking at a machine-made visual is entitled to know where it came from.
 */

import { useState } from "react";
import type { Beat } from "../lib/types";
import Lightbox from "./Lightbox";
import LiveScene from "./scenes/LiveScene";

interface Props {
  archetype: string;
  params: Record<string, unknown>;
  beats: Beat[];
  claim: string;
  source?: string;
  autoplay?: "view" | "always" | false;
  loop?: boolean;
  className?: string;
  /** Visual weight: the article stage is quieter than a landing showcase. */
  variant?: "card" | "plain";
  macros?: Record<string, string>;
}

export default function SceneCard({
  archetype,
  params,
  beats,
  claim,
  source,
  autoplay = "view",
  loop = false,
  className = "",
  variant = "card",
  macros,
}: Props) {
  const [enlarged, setEnlarged] = useState(false);
  return (
    <figure
      className={`${variant === "card" ? "rounded-[var(--radius-card)] border-[1.5px] border-ink bg-paper p-3 sm:p-4" : ""} ${className}`}
    >
      <LiveScene
        archetype={archetype}
        params={params}
        beats={beats}
        label={claim}
        autoplay={autoplay}
        loop={loop}
        onExpand={() => setEnlarged(true)}
        macros={macros}
      />
      <figcaption className="mt-4 border-t-[1.5px] border-dashed border-line pt-3">
        <p className="text-[0.95rem] font-medium leading-snug text-ink">{claim}</p>
        {source ? <p className="mt-1.5 text-xs text-muted">{source}</p> : null}
      </figcaption>
      <Lightbox open={enlarged} onClose={() => setEnlarged(false)} title={claim}>
        <LiveScene archetype={archetype} params={params} beats={beats} label={claim} autoplay="always" macros={macros} />
      </Lightbox>
    </figure>
  );
}
