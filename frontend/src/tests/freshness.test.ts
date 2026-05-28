import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { freshnessLabel } from "@/lib/freshness";

// Anchor wall-clock so freshnessLabel -> fmtRel(Date.now()) is deterministic.
// freshnessLabel does NOT thread a `now` argument; it relies on the internal
// Date.now() inside fmtRel, so we pin the system clock with fake timers.
const NOW = Date.UTC(2026, 4, 17, 12, 0, 0); // 2026-05-17T12:00:00Z

describe("freshnessLabel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the em-dash placeholder for undefined (no data yet)", () => {
    expect(freshnessLabel(undefined)).toBe("—");
  });

  it("returns the placeholder for falsy timestamps (0)", () => {
    // 0 is the epoch but also falsy — the guard treats it as "no data".
    expect(freshnessLabel(0)).toBe("—");
  });

  it("renders 'data just now' for very recent (<5s) updates", () => {
    expect(freshnessLabel(NOW)).toBe("data just now");
    expect(freshnessLabel(NOW - 2_000)).toBe("data just now");
  });

  it("renders seconds in [5s, 60s)", () => {
    expect(freshnessLabel(NOW - 30_000)).toBe("data 30s ago");
    expect(freshnessLabel(NOW - 59_000)).toBe("data 59s ago");
  });

  it("renders minutes in [1m, 60m)", () => {
    expect(freshnessLabel(NOW - 60_000)).toBe("data 1m ago");
    expect(freshnessLabel(NOW - 2 * 60_000)).toBe("data 2m ago");
  });

  it("renders hours in [1h, 24h)", () => {
    expect(freshnessLabel(NOW - 3_600_000)).toBe("data 1h ago");
    expect(freshnessLabel(NOW - 5 * 3_600_000)).toBe("data 5h ago");
  });

  it("always prefixes the relative phrase with 'data '", () => {
    expect(freshnessLabel(NOW - 86_400_000)).toBe("data 1d ago");
    // Beyond 14 days, fmtRel falls back to an absolute YYYY-MM-DD date,
    // still prefixed by "data ".
    expect(freshnessLabel(NOW - 100 * 86_400_000)).toMatch(
      /^data \d{4}-\d{2}-\d{2}$/,
    );
  });
});
