import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { JARGON } from "@/lib/jargon";
import { SIGNAL_FORMULAS } from "@/lib/jargon";
import { Icon } from "@/components/Icon";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Inline "?" popover for numeric quant-signal metrics.
 *
 * Differs from `JargonTooltip` in that it shows a structured pop-up with
 * three sections — description (pulled from JARGON dict), formula, and a
 * outside or press Escape to close (Escape is delegated to the global overlay
 * coordinator). Pure absolute positioning — no popper lib.
 *
 * `term` keys both the `JARGON` description and the `SIGNAL_FORMULAS` map
 * (formula + example). Either lookup can miss; the popover renders only
 * the sections it has data for, so this is safe to drop anywhere.
 */
interface Props {
  term: string;
  formula?: string;
  example?: string;
  children?: ReactNode;
}

export function SignalExplainer({ term, formula, example, children }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const surfaceId = useId();
  useOverlaySurface({
    id: `signal-explainer:${surfaceId}`,
    open,
    onClose: () => setOpen(false),
    lockBody: false,
  });

  const prevOpen = useRef(open);
  useEffect(() => {
    if (open) {
      dialogRef.current?.focus();
    } else if (prevOpen.current && !open) {
      triggerRef.current?.focus();
    }
    prevOpen.current = open;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  // Pull description + formula bundle from dictionaries. Either may miss.
  const description = JARGON[term];
  const fromDict = SIGNAL_FORMULAS[term];
  const effFormula = formula ?? fromDict?.formula;
  const effExample = example ?? fromDict?.example;

  const triggerDescId = `signal-explainer-trigger-desc-${surfaceId}`;
  const dialogDescId = `signal-explainer-dialog-desc-${surfaceId}`;

  return (
    <span ref={ref} className="relative inline-block">
      {children}
      <span id={triggerDescId} className="sr-only">
        Press to toggle explanation for the {term} signal.
      </span>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => {
          if (open) {
            setOpen(false);
            return;
          }
          closeAllOverlays();
          setOpen(true);
        }}
        aria-label={`Explain ${term}`}
        aria-expanded={open}
        aria-describedby={triggerDescId}
        className="ml-1 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full bg-[color:var(--bg-elev2)] text-[color:var(--fg-muted)] hover:bg-accent hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-accent transition-colors align-middle"
      >
        <Icon name="question" size={10} />
      </button>
      {open && (
        <div
          ref={dialogRef}
          role="dialog"
          tabIndex={-1}
          aria-label={`${term} explanation`}
          aria-describedby={dialogDescId}
          className="absolute z-30 left-0 top-full mt-1 w-72 rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)] p-3 shadow-lg text-left focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
        >
          <span id={dialogDescId} className="sr-only">
            Detailed description, formula, and example for {term}. Click outside or press Escape to close.
          </span>
          <div className="text-[10px] uppercase tracking-wider text-accent font-semibold">
            {term}
          </div>
          {description && (
            <p className="mt-1 text-[11px] text-[color:var(--fg)] leading-relaxed normal-case">
              {description}
            </p>
          )}
          {effFormula && (
            <div className="mt-2 text-[10px]">
              <div className="text-[color:var(--fg-muted)] uppercase tracking-wider">
                Formula
              </div>
              <code className="block mt-0.5 font-mono text-accent text-[10.5px] break-all">
                {effFormula}
              </code>
            </div>
          )}
          {effExample && (
            <div className="mt-2 text-[10px]">
              <div className="text-[color:var(--fg-muted)] uppercase tracking-wider">
                Example
              </div>
              <p className="mt-0.5 text-[10.5px] text-[color:var(--fg-dim)] leading-relaxed normal-case">
                {effExample}
              </p>
            </div>
          )}
          {!description && !effFormula && !effExample && (
            <p className="mt-1 text-[11px] text-[color:var(--fg-muted)] italic">
              No explanation available for this signal yet.
            </p>
          )}
        </div>
      )}
    </span>
  );
}
