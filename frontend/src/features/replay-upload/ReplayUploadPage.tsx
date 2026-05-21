import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { AnalysisSummary } from "@/components/AnalysisSummary";
import { DropZone } from "@/components/DropZone";
import { ErrorBox, Skeleton } from "@/components/Skeleton";
import type { DemoRunResponse, ReplayRunResponse } from "@/types/api";

type Tab = "paste" | "upload" | "replay";

const TAB_LABEL: Record<Tab, string> = {
  paste: "Paste article",
  upload: "Upload file",
  replay: "Replay JSONL",
};

export function ReplayUploadPage() {
  const [tab, setTab] = useState<Tab>("paste");
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_360px]">
      <section className="grid gap-3">
        <div role="tablist" aria-label="Replay/Upload mode" className="flex gap-1">
          {(["paste", "upload", "replay"] as const).map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`chip text-xs ${tab === t ? "chip-active" : ""}`}
              data-testid={`tab-${t}`}
            >
              {TAB_LABEL[t]}
            </button>
          ))}
        </div>
        {tab === "paste" ? <PasteForm /> : tab === "upload" ? <UploadForm /> : <ReplayForm />}
      </section>
      <aside className="grid gap-3">
        <HelpCard tab={tab} />
      </aside>
    </div>
  );
}

function PasteForm() {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [domain, setDomain] = useState("demo.local");
  const [url, setUrl] = useState("");
  const [result, setResult] = useState<DemoRunResponse | null>(null);

  const m = useMutation({
    mutationFn: () => api.demoPaste({ title, text, domain, url: url || undefined }),
    onSuccess: (r) => setResult(r),
  });

  const ready = title.trim().length > 0 && text.trim().length > 0;

  return (
    <>
      <form
        className="card grid gap-3"
        onSubmit={(e) => { e.preventDefault(); if (ready) m.mutate(); }}
        aria-busy={m.isPending}
      >
        <div className="grid gap-1">
          <label htmlFor="paste-title" className="label">title <span className="text-bad">*</span></label>
          <input
            id="paste-title"
            className="input"
            placeholder="Federal Reserve raises rates by 25 bps"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            autoComplete="off"
          />
        </div>
        <div className="grid gap-1">
          <label htmlFor="paste-text" className="label">article body <span className="text-bad">*</span></label>
          <textarea
            id="paste-text"
            className="input min-h-[260px] resize-y font-mono text-xs leading-relaxed"
            placeholder="Paste the article body here…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            required
            spellCheck={false}
          />
          <div className="text-[10px] text-[color:var(--fg-dim)]">{text.length.toLocaleString()} chars (max 5 MB)</div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1">
            <label htmlFor="paste-domain" className="label">domain</label>
            <input
              id="paste-domain"
              className="input"
              placeholder="reuters.com"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="grid gap-1">
            <label htmlFor="paste-url" className="label">url (optional)</label>
            <input
              id="paste-url"
              className="input"
              placeholder="https://reuters.com/…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              type="url"
              autoComplete="off"
            />
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button type="submit" className="btn btn-accent" disabled={!ready || m.isPending}>
            {m.isPending ? "analyzing…" : "Analyze"}
          </button>
          <button type="button" className="btn" disabled={m.isPending}
                  onClick={() => { setTitle(""); setText(""); setDomain("demo.local"); setUrl(""); setResult(null); }}>
            clear
          </button>
          {m.isError && (
            <span className="text-xs text-bad" role="alert">
              {m.error instanceof ApiError ? m.error.message : String(m.error)}
            </span>
          )}
        </div>
      </form>
      {m.isPending && <Skeleton className="h-40" />}
      {result && <AnalysisSummary result={result} />}
    </>
  );
}

function UploadForm() {
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("demo.local");
  const [result, setResult] = useState<DemoRunResponse | null>(null);
  const [previewName, setPreviewName] = useState<string | null>(null);

  const m = useMutation({
    mutationFn: (file: File) => api.demoUpload(file, { title: title || undefined, domain }),
    onSuccess: (r) => setResult(r),
  });

  // Round 7 R4: match PasteForm symmetry — let the user reset without
  // switching tabs after a completed/failed upload.
  const onClear = () => {
    setTitle("");
    setDomain("demo.local");
    setResult(null);
    setPreviewName(null);
    m.reset();
  };

  const dirty = !!previewName || !!result || !!title || domain !== "demo.local" || m.isError;

  return (
    <>
      <div className="card grid gap-3">
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1">
            <label htmlFor="up-title" className="label">title (optional — uses first heading otherwise)</label>
            <input
              id="up-title"
              className="input"
              placeholder="defaults to first heading or first sentence"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div className="grid gap-1">
            <label htmlFor="up-domain" className="label">domain</label>
            <input
              id="up-domain"
              className="input"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              autoComplete="off"
            />
          </div>
        </div>
        <DropZone
          disabled={m.isPending}
          onFile={(file) => {
            setPreviewName(file.name);
            m.mutate(file);
          }}
        />
        {previewName && (
          <div className="text-xs text-[color:var(--fg-dim)]">
            file <code className="font-mono">{previewName}</code> {m.isPending && "· uploading…"}
          </div>
        )}
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn"
            disabled={m.isPending || !dirty}
            onClick={onClear}
            data-testid="upload-clear"
          >
            clear
          </button>
        </div>
        {m.isError && (
          <ErrorBox err={m.error instanceof ApiError ? m.error.message : String(m.error)} />
        )}
      </div>
      {m.isPending && <Skeleton className="h-40" />}
      {result && <AnalysisSummary result={result} />}
    </>
  );
}

function ReplayForm() {
  const [maxRecords, setMaxRecords] = useState(50);
  const [result, setResult] = useState<ReplayRunResponse | null>(null);

  const m = useMutation({
    mutationFn: () => api.replay(maxRecords),
    onSuccess: (r) => setResult(r),
  });

  // The endpoint signature is `int = Body(50, embed=True)`, but FastAPI
  // accepts any positive integer. We clamp client-side to keep the UI
  // honest about what we'll ask for — a 0 or negative value would no-op.
  const ready = Number.isFinite(maxRecords) && maxRecords > 0;

  return (
    <>
      <form
        className="card grid gap-3"
        onSubmit={(e) => { e.preventDefault(); if (ready) m.mutate(); }}
        aria-busy={m.isPending}
      >
        <p className="text-xs text-[color:var(--fg-dim)]">
          Runs one pass of the supervisor over the configured Awareness JSONL directory.
          Useful for replaying recently-committed captures without restarting the sidecar.
          The supervisor is idempotent — already-processed capture IDs are skipped.
        </p>
        <div className="grid gap-1 max-w-xs">
          <label htmlFor="replay-max" className="label">max records to scan</label>
          <input
            id="replay-max"
            className="input"
            type="number"
            min={1}
            max={5000}
            step={1}
            value={maxRecords}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (Number.isFinite(n)) setMaxRecords(Math.max(1, Math.min(5000, Math.floor(n))));
            }}
            data-testid="replay-max-input"
          />
          <div className="text-[10px] text-[color:var(--fg-dim)]">1–5000 (default 50)</div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="submit"
            className="btn btn-accent"
            disabled={!ready || m.isPending}
            data-testid="replay-run"
          >
            {m.isPending ? "replaying…" : "Run replay"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={m.isPending || (!result && !m.isError)}
            onClick={() => { setResult(null); m.reset(); }}
          >
            clear
          </button>
          {m.isError && (
            <span className="text-xs text-bad" role="alert">
              {m.error instanceof ApiError ? m.error.message : String(m.error)}
            </span>
          )}
        </div>
      </form>
      {m.isPending && <Skeleton className="h-20" />}
      {result && (
        <article className="card" data-testid="replay-result">
          <header className="flex items-baseline justify-between flex-wrap gap-2 mb-3">
            <h2 className="text-base font-semibold">Replay result</h2>
            <span className="text-[10px] text-[color:var(--fg-dim)]">max scanned: {maxRecords.toLocaleString()}</span>
          </header>
          <div className="grid gap-3 grid-cols-2 md:grid-cols-3">
            <div className="card">
              <div className="label">processed</div>
              <div className="mt-1 text-xl font-semibold tabular-nums text-good" data-testid="replay-processed">
                {result.processed.toLocaleString()}
              </div>
            </div>
            <div className="card">
              <div className="label">skipped</div>
              <div className="mt-1 text-xl font-semibold tabular-nums" data-testid="replay-skipped">
                {result.skipped.toLocaleString()}
              </div>
            </div>
            <div className="card">
              <div className="label">net new</div>
              <div className="mt-1 text-xl font-semibold tabular-nums">
                {(result.processed - result.skipped).toLocaleString()}
              </div>
            </div>
          </div>
          <p className="mt-3 text-[10px] text-[color:var(--fg-muted)]">
            Newly-materialized records appear in the Live Feed automatically. If <code className="font-mono">processed</code> is
            zero, either the awareness data directory is empty or all captures up to <code className="font-mono">max</code> were
            already in storage.
          </p>
        </article>
      )}
    </>
  );
}

function HelpCard({ tab }: { tab: Tab }) {
  return (
    <aside className="card text-xs" data-testid="help-card">
      {tab === "replay" ? (
        <>
          <h3 className="label mb-2">What does Replay do?</h3>
          <ol className="grid gap-2 list-decimal pl-4 text-[color:var(--fg-dim)]">
            <li>Reads up to <code className="font-mono">max</code> rows from the configured Awareness JSONL directory.</li>
            <li>Routes each capture through the same supervisor used by the live tail.</li>
            <li>Skipped rows already exist in storage — replay is idempotent.</li>
            <li>Production-safe stays in effect: diagnostic fields stay pinned to <code className="font-mono">false</code>/<code className="font-mono">null</code>.</li>
          </ol>
        </>
      ) : (
        <>
          <h3 className="label mb-2">What happens to your article?</h3>
          <ol className="grid gap-2 list-decimal pl-4 text-[color:var(--fg-dim)]">
            <li>The text is written to a local Awareness-style JSONL row in <code className="font-mono">data/demo-input/</code>.</li>
            <li>The same replay pipeline used by the live feed processes it.</li>
            <li>You see the materialized <code className="font-mono">FinancialImpactRecord</code> with multi-label asset classes, reason codes, symbols, sentiment, and extractive evidence.</li>
            <li>Production-safe mode: every diagnostic field is pinned to <code className="font-mono">false</code> / <code className="font-mono">null</code> before reaching this page.</li>
          </ol>
        </>
      )}
      <hr className="my-3 border-[color:var(--border-subtle)]" />
      <p className="text-[color:var(--fg-dim)]">
        Articles never leave your machine. No external services are contacted.
      </p>
    </aside>
  );
}
