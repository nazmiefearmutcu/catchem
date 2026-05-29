import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTheme } from "@/hooks/useTheme";
import { useAccent, ACCENT_PRESETS, type AccentId } from "@/hooks/useAccent";
import { useOverlaySurface } from "@/context/overlayCoordinator";
import { api, fmtBytes, fmtRel } from "@/lib/api";
import { NAV_SHORTCUTS, chordLabel } from "@/lib/nav-shortcuts";
import { t, setLang, useLang, type Lang } from "@/lib/i18n";
import {
  exportSnapshot,
  downloadSnapshot,
  readSnapshotFile,
  importSnapshot,
  diffSnapshots,
  SNAPSHOT_ALLOW_LIST,
  type Snapshot,
  type SnapshotDiff,
} from "@/lib/snapshot";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { Sparkline } from "@/components/Sparkline";
import type {
  DbInfoResponse,
  DbImportResponse,
  DbSchemaVersionResponse,
  ReviewsStatus,
  WebhookStatus,
  WebhookConfigPatch,
  WebhookTestResult,
} from "@/lib/api";

// Asset classes + reason codes mirror configs/taxonomy.yaml. Hard-coded
// here so the SettingsPage doesn't need a runtime taxonomy endpoint just
// to render the filter chips. If taxonomy.yaml grows new ids, add them
// here too — the backend will accept any string list, so the worst case
// is "user can't pick the new label without retyping".
const ASSET_CLASSES = [
  "equities", "indices", "fx", "rates", "credit",
  "commodities", "crypto", "macro",
] as const;
const REASON_CODES = [
  "earnings", "guidance", "m_and_a", "regulation", "litigation",
  "central_bank", "inflation", "employment", "growth_recession",
  "funding_liquidity", "supply_chain", "geopolitics", "sanctions_trade",
  "energy", "metals", "cyber_outage", "product_launch",
  "fraud_governance", "esg_reputation", "natural_disaster",
] as const;

/**
 * Settings-surface shortcut docs. Built from the canonical NAV_SHORTCUTS
 * registry. Round 7 canonicalized Analysis on `g a`; the previous
 * `g m → Market Map` entry conflicted with the palette + HelpPage which
 * both used `g a`. `m` still works in the handler as an alias for
 * pre-Round-7 muscle memory, but the docs no longer advertise it.
 */
export const SHORTCUTS: { keys: string; description: string }[] = [
  { keys: "⌘K  /  Ctrl+K", description: "Open the command palette" },
  ...NAV_SHORTCUTS.map((s) => ({ keys: chordLabel(s), description: `Go to ${s.label}` })),
  { keys: "Esc", description: "Close drawer / palette" },
];

export function SettingsPage() {
  const { theme, toggle } = useTheme();
  const location = useLocation();
  const info = useQuery({ queryKey: ["app-info"], queryFn: api.appInfo });
  const reviewers = useQuery<ReviewsStatus>({
    queryKey: ["reviewers-status"],
    queryFn: api.reviewsStatus,
  });

  // Scroll-to-anchor when the route lands with a hash (e.g. the command
  // palette's "Open Settings → DeepSeek" action navigates to
  // `/settings#deepseek`). We defer one tick so the reviewer card has
  // mounted before we try to focus it.
  useEffect(() => {
    const hash = location.hash;
    if (!hash) return;
    const id = hash.startsWith("#") ? hash.slice(1) : hash;
    if (!id) return;
    const t = window.setTimeout(() => {
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 50);
    return () => window.clearTimeout(t);
  }, [location.hash]);

  const dsReady = reviewers.data?.deepseek_ready ?? false;
  const dsKeyed = reviewers.data?.deepseek_keyed ?? false;
  const dsEnabled = reviewers.data?.deepseek_enabled ?? false;
  const dsExhausted = reviewers.data?.exhausted ?? false;
  const dsConfigured = dsReady || dsKeyed;
  const dsSpent = reviewers.data?.usd_spent;
  const dsCap = reviewers.data?.usd_cap;

  const dsValueLabel = dsExhausted
    ? "budget hit"
    : dsReady
      ? "ready"
      : dsKeyed
        ? "keyed"
        : dsEnabled
          ? "needs key"
          : "off";
  const dsTone: "good" | "warn" | "bad" = dsExhausted
    ? "bad"
    : dsConfigured
      ? "good"
      : "warn";
  const dsHint =
    typeof dsSpent === "number" && typeof dsCap === "number"
      ? `$${dsSpent.toFixed(4)} / $${dsCap.toFixed(2)}`
      : "no spend recorded";

  const versionValue = info.data?.version ? `v${info.data.version}` : "—";
  const versionHint = info.data?.commit_sha ? info.data.commit_sha.slice(0, 10) : info.data?.mode ?? undefined;

  const headline = dsConfigured
    ? `${theme === "dark" ? "Dark" : "Light"} cockpit · DeepSeek online`
    : `${theme === "dark" ? "Dark" : "Light"} cockpit · local-only reviewer`;

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_280px]">
      <section className="grid gap-4">
        <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
          <div
            aria-hidden
            className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
          />
          <div className="relative flex items-start justify-between gap-3 mb-3">
            <div className="flex items-center gap-3">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
              </span>
              <div>
                <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                  Settings · local preferences
                </div>
                <h1 className="text-lg font-semibold mt-0.5 tracking-tight">{headline}</h1>
                <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                  current theme · {theme} · DeepSeek {dsConfigured ? "configured" : "not configured"} ·{" "}
                  {SHORTCUTS.length} shortcuts
                </div>
              </div>
            </div>
            <button className="btn shrink-0" onClick={toggle}>
              switch to {theme === "dark" ? "light" : "dark"}
            </button>
          </div>
          <div className="relative grid gap-2 grid-cols-2 md:grid-cols-4 text-[11px]">
            <SettingsStat
              label="theme"
              value={theme}
              hint='localStorage["catchem.theme"]'
              tone="good"
            />
            <SettingsStat
              label="DeepSeek"
              value={dsValueLabel}
              hint={dsHint}
              tone={dsTone}
            />
            <SettingsStat
              label="shortcuts"
              value={`${SHORTCUTS.length} chord${SHORTCUTS.length === 1 ? "" : "s"}`}
              hint="⌘K opens palette"
            />
            <SettingsStat
              label="app version"
              value={versionValue}
              hint={versionHint}
            />
          </div>
        </section>

        <AccentPickerCard />

        <LanguagePickerCard />

        <section className="card">
          <h2 className="label mb-2">{t("settings.shortcuts")}</h2>
          <ul className="grid gap-1.5">
            {SHORTCUTS.map((s) => (
              <li key={s.keys} className="grid grid-cols-[140px_1fr] gap-3 text-sm">
                <span className="kbd">{s.keys}</span>
                <span className="text-[color:var(--fg-dim)]">{s.description}</span>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-[10px] text-[color:var(--fg-muted)]">
            Chord shortcuts: press the first key, release, then the second within ~1.5s.
          </p>
        </section>

        <section className="card">
          <h2 className="label mb-2">modes</h2>
          <dl className="grid gap-3 text-sm">
            <div>
              <dt className="font-semibold">production_safe</dt>
              <dd className="text-[color:var(--fg-dim)] pl-3 text-xs">
                Default. Diagnostic adapter is hard-refused even with the env flag on.
              </dd>
            </div>
            <div>
              <dt className="font-semibold">replay_existing</dt>
              <dd className="text-[color:var(--fg-dim)] pl-3 text-xs">
                One pass over finalized Awareness JSONL. Idempotent via persisted offsets.
              </dd>
            </div>
            <div>
              <dt className="font-semibold">live_tail</dt>
              <dd className="text-[color:var(--fg-dim)] pl-3 text-xs">
                Long-running tail of newly committed JSONL chunks.
              </dd>
            </div>
            <div>
              <dt className="font-semibold text-warn">research_diagnostic</dt>
              <dd className="text-[color:var(--fg-dim)] pl-3 text-xs">
                Loads a labeled read-only diagnostic stamp from NewsImpact governance.
                Can NEVER override <code className="font-mono">is_finance_relevant</code>.
              </dd>
            </div>
          </dl>
        </section>

        <DatabaseBackupCard />

        <WorkspaceSnapshotCard />

        <DeepSeekReviewerCard />

        <WebhookOutputCard />

        <ResetPreferencesCard />

        <section className="card">
          <h2 className="label mb-2">api / debug links</h2>
          <ul className="text-sm grid gap-1">
            <li><a className="text-accent hover:underline" href="/legacy" target="_blank" rel="noopener noreferrer">/legacy — vanilla dashboard</a></li>
            <li><a className="text-accent hover:underline" href="/docs" target="_blank" rel="noopener noreferrer">/docs — OpenAPI</a></li>
            <li><a className="text-accent hover:underline" href="/ui/summary" target="_blank" rel="noopener noreferrer">/ui/summary — JSON overview</a></li>
            <li><a className="text-accent hover:underline" href="/ui/guards" target="_blank" rel="noopener noreferrer">/ui/guards — guard snapshot</a></li>
            <li><a className="text-accent hover:underline" href="/ui/benchmark/latest" target="_blank" rel="noopener noreferrer">/ui/benchmark/latest — last benchmark</a></li>
          </ul>
        </section>
      </section>

      {/* Sidebar — uses the right column that was empty before. */}
      <aside className="grid gap-3 h-fit lg:sticky lg:top-3">
        <section className="card">
          <h2 className="label mb-2">build</h2>
          {info.isLoading || !info.data ? (
            <Skeleton className="h-16" />
          ) : (
            <dl className="grid grid-cols-[80px_1fr] gap-y-1 text-[11px]">
              <dt className="text-[color:var(--fg-muted)]">version</dt>
              <dd className="font-mono">{info.data.version ?? "—"}</dd>
              <dt className="text-[color:var(--fg-muted)]">branch</dt>
              <dd className="font-mono truncate">{info.data.branch ?? "—"}</dd>
              <dt className="text-[color:var(--fg-muted)]">commit</dt>
              <dd className="font-mono truncate" title={info.data.commit_sha ?? ""}>
                {info.data.commit_sha?.slice(0, 10) ?? "—"}
              </dd>
              <dt className="text-[color:var(--fg-muted)]">mode</dt>
              <dd className="font-mono">{info.data.mode ?? "—"}</dd>
              <dt className="text-[color:var(--fg-muted)]">ML stubs</dt>
              <dd className="font-mono">{String(info.data.use_ml_stubs ?? "—")}</dd>
              <dt className="text-[color:var(--fg-muted)]">bundle</dt>
              <dd className="font-mono">{info.data.static_bundle_present ? "present" : "missing"}</dd>
            </dl>
          )}
        </section>

        <section className="card text-xs">
          <h2 className="label mb-2">tips</h2>
          <ul className="grid gap-2 text-[color:var(--fg-dim)]">
            <li><kbd className="kbd">⌘K</kbd> opens the command palette from anywhere.</li>
            <li>Use Replay/Upload to ingest articles without restarting the sidecar.</li>
            <li>Theme respects <em>prefers-reduced-motion</em> — flashes are toned down.</li>
          </ul>
        </section>
      </aside>
    </div>
  );
}

// ── DeepSeek reviewer settings card ─────────────────────────────────────

/**
 * Compute the progress-bar tone for cumulative DeepSeek spend.
 *
 *   < 50% spent   → "good" (green)   — comfortable headroom
 *   50%-80%       → "warn" (yellow)  — getting close, no surprises
 *   > 80%         → "bad"  (red)     — likely to hit cap within a session
 *
 * Cap of 0 (or non-finite) flattens to "good" so the visual doesn't
 * scream when the user is mid-edit. Exhausted budget hard-overrides
 * to "bad" upstream.
 */
function budgetTone(spent: number, cap: number): "good" | "warn" | "bad" {
  if (!Number.isFinite(cap) || cap <= 0) return "good";
  const pct = spent / cap;
  if (pct > 0.8) return "bad";
  if (pct >= 0.5) return "warn";
  return "good";
}

function DeepSeekReviewerCard() {
  const qc = useQueryClient();
  const status = useQuery<ReviewsStatus>({
    queryKey: ["reviews-status"],
    queryFn: api.reviewsStatus,
    refetchInterval: 6_000,
  });
  // 7-day rolling spend history. Slower refetch (60s) than the status pill
  // because daily aggregates don't shift fast and the chart redraw would
  // otherwise flicker the SVG path.
  const spendHistory = useQuery({
    queryKey: ["reviews-spend-history", 7],
    queryFn: () => api.reviewsSpendHistory(7),
    refetchInterval: 60_000,
  });
  // Locally-edited form values for inputs that need explicit "Save"
  // semantics (sampling rate, USD cap, model, optional API key).
  // The enable/disable toggle is auto-flushed — see toggleEnable().
  const [samplingRate, setSamplingRate] = useState<number>(0.1);
  const [usdCap, setUsdCap] = useState<number>(9.5);
  const [apiKey, setApiKey] = useState<string>("");
  const [model, setModel] = useState<string>("deepseek-chat");
  const [keyTouched, setKeyTouched] = useState<boolean>(false);
  // When the user clicks "Replace key" we flip the API-key field from
  // "configured ✓" to an editable input. Click again to cancel.
  const [editingKey, setEditingKey] = useState<boolean>(false);

  useEffect(() => {
    const d = status.data;
    if (!d) return;
    setSamplingRate(d.sampling_rate);
    setUsdCap(d.usd_cap);
    setModel(d.model);
  }, [status.data?.sampling_rate, status.data?.usd_cap, status.data?.model]);

  const save = useMutation({
    mutationFn: (patch: { enabled?: boolean; sampling_rate?: number; usd_cap?: number; model?: string; api_key?: string }) =>
      api.reviewsPatchSettings(patch),
    onSuccess: () => {
      // Reset key-replacement UX after a successful save so the field
      // returns to "configured ✓" state.
      setApiKey("");
      setKeyTouched(false);
      setEditingKey(false);
      qc.invalidateQueries({ queryKey: ["reviews-status"] });
      // ReviewsStatus is also queried elsewhere as ["reviewers-status"].
      qc.invalidateQueries({ queryKey: ["reviewers-status"] });
    },
  });

  if (status.isLoading) return <Skeleton className="h-40" />;
  if (status.error) return <ErrorBox err={status.error} />;
  const d = status.data;
  if (!d) return null;

  // Auto-save the enable toggle the moment the user flips it — no
  // separate "save" click. Falls back gracefully if the network is
  // down; mutation isError shows inline below.
  const toggleEnable = () => {
    save.mutate({ enabled: !d.deepseek_enabled });
  };

  const saveFormValues = () => {
    save.mutate({
      sampling_rate: samplingRate,
      usd_cap: usdCap,
      model,
      // Only ship the API key field when the user actually typed
      // something — otherwise we'd overwrite a stored key with "".
      ...(keyTouched ? { api_key: apiKey } : {}),
    });
  };

  const dirty =
    samplingRate !== d.sampling_rate ||
    usdCap !== d.usd_cap ||
    model !== d.model ||
    keyTouched;

  // Status pill text + tone, separated from the progress-bar tone so the
  // pill can show "budget hit" / "ready" / "needs key" / "disabled"
  // independently of the bar fill colour.
  const pillTone: "good" | "warn" | "bad" = d.exhausted
    ? "bad"
    : d.deepseek_ready
      ? "good"
      : d.deepseek_enabled
        ? "warn"
        : "warn";
  const pillText = d.exhausted
    ? "budget hit"
    : d.deepseek_ready
      ? "ready"
      : d.deepseek_enabled
        ? "needs key"
        : "disabled";

  // Budget progress visuals. Colour values come from tailwind.config.js
  // (`good`/`warn`/`bad` are hex literals there, not CSS vars).
  const spentPct = d.usd_cap > 0 ? Math.min(100, (d.usd_spent / d.usd_cap) * 100) : 0;
  const tone = d.exhausted ? "bad" : budgetTone(d.usd_spent, d.usd_cap);
  const fillColor =
    tone === "bad" ? "#f87171" : tone === "warn" ? "#fbbf24" : "#4ade80";
  const fillToneClass =
    tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-good";

  const lowBudget = Number.isFinite(usdCap) && usdCap > 0 && usdCap < 1;

  return (
    <section
      id="deepseek"
      className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-5 grid gap-4 scroll-mt-24"
      data-testid="deepseek-reviewer-card"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute -top-16 -right-16 h-40 w-40 rounded-full bg-accent/15 blur-3xl"
      />

      {/* ── Header: title block + status pill + auto-save toggle ── */}
      <header className="relative flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span
            className={`relative flex h-2 w-2 ${d.deepseek_ready ? "" : "opacity-60"}`}
            aria-hidden
          >
            {d.deepseek_ready && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-good opacity-75" />
            )}
            <span
              className={`relative inline-flex h-2 w-2 rounded-full ${
                pillTone === "good" ? "bg-good" : pillTone === "warn" ? "bg-warn" : "bg-bad"
              }`}
            />
          </span>
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Settings · AI Reviewer Configuration
            </div>
            <h2 className="text-base font-semibold mt-0.5 tracking-tight">
              DeepSeek second-opinion reviewer
            </h2>
            <div className={`mt-1 text-[11px] tabular-nums ${
              pillTone === "good" ? "text-good" : pillTone === "warn" ? "text-warn" : "text-bad"
            }`}>
              {pillText}
              {" · "}
              <span className="text-[color:var(--fg-muted)]">model</span>{" "}
              <code className="font-mono text-[color:var(--fg-dim)]">{d.model}</code>
            </div>
          </div>
        </div>

        {/* Auto-flush enable switch */}
        <ToggleSwitch
          checked={d.deepseek_enabled}
          onToggle={toggleEnable}
          busy={save.isPending}
          label={d.deepseek_enabled ? "enabled" : "disabled"}
          data-testid="deepseek-enable-toggle"
        />
      </header>

      <p className="relative text-[11px] text-[color:var(--fg-muted)] max-w-prose">
        When enabled, Catchem also sends a deterministic sample of ingested articles to
        DeepSeek for a second review. Disabling this restores the local-first
        "no external services" guarantee. The API key is held in sidecar memory only —
        it's not written to disk unless you put it in your <code className="font-mono">.env</code>.
      </p>

      {/* ── Section: Budget ── */}
      <section className="relative grid gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="label">monthly budget</h3>
          <div className={`text-[11px] tabular-nums ${fillToneClass}`}>
            ${d.usd_spent.toFixed(4)} of ${d.usd_cap.toFixed(2)} USD spent
            <span className="ml-1 text-[color:var(--fg-muted)]">
              ({spentPct.toFixed(0)}% of monthly budget)
            </span>
          </div>
        </div>
        <div
          className="relative h-2 overflow-hidden rounded-full bg-[color:var(--bg-elev2)]"
          role="progressbar"
          aria-valuenow={Math.round(spentPct)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="DeepSeek budget consumed"
          data-testid="deepseek-budget-bar"
          data-tone={tone}
        >
          <div
            className="h-full transition-[width] duration-500 ease-out"
            style={{ width: `${spentPct}%`, background: fillColor }}
          />
        </div>
        <div className="flex justify-between text-[9px] text-[color:var(--fg-muted)] tabular-nums">
          <span>0%</span>
          <span>50%</span>
          <span>80%</span>
          <span>100%</span>
        </div>

        {/* 7-day spend sparkline + caption — chronological order so the
            line reads left→right = oldest→newest. Empty state has its own
            copy so the user knows "no calls" vs "still loading". */}
        <SpendHistoryRow
          data={spendHistory.data}
          isLoading={spendHistory.isLoading}
        />
      </section>

      {/* ── Section: Sampling rate slider ── */}
      <section className="relative grid gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <label htmlFor="ds-rate" className="label">
            sampling rate
          </label>
          <span className="text-[11px] tabular-nums text-accent font-semibold">
            {(samplingRate * 100).toFixed(0)}%
          </span>
        </div>
        <input
          id="ds-rate"
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={samplingRate}
          onChange={(e) => setSamplingRate(parseFloat(e.target.value))}
          className="w-full accent-[color:var(--accent)]"
          data-testid="ds-sampling-slider"
        />
        <div className="flex justify-between text-[9px] text-[color:var(--fg-muted)] tabular-nums">
          <button type="button" className="hover:text-accent" onClick={() => setSamplingRate(0)}>0%</button>
          <button type="button" className="hover:text-accent" onClick={() => setSamplingRate(0.25)}>25%</button>
          <button type="button" className="hover:text-accent" onClick={() => setSamplingRate(0.5)}>50%</button>
          <button type="button" className="hover:text-accent" onClick={() => setSamplingRate(0.75)}>75%</button>
          <button type="button" className="hover:text-accent" onClick={() => setSamplingRate(1)}>100%</button>
        </div>
        <div className="text-[10px] text-[color:var(--fg-muted)]">
          deterministic — same capture_id always lands in the same bucket
        </div>
      </section>

      {/* ── Section: API key + model ── */}
      <section className="relative grid gap-3 sm:grid-cols-2">
        <div className="grid gap-1">
          <label htmlFor="ds-key" className="label">API key</label>
          {d.deepseek_keyed && !editingKey ? (
            <div
              className="flex items-center justify-between gap-2 rounded-md border border-good/40 bg-[color:var(--bg-elev)] px-3 py-1.5 text-xs"
              data-testid="ds-key-configured"
            >
              <span className="flex items-center gap-1 text-good">
                <span aria-hidden>✓</span> API key configured
              </span>
              <button
                type="button"
                className="btn text-[10px] !py-0.5 !px-2"
                onClick={() => setEditingKey(true)}
                data-testid="ds-key-replace"
              >
                Replace key
              </button>
            </div>
          ) : (
            <>
              <input
                id="ds-key"
                type="password"
                className="input"
                placeholder="sk-..."
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setKeyTouched(true);
                }}
                autoComplete="off"
                spellCheck={false}
                data-testid="ds-key-input"
              />
              <div className="flex items-center justify-between text-[10px] text-[color:var(--fg-muted)]">
                <span>
                  {d.deepseek_keyed
                    ? "paste a new key to replace the stored one"
                    : "get a key from "}
                  {!d.deepseek_keyed && (
                    <a
                      href="https://platform.deepseek.com/api_keys"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-accent hover:underline"
                    >
                      deepseek.com
                    </a>
                  )}
                </span>
                {d.deepseek_keyed && (
                  <button
                    type="button"
                    className="text-accent hover:underline"
                    onClick={() => {
                      setEditingKey(false);
                      setApiKey("");
                      setKeyTouched(false);
                    }}
                  >
                    cancel
                  </button>
                )}
              </div>
            </>
          )}
        </div>

        <div className="grid gap-1">
          <label htmlFor="ds-model" className="label">model</label>
          <input
            id="ds-model"
            className="input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="deepseek-chat"
            autoComplete="off"
          />
          <div className="text-[10px] text-[color:var(--fg-muted)]">
            most users want <code className="font-mono">deepseek-chat</code>
          </div>
        </div>
      </section>

      {/* ── Section: USD cap input + low-budget warning ── */}
      <section className="relative grid gap-2">
        <label htmlFor="ds-cap" className="label">USD cap</label>
        <div className="flex items-center gap-2">
          <span className="text-[color:var(--fg-muted)] text-xs">$</span>
          <input
            id="ds-cap"
            type="number"
            min={0}
            step={0.5}
            className="input flex-1"
            value={usdCap}
            onChange={(e) => {
              const n = parseFloat(e.target.value);
              if (Number.isFinite(n)) setUsdCap(Math.max(0, n));
            }}
            data-testid="ds-cap-input"
          />
        </div>
        <div className="text-[10px] text-[color:var(--fg-muted)]">
          hard-stop when cumulative spend reaches this number
        </div>
        {lowBudget && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-md border border-warn/40 bg-warn/5 px-2.5 py-1.5 text-[10px] text-warn"
            data-testid="ds-low-budget-warning"
          >
            <span aria-hidden>⚠</span>
            <span>
              Budget too low — DeepSeek calls may stop within minutes of use. Consider
              raising the cap above $1 to avoid mid-session interruption.
            </span>
          </div>
        )}
      </section>

      {/* ── Section: Action row (Save + Test connection) ── */}
      <div className="relative flex flex-wrap items-center gap-2 pt-3 border-t border-[color:var(--border-subtle)]">
        <button
          type="button"
          className="btn btn-accent text-xs"
          disabled={!dirty || save.isPending}
          onClick={saveFormValues}
          data-testid="ds-save-btn"
        >
          {save.isPending ? "saving…" : "save changes"}
        </button>
        {/* Test connection — disabled until backend ping endpoint exists.
           Tracked in: api.ts has no reviewsPing()/deepseekPing() method. */}
        <button
          type="button"
          className="btn text-xs"
          disabled
          title="API surface for ping not yet implemented"
          data-testid="ds-test-conn-btn"
        >
          Test connection
        </button>
        <span className="text-[9px] text-[color:var(--fg-muted)] italic">
          (ping API not yet implemented)
        </span>
        {dirty && !save.isPending && (
          <span className="text-[10px] text-[color:var(--fg-muted)] ml-auto">unsaved changes</span>
        )}
        {save.isError && (
          <span className="text-[10px] text-bad ml-auto" role="alert">
            {save.error instanceof Error ? save.error.message : "save failed"}
          </span>
        )}
        {save.isSuccess && !dirty && (
          <span className="text-[10px] text-good ml-auto" data-testid="ds-save-success">saved ✓</span>
        )}
      </div>

      <p className="relative text-[10px] text-[color:var(--fg-muted)]">
        Per-call cost on <code className="font-mono">deepseek-chat</code> averages
        ~$0.0005 — $10 covers roughly 20,000 reviews at the default 600-token output cap.
      </p>
    </section>
  );
}

/**
 * 7-day DeepSeek spend sparkline + caption row. Inlined as a sibling of the
 * budget progress bar so the reader sees the cumulative bar AND the
 * day-by-day shape together. Uses the shared <Sparkline> primitive so the
 * SVG geometry stays identical to the BenchStat tiles on /bench.
 */
function SpendHistoryRow({
  data,
  isLoading,
}: {
  data:
    | {
        days: number;
        history: Array<{ day: string; call_count: number; total_cost_usd: number }>;
        totals: { calls: number; cost_usd: number };
      }
    | undefined;
  isLoading: boolean;
}) {
  // Render a hairline placeholder during the first fetch so the card doesn't
  // jump in height once data lands. Same height as the eventual content row.
  if (isLoading && !data) {
    return (
      <div
        className="h-5 mt-1"
        aria-hidden
        data-testid="ds-spend-history-loading"
      />
    );
  }
  if (!data || data.history.length === 0) {
    return (
      <div
        className="mt-1 text-[10px] text-[color:var(--fg-muted)] italic"
        data-testid="ds-spend-history-empty"
      >
        No DeepSeek calls in the last 7 days
      </div>
    );
  }
  // Backend returns newest→oldest; reverse so the sparkline reads
  // left→right = oldest→newest (matches BenchmarkPage convention).
  const chronological = data.history.slice().reverse();
  const points = chronological.map((h) => h.total_cost_usd);
  return (
    <div
      className="mt-1 flex items-center justify-between gap-2"
      data-testid="ds-spend-history-row"
    >
      <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
        7-day total: ${data.totals.cost_usd.toFixed(3)} over {data.totals.calls}{" "}
        call{data.totals.calls === 1 ? "" : "s"}
      </span>
      <Sparkline
        points={points}
        className="text-accent"
        ariaLabel={`DeepSeek spend over the last ${data.days} days`}
      />
    </div>
  );
}

// ── Accent picker card ──────────────────────────────────────────────────
//
// Six hand-picked presets + one "custom" slot. Picking a preset writes
// `catchem.accent` to localStorage; the override style block in
// useAccent injects new `--accent` values for both themes. The
// utility class `bg-accent` / `border-accent` / `text-accent` then
// pick up the change via tailwind.config.js → `var(--accent)`.
// "Custom" reveals two `<input type="color">` so the user can pick
// separate light + dark hexes (light-theme contrast vs dark-theme glow
// are different colour-design problems — one hex never satisfies both).
function AccentPickerCard() {
  const { id, setId, customLight, setCustomLight, customDark, setCustomDark } = useAccent();
  return (
    <section className="card" id="accent-picker" data-testid="accent-picker-card">
      <header className="flex items-baseline justify-between gap-2 mb-2">
        <h2 className="label">accent color</h2>
        <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          {id === "custom" ? "custom" : id} ·{" "}
          <code className="font-mono">--accent</code>
        </span>
      </header>
      <p className="text-[11px] text-[color:var(--fg-muted)] max-w-prose mb-3">
        Switches the cockpit's accent colour everywhere — hero borders,
        focus rings, the "live" dot, and the brand monogram. Selection is
        local-only (<code className="font-mono">localStorage["catchem.accent"]</code>);
        no sidecar round-trip.
      </p>

      <div
        className="flex flex-wrap items-center gap-2"
        role="radiogroup"
        aria-label="Accent color preset"
        data-testid="accent-swatches"
      >
        {ACCENT_PRESETS.map((preset) => (
          <AccentSwatch
            key={preset.id}
            presetId={preset.id}
            light={preset.light}
            dark={preset.dark}
            active={id === preset.id}
            onPick={() => setId(preset.id as AccentId)}
          />
        ))}
        <AccentSwatch
          presetId="custom"
          // Rainbow gradient so "custom" reads as multi-colour without
          // committing to any specific tone. CSS conic-gradient runs
          // around the full hue wheel — feels live.
          gradient
          active={id === "custom"}
          onPick={() => setId("custom" as AccentId)}
        />
      </div>

      {id === "custom" && (
        <div
          className="mt-3 grid gap-3 sm:grid-cols-2 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-3"
          data-testid="accent-custom-pickers"
        >
          <div className="grid gap-1">
            <label htmlFor="accent-custom-light" className="label">
              light theme
            </label>
            <div className="flex items-center gap-2">
              <input
                id="accent-custom-light"
                type="color"
                className="h-7 w-12 cursor-pointer rounded border border-[color:var(--border)] bg-transparent p-0"
                value={customLight}
                onChange={(e) => setCustomLight(e.target.value)}
                aria-label="Accent color for light theme"
                data-testid="accent-custom-light"
              />
              <code className="font-mono text-[11px] text-[color:var(--fg-dim)]">
                {customLight}
              </code>
            </div>
          </div>
          <div className="grid gap-1">
            <label htmlFor="accent-custom-dark" className="label">
              dark theme
            </label>
            <div className="flex items-center gap-2">
              <input
                id="accent-custom-dark"
                type="color"
                className="h-7 w-12 cursor-pointer rounded border border-[color:var(--border)] bg-transparent p-0"
                value={customDark}
                onChange={(e) => setCustomDark(e.target.value)}
                aria-label="Accent color for dark theme"
                data-testid="accent-custom-dark"
              />
              <code className="font-mono text-[11px] text-[color:var(--fg-dim)]">
                {customDark}
              </code>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function AccentSwatch({
  presetId,
  light,
  dark,
  active,
  onPick,
  gradient = false,
}: {
  presetId: AccentId;
  light?: string;
  dark?: string;
  active: boolean;
  onPick: () => void;
  gradient?: boolean;
}) {
  // Visual: dark-theme stop on top half, light-theme stop on bottom half.
  // Lets the user preview both colours per swatch without sample text.
  const style: React.CSSProperties = gradient
    ? {
        background:
          "conic-gradient(from 90deg, #ef4444, #f59e0b, #eab308, #22c55e, #14b8a6, #3b82f6, #8b5cf6, #ec4899, #ef4444)",
      }
    : {
        background: `linear-gradient(to bottom, ${dark} 50%, ${light} 50%)`,
      };

  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      aria-label={`Accent preset: ${presetId}`}
      onClick={onPick}
      title={presetId}
      data-testid={`accent-swatch-${presetId}`}
      data-active={active ? "true" : "false"}
      className={`relative h-7 w-7 rounded-full transition-transform hover:scale-110 ${
        active
          ? "ring-2 ring-offset-2 ring-offset-[color:var(--bg-elev)] ring-accent"
          : "ring-1 ring-[color:var(--border)]"
      }`}
      style={style}
    >
      {active && (
        <span
          className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-white drop-shadow"
          aria-hidden
        >
          ✓
        </span>
      )}
    </button>
  );
}

// ── Language picker card ────────────────────────────────────────────────
//
// Two pills: English / Türkçe. Active locale gets the accent ring + ✓.
// Click flips the i18n store; `useLang()` re-renders this card and any
// other subscriber (notably Shell.tsx's NAV) on the same tick. The
// persist + `<html lang>` mirror happens inside `setLang()` itself —
// the card just dispatches the change.
function LanguagePickerCard() {
  const current = useLang();
  const options: { id: Lang; labelKey: string }[] = [
    { id: "en", labelKey: "settings.language.en" },
    { id: "tr", labelKey: "settings.language.tr" },
  ];
  return (
    <section
      className="card"
      id="language-picker"
      data-testid="language-picker-card"
    >
      <header className="flex items-baseline justify-between gap-2 mb-2">
        <h2 className="label">{t("settings.language")}</h2>
        <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          {current} · <code className="font-mono">{`localStorage["catchem.lang"]`}</code>
        </span>
      </header>
      <p className="text-[11px] text-[color:var(--fg-muted)] max-w-prose mb-3">
        {t("settings.language.hint")}
      </p>
      <div
        className="flex flex-wrap items-center gap-2"
        role="radiogroup"
        aria-label={t("settings.language")}
        data-testid="language-options"
      >
        {options.map((opt) => {
          const active = current === opt.id;
          return (
            <button
              key={opt.id}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => setLang(opt.id)}
              data-testid={`language-option-${opt.id}`}
              data-active={active ? "true" : "false"}
              className={`relative inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border transition-colors ${
                active
                  ? "border-accent bg-accent/15 text-accent font-semibold"
                  : "border-[color:var(--border)] bg-[color:var(--bg-elev2)]/40 text-[color:var(--fg-dim)] hover:border-accent/50 hover:text-[color:var(--fg)]"
              }`}
            >
              {active && <span aria-hidden>✓</span>}
              <span>{t(opt.labelKey)}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

// ── Switch component (proper on/off toggle, accent fill, accessible) ────

function ToggleSwitch({
  checked,
  onToggle,
  busy = false,
  label,
  ...rest
}: {
  checked: boolean;
  onToggle: () => void;
  busy?: boolean;
  label?: string;
  "data-testid"?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-busy={busy || undefined}
        disabled={busy}
        onClick={onToggle}
        className={`
          relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full
          border transition-colors disabled:opacity-60 disabled:cursor-wait
          ${checked
            ? "bg-accent border-accent"
            : "bg-[color:var(--bg-elev2)] border-[color:var(--border)]"}
        `.replace(/\s+/g, " ").trim()}
        {...rest}
      >
        <span
          className={`
            inline-block h-4 w-4 transform rounded-full bg-white shadow
            transition-transform duration-200
            ${checked ? "translate-x-6" : "translate-x-1"}
          `.replace(/\s+/g, " ").trim()}
          aria-hidden
        />
      </button>
      {label && (
        <span
          className={`text-[10px] uppercase tracking-wider tabular-nums ${
            checked ? "text-accent font-semibold" : "text-[color:var(--fg-muted)]"
          }`}
        >
          {busy ? "saving…" : label}
        </span>
      )}
    </div>
  );
}

// ── Hero stat tile (mirrors BenchStat pattern on BenchmarkPage) ──────────

function SettingsStat({
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
    tone === "good"
      ? "text-good"
      : tone === "warn"
        ? "text-warn"
        : tone === "bad"
          ? "text-bad"
          : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </div>
  );
}

// ── Database backup / restore card ──────────────────────────────────────
//
// Exports the live SQLite truth-store as a single file via a direct
// download (the GET endpoint sets Content-Disposition: attachment so the
// browser drives the Save dialog). Import takes a previously-exported
// .sqlite3 file, validates the magic header, and replaces the DB on
// disk — backing up the old one first. The supervisor's in-memory
// connection is now stale after a restore, so we force a page reload to
// reboot the SPA against the freshly-swapped DB.
function DatabaseBackupCard() {
  const qc = useQueryClient();
  const info = useQuery<DbInfoResponse>({
    queryKey: ["db-info"],
    queryFn: api.dbInfo,
    refetchInterval: 15_000,
  });
  // Schema version line below DB stats. Cheap PRAGMA query — no need to
  // hammer it, the value only changes on app restart after a build that
  // ships new migrations. Stale-while-revalidate is fine.
  const schemaVersion = useQuery<DbSchemaVersionResponse>({
    queryKey: ["db-schema-version"],
    queryFn: api.dbSchemaVersion,
    refetchInterval: 60_000,
    staleTime: 60_000,
  });
  const fileRef = useRef<HTMLInputElement | null>(null);
  // Two-step UX: select → confirm. Holding the picked file in state lets
  // the user back out before the destructive replace lands.
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [importedResult, setImportedResult] = useState<DbImportResponse | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const importMutation = useMutation({
    mutationFn: (file: File) => api.dbImport(file),
    onSuccess: (data) => {
      setImportedResult(data);
      setPendingFile(null);
      qc.invalidateQueries({ queryKey: ["db-info"] });
      // Reload after a short delay so the user has time to see the
      // success state. The supervisor still holds the previous DB
      // connection — a hard reload reboots the SPA and the sidecar
      // session keeps the new file as its source of truth on its
      // next query.
      if (data.ok) {
        window.setTimeout(() => {
          window.location.reload();
        }, 2_000);
      }
    },
  });

  const handleFile = (file: File | null) => {
    if (!file) return;
    // Cheap client-side gate: extension must look like SQLite. The
    // backend still validates the magic header, so this is purely UX.
    const lower = file.name.toLowerCase();
    const ok = lower.endsWith(".sqlite3") || lower.endsWith(".sqlite") || lower.endsWith(".db");
    if (!ok) {
      // Stash the file anyway and let the backend produce the real
      // error — the UI just nudges the user with the extension hint.
    }
    setPendingFile(file);
    setImportedResult(null);
    importMutation.reset();
  };

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) handleFile(file);
  };

  const confirmImport = () => {
    if (pendingFile) importMutation.mutate(pendingFile);
  };

  const cancelImport = () => {
    setPendingFile(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <section className="card" id="database" data-testid="db-backup-card">
      <header className="flex items-baseline justify-between gap-2 mb-2">
        <h2 className="label">database backup</h2>
        {info.data?.exists && (
          <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
            {fmtBytes(info.data.size_bytes)}
            {info.data.modified_at && ` · modified ${fmtRel(info.data.modified_at)}`}
          </span>
        )}
      </header>
      <p className="text-[11px] text-[color:var(--fg-muted)] max-w-prose mb-3">
        The SQLite truth-store holds every ingested capture plus its reviews.
        Export a single-file snapshot to back up off-disk, or import a
        previous snapshot to roll back. Imports automatically back up the
        current DB before replacing.
      </p>

      {/* Current DB stats */}
      {info.isLoading ? (
        <Skeleton className="h-12" />
      ) : info.error ? (
        <ErrorBox err={info.error} />
      ) : !info.data?.exists ? (
        <div className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-[11px] text-warn">
          No database file exists yet. Ingest at least one capture to seed it,
          then export from here.
        </div>
      ) : (
        <dl
          className="grid grid-cols-[120px_1fr] gap-y-1 text-[11px] mb-3 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
          data-testid="db-info-list"
        >
          <dt className="text-[color:var(--fg-muted)]">path</dt>
          <dd className="font-mono truncate" title={info.data.path}>{info.data.path}</dd>
          <dt className="text-[color:var(--fg-muted)]">size</dt>
          <dd className="font-mono tabular-nums" data-testid="db-info-size">{fmtBytes(info.data.size_bytes)}</dd>
          <dt className="text-[color:var(--fg-muted)]">modified</dt>
          <dd className="font-mono">{info.data.modified_at ? fmtRel(info.data.modified_at) : "—"}</dd>
        </dl>
      )}

      {/* Schema version hint. Sits below the stats so operators can spot
          a pending migration that hasn't yet run (e.g. after a fresh
          build before first launch). pending count > 0 is rare and
          mostly tells us the supervisor hasn't booted yet. */}
      {schemaVersion.data && (
        <div
          className="text-[10px] text-[color:var(--fg-muted)] mb-3 -mt-2"
          data-testid="db-schema-version"
        >
          Schema version: {schemaVersion.data.user_version}
          {schemaVersion.data.max_known > schemaVersion.data.user_version &&
            ` · ${schemaVersion.data.migrations_pending.length} pending`}
        </div>
      )}

      {/* Export */}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <a
          href={api.dbExportUrl}
          download
          className={`btn btn-accent text-xs ${!info.data?.exists ? "pointer-events-none opacity-50" : ""}`}
          aria-disabled={!info.data?.exists}
          data-testid="db-export-link"
        >
          Export database
        </a>
        <span className="text-[10px] text-[color:var(--fg-muted)]">
          Streams as a single .sqlite3 file via the browser's Save dialog.
        </span>
      </div>

      {/* Import zone */}
      <div className="grid gap-2">
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
          className={`rounded-md border border-dashed px-4 py-4 text-center text-xs transition-colors ${
            dragOver
              ? "border-accent bg-accent/10"
              : "border-[color:var(--border)] bg-[color:var(--bg-elev2)]/30"
          }`}
          data-testid="db-import-dropzone"
        >
          <div className="text-[color:var(--fg-dim)] mb-1">
            Drop a <code className="font-mono">.sqlite3</code> file here to import
          </div>
          <button
            type="button"
            className="btn text-xs"
            onClick={() => fileRef.current?.click()}
            data-testid="db-import-pick"
          >
            Choose file…
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".sqlite3,.sqlite,.db,application/octet-stream"
            className="hidden"
            onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
            data-testid="db-import-file-input"
          />
        </div>

        {/* Confirmation panel — shown only when a file is queued */}
        {pendingFile && !importMutation.isSuccess && (
          <div
            className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-[11px] grid gap-2"
            role="alertdialog"
            data-testid="db-import-confirm"
          >
            <div className="flex items-start gap-2">
              <span aria-hidden className="text-warn">⚠</span>
              <div>
                <div className="font-semibold text-warn">Confirm database import</div>
                <div className="text-[color:var(--fg-dim)] mt-0.5">
                  This will replace your current database with{" "}
                  <code className="font-mono">{pendingFile.name}</code>{" "}
                  ({fmtBytes(pendingFile.size)}). A backup of the current DB
                  will be saved automatically.
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                className="btn btn-accent text-xs"
                onClick={confirmImport}
                disabled={importMutation.isPending}
                data-testid="db-import-confirm-btn"
              >
                {importMutation.isPending ? "importing…" : "Replace database"}
              </button>
              <button
                type="button"
                className="btn text-xs"
                onClick={cancelImport}
                disabled={importMutation.isPending}
                data-testid="db-import-cancel-btn"
              >
                Cancel
              </button>
              {importMutation.isError && (
                <span className="text-[10px] text-bad ml-auto" role="alert" data-testid="db-import-error">
                  {importMutation.error instanceof Error
                    ? importMutation.error.message
                    : "import failed"}
                </span>
              )}
            </div>
          </div>
        )}

        {/* Success state — the page reload happens 2s after this lands */}
        {importedResult?.ok && (
          <div
            className="rounded-md border border-good/40 bg-good/5 px-3 py-2 text-[11px] grid gap-1"
            role="status"
            data-testid="db-import-success"
          >
            <div className="text-good font-semibold">
              ✓ Imported {fmtBytes(importedResult.imported_size_bytes)}
            </div>
            <div className="text-[color:var(--fg-dim)]">
              Backup of the previous DB saved at{" "}
              <code className="font-mono text-[10px]">{importedResult.backup_path ?? "(no previous DB)"}</code>.
            </div>
            <div className="text-[color:var(--fg-muted)] mt-1">
              Reloading the app in 2 seconds so the new database takes effect…
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Webhook output (Slack/Discord/Teams) ────────────────────────────────
//
// Pairs with the DeepSeek card visually (same hero-gradient shell + accent
// border) so the two "outbound integration" controls feel like siblings.
// The URL is treated as a soft secret — the GET never echoes it back; the
// chip pattern from the DeepSeek "API key configured ✓" UX is reused.
function WebhookOutputCard() {
  const qc = useQueryClient();
  const status = useQuery<WebhookStatus>({
    queryKey: ["webhook-config"],
    queryFn: api.webhookConfig,
    refetchInterval: 8_000,
  });

  // Locally-edited values (explicit "save" semantics like the DeepSeek
  // card's sampling/cap inputs). The enable toggle auto-flushes.
  const [url, setUrl] = useState<string>("");
  const [urlTouched, setUrlTouched] = useState<boolean>(false);
  const [editingUrl, setEditingUrl] = useState<boolean>(false);
  const [minScore, setMinScore] = useState<number>(0.7);
  const [assetFilter, setAssetFilter] = useState<string[]>([]);
  const [reasonFilter, setReasonFilter] = useState<string[]>([]);
  // Inline result chip for "Test webhook" — shows {ok, status} after a
  // test fires. Auto-clears after 5s on success / 8s on failure via
  // `testResultTimerRef`, OR the next time the operator edits anything.
  const [testResult, setTestResult] = useState<WebhookTestResult | null>(null);
  // Lifetime-managed timer handle so we can cancel a pending auto-clear
  // when a NEW test fires or the user navigates away from this card
  // before the previous chip times out. Without this, two fast clicks
  // would set up two competing setTimeouts and the chip would vanish on
  // the FIRST timer instead of the most-recent one.
  const testResultTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * Set the test-result chip AND schedule its auto-clear.
   *
   * Success (`ok: true`) clears after 5_000ms; failure (`ok: false`)
   * lingers for 8_000ms so the operator has time to read the error
   * code. Passing `null` cancels the timer outright (e.g. when the
   * operator edits the URL).
   */
  const setTestResultWithAutoClear = (result: WebhookTestResult | null) => {
    if (testResultTimerRef.current !== null) {
      clearTimeout(testResultTimerRef.current);
      testResultTimerRef.current = null;
    }
    setTestResult(result);
    if (result === null) return;
    const ttl = result.ok ? 5_000 : 8_000;
    testResultTimerRef.current = setTimeout(() => {
      setTestResult(null);
      testResultTimerRef.current = null;
    }, ttl);
  };

  // Cancel any pending auto-clear if the card unmounts (route change,
  // theme switch, etc.) — leaking a setTimeout against a torn-down
  // component would surface as a React state-update warning.
  useEffect(() => {
    return () => {
      if (testResultTimerRef.current !== null) {
        clearTimeout(testResultTimerRef.current);
        testResultTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const d = status.data;
    if (!d) return;
    setMinScore(d.min_score);
    setAssetFilter(d.asset_class_filter ?? []);
    setReasonFilter(d.reason_code_filter ?? []);
  }, [
    status.data?.min_score,
    // Stringify-stable comparisons so React doesn't churn the dependency
    // array on identical array contents (filters arrive fresh on every
    // poll).
    JSON.stringify(status.data?.asset_class_filter),
    JSON.stringify(status.data?.reason_code_filter),
  ]);

  const save = useMutation({
    mutationFn: (patch: Partial<WebhookConfigPatch>) => api.webhookSaveConfig(patch),
    onSuccess: () => {
      setUrl("");
      setUrlTouched(false);
      setEditingUrl(false);
      setTestResultWithAutoClear(null);
      qc.invalidateQueries({ queryKey: ["webhook-config"] });
    },
  });

  const test = useMutation({
    mutationFn: () => api.webhookTest({ title: "Catchem webhook test" }),
    onSuccess: (data) => {
      setTestResultWithAutoClear(data);
      qc.invalidateQueries({ queryKey: ["webhook-config"] });
    },
    onError: (err) => {
      // Translate fetch-layer failures (rate-limit 429, network error,
      // sidecar down) into the same {ok,status} envelope so the chip
      // renders consistently regardless of where the failure happened.
      const message = err instanceof Error ? err.message : "request_failed";
      setTestResultWithAutoClear({
        ok: false,
        status: message.slice(0, 120),
        url_configured: false,
        generated_at: "",
      });
    },
  });

  if (status.isLoading) return <Skeleton className="h-40" />;
  if (status.error) return <ErrorBox err={status.error} />;
  const d = status.data;
  if (!d) return null;

  const toggleEnable = () => save.mutate({ enabled: !d.enabled });

  const toggleChip = (current: string[], val: string): string[] =>
    current.includes(val) ? current.filter((v) => v !== val) : [...current, val];

  const dirty =
    minScore !== d.min_score ||
    JSON.stringify(assetFilter) !== JSON.stringify(d.asset_class_filter ?? []) ||
    JSON.stringify(reasonFilter) !== JSON.stringify(d.reason_code_filter ?? []) ||
    urlTouched;

  const saveFormValues = () => {
    save.mutate({
      min_score: minScore,
      asset_class_filter: assetFilter.length ? assetFilter : null,
      reason_code_filter: reasonFilter.length ? reasonFilter : null,
      ...(urlTouched ? { url } : {}),
    });
  };

  const pillTone: "good" | "warn" | "bad" = !d.url_configured
    ? "warn"
    : d.enabled
      ? "good"
      : "warn";
  const pillText = !d.url_configured
    ? "needs url"
    : d.enabled
      ? "ready"
      : "disabled";

  const totalSent = d.stats.sent;
  const totalAttempted = d.stats.attempted;
  const totalFailed = d.stats.failed;

  return (
    <section
      id="webhook"
      className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-5 grid gap-4 scroll-mt-24"
      data-testid="webhook-output-card"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute -top-16 -right-16 h-40 w-40 rounded-full bg-accent/15 blur-3xl"
      />

      <header className="relative flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="relative flex h-2 w-2" aria-hidden>
            {d.enabled && d.url_configured && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-good opacity-75" />
            )}
            <span
              className={`relative inline-flex h-2 w-2 rounded-full ${
                pillTone === "good" ? "bg-good" : pillTone === "warn" ? "bg-warn" : "bg-bad"
              }`}
            />
          </span>
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Settings · Outbound Webhook
            </div>
            <h2 className="text-base font-semibold mt-0.5 tracking-tight">
              Slack / Discord / Teams webhook output
            </h2>
            <div className={`mt-1 text-[11px] tabular-nums ${
              pillTone === "good" ? "text-good" : pillTone === "warn" ? "text-warn" : "text-bad"
            }`}>
              {pillText}
              {" · "}
              <span className="text-[color:var(--fg-muted)]">
                {totalSent}/{totalAttempted} sent · {totalFailed} failed
              </span>
            </div>
          </div>
        </div>

        <ToggleSwitch
          checked={d.enabled}
          onToggle={toggleEnable}
          busy={save.isPending}
          label={d.enabled ? "enabled" : "disabled"}
          data-testid="webhook-enable-toggle"
        />
      </header>

      <p className="relative text-[11px] text-[color:var(--fg-muted)] max-w-prose">
        When enabled, Catchem POSTs a Slack-compatible JSON payload to your webhook URL
        for every record whose <code className="font-mono">finance_relevance_score</code> clears the floor
        (and the optional asset-class / reason-code filters). The URL is held in sidecar
        memory only — it's never written to the workspace snapshot or any export.
      </p>

      {/* ── Section: Webhook URL ── */}
      <section className="relative grid gap-2">
        <label htmlFor="wh-url" className="label">webhook URL</label>
        {d.url_configured && !editingUrl ? (
          <div
            className="flex items-center justify-between gap-2 rounded-md border border-good/40 bg-[color:var(--bg-elev)] px-3 py-1.5 text-xs"
            data-testid="webhook-url-configured"
          >
            <span className="flex items-center gap-1 text-good">
              <span aria-hidden>✓</span> Webhook URL configured
            </span>
            <button
              type="button"
              className="btn text-[10px] !py-0.5 !px-2"
              onClick={() => setEditingUrl(true)}
              data-testid="webhook-url-replace"
            >
              Replace URL
            </button>
          </div>
        ) : (
          <>
            <input
              id="wh-url"
              type="url"
              className="input"
              placeholder="https://hooks.slack.com/services/T.../B.../..."
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setUrlTouched(true);
                setTestResultWithAutoClear(null);
              }}
              autoComplete="off"
              spellCheck={false}
              data-testid="webhook-url-input"
            />
            <div className="flex items-center justify-between text-[10px] text-[color:var(--fg-muted)]">
              <span>
                {d.url_configured
                  ? "paste a new URL to replace the stored one"
                  : "Slack incoming-webhook, Discord, or Teams connector URL"}
              </span>
              {d.url_configured && (
                <button
                  type="button"
                  className="text-accent hover:underline"
                  onClick={() => {
                    setEditingUrl(false);
                    setUrl("");
                    setUrlTouched(false);
                  }}
                >
                  cancel
                </button>
              )}
            </div>
          </>
        )}
        {/* Provider hint sits under the URL input/chip alike so it reads
            even after the chip collapses the editor away. Mentions the
            Slack token-bearing path shape because that's the most
            common confusion ("is the whole thing the URL?"). */}
        <p
          className="text-[10px] text-[color:var(--fg-muted)] leading-snug"
          data-testid="webhook-url-help"
        >
          Slack webhook URLs look like{" "}
          <code className="font-mono">
            https://hooks.slack.com/services/XXX/YYY/ZZZ
          </code>
          . Test will send a sample alert. Discord and Microsoft Teams
          webhooks also work.
        </p>
      </section>

      {/* ── Section: Min score slider ── */}
      <section className="relative grid gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <label htmlFor="wh-score" className="label">
            minimum score
          </label>
          <span className="text-[11px] tabular-nums text-accent font-semibold">
            {minScore.toFixed(2)}
          </span>
        </div>
        <input
          id="wh-score"
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={minScore}
          onChange={(e) => setMinScore(parseFloat(e.target.value))}
          className="w-full accent-[color:var(--accent)]"
          data-testid="webhook-score-slider"
        />
        <div className="flex justify-between text-[9px] text-[color:var(--fg-muted)] tabular-nums">
          <button type="button" className="hover:text-accent" onClick={() => setMinScore(0)}>0.00</button>
          <button type="button" className="hover:text-accent" onClick={() => setMinScore(0.5)}>0.50</button>
          <button type="button" className="hover:text-accent" onClick={() => setMinScore(0.7)}>0.70</button>
          <button type="button" className="hover:text-accent" onClick={() => setMinScore(0.85)}>0.85</button>
          <button type="button" className="hover:text-accent" onClick={() => setMinScore(1.0)}>1.00</button>
        </div>
        <div className="text-[10px] text-[color:var(--fg-muted)]">
          only records with <code className="font-mono">finance_relevance_score</code> ≥ this fire
        </div>
      </section>

      {/* ── Section: Asset-class filter chips ── */}
      <section className="relative grid gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className="label">asset classes</span>
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            {assetFilter.length === 0 ? "any (no filter)" : `${assetFilter.length} selected`}
          </span>
        </div>
        <div
          className="flex flex-wrap gap-1.5"
          data-testid="webhook-asset-chips"
          role="group"
          aria-label="Asset class filter"
        >
          {ASSET_CLASSES.map((ac) => {
            const active = assetFilter.includes(ac);
            return (
              <button
                key={ac}
                type="button"
                onClick={() => setAssetFilter((cur) => toggleChip(cur, ac))}
                className={`text-[10px] tabular-nums rounded-full border px-2.5 py-0.5 transition-colors ${
                  active
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-[color:var(--border)] text-[color:var(--fg-dim)] hover:border-accent/40 hover:text-accent"
                }`}
                aria-pressed={active}
                data-testid={`webhook-asset-${ac}`}
              >
                {ac}
              </button>
            );
          })}
        </div>
      </section>

      {/* ── Section: Reason-code filter chips ── */}
      <section className="relative grid gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className="label">reason codes</span>
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            {reasonFilter.length === 0 ? "any (no filter)" : `${reasonFilter.length} selected`}
          </span>
        </div>
        <div
          className="flex flex-wrap gap-1.5"
          data-testid="webhook-reason-chips"
          role="group"
          aria-label="Reason code filter"
        >
          {REASON_CODES.map((rc) => {
            const active = reasonFilter.includes(rc);
            return (
              <button
                key={rc}
                type="button"
                onClick={() => setReasonFilter((cur) => toggleChip(cur, rc))}
                className={`text-[10px] tabular-nums rounded-full border px-2.5 py-0.5 transition-colors ${
                  active
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-[color:var(--border)] text-[color:var(--fg-dim)] hover:border-accent/40 hover:text-accent"
                }`}
                aria-pressed={active}
                data-testid={`webhook-reason-${rc}`}
              >
                {rc}
              </button>
            );
          })}
        </div>
      </section>

      {/* ── Action row ── */}
      <div className="relative flex flex-wrap items-center gap-2 pt-3 border-t border-[color:var(--border-subtle)]">
        <button
          type="button"
          className="btn btn-accent text-xs"
          disabled={!dirty || save.isPending}
          onClick={saveFormValues}
          data-testid="webhook-save-btn"
        >
          {save.isPending ? "saving…" : "save changes"}
        </button>
        {(() => {
          // Test button is enabled ONLY when a URL is SAVED server-side.
          // The backend /api/webhook/test reads its target from the
          // persisted cfg.url and ignores the request body's url field —
          // so a freshly-typed-but-unsaved URL is NOT what gets tested.
          // Enabling on a typed-only URL would test the OLD saved URL (or
          // return no_url_configured), falsely "confirming" the new URL.
          // The operator must Save first; until then the chip below the
          // input already nudges them. Disabled while a test is in flight.
          const testDisabled = !d.url_configured || test.isPending;
          return (
            <button
              type="button"
              className="btn text-xs inline-flex items-center gap-1.5"
              disabled={testDisabled}
              onClick={() => test.mutate()}
              title={
                !d.url_configured
                  ? "save a webhook URL first, then test it"
                  : "send a synthetic test payload to the saved URL"
              }
              data-testid="webhook-test-btn"
            >
              {test.isPending && (
                <span
                  aria-hidden
                  className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent"
                  data-testid="webhook-test-spinner"
                />
              )}
              {test.isPending ? "sending…" : "Test webhook"}
            </button>
          );
        })()}
        {testResult && (() => {
          // Render success/failure with the structured information the
          // backend reported. `status === "sent"` is the happy path;
          // `status === "no_url_configured"` is the friendly 200 envelope
          // for the unsaved-URL case; everything else (`http_500`,
          // `timeout`, `invalid_url`, `error_*`) renders as a failure.
          if (testResult.ok && testResult.status === "sent") {
            return (
              <span
                className="text-[10px] tabular-nums text-good"
                role="status"
                aria-live="polite"
                data-testid="webhook-test-result"
                data-result="success"
              >
                ✓ Test webhook sent (HTTP 200)
              </span>
            );
          }
          const failureText =
            testResult.status === "no_url_configured"
              ? "save a webhook URL first"
              : testResult.status;
          return (
            <span
              className="text-[10px] tabular-nums text-bad"
              role="alert"
              aria-live="assertive"
              data-testid="webhook-test-result"
              data-result="failure"
            >
              ✗ Test failed: {failureText}
            </span>
          );
        })()}
        {dirty && !save.isPending && (
          <span className="text-[10px] text-[color:var(--fg-muted)] ml-auto">unsaved changes</span>
        )}
        {save.isError && (
          <span className="text-[10px] text-bad ml-auto" role="alert">
            {save.error instanceof Error ? save.error.message : "save failed"}
          </span>
        )}
        {save.isSuccess && !dirty && (
          <span className="text-[10px] text-good ml-auto" data-testid="webhook-save-success">saved ✓</span>
        )}
      </div>

      {d.last_status && d.last_status !== "sent" && d.last_error && (
        <p className="relative text-[10px] text-warn">
          last delivery: <code className="font-mono">{d.last_error}</code>
        </p>
      )}

      <p className="relative text-[10px] text-[color:var(--fg-muted)]">
        Compatible with Slack incoming webhooks, Discord webhooks, and Microsoft Teams
        connectors. Webhook URLs contain auth tokens — they're treated as secrets and
        excluded from <code className="font-mono">workspace snapshot</code> exports.
      </p>
    </section>
  );
}

// ── Workspace snapshot (preferences export/import) ──────────────────────
//
// This is the *preferences* counterpart to DatabaseBackupCard. Whereas
// the DB card ships the SQLite truth-store as a binary blob (and forces
// a reload because the supervisor's connection becomes stale), this
// card exports only the curated set of localStorage keys that
// represent user-facing workspace state — theme, accent, watchlist,
// onboarding flags, etc. No API keys, no session state, no DB rows.
//
// On import we deliberately do NOT auto-reload. We let the user see
// what was restored vs. skipped and click "Reload" themselves so they
// can verify the file was what they expected before the SPA reboots.
function WorkspaceSnapshotCard() {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  // We parse the picked file on the way in so we can render a diff
  // preview BEFORE the user confirms the restore. The full Snapshot is
  // held alongside the diff so confirmImport doesn't have to re-read
  // the file (which the user has already approved at this point).
  const [pendingSnapshot, setPendingSnapshot] = useState<Snapshot | null>(null);
  const [pendingDiff, setPendingDiff] = useState<SnapshotDiff | null>(null);
  const [importResult, setImportResult] = useState<{ restored: string[]; skipped: string[] } | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  // "Apply changes" stays disabled until the operator has acknowledged
  // the diff. Flipped by clicking the "Reviewed" checkbox in the diff
  // preview, reset every time a new file is picked.
  const [reviewedDiff, setReviewedDiff] = useState(false);

  const handleExport = () => {
    // exportSnapshot reads localStorage synchronously, so the download
    // fires immediately. No spinner needed — the browser drives the
    // Save dialog from `downloadSnapshot`.
    const snap = exportSnapshot();
    downloadSnapshot(snap);
  };

  const handleFilePicked = async (file: File | null) => {
    if (!file) return;
    setPendingFile(file);
    setImportResult(null);
    setImportError(null);
    setReviewedDiff(false);
    setPendingSnapshot(null);
    setPendingDiff(null);
    // Parse + diff eagerly so the operator sees what's about to change
    // immediately. Errors from a malformed file land in `importError`
    // and the diff stays null (the confirm panel surfaces the error).
    try {
      const parsed = await readSnapshotFile(file);
      const current = exportSnapshot();
      setPendingSnapshot(parsed);
      setPendingDiff(diffSnapshots(current, parsed));
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleConfirmImport = async () => {
    if (!pendingSnapshot) return;
    setImportBusy(true);
    setImportError(null);
    try {
      const result = importSnapshot(pendingSnapshot);
      setImportResult(result);
      setPendingFile(null);
      setPendingSnapshot(null);
      setPendingDiff(null);
      setReviewedDiff(false);
      if (fileRef.current) fileRef.current.value = "";
    } catch (err) {
      setImportError(err instanceof Error ? err.message : String(err));
    } finally {
      setImportBusy(false);
    }
  };

  const handleCancelImport = () => {
    setPendingFile(null);
    setPendingSnapshot(null);
    setPendingDiff(null);
    setReviewedDiff(false);
    setImportError(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <section className="card" id="workspace-snapshot" data-testid="workspace-snapshot-card">
      <header className="flex items-baseline justify-between gap-2 mb-2">
        <h2 className="label">workspace snapshot</h2>
        <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          {SNAPSHOT_ALLOW_LIST.length} preference{SNAPSHOT_ALLOW_LIST.length === 1 ? "" : "s"} tracked
        </span>
      </header>
      <p className="text-[11px] text-[color:var(--fg-muted)] max-w-prose mb-3">
        Export a JSON snapshot of your workspace preferences — theme, accent, watchlist,
        onboarding flags — to share between machines or to back up your setup. This is
        separate from the database backup above and contains <strong>no sensitive data</strong>
        : no API keys, no captured records, no session state. Importing only restores keys on
        the allow-list; unknown keys are skipped.
      </p>

      {/* Allow-list disclosure — so the user can see exactly what's about
          to leave or land on their machine. */}
      <details className="mb-3" data-testid="snapshot-allowlist-disclosure">
        <summary className="cursor-pointer text-[11px] text-accent hover:underline">
          What's included? ({SNAPSHOT_ALLOW_LIST.length} keys)
        </summary>
        <ul
          className="mt-2 grid gap-0.5 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 text-[10px] font-mono text-[color:var(--fg-dim)]"
          data-testid="snapshot-allowlist"
        >
          {SNAPSHOT_ALLOW_LIST.map((k) => (
            <li key={k}>{k}</li>
          ))}
        </ul>
      </details>

      {/* Export */}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <button
          type="button"
          className="btn btn-accent text-xs"
          onClick={handleExport}
          data-testid="snapshot-export-btn"
        >
          Export workspace snapshot
        </button>
        <span className="text-[10px] text-[color:var(--fg-muted)]">
          Pretty-printed JSON, safe to share — no secrets included.
        </span>
      </div>

      {/* Import */}
      <div className="grid gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn text-xs"
            onClick={() => fileRef.current?.click()}
            data-testid="snapshot-import-pick"
          >
            Import workspace snapshot…
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => handleFilePicked(e.target.files?.[0] ?? null)}
            data-testid="snapshot-import-file-input"
          />
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            Reload after import to apply theme + accent changes.
          </span>
        </div>

        {/* Confirmation panel — shown after a file is selected, before
            we write to localStorage. Gives the user one last chance to
            back out and confirms the file name + size. */}
        {pendingFile && !importResult && (
          <div
            className="rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-[11px] grid gap-2"
            role="alertdialog"
            data-testid="snapshot-import-confirm"
          >
            <div className="flex items-start gap-2">
              <span aria-hidden className="text-warn">⚠</span>
              <div>
                <div className="font-semibold text-warn">Confirm workspace import</div>
                <div className="text-[color:var(--fg-dim)] mt-0.5">
                  This will overwrite your current preferences with values from{" "}
                  <code className="font-mono">{pendingFile.name}</code>{" "}
                  ({fmtBytes(pendingFile.size)}). Only allow-listed keys are written.
                </div>
              </div>
            </div>

            {/* Diff preview — only rendered once the file has been
                parsed successfully. If parsing failed, `pendingDiff`
                stays null and `importError` (below) carries the reason. */}
            {pendingDiff && (
              <SnapshotDiffPreview
                diff={pendingDiff}
                reviewed={reviewedDiff}
                onReviewedChange={setReviewedDiff}
              />
            )}

            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                className="btn btn-accent text-xs"
                onClick={handleConfirmImport}
                disabled={importBusy || !pendingDiff || !reviewedDiff}
                data-testid="snapshot-import-confirm-btn"
              >
                {importBusy ? "importing…" : "Apply changes"}
              </button>
              <button
                type="button"
                className="btn text-xs"
                onClick={handleCancelImport}
                disabled={importBusy}
                data-testid="snapshot-import-cancel-btn"
              >
                Cancel
              </button>
              {importError && (
                <span className="text-[10px] text-bad ml-auto" role="alert" data-testid="snapshot-import-error">
                  {importError}
                </span>
              )}
            </div>
          </div>
        )}

        {/* Result panel — restored count, skipped list, explicit Reload
            button. We deliberately do NOT auto-reload so the user can
            audit the skipped list first. */}
        {importResult && (
          <div
            className="rounded-md border border-good/40 bg-good/5 px-3 py-2 text-[11px] grid gap-1"
            role="status"
            data-testid="snapshot-import-success"
          >
            <div className="text-good font-semibold">
              ✓ Restored {importResult.restored.length} preference{importResult.restored.length === 1 ? "" : "s"}
              {importResult.skipped.length > 0
                ? `, skipped ${importResult.skipped.length}`
                : ""}
            </div>
            {importResult.skipped.length > 0 && (
              <details className="text-[10px] text-[color:var(--fg-dim)]" data-testid="snapshot-import-skipped">
                <summary className="cursor-pointer text-[color:var(--fg-muted)] hover:text-accent">
                  Skipped keys ({importResult.skipped.length})
                </summary>
                <ul className="mt-1 grid gap-0.5 font-mono pl-2">
                  {importResult.skipped.map((k) => (
                    <li key={k}>• {k}</li>
                  ))}
                </ul>
              </details>
            )}
            <div className="text-[color:var(--fg-muted)] mt-1">
              Reload the app to apply theme / accent / watchlist changes.
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                className="btn btn-accent text-xs"
                onClick={() => window.location.reload()}
                data-testid="snapshot-reload-btn"
              >
                Reload app
              </button>
              <button
                type="button"
                className="btn text-xs"
                onClick={() => setImportResult(null)}
                data-testid="snapshot-dismiss-btn"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Snapshot diff preview (renders inside WorkspaceSnapshotCard) ────────
//
// Side-by-side comparison of the user's current preferences vs. the
// snapshot they're about to import. Three collapsible buckets (added /
// changed / removed) plus a small "identical: N" line for keys that
// would be a no-op restore. Long values are truncated with an ellipsis
// so the preview stays readable in the ~400px-wide card. The Apply
// button (in the parent) stays disabled until the operator ticks the
// "Reviewed" checkbox.
function SnapshotDiffPreview({
  diff,
  reviewed,
  onReviewedChange,
}: {
  diff: SnapshotDiff;
  reviewed: boolean;
  onReviewedChange: (checked: boolean) => void;
}) {
  const noChanges =
    diff.added.length === 0 &&
    diff.changed.length === 0 &&
    diff.removed.length === 0;

  return (
    <div
      className="grid gap-1.5 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2.5 py-2"
      data-testid="snapshot-diff-preview"
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] font-semibold">
          Diff preview
        </div>
        <div
          className="text-[10px] tabular-nums text-[color:var(--fg-muted)]"
          data-testid="snapshot-diff-summary"
        >
          {diff.added.length} added · {diff.changed.length} changed
          {diff.removed.length > 0 && ` · ${diff.removed.length} removed`}
        </div>
      </div>

      {noChanges && (
        <div
          className="text-[10px] text-[color:var(--fg-dim)] italic"
          data-testid="snapshot-diff-no-changes"
        >
          No changes — every key in the imported file matches your current value.
        </div>
      )}

      {diff.added.length > 0 && (
        <SnapshotDiffGroup
          tone="good"
          icon="+"
          label={`${diff.added.length} ${diff.added.length === 1 ? "key" : "keys"} will be added`}
          testid="snapshot-diff-added"
          entries={diff.added.map((key) => ({ key, value: null }))}
        />
      )}

      {diff.changed.length > 0 && (
        <SnapshotDiffGroup
          tone="warn"
          icon="~"
          label={`${diff.changed.length} ${diff.changed.length === 1 ? "key" : "keys"} will be overwritten`}
          testid="snapshot-diff-changed"
          entries={diff.changed.map((c) => ({
            key: c.key,
            from: c.from,
            to: c.to,
          }))}
        />
      )}

      {/* Removed group intentionally hidden when empty — most imports
          won't remove anything, and surfacing an empty bucket every
          time creates noise. */}
      {diff.removed.length > 0 && (
        <SnapshotDiffGroup
          tone="bad"
          icon="−"
          label={`${diff.removed.length} ${diff.removed.length === 1 ? "key" : "keys"} not in imported file`}
          testid="snapshot-diff-removed"
          entries={diff.removed.map((key) => ({ key, value: null }))}
          hint="(your current values stay — importing won't actively delete them)"
        />
      )}

      {diff.identical.length > 0 && (
        <div
          className="text-[10px] text-[color:var(--fg-muted)] pl-1"
          data-testid="snapshot-diff-identical-count"
        >
          {diff.identical.length}{" "}
          {diff.identical.length === 1 ? "key matches" : "keys match"} already (no-op)
        </div>
      )}

      <label
        className="flex items-center gap-2 mt-1 pt-1 border-t border-[color:var(--border-subtle)] text-[10px] cursor-pointer select-none"
        data-testid="snapshot-diff-reviewed-label"
      >
        <input
          type="checkbox"
          checked={reviewed}
          onChange={(e) => onReviewedChange(e.target.checked)}
          className="accent-[color:var(--accent)]"
          data-testid="snapshot-diff-reviewed-checkbox"
        />
        <span className="text-[color:var(--fg-dim)]">
          I&apos;ve reviewed these changes
        </span>
      </label>
    </div>
  );
}

/** Truncate a value for display in the diff preview. Long JSON-stringy
 *  values (watchlists, KPI history) blow out the row otherwise. */
function truncateValue(v: string): string {
  return v.length > 40 ? `${v.slice(0, 39)}…` : v;
}

/** One bucket in the diff preview — collapsible <details> with the
 *  affected keys and (for the "changed" bucket) the from→to pair. */
function SnapshotDiffGroup({
  tone,
  icon,
  label,
  entries,
  testid,
  hint,
}: {
  tone: "good" | "warn" | "bad";
  icon: string;
  label: string;
  entries: { key: string; value?: string | null; from?: string; to?: string }[];
  testid: string;
  hint?: string;
}) {
  const toneClass =
    tone === "good"
      ? "border-good/40 bg-good/5 text-good"
      : tone === "warn"
        ? "border-warn/40 bg-warn/5 text-warn"
        : "border-bad/40 bg-bad/5 text-bad";

  return (
    <details
      className={`rounded border ${toneClass} px-2 py-1 text-[10px]`}
      data-testid={testid}
    >
      <summary className="cursor-pointer font-semibold flex items-center gap-1.5">
        <span aria-hidden className="font-mono">{icon}</span>
        <span>{label}</span>
      </summary>
      <ul className="mt-1 grid gap-0.5 font-mono text-[color:var(--fg-dim)] pl-3">
        {entries.map((e) => (
          <li key={e.key} className="grid gap-0">
            <span className="text-[color:var(--fg)]">{e.key}</span>
            {e.from !== undefined && e.to !== undefined && (
              <span className="pl-3 text-[color:var(--fg-muted)]">
                <span title={e.from}>{truncateValue(e.from)}</span>
                <span aria-hidden> → </span>
                <span title={e.to}>{truncateValue(e.to)}</span>
              </span>
            )}
          </li>
        ))}
      </ul>
      {hint && (
        <div className="mt-1 pl-3 text-[9px] italic text-[color:var(--fg-muted)] font-sans">
          {hint}
        </div>
      )}
    </details>
  );
}

// ── Reset preferences (destructive — wipe all catchem.* localStorage) ────
//
// Sits below WebhookOutputCard and is the LAST card in the settings list so
// users never accidentally land on it. The bordered `border-bad/40` shell
// signals destructive intent at a glance and matches the visual tone used by
// SnapshotDiffGroup's "removed" bucket. The button itself is a plain
// `btn-bad` (no confirmation built-in) — the modal below catches the click.
//
// Scope of the reset:
//   - EVERY key whose name starts with `catchem.`. We do NOT use the
//     SNAPSHOT_ALLOW_LIST because that's a curated set for portable
//     snapshots; the reset is meant to wipe the WHOLE local profile so a
//     stuck user can recover from any combination of feature-flag state.
//   - Keys NOT under the `catchem.` namespace (other apps in the same
//     origin, e.g. dev-tools, third-party widgets) are left alone.
//   - The SQLite truth-store is untouched (it's not in localStorage), and
//     the DeepSeek API key isn't in localStorage either — it lives in the
//     user's .env or sidecar memory, so this reset can't leak it.
//
// After a confirmed wipe we force `window.location.reload()` so React
// re-mounts against a fresh storage state — otherwise components that
// cached values in module-scope (theme, accent, lang) would keep painting
// the old preferences until the next hard refresh.
const RESET_PREFERENCES_NAMESPACE = "catchem.";

/** Enumerate every key in localStorage that starts with `catchem.`.
 *  Tolerant of jsdom's incomplete Storage shim — if `localStorage.length`
 *  or `key(i)` throw, we return an empty array (the modal then shows "0
 *  preference keys" and the wipe still goes through; nothing breaks). */
export function collectCatchemKeys(): string[] {
  const out: string[] = [];
  try {
    const len = localStorage.length;
    for (let i = 0; i < len; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith(RESET_PREFERENCES_NAMESPACE)) out.push(k);
    }
  } catch {
    /* storage disabled / private mode — fall through with empty list */
  }
  return out;
}

/** Wipe every `catchem.*` key from localStorage. Returns the list of keys
 *  that were actually removed so callers (and tests) can assert the
 *  side-effect without snapshotting the whole store. */
export function resetCatchemPreferences(): string[] {
  // Collect first so we don't mutate while iterating — removing during a
  // for-loop over `localStorage.length` is well-defined per spec but
  // causes index drift that's easy to misread in tests.
  const keys = collectCatchemKeys();
  for (const k of keys) {
    try {
      localStorage.removeItem(k);
    } catch {
      /* a single bad key shouldn't abort the rest of the wipe */
    }
  }
  return keys;
}

function ResetPreferencesCard() {
  const [showModal, setShowModal] = useState(false);
  // Snapshot the count once, on render — the modal reads it live too, but
  // surfacing the count on the card itself helps users understand what
  // they're about to lose before they even click.
  const [keyCount, setKeyCount] = useState<number>(() => collectCatchemKeys().length);

  // Keep the on-card count fresh when storage changes from elsewhere
  // (cross-tab sync, snapshot import, manual setItem from other cards).
  // This is the same pattern useStorageSync uses; we just don't bother
  // with a hook because the count is purely cosmetic on the card.
  useEffect(() => {
    const refresh = () => setKeyCount(collectCatchemKeys().length);
    window.addEventListener("storage", refresh);
    return () => window.removeEventListener("storage", refresh);
  }, []);

  const handleOpen = () => {
    // Re-count on open so the modal headline reflects the latest state,
    // even if the card hasn't received a storage event since mount.
    setKeyCount(collectCatchemKeys().length);
    setShowModal(true);
  };

  return (
    <>
      <section
        className="card border border-bad/40"
        id="reset-preferences"
        data-testid="reset-preferences-card"
      >
        <header className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label text-bad">Reset preferences</h2>
          <span
            className="text-[10px] text-[color:var(--fg-muted)] tabular-nums"
            data-testid="reset-preferences-count"
          >
            {keyCount} key{keyCount === 1 ? "" : "s"} stored
          </span>
        </header>
        <p className="text-[11px] text-[color:var(--fg-muted)] max-w-prose mb-3">
          Clear all local preferences (theme, accent, language, watchlist,
          onboarding, etc.). The SQLite database is{" "}
          <strong>NOT touched</strong> — only your UI customisations.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn border-bad/60 text-bad hover:bg-bad/10 text-xs"
            onClick={handleOpen}
            data-testid="reset-preferences-open-btn"
          >
            Reset all preferences
          </button>
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            Destructive — confirmation required.
          </span>
        </div>
      </section>

      {showModal && (
        <ResetPreferencesModal
          onClose={() => setShowModal(false)}
          onConfirmed={() => {
            // The modal already wiped storage before calling this; we just
            // reload so module-scope caches (theme, accent, lang) reboot
            // against the now-empty state.
            window.location.reload();
          }}
        />
      )}
    </>
  );
}

function ResetPreferencesModal({
  onClose,
  onConfirmed,
}: {
  onClose: () => void;
  onConfirmed: () => void;
}) {
  useOverlaySurface({
    id: "reset-preferences-modal",
    open: true,
    onClose,
    lockBody: true,
  });

  // Live read on every render so the count stays accurate even if another
  // tab mutates storage between modal-open and confirm-click. Cheap —
  // O(n) over keys, n is tiny (< 30 in practice).
  const [keyCount, setKeyCount] = useState<number>(() => collectCatchemKeys().length);
  const cancelButtonRef = useRef<HTMLButtonElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  // Stash focus on open, restore on close — same pattern as OnboardingModal.
  useEffect(() => {
    lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
    const t = window.setTimeout(() => {
      cancelButtonRef.current?.focus();
    }, 0);
    return () => {
      window.clearTimeout(t);
      const prev = lastFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus();
        } catch {
          /* element may be gone — ignore */
        }
      }
    };
  }, []);

  // Refresh the live count on storage events (other tab, snapshot import).
  useEffect(() => {
    const refresh = () => setKeyCount(collectCatchemKeys().length);
    window.addEventListener("storage", refresh);
    return () => window.removeEventListener("storage", refresh);
  }, []);

  const handleConfirm = () => {
    resetCatchemPreferences();
    onConfirmed();
  };

  const titleId = "reset-preferences-modal-title";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 overflow-y-auto"
      data-testid="reset-preferences-modal-backdrop"
      // Clicking the backdrop dismisses — destructive actions should be
      // easy to back out of, and the explicit Reset button is the only
      // confirm path.
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid="reset-preferences-modal-card"
        className="relative w-full max-w-md rounded-xl border border-bad/40 hero-gradient shadow-soft animate-modal-enter overflow-hidden my-8"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute -top-24 -left-20 h-56 w-56 rounded-full bg-bad/20 blur-3xl"
        />
        <div className="relative p-6 grid gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-bad font-semibold">
              Destructive action
            </div>
            <h2
              id={titleId}
              className="text-xl font-semibold tracking-tight mt-1"
            >
              Are you sure?
            </h2>
          </div>
          <p
            className="text-sm leading-relaxed text-[color:var(--fg-dim)]"
            data-testid="reset-preferences-modal-body"
          >
            This clears{" "}
            <span
              className="font-semibold text-bad tabular-nums"
              data-testid="reset-preferences-modal-count"
            >
              {keyCount}
            </span>{" "}
            preference key{keyCount === 1 ? "" : "s"} and reloads the app. The
            SQLite database, your DeepSeek API key (in your <code className="font-mono">.env</code>),
            and any non-Catchem storage are <strong>not affected</strong>.
          </p>
          <div className="rounded-md border border-bad/40 bg-bad/5 px-3 py-2 text-[11px] text-[color:var(--fg-dim)]">
            After the reset you&apos;ll be returned to the welcome screen with the
            default theme, accent, and an empty watchlist. The first-run
            onboarding modal will reopen.
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
            <button
              ref={cancelButtonRef}
              type="button"
              className="btn text-xs"
              onClick={onClose}
              data-testid="reset-preferences-modal-cancel"
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn border-bad/60 text-bad hover:bg-bad/10 text-xs"
              onClick={handleConfirm}
              data-testid="reset-preferences-modal-confirm"
            >
              Reset &amp; reload
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
