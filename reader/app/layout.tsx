import type { Metadata, Viewport } from "next";
import { Lexend } from "next/font/google";
// KaTeX typesets equations on every page (the hero scene, articles), and without its
// stylesheet the hidden MathML copy of each formula shows beside the visible one.
import "katex/dist/katex.min.css";
import "./globals.css";

/*
 * One family, chosen for the job: Lexend was designed to measurably improve reading
 * fluency, which is the whole point of an explainer. Self-hosted by next/font at
 * build time, so there is no request to a font CDN at runtime.
 */
const lexend = Lexend({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  display: "swap",
  variable: "--font-lexend",
});

const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://arcvisual.vercel.app";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: {
    default: "ArcVisual — see the idea inside any arXiv paper",
    template: "%s · ArcVisual",
  },
  description:
    "Paste an arXiv link and get a readable explainer where the hardest ideas are animated, and every claim points back to the paper's own text.",
  openGraph: {
    type: "website",
    siteName: "ArcVisual",
    title: "ArcVisual — see the idea inside any arXiv paper",
    description:
      "Animated, grounded explainers for arXiv papers. Built for researchers, students and educators.",
  },
  // Public permalinks are the posture (see ArcVisual-Architecture.md §12.3).
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: "#f7f7f5",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={lexend.variable}>
      <body className="min-h-dvh">{children}</body>
    </html>
  );
}
