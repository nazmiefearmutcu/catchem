import { describe, it, expect } from "vitest";
import { fmtRel } from "@/lib/api";

const T = Date.UTC(2026, 4, 17, 12, 0, 0); // 2026-05-17T12:00:00Z — anchor

describe("fmtRel", () => {
  it("returns empty for null/undefined", () => {
    expect(fmtRel(null, T)).toBe("");
    expect(fmtRel(undefined, T)).toBe("");
  });

  it("returns the input for unparseable strings", () => {
    expect(fmtRel("not a date", T)).toBe("not a date");
  });

  it("renders <5s as 'just now'", () => {
    expect(fmtRel(new Date(T - 2_000).toISOString(), T)).toBe("just now");
    expect(fmtRel(new Date(T).toISOString(), T)).toBe("just now");
  });

  it("renders seconds in [5s, 60s)", () => {
    expect(fmtRel(new Date(T - 12_000).toISOString(), T)).toBe("12s ago");
    expect(fmtRel(new Date(T - 59_000).toISOString(), T)).toBe("59s ago");
  });

  it("renders minutes in [1m, 60m)", () => {
    expect(fmtRel(new Date(T - 60_000).toISOString(), T)).toBe("1m ago");
    expect(fmtRel(new Date(T - 30 * 60_000).toISOString(), T)).toBe("30m ago");
  });

  it("renders hours in [1h, 24h)", () => {
    expect(fmtRel(new Date(T - 3_600_000).toISOString(), T)).toBe("1h ago");
    expect(fmtRel(new Date(T - 23 * 3_600_000).toISOString(), T)).toBe("23h ago");
  });

  it("renders days in [1d, 14d)", () => {
    expect(fmtRel(new Date(T - 86_400_000).toISOString(), T)).toBe("1d ago");
    expect(fmtRel(new Date(T - 13 * 86_400_000).toISOString(), T)).toBe("13d ago");
  });

  it("falls back to YYYY-MM-DD beyond 14 days", () => {
    const long = T - 100 * 86_400_000;
    // The exact date depends on timezone of the test machine, so just
    // verify it matches the YYYY-MM-DD shape.
    expect(fmtRel(new Date(long).toISOString(), T)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("handles future timestamps with 'in X' phrasing", () => {
    expect(fmtRel(new Date(T + 90_000).toISOString(), T)).toBe("in 1m");
    expect(fmtRel(new Date(T + 5 * 3_600_000).toISOString(), T)).toBe("in 5h");
  });
});
