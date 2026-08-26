import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ArcVisual",
  description: "Paper in. Explained paper out.",
  // Public permalinks are the posture (see ArcVisual-Architecture.md §12.3).
  // If that decision changes to private links, this is the first thing to flip.
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
