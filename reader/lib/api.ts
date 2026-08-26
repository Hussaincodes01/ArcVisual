/**
 * The reader's only server dependency: the control-plane API and the R2 CDN.
 *
 * There is no database client here, and there should never be one. The reader
 * holding no credentials is what lets it be rewritten, open-sourced, or replaced
 * by a static export without touching the pipeline — and it means a compromise of
 * this deployment leaks nothing.
 */

import type { JobStatus, PaperResponse } from "./types";

const API_BASE = (process.env.ARCVISUAL_API_BASE ?? "http://localhost:8000").replace(
  /\/$/,
  ""
);

/** Public so the client component can poll the same origin it was served from. */
export const PUBLIC_API_BASE = (
  process.env.NEXT_PUBLIC_ARCVISUAL_API_BASE ?? API_BASE
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message);
  }
}

export async function fetchPaper(slug: string): Promise<PaperResponse | null> {
  const res = await fetch(`${API_BASE}/api/papers/${encodeURIComponent(slug)}`, {
    // ISR keyed on the slug. Revalidation is cheap and a completed article is
    // immutable at a given pipeline version, so a long window is safe.
    next: { revalidate: 300, tags: [`paper:${slug}`] },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `paper fetch failed: ${res.status}`);
  return (await res.json()) as PaperResponse;
}

export async function fetchJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, `job fetch failed: ${res.status}`);
  return (await res.json()) as JobStatus;
}

export interface SubmitResult {
  /** Present when a job row already exists (a cache hit). */
  job_id: string | null;
  /** Modal call id, for tracing only. NOT pollable — `/api/jobs/:id` takes a
   *  `job_id`, and the two are different identifiers. */
  call_id?: string | null;
  slug?: string | null;
  arxiv_id?: string;
  state: string;
  cached: boolean;
}

export async function submitPaper(url: string): Promise<SubmitResult> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body?.detail ?? {};
    throw new ApiError(
      res.status,
      detail.user_facing ?? `submission failed (${res.status})`
    );
  }
  return body as SubmitResult;
}

/**
 * Media URLs come from the CDN, never from R2 directly — the bucket is private.
 * An empty `mediaBase` means the deployment is misconfigured, so we return an
 * empty string and let the poster-only fallback render rather than emitting a
 * broken origin URL.
 */
export function mediaUrl(mediaBase: string, key: string | null): string {
  if (!key || !mediaBase) return "";
  const base = mediaBase.replace(/\/$/, "");
  const path = key.replace(/^\//, "");
  // A RELATIVE media base means the API is serving the bytes itself (no CDN
  // configured), so it has to be resolved against the API's origin — not the
  // reader's. They are different hosts in every deployment except a single-origin
  // one, so treating "/api/media" as same-origin points the player at the Next.js
  // server, which has no such route and returns its 404 page as a video.
  if (base.startsWith("/")) {
    return `${PUBLIC_API_BASE.replace(/\/$/, "")}${base}/${path}`;
  }
  return `${base}/${path}`;
}
