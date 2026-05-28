import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  NOTIFICATION_CATEGORIES,
  STICKY_SCORE_THRESHOLD,
  clearNotificationHistory,
  markNotificationsRead,
  useNotificationHistory,
  useNotificationsViewedAt,
  type ArrivalToast,
  type NotificationCategory,
  type ToastSeverity,
} from "@/hooks/useDesktopAlerts";
import { Pill } from "@/components/Pill";
import { Icon } from "@/components/Icon";
import { useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Notification Center modal (v37, task #142).
 *
 * Aggregates the ephemeral ToastTray queue into a browsable, persisted
 * history (last 50). Triggered by either the bell icon in Shell's header
 * or the `g n` keyboard chord. Bell badge surfaces unread count vs the
 * last-viewed timestamp; opening the modal marks all as read.
 *
 * UX deliberately NOT focus-trapping: the analyst can keep scrolling the
 * page behind the modal while reviewing history. Esc closes, click on
 * backdrop closes. Row click navigates to /feed/<capture_id> for "toast"
 * items that carry one (id is the capture_id for arrival toasts).
 *
 * Filter chips: All / Toast / Webhook / System — chip state lives in
 * component state, not persisted, since the analyst usually wants to
 * default back to "All" on every open.
 */

export type NotificationFilter = "all" | NotificationCategory;

const FILTER_LABEL: Record<NotificationFilter, string> = {
  all: "All",
  toast: "Toast",
  webhook: "Webhook",
  system: "System",
};

/** Resolve the severity used for the row's left-edge stripe + icon. */
function severityFor(t: ArrivalToast): ToastSeverity {
  if (t.severity) return t.severity;
  if (t.score >= STICKY_SCORE_THRESHOLD) return "error";
  if (t.score >= 0.75) return "warning";
  if (t.score >= 0.5) return "info";
  return "success";
}

const SEV_BORDER: Record<ToastSeverity, string> = {
  success: "border-l-good/70",
  info: "border-l-accent/70",
  warning: "border-l-warn/70",
  error: "border-l-bad/70",
};

const SEV_ICON: Record<ToastSeverity, "info" | "alert" | "check"> = {
  success: "check",
  info: "info",
  warning: "alert",
  error: "alert",
};

const SEV_ICON_COLOR: Record<ToastSeverity, string> = {
  success: "text-good",
  info: "text-accent",
  warning: "text-warn",
  error: "text-bad",
};

const CATEGORY_LABEL: Record<NotificationCategory, string> = {
  toast: "arrival",
  webhook: "webhook",
  system: "system",
};

/** Best-effort relative timestamp ("2m ago", "4h ago", "yesterday"). */
function fmtRelTime(ms: number, now: number = Date.now()): string {
  const delta = Math.max(0, Math.floor((now - ms) / 1000));
  if (delta < 30) return "just now";
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  if (delta < 604800) return `${Math.floor(delta / 86400)}d ago`;
  return new Date(ms).toLocaleDateString();
}

export interface NotificationCenterProps {
  open: boolean;
  onClose: () => void;
}

export function NotificationCenter({ open, onClose }: NotificationCenterProps) {
  const history = useNotificationHistory();
  const viewedAt = useNotificationsViewedAt();
  const nav = useNavigate();
  const location = useLocation();
  const [filter, setFilter] = useState<NotificationFilter>("all");
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  useOverlaySurface({
    id: "notification-center",
    open,
    onClose,
    lockBody: true,
  });

  // Mark all as read when the modal opens. We capture the read timestamp
  // BEFORE re-marking so the row "unread" highlight reflects the moment
  // the modal opened, not subsequent re-renders.
  const [openedAt, setOpenedAt] = useState<number | null>(null);
  // Snapshot the pre-open watermark via a ref so the open-effect can read
  // the latest `viewedAt` WITHOUT listing it as a dependency. Listing it
  // caused an infinite loop: markNotificationsRead() bumps the viewedAt
  // store, which re-runs this effect, which re-marks, ad infinitum.
  const viewedAtRef = useRef(viewedAt);
  viewedAtRef.current = viewedAt;
  useEffect(() => {
    if (!open) {
      const prev = lastFocusedRef.current;
      setOpenedAt(null);
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus();
        } catch {
          /* ignore if detached */
        }
      }
      return;
    }
    lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
    setOpenedAt(viewedAtRef.current);
    markNotificationsRead();
    const t = setTimeout(() => {
      closeButtonRef.current?.focus();
    }, 0);
    return () => {
      clearTimeout(t);
    };
  }, [open]);

  // Close the modal when route context CHANGES (for example, from a toast
  // drill-down or Settings quick action). We skip the very first run so
  // mounting the modal on the current route doesn't immediately self-close;
  // only a subsequent pathname change should dismiss it.
  const lastPathRef = useRef(location.pathname);
  useEffect(() => {
    if (lastPathRef.current === location.pathname) return;
    lastPathRef.current = location.pathname;
    if (open) onClose();
  }, [location.pathname, open, onClose]);

  const filtered = useMemo(() => {
    if (filter === "all") return history;
    return history.filter((entry) => (entry.category ?? "toast") === filter);
  }, [history, filter]);

  // Per-category counts for the chip labels — keeps the filter UI honest
  // (no "Webhook (0)" surprises after the analyst clears history).
  const counts = useMemo(() => {
    const out: Record<NotificationFilter, number> = {
      all: history.length,
      toast: 0,
      webhook: 0,
      system: 0,
    };
    for (const entry of history) {
      const cat = entry.category ?? "toast";
      out[cat] += 1;
    }
    return out;
  }, [history]);

  const handleRowClick = useCallback(
    (entry: ArrivalToast) => {
      const cat = entry.category ?? "toast";
      // Only "toast" rows have a capture_id (the canonical id is the
      // capture_id for arrival toasts — set by useDesktopAlerts.ts).
      if (cat === "toast" && entry.id) {
        nav(`/feed/${encodeURIComponent(entry.id)}`);
        onClose();
      }
    },
    [nav, onClose],
  );

  const handleClearAll = useCallback(() => {
    clearNotificationHistory();
  }, []);

  if (!open) return null;

  return (
    <div
      // Backdrop is click-to-close so the analyst can dismiss with a stray
      // click outside the card. NOT a real focus-trap container (per spec).
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 px-4 py-12 overflow-y-auto"
      data-testid="notification-center-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="false"
        aria-labelledby="notification-center-title"
        data-testid="notification-center-card"
        className="relative w-full max-w-2xl rounded-xl border border-accent/40 hero-gradient shadow-soft animate-modal-enter overflow-hidden"
      >
        {/* Accent blob — matches OnboardingModal / ShortcutOverlay aesthetic. */}
        <div
          aria-hidden
          className="pointer-events-none absolute -top-24 -left-20 h-56 w-56 rounded-full bg-accent/20 blur-3xl"
        />

        {/* Header */}
        <header className="relative flex items-start justify-between gap-3 border-b border-[color:var(--border-subtle)] px-5 py-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Alert history · last {history.length === 0 ? "0" : Math.min(50, history.length)}
            </div>
            <h2
              id="notification-center-title"
              className="text-lg font-semibold mt-0.5 tracking-tight"
            >
              Notifications
            </h2>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close notifications"
            data-testid="notification-center-close"
            className="btn shrink-0"
          >
            <Icon name="close" size={12} />
          </button>
        </header>

        {/* Filter chips */}
        <div
          className="relative flex flex-wrap items-center gap-1.5 px-5 py-3 border-b border-[color:var(--border-subtle)]"
          data-testid="notification-center-filters"
        >
          {(["all", ...NOTIFICATION_CATEGORIES] as NotificationFilter[]).map((f) => (
            <button
              key={f}
              type="button"
              role="tab"
              aria-selected={filter === f}
              data-testid={`notif-filter-${f}`}
              onClick={() => setFilter(f)}
              className={`chip cursor-pointer ${filter === f ? "chip-active" : ""}`}
            >
              {FILTER_LABEL[f]}
              <span className="ml-1 text-[10px] opacity-70 tabular-nums">{counts[f]}</span>
            </button>
          ))}
        </div>

        {/* List */}
        <div
          className="relative max-h-[60vh] overflow-y-auto px-2 py-2"
          data-testid="notification-center-list"
        >
          {filtered.length === 0 ? (
            <p
              className="text-xs italic text-[color:var(--fg-muted)] px-3 py-6 text-center"
              data-testid="notification-center-empty"
            >
              {filter === "all"
                ? "No notifications yet. High-relevance arrivals will show up here."
                : `No ${FILTER_LABEL[filter].toLowerCase()} notifications in history.`}
            </p>
          ) : (
            <ul className="grid gap-1">
              {filtered.map((entry) => {
                const sev = severityFor(entry);
                const cat = entry.category ?? "toast";
                const created = entry.createdAt ?? 0;
                const isUnread = openedAt != null && created > openedAt;
                const clickable = cat === "toast" && Boolean(entry.id);
                return (
                  <li key={`${entry.id}-${created}`}>
                    <button
                      type="button"
                      onClick={() => clickable && handleRowClick(entry)}
                      disabled={!clickable}
                      data-testid="notification-row"
                      data-category={cat}
                      data-unread={isUnread ? "1" : "0"}
                      className={`w-full text-left rounded-md border-l-2 ${SEV_BORDER[sev]} px-3 py-2 transition-colors ${
                        clickable
                          ? "hover:bg-[color:var(--bg-elev2)]/60 hover:border-l-accent cursor-pointer"
                          : "cursor-default"
                      } ${isUnread ? "bg-[color:var(--bg-elev2)]/40" : ""}`}
                    >
                      <div className="flex items-baseline gap-2 text-[10px] text-[color:var(--fg-dim)]">
                        <span className={`inline-flex items-center gap-1 ${SEV_ICON_COLOR[sev]}`}>
                          <Icon name={SEV_ICON[sev]} size={11} />
                          <span className="uppercase tracking-wider">{CATEGORY_LABEL[cat]}</span>
                        </span>
                        {isUnread && (
                          <span
                            className="inline-block h-1.5 w-1.5 rounded-full bg-accent"
                            aria-label="unread"
                            data-testid="notification-unread-dot"
                          />
                        )}
                        <span className="ml-auto tabular-nums text-[color:var(--fg-muted)]" title={new Date(created || Date.now()).toLocaleString()}>
                          {created ? fmtRelTime(created) : ""}
                        </span>
                      </div>
                      <div className="text-sm mt-0.5 text-[color:var(--fg)] line-clamp-2">
                        {entry.title}
                      </div>
                      {(entry.domain || entry.symbols.length > 0 || entry.score > 0) && (
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          {entry.domain && (
                            <span className="text-[10px] text-[color:var(--fg-muted)] mr-1">
                              {entry.domain}
                            </span>
                          )}
                          {entry.symbols.slice(0, 4).map((s) => (
                            <Pill key={s} variant="sym">{s}</Pill>
                          ))}
                          {entry.score > 0 && (
                            <Pill variant={entry.score >= 0.75 ? "warn" : "default"}>
                              score {entry.score.toFixed(2)}
                            </Pill>
                          )}
                        </div>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* Footer */}
        <footer className="relative flex items-center justify-between gap-3 border-t border-[color:var(--border-subtle)] px-5 py-3 text-[10px] text-[color:var(--fg-muted)]">
          <button
            type="button"
            onClick={handleClearAll}
            disabled={history.length === 0}
            data-testid="notification-center-clear"
            className="btn disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Clear all
          </button>
          <a
            href="/settings#alerts"
            onClick={(e) => {
              // Use the router so we don't full-page-reload inside the SPA.
              e.preventDefault();
              nav("/settings#alerts");
              onClose();
            }}
            data-testid="notification-center-settings-link"
            className="text-[color:var(--fg-dim)] hover:text-accent inline-flex items-center gap-1"
          >
            Settings: alert preferences
            <Icon name="arrowRight" size={11} />
          </a>
        </footer>
      </div>
    </div>
  );
}

export default NotificationCenter;
