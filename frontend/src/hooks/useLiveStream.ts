import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export type LiveStatus = "idle" | "connecting" | "open" | "polling" | "error";

// SSE reconnect cadence — 2s start, doubling on every consecutive failure,
// capped at ~60s. Exposed so tests can read the cadence directly rather
// than re-deriving it from individual timer firings.
export const SSE_BACKOFF_MIN_MS = 2_000;
export const SSE_BACKOFF_MAX_MS = 60_000;

/**
 * SSE first, polling fallback. The hook invalidates the 'summary' query when
 * new data arrives so the rest of the UI re-fetches lazily.
 *
 * Reliability behavior (Round 6):
 *   - On SSE error, close the stream and switch to polling.
 *   - Schedule a reconnect attempt with exponential backoff (2s → 60s).
 *   - When SSE re-establishes (open / first event), stop the polling
 *     fallback and reset the backoff so the next failure restarts cleanly.
 *
 * SSE event 'summary' → refresh; 'tick' → just bump status.
 */
export function useLiveStream(): { status: LiveStatus; lastBeatAt: number | null } {
  const [status, setStatus] = useState<LiveStatus>("idle");
  const [lastBeatAt, setLastBeatAt] = useState<number | null>(null);
  const qc = useQueryClient();
  const fallbackRef = useRef<number | null>(null);
  const reconnectRef = useRef<number | null>(null);
  const backoffRef = useRef<number>(SSE_BACKOFF_MIN_MS);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      // No SSE: start polling.
      setStatus("polling");
      const id = window.setInterval(() => {
        qc.invalidateQueries({ queryKey: ["summary"] });
        setLastBeatAt(Date.now());
      }, 10_000);
      fallbackRef.current = id;
      return () => window.clearInterval(id);
    }

    const startFallback = () => {
      if (fallbackRef.current != null) return;
      const id = window.setInterval(() => {
        qc.invalidateQueries({ queryKey: ["summary"] });
        setLastBeatAt(Date.now());
      }, 12_000);
      fallbackRef.current = id;
    };

    const stopFallback = () => {
      if (fallbackRef.current != null) {
        window.clearInterval(fallbackRef.current);
        fallbackRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (reconnectRef.current != null) return;
      const delay = backoffRef.current;
      reconnectRef.current = window.setTimeout(() => {
        reconnectRef.current = null;
        // Double the next attempt's backoff up front so a tight reconnect
        // failure loop walks the cap, not the floor.
        backoffRef.current = Math.min(backoffRef.current * 2, SSE_BACKOFF_MAX_MS);
        connect();
      }, delay) as unknown as number;
    };

    const connect = () => {
      setStatus("connecting");
      const es = new EventSource("/ui/stream");
      esRef.current = es;

      const onLive = () => {
        setStatus("open");
        setLastBeatAt(Date.now());
        // Live channel back — kill the polling fallback so we don't
        // double-invalidate, and reset the backoff so the next failure
        // restarts cleanly.
        stopFallback();
        backoffRef.current = SSE_BACKOFF_MIN_MS;
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
        qc.invalidateQueries({
          predicate: (q) =>
            Array.isArray(q.queryKey) && q.queryKey[0] === "feed-list",
        });
      });
      es.addEventListener("tick", onLive);
      es.onopen = () => {
        setStatus("open");
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

    connect();

    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
      stopFallback();
      if (reconnectRef.current != null) {
        window.clearTimeout(reconnectRef.current);
        reconnectRef.current = null;
      }
      backoffRef.current = SSE_BACKOFF_MIN_MS;
    };
  }, [qc]);

  return { status, lastBeatAt };
}
