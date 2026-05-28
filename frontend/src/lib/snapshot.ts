/**
 * Snapshot export/import for user preferences.
 *
 * Exports the curated set of localStorage keys that represent user-facing
 * workspace state. Does NOT export sensitive items (no API keys, no creds,
 * no session/runtime state). Importing is allow-listed: keys outside
 * SNAPSHOT_KEYS are skipped to prevent an attacker-supplied JSON from
 * planting arbitrary keys in the user's storage.
 *
 * For the SQLite truth-store backup, see `DatabaseBackupCard` in
 * `SettingsPage.tsx` — that's a separate feature with its own concerns
 * (binary blob, server-side replace, supervisor reload).
 */

/**
 * The exhaustive allow-list of keys we'll touch.
 *
 * Adding a key here both:
 *   1. lets `exportSnapshot()` include it in the dump, and
 *   2. lets `importSnapshot()` write it back on restore.
 *
 * Keys NOT in this list are silently skipped on import (security: don't
 * blindly write attacker-supplied keys) and never included on export
 * (privacy: don't ship anything the user didn't sign up to share).
 */
const SNAPSHOT_KEYS = [
  "catchem.theme",
  "catchem.accent",
  "catchem.accent.custom.light",
  "catchem.accent.custom.dark",
  "catchem.watchlist",
  "catchem.watchlist.sort",
  "catchem.palette.recent",
  "catchem.search.saved",
  "catchem.quant.kpi-history",
  "catchem.onboarding.completed",
  "catchem.alerts.enabled",
  "catchem.alerts.threshold",
] as const;

/** Public view of the allow-list — exposed so the UI can show users
 *  exactly what's about to be included, with no surprises. */
export const SNAPSHOT_ALLOW_LIST: readonly string[] = SNAPSHOT_KEYS;

export interface Snapshot {
  schema_version: 1;
  exported_at: string;
  app_version?: string;
  preferences: Record<string, string>;
}

/**
 * Collects the allow-listed keys currently in localStorage into a
 * snapshot envelope. Missing keys are simply omitted (they read back as
 * defaults on restore, which is the same behaviour the user gets on a
 * fresh install). The result is a plain JSON-serialisable object.
 */
export function exportSnapshot(): Snapshot {
  const preferences: Record<string, string> = {};
  for (const key of SNAPSHOT_KEYS) {
    try {
      const v = localStorage.getItem(key);
      if (v !== null) preferences[key] = v;
    } catch {
      // localStorage may throw (Safari private-mode / quota). Just skip
      // the key — exporting partial state is better than failing the
      // whole snapshot.
    }
  }
  return {
    schema_version: 1,
    exported_at: new Date().toISOString(),
    preferences,
  };
}

/**
 * Triggers a browser download of the JSON-serialised snapshot. The file
 * is pretty-printed with 2-space indent so it's grep-friendly and easy
 * to inspect in a text editor before importing on another machine.
 *
 * Default filename is timestamped from `exported_at` with `:` and `.`
 * replaced (they're filesystem-hostile on Windows / surface oddly on
 * macOS). Callers may pass a custom name.
 */
export function downloadSnapshot(
  snapshot: Snapshot,
  filename = `catchem_snapshot_${snapshot.exported_at.replace(/[:.]/g, "-")}.json`,
): void {
  const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Writes the snapshot back to localStorage. Only keys present in
 * SNAPSHOT_KEYS are restored; everything else is reported under
 * `skipped` so the UI can surface what was ignored.
 *
 * Returns the partition of input keys so callers can show a summary
 * ("restored N keys, skipped M") and let the user decide whether the
 * skipped list looks suspicious.
 *
 * Throws on unsupported schema versions — silent migration is too easy
 * to get wrong, so future-proofing is left to a versioned dispatcher.
 */
export function importSnapshot(snapshot: Snapshot): { restored: string[]; skipped: string[] } {
  if (snapshot.schema_version !== 1) {
    throw new Error(`Unsupported snapshot version: ${snapshot.schema_version}`);
  }
  const allow = new Set<string>(SNAPSHOT_KEYS as readonly string[]);
  const restored: string[] = [];
  const skipped: string[] = [];
  for (const [key, value] of Object.entries(snapshot.preferences ?? {})) {
    if (allow.has(key)) {
      try {
        localStorage.setItem(key, value);
        restored.push(key);
      } catch {
        // Quota / private-mode failure — track under skipped so the
        // user sees that the restore wasn't fully applied.
        skipped.push(key);
      }
    } else {
      skipped.push(key);
    }
  }
  return { restored, skipped };
}

/**
 * Reads the bytes of a Blob/File as a UTF-8 string. Modern browsers
 * expose `Blob.prototype.text()` directly; older Safari + jsdom (used
 * in our test runner) don't have it, so we fall back to FileReader. We
 * keep the fallback async-shaped so callers don't need to care which
 * path was taken.
 */
async function readBlobAsText(blob: Blob): Promise<string> {
  if (typeof (blob as Blob & { text?: () => Promise<string> }).text === "function") {
    return (blob as Blob & { text: () => Promise<string> }).text();
  }
  // FileReader fallback for environments without Blob.text() (jsdom).
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("FileReader error"));
    reader.onload = () => {
      const result = reader.result;
      resolve(typeof result === "string" ? result : "");
    };
    reader.readAsText(blob);
  });
}

/**
 * Side-by-side comparison of two snapshots (typically the user's current
 * preferences vs. an imported file). Partitions every key into one of
 * four buckets so the UI can surface a "what's about to change" preview
 * before the destructive `importSnapshot()` call lands.
 *
 *   added     — present in `imported`, missing in `current` (gain)
 *   removed   — present in `current`,  missing in `imported` (loss)
 *   changed   — present in both, different values (overwrite)
 *   identical — present in both, same value (no-op)
 *
 * Pure function — does not read or write localStorage. Callers feed in
 * `exportSnapshot()` for the current side and a parsed file for the
 * imported side.
 */
export interface SnapshotDiff {
  /** Keys in `imported.preferences` but not in `current.preferences`. */
  added: string[];
  /** Keys in `current.preferences` but not in `imported.preferences`. */
  removed: string[];
  /** Keys present in both with diverging values. */
  changed: { key: string; from: string; to: string }[];
  /** Keys present in both with the same value (would be a no-op restore). */
  identical: string[];
}

export function diffSnapshots(current: Snapshot, imported: Snapshot): SnapshotDiff {
  const currentKeys = new Set(Object.keys(current.preferences));
  const importedKeys = new Set(Object.keys(imported.preferences));

  const added: string[] = [];
  const removed: string[] = [];
  const changed: { key: string; from: string; to: string }[] = [];
  const identical: string[] = [];

  // Added: in imported, not in current.
  for (const key of importedKeys) {
    if (!currentKeys.has(key)) added.push(key);
  }

  // Removed: in current, not in imported. Note: `importSnapshot()` does
  // not actually delete these — it just won't restore them. Surfaced
  // here so the user knows the imported file is missing keys they have
  // locally (could be intentional, could be a stale snapshot).
  for (const key of currentKeys) {
    if (!importedKeys.has(key)) removed.push(key);
  }

  // Changed vs identical: in both.
  for (const key of currentKeys) {
    if (importedKeys.has(key)) {
      if (current.preferences[key] === imported.preferences[key]) {
        identical.push(key);
      } else {
        changed.push({
          key,
          from: current.preferences[key],
          to: imported.preferences[key],
        });
      }
    }
  }

  return { added, removed, changed, identical };
}

/**
 * Reads + validates a snapshot file. Just enough validation to catch
 * "this isn't a snapshot file" — the deeper allow-list filtering
 * happens in `importSnapshot()` so a partially-corrupt file can still
 * restore the valid bits.
 */
export async function readSnapshotFile(file: File): Promise<Snapshot> {
  const text = await readBlobAsText(file);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`Invalid snapshot: not valid JSON (${err instanceof Error ? err.message : String(err)})`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Invalid snapshot: not an object");
  }
  const obj = parsed as Record<string, unknown>;
  if (!("schema_version" in obj)) throw new Error("Invalid snapshot: missing schema_version");
  if (!("preferences" in obj)) throw new Error("Invalid snapshot: missing preferences");
  if (typeof obj.preferences !== "object" || obj.preferences === null || Array.isArray(obj.preferences)) {
    throw new Error("Invalid snapshot: preferences is not an object");
  }
  return parsed as Snapshot;
}
