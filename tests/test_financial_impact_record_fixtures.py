from __future__ import annotations

from pathlib import Path

from catchem.schemas import FinancialImpactRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "docs" / "examples" / "financial-impact-records"


def test_financial_impact_record_examples_validate() -> None:
    fixture_paths = sorted(FIXTURE_DIR.glob("*.json"))

    assert fixture_paths, f"no FinancialImpactRecord examples in {FIXTURE_DIR}"

    records = [
        FinancialImpactRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in fixture_paths
    ]

    assert any(record.is_finance_relevant for record in records)
    assert any(not record.is_finance_relevant for record in records)
    assert all(record.processing_mode.value == "production_safe" for record in records)
