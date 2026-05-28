import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSidecarHealth } from "@/hooks/useSidecarHealth";
import {
  SSE_EVENT_SIDECAR_DOWN,
  SSE_EVENT_SIDECAR_RECOVERED,
} from "@/hooks/useLiveStream";
import { pushNotification } from "@/hooks/useDesktopAlerts";

/**
 * Unified "Reconnecting…" banner for sidecar (FastAPI backend) outages.
 *
 * Before this lived per-page each `useQuery` would render its own error
 * fallback in isolation, leaving the analyst staring at half a dozen
 * "Failed to load" cards instead of a single, honest status. The banner
 * is sticky at the top of the main column, self-hides on healthy state
 * so no spacer is needed, and uses aria-live="assertive" so screen
 * readers announce the outage / recovery without manual focus.
 *
 * Existing React-Query error fallbacks remain for HTTP 4xx/5xx; the
 * banner is dedicated to the /healthz polling channel. When recovery
 * is detected (retryCount bump) we invalidate all live queries so the
 * UI immediately re-fetches instead of waiting for staleTime to elapse.
 *
 * v25 additions:
 *   The banner is also the canonical surface that publishes sidecar
 *   liveness events to other layers. It dispatches `catchem:sidecar-down`
 *   when state transitions to "down", and `catchem:sidecar-recovered`
 *   when /healthz flips back to "ok" after any failed window. useLiveStream
 *   listens for both: the former pauses SSE reconnect attempts (so we
 *   don't hammer /ui/stream against a dead backend), the latter tears
 *   down the backoff timer and reconnects immediately.
 */
export function SidecarBanner() {
  const { state, retryCount } = useSidecarHealth();
  const qc = useQueryClient();
  const wasDownRef = useRef(false);
  // Record when the outage started so the recovery toast can report how
  // long the analyst was offline. `null` = never been down in this session.
  const downSinceRef = useRef<number | null>(null);

  // On recovery: flush every cached query so stale 0-byte errors don't
  // linger past the moment the sidecar comes back. This is the bridge
  // between health polling (cheap) and the React-Query layer (which
  // would otherwise wait out its own staleTime before re-firing).
  useEffect(() => {
    if (retryCount > 0) {
      qc.invalidateQueries();
      // Surface a transient success notification with the outage duration
      // so the analyst doesn't miss the recovery. pushNotification (not
      // pushToast) routes through the Notification Center category="system"
      // so it lands in the bell-icon history too — matters when the user
      // wasn't looking at the screen during the outage.
      const offlineMs = downSinceRef.current != null
        ? Math.max(0, Date.now() - downSinceRef.current)
        : 0;
      const seconds = Math.round(offlineMs / 1000);
      const duration = seconds >= 60
        ? `${Math.round(seconds / 60)}m`
        : `${seconds}s`;
      pushNotification({
        id: `sidecar-recovered-${retryCount}`,
        title: offlineMs > 0
          ? `Sidecar reconnected after ${duration} offline`
          : "Sidecar reconnected",
        domain: "system",
        score: 1.0,
        reasons: [],
        symbols: [],
        severity: "success",
        category: "system",
      });
      downSinceRef.current = null;
    }
  }, [retryCount, qc]);

  // Bridge useSidecarHealth → useLiveStream over a DOM event so the SSE
  // consumer can pause/resume without re-rendering or coupling directly
  // to this component's render tree. Tested at the producer side via
  // SidecarBanner.test; the consumer-side contract is pinned in
  // useLiveStream.test (`catchem:sidecar-recovered` triggers immediate
  // reconnect, `catchem:sidecar-down` stops scheduling new attempts).
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (state === "down") {
      if (!wasDownRef.current) {
        wasDownRef.current = true;
        downSinceRef.current = Date.now();
        window.dispatchEvent(new Event(SSE_EVENT_SIDECAR_DOWN));
      }
    } else if (state === "ok") {
      if (wasDownRef.current) {
        wasDownRef.current = false;
        window.dispatchEvent(new Event(SSE_EVENT_SIDECAR_RECOVERED));
      }
    }
    // "reconnecting" is a transient single-failure state — don't toggle
    // the SSE pause flag yet; useSidecarHealth will either flip back to
    // "ok" within 3s (no event) or escalate to "down" (event fires above).
  }, [state]);

  if (state === "ok") return null;

  const isDown = state === "down";
  return (
    <div
      role="status"
      aria-live="assertive"
      data-testid="sidecar-banner"
      data-state={state}
      className="sticky top-0 z-40 mx-auto w-full max-w-screen-2xl px-4 pt-2"
    >
      <div
        className={`flex items-center gap-3 rounded-md border px-3 py-2 text-[12px] ${
          isDown
            ? "border-bad/40 bg-bad/10 text-bad"
            : "border-warn/40 bg-warn/10 text-warn"
        }`}
      >
        <span className="relative inline-flex h-2 w-2 shrink-0" aria-hidden>
          {/* Pulse rim — collapsed by globals.css under prefers-reduced-motion. */}
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-current" />
        </span>
        <div className="flex-1">
          <div className="font-semibold">
            {isDown ? "Sidecar is offline" : "Reconnecting to sidecar…"}
          </div>
          <div className="text-[11px] opacity-80 mt-0.5">
            {isDown
              ? "Backend stopped responding. Catchem will keep retrying. If this persists, restart the app from /Applications/Catchem.app."
              : "Pinging /healthz every 3 seconds. Live data is paused until reconnect."}
          </div>
        </div>
      </div>
    </div>
  );
}
