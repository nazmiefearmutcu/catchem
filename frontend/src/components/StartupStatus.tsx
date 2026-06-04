import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * Tiny banner the Tauri shell can show during startup. In the browser it
 * just polls /ui/sidecar-status every 4s and shows a one-line status.
 */
export function StartupStatus() {
  const q = useQuery({
    queryKey: ["sidecar-status"],
    queryFn: api.sidecarStatus,
    refetchInterval: 4_000,
    retry: 1,
  });

  if (q.isLoading) {
    return <span className="text-[10px] text-[color:var(--fg-dim)]" aria-live="polite">starting…</span>;
  }
  if (q.error) {
    return (
      <span className="text-[10px] text-bad" role="status" aria-live="polite">
        sidecar unreachable
      </span>
    );
  }
  const s = q.data!;
  return (
    <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums" aria-live="polite">
      pid <span className="text-fg">{s.pid}</span> · uptime <span className="text-fg">{Math.round(s.uptime_seconds)}s</span> · records <span className="text-fg">{s.records.total}</span>
      {s.diagnostic_enabled && <span className="ml-2 text-warn font-semibold">DIAG</span>}
    </span>
  );
}
