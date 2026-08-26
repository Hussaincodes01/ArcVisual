"use client";

/**
 * Scroll-triggered reveal, used throughout the landing page.
 *
 * IntersectionObserver, not a scroll listener: a scroll handler fires on every frame
 * and would compete with the page's own animations. The observer fires once per
 * element and then disconnects.
 *
 * `once` defaults to true because an element that re-animates every time it re-enters
 * the viewport reads as a glitch when a reader scrolls back to re-read something —
 * and re-reading is exactly what an explanatory article is for.
 */

import { useEffect, useRef, useState } from "react";

interface Props {
  children: React.ReactNode;
  /** Stagger within a group, in ms. */
  delay?: number;
  once?: boolean;
  className?: string;
  as?: "div" | "section" | "li" | "article";
}

export default function Reveal({
  children,
  delay = 0,
  once = true,
  className = "",
  as: Tag = "div",
}: Props) {
  const ref = useRef<HTMLElement | null>(null);
  const [shown, setShown] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    // Reduced motion: show immediately. The content is the point; the transition
    // was only ever decoration.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setShown(true);
      return;
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true);
          if (once) observer.disconnect();
        } else if (!once) {
          setShown(false);
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [once]);

  return (
    <Tag
      ref={ref as never}
      className={`reveal ${shown ? "reveal-in" : ""} ${className}`}
      style={{ transitionDelay: `${delay}ms` }}
    >
      {children}
    </Tag>
  );
}
