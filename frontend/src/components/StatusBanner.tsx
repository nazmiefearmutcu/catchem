import type { GuardSnapshot, Mode } from "@/types/api";

const MODE_LABEL: Record<Mode, string> = {
  production_safe: "production-safe",
  replay_existing: "replay",
  live_tail: "live",
  research_diagnostic: "research diagnostic",
};

interface Props {
  mode: Mode;
  diagnosticAllowed: boolean;
  guards: GuardSnapshot;
  useMlStubs: boolean;
}

/**
 * Always-visible safety + mode strip. Diagnostic mode flips the banner to
 * warning yellow. Guard failures show as red.
 */
export function StatusBanner({ mode, diagnosticAllowed, guards, useMlStubs }: Props) {
  const tone =
    !guards.ok ? "bad"
    : diagnosticAllowed ? "warn"
    : "ok";

  const toneCls =
    tone === "bad" ? "bg-bad/10 border-bad/40 text-bad"
    : tone === "warn" ? "bg-warn/10 border-warn/40 text-warn"
    : "bg-[color:var(--bg-elev)] border-[color:var(--border)] text-[color:var(--fg-dim)]";

  return (
    <div className={`mb-3 rounded-md border px-3 py-2 text-xs flex flex-wrap gap-3 items-center ${toneCls}`} role="status">
      <span><b>mode</b> {MODE_LABEL[mode]}</span>
      <span className="opacity-50">·</span>
      <span><b>stubs</b> {String(useMlStubs)}</span>
      <span className="opacity-50">·</span>
      {guards.ok ? (
        <>
          <span><b>NewsImpact</b> {guards.quarantine_state ?? "unknown"}</span>
          <span className="opacity-50">·</span>
          <span><b>release_gate</b> {String(guards.release_gate_passed)}</span>
          {guards.governance_index_sha256 && (
            <>
              <span className="opacity-50">·</span>
              <span title={guards.governance_index_sha256}>sha256 {guards.governance_index_sha256.slice(0, 8)}…</span>
            </>
          )}
        </>
      ) : (
        <span><b>guard error</b> {guards.error}</span>
      )}
      {diagnosticAllowed && (
        <>
          <span className="opacity-50">·</span>
          <span><b>diagnostic mode active</b> — read-only stamp; may not override is_finance_relevant.</span>
        </>
      )}
    </div>
  );
}
