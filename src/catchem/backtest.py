"""Lightweight backtest framework for catchem prediction calibration.

Evaluates how well the catchem pipeline's predictions hold up against the
"expert ground truth" we already collect: paired DeepSeek reviews of stub
predictions. No external market data — we re-use the existing reviewer
infrastructure as the ground-truth oracle.

Inputs:
    supervisor.storage.reviews_with_pair("stub", "deepseek", limit=N)
        → list of (stub_row, deepseek_row) tuples; both rows expose a
          JSON-decoded `payload` with `finance_relevance_score` already
          clamped to [0,1] by the reviewer's normalizer.

Outputs (BacktestRun):
    summary
        items_evaluated, mean_abs_error, mean_signed_error, max_abs_error
        — the at-a-glance "how miscalibrated is the stub?" number.
    calibration_bins
        Predictions grouped into 5 quintiles of `predicted_score`. For each
        non-empty bin we record `avg_predicted_score` vs `avg_ground_truth`.
        Perfect calibration ⇒ the two averages match within the bin width.
    relevance_predictions
        First 50 raw (predicted, ground_truth, delta) rows so the UI table
        can show concrete examples without paginating the full sample.

Pure-ish: takes a Supervisor (or anything with a `.storage` that satisfies
`reviews_with_pair`) and returns a dataclass. No I/O of its own — the API
wrapper handles `ran_at` so the result stays trivially mockable.

Empty / single-sided data is handled gracefully: every summary metric
degrades to 0.0, calibration_bins is `[]`, and `items_evaluated` is 0.
The API endpoint still returns a 200 with that empty shape so the UI can
render its "no paired reviews yet" empty state instead of an error card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# Quintile cut points. Closed-open intervals `[low, high)` except the last
# bin which is closed-closed so a perfect 1.0 prediction lands in 0.8-1.0
# instead of being silently dropped. Tests pin both boundary behaviors.
_BIN_EDGES: tuple[tuple[float, float], ...] = (
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.0),
)


class _StorageLike(Protocol):
    """Structural type for the storage handle we lean on."""

    def reviews_with_pair(
        self, reviewer_a: str, reviewer_b: str, limit: int = 500
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]: ...


class _SupervisorLike(Protocol):
    """Just enough surface for the unit tests to bypass the real Supervisor."""

    storage: _StorageLike


@dataclass
class BacktestRun:
    """Result envelope shipped to the API + UI.

    `schema_version` is stamped on the wire so we can evolve the shape
    without breaking the frontend's cached query — bump it whenever a
    field changes meaning.
    """

    schema_version: int = 1
    items_evaluated: int = 0
    relevance_predictions: list[dict[str, Any]] = field(default_factory=list)
    calibration_bins: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, float | int] = field(default_factory=dict)


def _bin_for(score: float) -> tuple[float, float] | None:
    """Return the bin (low, high) that owns this score, or None.

    The last bin is inclusive on the high side so a score of exactly 1.0
    is counted instead of vanishing. Anything outside [0, 1] (shouldn't
    happen — the reviewer normalizer clamps) returns None and is dropped.
    """
    if not 0.0 <= score <= 1.0:
        return None
    for low, high in _BIN_EDGES[:-1]:
        if low <= score < high:
            return (low, high)
    last_low, last_high = _BIN_EDGES[-1]
    if last_low <= score <= last_high:
        return (last_low, last_high)
    return None


def _empty_summary() -> dict[str, float | int]:
    """Zero-valued summary so the UI can always render its tiles."""
    return {
        "items_evaluated": 0,
        "mean_abs_error": 0.0,
        "mean_signed_error": 0.0,
        "max_abs_error": 0.0,
    }


def run_backtest(
    supervisor: _SupervisorLike, sample_size: int = 200
) -> BacktestRun:
    """Evaluate stub-vs-DeepSeek calibration over the last `sample_size` paired rows.

    We pull (stub, deepseek) pairs newest-first via `reviews_with_pair`,
    then drop any pair where either side errored (no usable score) or
    where DeepSeek returned a degenerate 0.0 because it had no content
    to score. Everything that survives the filter goes into both the
    calibration histogram and the raw predictions sample.

    The function is intentionally simple: no scipy, no sklearn. Mean
    absolute error is the headline metric because it's interpretable on
    the same [0,1] axis as the underlying scores — analysts can read
    `MAE = 0.08` as "stub is off by 8 percentage points on average".
    """
    sample_size = max(10, int(sample_size))
    storage = supervisor.storage
    paired = storage.reviews_with_pair("stub", "deepseek", limit=sample_size)

    predictions: list[dict[str, Any]] = []
    for stub_row, ds_row in paired:
        # Skip pairs where either reviewer recorded an error — those rows
        # have a zero-valued payload but the score isn't a real prediction.
        if stub_row.get("error_code") or ds_row.get("error_code"):
            continue
        stub_payload = stub_row.get("payload") or {}
        ds_payload = ds_row.get("payload") or {}
        stub_score = stub_payload.get("finance_relevance_score")
        ds_score = ds_payload.get("finance_relevance_score")
        if stub_score is None or ds_score is None:
            continue
        try:
            stub_score_f = float(stub_score)
            ds_score_f = float(ds_score)
        except (TypeError, ValueError):
            continue
        delta = ds_score_f - stub_score_f
        predictions.append(
            {
                "capture_id": stub_row.get("capture_id") or ds_row.get("capture_id"),
                "predicted_score": stub_score_f,
                "ground_truth_score": ds_score_f,
                "delta": delta,
            }
        )

    if not predictions:
        return BacktestRun(
            items_evaluated=0,
            relevance_predictions=[],
            calibration_bins=[],
            summary=_empty_summary(),
        )

    # Calibration histogram: group by predicted-score quintile, then
    # compare the average predicted score within the bin to the average
    # ground-truth score from DeepSeek. A well-calibrated model has the
    # two averages close to each other (and both close to the bin midpoint).
    calibration_bins: list[dict[str, Any]] = []
    for low, high in _BIN_EDGES:
        in_bin = [
            p for p in predictions
            if _bin_for(float(p["predicted_score"])) == (low, high)
        ]
        if not in_bin:
            continue
        avg_pred = sum(float(p["predicted_score"]) for p in in_bin) / len(in_bin)
        avg_truth = sum(float(p["ground_truth_score"]) for p in in_bin) / len(in_bin)
        calibration_bins.append(
            {
                "bin_low": low,
                "bin_high": high,
                "predicted_count": len(in_bin),
                "avg_predicted_score": round(avg_pred, 6),
                "avg_ground_truth_score": round(avg_truth, 6),
                # Per-bin calibration gap — positive means DeepSeek
                # scores higher than stub for items the stub put in
                # this bin (we're under-confident at this score level).
                "calibration_gap": round(avg_truth - avg_pred, 6),
            }
        )

    n = len(predictions)
    mean_abs = sum(abs(float(p["delta"])) for p in predictions) / n
    mean_signed = sum(float(p["delta"]) for p in predictions) / n
    max_abs = max(abs(float(p["delta"])) for p in predictions)

    summary: dict[str, float | int] = {
        "items_evaluated": n,
        "mean_abs_error": round(mean_abs, 6),
        "mean_signed_error": round(mean_signed, 6),
        "max_abs_error": round(max_abs, 6),
    }

    return BacktestRun(
        items_evaluated=n,
        # Keep the wire payload small — 50 rows is plenty for the UI
        # sample table. The full N is summarized in `summary`/`bins`.
        relevance_predictions=predictions[:50],
        calibration_bins=calibration_bins,
        summary=summary,
    )


__all__ = ["BacktestRun", "run_backtest"]
