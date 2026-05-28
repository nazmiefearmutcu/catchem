import { describe, it, expect } from "vitest";
import { extractDomain, formatSuccessRate } from "@/features/sources/SourcesPage";

/**
 * Pure-helper pins for the /sources page. The page component is wired
 * through react-query at runtime; these tests guard the two helpers that
 * shape the table cells — both of which have edge cases that would
 * silently produce garbage UI text on bad inputs:
 *
 *   - extractDomain(): pulls a publisher domain out of a full URL for
 *     the table's first column. Must strip `www.` and survive parse
 *     failure without throwing.
 *   - formatSuccessRate(): converts a success_rate ∈ [0, 1] into a
 *     percent string. Must reject NaN / negatives rather than emit
 *     `NaN%` into the cell.
 */

describe("extractDomain", () => {
  it("returns the host without `www.`", () => {
    expect(extractDomain("https://www.bbc.co.uk/news/rss")).toBe("bbc.co.uk");
    expect(extractDomain("https://feeds.bbci.co.uk/news/rss.xml")).toBe("feeds.bbci.co.uk");
  });

  it("preserves subdomains other than `www.`", () => {
    expect(extractDomain("https://feeds.feedburner.com/reuters/businessNews")).toBe(
      "feeds.feedburner.com",
    );
  });

  it("normalizes the host to lowercase", () => {
    expect(extractDomain("HTTPS://FEEDS.BBCI.CO.UK/news")).toBe("feeds.bbci.co.uk");
  });

  it("falls back to the raw string when the URL is unparseable", () => {
    expect(extractDomain("not a url")).toBe("not a url");
  });

  it("renders an em-dash placeholder for nullish inputs", () => {
    expect(extractDomain(null)).toBe("—");
    expect(extractDomain(undefined)).toBe("—");
    expect(extractDomain("")).toBe("—");
  });
});

describe("formatSuccessRate", () => {
  it("formats whole percents without a trailing decimal", () => {
    expect(formatSuccessRate(1)).toBe("100%");
    expect(formatSuccessRate(0)).toBe("0%");
    expect(formatSuccessRate(0.5)).toBe("50%");
  });

  it("formats fractional percents with at most one decimal", () => {
    expect(formatSuccessRate(0.8333)).toBe("83.3%");
    expect(formatSuccessRate(0.9876)).toBe("98.8%");
  });

  it("rejects NaN / negatives / Infinity rather than emit NaN%", () => {
    expect(formatSuccessRate(Number.NaN)).toBe("—");
    expect(formatSuccessRate(-0.5)).toBe("—");
    expect(formatSuccessRate(Number.POSITIVE_INFINITY)).toBe("—");
  });
});
