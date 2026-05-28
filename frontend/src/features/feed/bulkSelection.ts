/**
 * Bulk selection logic for the Feed page — pure helpers, plus a tiny
 * client-side JSON exporter for "Export selected".
 *
 * These functions are intentionally framework-agnostic so they're trivial
 * to unit-test. The React wiring (state, keyboard shortcuts, toolbar) lives
 * in FeedPage.tsx and reuses these primitives.
 */

import type { FinancialRecord } from "@/types/api";

/** Toggle a capture_id in a Set, returning a NEW Set (so React re-renders). */
export function toggleSelection(prev: Set<string>, captureId: string): Set<string> {
  const next = new Set(prev);
  if (next.has(captureId)) {
    next.delete(captureId);
  } else {
    next.add(captureId);
  }
  return next;
}

/**
 * Build the "select all" set from a list of records. Used by the header
 * checkbox + the Cmd+A shortcut. Caller passes the currently-visible
 * (post-filter) record list — never the full backend list.
 */
export function selectAll(items: FinancialRecord[]): Set<string> {
  return new Set(items.map((i) => i.capture_id));
}

/** Empty selection — used for "Clear" + Esc. */
export function clearSelection(): Set<string> {
  return new Set();
}

/**
 * Extract unique uppercase symbols from selected records. Used by the
 * "Add to watchlist" bulk action — the watchlist itself is the dedupe
 * authority (it ignores already-present symbols on add), but pre-deduping
 * keeps the toast count honest ("Added N symbols" instead of "Added N
 * symbols, some skipped").
 */
export function extractUniqueSymbols(records: FinancialRecord[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const r of records) {
    for (const sym of r.candidate_symbols ?? []) {
      const norm = (sym ?? "").trim().toUpperCase();
      if (!norm || seen.has(norm)) continue;
      seen.add(norm);
      out.push(norm);
    }
  }
  return out;
}

/**
 * Extract every non-empty URL from selected records, in record order.
 * Duplicates kept — analyst may want each record's link even if the
 * destination repeats (rare in practice; we leave the call to them).
 */
export function extractUrls(records: FinancialRecord[]): string[] {
  return records
    .map((r) => (r.url ?? "").trim())
    .filter((u) => u.length > 0);
}

/**
 * Return the records from `all` whose capture_id appears in `selected`,
 * preserving the order of `all` (the user expects the export to match
 * what they see on screen, not the order they ticked checkboxes in).
 */
export function selectedRecords(
  all: FinancialRecord[],
  selected: Set<string>,
): FinancialRecord[] {
  if (selected.size === 0) return [];
  return all.filter((r) => selected.has(r.capture_id));
}

/**
 * Trigger a client-side JSON download of the selected records. Backend
 * has no /api/export/records?capture_ids=... shape, and round-tripping
 * the selection through the server would be wasted work — the data is
 * already in memory.
 *
 * Visible side-effect (anchor click) is exercised by FeedPage; the
 * pure builder `buildExportBlob` below is what tests assert on.
 */
export interface ExportPayload {
  exported_at: string;
  count: number;
  items: FinancialRecord[];
}

export function buildExportPayload(items: FinancialRecord[]): ExportPayload {
  return {
    exported_at: new Date().toISOString(),
    count: items.length,
    items,
  };
}

export function buildExportBlob(items: FinancialRecord[]): Blob {
  const payload = buildExportPayload(items);
  return new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
}

/**
 * Compose a safe-for-disk filename for the download. Colons / dots in
 * ISO strings break on Windows and confuse some macOS apps; we collapse
 * them to dashes.
 */
export function buildExportFilename(now: Date = new Date()): string {
  const stamp = now.toISOString().replace(/[:.]/g, "-");
  return `catchem_selection_${stamp}.json`;
}

/**
 * Run the actual download. Kept as a separate function so the React
 * component can call it and tests can assert the pure helpers without
 * touching the DOM.
 */
export function downloadSelection(items: FinancialRecord[]): void {
  const blob = buildExportBlob(items);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = buildExportFilename();
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
