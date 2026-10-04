import Link from "next/link";
import { LogoMark } from "./art";

export function Logo({ className = "" }: { className?: string }) {
  return (
    <Link href="/" className={`inline-flex items-center gap-2.5 ${className}`} aria-label="ArcVisual home">
      <LogoMark className="size-8" />
      <span className="text-xl font-semibold tracking-tight">
        <span className="text-coral">Arc</span>Visual
      </span>
    </Link>
  );
}

const LINKS = [
  { href: "/#explainers", label: "Explainers" },
  { href: "/#how", label: "How it works" },
  { href: "/#visuals", label: "Visuals" },
  { href: "/#audience", label: "Who it's for" },
];

export default function SiteHeader({ cta = true }: { cta?: boolean }) {
  return (
    <header className="mx-auto flex max-w-7xl items-center justify-between gap-6 px-4 py-5 sm:px-6 lg:px-10">
      <Logo />
      <nav aria-label="Main" className="hidden items-center gap-8 text-[0.95rem] lg:flex">
        {LINKS.map((l) => (
          <a key={l.href} href={l.href} className="text-ink-soft transition-colors hover:text-ink">
            {l.label}
          </a>
        ))}
      </nav>
      {cta ? (
        <a href="/#start" className="btn btn-coral px-4 py-2.5 text-[0.95rem]">
          Explain a paper
        </a>
      ) : (
        <span />
      )}
    </header>
  );
}
