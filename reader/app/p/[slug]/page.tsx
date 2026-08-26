/**
 * The article route — the only route Phase 1 ships.
 *
 * A server component so the prose is in the initial HTML: the plan promises a
 * readable article at 90 seconds while animations are still rendering, and that is
 * only true if the text does not wait on client-side JavaScript.
 */

import { notFound } from "next/navigation";
import type { Metadata } from "next";
import { fetchPaper } from "../../../lib/api";
import { schemaSupported } from "../../../lib/types";
import Article from "../../../components/Article";

interface Props {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ job?: string }>;
}

export const revalidate = 300;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const data = await fetchPaper(slug).catch(() => null);
  if (!data) return { title: "ArcVisual" };
  const { paper } = data.storyboard;
  return {
    title: `${paper.title} — ArcVisual`,
    description: paper.abstract.slice(0, 200),
    openGraph: {
      title: paper.title,
      description: paper.abstract.slice(0, 200),
      type: "article",
    },
  };
}

export default async function PaperPage({ params, searchParams }: Props) {
  const { slug } = await params;
  const { job } = await searchParams;

  const data = await fetchPaper(slug);
  if (!data) notFound();

  if (!schemaSupported(data.storyboard)) {
    // A document from a newer pipeline than this reader understands. Say so
    // rather than mis-rendering it — a half-rendered article is worse than none.
    return (
      <main className="mx-auto max-w-2xl px-6 py-24">
        <h1 className="text-2xl font-semibold">This article needs a newer reader</h1>
        <p className="mt-4 text-muted">
          It was built with storyboard schema {data.storyboard.schema_version}, which
          this deployment does not support yet.
        </p>
        <a
          className="mt-6 inline-block text-accent underline decoration-dotted"
          href={data.storyboard.paper.origin_url}
        >
          Read the original paper →
        </a>
      </main>
    );
  }

  return (
    <main>
      <Article
        storyboard={data.storyboard}
        mediaBase={data.media_base}
        jobId={job}
      />
    </main>
  );
}
