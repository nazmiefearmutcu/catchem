import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export type LiveStatus = "idle" | "connecting" | "open" | "polling" | "error";

/**
 * SSE first, polling fallback. The hook invalidates the 'summary' query when
 * new data arrives so the rest of the UI re-fetches lazily.
 *
 * SSE event 'summary' → refresh; 'tick' → just bump status.
 */
export function useLiveStream(): { status: LiveStatus; lastBeatAt: number | null } {
  const [status, setStatus] = useState<LiveStatus>("idle");
  const [lastBeatAt, setLastBeatAt] = useState<number | null>(null);
  const qc = useQueryClient();
  const fallbackRef = useRef<number | null>(null);

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

    setStatus("connecting");
    const es = new EventSource("/ui/stream");

    es.addEventListener("summary", () => {
      setStatus("open");
      setLastBeatAt(Date.now());
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
    es.addEventListener("tick", () => {
      setStatus("open");
      setLastBeatAt(Date.now());
    });
    es.onopen = () => setStatus("open");
    es.onerror = () => {
      setStatus("error");
      // Switch to polling fallback if SSE fails.
      es.close();
      if (fallbackRef.current == null) {
        setStatus("polling");
        const id = window.setInterval(() => {
          qc.invalidateQueries({ queryKey: ["summary"] });
          setLastBeatAt(Date.now());
        }, 12_000);
        fallbackRef.current = id;
      }
    };

    return () => {
      es.close();
      if (fallbackRef.current != null) {
        window.clearInterval(fallbackRef.current);
        fallbackRef.current = null;
      }
    };
  }, [qc]);

  return { status, lastBeatAt };
}
