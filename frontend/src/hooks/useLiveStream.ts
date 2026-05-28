import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export type LiveStatus = "idle" | "connecting" | "open" | "polling" | "error";

// SSE reconnect cadence — 2s start, doubling on every consecutive failure,
// capped at ~60s. Exposed so tests can read the cadence directly rather
// than re-deriving it from individual timer firings.
export const SSE_BACKOFF_MIN_MS = 2_000;
export const SSE_BACKOFF_MAX_MS = 60_000;
// Jitter window. Added to every scheduled backoff so a fleet of clients
// don't synchronize their reconnect storms after a sidecar restart.
// Pure additive — never extends past MAX by more than the jitter ceiling,
// which is fine because MAX is the *cap on the doubling*, not a hard SLA.
export const SSE_BACKOFF_JITTER_MS = 500;

// Custom DOM events used to coordinate the SSE consumer with the higher
// /healthz polling layer. The sidecar-recovered event is dispatched by
// SidecarBanner (or anyone watching useSidecarHealth) when /healthz flips
// back from down → ok. useLiveStream listens for it and tears down its
// current backoff timer to reconnect immediately, instead of waiting out
// the full exponential window (which after a few failures can be 30-60s).
export const SSE_EVENT_RECONNECTED = "catchem:sse-reconnected";
export const SSE_EVENT_SIDECAR_DOWN = "catchem:sidecar-down";
export const SSE_EVENT_SIDECAR_RECOVERED = "catchem:sidecar-recovered";

// Staleness ladder consumers can drive UI off. Heartbeat ages below the
// FRESH bar read as a healthy live feed; between FRESH and STALE the dot
// goes amber; above STALE the dot goes red even if the EventSource socket
// hasn't errored yet (which happens with broken intermediaries that hold
// the socket open but stop forwarding bytes).
export const LIVE_FRESH_SECONDS = 30;
export const LIVE_STALE_SECONDS = 90;

export interface LiveStreamState {
  status: LiveStatus;
  lastBeatAt: number | null;
  /**
   * Seconds since the most recent SSE event (`summary` or `tick`). Null
   * until the first beat arrives. Updated every second on a wall-clock
   * timer so the LiveDot tooltip and SidecarBanner ribbon can render
   * "live 3s ago" / "stale 2m ago" without each consumer wiring its own
   * setInterval.
   */
  stalenessSeconds: number | null;
}

/**
 * SSE first, polling fallback. The hook invalidates the 'summary' query when
 * new data arrives so the rest of the UI re-fetches lazily.
 *
 * Reliability behavior (Round 6 + v25):
 *   - On SSE error, close the stream and switch to polling.
 *   - Schedule a reconnect attempt with exponential backoff (2s → 60s)
 *     plus jittered tail (≤ +500ms) so concurrent clients don't herd.
 *   - When `catchem:sidecar-down` is observed (from useSidecarHealth via
 *     SidecarBanner), pause reconnect attempts entirely — the /healthz
 *     poller will dispatch `catchem:sidecar-recovered` once the backend
 *     is reachable again, and we'll reconnect immediately on that signal.
 *   - When SSE re-establishes (open / first event), stop the polling
 *     fallback, reset the backoff, and emit `catchem:sse-reconnected`
 *     so caches that snapshotted during the outage can invalidate once.
 *
 * SSE event 'summary' → refresh; 'tick' → just bump status.
 * The socket opening alone is not a data beat; keep the UI in "connecting"
 * until the first event proves the stream is actually moving data.
 */
export function useLiveStream(): LiveStreamState {
  const [status, setStatus] = useState<LiveStatus>("idle");
  const [lastBeatAt, setLastBeatAt] = useState<number | null>(null);
  const [stalenessSeconds, setStalenessSeconds] = useState<number | null>(null);
  const qc = useQueryClient();
  const fallbackRef = useRef<number | null>(null);
  const reconnectRef = useRef<number | null>(null);
  const backoffRef = useRef<number>(SSE_BACKOFF_MIN_MS);
  const esRef = useRef<EventSource | null>(null);
  const sidecarDownRef = useRef<boolean>(false);
  const lastBeatRef = useRef<number | null>(null);
  const hasOpenedRef = useRef<boolean>(false);

  // Drive `stalenessSeconds` on a 1s tick once the first beat arrives so
  // consumers can render "live Xs ago" copy without each one wiring its
  // own setInterval. Mounted exactly once.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const id = window.setInterval(() => {
      if (lastBeatRef.current == null) {
        setStalenessSeconds(null);
        return;
      }
      const age = Math.floor((Date.now() - lastBeatRef.current) / 1000);
      setStalenessSeconds(age);
    }, 1_000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      // No SSE: start polling.
      setStatus("polling");
      const id = window.setInterval(() => {
        qc.invalidateQueries({ queryKey: ["summary"] });
        const now = Date.now();
        lastBeatRef.current = now;
        setLastBeatAt(now);
      }, 10_000);
      fallbackRef.current = id;
      return () => window.clearInterval(id);
    }

    const startFallback = () => {
      if (fallbackRef.current != null) return;
      const id = window.setInterval(() => {
        qc.invalidateQueries({ queryKey: ["summary"] });
        const now = Date.now();
        lastBeatRef.current = now;
        setLastBeatAt(now);
      }, 12_000);
      fallbackRef.current = id;
    };

    const stopFallback = () => {
      if (fallbackRef.current != null) {
        window.clearInterval(fallbackRef.current);
        fallbackRef.current = null;
      }
    };

    const clearReconnect = () => {
      if (reconnectRef.current != null) {
        window.clearTimeout(reconnectRef.current);
        reconnectRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (reconnectRef.current != null) return;
      // Sidecar is known-down — useSidecarHealth's /healthz poller will
      // dispatch the recovery event when the backend is reachable again.
      // Hammering /ui/stream in the meantime just wastes a socket per
      // backoff window.
      if (sidecarDownRef.current) return;
      const base = backoffRef.current;
      const jitter = Math.random() * SSE_BACKOFF_JITTER_MS;
      const delay = base + jitter;
      reconnectRef.current = window.setTimeout(() => {
        reconnectRef.current = null;
        // Double the next attempt's backoff up front so a tight reconnect
        // failure loop walks the cap, not the floor.
        backoffRef.current = Math.min(backoffRef.current * 2, SSE_BACKOFF_MAX_MS);
        connect();
      }, delay) as unknown as number;
    };

    const connect = () => {
      // If something racy ended us here while the sidecar was flagged down,
      // just bail — recovery will re-arm us. Keep status as "polling" so the
      // dot doesn't lie about a connection that never opened.
      if (sidecarDownRef.current) {
        setStatus("polling");
        startFallback();
        return;
      }
      setStatus("connecting");
      const es = new EventSource("/ui/stream");
      esRef.current = es;

      const onLive = () => {
        setStatus("open");
        const now = Date.now();
        lastBeatRef.current = now;
        setLastBeatAt(now);
        setStalenessSeconds(0);
        // Live channel back — kill the polling fallback so we don't
        // double-invalidate, and reset the backoff so the next failure
        // restarts cleanly.
        stopFallback();
        backoffRef.current = SSE_BACKOFF_MIN_MS;
        // If we'd previously opened-and-lost the connection, this beat is
        // a *reconnect*. Notify the rest of the app exactly once per
        // recovery so caches that snapshotted during the outage can
        // invalidate without each having to listen for status flips.
        if (hasOpenedRef.current) {
          try {
            window.dispatchEvent(new Event(SSE_EVENT_RECONNECTED));
          } catch {
            /* dispatchEvent on the JSDOM window is safe; ignore */
          }
        }
        hasOpenedRef.current = true;
      };

      es.addEventListener("summary", () => {
        onLive();
        // Bump every query the live feed actually watches. The feed page
        // keys on ["feed-list", ...filters], so we predicate-invalidate
        // every variant of that key rather than rely on a single string.
        qc.invalidateQueries({ queryKey: ["summary"] });
        qc.invalidateQueries({ queryKey: ["facets"] });
        qc.invalidateQueries({ queryKey: ["recent"] });
        qc.invalidateQueries({ queryKey: ["trends"] });
        qc.invalidateQueries({ queryKey: ["news-status"] });
        qc.invalidateQueries({ queryKey: ["top-symbols"] });
        qc.invalidateQueries({
          predicate: (q) =>
            Array.isArray(q.queryKey) && q.queryKey[0] === "symbol",
        });
        qc.invalidateQueries({
          predicate: (q) =>
            Array.isArray(q.queryKey) && q.queryKey[0] === "feed-list",
        });
      });
      es.addEventListener("tick", () => {
        onLive();
        qc.invalidateQueries({ queryKey: ["top-symbols"] });
        qc.invalidateQueries({
          predicate: (q) =>
            Array.isArray(q.queryKey) && q.queryKey[0] === "symbol",
        });
      });
      es.onopen = () => {
        setStatus("connecting");
        stopFallback();
        backoffRef.current = SSE_BACKOFF_MIN_MS;
      };
      es.onerror = () => {
        es.close();
        esRef.current = null;
        // Keep data flowing while SSE is down…
        startFallback();
        setStatus("polling");
        // …and arm the next SSE attempt.
        scheduleReconnect();
      };
    };

    const onSidecarDown = () => {
      sidecarDownRef.current = true;
      // Tear down any active connection + pending reconnect; pause the
      // fallback poller too — /healthz is failing, so /ui/recent is
      // almost certainly failing as well. The fallback will be re-armed
      // by `onSidecarRecovered` once we know the backend is back.
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      clearReconnect();
      stopFallback();
      setStatus("polling");
    };
    const onSidecarRecovered = () => {
      sidecarDownRef.current = false;
      // Reset backoff and reconnect *immediately* rather than waiting for
      // the next exponential tick — the analyst clicked "Sidecar restored"
      // (or /healthz flipped on its own), and they want live data now.
      backoffRef.current = SSE_BACKOFF_MIN_MS;
      clearReconnect();
      connect();
    };

    window.addEventListener(SSE_EVENT_SIDECAR_DOWN, onSidecarDown);
    window.addEventListener(SSE_EVENT_SIDECAR_RECOVERED, onSidecarRecovered);

    connect();

    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      stopFallback();
      clearReconnect();
      backoffRef.current = SSE_BACKOFF_MIN_MS;
      window.removeEventListener(SSE_EVENT_SIDECAR_DOWN, onSidecarDown);
      window.removeEventListener(SSE_EVENT_SIDECAR_RECOVERED, onSidecarRecovered);
    };
  }, [qc]);

  return { status, lastBeatAt, stalenessSeconds };
}
