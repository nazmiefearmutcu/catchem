import type { GuardSnapshot, Mode } from "@/types/api";
import {
  guardSeverity,
  guardStatusMessage,
  isBenignGuardState,
} from "@/lib/guardState";
import { JargonTooltip } from "@/components/JargonTooltip";

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
 * warning yellow. Real guard failures show as red. A benign "NewsImpact
 * not configured" state (the COMMON case for a fresh install without
 * merged_news on the box) renders neutral — catchem is healthy without it.
 */
export function StatusBanner({ mode, diagnosticAllowed, guards, useMlStubs }: Props) {
  const severity = diagnosticAllowed && guards.ok ? "warn" : guardSeverity(guards, mode);
  const benign = isBenignGuardState(guards, mode);

  // Tone → utility classes. "info" is the neutral muted style; "ok" inherits
  // the same baseline so a healthy run doesn't read as alarming.
  const toneCls =
    severity === "bad" ? "bg-bad/10 border-bad/40 text-bad"
    : severity === "warn" ? "bg-warn/10 border-warn/40 text-warn"
    : "bg-[color:var(--bg-elev)] border-[color:var(--border)] text-[color:var(--fg-dim)]";

  return (
    <div
      className={`mb-3 rounded-md border px-3 py-2 text-xs flex flex-wrap gap-3 items-center ${toneCls}`}
      role="status"
      aria-live="polite"
    >
      <span><b>mode</b> {MODE_LABEL[mode]}</span>
      <span className="opacity-50">·</span>
      <span><b><JargonTooltip term="use_ml_stubs">stubs</JargonTooltip></b> {String(useMlStubs)}</span>
      <span className="opacity-50">·</span>
      {guards.ok ? (
        <>
          <span><b><JargonTooltip term="NewsImpact" /></b> <JargonTooltip term="quarantine_state">{guards.quarantine_state ?? "unknown"}</JargonTooltip></span>
          <span className="opacity-50">·</span>
          <span><b><JargonTooltip term="release_gate_passed">release_gate</JargonTooltip></b> {String(guards.release_gate_passed)}</span>
          {guards.governance_index_sha256 && (
            <>
              <span className="opacity-50">·</span>
              <span title={guards.governance_index_sha256}><JargonTooltip term="governance sha256">sha256</JargonTooltip> {guards.governance_index_sha256.slice(0, 8)}…</span>
            </>
          )}
        </>
      ) : benign ? (
        <span><b><JargonTooltip term="NewsImpact" /></b> {guardStatusMessage(guards, mode)}</span>
      ) : (
        <span><b>guard error</b> {guardStatusMessage(guards, mode)}</span>
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
