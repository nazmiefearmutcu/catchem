import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

const SHORTCUTS = [
  { keys: "⌘K / Ctrl+K", description: "Open the command palette" },
  { keys: "g o", description: "Overview" },
  { keys: "g f", description: "Live Feed" },
  { keys: "g r", description: "Replay/Upload" },
  { keys: "g a", description: "Analysis" },
  { keys: "g m", description: "Model Controls" },
  { keys: "g s", description: "Settings" },
  { keys: "g h", description: "Help" },
  { keys: "Esc", description: "Close drawer / palette" },
];

export function HelpPage() {
  const info = useQuery({ queryKey: ["app-info"], queryFn: api.appInfo });

  return (
    <div className="grid gap-4 max-w-3xl">
      <header>
        <h1 className="text-lg font-bold">Help</h1>
        <p className="text-xs text-[color:var(--fg-dim)] mt-1">
          Catchem is a local-first desktop wrapper around the catchem pipeline.
          Everything runs on this machine — no cloud services are contacted.
        </p>
      </header>

      <section className="card">
        <h2 className="label mb-2">version</h2>
        <ul className="grid sm:grid-cols-2 gap-y-1 text-xs">
          <li><span className="text-[color:var(--fg-dim)]">app</span> <span className="font-mono">{info.data?.name ?? "catchem"}</span></li>
          <li><span className="text-[color:var(--fg-dim)]">version</span> <span className="font-mono">{info.data?.version ?? "—"}</span></li>
          <li><span className="text-[color:var(--fg-dim)]">branch</span> <span className="font-mono">{info.data?.branch ?? "—"}</span></li>
          <li><span className="text-[color:var(--fg-dim)]">commit</span> <span className="font-mono">{info.data?.commit_sha ?? "—"}</span></li>
          <li><span className="text-[color:var(--fg-dim)]">mode</span> <span className="font-mono">{info.data?.mode ?? "—"}</span></li>
          <li><span className="text-[color:var(--fg-dim)]">ML stubs</span> <span className="font-mono">{String(info.data?.use_ml_stubs ?? "—")}</span></li>
        </ul>
      </section>

      <section className="card">
        <h2 className="label mb-2">keyboard shortcuts</h2>
        <ul className="grid gap-1.5">
          {SHORTCUTS.map((s) => (
            <li key={s.keys} className="grid grid-cols-[140px_1fr] gap-3 text-sm">
              <span className="kbd">{s.keys}</span>
              <span className="text-[color:var(--fg-dim)]">{s.description}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2 className="label mb-2">modes</h2>
        <dl className="grid gap-2 text-sm">
          <dt className="font-semibold">production_safe</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">
            Default. NewsImpact diagnostic adapter is hard-refused even with the env flag on.
            Every diagnostic field on every record is forced to <code className="font-mono">false</code>/<code className="font-mono">null</code>.
          </dd>
          <dt className="font-semibold">replay_existing</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">One pass over finalized Awareness JSONL. Idempotent via persisted offsets.</dd>
          <dt className="font-semibold">live_tail</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">Long-running tail of newly committed JSONL chunks.</dd>
          <dt className="font-semibold text-warn">research_diagnostic</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">
            Loads a labeled read-only diagnostic stamp from NewsImpact governance.
            <strong className="text-warn ml-1">Can never override is_finance_relevant.</strong>
            Catchem shows a yellow banner whenever this mode is active.
          </dd>
        </dl>
      </section>

      <section className="card">
        <h2 className="label mb-2">guard rules</h2>
        <ul className="grid gap-1 text-sm text-[color:var(--fg-dim)]">
          <li>✓ NewsImpact stays quarantined (<code className="font-mono">release_gate_passed: false</code>)</li>
          <li>✓ <code className="font-mono">final_best.pt</code> never modified</li>
          <li>✓ No training, promotion, or external publishing</li>
          <li>✓ Production-safe scrubs <code className="font-mono">diagnostic_multimodal_*</code> at the API surface</li>
          <li>✓ External URLs sanitized via <code className="font-mono">safeHref</code> (http / https only)</li>
          <li>✓ Upload size capped at 5 MB; allowed: <code className="font-mono">.txt / .md / .html / .jsonl / .json</code></li>
        </ul>
      </section>

      <section className="card">
        <h2 className="label mb-2">troubleshooting</h2>
        <details className="text-sm">
          <summary className="cursor-pointer font-semibold">Catchem can't reach the sidecar</summary>
          <div className="mt-2 text-[color:var(--fg-dim)] pl-3 grid gap-1">
            <p>In a terminal:</p>
            <pre className="bg-[color:var(--bg-elev2)] rounded p-2 text-[10px] font-mono overflow-x-auto">
{`pkill -f 'catchem.cli serve' 2>/dev/null
bash scripts/catchem_bootstrap_and_run.sh --skip-frontend-build`}
            </pre>
          </div>
        </details>
        <details className="text-sm mt-2">
          <summary className="cursor-pointer font-semibold">Bundle missing → / shows placeholder</summary>
          <div className="mt-2 text-[color:var(--fg-dim)] pl-3 grid gap-1">
            <pre className="bg-[color:var(--bg-elev2)] rounded p-2 text-[10px] font-mono overflow-x-auto">
{`cd frontend && npm install && npm run build`}
            </pre>
          </div>
        </details>
        <details className="text-sm mt-2">
          <summary className="cursor-pointer font-semibold">Want real Hugging Face models</summary>
          <div className="mt-2 text-[color:var(--fg-dim)] pl-3 grid gap-1">
            <pre className="bg-[color:var(--bg-elev2)] rounded p-2 text-[10px] font-mono overflow-x-auto">
{`bash scripts/catchem_bootstrap_and_run.sh --with-ml`}
            </pre>
            <p>See <code className="font-mono">docs/ML_FALLBACK.md</code> for the stub→HF mapping and why <code className="font-mono">--with-ml</code> may degrade gracefully to stubs.</p>
          </div>
        </details>
      </section>

      <section className="card">
        <h2 className="label mb-2">links</h2>
        <ul className="text-sm grid gap-1">
          <li><a className="text-accent hover:underline" href="/legacy" target="_blank" rel="noopener noreferrer">/legacy — vanilla dashboard</a></li>
          <li><a className="text-accent hover:underline" href="/docs" target="_blank" rel="noopener noreferrer">/docs — OpenAPI</a></li>
          <li><a className="text-accent hover:underline" href="/ui/summary" target="_blank" rel="noopener noreferrer">/ui/summary — JSON overview</a></li>
          <li><a className="text-accent hover:underline" href="/ui/guards" target="_blank" rel="noopener noreferrer">/ui/guards — guard snapshot</a></li>
        </ul>
      </section>
    </div>
  );
}
