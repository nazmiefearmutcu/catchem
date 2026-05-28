import { describe, it, expect, beforeEach } from "vitest";
import {
  exportSnapshot,
  importSnapshot,
  readSnapshotFile,
  SNAPSHOT_ALLOW_LIST,
  type Snapshot,
} from "@/lib/snapshot";

// jsdom in this project ships without a full Storage implementation, so we
// install our own minimal shim before each test (mirrors the pattern in
// desktopAlerts.test.ts so failing-mode behaviour stays consistent).
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => {
      store.delete(k);
    },
    setItem: (k, v) => {
      store.set(k, String(v));
    },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

describe("snapshot export/import", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("exportSnapshot collects only allow-listed keys", () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.accent", "violet");
    // Should NOT appear — DeepSeek API key is sensitive runtime state
    // and is intentionally excluded from the allow-list.
    localStorage.setItem("catchem.deepseek.api_key", "sk-secret-do-not-leak");
    localStorage.setItem("not-our-key", "irrelevant");

    const snap = exportSnapshot();

    expect(snap.preferences["catchem.theme"]).toBe("dark");
    expect(snap.preferences["catchem.accent"]).toBe("violet");
    expect(snap.preferences).not.toHaveProperty("catchem.deepseek.api_key");
    expect(snap.preferences).not.toHaveProperty("not-our-key");
    // Sanity: the envelope is well-formed.
    expect(snap.schema_version).toBe(1);
    expect(typeof snap.exported_at).toBe("string");
    expect(new Date(snap.exported_at).toString()).not.toBe("Invalid Date");
  });

  it("exportSnapshot omits keys that don't exist in localStorage", () => {
    localStorage.setItem("catchem.theme", "light");
    // Other allow-listed keys are absent — they should NOT show up as
    // empty strings or nulls, they should simply be missing.

    const snap = exportSnapshot();

    expect(snap.preferences).toEqual({ "catchem.theme": "light" });
    expect(Object.keys(snap.preferences)).toHaveLength(1);
  });

  it("importSnapshot writes allow-listed keys back to localStorage", () => {
    const snap: Snapshot = {
      schema_version: 1,
      exported_at: "2026-05-28T10:00:00.000Z",
      preferences: {
        "catchem.theme": "dark",
        "catchem.accent": "rose",
        "catchem.watchlist": JSON.stringify(["AAPL", "TSLA"]),
      },
    };

    const result = importSnapshot(snap);

    expect(result.restored.sort()).toEqual(
      ["catchem.theme", "catchem.accent", "catchem.watchlist"].sort(),
    );
    expect(result.skipped).toEqual([]);
    expect(localStorage.getItem("catchem.theme")).toBe("dark");
    expect(localStorage.getItem("catchem.accent")).toBe("rose");
    expect(localStorage.getItem("catchem.watchlist")).toBe(JSON.stringify(["AAPL", "TSLA"]));
  });

  it("importSnapshot skips unknown keys (defense against attacker-supplied JSON)", () => {
    const snap: Snapshot = {
      schema_version: 1,
      exported_at: "2026-05-28T10:00:00.000Z",
      preferences: {
        "catchem.theme": "dark",
        // These three are NOT in the allow-list and must be refused.
        "catchem.deepseek.api_key": "sk-malicious-injection",
        "evil.script.payload": "<script>...</script>",
        "../../etc/passwd": "root::0:0::",
      },
    };

    const result = importSnapshot(snap);

    expect(result.restored).toEqual(["catchem.theme"]);
    expect(result.skipped.sort()).toEqual(
      ["catchem.deepseek.api_key", "evil.script.payload", "../../etc/passwd"].sort(),
    );
    // The crucial check: localStorage should NOT have been poisoned.
    expect(localStorage.getItem("catchem.deepseek.api_key")).toBeNull();
    expect(localStorage.getItem("evil.script.payload")).toBeNull();
    expect(localStorage.getItem("../../etc/passwd")).toBeNull();
  });

  it("importSnapshot throws on unsupported schema_version", () => {
    const futureSnap = {
      schema_version: 2,
      exported_at: "2026-05-28T10:00:00.000Z",
      preferences: { "catchem.theme": "dark" },
    } as unknown as Snapshot;

    expect(() => importSnapshot(futureSnap)).toThrow(/Unsupported snapshot version/);
    // Nothing should have been written.
    expect(localStorage.getItem("catchem.theme")).toBeNull();
  });

  it("readSnapshotFile parses valid JSON into a Snapshot", async () => {
    const valid: Snapshot = {
      schema_version: 1,
      exported_at: "2026-05-28T10:00:00.000Z",
      preferences: { "catchem.theme": "dark" },
    };
    // Vitest's jsdom ships a File polyfill — use it to mimic the
    // browser <input type="file"> handoff.
    const file = new File([JSON.stringify(valid)], "snap.json", { type: "application/json" });

    const parsed = await readSnapshotFile(file);

    expect(parsed.schema_version).toBe(1);
    expect(parsed.preferences["catchem.theme"]).toBe("dark");
  });

  it("readSnapshotFile rejects invalid input", async () => {
    // 1. Not JSON.
    await expect(
      readSnapshotFile(new File(["not json {{{"], "bad.json", { type: "application/json" })),
    ).rejects.toThrow(/not valid JSON/);

    // 2. JSON but not an object (an array).
    await expect(
      readSnapshotFile(new File(["[1, 2, 3]"], "bad.json", { type: "application/json" })),
    ).rejects.toThrow(/not an object/);

    // 3. Object but missing schema_version.
    await expect(
      readSnapshotFile(
        new File([JSON.stringify({ preferences: {} })], "bad.json", { type: "application/json" }),
      ),
    ).rejects.toThrow(/missing schema_version/);

    // 4. Object missing preferences entirely.
    await expect(
      readSnapshotFile(
        new File([JSON.stringify({ schema_version: 1 })], "bad.json", { type: "application/json" }),
      ),
    ).rejects.toThrow(/missing preferences/);
  });

  it("SNAPSHOT_ALLOW_LIST excludes any 'api_key' / 'secret' / 'token' / 'credential' keys", () => {
    // Belt-and-braces guard: if someone later adds a sensitive key to
    // the list, this catches it before the snapshot ships secrets.
    for (const key of SNAPSHOT_ALLOW_LIST) {
      expect(key.toLowerCase()).not.toMatch(/api[_.-]?key/);
      expect(key.toLowerCase()).not.toMatch(/secret/);
      expect(key.toLowerCase()).not.toMatch(/token/);
      expect(key.toLowerCase()).not.toMatch(/credential/);
      expect(key.toLowerCase()).not.toMatch(/password/);
    }
    // And we DO have actual preference-shaped keys in the list.
    expect(SNAPSHOT_ALLOW_LIST).toContain("catchem.theme");
    expect(SNAPSHOT_ALLOW_LIST).toContain("catchem.accent");
  });
});
