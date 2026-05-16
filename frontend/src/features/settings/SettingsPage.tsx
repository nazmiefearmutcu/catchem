import { useTheme } from "@/hooks/useTheme";

const SHORTCUTS: { keys: string; description: string }[] = [
  { keys: "⌘K  /  Ctrl+K", description: "Open the command palette" },
  { keys: "g o", description: "Go to Overview" },
  { keys: "g f", description: "Go to Live Feed" },
  { keys: "g m", description: "Go to Market Map" },
  { keys: "g s", description: "Go to Symbols" },
  { keys: "g b", description: "Go to Benchmark Lab" },
  { keys: "g x", description: "Go to System / Ops" },
  { keys: "g ,", description: "Go to Settings" },
  { keys: "Esc", description: "Close drawer / palette" },
];

export function SettingsPage() {
  const { theme, toggle } = useTheme();

  return (
    <div className="grid gap-4 max-w-3xl">
      <h1 className="text-lg font-bold">Settings / Help</h1>

      <section className="card">
        <h2 className="label mb-2">theme</h2>
        <div className="flex items-center gap-3">
          <button onClick={toggle} className="btn btn-accent">
            switch to {theme === "dark" ? "light" : "dark"}
          </button>
          <span className="text-xs text-[color:var(--fg-dim)]">current: {theme}</span>
        </div>
        <p className="mt-2 text-[10px] text-[color:var(--fg-muted)]">
          Stored in <code>localStorage["fusion.theme"]</code>. Respects <em>prefers-reduced-motion</em> globally.
        </p>
      </section>

      <section className="card">
        <h2 className="label mb-2">keyboard shortcuts</h2>
        <ul className="grid gap-1">
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
          <dd className="text-[color:var(--fg-dim)] pl-3">Default. Diagnostic adapter is hard-refused even with the env flag on.</dd>
          <dt className="font-semibold">replay_existing</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">One pass over finalized Awareness JSONL. Idempotent via persisted offsets.</dd>
          <dt className="font-semibold">live_tail</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">Long-running tail of newly committed JSONL chunks.</dd>
          <dt className="font-semibold text-warn">research_diagnostic</dt>
          <dd className="text-[color:var(--fg-dim)] pl-3">Loads a labeled read-only diagnostic stamp from NewsImpact governance.
            Can NEVER override <code>is_finance_relevant</code>.</dd>
        </dl>
      </section>

      <section className="card">
        <h2 className="label mb-2">links</h2>
        <ul className="text-sm grid gap-1">
          <li><a className="text-accent hover:underline" href="/legacy">/legacy — vanilla dashboard</a></li>
          <li><a className="text-accent hover:underline" href="/docs">/docs — OpenAPI</a></li>
          <li><a className="text-accent hover:underline" href="/ui/summary">/ui/summary — JSON overview</a></li>
          <li><a className="text-accent hover:underline" href="/ui/guards">/ui/guards — guard snapshot</a></li>
          <li><a className="text-accent hover:underline" href="/ui/benchmark/latest">/ui/benchmark/latest — re-run benchmark</a></li>
        </ul>
      </section>
    </div>
  );
}
