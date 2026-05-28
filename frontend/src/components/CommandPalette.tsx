import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useTheme } from "@/hooks/useTheme";
import { NAV_SHORTCUTS, chordLabel } from "@/lib/nav-shortcuts";
import { api } from "@/lib/api";
import { buildSymbolRoute } from "@/lib/symbolNavigation";
import { resetOnboarding } from "@/lib/onboarding";
import { closeAllOverlays, useOverlaySurface } from "@/context/overlayCoordinator";

/**
 * Palette entries. Routed entries are mirrored verbatim from the
 * canonical NAV_SHORTCUTS table; the legacy dashboard is appended as a
 * non-chord shortcut. The shape (label/path/kbd?) is contract — the
 * navShortcuts regression test imports this array directly and asserts
 * every entry matches the canonical handler.
 */
export const NAV: { label: string; path: string; kbd?: string }[] = [
  ...NAV_SHORTCUTS.map((s) => ({ label: s.label, path: s.path, kbd: chordLabel(s) })),
  { label: "Legacy Dashboard", path: "/legacy" },
];

export const SYMBOL_MENTION_PLACEHOLDER = "Type a page, symbol mention, or command...";
export const symbolMentionEmptyText = (raw: string) =>
  `No matches - press Enter to find symbol mentions for ${raw.trim().toUpperCase()}`;
export const symbolMentionActionLabel = (raw: string) =>
  `Find symbol mentions "${raw.trim().toUpperCase() || "AAPL"}"`;

const RECENT_STORAGE_KEY = "catchem.palette.recent";
const RECENT_CAP = 5;

/**
 * Global event the ShortcutOverlay listens for. Dispatched by the
 * "Show keyboard shortcuts" action so the palette can pop the overlay
 * without owning the overlay's open state.
 */
export const OPEN_SHORTCUT_OVERLAY_EVENT = "catchem:open-shortcut-overlay";
export const OPEN_COMMAND_PALETTE_EVENT = "catchem:open-command-palette";

/**
 * Type-discriminated command. `kind: "nav"` are mirrors of NAV entries
 * (with a kbd hint), `kind: "action"` are imperatives bucketed into a
 * small set of groups so the UI can show a "View", "Data", etc. badge
 * on the right.
 *
 * `aliases` only applies to actions and is folded into fuzzy scoring so
 * a user typing "dark" can find "Toggle theme".
 *
 * `id` is the stable identifier used for the recent-commands store
 * (e.g. `action:toggle-theme`) — labels can change over time but the id
 * stays put so the rail doesn't shuffle on rebrand.
 */
export type ActionGroup = "Settings" | "Data" | "View" | "System";
export type Command =
  | { kind: "nav"; label: string; path: string; kbd?: string }
  | {
      kind: "action";
      id: string;
      label: string;
      group: ActionGroup;
      run: () => void;
      aliases?: string[];
    };

/**
 * Fuzzy match scorer. Requires every char of `query` to appear in `label`
 * in order; returns -1 when there is no in-order match.
 *
 * Score model:
 *   - +100 exact lowercase match
 *   - +50  label starts with the query string
 *   - +20  per char hit that lands on a word boundary (start or post-space)
 *   - +10  per matched char (in-order)
 *   - -1   per skipped char between consecutive matches (gap penalty)
 */
export function fuzzyScore(query: string, label: string): number {
  const q = query.toLowerCase().trim();
  const l = label.toLowerCase();
  if (!q) return 0;
  if (q === l) return 100;
  let score = l.startsWith(q) ? 50 : 0;
  let li = 0;
  let lastHit = -1;
  for (const ch of q) {
    const idx = l.indexOf(ch, li);
    if (idx === -1) return -1;
    score += 10;
    if (idx === 0 || l[idx - 1] === " ") score += 20;
    if (lastHit !== -1) score -= idx - lastHit - 1;
    lastHit = idx;
    li = idx + 1;
  }
  return score;
}

/**
 * Score a query against a label PLUS optional aliases. Returns the best
 * (max) score across all candidates, or -1 if none of them match. Used
 * by the palette so action aliases (e.g. ["dark","light"] on toggle
 * theme) participate in ranking alongside the canonical label.
 */
export function fuzzyScoreWithAliases(
  query: string,
  label: string,
  aliases?: readonly string[],
): number {
  let best = fuzzyScore(query, label);
  if (aliases) {
    for (const a of aliases) {
      const s = fuzzyScore(query, a);
      if (s > best) best = s;
    }
  }
  return best;
}

/**
 * Recent-commands store. v19 wrote raw nav paths (e.g. "/feed"). v23
 * stores tag-prefixed ids so action invocations can also persist
 * (e.g. "action:toggle-theme"). On read, we accept either shape and
 * migrate older entries forward: bare strings that look like a path
 * are promoted to `nav:<path>`. Anything else is dropped.
 *
 * Public helpers (`tagNav`/`tagAction`/`untag`) keep the encoding
 * encapsulated so call-sites never hand-roll the prefix.
 */
export const tagNav = (path: string) => `nav:${path}`;
export const tagAction = (id: string) => `action:${id}`;

export interface UntaggedRecent {
  kind: "nav" | "action";
  value: string;
}

export function untag(raw: string): UntaggedRecent | null {
  if (raw.startsWith("nav:")) return { kind: "nav", value: raw.slice(4) };
  if (raw.startsWith("action:")) return { kind: "action", value: raw.slice(7) };
  // Legacy v19 entries were bare paths starting with "/". Promote them
  // to the new tagged form so the rail keeps working post-upgrade.
  if (raw.startsWith("/")) return { kind: "nav", value: raw };
  return null;
}

/**
 * Read + migrate the persisted recent list. v19 entries (bare paths)
 * are silently rewritten on the next save; we don't eagerly rewrite
 * disk on read so a no-op session doesn't churn storage.
 */
export function loadRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const migrated: string[] = [];
    for (const v of parsed) {
      if (typeof v !== "string") continue;
      const u = untag(v);
      if (!u) continue;
      migrated.push(u.kind === "nav" ? tagNav(u.value) : tagAction(u.value));
      if (migrated.length >= RECENT_CAP) break;
    }
    return migrated;
  } catch {
    return [];
  }
}

export function pushRecent(tag: string, prev: string[]): string[] {
  const next = [tag, ...prev.filter((p) => p !== tag)].slice(0, RECENT_CAP);
  try {
    localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* ignore quota / disabled storage */
  }
  return next;
}

/**
 * Build the action list. Closes over the imperative deps (theme toggle,
 * navigate, query client) so each action stays a 0-arg callable — the
 * palette doesn't have to know what each action needs to do its job.
 *
 * Exported (test seam) so the action wiring can be exercised without
 * mounting the whole palette + router.
 */
export interface ActionDeps {
  themeToggle: () => void;
  themeLabel: string;
  navigate: (path: string) => void;
  qc: QueryClient;
}

export function buildActions(deps: ActionDeps): Extract<Command, { kind: "action" }>[] {
  const { themeToggle, themeLabel, navigate, qc } = deps;
  return [
    {
      kind: "action",
      id: "toggle-theme",
      label: `Toggle theme (currently ${themeLabel})`,
      group: "View",
      aliases: ["dark", "light", "color", "theme"],
      run: () => {
        themeToggle();
      },
    },
    {
      kind: "action",
      id: "run-benchmark",
      label: "Run benchmark",
      group: "Data",
      aliases: ["bench", "score", "evaluate"],
      run: () => {
        qc.invalidateQueries({ queryKey: ["bench"] });
        qc.invalidateQueries({ queryKey: ["bench-hist"] });
        navigate("/benchmark");
      },
    },
    {
      kind: "action",
      id: "poll-news-now",
      label: "Poll news now",
      group: "Data",
      aliases: ["refresh", "feed", "ingest", "rss"],
      run: () => {
        // Fire-and-forget — invalidate immediately so the UI repaints
        // even if the network call is still in flight. If the request
        // 4xx/5xxs we still want the freshest in-memory cache.
        void api
          .newsPollNow()
          .catch(() => {
            /* surfaced to user via existing news-status hero */
          })
          .finally(() => {
            qc.invalidateQueries({ queryKey: ["news-status"] });
            qc.invalidateQueries({ queryKey: ["feed-list"] });
          });
      },
    },
    {
      kind: "action",
      id: "clear-query-caches",
      label: "Clear all caches",
      group: "System",
      aliases: ["reset", "invalidate", "purge"],
      run: () => {
        // We intentionally do NOT touch localStorage — that would nuke
        // theme, watchlist, recent commands. Only the in-memory react-
        // query cache is cleared.
        qc.clear();
      },
    },
    {
      kind: "action",
      id: "open-settings-deepseek",
      label: "Open Settings → DeepSeek",
      group: "Settings",
      aliases: ["reviewer", "api key", "deepseek"],
      run: () => {
        navigate("/settings#deepseek");
      },
    },
    {
      kind: "action",
      id: "show-keyboard-shortcuts",
      label: "Show keyboard shortcuts",
      group: "View",
      aliases: ["help", "kbd", "chord", "?"],
      run: () => {
        window.dispatchEvent(new Event(OPEN_SHORTCUT_OVERLAY_EVENT));
      },
    },
    {
      kind: "action",
      id: "open-new-window",
      label: "Open in new window (CmdOrCtrl+N)",
      group: "View",
      aliases: ["window", "secondary", "split", "new", "dashboard"],
      run: () => {
        // Dispatch the same `catchem:menu` event the native File→New
        // Window menu uses on the Rust side. In the bundled Tauri shell
        // the menu item is the canonical pathway (Rust calls
        // `menu::open_secondary_window` directly without going through
        // the CustomEvent bridge); this dispatch is for ⌘K-first
        // analysts who never touch the menu bar. The useTauriMenu hook
        // routes `new_window` to `window.open(location.href)` as a
        // best-effort fallback so the action is observable from vite
        // preview / vitest too.
        window.dispatchEvent(
          new CustomEvent("catchem:menu", { detail: "new_window" }),
        );
      },
    },
    {
      kind: "action",
      id: "restart-onboarding",
      label: "Restart onboarding",
      group: "Settings",
      aliases: ["welcome", "tour", "first run"],
      run: () => {
        // Clear the flag AND reload so the tour also shows on the next
        // cold launch — this is the "reset me to a first-run state"
        // action. The Help page's "Replay welcome tour" button is the
        // lighter, reload-free path (lib/onboarding.requestOpenOnboarding)
        // for users who just want to re-watch it once.
        resetOnboarding();
        window.location.reload();
      },
    },
  ];
}

type Row =
  | { kind: "nav"; label: string; path: string; kbd?: string }
  | {
      kind: "action";
      id: string;
      label: string;
      group: ActionGroup;
      run: () => void;
    }
  | { kind: "symbol"; label: string };

/**
 * Cmd/Ctrl+K opens the palette. Includes fuzzy-ranked navigation,
 * grouped imperative actions, theme toggle, symbol/reason mention
 * quick-jump, and a recent-commands rail (covering both nav and
 * action invocations since v23).
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [selected, setSelected] = useState(0);
  const [recent, setRecent] = useState<string[]>(() => loadRecent());
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);
  const shouldRestoreFocus = useRef(false);
  const openRef = useRef(open);
  const nav = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();
  const { theme, toggle } = useTheme();

  useOverlaySurface({
    id: "command-palette",
    open,
    onClose: () => setOpen(false),
    lockBody: true,
  });
  useEffect(() => {
    openRef.current = open;
  }, [open]);

  const actions = useMemo(
    () => buildActions({ themeToggle: toggle, themeLabel: theme, navigate: nav, qc }),
    [toggle, theme, nav, qc],
  );

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
      const typing = isTypingContext(e.target);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        if (typing) return;
        if (!openRef.current) {
          closeAndOpen();
          e.preventDefault();
          return;
        } else {
          shouldRestoreFocus.current = true;
        }
        e.preventDefault();
        setOpen(false);
      }
    };
    const openEvent = () => {
      if (openRef.current) return;
      closeAndOpen();
    };
    document.addEventListener("keydown", handler);
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, openEvent);
    return () => {
      document.removeEventListener("keydown", handler);
      window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, openEvent);
    };
  }, []);

  // Keep overlays ephemeral across navigations.
  useEffect(() => {
    if (open) setOpen(false);
  }, [location.pathname]);

  // Reset on open / new query.
  useEffect(() => {
    if (open) {
      if (!shouldRestoreFocus.current) shouldRestoreFocus.current = true;
      setInput("");
      setSelected(0);
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

  const rows = useMemo<Row[]>(() => {
    const q = input.trim();

    // Lookup helpers so the recent rail can find rows regardless of
    // whether the recent tag is `nav:` or `action:`.
    const navByPath = new Map(NAV.map((n) => [n.path, n] as const));
    const actionById = new Map(actions.map((a) => [a.id, a] as const));

    const recentNavPaths = new Set<string>();
    const recentActionIds = new Set<string>();
    for (const tag of recent) {
      const u = untag(tag);
      if (!u) continue;
      if (u.kind === "nav") recentNavPaths.add(u.value);
      else recentActionIds.add(u.value);
    }

    // Score helper: tie-break ranked results by recent-recency so a
    // freshly invoked command floats up on identical fuzzy score.
    const recentRank = (tag: string) => {
      const i = recent.indexOf(tag);
      return i === -1 ? 0 : recent.length - i;
    };

    if (!q) {
      // Empty query: recent first (in stored order), then nav, then
      // actions (grouped by `group`).
      const recentRows: Row[] = [];
      for (const tag of recent) {
        const u = untag(tag);
        if (!u) continue;
        if (u.kind === "nav") {
          const n = navByPath.get(u.value);
          if (n) recentRows.push({ kind: "nav", label: n.label, path: n.path, kbd: n.kbd });
        } else {
          const a = actionById.get(u.value);
          if (a)
            recentRows.push({
              kind: "action",
              id: a.id,
              label: a.label,
              group: a.group,
              run: a.run,
            });
        }
      }
      const navRest: Row[] = NAV.filter((n) => !recentNavPaths.has(n.path)).map((n) => ({
        kind: "nav",
        label: n.label,
        path: n.path,
        kbd: n.kbd,
      }));
      const actionRest: Row[] = actions
        .filter((a) => !recentActionIds.has(a.id))
        .map((a) => ({
          kind: "action",
          id: a.id,
          label: a.label,
          group: a.group,
          run: a.run,
        }));
      return [...recentRows, ...navRest, ...actionRest];
    }

    // Active query: rank both nav and actions in a single pass so
    // strong fuzzy matches on either kind win equally.
    const navScored = NAV.map((n) => ({ n, score: fuzzyScore(q, n.label) }))
      .filter((row) => row.score >= 0)
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        return recentRank(tagNav(b.n.path)) - recentRank(tagNav(a.n.path));
      });

    const actionScored = actions
      .map((a) => ({ a, score: fuzzyScoreWithAliases(q, a.label, a.aliases) }))
      .filter((row) => row.score >= 0)
      .sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        return recentRank(tagAction(b.a.id)) - recentRank(tagAction(a.a.id));
      });

    const navRows: Row[] = navScored.map(({ n }) => ({
      kind: "nav",
      label: n.label,
      path: n.path,
      kbd: n.kbd,
    }));
    const actionRows: Row[] = actionScored.map(({ a }) => ({
      kind: "action",
      id: a.id,
      label: a.label,
      group: a.group,
      run: a.run,
    }));

    const symbolRow: Row = { kind: "symbol", label: symbolMentionActionLabel(input) };
    return [...navRows, ...actionRows, symbolRow];
  }, [input, recent, actions]);

  useEffect(() => {
    if (!open) return;
    setSelected((current) => {
      if (rows.length === 0) return 0;
      return Math.min(current, rows.length - 1);
    });
  }, [rows, open]);

  const hasQuery = input.trim().length > 0;
  const recentRowCount = useMemo(() => {
    if (hasQuery) return 0;
    // The first N rows are recent (whatever order they were stored in)
    // and can be either kind. We count by walking until we hit a row
    // that isn't in the recent set.
    const recentNavPaths = new Set<string>();
    const recentActionIds = new Set<string>();
    for (const tag of recent) {
      const u = untag(tag);
      if (!u) continue;
      if (u.kind === "nav") recentNavPaths.add(u.value);
      else recentActionIds.add(u.value);
    }
    let count = 0;
    for (const r of rows) {
      if (r.kind === "nav" && recentNavPaths.has(r.path)) count++;
      else if (r.kind === "action" && recentActionIds.has(r.id)) count++;
      else break;
    }
    return count;
  }, [hasQuery, recent, rows]);
  const noNavOrActionMatches =
    hasQuery && rows.filter((r) => r.kind === "nav" || r.kind === "action").length === 0;

  const activate = (row: Row) => {
    if (row.kind === "symbol") {
      // Symbol-mention quick-jump bypasses the recent rail by design —
      // the canonical symbol resolution lives on the page itself.
      const route = buildSymbolRoute(input);
      if (!route) return;
      setOpen(false);
      nav(route);
      return;
    }
    if (row.kind === "action") {
      // Close FIRST, run AFTER. Some actions (clear-query-caches,
      // restart-onboarding) will re-render the world; closing first
      // means the palette doesn't flash a stale state while the new
      // tree mounts.
      const fn = row.run;
      const id = row.id;
      setOpen(false);
      setRecent((r) => pushRecent(tagAction(id), r));
      // Defer to next microtask so React commits the close before we
      // potentially dispatch a window-level reload/event.
      queueMicrotask(() => {
        try {
          fn();
        } catch {
          /* user-action errors must not crash the palette */
        }
      });
      return;
    }
    setRecent((r) => pushRecent(tagNav(row.path), r));
    setOpen(false);
    if (row.path.startsWith("/legacy")) window.location.href = row.path;
    else nav(row.path);
  };

  const handleKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      if (input.trim().length > 0) {
        e.preventDefault();
        e.stopPropagation();
        setInput("");
        setSelected(0);
        return;
      }
    }
    if (e.key === "Home") {
      if (rows.length === 0) return;
      e.preventDefault();
      setSelected(0);
      return;
    }
    if (e.key === "End") {
      if (rows.length === 0) return;
      e.preventDefault();
      setSelected(rows.length - 1);
      return;
    }
    if (e.key === "ArrowDown" || (e.key === "Tab" && !e.shiftKey)) {
      if (rows.length === 0) return;
      e.preventDefault();
      setSelected((i) => Math.min(rows.length - 1, i + 1));
      return;
    }
    if (e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey)) {
      e.preventDefault();
      setSelected((i) => Math.max(0, i - 1));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const row = rows[selected];
      if (row) activate(row);
      else if (hasQuery) {
        // No matches but user pressed Enter: jump to symbol search.
        const route = buildSymbolRoute(input);
        if (route) {
          setOpen(false);
          nav(route);
        }
      }
    }
  };

  // Keep the selected row scrolled into view.
  useEffect(() => {
    if (!open || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[data-row-index="${selected}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [selected, open]);

  if (!open) return null;

  return (
    <div
      className="command-palette fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-24 px-4"
      onClick={() => setOpen(false)}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-full max-w-xl rounded-lg border border-[color:var(--border)] bg-[color:var(--bg-elev)] shadow-soft animate-modal-enter"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-[color:var(--border)] px-3">
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder={SYMBOL_MENTION_PLACEHOLDER}
            aria-label="Command query"
            className="flex-1 bg-transparent py-3 text-sm outline-none"
          />
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="btn py-1 px-2 text-[10px] leading-none"
            aria-label="Close command palette"
          >
            close
          </button>
        </div>
        <div ref={listRef} className="max-h-96 overflow-auto p-1">
          {!hasQuery && recentRowCount > 0 && (
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
              Recent
            </div>
          )}
          {!hasQuery && recentRowCount === 0 && (
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
              Jump to
            </div>
          )}
          {hasQuery && !noNavOrActionMatches && (
            <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
              Jump to
            </div>
          )}
          {hasQuery && noNavOrActionMatches && (
            <div
              data-testid="palette-no-matches"
              className="px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]"
            >
              No matches
            </div>
          )}

          {rows.map((row, i) => {
            const isSelected = i === selected;
            const isFirstNonRecent =
              !hasQuery && recentRowCount > 0 && i === recentRowCount;
            const label =
              row.kind === "nav" || row.kind === "action" ? row.label : row.label;
            // Natural keys keep highlight stable when rows re-rank during
            // typing. Nav paths and action ids are unique per row-kind;
            // the symbol row is singular per render so a literal works.
            const naturalKey =
              row.kind === "nav"
                ? `nav-${row.path}`
                : row.kind === "action"
                  ? `action-${row.id}`
                  : "symbol";
            return (
              <div key={naturalKey}>
                {isFirstNonRecent && (
                  <div className="mt-1 px-3 py-1 text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
                    Jump to
                  </div>
                )}
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  data-row-index={i}
                  data-row-kind={row.kind}
                  data-row-id={row.kind === "action" ? row.id : undefined}
                  onMouseEnter={() => setSelected(i)}
                  onClick={() => activate(row)}
                  className={
                    "w-full text-left flex items-center justify-between rounded px-3 py-2 text-sm cursor-pointer transition-colors " +
                    (isSelected
                      ? "bg-[color:var(--bg-elev2)] border border-[color:var(--accent)]"
                      : "border border-transparent hover:bg-[color:var(--bg-elev2)]")
                  }
                >
                  <span>{label}</span>
                  {row.kind === "nav" && row.kbd && <span className="kbd">{row.kbd}</span>}
                  {row.kind === "action" && (
                    <span
                      className="kbd"
                      data-testid={`palette-action-group-${row.id}`}
                    >
                      {row.group}
                    </span>
                  )}
                </button>
              </div>
            );
          })}

          {!hasQuery && (
            <div className="px-3 py-2 text-[11px] text-[color:var(--fg-dim)]">
              Type to search.
            </div>
          )}
          {noNavOrActionMatches && (
            <div className="px-3 py-2 text-[11px] text-[color:var(--fg-dim)]">
              No matches. Press <span className="kbd">Esc</span> to close, or{" "}
              <span className="kbd">Enter</span> to find symbol mentions for{" "}
              <b className="text-good">{input.trim().toUpperCase()}</b>.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
