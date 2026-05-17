import { useNavigate } from "react-router-dom";
import { dismissToast, useToastQueue } from "@/hooks/useDesktopAlerts";
import { Pill } from "@/components/Pill";

/**
 * Top-right slide-in tray for high-relevance arrival toasts.
 *
 * Renders nothing when the queue is empty. Each toast auto-dismisses
 * after TOAST_TTL_MS (defined in useDesktopAlerts). Clicking the body
 * navigates to /feed/{capture_id}. × button dismisses.
 *
 * Markup: outer <div>; clickable body is its own <button> (no nesting);
 * the × is also a <button> rendered as a sibling, absolutely positioned.
 */
export function ToastTray() {
  const toasts = useToastQueue();
  const nav = useNavigate();
  if (toasts.length === 0) return null;
  return (
    <div
      className="fixed top-3 right-3 z-40 flex w-[340px] flex-col gap-2 pointer-events-none"
      aria-live="polite"
      aria-label="High-relevance arrivals"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className="animate-slide-in relative rounded-md border border-accent/60 bg-[color:var(--bg-elev)] shadow-soft pointer-events-auto"
        >
          <button
            type="button"
            onClick={() => { nav(`/feed/${encodeURIComponent(t.id)}`); dismissToast(t.id); }}
            className="block w-full text-left px-3 py-2 pr-8"
            title="Open record"
          >
            <span className="flex items-baseline gap-2 text-[10px] text-[color:var(--fg-dim)]">
              <span className="uppercase tracking-wider text-good">new arrival</span>
              <span className="ml-auto text-[color:var(--fg)] font-semibold">
                score {t.score.toFixed(2)}
              </span>
            </span>
            <span className="block text-sm mt-0.5 text-[color:var(--fg)] line-clamp-2">{t.title}</span>
            <span className="block text-[10px] text-[color:var(--fg-muted)] mt-0.5">{t.domain}</span>
            <span className="mt-1 flex flex-wrap gap-1">
              {t.reasons.map((r) => <Pill key={r} variant="rc">{r}</Pill>)}
              {t.symbols.map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
            </span>
          </button>
          <button
            type="button"
            onClick={() => dismissToast(t.id)}
            aria-label="Dismiss"
            className="absolute top-1 right-1 text-[color:var(--fg-muted)] hover:text-[color:var(--fg)] px-1.5 text-sm leading-none"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
