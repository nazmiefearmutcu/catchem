// Shared helpers for interpreting the NewsImpact guard state.
//
// Background: `/ui/guards` returns `{ok: false, error_code: "missing_governance_index"}`
// whenever the user's machine doesn't have the merged_news (NewsImpact) repo
// at the configured path. That's the COMMON case for someone who installs
// catchem standalone — the diagnostic path is opt-in and gated by a
// research_diagnostic mode flip, and production_safe mode (the default)
// refuses to load the diagnostic adapter regardless of guard state.
//
// Pre-fix the entire UI treated `!ok` as a red "guard error" banner, so a
// fresh install that booted perfectly fine looked like it had a critical
// failure. Distinguish a benign "NewsImpact is just not configured" from
// a real "we cannot prove the gate state" failure.

import type { GuardSnapshot, Mode } from "@/types/api";

/**
 * Error codes that signal "NewsImpact infrastructure is absent or
 * unconfigured", not "catchem is broken". A fresh install without the
 * merged_news repo lands here.
 */
export const BENIGN_GUARD_ERROR_CODES: ReadonlySet<string> = new Set([
  "missing_governance_index",
]);

export type GuardSeverity = "ok" | "info" | "warn" | "bad";

/**
 * Classify a guard snapshot for UI tone. The mode matters: in
 * production_safe the diagnostic adapter is forbidden regardless, so a
 * benign guard error is purely informational. In research_diagnostic
 * mode the SAME error is real — without the governance file the
 * diagnostic path can't operate.
 */
export function guardSeverity(
  guards: GuardSnapshot | undefined,
  mode: Mode,
): GuardSeverity {
  if (!guards) return "info";
  if (guards.ok) {
    // Guard available; if release_gate is passed, that's a flip we never expect.
    return guards.release_gate_passed ? "bad" : "ok";
  }
  const code = guards.error_code ?? "";
  if (BENIGN_GUARD_ERROR_CODES.has(code)) {
    // In production-safe we don't need NewsImpact at all — purely informational.
    return mode === "production_safe" ? "info" : "warn";
  }
  // Unknown / malformed / release_gate_flipped → real concern.
  return "bad";
}

/**
 * Human-readable status line for the StatusBanner when guards are not OK.
 * Callers prepend their own label ("NewsImpact", "guard error", etc.) —
 * the message returned here is the bare state phrase, never starts with
 * a label noun that the caller would double up on.
 */
export function guardStatusMessage(
  guards: GuardSnapshot | undefined,
  mode: Mode,
): string {
  if (!guards) return "snapshot unavailable";
  const code = guards.error_code ?? guards.error ?? "unknown_guard_failure";
  if (BENIGN_GUARD_ERROR_CODES.has(guards.error_code ?? "")) {
    if (mode === "production_safe") {
      return "not configured (production-safe mode — diagnostic disabled)";
    }
    return `not configured: ${code}`;
  }
  return String(code);
}

/**
 * True when the guard "error" is benign — the catchem pipeline is fully
 * operational, the missing piece (merged_news repo) is opt-in.
 */
export function isBenignGuardState(
  guards: GuardSnapshot | undefined,
  mode: Mode,
): boolean {
  if (!guards || guards.ok) return false;
  if (!BENIGN_GUARD_ERROR_CODES.has(guards.error_code ?? "")) return false;
  return mode === "production_safe";
}
