"use client";

/**
 * Scales its content down (never up) to fit the box it sits in.
 *
 * Equation width is the thing a derivation scene fights hardest — the Python
 * template's `fit_inside` exists for the same reason. Measured with a
 * ResizeObserver on both sides, so it stays right when the stage is resized, the
 * lightbox opens, or KaTeX's fonts finish loading and change the glyph widths.
 */

import { useLayoutEffect, useRef, useState } from "react";

interface Props {
  children: React.ReactNode;
  /** Fraction of the box the content may occupy. */
  widthFrac?: number;
  heightFrac?: number;
  className?: string;
  style?: React.CSSProperties;
}

export default function FitBox({
  children,
  widthFrac = 0.9,
  heightFrac = 0.62,
  className = "",
  style,
}: Props) {
  const outer = useRef<HTMLDivElement | null>(null);
  const inner = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(1);

  useLayoutEffect(() => {
    const o = outer.current;
    const i = inner.current;
    if (!o || !i) return;
    const measure = () => {
      const iw = i.offsetWidth;
      const ih = i.offsetHeight;
      if (!iw || !ih) return;
      const next = Math.min(1, (o.clientWidth * widthFrac) / iw, (o.clientHeight * heightFrac) / ih);
      setScale((prev) => (Math.abs(prev - next) > 0.005 ? next : prev));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(o);
    ro.observe(i);
    return () => ro.disconnect();
  }, [widthFrac, heightFrac]);

  return (
    <div
      ref={outer}
      className={`absolute inset-0 flex items-center justify-center ${className}`}
      style={style}
    >
      <div
        ref={inner}
        style={{
          transform: `scale(${scale})`,
          transformOrigin: "center",
          whiteSpace: "nowrap",
          flexShrink: 0,
        }}
      >
        {children}
      </div>
    </div>
  );
}
