# FinancialImpactRecord Fixtures

These examples show the durable JSON contract that Catchem writes for one
Awareness capture. They are intentionally small and deterministic so external
contributors can copy one into an issue or use it as a fixture when reporting a
taxonomy, symbol, sentiment, or evidence regression.

Every `*.json` file in this directory is validated by
`tests/test_financial_impact_record_fixtures.py` against
`catchem.schemas.FinancialImpactRecord`.
