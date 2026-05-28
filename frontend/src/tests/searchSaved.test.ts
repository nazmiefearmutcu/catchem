import { describe, it, expect, beforeEach } from "vitest";
import {
  loadSaved,
  saveSearch,
  removeSaved,
  SAVED_STORAGE_KEY,
  SAVED_CAP,
} from "@/lib/searchSaved";

/**
 * v33 (task #126) — pins the SearchPalette saved-queries store contract:
 *   - head insertion (last-saved-first)
 *   - case-insensitive dedupe
 *   - cap at SAVED_CAP
 *   - removeSaved by exact-string match
 *   - graceful fallback on missing / corrupt JSON
 *
 * jsdom in this project ships without a full Storage implementation, so
 * we install a minimal shim before each test (same pattern as
 * CommandPaletteActions.test.ts + desktopAlerts.test.ts).
 */

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

beforeEach(() => {
  installLocalStorage();
});

describe("loadSaved", () => {
  it("returns an empty array when localStorage is empty", () => {
    expect(loadSaved()).toEqual([]);
  });

  it("returns an empty array when the stored value is not valid JSON", () => {
    localStorage.setItem(SAVED_STORAGE_KEY, "{not json");
    expect(loadSaved()).toEqual([]);
  });

  it("filters out non-string elements (defensive parse)", () => {
    localStorage.setItem(
      SAVED_STORAGE_KEY,
      JSON.stringify(["tesla", 42, null, "aapl"]),
    );
    expect(loadSaved()).toEqual(["tesla", "aapl"]);
  });
});

describe("saveSearch", () => {
  it("adds a new query to the head of the list", () => {
    saveSearch("tesla");
    expect(loadSaved()).toEqual(["tesla"]);
    saveSearch("aapl");
    // last-saved-first
    expect(loadSaved()).toEqual(["aapl", "tesla"]);
  });

  it("dedupes case-insensitively and promotes the existing entry to head", () => {
    saveSearch("tesla");
    saveSearch("aapl");
    saveSearch("msft");
    // "tesla" promotes to head (was at tail), case folded
    saveSearch("TESLA");
    expect(loadSaved()).toEqual(["TESLA", "msft", "aapl"]);
    expect(loadSaved().length).toBe(3); // no duplicate
  });

  it("caps the list at SAVED_CAP (10) — oldest falls off the tail", () => {
    for (let i = 0; i < SAVED_CAP + 5; i += 1) {
      saveSearch(`q${i}`);
    }
    const saved = loadSaved();
    expect(saved.length).toBe(SAVED_CAP);
    // Newest at head: the last query saved (q14) is at index 0.
    expect(saved[0]).toBe(`q${SAVED_CAP + 4}`);
    // q0..q4 fell off the tail; q5 is at the very back.
    expect(saved[SAVED_CAP - 1]).toBe("q5");
    expect(saved).not.toContain("q0");
  });

  it("rejects whitespace-only queries (no-op)", () => {
    saveSearch("tesla");
    saveSearch("   ");
    saveSearch("");
    saveSearch("\t\n");
    expect(loadSaved()).toEqual(["tesla"]);
  });

  it("trims surrounding whitespace before storing", () => {
    saveSearch("  tesla  ");
    expect(loadSaved()).toEqual(["tesla"]);
  });
});

describe("removeSaved", () => {
  it("removes an entry by exact-string match", () => {
    saveSearch("tesla");
    saveSearch("aapl");
    saveSearch("msft");
    removeSaved("aapl");
    expect(loadSaved()).toEqual(["msft", "tesla"]);
  });

  it("is a no-op when the query is not in the list", () => {
    saveSearch("tesla");
    removeSaved("not-there");
    expect(loadSaved()).toEqual(["tesla"]);
  });

  it("is case-sensitive on removal (matches the stored capitalisation)", () => {
    saveSearch("Tesla");
    removeSaved("tesla"); // wrong case → no-op
    expect(loadSaved()).toEqual(["Tesla"]);
    removeSaved("Tesla"); // exact match → removed
    expect(loadSaved()).toEqual([]);
  });
});
