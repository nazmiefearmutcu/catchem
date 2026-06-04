import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  STICKY_SCORE_THRESHOLD,
  TOAST_TTL_BY_SEVERITY,
  dismissToast,
  useToastQueue,
  type ArrivalToast,
  type ToastSeverity,
} from "@/hooks/useDesktopAlerts";
import { Pill } from "@/components/Pill";
import { Icon } from "@/components/Icon";

/**
 * Top-right slide-in tray for high-relevance arrival toasts.
 *
 * Renders nothing when the queue is empty. Each toast auto-dismisses on
 * a tone-aware timer (see TOAST_TTL_BY_SEVERITY in useDesktopAlerts):
 *   success 3s / info 4s / warning 6s / error 8s.
 * Toasts with `score >= STICKY_SCORE_THRESHOLD` are sticky (no auto-dismiss).
 *
 * Interaction:
 *   - Hovering or focusing a toast pauses its dismiss timer; leaving resumes.
 *   - Clicking the body navigates to /feed/{capture_id} (triggers slide-out).
 *   - Clicking × dismisses with the same exit animation.
 *
 * Motion:
 *   - Slide-in: `.toast-enter` keyframe (220ms, translate-x-4 → 0, fade in)
 *   - Slide-out: `.toast-exit` keyframe (200ms, translate-x-0 → +4, fade out)
 *   - `prefers-reduced-motion: reduce` → animations collapse to instant
 *     (handled in globals.css; auto-dismiss timers still run so the tray
 *     doesn't pile up for users who simply asked for less motion).
 *
 * Markup: outer <div>; clickable body is its own <button> (no nesting);
 * the × is also a <button> rendered as a sibling, absolutely positioned.
 */
export function ToastTray() {
  const toasts = useToastQueue();
  const nav = useNavigate();
  // Mirror the queue locally so we can keep a toast on screen during its
  // exit animation even after the store has removed it. Map from id → toast.
  const [exiting, setExiting] = useState<Map<string, ArrivalToast>>(new Map());
  const exitTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  // When a toast leaves the store, hold its DOM for the exit animation.
  const prevIdsRef = useRef<Set<string>>(new Set());
  // Mirror of `exiting` so the toasts-deps effect can read the latest map
  // without re-running on every exiting change (which would defeat the
  // intent of write-through caching). Without this, a rapid dismiss burst
  // could observe a stale closure where `exiting.get(id)` returns undefined
  // even though a previous render added the ghost — that path then pushes
  // `undefined` into `removed` and crashes the followup
  // `scheduleExitCleanup(undefined.id)`.
  const exitingRef = useRef(exiting);
  useEffect(() => { exitingRef.current = exiting; }, [exiting]);
  useEffect(() => {
    const currentIds = new Set(toasts.map((t) => t.id));
    const removed: ArrivalToast[] = [];
    prevIdsRef.current.forEach((id) => {
      if (!currentIds.has(id)) {
        const ghost = exitingRef.current.get(id);
        // No ghost = the store dropped this id before it ever entered the
        // exit cache (rare race during rapid dismiss bursts). Skip — the
        // DOM is already gone, no cleanup needed.
        if (!ghost) return;
        removed.push(ghost);
      }
    });
    if (removed.length > 0) {
      setExiting((prev) => {
        const next = new Map(prev);
        // Already added below if dismissed from this component; this branch
        // covers store-side removals (e.g. another tab/window action).
        for (const r of removed) if (!next.has(r.id)) next.set(r.id, r);
        return next;
      });
      for (const r of removed) scheduleExitCleanup(r.id);
    }
    prevIdsRef.current = currentIds;
    // We intentionally depend ONLY on toasts: the exiting map is read via
    // exitingRef.current so this effect doesn't re-fire on every exit-cache
    // mutation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toasts]);

  // Cleanup any pending exit timers on unmount so we don't leak.
  useEffect(() => {
    return () => {
      exitTimers.current.forEach((tid) => clearTimeout(tid));
      exitTimers.current.clear();
    };
  }, []);

  const scheduleExitCleanup = useCallback((id: string) => {
    const existing = exitTimers.current.get(id);
    if (existing) clearTimeout(existing);
    const tid = setTimeout(() => {
      setExiting((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
      exitTimers.current.delete(id);
    }, 220);
    exitTimers.current.set(id, tid);
  }, []);

  // beginDismiss = mark as exiting, then ask the store to drop it. The
  // store removal lands on the next render; the exit-mirror in
  // `exiting` keeps the node alive for the slide-out window.
  const beginDismiss = useCallback((t: ArrivalToast) => {
    setExiting((prev) => {
      if (prev.has(t.id)) return prev;
      const next = new Map(prev);
      next.set(t.id, t);
      return next;
    });
    dismissToast(t.id);
    scheduleExitCleanup(t.id);
  }, [scheduleExitCleanup]);

  const handleOpen = useCallback((t: ArrivalToast) => {
    nav(`/feed/${encodeURIComponent(t.id)}`);
    beginDismiss(t);
  }, [beginDismiss, nav]);

  // Compose the visible list: store toasts first (in queue order), then
  // any toasts currently in their exit animation but already removed
  // from the store. Exit-mirrored toasts render at the bottom so they
  // visually "fall off" while live ones slot upward.
  const visible: { toast: ArrivalToast; phase: "enter" | "exit" }[] = useMemo(() => {
    const liveIds = new Set(toasts.map((t) => t.id));
    const live = toasts.map((t) => ({
      toast: t,
      phase: (exiting.has(t.id) ? "exit" : "enter") as "enter" | "exit",
    }));
    const exitOnly: { toast: ArrivalToast; phase: "exit" }[] = [];
    exiting.forEach((t, id) => {
      if (!liveIds.has(id)) exitOnly.push({ toast: t, phase: "exit" });
    });
    return [...live, ...exitOnly];
  }, [toasts, exiting]);

  if (visible.length === 0) return null;
  return (
    <div
      role="region"
      className="fixed top-3 right-3 z-40 flex w-[340px] flex-col gap-2 pointer-events-none"
      aria-label="High-relevance arrivals"
      aria-describedby="toast-tray-instructions"
    >
      <div id="toast-tray-instructions" className="sr-only">
        Active toast notifications. Press tab to navigate between toasts.
      </div>
      {visible.map(({ toast, phase }) => (
        <ToastItem
          key={toast.id}
          toast={toast}
          phase={phase}
          onOpen={handleOpen}
          onDismiss={beginDismiss}
        />
      ))}
    </div>
  );
}

// ── per-toast severity / tone helpers
//
// Reduced-motion handling: there is intentionally no JS-side
// `prefersReducedMotion()` flag here. Earlier revisions captured the value
// once at mount via `useMemo`, which meant a runtime OS-level toggle
// (System Settings → Accessibility → Reduce Motion) never propagated until
// the tray was unmounted. We now rely entirely on the CSS-layer
// `@media (prefers-reduced-motion: reduce)` rule in globals.css that
// collapses `.toast-enter` / `.toast-exit` to `animation: none`. CSS
// media-queries re-evaluate live, so flipping the OS setting takes effect
// immediately for the next animation frame.
function severityFor(t: ArrivalToast): ToastSeverity {
  if (t.severity) return t.severity;
  if (t.score >= STICKY_SCORE_THRESHOLD) return "error";
  if (t.score >= 0.75) return "warning";
  if (t.score >= 0.5) return "info";
  return "success";
}

const SEV_BORDER: Record<ToastSeverity, string> = {
  success: "border-good/60",
  info: "border-accent/60",
  warning: "border-warn/60",
  error: "border-bad/60",
};
const SEV_LABEL_COLOR: Record<ToastSeverity, string> = {
  success: "text-good",
  info: "text-good",      // "new arrival" stays green-good for the common-path
  warning: "text-warn",
  error: "text-bad",
};
const SEV_LABEL: Record<ToastSeverity, string> = {
  success: "new arrival",
  info: "new arrival",
  warning: "watchlist alert",
  error: "critical arrival",
};

function ToastItem({
  toast,
  phase,
  onOpen,
  onDismiss,
}: {
  toast: ArrivalToast;
  phase: "enter" | "exit";
  onOpen: (t: ArrivalToast) => void;
  onDismiss: (t: ArrivalToast) => void;
}) {
  const sev = severityFor(toast);
  const ttl = TOAST_TTL_BY_SEVERITY[sev];
  // Sticky behavior: critical-arrival toasts (score ≥ STICKY_SCORE_THRESHOLD)
  // stay until the analyst explicitly dismisses. They still slide-out cleanly.
  const sticky = sev === "error" && toast.score >= STICKY_SCORE_THRESHOLD;

  // Hover/focus pause state. We don't unmount the timer when paused —
  // we just stop it from firing.
  const [paused, setPaused] = useState(false);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // The auto-dismiss timer must NOT reset on every parent re-render. We read
  // `toast`/`onDismiss` through a ref inside the timer callback so the effect
  // can depend on the stable `toast.id` instead of the per-render `toast`
  // object identity. Earlier this effect depended on an inline `onDismiss`
  // closure that the parent recreated each render; during a burst (other
  // toasts entering/leaving exit) that cleared the pending timeout and
  // re-scheduled a full-TTL timer, so a still-visible toast lingered far past
  // its tone-based TTL.
  const fireRef = useRef<() => void>(() => {});
  fireRef.current = () => onDismiss(toast);

  useEffect(() => {
    // If we're in the exit phase, the parent owns cleanup; don't fight it.
    if (phase === "exit") return;
    if (sticky) return;
    if (paused) return;
    // Reduced-motion users still get auto-dismiss — they asked for less
    // motion, not less behavior. They just don't get the long lingering
    // critical-arrival glow.
    const handle = setTimeout(() => fireRef.current(), ttl);
    dismissTimer.current = handle;
    return () => {
      clearTimeout(handle);
      dismissTimer.current = null;
    };
    // toast.id (not the toast object) keeps the timer stable across the
    // parent's re-renders; fireRef always sees the freshest onDismiss/toast.
  }, [paused, phase, sticky, ttl, toast.id]);

  // Build the animation class. In reduced-motion mode the keyframes
  // collapse to instant via globals.css media-query override
  // (`@media (prefers-reduced-motion: reduce)` → `animation: none`).
  // We deliberately do NOT branch in JS — that would only sample the
  // OS preference once at mount and never react to runtime toggles.
  const animClass = phase === "exit" ? "toast-exit" : "toast-enter";

  return (
    <div
      role="status"
      aria-live={sev === "error" ? "assertive" : "polite"}
      aria-describedby={`toast-desc-${toast.id}`}
      className={`relative rounded-md border ${SEV_BORDER[sev]} bg-[color:var(--bg-elev)] shadow-soft pointer-events-auto ${animClass}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <div id={`toast-desc-${toast.id}`} className="sr-only">
        {`${SEV_LABEL[sev]} for ${toast.title} with score ${toast.score.toFixed(2)}. Click to open record, or click dismiss to close.`}
      </div>
      <button
        type="button"
        onClick={() => onOpen(toast)}
        className="block w-full text-left px-3 py-2 pr-8 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-md"
        title="Open record"
      >
        <span className="flex items-baseline gap-2 text-[10px] text-[color:var(--fg-dim)]">
          <span className={`uppercase tracking-wider ${SEV_LABEL_COLOR[sev]}`}>
            {SEV_LABEL[sev]}
          </span>
          <span className="ml-auto text-[color:var(--fg)] font-semibold">
            score {toast.score.toFixed(2)}
          </span>
        </span>
        <span className="block text-sm mt-0.5 text-[color:var(--fg)] line-clamp-2">{toast.title}</span>
        <span className="block text-[10px] text-[color:var(--fg-muted)] mt-0.5">{toast.domain}</span>
        <span className="mt-1 flex flex-wrap gap-1">
          {toast.reasons.map((r) => <Pill key={r} variant="rc">{r}</Pill>)}
          {toast.symbols.map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
        </span>
      </button>
      <button
        type="button"
        onClick={() => onDismiss(toast)}
        aria-label="Dismiss"
        className="absolute top-1 right-1 inline-flex items-center justify-center text-[color:var(--fg-muted)] hover:text-[color:var(--fg)] p-1 leading-none focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded"
      >
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}
