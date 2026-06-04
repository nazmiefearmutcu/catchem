import { describe, it, expect } from "vitest";
import { mergeByCaptureId, newCaptureIds, isIncompleteRecord, stableRecordSort } from "@/lib/feedMerge";

describe("feedMerge", () => {
  const a = { capture_id: "a", published_ts: "2026-05-16T10:00:00Z" };
  const b = { capture_id: "b", published_ts: "2026-05-16T11:00:00Z" };
  const c = { capture_id: "c", published_ts: "2026-05-16T09:00:00Z" };

  it("dedupes by capture_id and prefers the newer copy", () => {
    const old = [a, b];
    const newer = [{ capture_id: "a", published_ts: "2026-05-16T12:00:00Z" }];
    const merged = mergeByCaptureId(old, newer);
    expect(merged.map((r) => r.capture_id)).toEqual(["a", "b"]);
    expect(merged[0].published_ts).toBe("2026-05-16T12:00:00Z");
  });

  it("sorts newer first, stable on tie", () => {
    const merged = mergeByCaptureId([], [a, b, c]);
    expect(merged.map((r) => r.capture_id)).toEqual(["b", "a", "c"]);
  });

  it("identifies new capture_ids since baseline", () => {
    const baseline = [a, b];
    const incoming = [a, b, c, { capture_id: "d", published_ts: null }];
    expect(newCaptureIds(baseline, incoming).sort()).toEqual(["c", "d"].sort());
  });

  it("flags incomplete records", () => {
    expect(isIncompleteRecord({ capture_id: "", doc_id: "x" })).toBe(true);
    expect(isIncompleteRecord({ capture_id: "x", doc_id: "" })).toBe(true);
    expect(isIncompleteRecord({ capture_id: "x", doc_id: "y" })).toBe(false);
  });

  it("falls back to created_at when published_ts missing", () => {
    const x = { capture_id: "x", published_ts: null, created_at: "2026-05-16T13:00:00Z" };
    const y = { capture_id: "y", published_ts: null, created_at: "2026-05-16T08:00:00Z" };
    const arr = [x, y].sort(stableRecordSort);
    expect(arr[0].capture_id).toBe("x");
  });

  it("does not crash on empty inputs", () => {
    expect(mergeByCaptureId(undefined, undefined)).toEqual([]);
    expect(mergeByCaptureId([], [])).toEqual([]);
  });
});
