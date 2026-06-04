"""Dedicated unit tests for the reviewers subpackage internals.

Targets three modules left near-0% by the existing integration-flavored
``test_reviewers_module.py`` (which drives the DeepSeek client, sampling,
budget guard, and agreement helpers but never touches these directly):

  * ``catchem.reviewers.base``   — ``ReviewPayload`` shaping, the
    ``record_to_review_payload`` projection, and ``ReviewerError``.
  * ``catchem.reviewers.stub``   — the offline ``StubReviewer`` adapter:
    composite ``reviewer_version`` + projection of a pipeline record.
  * ``catchem.reviewers.prompts``— ``build_user_prompt`` template filling
    and the static system / schema constants.

Everything here is deterministic and offline: the StubReviewer is driven
by a duck-typed fake ``CatchemService`` so no ML stack, SQLite, or network
is involved, and prompts are built against the bundled taxonomy.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from catchem.reviewers.base import (
    REVIEWER_DEEPSEEK,
    REVIEWER_STUB,
    ReviewerError,
    ReviewPayload,
    record_to_review_payload,
)
from catchem.reviewers.prompts import (
    JSON_SCHEMA_HINT,
    SYSTEM_INSTRUCTION,
    build_user_prompt,
)
from catchem.reviewers.stub import StubReviewer
from catchem.schemas import (
    AwarenessCaptureView,
    FinancialImpactRecord,
    ProcessingMode,
    SentimentLabel,
)
from catchem.taxonomy import default_taxonomy_path, load_taxonomy

# ── shared deterministic builders ─────────────────────────────────────────


def _payload(**overrides) -> ReviewPayload:
    base = dict(
        capture_id="cap-1",
        reviewer_id=REVIEWER_STUB,
        reviewer_version="stub-v1",
        is_finance_relevant=True,
        finance_relevance_score=0.75,
        asset_classes=("equities", "rates"),
        impact_reason_codes=("earnings",),
        candidate_symbols=("AAPL",),
        sentiment_label="positive",
        sentiment_score=0.6,
        evidence_sentences=("Apple beat estimates.",),
        reason_text="Earnings beat moves the stock.",
    )
    base.update(overrides)
    return ReviewPayload(**base)


def _record(**overrides) -> FinancialImpactRecord:
    base = dict(
        capture_id="cap-rec-1",
        doc_id="doc-rec-1",
        title="Apple beats earnings expectations",
        text_excerpt="Apple reported strong quarterly earnings, beating analyst estimates.",
        domain="reuters.com",
        url="https://reuters.com/apple-earnings",
        is_finance_relevant=True,
        finance_relevance_score=0.81,
        asset_classes=["equities"],
        impact_reason_codes=["earnings"],
        candidate_symbols=["AAPL"],
        sentiment_label=SentimentLabel.POSITIVE,
        sentiment_score=0.72,
        evidence_sentences=["Apple reported strong quarterly earnings."],
        reason_text="Direct earnings beat for a tradeable equity.",
        processing_mode=ProcessingMode.REPLAY_EXISTING,
    )
    base.update(overrides)
    return FinancialImpactRecord(**base)


class _FakeService:
    """Duck-typed stand-in for ``CatchemService``.

    ``StubReviewer`` only reads ``model_versions`` and calls ``process``;
    this lets us exercise the adapter without the ML pipeline or storage.
    """

    def __init__(self, record: FinancialImpactRecord, model_versions: dict[str, str]):
        self._record = record
        self.model_versions = model_versions
        self.seen: list[AwarenessCaptureView] = []

    def process(self, cap: AwarenessCaptureView) -> FinancialImpactRecord:
        self.seen.append(cap)
        return self._record


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy(default_taxonomy_path())


# ══════════════════════════════════════════════════════════════════════════
# base.py
# ══════════════════════════════════════════════════════════════════════════


class TestReviewerError:
    def test_code_and_message_exposed(self):
        err = ReviewerError("rate_limit", "slow down, partner")
        assert err.code == "rate_limit"
        assert err.message == "slow down, partner"

    def test_str_includes_code_prefix(self):
        err = ReviewerError("auth", "bad key")
        assert str(err) == "auth: bad key"

    def test_is_an_exception(self):
        with pytest.raises(ReviewerError) as exc:
            raise ReviewerError("timeout", "took too long")
        assert exc.value.code == "timeout"


class TestReviewerIdConstants:
    def test_canonical_ids(self):
        # External-contract tokens — the compare-page join keys off these.
        assert REVIEWER_STUB == "stub"
        assert REVIEWER_DEEPSEEK == "deepseek"


class TestReviewPayloadStorageRow:
    def test_storage_row_keys_and_types(self):
        row = _payload(input_tokens=10, output_tokens=20, usd_cost=0.5, latency_ms=42).to_storage_row()
        assert set(row) == {
            "capture_id",
            "reviewer_id",
            "reviewer_version",
            "payload_json",
            "input_tokens",
            "output_tokens",
            "usd_cost",
            "latency_ms",
            "created_at",
            "error_code",
        }
        assert isinstance(row["input_tokens"], int)
        assert isinstance(row["output_tokens"], int)
        assert isinstance(row["usd_cost"], float)
        assert isinstance(row["latency_ms"], int)
        assert row["reviewer_id"] == REVIEWER_STUB

    def test_storage_row_coerces_numeric_strings(self):
        # Meta arrives as ints/floats normally, but to_storage_row hard-casts
        # so a sloppy caller can't poison the column type.
        row = _payload(input_tokens="7", output_tokens="3", usd_cost="0.25", latency_ms="9").to_storage_row()
        assert row["input_tokens"] == 7
        assert row["output_tokens"] == 3
        assert row["usd_cost"] == pytest.approx(0.25)
        assert row["latency_ms"] == 9

    def test_error_code_passthrough(self):
        row = _payload(error_code="bad_json").to_storage_row()
        assert row["error_code"] == "bad_json"

    def test_payload_json_round_trips(self):
        row = _payload().to_storage_row()
        # The nested payload must survive a JSON serialize/parse cycle intact.
        restored = json.loads(json.dumps(row["payload_json"]))
        assert restored["is_finance_relevant"] is True
        assert restored["finance_relevance_score"] == pytest.approx(0.75)
        assert restored["asset_classes"] == ["equities", "rates"]
        assert restored["impact_reason_codes"] == ["earnings"]
        assert restored["candidate_symbols"] == ["AAPL"]
        assert restored["sentiment_label"] == "positive"
        assert restored["sentiment_score"] == pytest.approx(0.6)
        assert restored["evidence_sentences"] == ["Apple beat estimates."]
        assert restored["reason_text"] == "Earnings beat moves the stock."
        assert restored["raw"] is None

    def test_tuples_become_json_lists(self):
        # Frozen dataclass stores tuples; JSON payload must use lists.
        pj = _payload()._payload_for_json()
        assert isinstance(pj["asset_classes"], list)
        assert isinstance(pj["impact_reason_codes"], list)
        assert isinstance(pj["candidate_symbols"], list)
        assert isinstance(pj["evidence_sentences"], list)

    def test_none_sentiment_score_stays_none(self):
        pj = _payload(sentiment_label=None, sentiment_score=None)._payload_for_json()
        assert pj["sentiment_label"] is None
        assert pj["sentiment_score"] is None

    def test_raw_response_preserved(self):
        raw = {"choices": [{"message": {"content": "{}"}}]}
        pj = _payload(raw_response=raw)._payload_for_json()
        assert pj["raw"] == raw

    def test_created_at_default_is_iso_string(self):
        p = _payload()
        # Default factory yields a parseable ISO-8601 timestamp.
        assert isinstance(p.created_at, str)
        datetime.fromisoformat(p.created_at)

    def test_meta_defaults_are_zero(self):
        p = ReviewPayload(
            capture_id="c",
            reviewer_id=REVIEWER_STUB,
            reviewer_version="v",
            is_finance_relevant=False,
            finance_relevance_score=0.0,
            asset_classes=(),
            impact_reason_codes=(),
            candidate_symbols=(),
            sentiment_label=None,
            sentiment_score=None,
            evidence_sentences=(),
        )
        assert p.input_tokens == 0
        assert p.output_tokens == 0
        assert p.usd_cost == 0.0
        assert p.latency_ms == 0
        assert p.error_code is None
        assert p.raw_response is None


class TestRecordToReviewPayload:
    def test_projects_all_structural_fields(self):
        rec = _record()
        payload = record_to_review_payload(rec, reviewer_id=REVIEWER_STUB, reviewer_version="stub-v9")
        assert payload.capture_id == rec.capture_id
        assert payload.reviewer_id == REVIEWER_STUB
        assert payload.reviewer_version == "stub-v9"
        assert payload.is_finance_relevant is True
        assert payload.finance_relevance_score == pytest.approx(0.81)
        assert payload.asset_classes == ("equities",)
        assert payload.impact_reason_codes == ("earnings",)
        assert payload.candidate_symbols == ("AAPL",)
        assert payload.evidence_sentences == ("Apple reported strong quarterly earnings.",)
        assert payload.reason_text == "Direct earnings beat for a tradeable equity."

    def test_collections_become_tuples(self):
        payload = record_to_review_payload(_record(), reviewer_id=REVIEWER_STUB, reviewer_version="v")
        assert isinstance(payload.asset_classes, tuple)
        assert isinstance(payload.impact_reason_codes, tuple)
        assert isinstance(payload.candidate_symbols, tuple)
        assert isinstance(payload.evidence_sentences, tuple)

    def test_sentiment_label_serialized_to_value(self):
        # The record carries a SentimentLabel enum; the payload stores its
        # str value so JSON export matches the DeepSeek reviewer's plain str.
        payload = record_to_review_payload(_record(), reviewer_id=REVIEWER_STUB, reviewer_version="v")
        assert payload.sentiment_label == "positive"
        assert payload.sentiment_score == pytest.approx(0.72)

    def test_none_sentiment_score_preserved(self):
        rec = _record(sentiment_label=None, sentiment_score=None)
        payload = record_to_review_payload(rec, reviewer_id=REVIEWER_STUB, reviewer_version="v")
        assert payload.sentiment_label is None
        assert payload.sentiment_score is None

    def test_meta_defaults_zero_for_inprocess(self):
        # In-process reviewer has no token budget — meta must be zeroed.
        payload = record_to_review_payload(_record(), reviewer_id=REVIEWER_STUB, reviewer_version="v")
        assert payload.input_tokens == 0
        assert payload.output_tokens == 0
        assert payload.usd_cost == 0.0
        assert payload.latency_ms == 0

    def test_not_finance_relevant_record(self):
        rec = _record(
            is_finance_relevant=False,
            finance_relevance_score=0.05,
            asset_classes=[],
            impact_reason_codes=[],
            candidate_symbols=[],
        )
        payload = record_to_review_payload(rec, reviewer_id=REVIEWER_STUB, reviewer_version="v")
        assert payload.is_finance_relevant is False
        assert payload.asset_classes == ()
        assert payload.impact_reason_codes == ()
        assert payload.candidate_symbols == ()


# ══════════════════════════════════════════════════════════════════════════
# stub.py
# ══════════════════════════════════════════════════════════════════════════


class TestStubReviewer:
    def test_reviewer_id_is_stub(self):
        svc = _FakeService(_record(), {"zero_shot": "zs-v1"})
        assert StubReviewer(svc).reviewer_id == REVIEWER_STUB

    def test_version_is_sorted_pipe_joined(self):
        svc = _FakeService(
            _record(),
            {"zero_shot": "zs-v2", "sentiment": "sent-v1", "embedding": "emb-v3"},
        )
        version = StubReviewer(svc).reviewer_version
        # Sorted by "k=v" string so the compare page detects component drift.
        assert version == "embedding=emb-v3|sentiment=sent-v1|zero_shot=zs-v2"

    def test_version_empty_versions_fallback(self):
        svc = _FakeService(_record(), {})
        assert StubReviewer(svc).reviewer_version == "stub-empty"

    def test_review_projects_record(self):
        rec = _record(capture_id="cap-stub-7")
        svc = _FakeService(rec, {"zero_shot": "zs-v1"})
        reviewer = StubReviewer(svc)
        cap = AwarenessCaptureView(
            capture_id="cap-stub-7",
            doc_id="doc-stub-7",
            title="Apple beats earnings expectations",
            text="Apple reported strong quarterly earnings.",
            domain="reuters.com",
        )
        payload = reviewer.review(cap)
        # Adapter delegated to the service exactly once with our capture.
        assert svc.seen == [cap]
        # Payload carries the canonical reviewer id + composite version.
        assert payload.reviewer_id == REVIEWER_STUB
        assert payload.reviewer_version == "zero_shot=zs-v1"
        # And the structural projection of the record.
        assert payload.capture_id == "cap-stub-7"
        assert payload.is_finance_relevant is True
        assert payload.asset_classes == ("equities",)
        assert payload.sentiment_label == "positive"
        # In-process meta stays zero.
        assert payload.usd_cost == 0.0
        assert payload.input_tokens == 0

    def test_review_output_is_storage_serializable(self):
        svc = _FakeService(_record(), {"zero_shot": "zs-v1"})
        cap = AwarenessCaptureView(
            capture_id="cap-rec-1",
            doc_id="doc-rec-1",
            text="body",
        )
        row = StubReviewer(svc).review(cap).to_storage_row()
        # End-to-end: the projected payload must produce a JSON-safe row.
        json.dumps(row["payload_json"])
        assert row["reviewer_id"] == REVIEWER_STUB
        assert row["error_code"] is None


# ══════════════════════════════════════════════════════════════════════════
# prompts.py
# ══════════════════════════════════════════════════════════════════════════


class TestSystemConstants:
    def test_system_instruction_mentions_strict_json(self):
        assert "STRICT JSON" in SYSTEM_INSTRUCTION
        assert "taxonomy" in SYSTEM_INSTRUCTION.lower()

    def test_schema_hint_lists_every_payload_key(self):
        for key in (
            "is_finance_relevant",
            "finance_relevance_score",
            "asset_classes",
            "impact_reason_codes",
            "candidate_symbols",
            "sentiment_label",
            "sentiment_score",
            "evidence_sentences",
            "reason_text",
        ):
            assert key in JSON_SCHEMA_HINT

    def test_schema_hint_is_stripped(self):
        assert JSON_SCHEMA_HINT == JSON_SCHEMA_HINT.strip()


class TestBuildUserPrompt:
    def test_includes_taxonomy_allow_lists(self, taxonomy):
        prompt = build_user_prompt(
            taxonomy=taxonomy,
            title="Fed cuts rates",
            body="The Federal Reserve cut rates.",
            domain="reuters.com",
            url="https://reuters.com/x",
        )
        assert "<allowed_asset_classes>" in prompt
        assert "</allowed_asset_classes>" in prompt
        assert "<allowed_reason_codes>" in prompt
        assert "</allowed_reason_codes>" in prompt
        # Every taxonomy id must appear in its allow-list block.
        for aid in taxonomy.asset_class_ids:
            assert aid in prompt
        for rid in taxonomy.reason_code_ids:
            assert rid in prompt

    def test_asset_ids_sorted_in_block(self, taxonomy):
        prompt = build_user_prompt(taxonomy=taxonomy, title="t", body="b", domain="d", url="u")
        expected = ", ".join(sorted(taxonomy.asset_class_ids))
        assert expected in prompt

    def test_embeds_article_context(self, taxonomy):
        prompt = build_user_prompt(
            taxonomy=taxonomy,
            title="Fed cuts rates",
            body="The Federal Reserve cut rates by 25bps.",
            domain="reuters.com",
            url="https://reuters.com/fed",
        )
        assert "title: Fed cuts rates" in prompt
        assert "domain: reuters.com" in prompt
        assert "url: https://reuters.com/fed" in prompt
        assert "The Federal Reserve cut rates by 25bps." in prompt
        assert "<article>" in prompt and "</article>" in prompt

    def test_embeds_schema_hint(self, taxonomy):
        prompt = build_user_prompt(taxonomy=taxonomy, title="t", body="b", domain="d", url="u")
        assert JSON_SCHEMA_HINT in prompt

    def test_none_fields_get_placeholders(self, taxonomy):
        prompt = build_user_prompt(taxonomy=taxonomy, title=None, body="body text", domain=None, url=None)
        assert "title: (untitled)" in prompt
        assert "domain: (unknown)" in prompt
        assert "url: (unknown)" in prompt
        # No raw "None" should leak into the prompt for the optional fields.
        assert "title: None" not in prompt
        assert "domain: None" not in prompt
        assert "url: None" not in prompt

    def test_body_clipped_to_6000_chars(self, taxonomy):
        long_body = "x" * 10_000
        prompt = build_user_prompt(taxonomy=taxonomy, title="t", body=long_body, domain="d", url="u")
        # The 6KB clip keeps token cost bounded; the 6001st 'x' run must be gone.
        assert "x" * 6000 in prompt
        assert "x" * 6001 not in prompt

    def test_short_body_not_truncated(self, taxonomy):
        prompt = build_user_prompt(taxonomy=taxonomy, title="t", body="short body", domain="d", url="u")
        assert "short body" in prompt

    def test_prompt_is_str(self, taxonomy):
        prompt = build_user_prompt(taxonomy=taxonomy, title="t", body="b", domain="d", url="u")
        assert isinstance(prompt, str)


class TestReviewerProtocol:
    def test_protocol_method_is_callable(self):
        from catchem.reviewers.base import Reviewer

        assert Reviewer.review(None, None) is None


def test_deepseek_private_helpers() -> None:
    from catchem.reviewers.deepseek import _as_str_list, _clean_text

    # _clean_text empty/None case
    assert _clean_text("") == ""
    assert _clean_text(None) == ""

    # _as_str_list limit break and type/empty filtering
    val = ["a", "b", 42, 3.14, None, "", "c", {}, []]
    assert _as_str_list(val, max_items=2) == ["a", "b"]
    assert _as_str_list(val, max_items=4) == ["a", "b", "42", "3.14"]
    assert _as_str_list(val) == ["a", "b", "42", "3.14", "c"]
    assert _as_str_list(None) == []
    assert _as_str_list("not a list") == []
