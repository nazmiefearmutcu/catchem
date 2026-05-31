import { useEffect, useRef, useState } from "react";
import { getBootToken } from "@/lib/bootToken";

/**
 * Coarse-grained sidecar liveness, exposed via /healthz polling.
 *
 *   "ok"           — last probe succeeded; we poll lazily (15s).
 *   "reconnecting" — exactly one consecutive failure; poll fast (3s)
 *                    so the recovery is felt within a second of the
 *                    sidecar coming back. Single fail can be a stray
 *                    socket reset, so we don't yet alarm.
 *   "down"         — ≥2 consecutive failures; backend is genuinely
 *                    not answering. UI surfaces the persistent banner.
 *
 * The effect mounts exactly once; the polling cadence is rebound from
 * inside the `check()` closure via `scheduleNext()` so a state change
 * (ok ↔ degraded) does not tear down + immediately re-fire a probe.
 * That re-fire was the source of a "instant down" bug in the first
 * implementation: the [state]-deped effect would unmount, the cancel
 * flag would flip, but the just-completed failed fetch had already
 * bumped failsRef from 0 → 1 → setState("reconnecting"); the effect
 * re-ran, fired a second check synchronously, that failed too, taking
 * failsRef 1 → 2 → setState("down") before any 3s interval elapsed.
 */

export type SidecarState = "ok" | "reconnecting" | "down";

// AbortSignal.timeout is Tauri/WebKit-safe (≥17) and Chromium-safe (≥103);
// we still feature-gate so SSR / older test envs don't blow up. For the
// JSDOM test environment it's polyfilled by AbortController + setTimeout
// because vitest's jsdom build doesn't ship the spec method.
function timeoutSignal(ms: number): AbortSignal {
  const Native = (AbortSignal as unknown as { timeout?: (ms: number) => AbortSignal }).timeout;
  if (typeof Native === "function") return Native.call(AbortSignal, ms);
  const c = new AbortController();
  setTimeout(() => c.abort(), ms);
  return c.signal;
}

export const SIDECAR_OK_INTERVAL_MS = 15_000;
export const SIDECAR_DEGRADED_INTERVAL_MS = 3_000;
export const SIDECAR_PROBE_TIMEOUT_MS = 3_000;

export function useSidecarHealth() {
  const [state, setState] = useState<SidecarState>("ok");
  const [retryCount, setRetryCount] = useState(0);
  const failsRef = useRef(0);
  const stateRef = useRef<SidecarState>("ok");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;

    const scheduleNext = () => {
      if (cancelledRef.current) return;
      const cadence =
        stateRef.current === "ok"
          ? SIDECAR_OK_INTERVAL_MS
          : SIDECAR_DEGRADED_INTERVAL_MS;
      timerRef.current = setTimeout(check, cadence);
    };

    const check = async () => {
      if (cancelledRef.current) return;
      try {
        const bootToken = getBootToken();
        const healthzUrl = bootToken
          ? `/healthz?boot_token=${encodeURIComponent(bootToken)}`
          : "/healthz";
        const res = await fetch(healthzUrl, {
          method: "GET",
          signal: timeoutSignal(SIDECAR_PROBE_TIMEOUT_MS),
        });
        if (cancelledRef.current) return;
        if (res.ok) {
          if (failsRef.current > 0) {
            // Recovery — bump retry counter so React-Query consumers
            // can `useEffect` on it and force re-fetch any cached
            // error queries.
            setRetryCount((r) => r + 1);
          }
          failsRef.current = 0;
          stateRef.current = "ok";
          setState("ok");
        } else {
          failsRef.current += 1;
          const next = failsRef.current >= 2 ? "down" : "reconnecting";
          stateRef.current = next;
          setState(next);
        }
      } catch {
        if (cancelledRef.current) return;
        failsRef.current += 1;
        const next = failsRef.current >= 2 ? "down" : "reconnecting";
        stateRef.current = next;
        setState(next);
      } finally {
        scheduleNext();
      }
    };

    // Kick off the first probe immediately so the analyst sees state
    // within ~1s of mount, not after a 15s wait.
    check();

    return () => {
      cancelledRef.current = true;
      if (timerRef.current != null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  return { state, retryCount };
}
