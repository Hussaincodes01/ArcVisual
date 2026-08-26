"use client";

/**
 * The waiting room route: `/watch/:jobId`.
 *
 * A client component on purpose — it exists to poll. The article route stays a server
 * component so its prose is in the initial HTML; this page has nothing to render until
 * the first poll returns, so there is no server-rendering benefit to trade away.
 *
 * On completion it redirects to the article rather than rendering it here, so the
 * permalink a reader shares is `/p/:slug` and never a job id that will mean nothing to
 * anyone else.
 */

import { use, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import PipelineProgress from "../../../components/PipelineProgress";
import { useJobStatus } from "../../../hooks/useJobStatus";

export default function WatchPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = use(params);
  const search = useSearchParams();
  const slugHint = search.get("slug");
  const view = useJobStatus(jobId);

  const slug =
    slugHint ??
    (typeof view.status?.stage_progress?.slug === "string"
      ? (view.status.stage_progress.slug as string)
      : null);

  // Hand off to the permalink once the article exists. `replace` rather than `push`
  // so the browser back button returns to the submit page, not to a finished job.
  useEffect(() => {
    if (view.status?.state === "complete" && slug) {
      const timeout = window.setTimeout(() => {
        window.location.replace(`/p/${slug}?job=${jobId}`);
      }, 900); // a beat, so the completed state is visible rather than a flash
      return () => window.clearTimeout(timeout);
    }
  }, [view.status?.state, slug, jobId]);

  // Ingest resolves the title within seconds, so show it as soon as it exists —
  // otherwise the header reads "Reading your paper" while the stage list already
  // says "Animating", which makes the page look like it is not tracking anything.
  const title =
    typeof view.status?.stage_progress?.title === "string"
      ? (view.status.stage_progress.title as string)
      : undefined;

  return (
    <main>
      <PipelineProgress {...view} slug={slug} title={title} />
    </main>
  );
}
