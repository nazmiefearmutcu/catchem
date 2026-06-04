import { describe, it, expect } from "vitest";
import {
  NAV_SHORTCUTS,
  chordLabel,
  resolveShortcut,
  type NavShortcut,
} from "@/lib/nav-shortcuts";

/**
 * Dedicated unit tests for the canonical shortcut registry in isolation.
 *
 * The sibling `navShortcuts.test.ts` cross-checks this registry against the
 * three documentation surfaces (HelpPage / SettingsPage / CommandPalette).
 * This file instead pins the module's OWN contract: registry integrity
 * (unique keys, valid route shapes, alias hygiene) and the full behaviour of
 * the `resolveShortcut` matcher + `chordLabel` formatter.
 */

describe("NAV_SHORTCUTS registry integrity", () => {
  it("is a non-empty array", () => {
    expect(Array.isArray(NAV_SHORTCUTS)).toBe(true);
    expect(NAV_SHORTCUTS.length).toBeGreaterThan(0);
  });

  it("every key is a single lowercase character", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(s.key, `key "${s.key}" should be one char`).toHaveLength(1);
      expect(s.key, `key "${s.key}" should equal its lowercase`).toBe(
        s.key.toLowerCase(),
      );
    }
  });

  it("every alias (when present) is a single lowercase character", () => {
    for (const s of NAV_SHORTCUTS) {
      if (s.alias === undefined) continue;
      expect(s.alias).toHaveLength(1);
      expect(s.alias).toBe(s.alias.toLowerCase());
    }
  });

  it("has no duplicate primary keys", () => {
    const keys = NAV_SHORTCUTS.map((s) => s.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("has no key/alias collisions across the whole registry", () => {
    const seen = new Set<string>();
    for (const s of NAV_SHORTCUTS) {
      expect(seen.has(s.key), `duplicate token "${s.key}"`).toBe(false);
      seen.add(s.key);
      if (s.alias) {
        expect(seen.has(s.alias), `alias "${s.alias}" collides`).toBe(false);
        seen.add(s.alias);
      }
    }
  });

  it("every path is absolute (starts with '/') and has no trailing slash", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(s.path.startsWith("/"), `path "${s.path}" must be absolute`).toBe(
        true,
      );
      if (s.path.length > 1) {
        expect(s.path.endsWith("/"), `path "${s.path}" trailing slash`).toBe(
          false,
        );
      }
    }
  });

  it("has no duplicate paths", () => {
    const paths = NAV_SHORTCUTS.map((s) => s.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("every label is a non-empty trimmed string", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(typeof s.label).toBe("string");
      expect(s.label.length).toBeGreaterThan(0);
      expect(s.label).toBe(s.label.trim());
    }
  });

  it("contains the well-known anchor routes", () => {
    const byPath = new Map(NAV_SHORTCUTS.map((s) => [s.path, s]));
    expect(byPath.get("/")).toMatchObject({ key: "o", label: "Overview" });
    expect(byPath.get("/feed")).toMatchObject({ key: "f" });
    expect(byPath.get("/settings")).toMatchObject({ key: "," });
  });

  it("keeps the legacy `m` alias attached to the analysis map only", () => {
    const aliased = NAV_SHORTCUTS.filter((s) => s.alias !== undefined);
    expect(aliased).toHaveLength(1);
    expect(aliased[0]).toMatchObject({ key: "a", path: "/map", alias: "m" });
  });
});

describe("resolveShortcut", () => {
  it("maps every primary key to its documented path", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(resolveShortcut(s.key)).toBe(s.path);
    }
  });

  it("maps every alias to the same path as its primary entry", () => {
    for (const s of NAV_SHORTCUTS) {
      if (s.alias) expect(resolveShortcut(s.alias)).toBe(s.path);
    }
  });

  it("is case-insensitive on the input key", () => {
    expect(resolveShortcut("O")).toBe("/");
    expect(resolveShortcut("F")).toBe("/feed");
    expect(resolveShortcut("A")).toBe("/map");
    // Alias resolves uppercase too.
    expect(resolveShortcut("M")).toBe("/map");
  });

  it("resolves the legacy `m` chord to /map (muscle-memory alias)", () => {
    expect(resolveShortcut("m")).toBe("/map");
  });

  it("returns null for unknown / non-shortcut keys", () => {
    expect(resolveShortcut("z")).toBeNull();
    expect(resolveShortcut("9")).toBeNull();
    expect(resolveShortcut("?")).toBeNull();
  });

  it("returns null for the empty string", () => {
    expect(resolveShortcut("")).toBeNull();
  });

  it("does not match a multi-character string even if it starts with a key", () => {
    // The handler feeds a single key; a two-char string must not resolve.
    expect(resolveShortcut("oo")).toBeNull();
    expect(resolveShortcut("fa")).toBeNull();
  });
});

describe("chordLabel", () => {
  it("renders the canonical `g <key>` form", () => {
    expect(chordLabel({ key: "o", path: "/", label: "Overview" })).toBe("g o");
    expect(chordLabel({ key: ",", path: "/settings", label: "Settings" })).toBe(
      "g ,",
    );
  });

  it("uses the primary key, never the alias", () => {
    const map = NAV_SHORTCUTS.find((s) => s.path === "/map") as NavShortcut;
    expect(map.alias).toBe("m");
    expect(chordLabel(map)).toBe("g a");
    expect(chordLabel(map)).not.toBe("g m");
  });

  it("produces a stable label for every registry entry", () => {
    for (const s of NAV_SHORTCUTS) {
      expect(chordLabel(s)).toBe(`g ${s.key}`);
    }
  });
});
