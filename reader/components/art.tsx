/**
 * Line-art in the house style: confident ink strokes over flat fills.
 * Decorative only — every piece is aria-hidden, and none carries information.
 */

const INK = "#151313";

type ArtProps = { className?: string; style?: React.CSSProperties };

export function Sparkle({ className = "", style, fill = "#fccc42" }: ArtProps & { fill?: string }) {
  return (
    <svg viewBox="0 0 48 48" className={className} style={style} aria-hidden="true">
      <path
        d="M24 3c1.6 9.4 4.4 14.4 21 21-16.6 6.6-19.4 11.6-21 21-1.6-9.4-4.4-14.4-21-21 16.6-6.6 19.4-11.6 21-21z"
        fill={fill}
      />
    </svg>
  );
}

export function Pencil({ className = "", style }: ArtProps) {
  return (
    <svg viewBox="0 0 420 120" className={className} style={style} aria-hidden="true">
      <g stroke={INK} strokeWidth="5" strokeLinejoin="round">
        <path d="M70 18 L400 18 Q412 18 412 30 L412 90 Q412 102 400 102 L70 102 Z" fill="#be94f5" />
        <path d="M70 18 L8 60 L70 102 Z" fill="#fff" />
        <path d="M8 60 L30 45 L30 75 Z" fill={INK} />
        <path d="M350 18 L350 102" fill="none" />
        <path d="M370 18 L370 102" fill="none" />
      </g>
    </svg>
  );
}

export function Cloud({ className = "", style }: ArtProps) {
  return (
    <svg viewBox="0 0 160 100" className={className} style={style} aria-hidden="true">
      <path
        d="M30 88 Q8 88 8 68 Q8 50 28 48 Q30 18 62 16 Q90 14 98 40 Q104 30 118 32 Q136 34 138 52 Q152 54 152 70 Q152 88 132 88 Z"
        fill="#fff"
        stroke={INK}
        strokeWidth="5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** A paper page with its lines of text, and a curve rising off it. */
export function PaperSheet({ className = "", style }: ArtProps) {
  return (
    <svg viewBox="0 0 220 220" className={className} style={style} aria-hidden="true">
      <g stroke={INK} strokeWidth="5" strokeLinejoin="round" strokeLinecap="round">
        <path d="M40 20 H140 L180 60 V200 H40 Z" fill="#fff" />
        <path d="M140 20 V60 H180" fill="#fccc42" />
        <path d="M62 86 H150 M62 110 H158 M62 134 H120" fill="none" />
        <path d="M62 176 Q96 120 130 160 T200 110" fill="none" stroke="#ff5734" strokeWidth="6" />
      </g>
    </svg>
  );
}

/** The ArcVisual mark: an arc drawn over a baseline, ending in a point. */
export function LogoMark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect x="1.5" y="1.5" width="29" height="29" rx="9" fill="#ff5734" stroke={INK} strokeWidth="2" />
      <path d="M8 22 Q16 4 24 22" fill="none" stroke="#fff" strokeWidth="3" strokeLinecap="round" />
      <circle cx="24" cy="22" r="2.6" fill="#fccc42" stroke={INK} strokeWidth="1.5" />
    </svg>
  );
}

/** Three overlapping blocks, for the closing band. */
export function Blocks({ className = "", style }: ArtProps) {
  return (
    <svg viewBox="0 0 260 220" className={className} style={style} aria-hidden="true">
      <g stroke={INK} strokeWidth="5" strokeLinejoin="round">
        <rect x="20" y="70" width="120" height="120" rx="22" fill="#be94f5" transform="rotate(-8 80 130)" />
        <rect x="110" y="40" width="120" height="120" rx="22" fill="#ff5734" transform="rotate(10 170 100)" />
        <circle cx="168" cy="160" r="46" fill="#fff" />
        <path d="M150 160 Q168 128 186 160" fill="none" strokeLinecap="round" />
        <circle cx="186" cy="160" r="5" fill={INK} />
      </g>
    </svg>
  );
}
