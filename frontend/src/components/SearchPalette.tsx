import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, type SearchResponse } from "@/lib/api";
import { buildSymbolRoute } from "@/lib/symbolNavigation";
import { loadSaved, saveSearch, removeSaved } from "@/lib/searchSaved";
import { Icon } from "@/components/Icon";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Global content-search palette. Distinct from CommandPalette (⌘K) which
 * is nav + imperative actions. This one searches actual *content*:
 *   - Recent records by title or domain
 *   - Top ticker symbols by mention frequency
 *   - Active quant clusters by id prefix or dominant-symbol membership
 *
 * Keybinding contract:
 *   - ⌘P (Mac) / Ctrl+P opens. preventDefault stops the browser's native
 *     Print dialog.
 *   - Esc closes.
 *   - ↑/↓ moves selection; Enter activates.
 *   - ⌘S (Mac) / Ctrl+S saves the current query (v33, task #126).
 *   - Backspace on a focused saved-query row removes it.
 *
 * UX:
 *   - 200ms debounce. We do NOT issue a request for every keystroke; the
 *     backend scan is in-memory and fast, but the debounce keeps the
 *     UI from re-rendering 5+ times per word.
 *   - Empty query → no fetch; show the SAVED list if any, else the hint.
 *   - q.trim().length < 2 → no fetch (matches backend min_length).
 *   - Matched substring is wrapped in <mark> for visual highlight.
 */

/** Open-event so tests / power users can pop the palette without keyboard. */
export const OPEN_SEARCH_PALETTE_EVENT = "catchem:open-search-palette";

const DEBOUNCE_MS = 200;
const MIN_QUERY = 2;
/** ms — fade-out duration before a saved row is dropped from the list. */
const REMOVE_FADE_MS = 200;

// ── Match highlighting helpers ─────────────────────────────────────────
// Escape user input before building a RegExp. Without this, a query of
// "(" or "$" would throw or — worse — silently match the wrong thing.
export function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Split a label into alternating non-match / match segments around a
 * case-insensitive substring. The component renders these into spans +
 * <mark> elements without dangerously setting innerHTML.
 */
export interface MatchSegment {
  text: string;
  match: boolean;
}

export function splitMatches(text: string, query: string): MatchSegment[] {
  if (!text) return [];
  const q = query.trim();
  if (q.length < 1) return [{ text, match: false }];
  const re = new RegExp(`(${escapeRegExp(q)})`, "ig");
  const parts = text.split(re);
  return parts
    .filter((p) => p !== "")
    .map((p) => ({ text: p, match: p.toLowerCase() === q.toLowerCase() }));
}

function Highlight({ text, query }: { text: string | null | undefined; query: string }) {
  if (!text) return null;
  const segs = splitMatches(text, query);
  return (
    <>
      {segs.map((s, i) =>
        s.match ? (
          <mark key={i} className="bg-accent/30 text-[color:var(--fg)] rounded px-0.5">
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}

// ── Row union (mirrors API result + a leading kind tag) ────────────────
type Row =
  | { kind: "record"; capture_id: string; title: string; domain: string | null; published_ts: string | null }
  | { kind: "symbol"; symbol: string; count: number }
  | { kind: "cluster"; cluster_id: string; size: number; symbols: string[] };

/**
 * Flatten the API response into a single keyboard-navigable list. We
 * keep the three buckets ordered (records → symbols → clusters) so the
 * UI can section them with headers while sharing one selected-index
 * cursor.
 */
export function flattenResponse(resp: SearchResponse | null): Row[] {
  if (!resp) return [];
  const out: Row[] = [];
  for (const r of resp.records) {
    out.push({
      kind: "record",
      capture_id: r.capture_id,
      title: r.title || `(untitled ${r.capture_id.slice(0, 8)})`,
      domain: r.domain,
      published_ts: r.published_ts,
    });
  }
  for (const s of resp.symbols) {
    out.push({ kind: "symbol", symbol: s.symbol, count: s.count });
  }
  for (const c of resp.clusters) {
    out.push({ kind: "cluster", cluster_id: c.cluster_id, size: c.size, symbols: c.symbols });
  }
  return out;
}

export function SearchPalette() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [debounced, setDebounced] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  // v33: saved queries store (mirrored from localStorage). We keep a
  // local copy so the UI can re-render on save / remove without forcing
  // every consumer to subscribe to the storage event.
  const [saved, setSaved] = useState<string[]>([]);
  // v33: ids being faded out so the remove animation can play before
  // the row is actually dropped from `saved`.
  const [removing, setRemoving] = useState<Set<string>>(new Set());
  // v33: brief toast-style confirmation when a save lands.
  const [justSaved, setJustSaved] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const reqIdRef = useRef(0); // monotonically increasing — drop stale responses
  const nav = useNavigate();
  const location = useLocation();
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const shouldRestoreFocus = useRef(false);
  const openRef = useRef(open);
  useOverlaySurface({
    id: "search-palette",
    open,
    onClose: () => setOpen(false),
    lockBody: true,
  });
  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // ── Global keyboard handler: ⌘P / Ctrl+P toggles. ───────────────────
  useEffect(() => {
    const closeAndOpen = () => {
      closeAllOverlays();
      shouldRestoreFocus.current = true;
      lastFocusedRef.current = (document.activeElement as HTMLElement | null) ?? null;
      setOpen(true);
    };

    const isTypingContext = (target: EventTarget | null) => {
      if (!target) return false;
      if (target instanceof HTMLElement) {
        return /^(input|textarea|select)$/i.test(target.tagName) || target.isContentEditable;
      }
      return false;
    };

    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "p") {
        if (isTypingContext(e.target)) return;
        if (!openRef.current) {
          closeAndOpen();
          e.preventDefault();
          return;
        }
        shouldRestoreFocus.current = true;
        // ⌘P collides with browser Print — preventDefault always, even
        // if we're closing the palette, so the dialog never sneaks in.
        e.preventDefault();
        setOpen(false);
      }
    };
    const openEvent = () => {
      if (openRef.current) return;
      closeAndOpen();
    };
    document.addEventListener("keydown", handler);
    window.addEventListener(OPEN_SEARCH_PALETTE_EVENT, openEvent);
    return () => {
      document.removeEventListener("keydown", handler);
      window.removeEventListener(OPEN_SEARCH_PALETTE_EVENT, openEvent);
    };
  }, []);

  // Close immediately on route transitions so saved-query context does
  // not leak into a new page.
  useEffect(() => {
    if (open) setOpen(false);
  }, [location.pathname]);

  // Reset on open / re-focus. We *do* reload `saved` here so a save
  // from another window is reflected when the user re-opens the palette.
  useEffect(() => {
    if (open) {
      if (!shouldRestoreFocus.current) shouldRestoreFocus.current = true;
      setInput("");
      setDebounced("");
      setResult(null);
      setError(null);
      setSelected(0);
      setSaved(loadSaved());
      setRemoving(new Set());
      setJustSaved(null);
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
    if (!shouldRestoreFocus.current) return;
    shouldRestoreFocus.current = false;
    const prev = lastFocusedRef.current;
    if (prev && typeof prev.focus === "function") {
      try {
        prev.focus();
      } catch {
        /* ignore */
      }
    }
    return;
  }, [open]);

  // Debounce input → debounced query.
  useEffect(() => {
    if (!open) return;
    const t = window.setTimeout(() => setDebounced(input), DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [input, open]);

  // Fetch on debounced change. Use a monotonic request id so an older
  // in-flight response can't overwrite a newer one (e.g. user types
  // fast → slow network → stale answer arrives last).
  useEffect(() => {
    if (!open) return;
    const q = debounced.trim();
    if (q.length < MIN_QUERY) {
      setResult(null);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    const id = ++reqIdRef.current;
    api
      .search(q, 20)
      .then((r) => {
        if (id !== reqIdRef.current) return;
        setResult(r);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== reqIdRef.current) return;
        setError(err instanceof Error ? err.message : "Search failed");
        setLoading(false);
      });
  }, [debounced, open]);

  // Reset cursor when results change.
  useEffect(() => {
    setSelected(0);
  }, [result]);

  const rows = useMemo(() => flattenResponse(result), [result]);
  const hasQuery = input.trim().length >= MIN_QUERY;
  const showSearching = hasQuery && loading;
  const showEmpty = hasQuery && !loading && rows.length === 0 && !error;
  const hasResults = hasQuery && !loading && rows.length > 0 && !error;
  // Empty-query view shows the SAVED list (when populated) — saved
  // queries participate in keyboard nav so ↑/↓ work the same way they
  // do for result rows. We expose a single "selectables" length so the
  // existing cursor logic doesn't need a parallel state machine.
  const visibleSaved = useMemo(
    () => saved.filter((q) => !removing.has(q)),
    [saved, removing],
  );
  const selectablesCount = hasQuery ? rows.length : visibleSaved.length;

  useEffect(() => {
    if (!open) return;
    setSelected((current) => {
      if (selectablesCount === 0) return 0;
      return Math.min(current, selectablesCount - 1);
    });
  }, [selectablesCount, open]);

  const activate = (row: Row) => {
    if (row.kind === "record") {
      setOpen(false);
      nav(`/feed/${encodeURIComponent(row.capture_id)}`);
    } else if (row.kind === "symbol") {
      const route = buildSymbolRoute(row.symbol);
      if (!route) return;
      setOpen(false);
      nav(route);
    } else {
      // Clusters live inside /scan as an in-page drill — no per-cluster
      // URL today. Route to /scan; the user lands on the events panel.
      setOpen(false);
      nav("/scan");
    }
  };

  /** Activate a saved query: refill the input and let the existing
   *  debounce/fetch effect take it from there. */
  const activateSaved = (query: string) => {
    setInput(query);
    // Skip the 200ms debounce so the user sees results immediately
    // when re-running a saved search; the normal debounce path is for
    // typing, not for explicit one-click invocation.
    setDebounced(query);
    setSelected(0);
    inputRef.current?.focus();
  };

  /** Save the *current* input. The "≥1 result" gate is enforced in the
   *  UI affordance (button hidden, ⌘S guarded). */
  const doSave = () => {
    if (!hasQuery) return;
    const q = input.trim();
    setSaved(saveSearch(q));
    setJustSaved(q);
    // Auto-clear the confirmation chip after a short window.
    window.setTimeout(() => setJustSaved((cur) => (cur === q ? null : cur)), 1400);
  };

  /** Remove a saved query with a 200ms fade. */
  const doRemove = (query: string) => {
    const nextCount = Math.max(0, visibleSaved.length - 1);
    setRemoving((prev) => {
      const next = new Set(prev);
      next.add(query);
      return next;
    });
    window.setTimeout(() => {
      setSaved(removeSaved(query));
      setRemoving((prev) => {
        const next = new Set(prev);
        next.delete(query);
        return next;
      });
      // Keep the cursor in a valid row while the fade-out removal runs.
      // `nextCount` is computed before the delayed drop and reflects the
      // actual post-removal list length.
      setSelected((i) => (nextCount === 0 ? 0 : Math.min(i, nextCount - 1)));
    }, REMOVE_FADE_MS);
  };

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      if (input.trim().length > 0) {
        e.preventDefault();
        e.stopPropagation();
        setInput("");
        setDebounced("");
        setSelected(0);
        return;
      }
    }
    if (e.key === "Home") {
      if (selectablesCount === 0) return;
      e.preventDefault();
      setSelected(0);
      return;
    }
    if (e.key === "End") {
      if (selectablesCount === 0) return;
      e.preventDefault();
      setSelected(selectablesCount - 1);
      return;
    }
    // ⌘S / Ctrl+S — save current query when results are present.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (hasResults) doSave();
      return;
    }
    if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
      if (selectablesCount === 0) return;
      e.preventDefault();
      setSelected((i) => Math.min(selectablesCount - 1, i + 1));
      return;
    }
    if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) {
      if (selectablesCount === 0) return;
      e.preventDefault();
      setSelected((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (hasQuery) {
        const row = rows[selected];
        if (row) {
          activate(row);
          return;
        }
        const route = buildSymbolRoute(input);
        if (route) {
          setOpen(false);
          nav(route);
          return;
        }
        setSelected(0);
      } else {
        const q = visibleSaved[selected];
        if (q) activateSaved(q);
      }
    }
    // Backspace on an empty input + focused saved row → remove that row.
    // Only fire when the input is genuinely empty so the user can still
    // freely delete characters while typing.
    if (e.key === "Backspace" && input === "" && visibleSaved.length > 0) {
      const q = visibleSaved[selected];
      if (q) {
        e.preventDefault();
        doRemove(q);
      }
    }
  };

  // Keep selected row in view.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[data-row-index="${selected}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [selected, open]);

  if (!open) return null;

  // Section boundaries for header insertion — we count to know where to
  // print "Records" / "Symbols" / "Clusters" labels.
  const recordCount = result?.records.length ?? 0;
  const symbolCount = result?.symbols.length ?? 0;
  const clusterCount = result?.clusters.length ?? 0;

  return (
    <div
      data-testid="search-palette"
      className="search-palette fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-24 px-4"
      onClick={() => setOpen(false)}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search palette"
        className="w-full max-w-2xl rounded-lg border border-[color:var(--border)] bg-[color:var(--bg-elev)] shadow-soft animate-modal-enter"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-[color:var(--border)] px-3">
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Search records, symbols, clusters..."
            aria-label="Search query"
            data-testid="search-palette-input"
            className="flex-1 bg-transparent py-3 text-sm outline-none"
          />
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="btn py-1 px-2 text-[10px] leading-none"
            aria-label="Close search palette"
          >
            close
          </button>
        </div>
        <div ref={listRef} className="max-h-[28rem] overflow-auto p-1">
          {/* ── Empty-query view: SAVED queries (v33) ───────────── */}
          {!hasQuery && visibleSaved.length === 0 && (
            <div className="px-3 py-4 text-[11px] text-[color:var(--fg-dim)] text-center">
              Type to search across records, symbols, and clusters.
            </div>
          )}
          {!hasQuery && visibleSaved.length > 0 && (
            <div data-testid="search-palette-saved-section">
              <div
                className="mt-1 px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)] label"
              >
                Saved ({visibleSaved.length})
              </div>
              {visibleSaved.map((q, i) => {
                const isSelected = i === selected;
                const isRemoving = removing.has(q);
                return (
                  <div
                    key={q}
                    data-row-index={i}
                    data-saved-row="1"
                    data-testid="search-palette-saved-row"
                    aria-selected={isSelected}
                    role="option"
                    className={
                      "group flex items-center justify-between gap-2 rounded px-3 py-2 text-sm cursor-pointer transition-opacity transition-colors " +
                      (isSelected
                        ? "bg-[color:var(--bg-elev2)] border border-[color:var(--accent)] "
                        : "border border-transparent hover:bg-[color:var(--bg-elev2)] ") +
                      (isRemoving ? "opacity-0" : "opacity-100")
                    }
                    style={{ transitionDuration: `${REMOVE_FADE_MS}ms` }}
                    onMouseEnter={() => setSelected(i)}
                    onClick={() => activateSaved(q)}
                  >
                    <div className="min-w-0 flex-1 flex items-center gap-2">
                      <span
                        aria-hidden="true"
                        className="inline-flex text-[color:var(--fg-dim)] shrink-0"
                      >
                        <Icon name="save" size={12} />
                      </span>
                      <span className="truncate">{q}</span>
                    </div>
                    <button
                      type="button"
                      aria-label={`Remove saved search "${q}"`}
                      data-testid="search-palette-saved-remove"
                      onClick={(ev) => {
                        ev.stopPropagation();
                        doRemove(q);
                      }}
                      className="shrink-0 inline-flex items-center justify-center text-[color:var(--fg-dim)] hover:text-[color:var(--bad)] rounded px-1 py-0.5 opacity-60 hover:opacity-100 transition-opacity"
                    >
                      <Icon name="close" size={10} />
                    </button>
                  </div>
                );
              })}
              <div className="px-3 py-2 mt-2 text-[10px] text-[color:var(--fg-dim)] border-t border-[color:var(--border)]">
                <span className="kbd">↑↓</span> navigate ·{" "}
                <span className="kbd">Enter</span> run ·{" "}
                <span className="kbd">⌫</span> remove ·{" "}
                <span className="kbd">Esc</span> close
              </div>
            </div>
          )}
          {showSearching && (
            <div
              data-testid="search-palette-loading"
              className="px-3 py-2 text-[11px] text-[color:var(--fg-dim)]"
            >
              Searching…
            </div>
          )}
          {error && (
            <div
              data-testid="search-palette-error"
              className="px-3 py-2 text-[11px] text-[color:var(--bad)]"
            >
              {error}
            </div>
          )}
          {showEmpty && (
            <div
              data-testid="search-palette-empty"
              className="px-3 py-2 text-[11px] text-[color:var(--fg-dim)]"
            >
              No matches.
            </div>
          )}

          {recordCount > 0 && (
            <div className="mt-1 px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
              Records ({recordCount})
            </div>
          )}
          {rows.map((row, i) => {
            const isSelected = i === selected;
            const sectionLabel =
              i === recordCount && symbolCount > 0
                ? `Symbols (${symbolCount})`
                : i === recordCount + symbolCount && clusterCount > 0
                  ? `Clusters (${clusterCount})`
                  : null;
            // Natural keys keep highlight stable when rows re-rank during
            // typing (index-based keys cause React to reuse DOM nodes at
            // positions that now hold different content → flicker).
            const naturalKey =
              row.kind === "record"
                ? row.capture_id
                : row.kind === "symbol"
                  ? `sym-${row.symbol}`
                  : `cluster-${row.cluster_id}`;
            return (
              <div key={naturalKey}>
                {sectionLabel && (
                  <div className="mt-1 px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
                    {sectionLabel}
                  </div>
                )}
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  data-row-index={i}
                  data-row-kind={row.kind}
                  onMouseEnter={() => setSelected(i)}
                  onClick={() => activate(row)}
                  className={
                    "w-full text-left flex items-start justify-between gap-3 rounded px-3 py-2 text-sm cursor-pointer transition-colors " +
                    (isSelected
                      ? "bg-[color:var(--bg-elev2)] border border-[color:var(--accent)]"
                      : "border border-transparent hover:bg-[color:var(--bg-elev2)]")
                  }
                >
                  {row.kind === "record" && (
                    <>
                      <div className="min-w-0 flex-1">
                        <div className="truncate">
                          <Highlight text={row.title} query={debounced} />
                        </div>
                        {row.domain && (
                          <div className="text-[10px] text-[color:var(--fg-dim)] truncate">
                            <Highlight text={row.domain} query={debounced} />
                          </div>
                        )}
                      </div>
                      <span className="kbd shrink-0">record</span>
                    </>
                  )}
                  {row.kind === "symbol" && (
                    <>
                      <div className="min-w-0 flex-1 flex items-baseline gap-2">
                        <span className="font-semibold">
                          <Highlight text={row.symbol} query={debounced} />
                        </span>
                        <span className="text-[10px] text-[color:var(--fg-dim)]">
                          {row.count} mention{row.count === 1 ? "" : "s"}
                        </span>
                      </div>
                      <span className="kbd shrink-0">symbol</span>
                    </>
                  )}
                  {row.kind === "cluster" && (
                    <>
                      <div className="min-w-0 flex-1">
                        <div className="truncate">
                          cluster #
                          <Highlight text={row.cluster_id.slice(0, 8)} query={debounced} />
                        </div>
                        {row.symbols.length > 0 && (
                          <div className="text-[10px] text-[color:var(--fg-dim)] truncate">
                            {row.symbols.slice(0, 6).map((s, j) => (
                              <span key={s}>
                                {j > 0 && ", "}
                                <Highlight text={s} query={debounced} />
                              </span>
                            ))}
                            {row.symbols.length > 6 && ` +${row.symbols.length - 6}`}
                          </div>
                        )}
                      </div>
                      <span className="kbd shrink-0">cluster ({row.size})</span>
                    </>
                  )}
                </button>
              </div>
            );
          })}

          {/* ── Save-this-search affordance (v33) ────────────────── */}
          {hasResults && (
            <div className="px-3 py-2 mt-2 flex items-center justify-between gap-3 border-t border-[color:var(--border)]">
              <span className="text-[10px] text-[color:var(--fg-dim)]">
                <span className="kbd">↑↓</span> navigate ·{" "}
                <span className="kbd">Enter</span> open ·{" "}
                <span className="kbd">⌘S</span> save ·{" "}
                <span className="kbd">Esc</span> close
              </span>
              <button
                type="button"
                onClick={doSave}
                data-testid="search-palette-save-button"
                className="text-[11px] px-2 py-1 rounded border border-[color:var(--border)] hover:bg-[color:var(--bg-elev2)] hover:border-[color:var(--accent)] transition-colors flex items-center gap-1"
              >
                <Icon name="save" size={12} />
                {justSaved === input.trim() ? "Saved!" : "Save this search"}
              </button>
            </div>
          )}
          {/* When no results but query exists, still show the basic
              hint (no save affordance — nothing to save). */}
          {hasQuery && !loading && !error && rows.length === 0 && (
            <div className="px-3 py-2 mt-2 text-[10px] text-[color:var(--fg-dim)] border-t border-[color:var(--border)]">
              <span className="kbd">Esc</span> close
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
