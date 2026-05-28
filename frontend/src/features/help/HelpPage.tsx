import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { requestOpenOnboarding } from "@/lib/onboarding";

/**
 * Help-surface. Round 8 redesign: this page used to duplicate Settings
 * (same keyboard table, same mode list, same version block). Now it's
 * the "how do I use Catchem" page — quick start, glossary, guard rules,
 * troubleshooting. Settings keeps the keyboard table + theme toggle.
 *
 * The SHORTCUTS export is re-exported from SettingsPage so the Round-7
 * "docs-against-canonical-registry" tests in src/tests/navShortcuts.test.ts
 * still pass — keyboard docs only live in Settings now, but the contract
 * stays addressable from HelpPage's module surface.
 */
export { SHORTCUTS } from "@/features/settings/SettingsPage";

const GLOSSARY: { term: string; body: string }[] = [
  {
    term: "asset class",
    body:
      "High-level category attached to a record (indices, equities, crypto, rates, fx, " +
      "commodities, credit, macro). One record can carry several.",
  },
  {
    term: "reason code",
    body:
      "Why the record matters — earnings, inflation, central_bank, regulation, " +
      "fraud_governance, m_and_a, cyber_outage, geopolitics, etc.",
  },
  {
    term: "finance relevance score",
    body:
      "Calibrated 0–1 score from the relevance scorer. Empirical max is ~0.80; the Overview + Feed " +
      "color-bands ≥0.70 (top decile) and ≥0.40 (solid middle).",
  },
  {
    term: "production_safe",
    body:
      "Default runtime. The diagnostic adapter is hard-refused; every diagnostic_* field on every " +
      "record is forced to false/null at the API surface.",
  },
  {
    term: "DLQ",
    body:
      "Dead-letter queue. Records that failed processing get parked here for re-tries — a non-zero " +
      "DLQ during steady-state usually means a malformed JSONL chunk upstream.",
  },
  {
    term: "fusion_verdict_class",
    body:
      "External governance contract owned by merged_news (newsimpact). Catchem mirrors the upstream " +
      "class name verbatim — the FUSION_* prefix is intentional, not a Catchem-side typo.",
  },
];

export function HelpPage() {
  const info = useQuery({ queryKey: ["app-info"], queryFn: api.appInfo });

  return (
    <div className="grid gap-5 max-w-4xl">
      <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
        <div aria-hidden className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl" />
        <div className="relative flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
            </span>
            <div>
              <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                Help · how to use Catchem
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                Local-first news-to-finance analyst workstation
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                v{info.data?.version ?? "—"} · everything runs on this machine · {GLOSSARY.length} glossary terms · no cloud calls
              </div>
            </div>
          </div>
          {/* Re-open the first-run tour. Uses the reload-free event path
              (lib/onboarding) so the welcome overlay pops instantly over
              this page — no navigation, no lost state. The command palette's
              "Restart onboarding" is the heavier reset-and-reload variant. */}
          <button
            type="button"
            onClick={requestOpenOnboarding}
            data-testid="help-replay-tour"
            title="Replay the first-run welcome tour"
            className="btn shrink-0 text-xs"
          >
            ↺ Replay welcome tour
          </button>
        </div>
        <div className="relative grid gap-2 grid-cols-2 md:grid-cols-4 text-[11px]">
          <HelpStat to="/feed" label="step 1" value="Open Live Feed →" hint="watch ingest in real time" />
          <HelpStat to="/scan" label="step 2" value="Run a Quant Scan →" hint="deep cross-asset signals" />
          <HelpStat to="/replay" label="step 3" value="Replay a sample →" hint="test specific article" />
          <HelpStat to="/settings" label="config" value="DeepSeek setup →" hint="optional second opinion" />
        </div>
      </section>

      <section className="card">
        <h2 className="label mb-2">quick start</h2>
        <ol className="grid gap-3 list-decimal pl-5 text-sm text-[color:var(--fg-dim)]">
          <li>
            Open <Link className="text-accent hover:underline" to="/feed">Live Feed</Link> — the
            news poller is already running. The status strip at the top shows whether ingestion is healthy.
          </li>
          <li>
            Want to test a specific article? Go to{" "}
            <Link className="text-accent hover:underline" to="/replay">Replay/Upload</Link> →{" "}
            <strong>Paste article</strong>, paste the body, hit <strong>Analyze</strong>.
          </li>
          <li>
            Need to re-run the supervisor over recent JSONL captures? Same page →{" "}
            <strong>Replay JSONL</strong>. It's idempotent; already-processed IDs are skipped.
          </li>
          <li>
            Check the runtime state on{" "}
            <Link className="text-accent hover:underline" to="/ops">Ops</Link> if a count looks off.
          </li>
        </ol>
      </section>

      <section className="card">
        <h2 className="label mb-2">glossary</h2>
        <dl className="grid gap-3 text-sm">
          {GLOSSARY.map((g) => (
            <div key={g.term}>
              <dt className="font-semibold">{g.term}</dt>
              <dd className="text-[color:var(--fg-dim)] text-xs leading-relaxed mt-0.5">{g.body}</dd>
            </div>
          ))}
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
        <details className="text-sm mt-2">
          <summary className="cursor-pointer font-semibold">Symbols page shows "no live quotes"</summary>
          <div className="mt-2 text-[color:var(--fg-dim)] pl-3 grid gap-1">
            <p>
              Expected on a fresh install — the default <code className="font-mono">local_fixture</code> quote
              provider only has data for a small seed set of symbols. Symbol mentions still work
              (they're extracted from the article text and don't need a quote feed).
            </p>
          </div>
        </details>
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">developer references</h2>
          <span className="text-[10px] text-[color:var(--fg-muted)]">opens in new tab</span>
        </div>
        <div className="grid gap-2 sm:grid-cols-3 text-xs">
          <a
            href="/api/docs"
            target="_blank"
            rel="noreferrer noopener"
            className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/70 transition-colors block"
          >
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">swagger</div>
            <div className="mt-0.5 text-sm font-semibold text-accent">API reference →</div>
            <div className="text-[10px] text-[color:var(--fg-dim)]">interactive /api/docs</div>
          </a>
          <a
            href="/api/redoc"
            target="_blank"
            rel="noreferrer noopener"
            className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/70 transition-colors block"
          >
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">redoc</div>
            <div className="mt-0.5 text-sm font-semibold text-accent">Readable docs →</div>
            <div className="text-[10px] text-[color:var(--fg-dim)]">narrative /api/redoc</div>
          </a>
          <a
            href="/api/_index"
            target="_blank"
            rel="noreferrer noopener"
            className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/70 transition-colors block"
          >
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">json</div>
            <div className="mt-0.5 text-sm font-semibold text-accent">Route index →</div>
            <div className="text-[10px] text-[color:var(--fg-dim)]">programmatic /api/_index</div>
          </a>
        </div>
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">build info</h2>
          <Link to="/settings" className="text-[10px] text-accent hover:underline">
            keyboard shortcuts + theme →
          </Link>
        </div>
        <dl className="grid sm:grid-cols-2 gap-y-1 text-xs">
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">app</dt>
            <dd className="font-mono">{info.data?.name ?? "catchem"}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">version</dt>
            <dd className="font-mono">{info.data?.version ?? "—"}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">branch</dt>
            <dd className="font-mono">{info.data?.branch ?? "—"}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">commit</dt>
            <dd className="font-mono truncate" title={info.data?.commit_sha ?? ""}>
              {info.data?.commit_sha?.slice(0, 10) ?? "—"}
            </dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">mode</dt>
            <dd className="font-mono">{info.data?.mode ?? "—"}</dd>
          </div>
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[color:var(--fg-dim)]">ML stubs</dt>
            <dd className="font-mono">{String(info.data?.use_ml_stubs ?? "—")}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

function HelpStat({ to, label, value, hint }: { to: string; label: string; value: string; hint?: string }) {
  return (
    <Link to={to} className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/70 transition-colors block">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className="mt-0.5 text-sm font-semibold text-accent">{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </Link>
  );
}
