"use client";

/**
 * Follows a job, and on a serverless deployment also *drives* it.
 *
 * Two loops, deliberately separate:
 *
 * - **The status poll** reads `GET /api/jobs/:id` every few seconds. It is what the
 *   reader sees move: stage changes, scenes landing one by one.
 * - **The driver** calls `POST /api/jobs/:id/advance` back to back while the job is
 *   unfinished. Each call does one bounded step of work (up to a few minutes) and
 *   returns. A job only moves while someone drives it, so this is what makes the
 *   waiting room more than a spinner. Several open tabs are safe: the API leases
 *   the job, so all but one caller come straight back with `busy`.
 *
 * Failures: a single dropped request is noise. Only a run of consecutive failures
 * surfaces, and the driver backs off rather than hammering a struggling API.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { advanceJob, fetchJob } from "../lib/api";
import type { JobStatus } from "../lib/types";

export const STAGES = [
  { key: "queued", label: "In line", detail: "Starting up" },
  { key: "ingesting", label: "Reading the paper", detail: "Fetching the LaTeX source from arXiv" },
  { key: "analyzing", label: "Finding the hard ideas", detail: "Concepts, prerequisites and what deserves a visual" },
  { key: "rendering", label: "Building the visuals", detail: "Filling and checking each animation" },
  { key: "complete", label: "Ready to read", detail: "Your explainer is done" },
] as const;

const POLL_MS = 3_000;
const SLOW_POLL_MS = 8_000;
const SLOW_AFTER_MS = 150_000;
const FAILURES_BEFORE_SURFACING = 3;

export interface JobView {
  status: JobStatus | null;
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

const terminal = (s: JobStatus | null) => s?.state === "complete" || s?.state === "failed";

export function useJobStatus(jobId: string | null | undefined, { drive = true } = {}): JobView {
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsedMs, setElapsed] = useState(0);
  const [nonce, setNonce] = useState(0);
  const startedAt = useRef(Date.now());
  const latest = useRef<JobStatus | null>(null);

  const accept = useCallback((next: JobStatus) => {
    latest.current = next;
    setStatus(next);
    setError(null);
  }, []);

  // -- status poll -------------------------------------------------------- //
  useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let timer: number | undefined;
    let failures = 0;
    startedAt.current = Date.now();

    const poll = async () => {
      if (stopped) return;
      try {
        const next = await fetchJob(jobId);
        if (stopped) return;
        failures = 0;
        accept(next);
        if (terminal(next)) return;
      } catch (err) {
        failures += 1;
        if (failures >= FAILURES_BEFORE_SURFACING) {
          setError(err instanceof Error ? err.message : "Lost contact with the server.");
        }
      }
      const age = Date.now() - startedAt.current;
      timer = window.setTimeout(poll, age > SLOW_AFTER_MS ? SLOW_POLL_MS : POLL_MS);
    };
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [jobId, accept, nonce]);

  // -- driver ------------------------------------------------------------- //
  useEffect(() => {
    if (!jobId || !drive) return;
    const abort = new AbortController();
    let failures = 0;

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const id = window.setTimeout(resolve, ms);
        abort.signal.addEventListener("abort", () => {
          window.clearTimeout(id);
          resolve();
        });
      });

    void (async () => {
      // Give the first status poll a moment, so a deployment that does not step
      // (stepped === false) is detected before any advance call is made.
      await sleep(400);
      while (!abort.signal.aborted) {
        const current = latest.current;
        if (terminal(current)) return;
        if (current && current.stepped === false) return;
        try {
          const next = await advanceJob(jobId, abort.signal);
          if (abort.signal.aborted) return;
          failures = 0;
          accept(next);
          if (terminal(next)) return;
          // Busy means another caller holds the job; watch rather than queue up.
          await sleep(next.busy ? 5_000 : 600);
        } catch (err) {
          if (abort.signal.aborted) return;
          failures += 1;
          if (failures >= FAILURES_BEFORE_SURFACING) {
            setError(err instanceof Error ? err.message : "The server stopped responding.");
          }
          await sleep(Math.min(30_000, 3_000 * 2 ** Math.min(failures, 4)));
        }
      }
    })();
    return () => abort.abort();
  }, [jobId, drive, accept, nonce]);

  // A cheap ticker so the elapsed time moves between polls.
  useEffect(() => {
    if (!jobId) return;
    const id = window.setInterval(() => {
      if (!terminal(latest.current)) setElapsed(Date.now() - startedAt.current);
    }, 1_000);
    return () => window.clearInterval(id);
  }, [jobId]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return { ...derive(status), error, elapsedMs, refresh, status };
}

function derive(status: JobStatus | null) {
  const stageIndex = Math.max(
    0,
    STAGES.findIndex((s) => s.key === (status?.state ?? "queued")),
  );
  const scenes = status?.scenes ?? [];
  const scenesTotal = scenes.length;
  const scenesDone = scenes.filter((s) => ["passed", "degraded", "failed"].includes(s.state)).length;
  const failed = status?.state === "failed";
  const isTerminal = failed || status?.state === "complete";

  // Visuals dominate the wall clock, so they own most of the bar; anything else
  // would park at 90% for minutes, which reads as broken.
  let progress: number;
  if (isTerminal) progress = 1;
  else if (status?.state === "rendering" && scenesTotal > 0) progress = 0.4 + 0.58 * (scenesDone / scenesTotal);
  else progress = [0.04, 0.14, 0.3, 0.4, 1][stageIndex] ?? 0.04;

  return { progress: Math.min(1, progress), stageIndex, isTerminal, failed, scenesDone, scenesTotal };
}

export function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes === 0) return `${seconds}s`;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}
