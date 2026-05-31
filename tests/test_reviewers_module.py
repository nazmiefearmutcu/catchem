"""Reviewers module: sampling determinism, cost guard, agreement helpers, DeepSeek client parse path."""

from __future__ import annotations

import json

import pytest

from catchem.api import _compute_agreement, _compute_compare_summary
from catchem.reviewers import (
    REVIEWER_DEEPSEEK,
    REVIEWER_STUB,
    DeepSeekReviewer,
    ReviewerError,
    ReviewPayload,
)
from catchem.schemas import AwarenessCaptureView
from catchem.settings import Settings
from catchem.supervisor import Supervisor
from catchem.taxonomy import default_taxonomy_path, load_taxonomy

# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Settings pinned to a temp dir so each test gets a clean SQLite."""
    monkeypatch.setenv("CATCHEM_NEWS__POLLER_ENABLED", "false")
    monkeypatch.setenv("CATCHEM_ARCHIVE__ENABLED", "false")
    s = Settings()
    s.paths.catchem_output_dir = tmp_path
    s.storage.sqlite_url = f"sqlite:///{tmp_path}/catchem.sqlite3"
    return s


@pytest.fixture
def supervisor(temp_settings):
    sup = Supervisor(temp_settings)
    yield sup
    sup.close()


@pytest.fixture
def capture():
    return AwarenessCaptureView(
        capture_id="cap-abc-001",
        doc_id="doc-001",
        title="Fed cuts rates by 25 bps to combat slowing growth",
        text="The Federal Reserve cut rates by 25 basis points on Wednesday, citing slowing economic growth.",
        domain="reuters.com",
        url="https://reuters.com/fed-cuts",
    )


# ── sampling determinism ──────────────────────────────────────────────────


class TestSamplingDeterminism:
    def test_same_capture_id_same_decision(self, supervisor):
        """Identical capture_id MUST produce the same sampling outcome."""
        # Enable DeepSeek to bypass the disabled-shortcut path.
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.sampling_rate = 0.5

        decisions = [
            supervisor.reviewers.should_sample_for_deepseek("cap-same") for _ in range(5)
        ]
        assert len(set(decisions)) == 1

    def test_rate_zero_never_samples(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.sampling_rate = 0.0
        for i in range(20):
            assert not supervisor.reviewers.should_sample_for_deepseek(f"cap-{i}")

    def test_rate_one_always_samples(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.sampling_rate = 1.0
        for i in range(20):
            assert supervisor.reviewers.should_sample_for_deepseek(f"cap-{i}")

    def test_rate_distribution_matches_target(self, supervisor):
        """10% sampling should give ~10% of a 1000-capture pool."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.sampling_rate = 0.1
        sampled = sum(
            1
            for i in range(1000)
            if supervisor.reviewers.should_sample_for_deepseek(f"cap-{i:04d}")
        )
        # SHA-256 is uniform — expect 100 ± reasonable jitter.
        assert 70 <= sampled <= 130

    def test_disabled_blocks_sampling(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = False
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.sampling_rate = 1.0
        assert not supervisor.reviewers.should_sample_for_deepseek("cap-x")

    def test_missing_key_blocks_sampling(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = ""
        supervisor.settings.reviewers.deepseek.sampling_rate = 1.0
        assert not supervisor.reviewers.should_sample_for_deepseek("cap-x")


# ── cost guard ────────────────────────────────────────────────────────────


class TestBudgetGuard:
    def test_under_cap_allows_sampling(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.usd_cap = 1.0
        # Synthetic spend of $0.50 — still under cap.
        supervisor.reviewers.add_spend(0.50)
        assert not supervisor.reviewers.budget_state().exhausted
        # Should not block sampling.
        supervisor.settings.reviewers.deepseek.sampling_rate = 1.0
        assert supervisor.reviewers.should_sample_for_deepseek("cap-test")

    def test_at_cap_blocks_sampling(self, supervisor):
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        supervisor.settings.reviewers.deepseek.usd_cap = 1.0
        supervisor.settings.reviewers.deepseek.sampling_rate = 1.0
        supervisor.reviewers.add_spend(1.0)
        assert supervisor.reviewers.budget_state().exhausted
        assert not supervisor.reviewers.should_sample_for_deepseek("cap-test")

    def test_over_cap_clamps_remaining_at_zero(self, supervisor):
        supervisor.settings.reviewers.deepseek.usd_cap = 1.0
        supervisor.reviewers.add_spend(2.5)
        state = supervisor.reviewers.budget_state()
        assert state.exhausted
        assert state.remaining_usd == 0.0

    def test_spend_persists_via_storage(self, supervisor, capture):
        """Cumulative spend should reload from storage even after registry restart."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"
        # Insert a synthetic review row with cost so storage.sum_review_cost > 0.
        payload = ReviewPayload(
            capture_id="cap-spent",
            reviewer_id=REVIEWER_DEEPSEEK,
            reviewer_version="deepseek-chat|prompt-v1",
            is_finance_relevant=True,
            finance_relevance_score=0.8,
            asset_classes=("equities",),
            impact_reason_codes=("earnings",),
            candidate_symbols=("AAPL",),
            sentiment_label="positive",
            sentiment_score=0.7,
            evidence_sentences=(),
            usd_cost=0.0042,
            input_tokens=1500,
            output_tokens=420,
        )
        supervisor.storage.upsert_review(payload.to_storage_row())
        supervisor.reviewers.invalidate_budget_cache()
        state = supervisor.reviewers.budget_state()
        assert state.spent_usd == pytest.approx(0.0042, rel=1e-6)


# ── agreement helpers ─────────────────────────────────────────────────────


class TestAgreement:
    def test_perfect_agreement(self):
        p = {
            "is_finance_relevant": True,
            "finance_relevance_score": 0.7,
            "asset_classes": ["equities"],
            "impact_reason_codes": ["earnings"],
            "candidate_symbols": ["AAPL"],
            "sentiment_label": "positive",
        }
        a = _compute_agreement(p, p)
        assert a["relevance_match"] is True
        assert a["sentiment_match"] is True
        assert a["asset_jaccard"] == 1.0
        assert a["reason_jaccard"] == 1.0
        assert a["symbol_jaccard"] == 1.0
        assert a["score_delta"] == 0.0
        assert a["overall"] == 1.0

    def test_total_disagreement(self):
        a = _compute_agreement(
            {
                "is_finance_relevant": True,
                "finance_relevance_score": 1.0,
                "asset_classes": ["equities"],
                "impact_reason_codes": ["earnings"],
                "candidate_symbols": ["AAPL"],
                "sentiment_label": "positive",
            },
            {
                "is_finance_relevant": False,
                "finance_relevance_score": 0.0,
                "asset_classes": ["crypto"],
                "impact_reason_codes": ["regulation"],
                "candidate_symbols": ["BTC"],
                "sentiment_label": "negative",
            },
        )
        assert a["relevance_match"] is False
        assert a["sentiment_match"] is False
        assert a["asset_jaccard"] == 0.0
        assert a["reason_jaccard"] == 0.0
        # 0 + 0 (1-1.0 score delta) + 0 + 0 + 0 / 5 = 0.0
        assert a["overall"] == 0.0

    def test_empty_lists_count_as_match(self):
        """Vacuous Jaccard (both empty) is 1.0 — matches set-theory convention."""
        p = {"is_finance_relevant": False, "finance_relevance_score": 0.1,
             "asset_classes": [], "impact_reason_codes": [], "candidate_symbols": [],
             "sentiment_label": "neutral"}
        a = _compute_agreement(p, p)
        assert a["asset_jaccard"] == 1.0
        assert a["reason_jaccard"] == 1.0
        assert a["symbol_jaccard"] == 1.0

    def test_summary_aggregates(self):
        items = [
            {
                "agreement": {
                    "relevance_match": True,
                    "sentiment_match": False,
                    "asset_jaccard": 0.5,
                    "reason_jaccard": 0.5,
                    "symbol_jaccard": 1.0,
                    "score_delta": 0.2,
                    "overall": 0.6,
                },
                "deepseek": {"error_code": None},
            },
            {
                "agreement": {
                    "relevance_match": True,
                    "sentiment_match": True,
                    "asset_jaccard": 1.0,
                    "reason_jaccard": 1.0,
                    "symbol_jaccard": 1.0,
                    "score_delta": 0.0,
                    "overall": 1.0,
                },
                "deepseek": {"error_code": "rate_limit"},
            },
        ]
        s = _compute_compare_summary(items)
        assert s["n"] == 2
        assert s["relevance_match_rate"] == 1.0
        assert s["sentiment_match_rate"] == 0.5
        assert s["mean_asset_jaccard"] == 0.75
        assert s["deepseek_errors"] == 1

    def test_summary_empty_safe(self):
        s = _compute_compare_summary([])
        assert s["n"] == 0
        assert s["mean_overall"] == 0.0


# ── DeepSeek client (mock transport) ──────────────────────────────────────


class _MockResponse:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        if isinstance(body, dict):
            self._body = json.dumps(body)
        else:
            self._body = body
        self.text = self._body[:1000]

    def json(self):
        return json.loads(self._body)


class _MockClient:
    def __init__(self, responses: list[_MockResponse]):
        self._queue = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json or {}))
        if not self._queue:
            raise AssertionError("mock client received unexpected extra call")
        return self._queue.pop(0)

    def close(self):
        pass


class TestDeepSeekClient:
    @pytest.fixture
    def taxonomy(self):
        return load_taxonomy(default_taxonomy_path())

    @pytest.fixture
    def cap(self):
        return AwarenessCaptureView(
            capture_id="cap-ds-1",
            doc_id="doc-ds-1",
            title="Fed cuts rates 25bps",
            text="The Federal Reserve cut interest rates by 25 basis points on Wednesday.",
            domain="reuters.com",
        )

    def test_happy_path(self, taxonomy, cap):
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "is_finance_relevant": True,
                                "finance_relevance_score": 0.92,
                                "asset_classes": ["rates", "macro"],
                                "impact_reason_codes": ["central_bank"],
                                "candidate_symbols": [],
                                "sentiment_label": "neutral",
                                "sentiment_score": 0.5,
                                "evidence_sentences": [
                                    "The Federal Reserve cut interest rates by 25 basis points on Wednesday."
                                ],
                                "reason_text": "Direct Fed rate decision impacting rates and macro.",
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 250},
        }
        client = _MockClient([_MockResponse(200, body)])
        reviewer = DeepSeekReviewer(
            api_key="test-key",
            taxonomy=taxonomy,
            client=client,
        )
        payload = reviewer.review(cap)
        assert payload.is_finance_relevant is True
        assert payload.finance_relevance_score == pytest.approx(0.92)
        assert payload.asset_classes == ("rates", "macro")
        assert payload.impact_reason_codes == ("central_bank",)
        assert payload.sentiment_label == "neutral"
        assert payload.input_tokens == 1234
        assert payload.output_tokens == 250
        # Pricing on deepseek-chat: input $0.27/1M, output $1.10/1M
        # = (1234/1M)*0.27 + (250/1M)*1.10 = 0.000333 + 0.000275 ≈ 0.000608
        assert payload.usd_cost == pytest.approx(0.000608, rel=1e-3)

    def test_invented_labels_dropped(self, taxonomy, cap):
        """LLM-invented labels not in taxonomy must be silently filtered."""
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "is_finance_relevant": True,
                                "finance_relevance_score": 0.6,
                                "asset_classes": ["equities", "real_estate"],  # real_estate not in taxonomy
                                "impact_reason_codes": ["earnings", "made_up_reason"],
                                "candidate_symbols": ["AAPL"],
                                "sentiment_label": "positive",
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 800, "completion_tokens": 80},
        }
        client = _MockClient([_MockResponse(200, body)])
        reviewer = DeepSeekReviewer(api_key="test-key", taxonomy=taxonomy, client=client)
        payload = reviewer.review(cap)
        assert "real_estate" not in payload.asset_classes
        assert "equities" in payload.asset_classes
        assert "made_up_reason" not in payload.impact_reason_codes
        assert "earnings" in payload.impact_reason_codes

    def test_401_raises_auth(self, taxonomy, cap):
        client = _MockClient([_MockResponse(401, {"error": {"message": "unauth"}})])
        reviewer = DeepSeekReviewer(api_key="bad-key", taxonomy=taxonomy, client=client)
        with pytest.raises(ReviewerError) as exc:
            reviewer.review(cap)
        assert exc.value.code == "auth"

    def test_429_raises_rate_limit(self, taxonomy, cap):
        client = _MockClient([_MockResponse(429, {"error": "slow down"})])
        reviewer = DeepSeekReviewer(api_key="ok", taxonomy=taxonomy, client=client)
        with pytest.raises(ReviewerError) as exc:
            reviewer.review(cap)
        assert exc.value.code == "rate_limit"

    def test_5xx_raises_upstream(self, taxonomy, cap):
        client = _MockClient([_MockResponse(503, "service unavailable")])
        reviewer = DeepSeekReviewer(api_key="ok", taxonomy=taxonomy, client=client)
        with pytest.raises(ReviewerError) as exc:
            reviewer.review(cap)
        assert exc.value.code == "upstream"

    def test_non_json_content_raises_bad_json(self, taxonomy, cap):
        body = {
            "choices": [{"message": {"content": "not json {{{}}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        }
        client = _MockClient([_MockResponse(200, body)])
        reviewer = DeepSeekReviewer(api_key="ok", taxonomy=taxonomy, client=client)
        with pytest.raises(ReviewerError) as exc:
            reviewer.review(cap)
        assert exc.value.code == "bad_json"

    def test_empty_api_key_raises_at_construct(self, taxonomy):
        with pytest.raises(ReviewerError) as exc:
            DeepSeekReviewer(api_key="", taxonomy=taxonomy)
        assert exc.value.code == "auth"


# ── registry integration ──────────────────────────────────────────────────


class TestRegistryIntegration:
    def test_status_dict_shape(self, supervisor):
        s = supervisor.reviewers.status()
        assert "deepseek_enabled" in s
        assert "deepseek_keyed" in s
        assert "deepseek_ready" in s
        assert "usd_spent" in s
        assert "usd_remaining" in s
        assert "exhausted" in s
        assert "sampling_rate" in s

    def test_run_and_persist_writes_error_row_on_failure(self, supervisor, capture, monkeypatch):
        """Registry catches ReviewerError and writes a row with error_code set."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"

        from catchem.reviewers import deepseek as deepseek_module

        class _FailingClient:
            def post(self, *args, **kwargs):
                return _MockResponse(401, {"error": "bad key"})

            def close(self):
                pass

        monkeypatch.setattr(
            deepseek_module.httpx,
            "Client",
            lambda *a, **kw: _FailingClient(),
        )
        # Force re-init so the failing client gets picked up.
        supervisor.reviewers._deepseek = None  # type: ignore[attr-defined]
        payload = supervisor.reviewers.run_and_persist_deepseek(capture)
        assert payload is not None
        assert payload.error_code == "auth"
        rows = supervisor.storage.get_reviews_for_capture(capture.capture_id)
        assert any(r["error_code"] == "auth" for r in rows)

    def test_run_and_persist_does_not_double_count_spend(self, supervisor, capture, monkeypatch):
        """Regression: a formal DeepSeek review persists its spend ONCE (the
        reviews row). It must not also write a narrative-ledger row, or
        budget_state() double-counts after invalidate_budget_cache() and the
        usd_cap is silently halved (the round-1 durable-ledger regression)."""
        supervisor.settings.reviewers.deepseek.enabled = True
        supervisor.settings.reviewers.deepseek.api_key = "test-key"

        from catchem.reviewers import deepseek as deepseek_module

        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "is_finance_relevant": True,
                                "finance_relevance_score": 0.9,
                                "asset_classes": ["rates"],
                                "impact_reason_codes": ["central_bank"],
                                "candidate_symbols": [],
                                "sentiment_label": "neutral",
                                "sentiment_score": 0.5,
                                "evidence_sentences": ["Fed cut rates."],
                                "reason_text": "Fed decision.",
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100_000, "completion_tokens": 100_000},
        }
        monkeypatch.setattr(
            deepseek_module.httpx,
            "Client",
            lambda *a, **kw: _MockClient([_MockResponse(200, body)]),
        )
        supervisor.reviewers._deepseek = None  # type: ignore[attr-defined]

        payload = supervisor.reviewers.run_and_persist_deepseek(capture)
        assert payload is not None and payload.error_code is None
        cost = payload.usd_cost
        assert cost > 0
        # Cache is correct immediately (in-memory bump).
        assert supervisor.reviewers.budget_state().spent_usd == pytest.approx(cost)
        # After invalidation the cache rebuilds from SQLite — must still be the
        # single cost, NOT 2x. (Pre-fix: reviews row + ledger row → 2*cost.)
        supervisor.reviewers.invalidate_budget_cache()
        assert supervisor.reviewers.budget_state().spent_usd == pytest.approx(cost)

    def test_storage_reviews_table_roundtrip(self, supervisor):
        payload = ReviewPayload(
            capture_id="cap-rt",
            reviewer_id=REVIEWER_STUB,
            reviewer_version="stub-v1",
            is_finance_relevant=True,
            finance_relevance_score=0.5,
            asset_classes=("equities",),
            impact_reason_codes=("earnings",),
            candidate_symbols=(),
            sentiment_label=None,
            sentiment_score=None,
            evidence_sentences=(),
        )
        supervisor.storage.upsert_review(payload.to_storage_row())
        rows = supervisor.storage.get_reviews_for_capture("cap-rt")
        assert len(rows) == 1
        assert rows[0]["reviewer_id"] == REVIEWER_STUB
        assert rows[0]["payload"]["asset_classes"] == ["equities"]
