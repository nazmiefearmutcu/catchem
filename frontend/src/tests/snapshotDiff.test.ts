import { describe, it, expect } from "vitest";
import { diffSnapshots, type Snapshot } from "@/lib/snapshot";

// Tiny helper — every test starts from a sparse snapshot envelope; the
// schema_version + exported_at fields are constant noise here.
function snap(preferences: Record<string, string>): Snapshot {
  return {
    schema_version: 1,
    exported_at: "2026-05-28T10:00:00.000Z",
    preferences,
  };
}

describe("diffSnapshots", () => {
  it("treats every imported key as 'added' when current is empty", () => {
    const current = snap({});
    const imported = snap({
      "catchem.theme": "dark",
      "catchem.accent": "violet",
    });

    const d = diffSnapshots(current, imported);

    expect(d.added.sort()).toEqual(["catchem.accent", "catchem.theme"]);
    expect(d.removed).toEqual([]);
    expect(d.changed).toEqual([]);
    expect(d.identical).toEqual([]);
  });

  it("treats matching snapshots as fully 'identical'", () => {
    const prefs = {
      "catchem.theme": "dark",
      "catchem.accent": "violet",
      "catchem.watchlist": JSON.stringify(["AAPL", "TSLA"]),
    };

    const d = diffSnapshots(snap(prefs), snap({ ...prefs }));

    expect(d.added).toEqual([]);
    expect(d.removed).toEqual([]);
    expect(d.changed).toEqual([]);
    expect(d.identical.sort()).toEqual([
      "catchem.accent",
      "catchem.theme",
      "catchem.watchlist",
    ]);
  });

  it("captures a single value flip as 'changed' with from/to", () => {
    const current = snap({
      "catchem.theme": "dark",
      "catchem.accent": "violet",
    });
    const imported = snap({
      "catchem.theme": "light", // flipped
      "catchem.accent": "violet", // unchanged → identical
    });

    const d = diffSnapshots(current, imported);

    expect(d.changed).toEqual([
      { key: "catchem.theme", from: "dark", to: "light" },
    ]);
    expect(d.identical).toEqual(["catchem.accent"]);
    expect(d.added).toEqual([]);
    expect(d.removed).toEqual([]);
  });

  it("flags keys only present in current as 'removed'", () => {
    const current = snap({
      "catchem.theme": "dark",
      "catchem.accent": "violet",
      "catchem.watchlist": "[]",
    });
    const imported = snap({
      "catchem.theme": "dark", // identical
      // accent + watchlist missing → removed
    });

    const d = diffSnapshots(current, imported);

    expect(d.removed.sort()).toEqual(["catchem.accent", "catchem.watchlist"]);
    expect(d.identical).toEqual(["catchem.theme"]);
    expect(d.added).toEqual([]);
    expect(d.changed).toEqual([]);
  });

  it("handles a mixed diff covering all four buckets simultaneously", () => {
    const current = snap({
      "catchem.theme": "dark", // changed → light
      "catchem.accent": "violet", // identical
      "catchem.watchlist": "[]", // removed (not in imported)
    });
    const imported = snap({
      "catchem.theme": "light",
      "catchem.accent": "violet",
      "catchem.alerts.enabled": "true", // added (not in current)
    });

    const d = diffSnapshots(current, imported);

    expect(d.added).toEqual(["catchem.alerts.enabled"]);
    expect(d.removed).toEqual(["catchem.watchlist"]);
    expect(d.changed).toEqual([
      { key: "catchem.theme", from: "dark", to: "light" },
    ]);
    expect(d.identical).toEqual(["catchem.accent"]);
  });

  it("does not mutate either input snapshot", () => {
    const current = snap({ "catchem.theme": "dark" });
    const imported = snap({ "catchem.theme": "light", "catchem.accent": "rose" });
    const currentJson = JSON.stringify(current);
    const importedJson = JSON.stringify(imported);

    diffSnapshots(current, imported);

    expect(JSON.stringify(current)).toBe(currentJson);
    expect(JSON.stringify(imported)).toBe(importedJson);
  });

  it("returns empty arrays for two empty snapshots (no false positives)", () => {
    const d = diffSnapshots(snap({}), snap({}));

    expect(d.added).toEqual([]);
    expect(d.removed).toEqual([]);
    expect(d.changed).toEqual([]);
    expect(d.identical).toEqual([]);
  });
});
