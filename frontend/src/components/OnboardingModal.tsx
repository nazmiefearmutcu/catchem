import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * First-run onboarding modal (v21, task #76).
 *
 * Renders ONCE on first app launch. After the user completes or dismisses
 * the flow, we persist a flag to localStorage so the modal never reopens
 * for that profile. The 4-step content introduces:
 *   1. Welcome / value prop — the asset-class + direction + how-soon framing
 *   2. Live Feed (news poller + the labels each article gets)
 *   3. Replay / Upload (analyze your own articles)
 *   4. Optional DeepSeek reviewer
 *
 * UX details:
 *   - Centered, dim backdrop, hero-gradient card capped at max-w-xl.
 *   - 4 dot indicators (current step accent-coloured + slightly larger).
 *   - Prev/Next buttons reuse the global `.btn` styling.
 *   - Final step swaps Next → "Get started" (also marks completed).
 *   - Close X top-right is `aria-label="Skip onboarding"` and *also*
 *     marks completed — once dismissed, never shown again, by design.
 *   - Keyboard: ←/→ step, Esc closes, Enter advances on final step.
 *   - Slide-in animation reuses the `modal-enter` keyframe from
 *     globals.css; `prefers-reduced-motion: reduce` is honoured via the
 *     same CSS guard that already handles ShortcutOverlay/ToastTray.
 *
 * Co-existence with other global overlays:
 *   - ShortcutOverlay listens for "?" — our handler doesn't claim it.
 *   - CommandPalette listens for Cmd/Ctrl+K — we ignore modifier keys.
 *   - The mount returns null after dismissal, so it never interferes
 *     with Outlet rendering or any subsequent modal.
 */

export const ONBOARDING_STORAGE_KEY = "catchem.onboarding.completed";

interface OnboardingStep {
  eyebrow: string;
  title: string;
  body: string;
  hint?: string;
}

export const ONBOARDING_STEPS: OnboardingStep[] = [
  {
    eyebrow: "Local-first analyst workstation",
    title: "Welcome to Catchem",
    body:
      "Catchem reads financial news as it breaks and tags every item three ways: which asset class it touches, which direction it points (bullish, bearish, or neutral), and how soon the impact is likely to land. Everything runs on your machine — no cloud calls.",
  },
  {
    eyebrow: "Step 2 of 4",
    title: "Live Feed — news, scored in real time",
    body:
      "The Live Feed polls 50+ RSS sources every few seconds. Each new article gets a finance-relevance score, asset-class labels (equities, FX, rates, crypto…), a reason code (earnings, central bank, M&A…), a direction from its sentiment, and a how-soon horizon: intraday, one day, one week, or structural.",
    hint: "Watch the green pulse dot at the top — that's your live news poller.",
  },
  {
    eyebrow: "Step 3 of 4",
    title: "Replay/Upload — analyze your own article",
    body:
      "Got a specific story to check? Open Replay/Upload, paste a URL or raw text, and Catchem extracts it, scores it, and shows you exactly which labels fired and the evidence sentences behind them.",
  },
  {
    eyebrow: "Step 4 of 4",
    title: "Deeper context with DeepSeek",
    body:
      "Want a second-opinion narrative? Settings → DeepSeek reviewer. Provide your API key, set a budget cap, and Catchem will run cross-asset synthesis on the most relevant items.",
    hint: "Optional. Stub reviewer works fine without DeepSeek.",
  },
];

function readCompleted(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_STORAGE_KEY) === "true";
  } catch {
    // Storage may be disabled (private mode, quota) — treat as completed
    // so we don't pester users on every launch.
    return true;
  }
}

function writeCompleted(): void {
  try {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "true");
  } catch {
    /* ignore quota / disabled storage — flag stays in-memory only */
  }
}

export function OnboardingModal() {
  // Pull the persisted flag once on mount. If `true`, we never render the
  // overlay this session. We *don't* watch storage for cross-tab updates —
  // first-run-onboarding is, by definition, per-profile and per-process.
  const [open, setOpen] = useState<boolean>(() => !readCompleted());
  const [step, setStep] = useState(0);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  useOverlaySurface({
    id: "onboarding-modal",
    open,
    onClose: () => setOpen(false),
    lockBody: true,
  });

  const total = ONBOARDING_STEPS.length;
  const current = useMemo(() => ONBOARDING_STEPS[step] ?? ONBOARDING_STEPS[0], [step]);
  const isFirst = step === 0;
  const isLast = step === total - 1;
  const titleId = `onboarding-step-${step}-title`;

  const close = useCallback(() => {
    writeCompleted();
    setOpen(false);
  }, []);

  const goPrev = useCallback(() => {
    setStep((s) => (s > 0 ? s - 1 : s));
  }, []);

  const goNext = useCallback(() => {
    setStep((s) => {
      if (s >= total - 1) {
        // Final step: clicking "Get started" runs through goNext which
        // delegates to `close()` via the effect below — but to keep the
        // happy path tight we also close inline.
        writeCompleted();
        setOpen(false);
        return s;
      }
      return s + 1;
    });
  }, [total]);

  // Stash focus on open so we can restore it post-dismissal. Only fires
  // when transitioning closed→open (which only happens on initial mount).
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
