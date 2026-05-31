import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOverlaySurface } from "@/context/overlayCoordinator";
import {
  OPEN_ONBOARDING_EVENT,
  ONBOARDING_STORAGE_KEY as STORAGE_KEY,
  hasSeenOnboarding,
  markOnboardingSeen,
} from "@/lib/onboarding";

/**
 * First-run onboarding modal (v21, task #76).
 *
 * Renders automatically on first app launch. After the user completes or
 * dismisses the flow we persist a flag to localStorage (via lib/onboarding)
 * so the modal never reopens by itself for that profile. The 4-step content
 * walks the analyst through what Catchem does and the three views they'll
 * use most:
 *   1. Welcome / value prop — the asset-class + direction + how-soon framing
 *   2. Live Feed (news poller + the labels each article gets)
 *   3. Replay / Upload (analyze your own articles)
 *   4. Finding your way around — nav chords, command palette, the floating
 *      "?" help, and how to replay this tour from Help.
 *
 * Re-opening: this is also a controlled surface. Any time the global
 * `catchem:open-onboarding` event fires (Help page "Replay welcome tour"
 * button, command palette, a future menu item) the modal re-opens from
 * step 1 WITHOUT a page reload. See lib/onboarding.requestOpenOnboarding.
 *
 * UX details:
 *   - Centered, dim backdrop, hero-gradient card capped at max-w-xl.
 *   - 4 dot indicators (current step accent-coloured + slightly larger).
 *   - Prev/Next buttons reuse the global `.btn` styling.
 *   - Final step swaps Next → "Get started" (also marks completed).
 *   - Close X top-right is `aria-label="Skip onboarding"` and *also*
 *     marks completed — once dismissed, never shown again on its own.
 *   - Keyboard: ←/→ step, Esc closes, Enter advances on final step.
 *   - Slide-in animation reuses the `modal-enter` keyframe from
 *     globals.css; `prefers-reduced-motion: reduce` is honoured via the
 *     same CSS guard that already handles ShortcutOverlay/ToastTray.
 *
 * Co-existence with other global overlays:
 *   - ShortcutOverlay listens for "?" — our handler doesn't claim it.
 *   - CommandPalette listens for Cmd/Ctrl+K — we ignore modifier keys.
 *   - The mount returns null while closed, so it never interferes with
 *     Outlet rendering or any subsequent modal.
 */

// Re-exported for back-compat: existing imports (CommandPalette, snapshot,
// tests) reference `ONBOARDING_STORAGE_KEY` from this module. The canonical
// definition now lives in lib/onboarding.ts.
export const ONBOARDING_STORAGE_KEY = STORAGE_KEY;

interface OnboardingStep {
  eyebrow: string;
  title: string;
  body: string;
  hint?: string;
}

export const ONBOARDING_STEPS: OnboardingStep[] = [
  {
    eyebrow: "Local-first · runs on your machine",
    title: "Welcome to Catchem",
    body:
      "Catchem reads financial news and RSS on your laptop as stories break, then tags every item three ways: which asset class it would move, which direction it points (bullish, bearish, or neutral), and how soon the impact is likely to land. Core ingestion and storage stay on your machine; optional DeepSeek review and narrative calls only run when enabled.",
  },
  {
    eyebrow: "Step 2 of 4 · the view you'll live in",
    title: "Live Feed — news, tagged as it arrives",
    body:
      "The Live Feed polls 50+ public news sources in the background. Each article gets a finance-relevance score, asset-class labels (equities, FX, rates, crypto…), a reason code (earnings, central bank, M&A…), a direction, and a how-soon horizon. Use the sidebar filters to narrow by class, reason, or symbol.",
    hint: "The pulsing dot in the header is your live poller. Press g f to jump here any time.",
  },
  {
    eyebrow: "Step 3 of 4 · check a specific story",
    title: "Replay / Upload — analyze your own article",
    body:
      "Have a story you want scored? Open Replay/Upload, paste a URL or raw text (or drop a file), and Catchem extracts it, tags it, and shows the exact evidence sentences behind each label. Results land in your Feed and persist locally.",
    hint: "Press g r for Replay/Upload.",
  },
  {
    eyebrow: "Step 4 of 4 · finding your way",
    title: "Getting around",
    body:
      "Press g then a letter to jump between views (g o Overview, g f Feed, g s Symbols…). Hit ⌘K for the command palette, and ? any time for the full shortcut list. Every page has a floating ? in the bottom-right with tips for that screen.",
    hint: "Replay this tour whenever you like from Help (g h) → Replay welcome tour.",
  },
];

export function OnboardingModal() {
  // Pull the persisted flag once on mount. If `true`, we don't render the
  // overlay on its own — but we still mount (returning null) so the
  // `catchem:open-onboarding` listener below can re-open the tour on
  // demand. We *don't* watch storage for cross-tab updates — first-run
  // onboarding is, by definition, per-profile and per-process.
  const [open, setOpen] = useState<boolean>(() => !hasSeenOnboarding());
  const [step, setStep] = useState(0);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const openRef = useRef(open);
  // `close()` (declared below) persists the "seen" flag AND hides the modal.
  // We thread it through a ref so the coordinator-driven Escape path also
  // marks onboarding seen — otherwise a first-run user who dismisses the tour
  // with Esc would see it again on the next cold launch, contradicting the
  // "once dismissed, never shown again on its own" contract. The X button and
  // "Get started" already mark seen; Esc went through the bare setOpen(false).
  const closeRef = useRef<() => void>(() => setOpen(false));
  useOverlaySurface({
    id: "onboarding-modal",
    open,
    onClose: () => closeRef.current(),
    lockBody: true,
  });

  const total = ONBOARDING_STEPS.length;
  const current = useMemo(() => ONBOARDING_STEPS[step] ?? ONBOARDING_STEPS[0], [step]);
  const isFirst = step === 0;
  const isLast = step === total - 1;
  const titleId = `onboarding-step-${step}-title`;

  useEffect(() => {
    openRef.current = open;
  }, [open]);

  const close = useCallback(() => {
    markOnboardingSeen();
    setOpen(false);
  }, []);
  // Keep the coordinator's onClose pointed at the seen-persisting `close`
  // so the global Escape handler (overlayCoordinator) marks onboarding seen.
  closeRef.current = close;

  const goPrev = useCallback(() => {
    setStep((s) => (s > 0 ? s - 1 : s));
  }, []);

  const goNext = useCallback(() => {
    setStep((s) => {
      if (s >= total - 1) {
        // Final step: clicking "Get started" runs through goNext which
        // delegates to `close()` via the effect below — but to keep the
        // happy path tight we also close inline.
        markOnboardingSeen();
        setOpen(false);
        return s;
      }
      return s + 1;
    });
  }, [total]);

  // On-demand re-open. Any surface can dispatch `catchem:open-onboarding`
  // (Help page button, command palette, native menu) to replay the tour
  // from the top with no page reload. We reset to step 0 so a replay
  // always starts at the welcome card, and we don't touch the "seen"
  // flag — replaying a tour you've already seen shouldn't make it pop
  // again next launch.
  useEffect(() => {
    const onOpen = () => {
      if (openRef.current) return;
      lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
      setStep(0);
      setOpen(true);
    };
    window.addEventListener(OPEN_ONBOARDING_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_ONBOARDING_EVENT, onOpen);
  }, []);

  // Stash focus on open so we can restore it post-dismissal, and move
  // focus into the dialog. Fires on the initial first-run open and on
  // every on-demand re-open (the open-event handler pre-stashes the
  // trigger; this confirms it once `open` flips true).
  useEffect(() => {
    if (open) {
      lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
      // Defer to next tick so the dialog is mounted before we move focus.
      const t = setTimeout(() => {
        primaryRef.current?.focus();
      }, 0);
      return () => clearTimeout(t);
    }
    const prev = lastFocusedRef.current;
    if (prev && typeof prev.focus === "function") {
      try {
        prev.focus();
      } catch {
        /* element may be gone — ignore */
      }
    }
    return;
  }, [open]);

  // Re-focus the primary button when the step changes so SR users hear
  // the new step's title (via aria-labelledby) without manual re-tab.
  useEffect(() => {
    if (!open) return;
    primaryRef.current?.focus();
  }, [open, step]);

  // Keyboard nav. Ignore key events when the user is typing in an input
  // anywhere — but our trap means the only focusable elements are the
  // dialog's own buttons, so this is mostly defensive.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      if (target && (/^(input|textarea|select)$/i.test(target.tagName) || target.isContentEditable)) {
        return;
      }
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        goPrev();
        return;
      }
      if (e.key === "ArrowRight") {
        e.preventDefault();
        goNext();
        return;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, close, goPrev, goNext]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 overflow-y-auto"
      data-testid="onboarding-backdrop"
      // Backdrop click is intentionally a no-op — first-run onboarding is
      // important enough that an accidental misclick shouldn't dismiss it.
      // Users dismiss via the X (top-right) or the Get-started button.
    >
      <div
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid="onboarding-card"
        className="relative w-full max-w-xl rounded-xl border border-accent/40 hero-gradient shadow-soft animate-modal-enter overflow-hidden my-8"
      >
        {/* Hero accent blob to match ShortcutOverlay / Welcome page. */}
        <div
          aria-hidden
          className="pointer-events-none absolute -top-24 -left-20 h-56 w-56 rounded-full bg-accent/20 blur-3xl"
        />

        {/* Close X — also marks completed so a "Skip" path exists. */}
        <button
          type="button"
          onClick={close}
          aria-label="Skip onboarding"
          data-testid="onboarding-skip"
          className="btn absolute right-3 top-3 z-10"
        >
          ×
        </button>

        <div className="relative p-7">
          <div className="flex items-start gap-3 mb-2">
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              {current.eyebrow}
            </div>
            {isFirst && (
              <span
                aria-hidden
                className="relative inline-flex h-2 w-2 mt-1"
                data-testid="onboarding-ping"
              >
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
              </span>
            )}
          </div>

          <h2
            id={titleId}
            className="text-2xl font-semibold tracking-tight mb-3"
          >
            {current.title}
          </h2>

          <p className="text-sm leading-relaxed text-[color:var(--fg-dim)]">
            {current.body}
          </p>

          {current.hint && (
            <p className="mt-4 text-[11px] text-[color:var(--fg-muted)] italic border-l-2 border-accent/40 pl-3">
              {current.hint}
            </p>
          )}

          {/* Step indicator — 4 dots, current is accent + larger. */}
          <div
            className="mt-6 flex items-center justify-center gap-2"
            role="tablist"
            aria-label="Onboarding progress"
            data-testid="onboarding-dots"
          >
            {ONBOARDING_STEPS.map((_, idx) => {
              const active = idx === step;
              return (
                <button
                  key={idx}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  aria-label={`Go to step ${idx + 1} of ${total}`}
                  data-testid={`onboarding-dot-${idx}`}
                  data-active={active ? "1" : "0"}
                  onClick={() => setStep(idx)}
                  className={`rounded-full transition-all ${
                    active
                      ? "h-2.5 w-2.5 bg-accent"
                      : "h-1.5 w-1.5 bg-[color:var(--fg-muted)]/50 hover:bg-[color:var(--fg-dim)]"
                  }`}
                />
              );
            })}
          </div>

          {/* Footer row: prev / step counter / next-or-finish */}
          <div className="mt-6 flex items-center justify-between gap-3">
            <button
              type="button"
              onClick={goPrev}
              disabled={isFirst}
              className="btn disabled:opacity-40 disabled:cursor-not-allowed"
              data-testid="onboarding-prev"
            >
              ← Back
            </button>
            <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
              {step + 1} / {total}
            </span>
            <button
              ref={primaryRef}
              type="button"
              onClick={goNext}
              className="btn btn-accent"
              data-testid={isLast ? "onboarding-finish" : "onboarding-next"}
            >
              {isLast ? "Get started" : "Next →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default OnboardingModal;
