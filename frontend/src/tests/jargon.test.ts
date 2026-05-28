import { describe, it, expect } from "vitest";
import { JARGON, SIGNAL_FORMULAS } from "@/lib/jargon";

// jargon.ts ships two pure data dictionaries consumed by the tooltip /
// popover components. These tests pin the data contract: shape, non-empty
// content, and cross-dictionary consistency. No DOM / network involved.

describe("JARGON dictionary", () => {
  it("is a non-empty record", () => {
    expect(Object.keys(JARGON).length).toBeGreaterThan(0);
  });

  it("maps every term to a non-empty trimmed string definition", () => {
    for (const [term, def] of Object.entries(JARGON)) {
      expect(typeof def).toBe("string");
      expect(def.trim().length, `definition for "${term}" should not be blank`).toBeGreaterThan(0);
      expect(def, `definition for "${term}" should be trimmed`).toBe(def.trim());
    }
  });

  it("has no blank term keys", () => {
    for (const term of Object.keys(JARGON)) {
      expect(term.trim().length).toBeGreaterThan(0);
    }
  });

  it("preserves the intentional upstream FUSION_* contract spelling", () => {
    // These names are mirrored verbatim from merged_news / newsimpact.
    expect(JARGON).toHaveProperty("fusion_verdict_class");
    expect(JARGON).toHaveProperty("FUSION_REGRESSIVE");
    expect(JARGON["FUSION_REGRESSIVE"]).toContain("Fusion");
  });
});

describe("SIGNAL_FORMULAS dictionary", () => {
  it("is a non-empty record", () => {
    expect(Object.keys(SIGNAL_FORMULAS).length).toBeGreaterThan(0);
  });

  it("gives each entry at least a formula or an example", () => {
    for (const [term, entry] of Object.entries(SIGNAL_FORMULAS)) {
      expect(entry, `entry for "${term}" should be an object`).toBeTypeOf("object");
      const hasFormula = typeof entry.formula === "string" && entry.formula.trim().length > 0;
      const hasExample = typeof entry.example === "string" && entry.example.trim().length > 0;
      expect(hasFormula || hasExample, `"${term}" needs a formula or example`).toBe(true);
    }
  });

  it("uses string typing for any present formula / example field", () => {
    for (const entry of Object.values(SIGNAL_FORMULAS)) {
      if (entry.formula !== undefined) expect(typeof entry.formula).toBe("string");
      if (entry.example !== undefined) expect(typeof entry.example).toBe("string");
    }
  });

  it("only documents formulas for terms that also have a glossary entry", () => {
    // SignalExplainer renders the formula next to a jargon term badge — so
    // every formula key must resolve to a JARGON definition too.
    for (const term of Object.keys(SIGNAL_FORMULAS)) {
      expect(JARGON, `"${term}" formula has no matching JARGON definition`).toHaveProperty(term);
    }
  });

  it("covers the canonical quant signals", () => {
    expect(SIGNAL_FORMULAS).toHaveProperty("z-score");
    expect(SIGNAL_FORMULAS).toHaveProperty("Pearson r");
    expect(SIGNAL_FORMULAS["z-score"].formula).toContain("mean");
  });
});
