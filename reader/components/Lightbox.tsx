"use client";

/**
 * A modal built on the native <dialog>: focus trapping, Escape to close and
 * `inert` on the page behind it come from the browser, not from code here.
 */

import { useEffect, useRef } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
}

export default function Lightbox({ open, onClose, title, children }: Props) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose(); // click on the backdrop
      }}
      aria-label={title}
      className="m-auto w-[min(1100px,94vw)] rounded-[1.6rem] border-[1.5px] border-ink bg-canvas p-0 text-ink backdrop:bg-ink/55 backdrop:backdrop-blur-[2px]"
    >
      <div className="p-4 sm:p-6">
        <div className="mb-4 flex items-start justify-between gap-4">
          <p className="text-lg font-medium leading-snug">{title}</p>
          <button
            type="button"
            onClick={onClose}
            className="flex size-9 shrink-0 items-center justify-center rounded-full border-[1.5px] border-ink bg-paper hover:bg-wash"
            aria-label="Close"
          >
            <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
              <path d="M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        {open ? children : null}
      </div>
    </dialog>
  );
}
