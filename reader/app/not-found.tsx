import SiteHeader from "../components/SiteHeader";

export default function NotFound() {
  return (
    <>
      <SiteHeader />
      <main className="mx-auto max-w-2xl px-6 py-24">
        <div className="rounded-[2rem] border-[1.5px] border-ink bg-paper p-8 sm:p-12">
          <span className="pill bg-mustard text-xs">Not found</span>
          <h1 className="mt-4 text-3xl font-semibold tracking-tight">There is no explainer at this address</h1>
          <p className="mt-4 leading-relaxed text-ink-soft">
            The link may be mistyped, or the paper has not been explained yet. Paste its arXiv link and it will be.
          </p>
          <a href="/#start" className="btn btn-coral mt-8">
            Explain a paper
          </a>
        </div>
      </main>
    </>
  );
}
