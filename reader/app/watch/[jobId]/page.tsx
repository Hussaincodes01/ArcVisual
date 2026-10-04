"use client";

/**
 * The waiting room: `/watch/:jobId`.
 *
 * It follows the job and, on a serverless deployment, drives it forward. When the
 * article exists it hands off to the permalink, so what a reader shares is
 * `/p/:slug` and never a job id that means nothing to anyone else.
 */

import { use, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import PipelineProgress from "../../../components/PipelineProgress";
import SiteHeader from "../../../components/SiteHeader";
import { useJobStatus } from "../../../hooks/useJobStatus";

export default function WatchPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = use(params);
  const search = useSearchParams();
  const view = useJobStatus(jobId);
  const progressSlug = view.status?.stage_progress?.slug;
  const slug = typeof progressSlug === "string" ? progressSlug : search.get("slug");
  const title = view.status?.stage_progress?.title;

  useEffect(() => {
    if (view.status?.state === "complete" && slug) {
      // A beat, so the finished state registers rather than flashing past.
      const id = window.setTimeout(() => window.location.replace(`/p/${slug}`), 900);
      return () => window.clearTimeout(id);
    }
  }, [view.status?.state, slug]);

  return (
    <>
      <SiteHeader cta={false} />
      <main>
        <PipelineProgress {...view} slug={slug} title={typeof title === "string" ? title : undefined} />
      </main>
    </>
  );
}
