import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";
import url from "node:url";

/**
 * Smoke test for the @media print block in globals.css.
 *
 * Verifies that print-friendly rules survive future edits — the dark cockpit
 * is unreadable when printed without these overrides, so we pin the key
 * tokens + structural rules in CI.
 */

const here = path.dirname(url.fileURLToPath(import.meta.url));
const cssPath = path.resolve(here, "../styles/globals.css");
const css = fs.readFileSync(cssPath, "utf-8");

describe("globals.css print stylesheet", () => {
  it("declares an @media print block", () => {
    expect(css).toContain("@media print");
  });

  it("forces light tokens at print time", () => {
    expect(css).toContain("--bg: #ffffff");
    expect(css).toContain("--fg: #000000");
  });

  it("hides app chrome via display:none rules", () => {
    expect(css).toMatch(/header,[\s\S]*?display: none/);
    expect(css).toContain(".no-print");
  });

  it("keeps cards intact across page breaks", () => {
    expect(css).toContain("page-break-inside: avoid");
  });

  it("reveals real URLs after external links", () => {
    expect(css).toMatch(/a\[href\][\s\S]*?::after[\s\S]*?content/);
  });

  it("sets a printable page size + margins", () => {
    expect(css).toMatch(/@page\s*\{[\s\S]*?margin:\s*1in/);
  });

  it("disables animations during print", () => {
    expect(css).toMatch(/@media print[\s\S]*animation: none/);
  });
});
