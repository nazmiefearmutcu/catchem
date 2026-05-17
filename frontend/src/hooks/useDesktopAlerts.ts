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
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { FinancialRecord } from "@/types/api";

const NOTIFY_THRESHOLD = 0.85;
const POLL_INTERVAL_MS = 6_000;
const STORAGE_KEY = "catchem:arrival-toasts-enabled";
const TOAST_TTL_MS = 9_000;
const MAX_VISIBLE = 4;

export interface ArrivalToast {
  id: string;
  title: string;
  domain: string;
  score: number;
  reasons: string[];
  symbols: string[];
}

// ── tiny external store. Hook subscribers re-render via React 18's
//    useSyncExternalStore. No module-global setState assignment (which
//    is what was crashing the previous iteration — passing setState
//    directly into a typed Set caused a stale-closure on cleanup).
let _toasts: ArrivalToast[] = [];
const _subs = new Set<() => void>();
function _emit() { for (const cb of _subs) cb(); }
function _subscribe(cb: () => void): () => void {
  _subs.add(cb);
  return () => { _subs.delete(cb); };
}
function _getSnapshot(): ArrivalToast[] { return _toasts; }

function push(t: ArrivalToast): void {
  _toasts = [t, ..._toasts.filter((x) => x.id !== t.id)].slice(0, MAX_VISIBLE);
  _emit();
  if (typeof window !== "undefined") {
    window.setTimeout(() => {
      _toasts = _toasts.filter((x) => x.id !== t.id);
      _emit();
    }, TOAST_TTL_MS);
  }
}

export function dismissToast(id: string): void {
  _toasts = _toasts.filter((x) => x.id !== id);
  _emit();
}

export function useToastQueue(): ArrivalToast[] {
  return useSyncExternalStore(_subscribe, _getSnapshot, _getSnapshot);
}

export function useDesktopAlerts(): void {
  const qc = useQueryClient();
  const notified = useRef<Set<string>>(new Set());
  const seededRef = useRef(false);

  useEffect(() => {
    let alive = true;

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
          return;
        }
        const fresh = items.filter(
          (r: FinancialRecord) =>
            !notified.current.has(r.capture_id) &&
            (r.finance_relevance_score ?? 0) >= NOTIFY_THRESHOLD,
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
      } catch {
        /* network blip — try again next tick */
      }
    }

    void tick();
    const id = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    const unsub = qc.getQueryCache().subscribe((ev) => {
      if (ev.type !== "updated") return;
      const key = ev.query.queryKey;
      if (Array.isArray(key) && key[0] === "summary") void tick();
    });
    return () => {
      alive = false;
      window.clearInterval(id);
      unsub();
    };
  }, [qc]);
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
