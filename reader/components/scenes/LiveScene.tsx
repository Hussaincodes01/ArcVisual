"use client";

/**
 * The player around every live scene.
 *
 * Playback rules, carried over from the video player this replaces:
 *  - **Autoplay on enter**, once, when at least 40% of the stage is visible; paused
 *    when it scrolls away. No scroll listener — an IntersectionObserver is the whole
 *    trigger, so a page with a dozen scenes stays cheap.
 *  - **Reduced motion gets the finished frame** and an explicit play button. Never
 *    an animation the reader did not ask for.
 *  - **Scrubbable everywhere.** A scene is a pure function of time, so seeking is
 *    free — the property the old MP4s only had where they were all-keyframe encoded.
 *
 * The requestAnimationFrame loop only runs while playing, and stops at the end.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Beat } from "../../lib/types";
import { RENDERERS } from "./registry";

interface Props {
  archetype: string;
  params: Record<string, unknown>;
  beats?: Beat[];
  /** Accessible name for the stage; usually the scene's claim. */
  label: string;
  autoplay?: "view" | "always" | false;
  loop?: boolean;
  controls?: boolean;
  /** Called when the reader asks for a larger view. */
  onExpand?: () => void;
  className?: string;
  macros?: Record<string, string>;
}

const RATES = [1, 1.5, 0.5] as const;
/** Seconds the final frame holds before a looping scene starts again. */
const LOOP_REST_S = 2.6;

function fmt(s: number): string {
  const v = Math.max(0, Math.round(s));
  return `${Math.floor(v / 60)}:${String(v % 60).padStart(2, "0")}`;
}

export default function LiveScene({
  archetype,
  params,
  beats = [],
  label,
  autoplay = "view",
  loop = false,
  controls = true,
  onExpand,
  className = "",
  macros,
}: Props) {
  const renderer = RENDERERS[archetype];
  const plan = useMemo(() => {
    try {
      return renderer ? renderer.plan(params) : null;
    } catch {
      return null;
    }
  }, [renderer, params]);

  const beatEnd = beats.length ? beats[beats.length - 1].t + beats[beats.length - 1].dur : 0;
  const duration = Math.max(plan?.duration ?? 0, beatEnd, 0.1);

  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState<(typeof RATES)[number]>(1);
  const [reduced, setReduced] = useState(false);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const started = useRef(false);
  const raf = useRef<number | null>(null);
  const last = useRef<number | null>(null);
  const tRef = useRef(0);
  tRef.current = t;

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => {
      setReduced(mq.matches);
      if (mq.matches) {
        setPlaying(false);
        setT(duration);
      }
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [duration]);

  // The clock. Elapsed time is measured, not assumed, so a dropped frame never
  // slows the animation down — it just skips ahead.
  useEffect(() => {
    if (!playing) {
      last.current = null;
      return;
    }
    const tick = (now: number) => {
      const prev = last.current ?? now;
      last.current = now;
      let next = tRef.current + ((now - prev) / 1000) * rate;
      if (next >= duration + (loop ? LOOP_REST_S : 0)) {
        if (loop) {
          next = 0;
        } else {
          setT(duration);
          setPlaying(false);
          return;
        }
      }
      setT(next);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current !== null) cancelAnimationFrame(raf.current);
    };
  }, [playing, rate, duration, loop]);

  // Autoplay when the stage comes into view; pause when it leaves.
  useEffect(() => {
    const el = stageRef.current;
    if (!el || reduced || !autoplay) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          if (autoplay === "always" || !started.current) {
            started.current = true;
            if (tRef.current >= duration) setT(0);
            setPlaying(true);
          }
        } else {
          setPlaying(false);
        }
      },
      { threshold: 0.4 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [autoplay, reduced, duration]);

  const toggle = useCallback(() => {
    if (playing) {
      setPlaying(false);
    } else {
      if (tRef.current >= duration) setT(0);
      setPlaying(true);
    }
  }, [playing, duration]);

  const replay = useCallback(() => {
    setT(0);
    setPlaying(true);
  }, []);

  if (!renderer || !plan) {
    return (
      <div className={`scene-stage outlined flex items-center justify-center p-6 text-center ${className}`}>
        <p className="max-w-sm text-sm text-muted">
          This visual could not be drawn from its parameters. The explanation is in the text beside it.
        </p>
      </div>
    );
  }

  const shown = Math.min(t, duration);
  const caption =
    beats.find((b) => shown >= b.t && shown < b.t + b.dur)?.caption ??
    (shown >= duration && beats.length ? beats[beats.length - 1].caption : beats[0]?.caption) ??
    "";
  const ended = !playing && shown >= duration - 0.01;
  const { Component } = renderer;

  return (
    <div className={className}>
      <div
        ref={stageRef}
        className="scene-stage outlined"
        role="group"
        aria-roledescription="animation"
        aria-label={label}
      >
        <Component plan={plan} t={shown} macros={macros} />
        {controls && ended ? (
          <button
            type="button"
            onClick={replay}
            className="pill absolute right-3 top-3 bg-paper text-sm font-medium hover:bg-mustard"
          >
            <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
              <path d="M3 8a5 5 0 1 0 1.6-3.7M3 2.5V5h2.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
            Replay
          </button>
        ) : null}
      </div>

      {caption ? (
        <p className="mt-3 min-h-[3rem] text-[0.95rem] leading-snug text-ink" aria-live="off">
          {caption}
        </p>
      ) : null}

      {controls ? (
        <div className="mt-2 flex items-center gap-3">
          <button
            type="button"
            onClick={toggle}
            aria-label={playing ? "Pause" : "Play"}
            className="flex size-9 shrink-0 items-center justify-center rounded-full border-[1.5px] border-ink bg-ink text-paper transition-colors hover:bg-coral"
          >
            {playing ? (
              <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                <rect x="3.5" y="3" width="3" height="10" rx="1" fill="currentColor" />
                <rect x="9.5" y="3" width="3" height="10" rx="1" fill="currentColor" />
              </svg>
            ) : (
              <svg viewBox="0 0 16 16" className="ml-0.5 size-3.5" aria-hidden="true">
                <path d="M4 2.8v10.4L13 8z" fill="currentColor" />
              </svg>
            )}
          </button>
          <input
            type="range"
            className="scrubber"
            min={0}
            max={duration}
            step={0.01}
            value={shown}
            aria-label="Scrub through the animation"
            aria-valuetext={`${fmt(shown)} of ${fmt(duration)}`}
            style={{ "--fill": `${(shown / duration) * 100}%` } as React.CSSProperties}
            onChange={(e) => {
              setPlaying(false);
              setT(Number(e.target.value));
            }}
          />
          <span className="w-[4.6rem] shrink-0 text-right text-xs tabular-nums text-muted">
            {fmt(shown)} / {fmt(duration)}
          </span>
          <button
            type="button"
            onClick={() => setRate(RATES[(RATES.indexOf(rate) + 1) % RATES.length])}
            className="pill shrink-0 bg-paper text-xs tabular-nums hover:bg-wash"
            aria-label={`Playback speed ${rate} times`}
          >
            {rate}×
          </button>
          {onExpand ? (
            <button
              type="button"
              onClick={onExpand}
              aria-label="Enlarge"
              className="flex size-8 shrink-0 items-center justify-center rounded-full border-[1.5px] border-ink bg-paper hover:bg-wash"
            >
              <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                <path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5 9 7M2.5 13.5 7 9" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
              </svg>
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
