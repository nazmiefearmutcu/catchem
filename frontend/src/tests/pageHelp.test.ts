import { describe, it, expect } from "vitest";
import { PAGE_HELP, matchHelp, type HelpContent } from "@/lib/page-help";
import { NAV_SHORTCUTS } from "@/lib/nav-shortcuts";

/**
 * Unit tests for the contextual page-help registry + its resolver.
 *
 * page-help.ts is pure data plus a small matcher. We pin:
 *   - structural integrity of every PAGE_HELP entry,
 *   - that embedded `navShortcutFor(...)` chords agree with the canonical
 *     NAV_SHORTCUTS registry (the help drawer must not advertise a chord
 *     that the handler can't route),
 *   - matchHelp exact / trailing-slash / prefix / alias / unknown paths.
 */

const KNOWN_CHORDS = new Set(NAV_SHORTCUTS.map((s) => `g ${s.key}`));

describe("PAGE_HELP registry integrity", () => {
  it("is keyed only by absolute, trailing-slash-free paths", () => {
    for (const path of Object.keys(PAGE_HELP)) {
      expect(path.startsWith("/"), `key "${path}" must be absolute`).toBe(true);
      if (path.length > 1) {
        expect(path.endsWith("/"), `key "${path}" trailing slash`).toBe(false);
      }
    }
  });

  it("every entry has the full HelpContent shape", () => {
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      expect(Array.isArray(content.quickTips), `${path}.quickTips`).toBe(true);
      expect(Array.isArray(content.questions), `${path}.questions`).toBe(true);
      expect(Array.isArray(content.shortcuts), `${path}.shortcuts`).toBe(true);
    }
  });

  it("quickTips are non-empty strings (3-5 per the module contract)", () => {
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      expect(content.quickTips.length, `${path} quickTips count`).toBeGreaterThan(
        0,
      );
      for (const tip of content.quickTips) {
        expect(typeof tip).toBe("string");
        expect(tip.trim().length).toBeGreaterThan(0);
      }
    }
  });

  it("every question pair has non-empty q and a fields", () => {
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      for (const qa of content.questions) {
        expect(qa.q.trim().length, `${path} empty q`).toBeGreaterThan(0);
        expect(qa.a.trim().length, `${path} empty a`).toBeGreaterThan(0);
      }
    }
  });

  it("every shortcut has a non-empty key and description", () => {
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      for (const sc of content.shortcuts) {
        expect(sc.key.trim().length, `${path} empty shortcut key`).toBeGreaterThan(
          0,
        );
        expect(
          sc.description.trim().length,
          `${path} empty shortcut description`,
        ).toBeGreaterThan(0);
      }
    }
  });

  it("every `g X` shortcut agrees with the canonical NAV_SHORTCUTS registry", () => {
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      for (const sc of content.shortcuts) {
        if (!sc.key.startsWith("g ")) continue; // skip "/", "Esc", etc.
        expect(
          KNOWN_CHORDS.has(sc.key),
          `${path} advertises chord "${sc.key}" that no NAV_SHORTCUTS entry defines`,
        ).toBe(true);
      }
    }
  });

  it("each page that has a navShortcutFor self-link uses its own canonical chord", () => {
    // navShortcutFor(path, ...) injects `g <key>` for that exact path. So the
    // first `g X` shortcut on a page keyed by a routable path must match the
    // chord registered for that path.
    for (const [path, content] of Object.entries(PAGE_HELP)) {
      const spec = NAV_SHORTCUTS.find((s) => s.path === path);
      if (!spec) continue;
      const selfChord = `g ${spec.key}`;
      const hasSelf = content.shortcuts.some((sc) => sc.key === selfChord);
      expect(
        hasSelf,
        `${path} should surface its own chord ${selfChord}`,
      ).toBe(true);
    }
  });
});

describe("matchHelp", () => {
  it("returns the exact entry for a known path", () => {
    expect(matchHelp("/feed")).toBe(PAGE_HELP["/feed"]);
    expect(matchHelp("/")).toBe(PAGE_HELP["/"]);
    expect(matchHelp("/settings")).toBe(PAGE_HELP["/settings"]);
  });

  it("resolves every PAGE_HELP key back to its own entry", () => {
    for (const path of Object.keys(PAGE_HELP)) {
      expect(matchHelp(path)).toBe(PAGE_HELP[path]);
    }
  });

  it("normalizes a trailing slash before matching", () => {
    expect(matchHelp("/feed/")).toBe(PAGE_HELP["/feed"]);
    expect(matchHelp("/symbols///")).toBe(PAGE_HELP["/symbols"]);
  });

  it("keeps the root path '/' intact (does not strip to empty)", () => {
    expect(matchHelp("/")).toBe(PAGE_HELP["/"]);
  });

  it("prefix-matches dynamic /feed/<id> segments to /feed", () => {
    expect(matchHelp("/feed/abc-123")).toBe(PAGE_HELP["/feed"]);
    expect(matchHelp("/feed/deadbeef")).toBe(PAGE_HELP["/feed"]);
  });

  it("prefix-matches dynamic /symbols/<ticker> segments to /symbols", () => {
    expect(matchHelp("/symbols/AAPL")).toBe(PAGE_HELP["/symbols"]);
    expect(matchHelp("/symbols/btc-usd")).toBe(PAGE_HELP["/symbols"]);
  });

  it("aliases /analysis to the /map help entry", () => {
    expect(matchHelp("/analysis")).toBe(PAGE_HELP["/map"]);
  });

  it("returns null for an unknown route", () => {
    expect(matchHelp("/does-not-exist")).toBeNull();
    expect(matchHelp("/feeed")).toBeNull();
  });

  it("returns null for a sibling that only shares a prefix without the slash", () => {
    // "/feedback" must NOT collapse to "/feed" — the prefix guard checks
    // for the "/feed/" boundary, not a bare "/feed" substring.
    expect(matchHelp("/feedback")).toBeNull();
    expect(matchHelp("/symbolsX")).toBeNull();
  });

  it("returns a usable HelpContent object (smoke on the resolved shape)", () => {
    const content = matchHelp("/feed") as HelpContent;
    expect(content.quickTips.length).toBeGreaterThan(0);
    expect(content.shortcuts.some((s) => s.key === "Esc")).toBe(true);
  });
});
