import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Skeleton, ErrorBox } from "@/components/Skeleton";

export function ModelControlsPage() {
  const qc = useQueryClient();
  const info = useQuery({ queryKey: ["app-info"], queryFn: api.appInfo, refetchInterval: 10_000 });
  const status = useQuery({ queryKey: ["sidecar-status"], queryFn: api.sidecarStatus, refetchInterval: 4_000 });
  const logs = useQuery({ queryKey: ["log-tail"], queryFn: () => api.logTail(200), refetchInterval: 6_000 });
  const guards = useQuery({ queryKey: ["guards"], queryFn: api.guards, refetchInterval: 10_000 });

  if (info.isLoading || status.isLoading) return <Skeleton className="h-72" />;
  if (info.error) return <ErrorBox err={info.error} />;
  if (!info.data || !status.data) return null;

  const a = info.data;
  const s = status.data;
  const usingStubs = a.use_ml_stubs;

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-bold">Model Controls</h1>
        <button className="btn ml-auto" onClick={() => {
          qc.invalidateQueries({ queryKey: ["app-info"] });
          qc.invalidateQueries({ queryKey: ["sidecar-status"] });
        }}>refresh</button>
      </header>

      {/* Top strip: mode + ml + diagnostic */}
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Card label="mode" value={a.mode} tone={a.mode === "production_safe" ? "good" : "warn"} mono />
        <Card label="ML path" value={usingStubs ? "stubs" : "HF"} tone={usingStubs ? "warn" : "good"}
              hint={usingStubs ? "deterministic; no network" : "real HF models active"} />
        <Card label="diagnostic" value={s.diagnostic_enabled ? "ON" : "off"}
              tone={s.diagnostic_enabled ? "bad" : "good"} />
        <Card label="sidecar" value={s.healthy ? "healthy" : "down"} tone={s.healthy ? "good" : "bad"}
              hint={`pid ${s.pid} · uptime ${Math.round(s.uptime_seconds)}s`} />
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
          <KV label="diagnostic allowed" value={String(a.diagnostic_allowed)} tone={a.diagnostic_allowed ? "warn" : "good"} />
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
            Stubs are deterministic, CPU-only, and explicitly labeled. They produce 100% on the
            synthetic golden set. To switch to real Hugging Face models, run the bootstrap with
            <code className="font-mono ml-1">--with-ml</code>.
          </p>
        )}
      </section>

      {/* Guards */}
      <section className="card">
        <h2 className="label mb-2">NewsImpact guard</h2>
        {!guards.data ? <Skeleton className="h-12" /> : !guards.data.ok ? (
          <ErrorBox err={guards.data.error ?? "guard error"} />
        ) : (
          <ul className="text-xs grid sm:grid-cols-2 gap-y-1">
            <KVInline label="release_gate_passed" value={String(guards.data.release_gate_passed)}
                      tone={guards.data.release_gate_passed ? "bad" : "good"} />
            <KVInline label="quarantine" value={guards.data.quarantine_state ?? "—"} />
            <KVInline label="verdict" value={guards.data.fusion_verdict_class ?? "—"} />
            <KVInline label="safe_to_publish" value={String(guards.data.safe_to_publish)}
                      tone={guards.data.safe_to_publish ? "bad" : "good"} />
            <KVInline label="safe_to_promote" value={String(guards.data.safe_to_promote)}
                      tone={guards.data.safe_to_promote ? "bad" : "good"} />
            <KVInline label="governance sha256" value={(guards.data.governance_index_sha256 ?? "—").slice(0, 16) + "…"} mono />
          </ul>
        )}
      </section>

      {/* Log tail */}
      <section className="card">
        <h2 className="label mb-2">log tail</h2>
        {logs.isLoading ? <Skeleton className="h-32" /> :
          (logs.data?.lines.length ?? 0) === 0 ? (
            <p className="text-xs text-[color:var(--fg-dim)]">no log lines yet</p>
          ) : (
            <pre className="max-h-72 overflow-auto text-[10px] leading-relaxed bg-[color:var(--bg-elev2)] rounded p-2 font-mono">
              {(logs.data!.lines).join("\n")}
            </pre>
          )}
        {logs.data?.truncated && (
          <div className="mt-1 text-[10px] text-[color:var(--fg-muted)]">log truncated to last 200 lines</div>
        )}
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

function Card({ label, value, hint, tone, mono }: { label: string; value: string; hint?: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${cls} ${mono ? "font-mono" : ""}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] mt-0.5">{hint}</div>}
    </div>
  );
}

function KV({ label, value, tone, mono }: { label: string; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <li>
      <span className="text-[color:var(--fg-dim)]">{label}</span>{" "}
      <span className={`${mono ? "font-mono" : ""} ${cls}`}>{value}</span>
    </li>
  );
}

function KVInline({ label, value, tone, mono }: { label: string; value: string; tone?: "good" | "bad" | "warn"; mono?: boolean }) {
  return <KV label={label} value={value} tone={tone} mono={mono} />;
}
