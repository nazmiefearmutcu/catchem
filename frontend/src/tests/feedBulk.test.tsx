import { describe, it, expect, vi } from "vitest";
import {
  buildExportFilename,
  buildExportPayload,
  clearSelection,
  extractUniqueSymbols,
  extractUrls,
  selectAll,
  selectedRecords,
  toggleSelection,
} from "@/features/feed/bulkSelection";
import type { FinancialRecord } from "@/types/api";

function makeRecord(overrides: Partial<FinancialRecord> = {}): FinancialRecord {
  return {
    capture_id: "cap-1",
    doc_id: "doc-1",
    title: "Headline",
    domain: "example.com",
    language: "en",
    url: "https://example.com/a",
    is_finance_relevant: true,
    finance_relevance_score: 0.7,
    asset_classes: ["equity"],
    impact_reason_codes: ["EARNINGS"],
    candidate_symbols: ["AAPL"],
    candidate_entities: [],
    impact_horizons: [],
    sentiment_label: "positive",
    sentiment_score: 0.4,
    evidence_sentences: [],
    reason_text: null,
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "live",
    model_versions: {},
    published_ts: "2026-05-28T10:00:00Z",
    created_at: "2026-05-28T10:00:01Z",
    ...overrides,
  };
}

describe("bulkSelection: toggleSelection", () => {
  it("adds a missing id and returns a NEW Set reference", () => {
    const prev = new Set<string>(["a"]);
    const next = toggleSelection(prev, "b");
    expect(next).not.toBe(prev);
    expect([...next].sort()).toEqual(["a", "b"]);
  });

  it("removes a present id and returns a NEW Set reference", () => {
    const prev = new Set<string>(["a", "b"]);
    const next = toggleSelection(prev, "a");
    expect(next).not.toBe(prev);
    expect([...next]).toEqual(["b"]);
  });

  it("never mutates the input Set", () => {
    const prev = new Set<string>(["a"]);
    toggleSelection(prev, "a");
    toggleSelection(prev, "b");
    expect([...prev]).toEqual(["a"]);
  });
});

describe("bulkSelection: selectAll + clearSelection", () => {
  it("selectAll returns a Set of every visible capture_id", () => {
    const items = [
      makeRecord({ capture_id: "a" }),
      makeRecord({ capture_id: "b" }),
      makeRecord({ capture_id: "c" }),
    ];
    const set = selectAll(items);
    expect(set.size).toBe(3);
    expect(set.has("a")).toBe(true);
    expect(set.has("b")).toBe(true);
    expect(set.has("c")).toBe(true);
  });

  it("selectAll on empty list returns an empty Set", () => {
    expect(selectAll([]).size).toBe(0);
  });

  it("clearSelection returns an empty Set", () => {
    expect(clearSelection().size).toBe(0);
  });
});

describe("bulkSelection: extractUniqueSymbols", () => {
  it("de-dupes, uppercases, and trims across records", () => {
    const records = [
      makeRecord({ capture_id: "a", candidate_symbols: ["aapl", "MSFT"] }),
      makeRecord({ capture_id: "b", candidate_symbols: [" AAPL ", "googl"] }),
      makeRecord({ capture_id: "c", candidate_symbols: [] }),
    ];
    expect(extractUniqueSymbols(records)).toEqual(["AAPL", "MSFT", "GOOGL"]);
  });

  it("skips empty/whitespace-only symbols", () => {
    const records = [
      makeRecord({ candidate_symbols: ["", "  ", "TSLA"] }),
    ];
    expect(extractUniqueSymbols(records)).toEqual(["TSLA"]);
  });

  it("returns [] when nothing has symbols", () => {
    expect(extractUniqueSymbols([
      makeRecord({ candidate_symbols: [] }),
      makeRecord({ candidate_symbols: [] }),
    ])).toEqual([]);
  });

  it("returns [] for an empty record list", () => {
    expect(extractUniqueSymbols([])).toEqual([]);
  });
});

describe("bulkSelection: extractUrls", () => {
  it("returns all non-empty URLs preserving record order", () => {
    const records = [
      makeRecord({ capture_id: "a", url: "https://a.example.com" }),
      makeRecord({ capture_id: "b", url: null }),
      makeRecord({ capture_id: "c", url: "https://c.example.com" }),
      makeRecord({ capture_id: "d", url: "" }),
    ];
    expect(extractUrls(records)).toEqual([
      "https://a.example.com",
      "https://c.example.com",
    ]);
  });

  it("returns [] when no record has a URL", () => {
    expect(extractUrls([
      makeRecord({ url: null }),
      makeRecord({ url: "" }),
    ])).toEqual([]);
  });
});

describe("bulkSelection: selectedRecords", () => {
  it("filters by Set membership and preserves source order", () => {
    const all = [
      makeRecord({ capture_id: "a" }),
      makeRecord({ capture_id: "b" }),
      makeRecord({ capture_id: "c" }),
    ];
    const picked = selectedRecords(all, new Set(["c", "a"]));
    // Source order, not selection order.
    expect(picked.map((r) => r.capture_id)).toEqual(["a", "c"]);
  });

  it("returns [] when the selection is empty", () => {
    const all = [makeRecord({ capture_id: "a" })];
    expect(selectedRecords(all, new Set())).toEqual([]);
  });
});

describe("bulkSelection: buildExportPayload + buildExportFilename", () => {
  it("payload carries count, items, and an ISO exported_at timestamp", () => {
    const items = [
      makeRecord({ capture_id: "x" }),
      makeRecord({ capture_id: "y" }),
    ];
    const payload = buildExportPayload(items);
    expect(payload.count).toBe(2);
    expect(payload.items).toBe(items);
    // Loose ISO check: ends with Z or has a timezone offset.
    expect(payload.exported_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  });

  it("filename is safe-for-disk: no colons, no dots in stamp section", () => {
    const fixed = new Date("2026-05-28T10:00:00.000Z");
    const name = buildExportFilename(fixed);
    // The substring AFTER the prefix shouldn't contain colons or dots.
    expect(name.startsWith("catchem_selection_")).toBe(true);
    expect(name.endsWith(".json")).toBe(true);
    const stem = name.slice("catchem_selection_".length, -".json".length);
    expect(stem.includes(":")).toBe(false);
    expect(stem.includes(".")).toBe(false);
  });
});

describe("bulkSelection: end-to-end Set workflow", () => {
  it("toggle adds → select all → clear cycles cleanly", () => {
    const items = [
      makeRecord({ capture_id: "a" }),
      makeRecord({ capture_id: "b" }),
      makeRecord({ capture_id: "c" }),
    ];
    let sel = new Set<string>();
    sel = toggleSelection(sel, "a");
    expect(sel.size).toBe(1);
    sel = toggleSelection(sel, "b");
    expect(sel.size).toBe(2);
    // Toggle "a" off
    sel = toggleSelection(sel, "a");
    expect([...sel]).toEqual(["b"]);
    // Select all from a clean state
    sel = selectAll(items);
    expect(sel.size).toBe(3);
    // Clear
    sel = clearSelection();
    expect(sel.size).toBe(0);
  });

  it("Add-to-watchlist pipeline: select → extract → unique uppercase", () => {
    const all = [
      makeRecord({ capture_id: "a", candidate_symbols: ["aapl"] }),
      makeRecord({ capture_id: "b", candidate_symbols: ["msft", " aapl "] }),
      makeRecord({ capture_id: "c", candidate_symbols: ["googl"] }),
    ];
    // User ticks a + b only — c stays out
    const sel = new Set(["a", "b"]);
    const picked = selectedRecords(all, sel);
    expect(picked.map((r) => r.capture_id)).toEqual(["a", "b"]);
    const symbols = extractUniqueSymbols(picked);
    // GOOGL is correctly excluded
    expect(symbols).toEqual(["AAPL", "MSFT"]);
  });
});

// Integration smoke against the bulk-action helper that touches the DOM —
// no need to mount FeedPage; the React wiring is exercised when the suite
// re-runs in app mode. Here we only assert downloadSelection wires the
// anchor click without throwing.
describe("bulkSelection: downloadSelection (DOM)", () => {
  it("creates and revokes a Blob URL exactly once per call", async () => {
    const { downloadSelection } = await import("@/features/feed/bulkSelection");
    const items = [makeRecord({ capture_id: "x" })];
    // jsdom does not ship URL.createObjectURL / revokeObjectURL by default —
    // install stubs (not spies, since `spyOn` requires a property to exist).
    const createMock = vi.fn().mockReturnValue("blob:mock");
    const revokeMock = vi.fn();
    const origCreate = (URL as unknown as { createObjectURL?: unknown }).createObjectURL;
    const origRevoke = (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL;
    (URL as unknown as { createObjectURL: typeof createMock }).createObjectURL = createMock;
    (URL as unknown as { revokeObjectURL: typeof revokeMock }).revokeObjectURL = revokeMock;
    try {
      downloadSelection(items);
      expect(createMock).toHaveBeenCalledTimes(1);
      expect(revokeMock).toHaveBeenCalledTimes(1);
      expect(revokeMock).toHaveBeenCalledWith("blob:mock");
    } finally {
      (URL as unknown as Record<string, unknown>).createObjectURL = origCreate as unknown;
      (URL as unknown as Record<string, unknown>).revokeObjectURL = origRevoke as unknown;
    }
  });
});
