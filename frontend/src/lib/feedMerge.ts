// Pure functions for live-feed merge + buffering.
// Extracted so unit tests cover the behavior without rendering React.

export interface MinimalRecord {
  capture_id: string;
  published_ts?: string | null;
  created_at?: string | null;
}

export function stableRecordSort<T extends MinimalRecord>(a: T, b: T): number {
  // Newer first. Fall back to created_at when published_ts is missing.
  const at = a.published_ts || a.created_at || "";
  const bt = b.published_ts || b.created_at || "";
  if (at === bt) return a.capture_id.localeCompare(b.capture_id);
  return at < bt ? 1 : -1;
}

export function mergeByCaptureId<T extends MinimalRecord>(oldRows: T[] | undefined, newRows: T[] | undefined): T[] {
  const byId = new Map<string, T>();
  for (const row of oldRows || []) {
    if (row && row.capture_id) byId.set(row.capture_id, row);
  }
  for (const row of newRows || []) {
    if (row && row.capture_id) byId.set(row.capture_id, row);
  }
  return Array.from(byId.values()).sort(stableRecordSort);
}

/**
 * Compute the diff between two snapshots: which capture_ids are NEW since the
 * baseline. This is how the FeedPage decides whether to show the
 * "N new items available" affordance vs. silently re-rendering.
 */
export function newCaptureIds(baseline: MinimalRecord[], incoming: MinimalRecord[]): string[] {
  const seen = new Set(baseline.map((r) => r.capture_id));
  return incoming
    .map((r) => r.capture_id)
    .filter((id) => id && !seen.has(id));
}

/** True if any row has missing required identity (used to defer rendering). */
export function isIncompleteRecord(r: { capture_id?: string | null; doc_id?: string | null }): boolean {
  return !r.capture_id || !r.doc_id;
}
