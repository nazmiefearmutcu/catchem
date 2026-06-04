import { t } from "@/lib/i18n";

/**
 * Narrow slice of the /api/quant/diagnostics payload this pill needs.
 *
 * The full endpoint returns `recent[]` (the last 50 failure records with
 * traceback heads) + `buffer_capacity`, but the hero chip only cares
 * about the headline count and the per-signal breakdown for its tooltip.
 * Keeping the prop type narrow means the pill can't accidentally couple
 * to fields it shouldn't render. Exported so HeroLiveRead types its prop
 * against the exact same contract.
 */
export type DegradedDiagnostics = {
  total_failures: number;
  per_signal: Record<string, number>;
};

/**
 * v72/v74 — "N signals degraded" warning chip for the QuantScan hero.
 *
 * Every quant signal runs through `_safe_call` (quant/engine.py) which
 * catches exceptions, returns None, and lets the dashboard keep
 * rendering. That fail-soft degradation is invisible unless surfaced —
 * this chip is the visible channel. It renders:
 *   - nothing in the healthy steady state (undefined diagnostics OR
 *     total_failures === 0), so a nominal dashboard stays quiet;
 *   - a warn-toned chip otherwise, whose tooltip lists each failing
 *     signal and its count, highest first.
 *
 * i18n: the label routes through t() with the `quant.degraded.label`
 * key. v74 fixed the v72 leak that baked the Turkish "sinyal degrade"
 * string directly into JSX (which would have rendered Turkish even in
 * English locale, defeating the v31 i18n layer + v71 parity gate).
 */
export function DegradedSignalsPill({
  diagnostics,
}: {
  diagnostics: DegradedDiagnostics | undefined;
}) {
  // Healthy steady state — render nothing. Returning null (not an empty
  // fragment) keeps the chip row's flex gap from reserving phantom space.
  if (!diagnostics || diagnostics.total_failures <= 0) return null;

  // Tooltip: "signal: count" lines, highest-failure signal first so the
  // worst offender is the first thing the operator reads. Mirrors the
  // descending sort used by `catchem signals --diagnostics` (v73) so the
  // CLI and UI agree on ordering.
  const tooltip = Object.entries(diagnostics.per_signal)
    .sort(([, a], [, b]) => b - a)
    .map(([signal, count]) => `${signal}: ${count}`)
    .join("\n");

  const label = t("quant.degraded.label").replace(
    "{n}",
    String(diagnostics.total_failures),
  );

  return (
    <span
      className="chip text-[10px] !border-warn/60 !text-warn"
      title={tooltip}
      data-testid="quant-degraded-pill"
    >
      ⚠ {label}
    </span>
  );
}
