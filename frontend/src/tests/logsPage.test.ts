import { describe, it, expect } from "vitest";
import { classifyLine, deriveLinesPerMinute } from "@/features/logs/LogsPage";

/**
 * Pure-helper pins for the /logs page. The component is exercised via
 * the UI truth-surface harness; these tests guard the two functions
 * that have non-trivial branching:
 *
 *   - classifyLine() — used by the level filter + KPI tiles + line
 *     colorization. Mis-classification would silently drop lines from
 *     the wrong filter bucket or paint warnings as errors.
 *   - deriveLinesPerMinute() — gates the "rate" KPI tile against
 *     Infinity/NaN on the first tick and flips back to 0 when no new
 *     lines arrive.
 */

describe("classifyLine", () => {
  it("tags python/uvicorn level prefixes", () => {
    expect(classifyLine("INFO:     uvicorn started")).toBe("info");
    expect(classifyLine("WARNING: feed not configured")).toBe("warn");
    expect(classifyLine("ERROR: failed to connect")).toBe("error");
  });

  it("tags structlog-style key=value lines", () => {
    expect(classifyLine("2026-05-28T01:00 level=info msg=started")).toBe("info");
    expect(classifyLine("2026-05-28T01:00 level=warning msg=slow")).toBe("warn");
    expect(classifyLine("2026-05-28T01:00 level=error msg=oops")).toBe("error");
  });

  it("falls back to other for unrecognized lines", () => {
    expect(classifyLine("just a free-form trace")).toBe("other");
    expect(classifyLine("")).toBe("other");
    expect(classifyLine("12345")).toBe("other");
  });

  it("treats critical/fatal as error", () => {
    expect(classifyLine("CRITICAL: oh no")).toBe("error");
    expect(classifyLine("FATAL: stop")).toBe("error");
  });

  it("does not match info/error if they appear only inside a URL", () => {
    // "info" inside a URL path shouldn't promote a noise line to info.
    expect(classifyLine("GET /api/info HTTP/1.1")).toBe("other");
  });
});

describe("deriveLinesPerMinute", () => {
  it("returns 0 when the window is smaller than 1s", () => {
    expect(deriveLinesPerMinute(0, 1000, 100, 1500)).toBe(0);
  });

  it("returns 0 on a stale or zero delta", () => {
    expect(deriveLinesPerMinute(50, 1000, 50, 4000)).toBe(0);
    // Negative delta (lines rolled out of the tail window): also 0.
    expect(deriveLinesPerMinute(50, 1000, 40, 4000)).toBe(0);
  });

  it("computes lines/min from delta over elapsed window", () => {
    // 60 new lines over 60 seconds → 60/min
    expect(deriveLinesPerMinute(0, 0, 60, 60_000)).toBe(60);
    // 10 new lines over 30 seconds → 20/min
    expect(deriveLinesPerMinute(0, 0, 10, 30_000)).toBe(20);
  });

  it("guards against backwards clock skew", () => {
    expect(deriveLinesPerMinute(0, 5000, 100, 4000)).toBe(0);
  });
});
