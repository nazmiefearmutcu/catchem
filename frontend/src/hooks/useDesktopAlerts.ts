/**
 * In-app toasts for high-relevance arrivals.
 *
 * Why this exists:
 *   Catchem flows ~50 items/hr from 30 sources. The analyst can't stare
 *   at the Live Feed. This hook surfaces only the items that matter
 *   (`finance_relevance_score >= NOTIFY_THRESHOLD`) as slide-in toasts
 *   in the top-right of the app, regardless of which tab is active.
 *
 * Why NOT the browser Notification API:
 *   Catchem's webview loads from http://127.0.0.1:8087 (non-HTTPS).
 *   Most WebKit builds — including Tauri's WKWebView — refuse
 *   Notification.requestPermission() on plain HTTP, silently returning
 *   "denied" without showing the OS prompt. In-app toasts are also a
 *   better UX here: more visible, no permissions, work from any tab.
 *
 * Mute toggle:
 *   Defaults on; muted via `setAlertEnabled(false)` from the Live Feed
 *   chip. Persisted in localStorage so the choice survives relaunch.
 *
 * Backfill safety:
 *   On first mount we seed `notified` with every currently-visible
 *   capture_id. Prevents the user from getting bombed with 30+ toasts
 *   on initial load after a long Catchem nap.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api } from "@/lib/api";
import type { FinancialRecord } from "@/types/api";

// Default alert threshold. Calibration note:
//   The finance-relevance scorer's empirical distribution on the live RSS
//   feed reads max ≈ 0.80, p90 ≈ 0.63, median ≈ 0.48. The previous default
//   of 0.85 sat ABOVE the empirical max, so no item ever cleared the bar
//   and the alerts never fired ("alarm çalışmıyor"). 0.65 catches the
//   top ~10% — roughly 1-2 toasts per active hour at current ingest rates.
//
// The user can tune this at runtime via localStorage key
// `catchem:alerts-threshold` (number, 0..1).
const DEFAULT_NOTIFY_THRESHOLD = 0.65;
const THRESHOLD_STORAGE_KEY = "catchem:alerts-threshold";
const POLL_INTERVAL_MS = 6_000;
const STORAGE_KEY = "catchem:arrival-toasts-enabled";
const MAX_VISIBLE = 4;

// Tone-aware auto-dismiss timing. Severity reflects how important the toast
// is to the user — error stays the longest because the analyst probably
// hasn't seen it yet (it competes with real work for attention), success
// is fastest because positive confirmations don't need to linger.
//
// The ToastTray component is responsible for the actual timer (so hover-pause
// works); these constants are exported so the component can resolve a TTL
// from a tone without baking the table into the view layer.
export type ToastSeverity = "info" | "success" | "warning" | "error";
export const TOAST_TTL_BY_SEVERITY: Record<ToastSeverity, number> = {
  success: 3_000,
  info: 4_000,
  warning: 6_000,
  error: 8_000,
};
// Score > 0.9 is "critical arrival" territory — sticky until the user
// acknowledges. Used by the news poller when finance_relevance_score peaks
// near the empirical max (~0.8-0.95 in practice).
export const STICKY_SCORE_THRESHOLD = 0.9;

function readThreshold(): number {
  if (typeof window === "undefined") return DEFAULT_NOTIFY_THRESHOLD;
  try {
    const raw = window.localStorage?.getItem(THRESHOLD_STORAGE_KEY);
    if (raw == null) return DEFAULT_NOTIFY_THRESHOLD;
    const n = Number.parseFloat(raw);
    if (!Number.isFinite(n) || n < 0 || n > 1) return DEFAULT_NOTIFY_THRESHOLD;
    return n;
  } catch {
    return DEFAULT_NOTIFY_THRESHOLD;
  }
}

export function setAlertThreshold(value: number): number {
  const clamped = Math.max(0, Math.min(1, value));
  try {
    window.localStorage?.setItem(THRESHOLD_STORAGE_KEY, String(clamped));
  } catch { /* ignore */ }
  return clamped;
}

export function getAlertThreshold(): number {
  return readThreshold();
}

// Notification categories — used by the Notification Center modal so the
// analyst can filter the history by source. Every toast pushed through the
// store carries a category; default is "toast" (the news-poller arrival path).
//   "toast"   — generic ArrivalToast (news arrivals, watchlist bursts)
//   "webhook" — webhook delivery test results
//   "system"  — sidecar disconnects, ingestion errors
// New categories should be appended here AND in NOTIFICATION_CATEGORIES below
// so the filter chip set stays in sync.
export type NotificationCategory = "toast" | "webhook" | "system";
export const NOTIFICATION_CATEGORIES: readonly NotificationCategory[] = [
  "toast",
  "webhook",
  "system",
];

export interface ArrivalToast {
  id: string;
  title: string;
  domain: string;
  score: number;
  reasons: string[];
  symbols: string[];
  // Optional. When absent, ToastTray infers a tone from score:
  //   ≥ STICKY_SCORE_THRESHOLD  → "error"   (critical, near-sticky)
  //   ≥ 0.75                    → "warning"
  //   ≥ 0.50                    → "info"
  //   below                     → "success"
  // Explicit values let callers override (e.g. /scan watchlist bursts
  // pass "warning" regardless of normalised z-score).
  severity?: ToastSeverity;
  // Optional. Defaults to "toast" when omitted so existing callers keep
  // working without churn. Surfaced in the Notification Center modal so the
  // analyst can filter the history by source. The capture_id (if any) is
  // assumed to live on `id` for "toast" items — clicking a "toast" row in
  // the modal navigates to /feed/<id>.
  category?: NotificationCategory;
  // Optional. Wall-clock when the toast was first enqueued, in ms since
  // epoch. The store stamps this on `push()` so consumers (ToastTray for
  // ephemeral display, NotificationCenter for history) read a single
  // canonical timestamp. We keep this nullable on the *input* so existing
  // call sites stay unchanged; the persisted-history entries always have
  // it set because the store fills it in below.
  createdAt?: number;
}

// ── tiny external store. Hook subscribers re-render via React 18's
//    useSyncExternalStore. No module-global setState assignment (which
//    is what was crashing the previous iteration — passing setState
//    directly into a typed Set caused a stale-closure on cleanup).
//
// Auto-dismiss is OWNED BY THE VIEW LAYER now (ToastTray). The store
// just holds the queue and emits dedup-trim updates. This is what makes
// hover-pause / exit-animation possible — the component can run its
// own timer per toast without the store firing an out-of-band delete
// while the slide-out is in flight.
let _toasts: ArrivalToast[] = [];
const _subs = new Set<() => void>();
function _emit() { for (const cb of _subs) cb(); }
function _subscribe(cb: () => void): () => void {
  _subs.add(cb);
  return () => { _subs.delete(cb); };
}
function _getSnapshot(): ArrivalToast[] { return _toasts; }

// ── notification history (v37 task #142, Notification Center modal) ──────
// The ephemeral toast queue trims to MAX_VISIBLE (4). Analysts who blink
// during a burst of arrivals would lose the history — the Notification
// Center modal aggregates the last NOTIFICATION_HISTORY_LIMIT toasts so
// they're recoverable from a single panel.
//
// Persistence: history is mirrored to localStorage under
// HISTORY_STORAGE_KEY. On module load we hydrate the in-memory list from
// storage so the modal still shows previous-session arrivals after a
// relaunch. Reads/writes are best-effort — a quota error or disabled
// storage degrades to in-memory-only, no thrown exception.
//
// Last-viewed: HISTORY_VIEWED_KEY stores the epoch ms when the modal was
// last opened. NotificationCenter compares each entry's `createdAt`
// against this stamp to compute the unread badge. The bell icon clears it
// on open via `markNotificationsRead()`.
export const NOTIFICATION_HISTORY_LIMIT = 50;
export const HISTORY_STORAGE_KEY = "catchem.notifications.history";
export const HISTORY_VIEWED_KEY = "catchem.notifications.viewed-at";

let _history: ArrivalToast[] = [];
const _historySubs = new Set<() => void>();
function _historyEmit() { for (const cb of _historySubs) cb(); }
function _historySubscribe(cb: () => void): () => void {
  _historySubs.add(cb);
  return () => { _historySubs.delete(cb); };
}
function _getHistorySnapshot(): ArrivalToast[] { return _history; }

let _viewedAt = 0;
const _viewedSubs = new Set<() => void>();
function _viewedEmit() { for (const cb of _viewedSubs) cb(); }
function _viewedSubscribe(cb: () => void): () => void {
  _viewedSubs.add(cb);
  return () => { _viewedSubs.delete(cb); };
}
function _getViewedSnapshot(): number { return _viewedAt; }

function _persistHistory(): void {
  const storage = safeStorage();
  if (!storage) return;
  try {
    storage.setItem(HISTORY_STORAGE_KEY, JSON.stringify(_history));
  } catch {
    /* quota / disabled — degrade to in-memory only */
  }
}

/**
 * Coerce a persisted (possibly stale / partial) entry into a shape-complete
 * ArrivalToast. Only `id`+`title` are validated by the caller's filter; this
 * fills the remaining required fields with safe defaults so downstream
 * consumers never read `undefined.length` or index a fixed Record by an
 * out-of-union category string.
 */
function _normalizeHistoryEntry(x: ArrivalToast): ArrivalToast {
  return {
    ...x,
    symbols: Array.isArray(x.symbols) ? x.symbols : [],
    reasons: Array.isArray(x.reasons) ? x.reasons : [],
    score: typeof x.score === "number" && Number.isFinite(x.score) ? x.score : 0,
    domain: typeof x.domain === "string" ? x.domain : "",
    category: NOTIFICATION_CATEGORIES.includes(x.category as NotificationCategory)
      ? x.category
      : "toast",
  };
}

function _hydrateHistoryFromStorage(): void {
  const storage = safeStorage();
  if (!storage) return;
  try {
    const raw = storage.getItem(HISTORY_STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        _history = (parsed as ArrivalToast[])
          .filter((x) => x && typeof x.id === "string" && typeof x.title === "string")
          // Normalize every surviving entry to a shape-complete ArrivalToast.
          // The persisted blob can come from an older build, a partial write,
          // or a hand edit that satisfies the id+title filter but is missing
          // required fields (e.g. `symbols`). Without this, NotificationCenter
          // throws on `entry.symbols.length` and `counts` mints a phantom key
          // for out-of-union categories. Backfill defensively so the modal
          // renders instead of white-screening.
          .map((x) => _normalizeHistoryEntry(x))
          .slice(0, NOTIFICATION_HISTORY_LIMIT);
      }
    }
    const stamp = storage.getItem(HISTORY_VIEWED_KEY);
    if (stamp != null) {
      const n = Number.parseInt(stamp, 10);
      if (Number.isFinite(n) && n > 0) _viewedAt = n;
    }
  } catch {
    /* malformed JSON / read failure → leave in-memory defaults */
  }
}

// Hydrate once on module load so the bell badge has a correct unread count
// on the very first render. Safe under jsdom — _hydrate gracefully no-ops
// if window.localStorage is unavailable.
_hydrateHistoryFromStorage();

function _appendHistory(t: ArrivalToast): void {
  const stamped: ArrivalToast = {
    ...t,
    category: t.category ?? "toast",
    createdAt: t.createdAt ?? Date.now(),
  };
  _history = [stamped, ..._history.filter((x) => x.id !== stamped.id)].slice(
    0,
    NOTIFICATION_HISTORY_LIMIT,
  );
  _persistHistory();
  _historyEmit();
}

function push(t: ArrivalToast): void {
  _toasts = [t, ..._toasts.filter((x) => x.id !== t.id)].slice(0, MAX_VISIBLE);
  _appendHistory(t);
  _emit();
}

/**
 * Public helper so other surfaces (e.g. /scan's watchlist alert hook)
 * can enqueue a toast without going through the news-poller signal.
 * Same id-dedupe + TTL behavior; payload shape is identical so the
 * ToastTray component renders uniformly.
 */
export function pushToast(t: ArrivalToast): void {
  push(t);
}

export function dismissToast(id: string): void {
  _toasts = _toasts.filter((x) => x.id !== id);
  _emit();
}

export function useToastQueue(): ArrivalToast[] {
  return useSyncExternalStore(_subscribe, _getSnapshot, _getSnapshot);
}

// ── notification history public API ──────────────────────────────────────
/**
 * Subscribe to the persisted notification history (last 50). Newest first.
 * Used by NotificationCenter; ToastTray stays on `useToastQueue()`.
 */
export function useNotificationHistory(): ArrivalToast[] {
  return useSyncExternalStore(
    _historySubscribe,
    _getHistorySnapshot,
    _getHistorySnapshot,
  );
}

/**
 * Push a notification that lives ONLY in the history modal (no ephemeral
 * toast slide-in). Use this for low-priority events the analyst should be
 * able to look back on but shouldn't get a slide-in for — e.g. webhook
 * delivery confirmations or sidecar reconnect messages.
 */
export function pushNotification(t: ArrivalToast): void {
  _appendHistory(t);
}

export function clearNotificationHistory(): void {
  _history = [];
  _persistHistory();
  _historyEmit();
}

export function markNotificationsRead(at?: number): number {
  _viewedAt = at ?? Date.now();
  const storage = safeStorage();
  if (storage) {
    try {
      storage.setItem(HISTORY_VIEWED_KEY, String(_viewedAt));
    } catch {
      /* ignore */
    }
  }
  _viewedEmit();
  return _viewedAt;
}

export function useNotificationsViewedAt(): number {
  return useSyncExternalStore(
    _viewedSubscribe,
    _getViewedSnapshot,
    _getViewedSnapshot,
  );
}

/**
 * Number of history entries whose `createdAt` is strictly newer than the
 * last "viewed" timestamp. Drives the bell-icon badge in Shell.
 */
export function useUnreadNotificationCount(): number {
  const items = useNotificationHistory();
  const viewedAt = useNotificationsViewedAt();
  let count = 0;
  for (const item of items) {
    const ts = item.createdAt ?? 0;
    if (ts > viewedAt) count += 1;
  }
  return count;
}

/**
 * Test-only seam — resets the in-module history + viewed state to the
 * defaults you'd see on a fresh install. Vitest beforeEach() calls this so
 * tests don't cross-contaminate via the module-global cache.
 */
export function __resetNotificationStoreForTests(): void {
  _history = [];
  _viewedAt = 0;
  _historyEmit();
  _viewedEmit();
}

// Upper bound on the dedupe Set. Each tick only inspects api.recent(40), so
// any id that can still re-appear lives well within the most-recent window.
// A cap two orders of magnitude above 40 keeps the structure trivially small
// while leaving generous headroom against rapid id churn. Without this the
// Set grows monotonically for the lifetime of a long-lived desktop session.
const NOTIFIED_CAP = 2_000;

export function useDesktopAlerts(): void {
  const notified = useRef<Set<string>>(new Set());
  const seededRef = useRef(false);

  useEffect(() => {
    let alive = true;

    // Drop the oldest ids (insertion order is preserved by Set iteration)
    // once the dedupe Set exceeds NOTIFIED_CAP, keeping only the most recent
    // window. Prevents unbounded growth across a long-running session.
    function pruneNotified() {
      const set = notified.current;
      if (set.size <= NOTIFIED_CAP) return;
      const overflow = set.size - NOTIFIED_CAP;
      const it = set.values();
      for (let i = 0; i < overflow; i += 1) {
        const { value, done } = it.next();
        if (done) break;
        set.delete(value);
      }
    }

    async function tick() {
      if (typeof window === "undefined") return;
      const storage = safeStorage();
      if (storage?.getItem(STORAGE_KEY) === "0") return;
      try {
        const { items } = await api.recent(40, true);
        if (!alive) return;
        if (!seededRef.current) {
          items.forEach((r: FinancialRecord) => notified.current.add(r.capture_id));
          seededRef.current = true;
          pruneNotified();
          return;
        }
        // Re-read each tick so the user's choice via the threshold
        // setter takes effect immediately, no remount needed.
        const threshold = readThreshold();
        const fresh = items.filter(
          (r: FinancialRecord) =>
            !notified.current.has(r.capture_id) &&
            (r.finance_relevance_score ?? 0) >= threshold,
        );
        for (const r of fresh) {
          notified.current.add(r.capture_id);
          push({
            id: r.capture_id,
            title: r.title ?? "(untitled)",
            domain: r.domain ?? "",
            score: r.finance_relevance_score ?? 0,
            reasons: (r.impact_reason_codes ?? []).slice(0, 3),
            symbols: (r.candidate_symbols ?? []).slice(0, 4),
          });
        }
        items.forEach((r: FinancialRecord) => notified.current.add(r.capture_id));
        pruneNotified();
      } catch {
        /* network blip — try again next tick */
      }
    }

    void tick();
    const id = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    // Note: a previous version subscribed to queryCache "summary" updates
    // and called tick() on every event. That double-fetched the alerts
    // feed against the SSE-driven feed-list invalidation upstream
    // (useLiveStream already invalidates feed-list on summary events;
    // feed-list itself has a 5s refetchInterval). The interval here is
    // the canonical freshness budget; the subscribe was pure noise.
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);
}

// ── opt-in toggle (defaults to on) ───────────────────────────────────────
export type DesktopAlertState = "on" | "off";

function safeStorage(): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readDesktopAlertState(): DesktopAlertState {
  const s = safeStorage();
  return s?.getItem(STORAGE_KEY) === "0" ? "off" : "on";
}

export function toggleDesktopAlerts(next: boolean): DesktopAlertState {
  const s = safeStorage();
  if (next) {
    s?.removeItem(STORAGE_KEY);
    return "on";
  }
  s?.setItem(STORAGE_KEY, "0");
  return "off";
}

export function useDesktopAlertState(): [DesktopAlertState, (next: boolean) => void] {
  const [state, setState] = useState<DesktopAlertState>(() => readDesktopAlertState());
  function flip(next: boolean): void {
    setState(toggleDesktopAlerts(next));
  }
  return [state, flip];
}
