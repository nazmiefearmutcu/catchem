import { describe, it, expect, expectTypeOf } from "vitest";
import { deriveLinesPerMinute } from "@/features/logs/LogsPage";
import { api } from "@/lib/api";
import type { QuantMember } from "@/lib/api";
import type {
  FinancialRecord,
  FinancialRecordSummary,
  LogTail,
} from "@/types/api";

/**
 * Round-4 frontend bug-hunt regression pins.
 *
 *   F2 (logic)   — LogsPage rate KPI must diff `total_lines` (monotonic
 *                  full-file count), NOT `lines.length`, which pins at the
 *                  1000-line tail cap and stalls the rate at 0 forever.
 *   F1 (contract)— compact list endpoints return FinancialRecordSummary[],
 *                  and a detail-only field is NOT readable off a summary row.
 *   F3 (contract)— QuantMember.sentiment_label accepts "unknown".
 */

// ════════════════════════════════════════════════════════════════════════
// F2 — rate uses total_lines so a file growing past the tail cap still
// reports a non-zero rate.
// ════════════════════════════════════════════════════════════════════════
describe("LogsPage rate — total_lines vs lines.length (finding 2)", () => {
  // Mirror the exact two-step sampler the LogsPage effect runs: first call
  // seeds the baseline (rate stays 0), second call diffs against it. We feed
  // it the field the page is supposed to feed it.
  function sampleRate(
    sampleA: { count: number; tsMs: number },
    sampleB: { count: number; tsMs: number },
  ): number {
    // first sample seeds (ts === 0 guard) → rate 0
    const baseline = { count: sampleA.count, ts: sampleA.tsMs, rate: 0 };
    // second sample diffs
    return deriveLinesPerMinute(
      baseline.count,
      baseline.ts,
      sampleB.count,
      sampleB.tsMs,
    );
  }

  it("once the file exceeds the 1000-line tail cap, lines.length pins → rate stalls at 0 (the bug)", () => {
    // lines.length is capped at 1000 on both ticks even though the file grew.
    const buggy = sampleRate(
      { count: 1000, tsMs: 1_000 },
      { count: 1000, tsMs: 61_000 },
    );
    expect(buggy).toBe(0);
  });

  it("feeding total_lines keeps the diff growing → non-zero rate (the fix)", () => {
    // total_lines grew 1000 → 1120 across a 60s window: 120 lines/min.
    const fixed = sampleRate(
      { count: 1000, tsMs: 1_000 },
      { count: 1120, tsMs: 61_000 },
    );
    expect(fixed).toBeGreaterThan(0);
    expect(fixed).toBeCloseTo(120, 5);
  });

  it("LogsPage source feeds logs.data.total_lines into the rate sampler, not lines.length", () => {
    // The source is verified structurally so the test fails if a refactor
    // reverts the consumer back to lines.length.
    // (Read relative to this test file so it works from any cwd.)
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { readFileSync } = require("node:fs") as typeof import("node:fs");
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { resolve, dirname } = require("node:path") as typeof import("node:path");
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { fileURLToPath } = require("node:url") as typeof import("node:url");
    const here = dirname(fileURLToPath(import.meta.url));
    const src = readFileSync(
      resolve(here, "../features/logs/LogsPage.tsx"),
      "utf8",
    );
    // total_lines is read and fed into deriveLinesPerMinute + rateRef.count.
    expect(src).toMatch(/logs\.data\.total_lines/);
    expect(src).toMatch(/const\s+totalCount\s*=\s*logs\.data\.total_lines/);
    // It must NOT diff lines.length into the rate anymore.
    expect(src).not.toMatch(/deriveLinesPerMinute\([^)]*lines\.length/s);
  });

  it("LogTail type carries total_lines", () => {
    expectTypeOf<LogTail>().toHaveProperty("total_lines");
    expectTypeOf<LogTail["total_lines"]>().toEqualTypeOf<number>();
  });
});

// ════════════════════════════════════════════════════════════════════════
// F1 — compact list endpoints return FinancialRecordSummary[], and reading a
// detail-only field off a summary row is a type error.
// ════════════════════════════════════════════════════════════════════════
describe("list endpoints return FinancialRecordSummary[] (finding 1)", () => {
  it("recent / bySymbol / byAssetClass / byReason / recordsByTag are typed to summaries", () => {
    expectTypeOf(api.recent).returns.resolves.toEqualTypeOf<{
      items: FinancialRecordSummary[];
    }>();
    expectTypeOf(api.bySymbol).returns.resolves.toEqualTypeOf<{
      items: FinancialRecordSummary[];
    }>();
    expectTypeOf(api.byAssetClass).returns.resolves.toEqualTypeOf<{
      items: FinancialRecordSummary[];
    }>();
    expectTypeOf(api.byReason).returns.resolves.toEqualTypeOf<{
      items: FinancialRecordSummary[];
    }>();
    expectTypeOf(api.recordsByTag).returns.resolves.toEqualTypeOf<{
      items: FinancialRecordSummary[];
    }>();
    // The full-record detail endpoint is unchanged.
    expectTypeOf(api.record).returns.resolves.toEqualTypeOf<FinancialRecord>();
  });

  it("reading a detail-only field off a summary row is a compile error", () => {
    const summary = {} as FinancialRecordSummary;
    // Summary-present fields read fine.
    expectTypeOf(summary.capture_id).toEqualTypeOf<string>();
    expectTypeOf(summary.evidence_preview).toEqualTypeOf<string | null>();
    // Detail-only fields are absent from the summary shape.
    // @ts-expect-error evidence_sentences is detail-only, not on a summary row
    void summary.evidence_sentences;
    // @ts-expect-error reason_text is detail-only, not on a summary row
    void summary.reason_text;
    // @ts-expect-error component_scores is detail-only, not on a summary row
    void summary.component_scores;
    expect(true).toBe(true);
  });
});

// ════════════════════════════════════════════════════════════════════════
// F3 — QuantMember.sentiment_label accepts "unknown".
// ════════════════════════════════════════════════════════════════════════
describe("QuantMember.sentiment_label widened to include 'unknown' (finding 3)", () => {
  it("a member with sentiment_label='unknown' type-checks", () => {
    const member: QuantMember = {
      capture_id: "cap-unknown",
      title: "Ambiguous headline",
      domain: "example.com",
      url: null,
      published_ts: null,
      finance_relevance_score: 0.42,
      sentiment_label: "unknown",
      asset_classes: [],
      impact_reason_codes: [],
      candidate_symbols: [],
    };
    expect(member.sentiment_label).toBe("unknown");
    expectTypeOf<QuantMember["sentiment_label"]>().toEqualTypeOf<
      "positive" | "neutral" | "negative" | "unknown" | null
    >();
  });
});
