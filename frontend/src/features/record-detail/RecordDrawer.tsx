import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, fmtDate, fmtScore, safeHref } from "@/lib/api";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox } from "@/components/Skeleton";

interface Props {
  captureId: string;
  onClose: () => void;
}

export function RecordDrawer({ captureId, onClose }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["record", captureId],
    queryFn: () => api.record(captureId),
  });
  const [showRaw, setShowRaw] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label="Record detail"
      onClick={onClose}
    >
      <aside
        className="w-full sm:w-[640px] h-full bg-[color:var(--bg)] border-l border-[color:var(--border)] overflow-auto animate-slide-in"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="sticky top-0 z-10 bg-[color:var(--bg)] border-b border-[color:var(--border)] px-4 py-3 flex items-center justify-between">
          <div className="text-xs text-[color:var(--fg-dim)] truncate" title={captureId}>{captureId}</div>
          <button ref={closeRef} className="btn" onClick={onClose} aria-label="Close">esc · close</button>
        </header>
        <div className="p-4 grid gap-4">
          {isLoading && <DetailSkeleton />}
          {error && <ErrorBox err={error} />}
          {data && (
            <>
              <section>
                <div className="text-[10px] text-[color:var(--fg-dim)] mb-1">
                  {fmtDate(data.published_ts) || fmtDate(data.created_at)} · {data.domain ?? "—"} · {data.language ?? "—"}
                </div>
                <h2 className="text-lg font-semibold leading-snug">{data.title ?? "(untitled)"}</h2>
                {safeHref(data.url) && (
                  <a href={safeHref(data.url)} target="_blank" rel="noopener noreferrer" className="text-xs text-accent hover:underline">
                    open source ↗
                  </a>
                )}
              </section>

              <section className="grid grid-cols-2 gap-3">
                <Card label="finance score" value={fmtScore(data.finance_relevance_score)}
                      tone={data.is_finance_relevant ? "good" : "bad"} />
                <Card label="relevant" value={data.is_finance_relevant ? "YES" : "no"}
                      tone={data.is_finance_relevant ? "good" : "bad"} />
                <Card label="sentiment" value={data.sentiment_label ?? "—"} hint={data.sentiment_score != null ? fmtScore(data.sentiment_score) : undefined} />
                <Card label="processing mode" value={data.processing_mode} />
              </section>

              <section>
                <div className="label mb-1">labels</div>
                <div className="flex flex-wrap gap-1">
                  {data.asset_classes.map((ac) => <Pill key={ac} variant="ac">{ac}</Pill>)}
                  {data.impact_reason_codes.map((rc) => <Pill key={rc} variant="rc">{rc}</Pill>)}
                  {data.impact_horizons.map((h) => <Pill key={h}>{h}</Pill>)}
                  {data.asset_classes.length + data.impact_reason_codes.length === 0 && (
                    <span className="text-xs text-[color:var(--fg-dim)]">none</span>
                  )}
                </div>
              </section>

              {(data.candidate_symbols.length > 0 || data.candidate_entities.length > 0) && (
                <section>
                  <div className="label mb-1">symbols / entities</div>
                  <div className="flex flex-wrap gap-1">
                    {data.candidate_symbols.map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
                    {data.candidate_entities.slice(0, 12).map((e) => <Pill key={e}>{e}</Pill>)}
                  </div>
                </section>
              )}

              <section>
                <div className="label mb-1">evidence sentences</div>
                {data.evidence_sentences.length === 0 ? (
                  <p className="text-xs text-[color:var(--fg-dim)]">no extractive evidence picked</p>
                ) : (
                  <ul className="space-y-1">
                    {data.evidence_sentences.map((s, i) => (
                      <li key={i} className="text-sm italic text-[color:var(--fg-dim)] before:content-['“'] after:content-['”']">{s}</li>
                    ))}
                  </ul>
                )}
                {data.reason_text && (
                  <p className="mt-2 text-[10px] text-[color:var(--fg-muted)] font-mono">{data.reason_text}</p>
                )}
              </section>

              <section>
                <div className="label mb-1">component scores</div>
                <ComponentTable scores={data.component_scores as unknown as Record<string, number>} />
              </section>

              <section>
                <div className="label mb-1">model versions</div>
                <ul className="text-xs grid grid-cols-2 gap-y-1">
                  {Object.entries(data.model_versions as unknown as Record<string, string>).map(([k, v]) => (
                    <li key={k}><span className="text-[color:var(--fg-dim)]">{k}</span> <span>{v}</span></li>
                  ))}
                </ul>
              </section>

              {data.diagnostic_multimodal_enabled && data.diagnostic_multimodal_result && (
                <section className="card border-warn/40 bg-warn/5">
                  <div className="label mb-1 text-warn">diagnostic (read-only)</div>
                  <pre className="text-[10px] overflow-x-auto">{JSON.stringify(data.diagnostic_multimodal_result, null, 2)}</pre>
                </section>
              )}

              <section>
                <button className="btn" onClick={() => setShowRaw((v) => !v)}>
                  {showRaw ? "hide raw JSON" : "show raw JSON"}
                </button>
                {showRaw && (
                  <pre className="mt-2 max-h-72 overflow-auto rounded border border-[color:var(--border)] bg-[color:var(--bg-elev)] p-2 text-[10px]">
                    {JSON.stringify(data, null, 2)}
                  </pre>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function Card({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "good" | "bad" }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)]">{hint}</div>}
    </div>
  );
}

function ComponentTable({ scores }: { scores: Record<string, number> }) {
  const entries = Object.entries(scores).sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) return <p className="text-xs text-[color:var(--fg-dim)]">none</p>;
  const max = Math.max(...entries.map(([, n]) => Math.abs(n) || 0), 1);
  return (
    <ul className="grid gap-1">
      {entries.map(([k, v]) => (
        <li key={k} className="grid grid-cols-[160px_1fr_60px] gap-2 items-center text-xs">
          <span className="text-[color:var(--fg-dim)] truncate" title={k}>{k}</span>
          <span className="h-1.5 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
            <span className="block h-full bg-accent/70" style={{ width: `${100 * Math.min(1, Math.abs(v) / max)}%` }} />
          </span>
          <span className="text-right tabular-nums">{v.toFixed(3)}</span>
        </li>
      ))}
    </ul>
  );
}

function DetailSkeleton() {
  return (
    <div className="grid gap-3" aria-busy="true">
      <Skeleton className="h-6 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-32" />
      <Skeleton className="h-24" />
    </div>
  );
}
