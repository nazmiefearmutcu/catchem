import { describe, it, expect } from "vitest";
import {
  SYMBOL_NAV_PATTERN,
  normalizeSymbolQuery,
  parseSymbolNavigation,
  buildSymbolRoute,
} from "@/lib/symbolNavigation";

describe("normalizeSymbolQuery", () => {
  it("trims surrounding whitespace", () => {
    expect(normalizeSymbolQuery("  aapl  ")).toBe("AAPL");
    expect(normalizeSymbolQuery("\tmsft\n")).toBe("MSFT");
  });

  it("upper-cases the token", () => {
    expect(normalizeSymbolQuery("btc")).toBe("BTC");
    expect(normalizeSymbolQuery("BrK.b")).toBe("BRK.B");
  });

  it("strips a single leading $ prefix", () => {
    expect(normalizeSymbolQuery("$aapl")).toBe("AAPL");
    expect(normalizeSymbolQuery("  $tsla ")).toBe("TSLA");
  });

  it("only strips the first $ — a second is preserved", () => {
    expect(normalizeSymbolQuery("$$aapl")).toBe("$AAPL");
  });

  it("returns empty string for blank / whitespace-only input", () => {
    expect(normalizeSymbolQuery("")).toBe("");
    expect(normalizeSymbolQuery("   ")).toBe("");
    expect(normalizeSymbolQuery("$")).toBe("");
  });
});

describe("SYMBOL_NAV_PATTERN", () => {
  it("accepts 1-12 char alnum, dot and hyphen tokens", () => {
    expect(SYMBOL_NAV_PATTERN.test("A")).toBe(true);
    expect(SYMBOL_NAV_PATTERN.test("AAPL")).toBe(true);
    expect(SYMBOL_NAV_PATTERN.test("BRK.B")).toBe(true);
    expect(SYMBOL_NAV_PATTERN.test("BTC-USD")).toBe(true);
    expect(SYMBOL_NAV_PATTERN.test("123456789012")).toBe(true); // exactly 12
  });

  it("rejects empty, too-long, and punctuated tokens", () => {
    expect(SYMBOL_NAV_PATTERN.test("")).toBe(false);
    expect(SYMBOL_NAV_PATTERN.test("1234567890123")).toBe(false); // 13 chars
    expect(SYMBOL_NAV_PATTERN.test("A B")).toBe(false); // space
    expect(SYMBOL_NAV_PATTERN.test("AA$PL")).toBe(false); // dollar inside
    expect(SYMBOL_NAV_PATTERN.test("AA/PL")).toBe(false); // slash
  });
});

describe("parseSymbolNavigation", () => {
  it("returns the normalized symbol for valid input", () => {
    expect(parseSymbolNavigation("aapl")).toBe("AAPL");
    expect(parseSymbolNavigation("  $btc-usd ")).toBe("BTC-USD");
    expect(parseSymbolNavigation("brk.b")).toBe("BRK.B");
  });

  it("returns null for empty / whitespace-only input", () => {
    expect(parseSymbolNavigation("")).toBeNull();
    expect(parseSymbolNavigation("   ")).toBeNull();
    expect(parseSymbolNavigation("$")).toBeNull();
  });

  it("returns null for tokens longer than 12 chars after normalization", () => {
    expect(parseSymbolNavigation("ABCDEFGHIJKLM")).toBeNull(); // 13
    expect(parseSymbolNavigation("$ABCDEFGHIJKLM")).toBeNull(); // 13 after $ strip
  });

  it("returns null for tokens with spaces or disallowed punctuation", () => {
    expect(parseSymbolNavigation("apple inc")).toBeNull();
    expect(parseSymbolNavigation("AA/PL")).toBeNull();
    expect(parseSymbolNavigation("foo@bar")).toBeNull();
  });
});

describe("buildSymbolRoute", () => {
  it("builds an encoded /symbols/:symbol path for valid input", () => {
    expect(buildSymbolRoute("aapl")).toBe("/symbols/AAPL");
    expect(buildSymbolRoute("  $btc-usd ")).toBe("/symbols/BTC-USD");
  });

  it("encodes the dot-containing symbols safely (dots are not %-encoded)", () => {
    // encodeURIComponent leaves '.' and '-' unescaped, so the route stays clean.
    expect(buildSymbolRoute("brk.b")).toBe("/symbols/BRK.B");
  });

  it("returns null when the symbol cannot be parsed", () => {
    expect(buildSymbolRoute("")).toBeNull();
    expect(buildSymbolRoute("   ")).toBeNull();
    expect(buildSymbolRoute("apple inc")).toBeNull();
    expect(buildSymbolRoute("ABCDEFGHIJKLM")).toBeNull();
  });
});
