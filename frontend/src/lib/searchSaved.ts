/**
 * Saved-search store for the ⌘P SearchPalette (task #126).
 *
 * Persists a small list of user-chosen queries to localStorage so the
 * empty-query state of the palette can offer one-click re-runs. The
 * store is intentionally tiny and synchronous: ten strings max, no
 * timestamps, no extra metadata. Anything fancier would be visual
 * clutter the palette doesn't need.
 *
 * Storage shape:
 *   localStorage["catchem.search.saved"] = JSON.stringify(string[])
 *
 * Invariants enforced by `saveSearch`:
 *   - last-saved-first ordering (head insertion)
 *   - case-insensitive dedupe (saving "TSLA" then "tsla" results in one
 *     entry, the more-recent capitalisation wins)
 *   - cap at SAVED_CAP (10) — older entries fall off the tail
 *   - whitespace-only queries are rejected outright
 */

export const SAVED_STORAGE_KEY = "catchem.search.saved";
export const SAVED_CAP = 10;

/**
 * Read the persisted list. Returns `[]` on any failure mode (missing
 * key, corrupt JSON, non-array payload, non-string elements) — the
 * palette never crashes because someone hand-edited localStorage.
 */
export function loadSaved(): string[] {
  try {
    const raw = localStorage.getItem(SAVED_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v): v is string => typeof v === "string");
  } catch {
    return [];
  }
}

/** Internal: persist without throwing on quota / disabled storage. */
function persist(list: string[]): void {
  try {
    localStorage.setItem(SAVED_STORAGE_KEY, JSON.stringify(list));
  } catch {
    // Quota exceeded / private browsing with storage disabled — we
    // silently fall back to in-memory only. The next reload will give
    // the user a clean slate, which is preferable to throwing inside a
    // click handler.
  }
}

/**
 * Add `query` to the head of the saved list. If `query` already exists
 * (case-insensitive), the existing entry is removed first so the new
 * one takes the head slot — this means re-saving a query "promotes" it
 * to the most-recent position. Returns the new list (pure functional
 * style; caller can choose whether to call loadSaved() again).
 *
 * Whitespace-only queries are rejected (no-op, returns current list).
 */
export function saveSearch(query: string): string[] {
  const trimmed = query.trim();
  if (!trimmed) return loadSaved();
  const lower = trimmed.toLowerCase();
  const current = loadSaved();
  const withoutDup = current.filter((q) => q.toLowerCase() !== lower);
  const next = [trimmed, ...withoutDup].slice(0, SAVED_CAP);
  persist(next);
  return next;
}

/**
 * Remove a saved query by exact-string match. No-op when the query is
 * not in the list. Returns the new list.
 */
export function removeSaved(query: string): string[] {
  const current = loadSaved();
  const next = current.filter((q) => q !== query);
  if (next.length === current.length) return current;
  persist(next);
  return next;
}

