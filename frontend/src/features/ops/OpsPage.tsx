import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Skeleton, ErrorBox } from "@/components/Skeleton";

export function OpsPage() {
  const summary = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const metrics = useQuery({ queryKey: ["metrics"], queryFn: api.metrics });
  const guards = useQuery({ queryKey: ["guards"], queryFn: api.guards });

  if (summary.isLoading) return <Skeleton className="h-72" />;
  if (summary.error) return <ErrorBox err={summary.error} />;
  if (!summary.data) return null;
  const s = summary.data;

  return (
    <div className="grid gap-3">
      <h1 className="text-lg font-bold">System / Ops</h1>

      <section className="grid md:grid-cols-2 gap-3">
        <KV label="mode" value={s.mode} mono />
        <KV label="production_safe" value={String(s.is_production_safe)} />
        <KV label="diagnostic_allowed" value={String(s.diagnostic_allowed)} />
        <KV label="use_ml_stubs" value={String(s.use_ml_stubs)} />
        <KV label="records.total" value={s.totals.total.toLocaleString()} />
        <KV label="records.finance_relevant" value={s.totals.finance_relevant.toLocaleString()} />
        <KV label="DLQ" value={String(s.dlq)} tone={s.dlq > 0 ? "warn" : undefined} />
        <KV label="diagnostic stamps" value={String(s.diagnostic_count)} />
      </section>

      <section className="card">
        <h2 className="label mb-2">NewsImpact guard</h2>
        {guards.isLoading ? <Skeleton className="h-12" /> :
          guards.error ? <ErrorBox err={guards.error} /> :
          !guards.data ? <ErrorBox err="guard snapshot unavailable" /> :
          !guards.data.ok ? <ErrorBox err={guardErrorText(guards.data)} /> : (
            <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
              <KVInline label="release_gate_passed" value={String(guards.data.release_gate_passed)} tone={guards.data.release_gate_passed ? "bad" : "good"} />
              <KVInline label="quarantine_state" value={guards.data.quarantine_state ?? "—"} />
              <KVInline
                label="fusion_verdict_class"
                value={guards.data.fusion_verdict_class ?? "—"}
                title="External governance contract from merged_news (newsimpact). The 'FUSION_*' prefix is intentionally pinned — Catchem mirrors the upstream class verbatim and does not rename it locally."
              />
              <KVInline label="safe_to_publish" value={String(guards.data.safe_to_publish)} tone={guards.data.safe_to_publish ? "bad" : "good"} />
              <KVInline label="safe_to_promote" value={String(guards.data.safe_to_promote)} tone={guards.data.safe_to_promote ? "bad" : "good"} />
              <KVInline label="error_code" value={guardErrorCode(guards.data)} tone="warn" mono />
              <KVInline label="governance sha256" value={formatGovernanceHash(guards.data.governance_index_sha256)} mono />
            </ul>
          )}
        <p className="text-[10px] text-[color:var(--fg-muted)] mt-2">
          The release gate is intentionally <b className="text-good">false</b> — that means the candidate stays quarantined.
        </p>
      </section>

      <section className="card">
        <h2 className="label mb-2">model versions</h2>
        <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
          {Object.entries(s.model_versions).map(([k, v]) => (
            <KVInline key={k} label={k} value={v} mono />
          ))}
        </ul>
      </section>

      <section className="card">
        <h2 className="label mb-2">raw config payload</h2>
        <pre className="text-[10px] overflow-x-auto max-h-72">
          {JSON.stringify({ config: config.data, metrics: metrics.data, summary: { mode: s.mode, diagnostic_allowed: s.diagnostic_allowed, totals: s.totals } }, null, 2)}
        </pre>
      </section>
    </div>
  );
}

function guardErrorCode(guards: { ok: boolean; error_code?: string | null }) {
  return guards.error_code || "—";
}

function guardErrorText(guards: { error?: string | null; error_code?: string | null }) {
  const message = guards.error ?? "guard error";
  return guards.error_code ? `${guards.error_code}: ${message}` : message;
}

function formatGovernanceHash(hash: string | null | undefined) {
  return hash ? `${hash.slice(0, 16)}…` : "not reported";
}

function KV({ label, value, tone, mono }: { label: string; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 ${mono ? "font-mono" : ""} text-sm ${cls}`}>{value}</div>
    </div>
  );
}

function KVInline({ label, value, tone, mono, title }: { label: string; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean; title?: string }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <li title={title}>
      <span className="text-[color:var(--fg-dim)]">{label}</span>{" "}
      <span className={`${mono ? "font-mono" : ""} ${cls}`}>{value}</span>
    </li>
  );
}
