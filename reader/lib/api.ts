/**
 * The reader's only server dependency: the control-plane API.
 *
 * There is no database client here, and there should never be one. The reader
 * holding no credentials is what lets it be rewritten, open-sourced, or replaced
 * by a static export without touching the pipeline — and it means a compromise of
 * this deployment leaks nothing.
 *
 * Browser calls go straight to the API's own origin (it allows this site via CORS)
 * rather than through a rewrite on this one: `/advance` legitimately runs for
 * minutes, longer than a proxy hop should be trusted to hold a connection, and the
 * API's rate limiter needs to see the reader's real address, not a proxy's.
 */

import type { JobStatus, PaperResponse, RecentPaper } from "./types";

const API_BASE = (process.env.ARCVISUAL_API_BASE ?? process.env.NEXT_PUBLIC_ARCVISUAL_API_BASE ?? "http://localhost:8000").replace(/\/$/, "");

/** Public so client components call the same API the server rendered from. */
export const PUBLIC_API_BASE = (process.env.NEXT_PUBLIC_ARCVISUAL_API_BASE ?? API_BASE).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function detailOf(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => ({}));
  const detail = body?.detail;
  if (detail && typeof detail === "object" && typeof detail.user_facing === "string") {
    return detail.user_facing;
  }
  return fallback;
}

// -- server-side reads ------------------------------------------------------ //

export async function fetchPaper(slug: string): Promise<PaperResponse | null> {
  const res = await fetch(`${API_BASE}/api/papers/${encodeURIComponent(slug)}`, {
    // Short window: a finished article is immutable, but one still being built
    // changes every few seconds, and the page polls for those changes anyway.
    next: { revalidate: 30, tags: [`paper:${slug}`] },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `paper fetch failed: ${res.status}`);
  return (await res.json()) as PaperResponse;
}

export async function fetchRecentPapers(limit = 8): Promise<RecentPaper[]> {
  try {
    const res = await fetch(`${API_BASE}/api/papers?limit=${limit}`, {
      next: { revalidate: 120, tags: ["papers"] },
    });
    if (!res.ok) return [];
    const body = (await res.json()) as { papers?: RecentPaper[] };
    return body.papers ?? [];
  } catch {
    // The landing page must render even when the API is unreachable.
    return [];
  }
}

// -- browser calls ---------------------------------------------------------- //

export async function fetchPaperFresh(slug: string): Promise<PaperResponse | null> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/papers/${encodeURIComponent(slug)}`, {
    cache: "no-store",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, `paper fetch failed: ${res.status}`);
  return (await res.json()) as PaperResponse;
}

export async function fetchJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, await detailOf(res, `job fetch failed: ${res.status}`));
  return (await res.json()) as JobStatus;
}

/**
 * Drive a job forward one bounded step. Safe to call from several tabs at once —
 * the API leases the job, so extra callers come straight back with `busy: true`.
 */
export async function advanceJob(jobId: string, signal?: AbortSignal): Promise<JobStatus> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/jobs/${encodeURIComponent(jobId)}/advance`, {
    method: "POST",
    cache: "no-store",
    signal,
  });
  if (!res.ok) throw new ApiError(res.status, await detailOf(res, `advance failed: ${res.status}`));
  return (await res.json()) as JobStatus;
}

export interface SubmitResult {
  job_id: string | null;
  call_id?: string | null;
  slug?: string | null;
  arxiv_id?: string;
  state: string;
  cached: boolean;
  resumed?: boolean;
  stepped?: boolean;
}

export async function submitPaper(url: string): Promise<SubmitResult> {
  const res = await fetch(`${PUBLIC_API_BASE}/api/jobs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) throw new ApiError(res.status, await detailOf(res, `Submission failed (${res.status}). Try again in a moment.`));
  return (await res.json()) as SubmitResult;
}

/**
 * Media URLs for video scenes (Modal deployments). Client-rendered scenes fetch
 * nothing. A relative media base means the API serves the bytes itself, so it is
 * resolved against the API's origin, not this one.
 */
export function mediaUrl(mediaBase: string, key: string | null): string {
  if (!key || !mediaBase) return "";
  const base = mediaBase.replace(/\/$/, "");
  const path = key.replace(/^\//, "");
  if (base.startsWith("/")) return `${PUBLIC_API_BASE}${base}/${path}`;
  return `${base}/${path}`;
}
