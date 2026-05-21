import { Link } from "react-router-dom";
import type { DemoRunResponse } from "@/types/api";
import { fmtScore, safeHref } from "@/lib/api";
import { Pill } from "@/components/Pill";

/**
 * Compact result card rendered after a paste/upload analysis. Always shows
 * the production-safe diagnostic banner when applicable.
 */
export function AnalysisSummary({ result }: { result: DemoRunResponse }) {
  const r = result.record;
  const href = safeHref(r.url ?? undefined);
  const isProd = r.processing_mode === "production_safe";
  const relevant = r.is_finance_relevant;

  return (
    <article className="card" aria-label="Analysis result">
      <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
        <h2 className="text-base font-semibold">Analysis result</h2>
        <div className="flex items-center gap-2 text-[10px] text-[color:var(--fg-dim)]">
          {/* Truncated visually; full id available on hover + via copy JSON. */}
          <span title={r.capture_id} data-testid="capture-id-display">
            capture_id <code className="font-mono">{r.capture_id.slice(0, 16)}…</code>
          </span>
          <span>·</span>
          <span>jsonl <code className="font-mono">{result.jsonl_basename}</code></span>
        </div>
      </header>

      <div className="grid gap-3 grid-cols-2 md:grid-cols-4 mb-3">
        <div className="card">
          <div className="label">finance relevant</div>
          <div className={`mt-1 text-xl font-semibold ${relevant ? "text-good" : "text-bad"}`}>
            {relevant ? "YES" : "no"}
          </div>
        </div>
        <div className="card">
          <div className="label">score</div>
          <div className="mt-1 text-xl font-semibold tabular-nums">{fmtScore(r.finance_relevance_score)}</div>
        </div>
        <div className="card">
          <div className="label">sentiment</div>
          <div className={`mt-1 text-xl font-semibold ${
            r.sentiment_label === "positive" ? "text-good" :
            r.sentiment_label === "negative" ? "text-bad" : "text-[color:var(--fg-dim)]"
          }`}>{r.sentiment_label ?? "—"}</div>
        </div>
        <div className="card">
          <div className="label">processed / skipped</div>
          <div className="mt-1 text-xl font-semibold tabular-nums">
            {result.processed} <span className="text-[color:var(--fg-dim)]">/</span> {result.skipped}
          </div>
        </div>
      </div>

      {r.title && (
        <h3 className="text-sm font-semibold mb-1">
          {href ? (
            <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
              {r.title}
            </a>
          ) : r.title}
        </h3>
      )}
      {r.domain && (
        <div className="text-[10px] text-[color:var(--fg-dim)] mb-2">{r.domain}</div>
      )}

      <div className="flex flex-wrap gap-1 mb-3">
        {r.asset_classes.map((x) => <Pill key={`ac-${x}`} variant="ac">{x}</Pill>)}
        {r.impact_reason_codes.slice(0, 6).map((x) => <Pill key={`rc-${x}`} variant="rc">{x}</Pill>)}
        {r.candidate_symbols.slice(0, 6).map((x) => <Pill key={`s-${x}`} variant="sym">{x}</Pill>)}
      </div>

      {r.evidence_sentences.length > 0 && (
        <section className="mb-3">
          <div className="label mb-1">evidence</div>
          <ul className="space-y-1">
            {r.evidence_sentences.slice(0, 3).map((s, i) => (
              <li key={i} className="text-sm italic text-[color:var(--fg-dim)] before:content-['“'] after:content-['”']">
                {s}
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="flex flex-wrap items-center gap-2 pt-2 border-t border-[color:var(--border-subtle)]">
        <Link to={`/feed/${encodeURIComponent(r.capture_id)}`} className="btn btn-accent text-xs">
          open in feed →
        </Link>
        <button
          className="btn text-xs"
          onClick={() => navigator.clipboard?.writeText(JSON.stringify(result, null, 2))}
          title="Copy the full DemoRunResponse JSON"
        >
          copy JSON
        </button>
        <span className="ml-auto text-[10px] text-[color:var(--fg-dim)]">
          mode {r.processing_mode}
          {!isProd && <span className="ml-2 text-warn font-semibold">DIAGNOSTIC</span>}
        </span>
      </footer>
    </article>
  );
}
