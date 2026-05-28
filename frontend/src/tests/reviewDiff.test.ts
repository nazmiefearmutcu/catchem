import { describe, it, expect } from "vitest";
import { computeReviewDiff } from "@/features/reviews/ReviewsComparePage";
import type { CompareItem, ReviewPayload, ReviewSide } from "@/lib/api";

/**
 * Pure diff math — pins the row-level set diff semantics used by
 * `<ReviewDiffPair>` and `<DiffSummaryStrip>`. Anything that flips a label
 * from "agreed" to "differed" needs a regression here.
 */

function makePayload(p: Partial<ReviewPayload>): ReviewPayload {
  return {
    is_finance_relevant: true,
    finance_relevance_score: 0.5,
    asset_classes: [],
    impact_reason_codes: [],
    candidate_symbols: [],
    sentiment_label: "neutral",
    sentiment_score: 0,
    evidence_sentences: [],
    reason_text: null,
    ...p,
  };
}

function makeSide(p: Partial<ReviewPayload>, reviewerId = "stub"): ReviewSide {
  return {
    capture_id: "cap",
    reviewer_id: reviewerId,
    reviewer_version: "v1",
    created_at: "2026-01-01T00:00:00Z",
    error_code: null,
    payload: makePayload(p),
  };
}

function makeItem(
  stubP: Partial<ReviewPayload>,
  dsP: Partial<ReviewPayload>,
): CompareItem {
  return {
    capture_id: "cap",
    title: null,
    domain: null,
    url: null,
    stub: makeSide(stubP, "stub"),
    deepseek: makeSide(dsP, "deepseek"),
    agreement: {
      relevance_match: true,
      score_delta: 0,
      asset_jaccard: 1,
      reason_jaccard: 1,
      symbol_jaccard: 1,
      sentiment_match: true,
      overall: 1,
    },
  };
}

describe("computeReviewDiff", () => {
  it("partitions asset_classes into added/removed/kept", () => {
    const d = computeReviewDiff(
      makeItem(
        { asset_classes: ["equities", "fx"] },
        { asset_classes: ["equities", "macro"] },
      ),
    );
    expect(d.acAdded).toEqual(["macro"]);
    expect(d.acRemoved).toEqual(["fx"]);
    expect(d.acKept).toEqual(["equities"]);
  });

  it("partitions impact_reason_codes the same way", () => {
    const d = computeReviewDiff(
      makeItem(
        { impact_reason_codes: ["earnings"] },
        { impact_reason_codes: ["earnings", "regulation"] },
      ),
    );
    expect(d.rcAdded).toEqual(["regulation"]);
    expect(d.rcRemoved).toEqual([]);
    expect(d.rcKept).toEqual(["earnings"]);
  });

  it("scoreDelta is deepseek minus stub (sign matters)", () => {
    const d = computeReviewDiff(
      makeItem({ finance_relevance_score: 0.3 }, { finance_relevance_score: 0.8 }),
    );
    expect(d.scoreDelta).toBeCloseTo(0.5, 5);
  });

  it("scoreDelta is negative when DeepSeek scores lower", () => {
    const d = computeReviewDiff(
      makeItem({ finance_relevance_score: 0.8 }, { finance_relevance_score: 0.2 }),
    );
    expect(d.scoreDelta).toBeCloseTo(-0.6, 5);
  });

  it("sentimentChanged flips when labels differ", () => {
    const d = computeReviewDiff(
      makeItem({ sentiment_label: "neutral" }, { sentiment_label: "positive" }),
    );
    expect(d.sentimentChanged).toBe(true);
  });

  it("sentimentChanged is false when labels agree", () => {
    const d = computeReviewDiff(
      makeItem({ sentiment_label: "positive" }, { sentiment_label: "positive" }),
    );
    expect(d.sentimentChanged).toBe(false);
  });

  it("treats empty-vs-non-empty as full-side change (no false 'agreed')", () => {
    const d = computeReviewDiff(
      makeItem({ asset_classes: [] }, { asset_classes: ["crypto"] }),
    );
    expect(d.acAdded).toEqual(["crypto"]);
    expect(d.acRemoved).toEqual([]);
    expect(d.acKept).toEqual([]);
  });

  it("perfectly agreed pair shows no additions, no removals, no sentiment change", () => {
    const d = computeReviewDiff(
      makeItem(
        {
          asset_classes: ["equities"],
          impact_reason_codes: ["earnings"],
          finance_relevance_score: 0.7,
          sentiment_label: "positive",
        },
        {
          asset_classes: ["equities"],
          impact_reason_codes: ["earnings"],
          finance_relevance_score: 0.7,
          sentiment_label: "positive",
        },
      ),
    );
    expect(d.acAdded).toEqual([]);
    expect(d.acRemoved).toEqual([]);
    expect(d.acKept).toEqual(["equities"]);
    expect(d.rcAdded).toEqual([]);
    expect(d.rcRemoved).toEqual([]);
    expect(d.rcKept).toEqual(["earnings"]);
    expect(d.scoreDelta).toBe(0);
    expect(d.sentimentChanged).toBe(false);
  });
});
