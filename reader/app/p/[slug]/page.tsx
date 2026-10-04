/**
 * The article route. A server component, so the prose is in the initial HTML:
 * readable before any JavaScript runs, and indexable.
 */

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Article from "../../../components/Article";
import SiteHeader from "../../../components/SiteHeader";
import { fetchPaper } from "../../../lib/api";
import { plainText } from "../../../lib/tex";
import { schemaSupported } from "../../../lib/types";

interface Props {
  params: Promise<{ slug: string }>;
}

export const revalidate = 30;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const data = await fetchPaper(slug).catch(() => null);
  if (!data) return { title: "Explainer not found" };
  const { paper } = data.storyboard;
  // Browser tabs and link previews cannot typeset math; show it readably instead.
  const title = plainText(paper.title, paper.tex_macros);
  const description = plainText(paper.abstract.slice(0, 200), paper.tex_macros);
  return {
    title,
    description,
    openGraph: { title: `${title}, explained`, description, type: "article" },
  };
}

export default async function PaperPage({ params }: Props) {
  const { slug } = await params;
  const data = await fetchPaper(slug);
  if (!data) notFound();

  if (!schemaSupported(data.storyboard)) {
    return (
      <>
        <SiteHeader />
        <main className="mx-auto max-w-2xl px-6 py-24">
          <h1 className="text-3xl font-semibold tracking-tight">This explainer needs a newer reader</h1>
          <p className="mt-4 text-ink-soft">
            It was built with storyboard version {data.storyboard.schema_version}, which this site cannot display yet.
          </p>
          <a className="btn btn-coral mt-8" href={data.storyboard.paper.origin_url}>
            Read the paper on arXiv
          </a>
        </main>
      </>
    );
  }

  return (
    <>
      <SiteHeader />
      <main>
        <Article initial={data} />
      </main>
    </>
  );
}
