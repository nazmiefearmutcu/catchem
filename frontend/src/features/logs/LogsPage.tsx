import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";

/**
 * Dedicated live sidecar log viewer at /logs.
 *
 * The Model Controls page used to carry a passive 200-line pre-formatted
 * tail with no controls. This page promotes that surface to a first-class
 * dashboard:
 *
 *   - Premium hero with animated ping dot + 4 KPI tiles
 *     (total / errors / warnings / lines/min).
 *   - Toolbar with level filter, case-insensitive search, auto-scroll
 *     toggle, pause toggle, and copy-to-clipboard.
 *   - Color-coded log lines (info gray / warn yellow / error red).
 *   - "Jump to bottom" affordance when auto-scroll is off and the user
 *     has scrolled away from the latest line.
 *   - Empty state with a Replay CTA so a fresh-install analyst always
 *     has somewhere to click instead of staring at a blank pane.
 *
 * Refetch cadence is 3s (faster than the 6s the old ModelControls tail
 * used — this page is dedicated and the user is presumed to want fresh
 * data). When `paused` is true we set `refetchInterval: false` so the
 * polling actually stops at the network layer; no work runs in the
 * background while the analyst inspects a specific block.
 */

type Level = "info" | "warn" | "error" | "other";

/**
 * Best-effort log-line classifier. Catches the two shapes we see most
 * in catchem sidecar output:
 *
 *   1. `INFO:  uvicorn ...`, `WARNING: ...`, `ERROR: ...` (Python
 *      logging.basicConfig default + uvicorn).
 *   2. `level=info msg=...`, `level=warning msg=...` (structlog-ish
 *      key=value lines we emit from the news poller).
 *
 * Anything else falls through to `other` and renders in the muted-fg
 * tone so the eye doesn't grade noise as warnings.
 */
export function classifyLine(line: string): Level {
  const lower = line.toLowerCase();
  // structlog-style key=value form, checked first because the prefix
  // form (INFO:) can incidentally appear inside a longer log message.
  if (/(^|\s)level=err(or)?(\s|$)/.test(lower)) return "error";
  if (/(^|\s)level=warn(ing)?(\s|$)/.test(lower)) return "warn";
  if (/(^|\s)level=info(\s|$)/.test(lower)) return "info";
  // Python / uvicorn default form. Uses prefixes anchored at line start
  // OR following a timestamp+space so "INFO" inside a URL doesn't trip.
  if (/^(?:\S+\s+)?error[:\s]/i.test(line)) return "error";
  if (/^(?:\S+\s+)?(warn(?:ing)?)[:\s]/i.test(line)) return "warn";
  if (/^(?:\S+\s+)?info[:\s]/i.test(line)) return "info";
  // Critical/fatal are upgraded to error for the KPI tile.
  if (/(^|\s)(critical|fatal)[:\s]/i.test(line)) return "error";
  return "other";
}

function levelToneClass(level: Level): string {
  switch (level) {
    case "error":
      return "text-bad";
    case "warn":
      return "text-warn";
    case "info":
      return "text-[color:var(--fg)]";
    case "other":
    default:
      return "text-[color:var(--fg-dim)]";
  }
}

/**
 * Pure rate calculator — exported so the test can pin behaviour without
 * mounting the whole page. Counts new lines since the previous tick and
 * divides by elapsed minutes. Returns 0 (not NaN) when the elapsed
 * window is too small to be meaningful (< 1s), so the KPI tile never
 * flashes "Infinity/min" on the first tick.
 */
export function deriveLinesPerMinute(
  prevCount: number,
  prevTimestampMs: number,
  nowCount: number,
  nowTimestampMs: number,
): number {
  if (nowTimestampMs <= prevTimestampMs) return 0;
  const elapsedMs = nowTimestampMs - prevTimestampMs;
  if (elapsedMs < 1000) return 0;
  const delta = nowCount - prevCount;
  if (delta <= 0) return 0;
  return (delta / elapsedMs) * 60_000;
}

type FilterValue = "all" | "info" | "warn" | "error";

export function LogsPage() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<FilterValue>("all");
  const [search, setSearch] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);

  // Rate state — when paused, the rate freezes (no new samples logged).
  // `ts` MUST start at 0 (not Date.now()) so the first-sample guard in the
  // rate effect fires once and records the baseline WITHOUT surfacing a
  // rate. Seeding it with a real timestamp made that guard dead code: the
  // very first logTail(1000) response was then diffed against count 0, so
  // the entire initial buffer was counted as "new lines since mount" and
  // the rate KPI flashed a wildly inflated value on slow first fetches.
  const rateRef = useRef<{ count: number; ts: number; rate: number }>({
    count: 0,
    ts: 0,
    rate: 0,
  });

  const logs = useQuery({
    queryKey: ["logs-page-tail"],
    queryFn: () => api.logTail(1000),
    refetchInterval: paused ? false : 3_000,
  });

  const scrollRef = useRef<HTMLPreElement | null>(null);

  // ── Update rate sample on each fetch (skip when paused so a long
  // inspection doesn't dilute the rate to zero artificially). ──────
  useEffect(() => {
    if (paused) return;
    // Wait for a real fetch before sampling. The effect also runs on mount
    // while logs.data is still undefined; sampling then would burn the
    // first-sample exemption against an empty buffer, so the FIRST real
    // tail would be diffed against count 0 and the whole initial buffer
    // counted as new throughput.
    if (!logs.data) return;
    const lines = logs.data.lines ?? [];
    const now = Date.now();
    const next = deriveLinesPerMinute(
      rateRef.current.count,
      rateRef.current.ts,
      lines.length,
      now,
    );
    // First sample: just record without surfacing a rate (we don't know
    // what "lines/min" means until we have two readings).
    if (rateRef.current.ts === 0) {
      rateRef.current = { count: lines.length, ts: now, rate: 0 };
      return;
    }
    rateRef.current = { count: lines.length, ts: now, rate: next };
  }, [logs.data, paused]);

  const lines = logs.data?.lines ?? [];
  const truncated = logs.data?.truncated ?? false;

  // Pre-classify so the KPI tiles + filter share the same labels.
  const classified = useMemo(
    () => lines.map((line) => ({ line, level: classifyLine(line) })),
    [lines],
  );

  const totalLines = classified.length;
  const errorCount = useMemo(
    () => classified.filter((c) => c.level === "error").length,
    [classified],
  );
  const warnCount = useMemo(
    () => classified.filter((c) => c.level === "warn").length,
    [classified],
  );
  const linesPerMin = rateRef.current.rate;

  // ── Apply level filter + search on top of the classified set. ──
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return classified.filter(({ line, level }) => {
      if (filter !== "all" && level !== filter) return false;
      if (needle && !line.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [classified, filter, search]);

  // ── Auto-scroll to bottom on new lines (when enabled). ────────
  useEffect(() => {
    if (!autoScroll) return;
    const el = scrollRef.current;
    if (!el) return;
    // Defer to next frame so layout has settled with the new content
    // before we measure scrollHeight.
    const id = window.requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => window.cancelAnimationFrame(id);
  }, [autoScroll, filtered.length]);

  // ── "↓ to bottom" affordance: surface when auto-scroll is off
  // AND the user has scrolled away from the latest line. ───────
  useEffect(() => {
    if (autoScroll) {
      setShowJumpToBottom(false);
      return;
    }
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distanceFromBottom =
        el.scrollHeight - el.scrollTop - el.clientHeight;
      // 24px slack — micro-overflow shouldn't trigger the chip.
      setShowJumpToBottom(distanceFromBottom > 24);
    };
    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [autoScroll, filtered.length]);

  const copyVisible = async () => {
    const text = filtered.map((f) => f.line).join("\n");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard API can be blocked (iframe / insecure origin). Fall
      // back to a transient textarea so the user still gets the lines.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* ignore — both paths exhausted */
      }
      document.body.removeChild(ta);
    }
  };

  // Hero tone follows the loudest signal: errors > warnings > nominal.
  // Pause/down sidecar isn't tracked here because /logs is dedicated to
  // the log surface itself; sidecar health is on Model Controls.
  const heroTone: "good" | "warn" | "bad" =
    errorCount > 0 ? "bad" : warnCount > 0 ? "warn" : "good";
  const heroAccent =
    heroTone === "good"
      ? "border-good/40 from-good/15"
      : heroTone === "warn"
        ? "border-warn/40 from-warn/15"
        : "border-bad/40 from-bad/15";
  const dotAccent =
    heroTone === "good" ? "bg-good" : heroTone === "warn" ? "bg-warn" : "bg-bad";
  const eyebrowAccent =
    heroTone === "good" ? "text-good" : heroTone === "warn" ? "text-warn" : "text-bad";

  const headlineCount = totalLines === 1 ? "1 line" : `${totalLines} lines`;

  if (logs.isLoading) return <Skeleton className="h-72" />;
  if (logs.error) return <ErrorBox err={logs.error} />;

  return (
    <div className="grid gap-5" data-testid="logs-page">
      {/* Hero: synthesized log status + 4 KPI tiles. */}
      <section
        className={`relative overflow-hidden rounded-xl border ${heroAccent} bg-gradient-to-br via-[color:var(--bg-elev)] to-[color:var(--bg-elev)] p-6`}
      >
        <div
          aria-hidden
          className={`pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full ${
            heroTone === "good"
              ? "bg-good/20"
              : heroTone === "warn"
                ? "bg-warn/20"
                : "bg-bad/20"
          } blur-3xl`}
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              {!paused && (
                <span
                  className={`absolute inline-flex h-full w-full animate-ping rounded-full ${dotAccent} opacity-75`}
                />
              )}
              <span
                className={`relative inline-flex h-2 w-2 rounded-full ${dotAccent}`}
              />
            </span>
            <div>
              <div
                className={`text-[10px] uppercase tracking-[0.25em] ${eyebrowAccent} font-semibold`}
              >
                LOGS · sidecar tail
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                Live log stream · {headlineCount}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {paused ? "polling paused" : "polling every 3s"}
                {truncated && " · truncated to the last 1000 lines"}
              </div>
            </div>
          </div>
          <button
            className="btn shrink-0"
            onClick={() => {
              qc.invalidateQueries({ queryKey: ["logs-page-tail"] });
            }}
          >
            refresh
          </button>
        </div>
        <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <LogsStat label="total" value={totalLines.toLocaleString()} hint="visible in tail" />
          <LogsStat
            label="errors"
            value={errorCount.toLocaleString()}
            hint={errorCount > 0 ? "investigate" : "all clear"}
            tone={errorCount > 0 ? "bad" : "good"}
          />
          <LogsStat
            label="warnings"
            value={warnCount.toLocaleString()}
            hint={warnCount > 0 ? "review" : "all clear"}
            tone={warnCount > 0 ? "warn" : "good"}
          />
          <LogsStat
            label="rate"
            value={
              linesPerMin > 0
                ? `${linesPerMin.toFixed(1)}/min`
                : paused
                  ? "paused"
                  : "—"
            }
            hint={paused ? "polling paused" : "lines per minute"}
          />
        </div>
      </section>

      {/* Toolbar */}
      <section className="card">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            level
          </label>
          <select
            aria-label="Log level filter"
            data-testid="logs-filter-select"
            value={filter}
            onChange={(e) => setFilter(e.target.value as FilterValue)}
            className="rounded border border-[color:var(--border)] bg-[color:var(--bg-elev2)] px-2 py-1 text-xs"
          >
            <option value="all">All</option>
            <option value="info">Info</option>
            <option value="warn">Warn</option>
            <option value="error">Error</option>
          </select>
          <input
            type="search"
            aria-label="Search log lines"
            data-testid="logs-search-input"
            placeholder="search lines…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="rounded border border-[color:var(--border)] bg-[color:var(--bg-elev2)] px-2 py-1 text-xs min-w-[160px] flex-1 max-w-md"
          />
          <label className="flex items-center gap-1 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              data-testid="logs-autoscroll-toggle"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
            />
            <span>auto-scroll</span>
          </label>
          <label className="flex items-center gap-1 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              data-testid="logs-pause-toggle"
              checked={paused}
              onChange={(e) => setPaused(e.target.checked)}
            />
            <span>pause</span>
          </label>
          <button
            type="button"
            className="btn ml-auto"
            data-testid="logs-copy-button"
            onClick={copyVisible}
            disabled={filtered.length === 0}
            title={
              filtered.length === 0
                ? "no lines to copy"
                : `copy ${filtered.length} line${filtered.length === 1 ? "" : "s"}`
            }
          >
            copy {filtered.length || ""}
          </button>
        </div>
      </section>

      {/* Log viewer */}
      <section className="card relative">
        <h2 className="label mb-2">
          tail{" "}
          <span className="text-[color:var(--fg-muted)] font-normal">
            ({filtered.length} of {totalLines}
            {totalLines > 0 ? `, ${Math.round((filtered.length / totalLines) * 100)}%` : ""})
          </span>
        </h2>
        {totalLines === 0 ? (
          <EmptyState
            title="no log lines yet"
            hint="The sidecar hasn't written anything yet. Triggering a replay or poll-news will produce activity."
            action={
              <Link to="/feed" className="btn" data-testid="logs-empty-cta">
                Trigger log activity
              </Link>
            }
          />
        ) : filtered.length === 0 ? (
          <EmptyState
            title="no lines match"
            hint={
              filter !== "all" && search
                ? `Level "${filter}" + search "${search}" filtered every line.`
                : filter !== "all"
                  ? `No lines at level "${filter}". Try All.`
                  : `Search "${search}" matched no lines.`
            }
            action={
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setFilter("all");
                  setSearch("");
                }}
              >
                Clear filters
              </button>
            }
          />
        ) : (
          <>
            <pre
              ref={scrollRef}
              data-testid="logs-viewer"
              className="max-h-[60vh] overflow-y-auto text-[10px] leading-relaxed bg-[color:var(--bg-elev2)] rounded p-2 font-mono"
            >
              {filtered.map(({ line, level }, idx) => (
                <div
                  key={idx}
                  data-level={level}
                  className={levelToneClass(level)}
                >
                  {line}
                </div>
              ))}
            </pre>
            {showJumpToBottom && (
              <button
                type="button"
                data-testid="logs-jump-to-bottom"
                className="absolute right-4 bottom-4 btn shadow-soft"
                onClick={() => {
                  const el = scrollRef.current;
                  if (el) el.scrollTop = el.scrollHeight;
                }}
              >
                ↓ to bottom
              </button>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function LogsStat({
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
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && (
        <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>
      )}
    </div>
  );
}
