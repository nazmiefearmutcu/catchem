import type { LiveStatus } from "@/hooks/useLiveStream";
import { LIVE_FRESH_SECONDS, LIVE_STALE_SECONDS } from "@/hooks/useLiveStream";

const STATUS_COLOR: Record<LiveStatus, string> = {
  idle: "bg-[color:var(--fg-muted)]",
  connecting: "bg-warn",
  open: "bg-good",
  polling: "bg-accent",
  error: "bg-bad",
};

const STATUS_LABEL: Record<LiveStatus, string> = {
  idle: "idle",
  connecting: "connecting",
  open: "live",
  polling: "polling",
  error: "error",
};

function fmtAgo(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

/**
 * Connection-state dot rendered in the Shell header (and reused at smaller
 * sizes in feed-status strips). Color resolution rules:
 *   - `status` non-"open" wins outright — connecting/polling/idle/error
 *     all map to their semantic token regardless of staleness.
 *   - `status === "open"` resolves against `stalenessSeconds`:
 *       <30s   → good (green)
 *       30-90s → warn (amber) — socket alive but no events
 *       >90s   → bad  (red)  — channel held open but stopped flowing
 *     This catches the "stuck channel" failure mode where an intermediary
 *     proxy keeps the socket open after the backend stopped publishing.
 *
 * The pulse is wrapped in `motion-safe:` so reduced-motion users don't
 * see the dot breathe; the steady color still communicates state.
 */
export function LiveDot({
  status,
  stalenessSeconds = null,
  label,
}: {
  status: LiveStatus;
  stalenessSeconds?: number | null;
  label?: string;
}) {
  // Resolve effective color. Open + stale → degrade through warn → bad.
  let color = STATUS_COLOR[status];
  let stateLabel = label ?? STATUS_LABEL[status];
  if (status === "open" && stalenessSeconds != null) {
    if (stalenessSeconds >= LIVE_STALE_SECONDS) {
      color = "bg-bad";
      stateLabel = label ?? "stale";
    } else if (stalenessSeconds >= LIVE_FRESH_SECONDS) {
      color = "bg-warn";
      stateLabel = label ?? "idle";
    }
  }

  // Build a human tooltip the analyst can hover the dot to read. Useful
  // when the header is too cramped to surface "live 3s ago" inline.
  let tip: string;
  if (status === "open" && stalenessSeconds != null) {
    tip = stalenessSeconds < LIVE_FRESH_SECONDS
      ? `live · last beat ${fmtAgo(stalenessSeconds)}`
      : `stale · last beat ${fmtAgo(stalenessSeconds)}`;
  } else if (status === "polling") {
    tip = "polling fallback · SSE channel offline";
  } else if (status === "connecting") {
    tip = "connecting to /ui/stream…";
  } else if (status === "error") {
    tip = "stream errored — retrying with backoff";
  } else {
    tip = "no live channel yet";
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]"
      title={tip}
      aria-label={tip}
      data-testid="live-dot"
      data-status={status}
      data-staleness={stalenessSeconds == null ? "none" : String(stalenessSeconds)}
    >
      <span
        className={`inline-block h-2 w-2 rounded-full motion-safe:animate-pulse-dot ${color}`}
        aria-hidden
      />
      {stateLabel}
    </span>
  );
}
