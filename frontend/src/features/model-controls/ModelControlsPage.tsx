import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { isBenignGuardState } from "@/lib/guardState";
import { JargonTooltip } from "@/components/JargonTooltip";
import type { Mode } from "@/types/api";

export function ModelControlsPage() {
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["app-info"], queryFn: api.appInfo, refetchInterval: 10_000 });
  const status = useQuery({ queryKey: ["sidecar-status"], queryFn: api.sidecarStatus, refetchInterval: 4_000 });
  const guards = useQuery({ queryKey: ["guards"], queryFn: api.guards, refetchInterval: 10_000 });

  if (info.isLoading || status.isLoading) return <Skeleton className="h-72" />;
  if (info.error) return <ErrorBox err={info.error} />;
  if (!info.data || !status.data) return null;

  const a = info.data;
  const s = status.data;
  const usingStubs = a.use_ml_stubs;

  // Hero synthesis: the loudest signal wins headline; sidecar down >>
  // diagnostic-on >> stubs-on >> nominal.
  const sidecarDown = !s.healthy;
  const heroTone: "good" | "warn" | "bad" =
    sidecarDown ? "bad"
    : s.diagnostic_enabled ? "warn"
    : usingStubs ? "warn"
    : "good";
  const heroHeadline =
    sidecarDown ? "Sidecar is unreachable"
    : s.diagnostic_enabled ? "Diagnostic path is enabled"
    : usingStubs ? "Running on deterministic ML stubs"
    : "Real HF models active";
  const heroAccent =
    heroTone === "good" ? "border-good/40 from-good/15"
    : heroTone === "warn" ? "border-warn/40 from-warn/15"
    : "border-bad/40 from-bad/15";
  const dotAccent =
    heroTone === "good" ? "bg-good" : heroTone === "warn" ? "bg-warn" : "bg-bad";
  const eyebrowAccent =
    heroTone === "good" ? "text-good" : heroTone === "warn" ? "text-warn" : "text-bad";
  const uptimeMin = Math.round(s.uptime_seconds / 60);
  const uptimeStr = uptimeMin >= 60 ? `${Math.floor(uptimeMin / 60)}h ${uptimeMin % 60}m` : `${uptimeMin}m`;

  return (
    <div className="grid gap-5">
      {/* Hero: synthesized model/sidecar status + 4 critical KPI tiles. */}
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
                Model Controls · runtime provenance
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {heroHeadline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                v{a.version} · {a.branch ?? "no branch"} · {(a.commit_sha ?? "—").slice(0, 7)} ·
                pid {s.pid} · uptime {uptimeStr}
              </div>
            </div>
          </div>
          <button className="btn shrink-0 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent" onClick={() => {
            qc.invalidateQueries({ queryKey: ["app-info"] });
            qc.invalidateQueries({ queryKey: ["sidecar-status"] });
          }}>refresh</button>
        </div>
        <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <MCStat
            label="mode"
            value={a.mode === "production_safe" ? "prod-safe" : a.mode}
            hint={a.diagnostic_allowed ? "diagnostic allowed" : "diagnostic blocked"}
            tone={a.mode === "production_safe" ? "good" : "warn"}
          />
          <MCStat
            label="ML path"
            value={usingStubs ? "stubs" : "HF"}
            hint={usingStubs ? "deterministic · no net" : "real HF models"}
            tone={usingStubs ? "warn" : "good"}
          />
          <MCStat
            label="diagnostic"
            value={s.diagnostic_enabled ? "ON" : "off"}
            hint={s.diagnostic_enabled ? "writes diag stamps" : "writes are clean"}
            tone={s.diagnostic_enabled ? "bad" : "good"}
          />
          <MCStat
            label="sidecar"
            value={s.healthy ? "healthy" : "down"}
            hint={`${s.api_host}:${s.api_port}`}
            tone={s.healthy ? "good" : "bad"}
          />
        </div>
      </section>

      {/* Connection details */}
      <section className="card">
        <h2 className="label mb-2">connection</h2>
        <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
          <KV label="api" value={`${s.api_host}:${s.api_port}`} mono />
          <KV label="version" value={a.version} mono />
          <KV label="branch" value={a.branch ?? "—"} mono />
          <KV label="commit" value={a.commit_sha ?? "—"} mono />
          <KV label="bundle present" value={String(a.static_bundle_present)} tone={a.static_bundle_present ? "good" : "bad"} />
          <KV label={<JargonTooltip term="diagnostic_allowed">diagnostic allowed</JargonTooltip>} value={String(a.diagnostic_allowed)} tone={a.diagnostic_allowed ? "warn" : "good"} />
        </ul>
      </section>

      {/* Model versions */}
      <section className="card">
        <h2 className="label mb-2">model versions (provenance)</h2>
        <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
          {Object.entries(a.model_versions).map(([k, v]) => {
            const isStub = typeof v === "string" && v.startsWith("stub-");
            return (
              <KVInline key={k} label={k} value={v} mono tone={isStub ? "warn" : "good"} />
            );
          })}
        </ul>
        {usingStubs && (
          <p className="mt-2 text-[10px] text-[color:var(--fg-muted)]">
            <JargonTooltip term="use_ml_stubs">Stubs</JargonTooltip> are deterministic, CPU-only, and explicitly labeled. They produce 100% on the
            synthetic golden set. To switch to real Hugging Face models, run the bootstrap with
            <code className="font-mono ml-1">--with-ml</code>.
          </p>
        )}
      </section>

      {/* Guards */}
      <section className="card">
        <h2 className="label mb-2">NewsImpact guard</h2>
        {!guards.data ? <Skeleton className="h-12" /> :
          !guards.data.ok && isBenignGuardState(guards.data, a.mode as Mode) ? (
            // BUG-OO: missing_governance_index + production_safe is the
            // common fresh-install case. Show informational note, not error.
            <p className="text-xs text-[color:var(--fg-dim)]">
              <JargonTooltip term="NewsImpact" /> (merged_news) is not configured. Catchem operates in
              <span> </span><JargonTooltip term="production_safe">production-safe</JargonTooltip> mode without it; diagnostic features stay off.
            </p>
          ) : !guards.data.ok ? (
          <ErrorBox err={guards.data.error ?? "guard error"} />
        ) : (
          <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
            <KVInline label={<JargonTooltip term="release_gate_passed" />} value={String(guards.data.release_gate_passed)}
                      tone={guards.data.release_gate_passed ? "bad" : "good"} />
            <KVInline label={<JargonTooltip term="quarantine_state">quarantine</JargonTooltip>} value={guards.data.quarantine_state ?? "—"} />
            <KVInline label={<JargonTooltip term="fusion_verdict_class">verdict</JargonTooltip>} value={guards.data.fusion_verdict_class ?? "—"} />
            <KVInline label="safe_to_publish" value={String(guards.data.safe_to_publish)}
                      tone={guards.data.safe_to_publish ? "bad" : "good"} />
            <KVInline label="safe_to_promote" value={String(guards.data.safe_to_promote)}
                      tone={guards.data.safe_to_promote ? "bad" : "good"} />
            <KVInline label={<JargonTooltip term="governance sha256" />} value={(guards.data.governance_index_sha256 ?? "—").slice(0, 16) + "…"} mono />
          </ul>
        )}
      </section>

      {/* Log tail — promoted to its own /logs page in v24 so we link out
          instead of duplicating the (now richer) viewer here. */}
      <section className="card">
        <h2 className="label mb-2">sidecar logs</h2>
        <p className="text-xs text-[color:var(--fg-dim)]">
          Live tail with level filter, search, pause, auto-scroll, and copy
          lives on the dedicated page.
        </p>
        <div className="mt-2">
          <Link to="/logs" className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">View logs →</Link>
        </div>
      </section>

      {/* Sidecar control note */}
      <section className="card border-warn/40 bg-warn/5">
        <h2 className="label text-warn mb-1">sidecar control</h2>
        <p className="text-xs text-[color:var(--fg-dim)]">
          Start / restart / stop is handled by the Catchem desktop shell (Tauri).
          In the browser, the API process is owned by whoever launched
          <code className="font-mono ml-1">catchem serve</code>. See Help for restart instructions.
        </p>
      </section>
    </div>
  );
}

function MCStat({
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
  const cls =
    tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </div>
  );
}

function KV({ label, value, tone, mono }: { label: ReactNode; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <li>
      <span className="text-[color:var(--fg-dim)]">{label}</span>{" "}
      <span className={`${mono ? "font-mono" : ""} ${cls}`}>{value}</span>
    </li>
  );
}

function KVInline({ label, value, tone, mono }: { label: ReactNode; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  return <KV label={label} value={value} tone={tone} mono={mono} />;
}
