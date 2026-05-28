import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { t, useLang } from "@/lib/i18n";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { isBenignGuardState } from "@/lib/guardState";
import { JargonTooltip } from "@/components/JargonTooltip";

export function OpsPage() {
  // Re-render every 30s so the hero freshness suffix keeps ticking.
  useTick();
  // Subscribe to locale changes so the "All systems nominal" headline
  // flips to Turkish ("Tüm sistemler normal") without a route remount.
  useLang();
  const summary = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const metrics = useQuery({ queryKey: ["metrics"], queryFn: api.metrics });
  const guards = useQuery({ queryKey: ["guards"], queryFn: api.guards });
  // 5s refetch matches the backend's 2s response cache headroom — the
  // operator sees fresh numbers without the storage lock being polled
  // raw at SPA refresh rate.
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: api.stats,
    refetchInterval: 5_000,
  });
  // Readiness probe — 30s cadence is loose enough that even a slow
  // subsystem check (e.g. disk usage on a network-mounted DB dir)
  // doesn't dominate the request log. Used by the "deep health" pill
  // in the runtime stats card header.
  const health = useQuery({
    queryKey: ["health-deep"],
    queryFn: api.healthDeep,
    refetchInterval: 30_000,
    // The endpoint returns 503 *on purpose* when something is wrong —
    // we already parse that body in api.healthDeep, so retries here
    // would just hammer the sidecar without adding signal.
    retry: false,
  });
  // v64: per-table SQLite breakdown. 60s cadence — table sizes don't
  // change every second on a single-machine analyst workstation.
  const dbStats = useQuery({
    queryKey: ["db-stats"],
    queryFn: api.dbStats,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const [rawOpen, setRawOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  if (summary.isLoading) return <Skeleton className="h-72" />;
  if (summary.error) return <ErrorBox err={summary.error} />;
  if (!summary.data) return null;
  const s = summary.data;
  const guardBenign = isBenignGuardState(guards.data, s.mode);

  const rawJson = JSON.stringify(
    {
      config: config.data,
      metrics: metrics.data,
      summary: {
        mode: s.mode,
        diagnostic_allowed: s.diagnostic_allowed,
        totals: s.totals,
      },
    },
    null,
    2,
  );

  const copyRaw = async () => {
    try {
      await navigator.clipboard.writeText(rawJson);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked — no-op */
    }
  };

  // Hero status synthesis: pick the loudest issue, fall back to "nominal".
  const dlqAlarm = s.dlq > 0;
  const diagnosticAlarm = s.diagnostic_count > 0;
  const guardLoaded = !!guards.data;
  const guardStrict = guardLoaded && guards.data?.ok && !guards.data.release_gate_passed;
  const heroTone: "good" | "warn" | "bad" =
    dlqAlarm ? "warn" : diagnosticAlarm ? "warn" : "good";
  const heroHeadline =
    dlqAlarm ? `DLQ has ${s.dlq.toLocaleString()} unprocessed record${s.dlq === 1 ? "" : "s"}` :
    diagnosticAlarm ? `${s.diagnostic_count} record${s.diagnostic_count === 1 ? "" : "s"} carry diagnostic stamps` :
    t("ops.title.nominal");
  const heroAccent =
    heroTone === "good" ? "border-good/40 from-good/15"
    : heroTone === "warn" ? "border-warn/40 from-warn/15"
    : "border-bad/40 from-bad/15";
  const dotAccent =
    heroTone === "good" ? "bg-good" : heroTone === "warn" ? "bg-warn" : "bg-bad";
  const eyebrowAccent =
    heroTone === "good" ? "text-good" : heroTone === "warn" ? "text-warn" : "text-bad";

  return (
    <div className="grid gap-5">
      {/* Hero: status synthesis + 4 KPI tiles for at-a-glance health. */}
      <section className={`relative overflow-hidden rounded-xl border ${heroAccent} bg-gradient-to-br via-[color:var(--bg-elev)] to-[color:var(--bg-elev)] p-6`}>
        <div
          aria-hidden
          className={`pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full ${heroTone === "good" ? "bg-good/20" : heroTone === "warn" ? "bg-warn/20" : "bg-bad/20"} blur-3xl`}
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${dotAccent} opacity-75`} />
              <span className={`relative inline-flex h-2 w-2 rounded-full ${dotAccent}`} />
            </span>
            <div>
              <div className={`text-[10px] uppercase tracking-[0.25em] ${eyebrowAccent} font-semibold`}>
                Ops · system status · {s.mode}
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {heroHeadline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                local-first runtime · {s.is_production_safe ? "production-safe" : "diagnostic"} · stubs {String(s.use_ml_stubs)}
                <span className="text-[10px] text-[color:var(--fg-dim)]"> · {freshnessLabel(summary.dataUpdatedAt)}</span>
              </div>
            </div>
          </div>
        </div>
        <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <OpsStat
            label="mode"
            value={s.is_production_safe ? "prod-safe" : "diagnostic"}
            hint={s.mode}
            tone={s.is_production_safe ? "good" : "warn"}
          />
          <OpsStat
            label="DLQ"
            value={s.dlq.toLocaleString()}
            hint={dlqAlarm ? "needs attention" : "clean"}
            tone={dlqAlarm ? "warn" : "good"}
          />
          <OpsStat
            label="records · relevant"
            value={`${s.totals.total.toLocaleString()} · ${s.totals.finance_relevant.toLocaleString()}`}
            hint={s.totals.total ? `${((s.totals.finance_relevant / s.totals.total) * 100).toFixed(0)}% relevant` : "no flow"}
          />
          <OpsStat
            label="NewsImpact gate"
            value={
              !guardLoaded ? "loading…" :
              guards.data && !guards.data.ok ? (guardBenign ? "not configured" : "guard error") :
              guardStrict ? "quarantined" :
              guards.data?.release_gate_passed ? "PASSED" : "—"
            }
            hint={
              !guardLoaded ? "" :
              guards.data?.governance_index_sha256
                ? `sha ${guards.data.governance_index_sha256.slice(0, 8)}…`
                : guardBenign ? "merged_news off" : ""
            }
            tone={
              !guardLoaded ? undefined :
              guards.data && !guards.data.ok ? (guardBenign ? "good" : "bad") :
              guardStrict ? "good" :
              guards.data?.release_gate_passed ? "bad" : undefined
            }
          />
        </div>
      </section>

      <RuntimeStatsCard
        data={stats.data}
        isLoading={stats.isLoading}
        error={stats.error}
        health={health.data}
        healthLoading={health.isLoading}
      />

      <DatabaseBreakdownCard
        data={dbStats.data}
        isLoading={dbStats.isLoading}
        error={dbStats.error}
      />

      <section className="grid gap-3 md:grid-cols-2">
        <GroupCard title="runtime mode" hint="how the supervisor is configured">
          <KVInline label="mode" value={s.mode} mono />
          <KVInline label={<JargonTooltip term="production_safe" />} value={String(s.is_production_safe)} tone={s.is_production_safe ? "good" : "warn"} />
          <KVInline label={<JargonTooltip term="diagnostic_allowed" />} value={String(s.diagnostic_allowed)} tone={s.diagnostic_allowed ? "warn" : "good"} />
          <KVInline label={<JargonTooltip term="use_ml_stubs" />} value={String(s.use_ml_stubs)} mono />
        </GroupCard>
        <GroupCard title="storage tier" hint="what the supervisor has materialized">
          <KVInline label="records.total" value={s.totals.total.toLocaleString()} mono />
          <KVInline label="records.finance_relevant" value={s.totals.finance_relevant.toLocaleString()} mono />
          <KVInline label={<JargonTooltip term="DLQ" />} value={String(s.dlq)} tone={s.dlq > 0 ? "warn" : "good"} mono />
          <KVInline label="diagnostic stamps" value={String(s.diagnostic_count)} mono tone={s.diagnostic_count > 0 ? "warn" : undefined} />
        </GroupCard>
      </section>

      <section className="card">
        <h2 className="label mb-2">NewsImpact guard</h2>
        {guards.isLoading ? <Skeleton className="h-12" /> :
          guards.error ? <ErrorBox err={guards.error} /> :
          !guards.data ? <ErrorBox err="guard snapshot unavailable" /> :
          !guards.data.ok && guardBenign ? (
            // BUG-OO: missing_governance_index + production_safe is the
            // common fresh-install case. Show informational state, not red error.
            <p className="text-xs text-[color:var(--fg-dim)]">
              NewsImpact (merged_news) is not configured on this machine
              (<code className="font-mono">{guards.data.error_code ?? "missing_governance_index"}</code>).
              Catchem runs fully in production-safe mode without it; the
              diagnostic path is opt-in via <code className="font-mono">CATCHEM_MODE=research_diagnostic</code>
              and a populated NewsImpact repo.
            </p>
          ) :
          !guards.data.ok ? <ErrorBox err={guardErrorText(guards.data)} /> : (
            <ul className="text-xs grid gap-x-6 gap-y-1 sm:grid-cols-2">
              <KVInline label={<JargonTooltip term="release_gate_passed" />} value={String(guards.data.release_gate_passed)} tone={guards.data.release_gate_passed ? "bad" : "good"} />
              <KVInline label={<JargonTooltip term="quarantine_state" />} value={guards.data.quarantine_state ?? "—"} />
              <KVInline
                label={<JargonTooltip term="fusion_verdict_class" />}
                value={guards.data.fusion_verdict_class ?? "—"}
              />
              <KVInline label="safe_to_publish" value={String(guards.data.safe_to_publish)} tone={guards.data.safe_to_publish ? "bad" : "good"} />
              <KVInline label="safe_to_promote" value={String(guards.data.safe_to_promote)} tone={guards.data.safe_to_promote ? "bad" : "good"} />
              <KVInline label="error_code" value={guardErrorCode(guards.data)} tone="warn" mono />
              <KVInline label={<JargonTooltip term="governance sha256" />} value={formatGovernanceHash(guards.data.governance_index_sha256)} mono />
            </ul>
          )}
        <GuardExplanation guards={guards.data} benign={guardBenign} />
      </section>

      <section className="card">
        <h2 className="label mb-2">model versions</h2>
        <ul className="text-xs grid gap-x-6 gap-y-1 sm:grid-cols-2">
          {Object.entries(s.model_versions).map(([k, v]) => (
            <KVInline key={k} label={k} value={v} mono />
          ))}
        </ul>
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">raw config payload</h2>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn text-[10px] py-0.5 px-2"
              onClick={copyRaw}
              title="Copy the full JSON payload"
            >
              {copied ? "copied ✓" : "copy JSON"}
            </button>
            <button
              type="button"
              className="btn text-[10px] py-0.5 px-2"
              onClick={() => setRawOpen((v) => !v)}
              aria-expanded={rawOpen}
            >
              {rawOpen ? "hide" : "show"}
            </button>
          </div>
        </div>
        {rawOpen ? (
          <pre className="text-[10px] overflow-x-auto max-h-72 rounded bg-[color:var(--bg-elev2)] p-2 leading-relaxed">
            {rawJson}
          </pre>
        ) : (
          <p className="text-[11px] text-[color:var(--fg-muted)]">
            {(rawJson.length / 1024).toFixed(1)} KB — collapsed by default to keep the page scannable.
            The grouped sections above already cover the day-to-day fields.
          </p>
        )}
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

function GuardExplanation({
  guards,
  benign,
}: {
  guards: { ok: boolean; release_gate_passed?: boolean } | undefined;
  benign: boolean;
}) {
  if (!guards) return null;
  // BUG-OO: the benign "merged_news not configured" case already has its
  // own friendly explanation above; the alarming "cannot prove the
  // release-gate state" wording is only for actual guard failures
  // (malformed/missing files in research_diagnostic, release_gate_flipped, …).
  if (benign) return null;
  if (!guards.ok) {
    return (
      <p className="text-[10px] text-warn mt-2">
        Guard snapshot unavailable — Catchem cannot prove the release-gate state from governance metadata.
      </p>
    );
  }
  if (guards.release_gate_passed) {
    return (
      <p className="text-[10px] text-bad mt-2">
        The release gate is <b>true</b> — verify governance before using this candidate outside diagnostics.
      </p>
    );
  }
  return (
    <p className="text-[10px] text-[color:var(--fg-muted)] mt-2">
      The release gate is intentionally <b className="text-good">false</b> — that means the candidate stays quarantined.
    </p>
  );
}

function GroupCard({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="card">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="label">{title}</h2>
        {hint && <span className="text-[10px] text-[color:var(--fg-muted)]">{hint}</span>}
      </div>
      <ul className="text-xs grid gap-y-1">{children}</ul>
    </div>
  );
}

function OpsStat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "good" | "warn" | "bad";
}) {
  const valueCls =
    tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${valueCls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </div>
  );
}

function KVInline({ label, value, tone, mono, title }: { label: ReactNode; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean; title?: string }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <li title={title} className="flex items-baseline justify-between gap-2">
      <span className="text-[color:var(--fg-dim)]">{label}</span>
      <span className={`${mono ? "font-mono" : ""} ${cls} text-right`}>{value}</span>
    </li>
  );
}

// ── Runtime stats card ──────────────────────────────────────────────────────
// Polled telemetry from GET /api/stats. Shows live uptime, total HTTP
// request volume, SQLite row counts, the busiest endpoint paths since the
// sidecar booted, and DeepSeek's cumulative USD spend (only when > 0).
//
// Read-only — never mutates server state. Designed to coexist with the
// hero KPIs above: the hero answers "is anything on fire right now?",
// this card answers "what has the sidecar been doing since it started?".

interface RuntimeStatsResponse {
  schema_version: number;
  generated_at: string;
  uptime_seconds: number;
  total_requests: number;
  request_counts: Record<string, number>;
  db: { records: number; reviews: number; dlq: number };
  reviewers: { deepseek_usd_spent: number; stub_active: boolean };
  // Process-level telemetry — RSS / CPU% / thread count. ``psutil_available``
  // is the truth flag for "was this a real reading or a resource.getrusage
  // estimate?". When false, the UI labels the row "(estimate)" so the
  // operator knows the CPU% will be 0 and RSS may be coarser than usual.
  process?: {
    rss_mb: number;
    vms_mb: number;
    cpu_percent: number;
    num_threads: number;
    psutil_available: boolean;
  };
  version?: string | null;
}

interface DeepHealthResponse {
  ok: boolean;
  checks: Record<string, unknown>;
  issues: string[];
  generated_at: string;
  schema_version: number;
}

/** Small status pill mirroring `/api/health/deep`. Green dot + "READY"
 * when `ok:true`; red badge + issue count when `ok:false`. Designed to
 * sit inline in the runtime stats card header so an operator scanning
 * the Ops page sees readiness state without opening a separate route. */
function DeepHealthPill({
  health,
  loading,
}: {
  health: DeepHealthResponse | undefined;
  loading: boolean;
}) {
  if (loading && !health) {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-0.5 text-[9px] uppercase tracking-wider text-[color:var(--fg-dim)]"
        data-testid="deep-health-pill-loading"
      >
        deep · checking…
      </span>
    );
  }
  if (!health) return null;
  const ok = health.ok;
  const issueCount = health.issues?.length ?? 0;
  const dotCls = ok ? "bg-good" : "bg-bad";
  const wrapCls = ok
    ? "border-good/40 text-good"
    : "border-bad/50 text-bad bg-bad/10";
  const label = ok ? "READY" : `${issueCount} ISSUE${issueCount === 1 ? "" : "S"}`;
  const tooltip = ok
    ? "All subsystems pass /api/health/deep"
    : `Subsystem issues:\n• ${health.issues.join("\n• ")}`;
  return (
    <span
      title={tooltip}
      className={`inline-flex items-center gap-1.5 rounded-full border ${wrapCls} px-2 py-0.5 text-[9px] uppercase tracking-wider font-semibold`}
      data-testid="deep-health-pill"
      data-ok={ok ? "1" : "0"}
    >
      <span className={`inline-flex h-1.5 w-1.5 rounded-full ${dotCls}`} />
      deep · {label}
    </span>
  );
}

/**
 * v64: per-table SQLite breakdown card. Drives off /api/db/stats so the
 * analyst sees row counts + page-derived size without dropping into the CLI.
 * Tables sorted by row count desc — biggest table on top.
 */
function DatabaseBreakdownCard({
  data,
  isLoading,
  error,
}: {
  data:
    | {
        tables: Array<{ name: string; rows: number }>;
        total_tables: number;
        total_indexes: number;
        estimated_size_bytes: number;
      }
    | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  if (isLoading) return null; // Skeleton would be noisy below the much-larger runtime card
  if (error || !data) return null;
  const sorted = [...data.tables].sort((a, b) => b.rows - a.rows);
  const sizeMb = data.estimated_size_bytes / 1024 / 1024;
  return (
    <section className="card">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="label">{t("ops.db_breakdown.title")}</h2>
        <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          {t("ops.db_breakdown.summary")
            .replace("{tables}", String(data.total_tables))
            .replace("{indexes}", String(data.total_indexes))
            .replace("{sizeMb}", sizeMb.toFixed(1))}
        </span>
      </div>
      <ul className="grid gap-1 text-xs">
        {sorted.map((t) => (
          <li key={t.name} className="grid grid-cols-[120px_1fr_80px] items-center gap-2">
            <span className="font-mono">{t.name}</span>
            <span className="h-2 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
              <span
                className={`block h-full ${t.rows === 0 ? "bg-[color:var(--bg-elev2)]" : "bg-accent/60"}`}
                style={{
                  width: sorted[0]?.rows
                    ? `${Math.max(2, (t.rows / sorted[0].rows) * 100)}%`
                    : "0%",
                }}
              />
            </span>
            <span className="text-right tabular-nums">{t.rows.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RuntimeStatsCard({
  data,
  isLoading,
  error,
  health,
  healthLoading,
}: {
  data: RuntimeStatsResponse | undefined;
  isLoading: boolean;
  error: unknown;
  health: DeepHealthResponse | undefined;
  healthLoading: boolean;
}) {
  if (isLoading && !data) {
    return (
      <section className="card">
        <h2 className="label mb-2">runtime stats</h2>
        <Skeleton className="h-24" />
      </section>
    );
  }
  if (error && !data) {
    return (
      <section className="card">
        <h2 className="label mb-2">runtime stats</h2>
        <ErrorBox err={error} />
      </section>
    );
  }
  if (!data) return null;

  // Sort paths descending by hit count, keep the top 5. Falls back to an
  // empty list when the sidecar just booted and the counter map is still
  // sparse — the card renders an inline "no traffic yet" hint instead.
  const topPaths = Object.entries(data.request_counts)
    .filter(([k]) => !!k)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  const spent = data.reviewers?.deepseek_usd_spent ?? 0;
  const showSpend = Number.isFinite(spent) && spent > 0;

  return (
    <section className="card">
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <h2 className="label">runtime stats</h2>
        <div className="flex items-center gap-2">
          <DeepHealthPill health={health} loading={healthLoading} />
          <span className="text-[10px] text-[color:var(--fg-dim)]">
            live · 5s refresh
          </span>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 md:grid-cols-3">
        <OpsStat
          label="uptime"
          value={fmtUptime(data.uptime_seconds)}
          hint={`since ${new Date(Date.now() - data.uptime_seconds * 1000).toLocaleTimeString()}`}
        />
        <OpsStat
          label="total requests"
          value={data.total_requests.toLocaleString()}
          hint={topPaths.length ? `${topPaths.length} distinct paths` : "no traffic yet"}
        />
        {showSpend && (
          <OpsStat
            label="DeepSeek spend"
            value={`$${spent.toFixed(4)}`}
            hint="cumulative usd"
            tone="warn"
          />
        )}
      </div>

      <div className="mt-4">
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
          SQLite rows
        </div>
        <div className="grid gap-2 grid-cols-3 text-xs">
          <DbStat label="records" value={data.db.records} />
          <DbStat label="reviews" value={data.db.reviews} />
          <DbStat label="dlq" value={data.db.dlq} tone={data.db.dlq > 0 ? "warn" : undefined} />
        </div>
      </div>

      <ProcessRow process={data.process} />

      <div className="mt-4">
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
          top 5 paths · since boot
        </div>
        {topPaths.length === 0 ? (
          <p className="text-[11px] text-[color:var(--fg-dim)]">
            No requests recorded yet. Open the Feed or Analysis tab to
            generate traffic.
          </p>
        ) : (
          <ul className="text-xs grid gap-1">
            {topPaths.map(([path, count]) => (
              <li
                key={path}
                className="flex items-baseline justify-between gap-2 font-mono"
              >
                <span className="truncate text-[color:var(--fg-dim)]" title={path}>
                  {path}
                </span>
                <span className="tabular-nums text-[color:var(--fg)]">
                  {count.toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/** Process telemetry row — RSS / CPU% / threads from psutil (or the
 * resource.getrusage fallback). When ``psutil_available`` is false the
 * heading is suffixed with "(estimate)" and the CPU%/threads cells are
 * dimmed because rusage doesn't expose them; the RSS reading is still
 * approximately right (peak resident size in MB). Renders nothing when
 * the sidecar didn't supply a ``process`` block (older build).
 *
 * Test-IDs: ``process-row`` (container), ``process-rss`` (RSS cell),
 * ``process-cpu`` (CPU%), ``process-threads`` (thread count). The card
 * stays renderable even when the backend omits the block — so older
 * sidecars don't break the Ops page during a rolling deploy.
 */
function ProcessRow({
  process,
}: {
  process: RuntimeStatsResponse["process"];
}) {
  if (!process) return null;
  const available = process.psutil_available;
  const dimCls = available ? "" : "opacity-60";
  const headingSuffix = available ? "" : " · (estimate)";
  const rssLabel = `${process.rss_mb.toFixed(1)} MB`;
  const cpuLabel = available ? `${process.cpu_percent.toFixed(1)}%` : "—";
  const threadLabel = available ? process.num_threads.toLocaleString() : "—";
  // Compact summary line so the page passes a glance — full triple cells
  // sit below for the analyst who wants to compare snapshots.
  const summary = available
    ? `Process: ${rssLabel} RSS · ${threadLabel} thread${
        process.num_threads === 1 ? "" : "s"
      } · ${cpuLabel} CPU`
    : `Process: ~${rssLabel} RSS (estimate — psutil not installed)`;
  return (
    <div className="mt-4" data-testid="process-row">
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
        process telemetry{headingSuffix}
      </div>
      <div className="text-[11px] text-[color:var(--fg-dim)] mb-2 font-mono">
        {summary}
      </div>
      <div className={`grid gap-2 grid-cols-3 text-xs ${dimCls}`}>
        <div
          className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
          data-testid="process-rss"
        >
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            RSS (MB)
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {Math.round(process.rss_mb).toLocaleString()}
          </div>
        </div>
        <div
          className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
          data-testid="process-cpu"
        >
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            CPU %
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {cpuLabel}
          </div>
        </div>
        <div
          className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
          data-testid="process-threads"
        >
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            threads
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {threadLabel}
          </div>
        </div>
      </div>
    </div>
  );
}

function DbStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "good" | "warn" | "bad";
}) {
  const cls =
    tone === "good"
      ? "text-good"
      : tone === "warn"
      ? "text-warn"
      : tone === "bad"
      ? "text-bad"
      : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>
        {value.toLocaleString()}
      </div>
    </div>
  );
}

/** Humanize an uptime measured in seconds.
 *
 * Format ladder:
 *   <60s   → "12s"
 *   <60m   → "5m 12s"
 *   <24h   → "3h 5m"
 *   else   → "2d 3h 5m"
 *
 * The colon-free, unit-suffixed format reads in either locale and lines
 * up with the OS uptime convention an operator already knows. The
 * highest unit is always shown so a sub-second clock skew never displays
 * as a blank string. */
export function fmtUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const minutes = Math.floor(s / 60);
  const secs = s % 60;
  if (minutes < 60) return `${minutes}m ${secs}s`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours < 24) return `${hours}h ${mins}m`;
  const days = Math.floor(hours / 24);
  const hrs = hours % 24;
  return `${days}d ${hrs}h ${mins}m`;
}
