import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Streaming live-read hook for the QuantScan hero.
 *
 * Opens an EventSource against `/api/quant/live-read-stream`, accumulates
 * the DeepSeek narrative chunk-by-chunk for a "typing effect," and exposes
 * a manual `start()` / `stop()` API so the hero can regenerate on demand
 * (e.g. after a window-size change) without coupling to React Query's
 * refetch interval.
 *
 * Behaviour notes:
 *   - On mount the hook does NOT auto-start; the caller must call `start()`.
 *     This is intentional — pages that don't render the hero pay nothing.
 *   - The hook is single-stream — calling `start()` while already streaming
 *     closes the previous EventSource and starts a fresh one. The new
 *     stream's `start` event clears the buffer so partial text from the
 *     prior generation never leaks into the new render.
 *   - On any wire-level error (connection drop, 5xx, EventSource onerror)
 *     the state flips to "error" and the caller decides whether to fall
 *     back to the non-streaming `/api/quant/live-read` endpoint or just
 *     surface the error chip.
 */
export type StreamState = "idle" | "streaming" | "done" | "error";

export interface StreamingLiveReadMeta {
  /** "deepseek" or "local" — copied from the server's `start` envelope. */
  source: "deepseek" | "local" | null;
  /** Server-side timestamp from the `start` envelope. */
  generatedAt: string | null;
  /** Final USD cost from the `done` envelope (DeepSeek only). */
  usdCost: number | null;
  /** Set when the stream fell back to local mid-flight. */
  fallbackReason: string | null;
}

export interface UseStreamingLiveRead {
  text: string;
  state: StreamState;
  error: string | null;
  meta: StreamingLiveReadMeta;
  start: () => void;
  stop: () => void;
}

const EMPTY_META: StreamingLiveReadMeta = {
  source: null,
  generatedAt: null,
  usdCost: null,
  fallbackReason: null,
};

export function useStreamingLiveRead(limit: number = 1000): UseStreamingLiveRead {
  const [text, setText] = useState<string>("");
  const [state, setState] = useState<StreamState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<StreamingLiveReadMeta>(EMPTY_META);
  const esRef = useRef<EventSource | null>(null);
  // Tracks whether `done` has fired for the current stream. Used to
  // suppress the EventSource `onerror` event that fires AFTER a clean
  // server close (EventSource treats the FIN as an error). Without this
  // we'd flip the UI from "done" to "error" right as it finishes.
  const doneRef = useRef<boolean>(false);

  const stop = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    setState("idle");
  }, []);

  const start = useCallback(() => {
    // Tear down any in-flight stream BEFORE clearing state — otherwise
    // an `onerror` from the closing socket could land on the new buffer.
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    doneRef.current = false;
    setText("");
    setError(null);
    setMeta(EMPTY_META);
    setState("streaming");

    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      // SSR / non-browser environment — caller should fall back to the
      // non-streaming endpoint via useQuery. We surface an error so the
      // hero doesn't sit in "streaming" forever.
      setState("error");
      setError("EventSource not supported");
      return;
    }

    const url = `/api/quant/live-read-stream?limit=${encodeURIComponent(String(limit))}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener("start", (evt) => {
      try {
        const payload = JSON.parse((evt as MessageEvent).data) as {
          source?: "deepseek" | "local";
          generated_at?: string;
        };
        setMeta((m) => ({
          ...m,
          source: payload.source ?? null,
          generatedAt: payload.generated_at ?? null,
        }));
      } catch {
        /* malformed start frame — keep state, don't break the stream */
      }
    });

    es.addEventListener("chunk", (evt) => {
      try {
        const payload = JSON.parse((evt as MessageEvent).data) as { text?: string };
        const chunk = payload.text;
        if (typeof chunk === "string" && chunk.length > 0) {
          setText((t) => t + chunk);
        }
      } catch {
        /* skip malformed chunk */
      }
    });

    es.addEventListener("done", (evt) => {
      doneRef.current = true;
      try {
        const payload = JSON.parse((evt as MessageEvent).data) as {
          source?: "deepseek" | "local";
          usd_cost?: number;
          fallback_reason?: string;
        };
        setMeta((m) => ({
          source: payload.source ?? m.source,
          generatedAt: m.generatedAt,
          usdCost: typeof payload.usd_cost === "number" ? payload.usd_cost : m.usdCost,
          fallbackReason: payload.fallback_reason ?? m.fallbackReason,
        }));
      } catch {
        /* keep partial meta */
      }
      setState("done");
      es.close();
      esRef.current = null;
    });

    // Server-emitted error frame (vs. transport error below).
    es.addEventListener("error", (evt) => {
      // EventSource fires this for BOTH connection failures and (rarely)
      // when the server emits a named "error" event. We can tell them
      // apart: a real server-emitted "error" carries `data`, while the
      // generic EventSource transport error does NOT — it's a plain Event.
      const data = (evt as MessageEvent).data;
      if (data) {
        try {
          const payload = JSON.parse(data) as { error?: string };
          setError(payload.error ?? "stream error");
        } catch {
          setError("stream error");
        }
        setState("error");
        es.close();
        esRef.current = null;
        return;
      }
      // Transport-level error. If the server already emitted "done",
      // this is just the FIN packet — leave the state as "done".
      if (doneRef.current) return;
      setError("Stream interrupted");
      setState("error");
      es.close();
      esRef.current = null;
    });
  }, [limit]);

  // Clean up if the consumer component unmounts mid-stream.
  useEffect(() => {
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

  return { text, state, error, meta, start, stop };
}
