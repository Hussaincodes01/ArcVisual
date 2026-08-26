"use client";

/**
 * Polls `GET /api/jobs/:id` and derives everything the UI needs from it.
 *
 * Polling rather than SSE, deliberately (see `arcvisual/render/api.py`): one JSONB read
 * per tick, survives every proxy between Vercel and Modal, nothing to operate. The
 * backoff below is the whole reason it stays cheap — 3s while a reader is watching
 * closely, easing to 10s once the wait is clearly a long one.
 *
 * Two failure modes handled explicitly, because both are common in practice:
 *
 * - **A dropped poll is not an error.** Transient network blips are ignored and the
 *   next tick recovers. Only a run of consecutive failures surfaces to the reader.
 * - **The interval is cleared on unmount and on terminal state.** A forgotten
 *   `setTimeout` chain polling a finished job forever is the classic version of this
 *   bug, and it is invisible until someone reads the access log.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJob } from "../lib/api";
import type { JobStatus } from "../lib/types";

export const STAGES = [
  { key: "queued", label: "Queued", detail: "Waiting for a worker" },
  { key: "ingesting", label: "Reading the paper", detail: "Sections, equations, figures" },
  { key: "analyzing", label: "Working out what to explain", detail: "Concepts and triage" },
  { key: "rendering", label: "Animating", detail: "One scene at a time" },
  { key: "complete", label: "Done", detail: "Article ready" },
] as const;

export type StageKey = (typeof STAGES)[number]["key"];

const FAST_MS = 3_000;
const SLOW_MS = 10_000;
const EASE_AFTER_MS = 120_000;
//: Only tell the reader something is wrong after this many consecutive failures; a
//: single dropped request is noise, not news.
const FAILURES_BEFORE_SURFACING = 3;

export interface JobView {
  status: JobStatus | null;
  /** 0–1 across the whole pipeline, blending stage index with scene completion. */
  progress: number;
  stageIndex: number;
  isTerminal: boolean;
  failed: boolean;
  scenesDone: number;
  scenesTotal: number;
  error: string | null;
  elapsedMs: number;
  refresh: () => void;
}

export function useJobStatus(jobId: string | null | undefined): JobView {
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsed] = useState(0);

  const startedAt = useRef<number>(Date.now());
  const timer = useRef<number | null>(null);
  const failures = useRef(0);
  const stopped = useRef(false);

  const clear = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const poll = useCallback(async () => {
    if (!jobId || stopped.current) return;
    try {
      const next = await fetchJob(jobId);
      failures.current = 0;
      setError(null);
      setStatus(next);
      if (next.state === "complete" || next.state === "failed") {
        stopped.current = true;
        clear();
        return;
      }
    } catch (err) {
      failures.current += 1;
      if (failures.current >= FAILURES_BEFORE_SURFACING) {
        setError(
          err instanceof Error ? err.message : "Lost contact with the pipeline."
        );
      }
    }
    const age = Date.now() - startedAt.current;
    timer.current = window.setTimeout(poll, age > EASE_AFTER_MS ? SLOW_MS : FAST_MS);
  }, [jobId, clear]);

  useEffect(() => {
    if (!jobId) return;
    stopped.current = false;
    failures.current = 0;
    startedAt.current = Date.now();
    void poll();
    return () => {
      stopped.current = true;
      clear();
    };
  }, [jobId, poll, clear]);

  // A separate, cheap ticker so the elapsed readout moves between polls. Without it
  // the page looks frozen for ten seconds at a stretch.
  useEffect(() => {
    if (!jobId) return;
    const id = window.setInterval(() => {
      if (!stopped.current) setElapsed(Date.now() - startedAt.current);
    }, 1_000);
    return () => window.clearInterval(id);
  }, [jobId]);

  const refresh = useCallback(() => {
    stopped.current = false;
    clear();
    void poll();
  }, [poll, clear]);

  return { ...derive(status), error, elapsedMs, refresh, status };
}

function derive(status: JobStatus | null) {
  const stageIndex = Math.max(
    0,
    STAGES.findIndex((s) => s.key === (status?.state ?? "queued"))
  );
  const scenes = status?.scenes ?? [];
  const scenesTotal = scenes.length;
  const scenesDone = scenes.filter((s) => s.state !== "pending" && s.state !== "generating" && s.state !== "validating").length;

  const failed = status?.state === "failed";
  const isTerminal = failed || status?.state === "complete";

  // Rendering dominates the wall clock, so it owns most of the bar. Anything else
  // would sit at 90% for ten minutes, which reads as broken.
  let progress: number;
  if (isTerminal) {
    progress = 1;
  } else if (status?.state === "rendering" && scenesTotal > 0) {
    progress = 0.35 + 0.6 * (scenesDone / scenesTotal);
  } else {
    progress = [0.02, 0.12, 0.3, 0.35, 1][stageIndex] ?? 0.02;
  }

  return {
    progress: Math.min(1, progress),
    stageIndex,
    isTerminal,
    failed,
    scenesDone,
    scenesTotal,
  };
}

export function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes === 0) return `${seconds}s`;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}
