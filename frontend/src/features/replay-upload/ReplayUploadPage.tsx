import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError, fmtRel } from "@/lib/api";
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

const TAB_SUBTITLE: Record<Tab, string> = {
  paste: "Drop article text straight into the demo pipeline.",
  upload: "Send a .txt/.md/.html/.jsonl file from disk.",
  replay: "Re-run the supervisor against committed JSONL captures.",
};

const SAMPLE_ARTICLE = {
  title: "Federal Reserve raises rates by 25 basis points to combat inflation",
  text:
    "The Federal Reserve raised its benchmark interest rate by a quarter percentage point on " +
    "Wednesday, pushing the federal funds rate target to a range of 5.25% to 5.50% — the " +
    "highest level in 22 years. Chair Jerome Powell signaled the central bank remains " +
    "committed to bringing inflation back toward its 2% target, but stopped short of " +
    "committing to additional hikes this year. Markets initially rallied on the dovish tone, " +
    "with the S&P 500 closing up 0.6% and the 10-year Treasury yield falling four basis " +
    "points. Bank stocks led gains in the financial sector, while regional lenders such as " +
    "PacWest and Western Alliance jumped more than 3%. Goldman Sachs analysts now see one " +
    "more hike before year-end, while JPMorgan economists expect rates to plateau at " +
    "current levels.",
  domain: "reuters.com",
  url: "https://reuters.com/fed-raises-rates-25bps",
};

// ───────────────────────────────────────────────────────────────────────────
// Icon set (inline SVG — no extra dep). 16px viewBox, stroke=currentColor.
// ───────────────────────────────────────────────────────────────────────────

const ICON_COMMON = {
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.5,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

function IconPaste(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <rect x="3" y="3" width="10" height="11" rx="1.5" />
      <path d="M5.5 2.5h5l-.5 1.5h-4l-.5-1.5z" />
      <path d="M6 7h4M6 9.5h4M6 12h2.5" />
    </svg>
  );
}

function IconUpload(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <path d="M8 10V2.5M5 5l3-3 3 3" />
      <path d="M3 11v1.5A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5V11" />
    </svg>
  );
}

function IconReplay(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" />
      <path d="M13.5 2v3h-3" />
    </svg>
  );
}

function IconFile(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <path d="M3.5 1.5h6L12.5 4.5v9.5a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5z" />
      <path d="M9.5 1.5v3h3" />
    </svg>
  );
}

function IconCheck(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <path d="M3 8.5l3 3 7-7" />
    </svg>
  );
}

function IconSpark(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg {...ICON_COMMON} {...props}>
      <path d="M8 2v4M8 10v4M2 8h4M10 8h4M4 4l2.5 2.5M9.5 9.5L12 12M4 12l2.5-2.5M9.5 6.5L12 4" />
    </svg>
  );
}

const TAB_ICON: Record<Tab, (p: React.SVGProps<SVGSVGElement>) => JSX.Element> = {
  paste: IconPaste,
  upload: IconUpload,
  replay: IconReplay,
};

// ───────────────────────────────────────────────────────────────────────────

export function ReplayUploadPage() {
  const [tab, setTab] = useState<Tab>("paste");
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <section className="grid gap-3">
        {/* Screen-reader-only page heading — the visible mode strip already
            communicates the active tab; this gives every page exactly one
            <h1> for landmark/outline traversal. */}
        <h1 className="sr-only">Replay and upload</h1>
        <ModeStrip current={tab} onChange={setTab} />
        <p className="-mt-1 pl-1 text-[11px] text-[color:var(--fg-dim)]">
          {TAB_SUBTITLE[tab]}
        </p>
        {tab === "paste" ? <PasteForm /> : tab === "upload" ? <UploadForm /> : <ReplayForm />}
      </section>
      <aside className="grid gap-3">
        <HelpCard tab={tab} />
        <QuickStats />
      </aside>
    </div>
  );
}

// ── Mode strip ─────────────────────────────────────────────────────────────
//
// Each tab's textContent is *exactly* the label (e.g. "Paste article") — the
// icon ships via aria-hidden SVG, the subtitle renders OUTSIDE the tablist.
// The Round-7 R3 regression test pins `tabs.map(t => t.textContent)` against
// the three labels verbatim, so any change here that bleeds extra characters
// into the button breaks that contract.

function ModeStrip({ current, onChange }: { current: Tab; onChange: (t: Tab) => void }) {
  return (
    <div role="tablist" aria-label="Replay/Upload mode" className="grid grid-cols-3 gap-2">
      {(["paste", "upload", "replay"] as const).map((t) => {
        const Icon = TAB_ICON[t];
        const active = t === current;
        return (
          <button
            key={t}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(t)}
            data-testid={`tab-${t}`}
            className={`group flex items-center gap-2 rounded-lg border px-3 py-2.5 text-left transition-colors ${
              active
                ? "border-accent bg-accent/10 text-[color:var(--fg)]"
                : "border-[color:var(--border)] bg-[color:var(--bg-elev)] hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]"
            }`}
          >
            <span
              aria-hidden
              className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${
                active ? "bg-accent text-[#0b1018]" : "bg-[color:var(--bg-elev2)] text-[color:var(--fg-dim)]"
              }`}
            >
              <Icon />
            </span>
            <span className="text-sm font-semibold">{TAB_LABEL[t]}</span>
          </button>
        );
      })}
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function fmtElapsed(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)}m ${(s % 60).toFixed(0)}s`;
}

/** Scroll an element into view once it appears in the DOM. */
function useScrollIntoView(deps: unknown[]) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!deps.some(Boolean)) return;
    const el = ref.current;
    // jsdom (vitest) doesn't implement scrollIntoView; guard so the test
    // commit phase doesn't throw on a benign nicety.
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}

/** Track elapsed milliseconds while `running` is true; resets when it goes false. */
function useElapsed(running: boolean): number {
  const [ms, setMs] = useState(0);
  useEffect(() => {
    if (!running) {
      setMs(0);
      return;
    }
    const start = Date.now();
    setMs(0);
    const id = window.setInterval(() => setMs(Date.now() - start), 200);
    return () => window.clearInterval(id);
  }, [running]);
  return ms;
}

// ── Paste form ──────────────────────────────────────────────────────────────

function PasteForm() {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [domain, setDomain] = useState("demo.local");
  const [url, setUrl] = useState("");
  const [result, setResult] = useState<DemoRunResponse | null>(null);
  const elapsed = useElapsed(false);

  const m = useMutation({
    mutationFn: () => api.demoPaste({ title, text, domain, url: url || undefined }),
    onSuccess: (r) => setResult(r),
  });
  const runElapsed = useElapsed(m.isPending);

  const ready = title.trim().length > 0 && text.trim().length > 0;
  const dirty = title.length > 0 || text.length > 0 || url.length > 0 || domain !== "demo.local" || !!result || m.isError;
  const resultRef = useScrollIntoView([result]);

  const onClear = () => {
    setTitle("");
    setText("");
    setDomain("demo.local");
    setUrl("");
    setUrl("");
    setResult(null);
    m.reset();
  };

  const onLoadSample = () => {
    setTitle(SAMPLE_ARTICLE.title);
    setText(SAMPLE_ARTICLE.text);
    setDomain(SAMPLE_ARTICLE.domain);
    setUrl(SAMPLE_ARTICLE.url);
    m.reset();
    setResult(null);
  };

  const sizePct = Math.min(100, (text.length / (5 * 1024 * 1024)) * 100);

  return (
    <>
      <form
        className="card grid gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready) m.mutate();
        }}
        aria-busy={m.isPending}
      >
        <div className="grid gap-1">
          <label htmlFor="paste-title" className="label">
            title <span className="text-bad">*</span>
          </label>
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
          <div className="flex items-baseline justify-between">
            <label htmlFor="paste-text" className="label">
              article body <span className="text-bad">*</span>
            </label>
            <span className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
              {text.length.toLocaleString()} chars
            </span>
          </div>
          <textarea
            id="paste-text"
            className="input min-h-[260px] resize-y font-mono text-xs leading-relaxed"
            placeholder="Paste the article body here…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            required
            spellCheck={false}
          />
          <div className="h-1 overflow-hidden rounded-full bg-[color:var(--bg-elev2)]">
            <div
              className="h-full bg-accent transition-[width]"
              style={{ width: `${sizePct}%` }}
              aria-hidden
            />
          </div>
          <div className="text-[10px] text-[color:var(--fg-muted)]">
            max 5 MB · cleared once you click <strong className="text-[color:var(--fg-dim)]">Analyze</strong>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1">
            <label htmlFor="paste-domain" className="label">
              domain
            </label>
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
            <label htmlFor="paste-url" className="label">
              url (optional)
            </label>
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
        <div className="flex flex-wrap items-center gap-2">
          <button type="submit" className="btn btn-accent" disabled={!ready || m.isPending}>
            {m.isPending ? `analyzing… ${fmtElapsed(runElapsed)}` : "Analyze"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={m.isPending || !dirty}
            onClick={onClear}
          >
            clear
          </button>
          <button
            type="button"
            className="btn"
            disabled={m.isPending}
            onClick={onLoadSample}
          >
            <IconSpark className="opacity-80" /> Load sample
          </button>
          {!ready && !m.isPending && (
            <span className="text-[10px] text-[color:var(--fg-muted)]">
              title + body required
            </span>
          )}
          {m.isError && (
            <span className="text-xs text-bad" role="alert">
              {m.error instanceof ApiError ? m.error.message : String(m.error)}
            </span>
          )}
        </div>
      </form>
      {m.isPending && <Skeleton className="h-40" />}
      {result && (
        <div ref={resultRef} aria-label="analysis result region">
          <AnalysisSummary result={result} />
        </div>
      )}
      {/* keep elapsed referenced so dev tools tree-shake notices the hook */}
      <span aria-hidden className="hidden">{elapsed}</span>
    </>
  );
}

// ── Upload form ─────────────────────────────────────────────────────────────

function UploadForm() {
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("demo.local");
  const [result, setResult] = useState<DemoRunResponse | null>(null);
  const [staged, setStaged] = useState<File | null>(null);
  const [preview, setPreview] = useState<{ snippet: string; lines: number; truncated: boolean } | null>(null);

  const m = useMutation({
    mutationFn: (file: File) => api.demoUpload(file, { title: title || undefined, domain }),
    onSuccess: (r) => {
      setResult(r);
      setStaged(null);
      setPreview(null);
    },
  });
  const runElapsed = useElapsed(m.isPending);
  const resultRef = useScrollIntoView([result]);

  const dirty = !!staged || !!result || !!title || domain !== "demo.local" || m.isError;

  const onClear = () => {
    setTitle("");
    setDomain("demo.local");
    setResult(null);
    setStaged(null);
    setPreview(null);
    m.reset();
  };

  const onStage = async (file: File) => {
    setStaged(file);
    m.reset();
    // Read a short head of the file to render a preview snippet without
    // pulling the whole thing into memory.
    try {
      const head = await file.slice(0, 4 * 1024).text();
      const lines = head.split(/\r?\n/);
      const truncated = file.size > 4 * 1024;
      setPreview({
        snippet: head.slice(0, 600),
        lines: lines.length,
        truncated,
      });
    } catch {
      setPreview({ snippet: "(preview unavailable)", lines: 0, truncated: file.size > 0 });
    }
  };

  const onProcess = () => {
    if (staged) m.mutate(staged);
  };

  return (
    <>
      <div className="card grid gap-3">
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1">
            <label htmlFor="up-title" className="label">
              title (optional — uses first heading otherwise)
            </label>
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
            <label htmlFor="up-domain" className="label">
              domain
            </label>
            <input
              id="up-domain"
              className="input"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              autoComplete="off"
            />
          </div>
        </div>

        {!staged && (
          <DropZone
            disabled={m.isPending}
            onFile={onStage}
          />
        )}

        {staged && preview && (
          <div className="rounded-lg border border-accent/30 bg-accent/5 p-3" data-testid="upload-staged">
            <div className="flex items-center gap-2">
              <IconFile className="text-accent" />
              <span className="font-mono text-xs" title={staged.name}>{staged.name}</span>
              <span className="text-[10px] text-[color:var(--fg-dim)]">·</span>
              <span className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
                {fmtBytes(staged.size)} · {preview.lines}{preview.truncated ? "+" : ""} lines
              </span>
              <button
                type="button"
                className="ml-auto btn text-[10px] py-0.5 px-2"
                disabled={m.isPending}
                onClick={() => { setStaged(null); setPreview(null); }}
              >
                discard
              </button>
            </div>
            <pre className="mt-2 max-h-32 overflow-auto rounded bg-[color:var(--bg-elev2)] p-2 text-[10px] leading-relaxed text-[color:var(--fg-dim)]">
              {preview.snippet || "(empty file)"}
            </pre>
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                className="btn btn-accent"
                disabled={m.isPending}
                onClick={onProcess}
                data-testid="upload-process"
              >
                {m.isPending ? `processing… ${fmtElapsed(runElapsed)}` : "Process file"}
              </button>
              <span className="text-[10px] text-[color:var(--fg-muted)]">
                title/domain above apply to this upload
              </span>
            </div>
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
          {staged && !m.isPending && (
            <span className="text-[10px] text-[color:var(--fg-muted)]">
              file staged — click <strong className="text-[color:var(--fg-dim)]">Process file</strong> to send
            </span>
          )}
        </div>

        {m.isError && (
          <ErrorBox err={m.error instanceof ApiError ? m.error.message : String(m.error)} />
        )}
      </div>
      {m.isPending && <Skeleton className="h-40" />}
      {result && (
        <div ref={resultRef} aria-label="analysis result region">
          <AnalysisSummary result={result} />
        </div>
      )}
    </>
  );
}

// ── Replay form ─────────────────────────────────────────────────────────────

function ReplayForm() {
  const [maxRecords, setMaxRecords] = useState(50);
  const [result, setResult] = useState<ReplayRunResponse | null>(null);
  const [lastRunAt, setLastRunAt] = useState<string | null>(null);

  // Live sidecar/storage state so the user knows the "before" picture
  // without having to switch to the Ops tab.
  const status = useQuery({
    queryKey: ["sidecar-status"],
    queryFn: api.sidecarStatus,
    refetchInterval: 4_000,
  });

  const m = useMutation({
    mutationFn: () => api.replay(maxRecords),
    onSuccess: (r) => {
      setResult(r);
      setLastRunAt(new Date().toISOString());
      status.refetch();
    },
  });
  const runElapsed = useElapsed(m.isPending);
  const resultRef = useScrollIntoView([result]);

  // The endpoint signature is `int = Body(50, embed=True)`, but FastAPI
  // accepts any positive integer. We clamp client-side to keep the UI
  // honest about what we'll ask for — a 0 or negative value would no-op.
  const ready = Number.isFinite(maxRecords) && maxRecords > 0;
  const replayNetNew =
    result?.net_new_records ?? (result ? Math.max(0, result.processed - result.skipped) : 0);
  const replayFailed = result?.failed ?? 0;
  const replayDlq = result?.dlq ?? 0;
  const replayDlqDelta = result?.dlq_delta ?? replayFailed;
  const replayInserted = result?.inserted ?? replayNetNew;
  const replayReplaced = result?.replaced ?? 0;
  const replayBeforeTotal = result?.records_before?.total;
  const replayAfterTotal = result?.records_after?.total;

  const totalsRow = useMemo(
    () => [
      { label: "records", value: status.data?.records?.total ?? "—" },
      { label: "relevant", value: status.data?.records?.finance_relevant ?? "—" },
      { label: "DLQ", value: status.data?.dlq ?? "—" },
      {
        label: "last replay",
        value: lastRunAt ? fmtRel(lastRunAt) : "—",
      },
    ],
    [status.data, lastRunAt],
  );

  return (
    <>
      <div className="card grid gap-3">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {totalsRow.map((cell) => (
            <div
              key={cell.label}
              className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2"
            >
              <div className="label">{cell.label}</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums">
                {typeof cell.value === "number" ? cell.value.toLocaleString() : cell.value}
              </div>
            </div>
          ))}
        </div>

        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (ready) m.mutate();
          }}
          aria-busy={m.isPending}
        >
          <p className="text-xs text-[color:var(--fg-dim)]">
            Runs one pass of the supervisor over the configured Awareness JSONL directory.
            Useful for replaying recently-committed captures without restarting the sidecar.
            The supervisor is idempotent — already-processed capture IDs are skipped.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid gap-1">
              <label htmlFor="replay-max" className="label">
                max records to scan
              </label>
              <input
                id="replay-max"
                className="input w-32"
                type="number"
                min={1}
                max={5000}
                step={1}
                value={maxRecords}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  if (Number.isFinite(n))
                    setMaxRecords(Math.max(1, Math.min(5000, Math.floor(n))));
                }}
                data-testid="replay-max-input"
              />
              <div className="text-[10px] text-[color:var(--fg-muted)]">1–5000 (default 50)</div>
            </div>
            <div className="grid gap-1">
              <span className="label">presets</span>
              <div className="flex gap-1">
                {[25, 50, 200, 1000].map((n) => (
                  <button
                    key={n}
                    type="button"
                    className={`chip text-[10px] ${maxRecords === n ? "chip-active" : ""}`}
                    onClick={() => setMaxRecords(n)}
                    disabled={m.isPending}
                  >
                    {n}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="submit"
              className="btn btn-accent"
              disabled={!ready || m.isPending}
              data-testid="replay-run"
            >
              {m.isPending ? `replaying… ${fmtElapsed(runElapsed)}` : "Run replay"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={m.isPending || (!result && !m.isError)}
              onClick={() => {
                setResult(null);
                m.reset();
              }}
            >
              clear
            </button>
            {m.isError && (
              <span className="text-xs text-bad" role="alert">
                {m.error instanceof ApiError ? m.error.message : String(m.error)}
              </span>
            )}
          </div>
          {m.isPending && (
            <div className="rounded-md border border-accent/40 bg-accent/10 px-3 py-2">
              <div className="flex items-center justify-between text-[10px] text-[color:var(--fg-dim)]">
                <span>scanning up to {maxRecords.toLocaleString()} records…</span>
                <span className="tabular-nums">{fmtElapsed(runElapsed)}</span>
              </div>
              <div className="mt-1 h-1 overflow-hidden rounded-full bg-[color:var(--bg-elev2)]">
                <div className="h-full w-1/3 animate-pulse rounded-full bg-accent" />
              </div>
            </div>
          )}
        </form>
      </div>

      {result && (
        <article ref={resultRef} className="card" data-testid="replay-result">
          <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-base font-semibold">
              <IconCheck className="inline -mt-0.5 text-good" /> Replay complete
            </h2>
            <span className="text-[10px] text-[color:var(--fg-dim)]">
              max scanned {maxRecords.toLocaleString()} ·{" "}
              {lastRunAt ? fmtRel(lastRunAt) : "just now"}
            </span>
          </header>

          <div className="grid gap-3">
            <ResultGroup title="Pass summary" hint="how the replay supervisor saw each row">
              <ResultCell
                label="processed"
                value={result.processed}
                testid="replay-processed"
                tone={result.processed > 0 ? "good" : "neutral"}
              />
              <ResultCell label="skipped" value={result.skipped} testid="replay-skipped" />
              <ResultCell
                label="failed"
                value={replayFailed}
                testid="replay-failed"
                tone={replayFailed > 0 ? "bad" : "neutral"}
              />
              <ResultCell
                label="DLQ"
                testid="replay-dlq"
                tone={replayDlqDelta > 0 ? "bad" : "neutral"}
                render={
                  <>
                    {replayDlq.toLocaleString()}
                    {replayDlqDelta > 0 && (
                      <span className="ml-1 text-xs">+{replayDlqDelta.toLocaleString()}</span>
                    )}
                  </>
                }
              />
            </ResultGroup>

            <ResultGroup title="Storage impact" hint="how the storage tier changed as a result">
              <ResultCell
                label="net new"
                value={replayNetNew}
                testid="replay-net-new"
                tone={replayNetNew > 0 ? "good" : "neutral"}
              />
              <ResultCell label="inserted" value={replayInserted} testid="replay-inserted" />
              <ResultCell label="replaced" value={replayReplaced} testid="replay-replaced" />
              {replayBeforeTotal !== undefined && replayAfterTotal !== undefined && (
                <ResultCell
                  label="total"
                  testid="replay-records-total"
                  render={
                    <span className="tabular-nums">
                      {replayBeforeTotal.toLocaleString()}{" "}
                      <span className="text-[color:var(--fg-dim)]">→</span>{" "}
                      {replayAfterTotal.toLocaleString()}
                    </span>
                  }
                />
              )}
            </ResultGroup>
          </div>

          <p className="mt-3 text-[10px] text-[color:var(--fg-muted)]">
            Newly-inserted records appear in the Live Feed automatically.{" "}
            <code className="font-mono">net_new_records</code> comes from storage before/after
            totals; <code className="font-mono">processed</code>,{" "}
            <code className="font-mono">skipped</code>, and{" "}
            <code className="font-mono">failed</code> describe the replay pass itself.
          </p>
        </article>
      )}
    </>
  );
}

function ResultGroup({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-[color:var(--fg-dim)]">
          {title}
        </h3>
        {hint && <span className="text-[10px] text-[color:var(--fg-muted)]">{hint}</span>}
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">{children}</div>
    </section>
  );
}

type Tone = "good" | "bad" | "neutral";

function ResultCell({
  label,
  value,
  testid,
  tone = "neutral",
  render,
}: {
  label: string;
  value?: number;
  testid: string;
  tone?: Tone;
  render?: React.ReactNode;
}) {
  const toneCls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="label">{label}</div>
      <div
        className={`mt-0.5 text-xl font-semibold tabular-nums ${toneCls}`}
        data-testid={testid}
      >
        {render ?? (value !== undefined ? value.toLocaleString() : "—")}
      </div>
    </div>
  );
}

// ── Help card ───────────────────────────────────────────────────────────────

function HelpCard({ tab }: { tab: Tab }) {
  return (
    <aside className="card text-xs" data-testid="help-card">
      {tab === "replay" ? (
        <>
          <h3 className="label mb-2">What does Replay do?</h3>
          <ol className="grid gap-2 list-decimal pl-4 text-[color:var(--fg-dim)]">
            <li>
              Reads up to <code className="font-mono">max</code> rows from the configured
              Awareness JSONL directory.
            </li>
            <li>Routes each capture through the same supervisor used by the live tail.</li>
            <li>Skipped rows already exist in storage — replay is idempotent.</li>
            <li>
              Production-safe stays in effect: diagnostic fields stay pinned to{" "}
              <code className="font-mono">false</code>/<code className="font-mono">null</code>.
            </li>
          </ol>
        </>
      ) : (
        <>
          <h3 className="label mb-2">What happens to your article?</h3>
          <ol className="grid gap-2 list-decimal pl-4 text-[color:var(--fg-dim)]">
            <li>
              The text is written to a local Awareness-style JSONL row in{" "}
              <code className="font-mono">data/demo-input/</code>.
            </li>
            <li>The same replay pipeline used by the live feed processes it.</li>
            <li>
              You see the materialized <code className="font-mono">FinancialImpactRecord</code>{" "}
              with multi-label asset classes, reason codes, symbols, sentiment, and extractive
              evidence.
            </li>
            <li>
              Production-safe mode: every diagnostic field is pinned to{" "}
              <code className="font-mono">false</code> / <code className="font-mono">null</code>{" "}
              before reaching this page.
            </li>
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

function QuickStats() {
  const status = useQuery({
    queryKey: ["sidecar-status"],
    queryFn: api.sidecarStatus,
    refetchInterval: 4_000,
  });
  const data = status.data;
  return (
    <aside className="card grid gap-2 text-xs">
      <div className="flex items-baseline justify-between">
        <h3 className="label">storage state</h3>
        {data?.diagnostic_enabled === false && (
          <span className="text-[10px] text-good">production-safe</span>
        )}
      </div>
      {!data?.records ? (
        <Skeleton className="h-10" />
      ) : (
        <dl className="grid grid-cols-2 gap-1 text-[10px]">
          <dt className="text-[color:var(--fg-muted)]">total</dt>
          <dd className="text-right font-mono tabular-nums">
            {data.records.total.toLocaleString()}
          </dd>
          <dt className="text-[color:var(--fg-muted)]">finance relevant</dt>
          <dd className="text-right font-mono tabular-nums">
            {data.records.finance_relevant.toLocaleString()}
          </dd>
          <dt className="text-[color:var(--fg-muted)]">DLQ</dt>
          <dd className="text-right font-mono tabular-nums">
            {(data.dlq ?? 0).toLocaleString()}
          </dd>
          <dt className="text-[color:var(--fg-muted)]">uptime</dt>
          <dd className="text-right font-mono tabular-nums">
            {Math.floor((data.uptime_seconds ?? 0) / 60).toLocaleString()}m
          </dd>
        </dl>
      )}
    </aside>
  );
}
