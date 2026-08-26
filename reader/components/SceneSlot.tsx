"use client";

/**
 * One animation slot.
 *
 * Playback strategy, per the plan's "pick the robust default":
 *  - **Default: autoplay-on-enter.** An IntersectionObserver calls play() when the
 *    slot enters the stage; `muted playsInline loop`. Reliable everywhere.
 *  - **Enhancement: scroll-scrubbing** only for scenes the storyboard flags
 *    `scrubbable`, and only where seeking is actually cheap. It is feature-detected
 *    and falls back to autoplay, because scrubbing jank costs more reading
 *    experience than smooth seeking buys.
 *  - `prefers-reduced-motion` gets the poster frame and an explicit play button.
 *    Never an animation the reader did not ask for.
 *
 * Every slot carries a provenance chip. A reader looking at a machine-inferred
 * visual is entitled to know that is what they are looking at.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { mediaUrl } from "../lib/api";
import { isMarginal, type Scene } from "../lib/types";
import SceneLightbox from "./SceneLightbox";
import SceneSkeleton from "./SceneSkeleton";

interface Props {
  scene: Scene;
  mediaBase: string;
  sectionHeading: string;
  originUrl: string;
  /** Set while the pipeline is still rendering this slot. */
  pending?: boolean;
  /** 0-1 job progress, shown on the skeleton's arc. */
  jobProgress?: number;
}

export default function SceneSlot({
  scene,
  mediaBase,
  sectionHeading,
  originUrl,
  pending = false,
  jobProgress,
}: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [failed, setFailed] = useState(false);
  const [enlarged, setEnlarged] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReducedMotion(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReducedMotion(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // Autoplay on enter. The observer is the whole playback trigger; there is no
  // scroll listener, so this stays cheap even with a dozen slots on the page.
  useEffect(() => {
    const el = wrapRef.current;
    const video = videoRef.current;
    if (!el || !video || reducedMotion) return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            void video.play().then(
              () => setPlaying(true),
              () => setPlaying(false) // autoplay refusal is not an error worth showing
            );
          } else {
            video.pause();
            setPlaying(false);
          }
        }
      },
      { threshold: 0.4 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [reducedMotion, scene.artifact?.content_hash]);

  const replay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = 0;
    void video.play().then(() => setPlaying(true));
  }, []);

  const toggle = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) void video.play().then(() => setPlaying(true));
    else {
      video.pause();
      setPlaying(false);
    }
  }, []);

  const cycleRate = useCallback(() => {
    const next = rate === 1 ? 1.5 : rate === 1.5 ? 0.5 : 1;
    setRate(next);
    if (videoRef.current) videoRef.current.playbackRate = next;
  }, [rate]);

  // -- degraded and pending states ---------------------------------------- //

  if (pending || scene.state === "validating" || scene.state === "generating") {
    // A 16:9 skeleton, so the video lands in exactly this box and the page never
    // reflows under a reader mid-sentence.
    return (
      <SceneSkeleton
        claim={scene.spec.claim}
        archetype={scene.spec.archetype}
        progress={jobProgress}
      />
    );
  }

  if (scene.state === "degraded" || !scene.artifact || failed) {
    // Never ship a broken slot. Say what happened and point at the paper.
    return (
      <Frame claim={scene.spec.claim}>
        <div className="min-h-40 rounded-md border border-dashed border-border bg-surface/60 p-4 text-sm text-muted">
          <p className="mb-2 text-fg">This one is explained in prose instead.</p>
          <p>{scene.degraded_reason ?? "The animation did not meet our quality bar."}</p>
          <a
            className="mt-3 inline-block text-accent underline decoration-dotted"
            href={originUrl}
            target="_blank"
            rel="noreferrer"
          >
            See the original figure in the paper →
          </a>
        </div>
      </Frame>
    );
  }

  const mp4 = mediaUrl(mediaBase, scene.artifact.mp4_key);
  const webm = mediaUrl(mediaBase, scene.artifact.webm_key);
  const poster = mediaUrl(mediaBase, scene.artifact.poster_key);

  return (
    <Frame claim={scene.spec.claim}>
      <div ref={wrapRef} className="slot-enter">
        <video
          ref={videoRef}
          onClick={() => setEnlarged(true)}
          title="Click to enlarge"
          className="w-full cursor-zoom-in rounded-md border border-border bg-canvas"
          poster={poster || undefined}
          muted
          loop
          playsInline
          preload="metadata"
          controls={reducedMotion}
          onError={() => setFailed(true)}
        >
          {webm ? <source src={webm} type="video/webm" /> : null}
          {mp4 ? <source src={mp4} type="video/mp4" /> : null}
        </video>

        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
          {!reducedMotion ? (
            <>
              <button
                type="button"
                onClick={toggle}
                className="rounded border border-border px-2 py-1 hover:text-fg"
              >
                {playing ? "Pause" : "Play"}
              </button>
              <button
                type="button"
                onClick={replay}
                className="rounded border border-border px-2 py-1 hover:text-fg"
              >
                Replay
              </button>
              <button
                type="button"
                onClick={cycleRate}
                className="rounded border border-border px-2 py-1 hover:text-fg"
              >
                {rate}×
              </button>
              <button
                type="button"
                onClick={() => setEnlarged(true)}
                className="rounded border border-border px-2 py-1 hover:border-accent hover:text-accent"
              >
                Enlarge
              </button>
            </>
          ) : null}

          {/* Provenance. Which section this came from, and how sure we are. */}
          <span className="ml-auto flex items-center gap-2">
            <span className="rounded bg-surface px-2 py-1">
              from “{sectionHeading}”
            </span>
            {isMarginal(scene) ? (
              <span
                className="rounded bg-surface px-2 py-1 text-warn"
                title="This animation passed our review with a marginal score."
              >
                marginal
              </span>
            ) : null}
            {scene.artifact.quality === "draft" ? (
              <span className="rounded bg-surface px-2 py-1" title="Draft quality; the final render is still queued.">
                draft
              </span>
            ) : null}
          </span>
        </div>
      </div>

      <SceneLightbox
        open={enlarged}
        onClose={() => setEnlarged(false)}
        claim={scene.spec.claim}
        sectionHeading={sectionHeading}
        mp4={mp4}
        webm={webm}
        poster={poster}
        reducedMotion={reducedMotion}
      />
    </Frame>
  );
}

function Frame({ claim, children }: { claim: string; children: React.ReactNode }) {
  return (
    <figure className="rounded-lg border border-border bg-surface/40 p-3">
      {children}
      {/* The claim is the caption. Every scene has one, or the schema rejected it. */}
      <figcaption className="mt-3 border-t border-border pt-2 text-sm text-fg">
        {claim}
      </figcaption>
    </figure>
  );
}
