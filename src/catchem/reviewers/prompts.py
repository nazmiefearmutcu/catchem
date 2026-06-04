"""Prompt + JSON schema for the DeepSeek reviewer.

The prompt locks the model onto the catchem taxonomy and the
`ReviewPayload` shape so the parse step in `deepseek.py` is a strict
allow-list filter, not a heuristic.
"""

from __future__ import annotations

from ..taxonomy import Taxonomy

SYSTEM_INSTRUCTION = (
    "You are a financial-news classifier embedded in Catchem, a local-first "
    "news-impact analyst. For each article, return a STRICT JSON object that "
    "matches the schema below. Use ONLY taxonomy IDs listed in the "
    "<allowed_*> blocks — never invent labels. Be conservative: prefer fewer "
    "high-confidence labels to many low-confidence ones. Evidence sentences "
    "must be verbatim spans copied from the article body (no paraphrasing)."
)

JSON_SCHEMA_HINT = """
Return JSON shaped exactly like:
{
  "is_finance_relevant": true | false,
  "finance_relevance_score": 0.0..1.0,
  "asset_classes": ["<id from allowed_asset_classes>", ...],
  "impact_reason_codes": ["<id from allowed_reason_codes>", ...],
  "candidate_symbols": ["AAPL", "BTC-USD", ...],          // tickers ONLY, max 8
  "sentiment_label": "positive" | "neutral" | "negative",
  "sentiment_score": 0.0..1.0,
  "evidence_sentences": ["verbatim sentence", ...],       // max 3, copied as-is
  "reason_text": "one-sentence rationale for the relevance call"
}
""".strip()


def build_user_prompt(
    *,
    taxonomy: Taxonomy,
    title: str | None,
    body: str,
    domain: str | None,
    url: str | None,
) -> str:
    """Build the user-side prompt: taxonomy allow-list + article context."""
    asset_ids = ", ".join(sorted(taxonomy.asset_class_ids))
    reason_ids = ", ".join(sorted(taxonomy.reason_code_ids))
    # The body is clipped to ~6KB so we stay well under the deepseek-chat
    # 128K context window AND keep token cost predictable per call. The
    # 6KB clip matches the catchem text_excerpt convention.
    clipped_body = body[:6000]
    return "\n".join(
        [
            "<allowed_asset_classes>",
            asset_ids,
            "</allowed_asset_classes>",
            "",
            "<allowed_reason_codes>",
            reason_ids,
            "</allowed_reason_codes>",
            "",
            JSON_SCHEMA_HINT,
            "",
            "<article>",
            f"title: {title or '(untitled)'}",
            f"domain: {domain or '(unknown)'}",
            f"url: {url or '(unknown)'}",
            "body:",
            clipped_body,
            "</article>",
        ]
    )
