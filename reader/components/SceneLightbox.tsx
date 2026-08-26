"use client";

/**
 * Open one scene at the full size of the viewport.
 *
 * A dense diagram or a multi-term equation is legible in a column at reading width
 * only up to a point. The template already fills its frame and keeps text above the
 * legibility floor, but "above the floor" at 1080p becomes small when the video is
 * painted into a 500px column. Rather than trade the reading layout away for the
 * rare wide scene, this lets the reader enlarge the one they care about.
 *
 * Deliberately not the browser's native fullscreen API: that hides the claim and the
 * provenance chip, which are the parts that make the visual trustworthy. This keeps
 * them on screen underneath.
 *
 * Accessibility is the whole job of the rest of this file — a modal that traps focus
 * badly is worse than no modal. Escape closes, focus moves in and is restored on
 * close, background scroll is locked, and the backdrop is click-to-dismiss.
 */

import { useCallback, useEffect, useRef } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  claim: string;
  sectionHeading: string;
  mp4: string;
  webm?: string;
  poster?: string;
  reducedMotion: boolean;
}

export default function SceneLightbox({
  open,
  onClose,
  claim,
  sectionHeading,
  mp4,
  webm,
  poster,
  reducedMotion,
}: Props) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusTo = useRef<HTMLElement | null>(null);

  const handleKey = useCallback(
    (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      // Minimal focus trap. Only two focusables live here (close, video controls),
      // so cycling between them is enough — no need for a generic trap library.
      if (event.key === "Tab" && dialogRef.current) {
        const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], video[controls], [tabindex]:not([tabindex="-1"])'
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    },
    [onClose]
  );

  useEffect(() => {
    if (!open) return;

    restoreFocusTo.current = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", handleKey, true);
    closeRef.current?.focus();

    return () => {
      document.removeEventListener("keydown", handleKey, true);
      document.body.style.overflow = previousOverflow;
      // Return focus to whatever opened this, or the reader loses their place.
      restoreFocusTo.current?.focus?.();
    };
  }, [open, handleKey]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-canvas/95 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={`Enlarged animation: ${claim}`}
      onClick={onClose}
      ref={dialogRef}
    >
      <div className="mb-3 flex shrink-0 items-start justify-between gap-4">
        <p className="text-xs uppercase tracking-widest text-muted">
          from “{sectionHeading}”
        </p>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          className="rounded border border-border px-3 py-1 text-sm text-muted transition-colors hover:border-accent hover:text-accent"
        >
          Close ✕
        </button>
      </div>

      {/* stopPropagation so clicking the video does not dismiss the dialog. */}
      <div
        className="flex min-h-0 flex-1 items-center justify-center"
        onClick={(e) => e.stopPropagation()}
      >
        <video
          className="max-h-full max-w-full rounded-md border border-border bg-canvas"
          poster={poster || undefined}
          autoPlay={!reducedMotion}
          muted
          loop
          playsInline
          controls
        >
          {webm ? <source src={webm} type="video/webm" /> : null}
          {mp4 ? <source src={mp4} type="video/mp4" /> : null}
        </video>
      </div>

      {/* The claim stays visible: an enlarged visual without its argument is just a
          picture, and the argument is what makes it checkable. */}
      <p className="mx-auto mt-3 max-w-3xl shrink-0 text-center text-sm text-fg">
        {claim}
      </p>
      <p className="mt-1 shrink-0 text-center text-xs text-muted">
        Press Escape or click outside to close
      </p>
    </div>
  );
}
