# UI Hardening — Bug Hunt Remediation Notes

This document captures the post-launch hardening pass on the UI/API boundary.
Every section maps to a P0/P1 risk identified during the bug hunt.

## P0 · Static asset packaging

**Risk.** `Path(__file__).parent / "static"` works in editable installs but
breaks in wheel installs where the package directory is not next to a
writable `static/` subtree.

**Fix.** Two changes:

1. `pyproject.toml` switched from `[tool.hatch.build.targets.wheel.shared-data]`
   to `[tool.hatch.build.targets.wheel] force-include` so the static folder
   ships INSIDE the package wheel (`catchem/static/...`), not in the
   system shared-data prefix.
2. New `src/catchem/static_assets.py` resolves files via
   `importlib.resources.files("catchem") / "static" / name` and exposes
   `get_static_path(name)` and `open_static_bytes(name)`. The FastAPI surface
   (`api.py`) calls these instead of constructing filesystem paths.

**Dev override.** `CATCHEM_STATIC_DIR=/some/dir` is honored — but only for
files that actually exist there. Missing files fall through to package
resources. Path traversal (`..`, leading `/`) is rejected with `ValueError`.

**Test.** `tests/test_static_dashboard_packaged_install.py` (10 tests including
a real `python -m build` wheel canary that opens the wheel ZIP and asserts
`catchem/static/dashboard.html` is present).

## P0 · Nested env precedence

**Risk.** Earlier output showed `CATCHEM_LIVE__POLL_SECONDS` not always
propagating. pydantic-settings v2 defaults to *init kwargs* overriding env;
our YAML loader passes config as init kwargs, so YAML was silently winning.

**Fix.** `Settings.settings_customise_sources` was already flipping the
priority order to `(dotenv, env, init, secrets)`. We added:

- A `@model_validator(mode="after")` that propagates `CATCHEM_USE_ML_STUBS`
  (the documented flat env) into `models_.use_ml_stubs`. Previously the flat
  var was inert.
- 10 explicit regression tests in `tests/test_settings_live_env_override.py`
  covering nested env override for `live.poll_seconds`,
  `live.tail_max_per_tick`, `replay.batch_size`, `mode`, `use_ml_stubs`,
  invalid value rejection, unknown-key tolerance.

**Documented precedence (lowest → highest):**
`defaults < configs/catchem.yaml (init kwargs) < .env file < process env`

> Process env beats `.env` so that explicit shell overrides win, pytest's
> `monkeypatch.setenv` works as expected, and CI job env beats any committed
> `.env`. See `settings_customise_sources` in `src/catchem/settings.py`.

## P0 · Guard redaction in production_safe

**Risk.** UI or /ui endpoints may expose diagnostic-only fields, or allow a
client to infer/enable blocked NewsImpact behavior in production_safe mode.

**Fix.** Defense in depth via new `src/catchem/redaction.py`:

- `redact_record_for_mode(record, production_safe=True)` always sets
  `diagnostic_multimodal_enabled=False` and `diagnostic_multimodal_result=None`
  before sending. Pure function, never mutates input.
- `redact_records_for_mode(items, production_safe=...)` for list payloads.
- `safe_guard_view(snapshot)` projects only an audit-safe set of keys
  (no `governance_index_path`, no `error` string that might contain local
  paths — only an `error_code` token).

Applied at: `/recent`, `/record/{id}`, `/records/by-*`, `/dashboard`,
`/process-one`, `/ui/summary`, `/ui/symbol/{sym}`, `/ui/guards`.

**Tests.** `tests/test_guard_redaction_in_production.py` — 15 tests covering
pure-redactor unit checks, every list/detail/summary endpoint, the `/metrics`
diagnostic pin, the env-flag override refusal, and a mock that asserts
`NewsImpactGuardedAdapter` is never even constructed in production_safe.

## P0 · Summary vs detail API contract

**Risk.** List routes returning full-record payloads (with `text_excerpt` and
all internals) cause overfetching, cache instability, and contract drift.

**Fix.** New `src/catchem/contracts.py` defines:

- `FinancialImpactSummary` — list shape. NO `text_excerpt`, NO
  `evidence_sentences` (replaced with `evidence_preview` ≤ 240 chars +
  `evidence_count`), NO `component_scores`, NO `model_versions`. Still carries
  `diagnostic_multimodal_enabled` (pinned False in prod-safe) so the UI never
  has to handle `undefined`.
- `FinancialImpactDetail` — drawer shape. All rich fields.
- `RecordListResponse`, `MetricsSummary`, `GuardSummary`.

Routes use `response_model=...` so FastAPI:
- filters the dict to the documented keys (drops accidental extras),
- generates correct OpenAPI,
- gives us a single place to evolve the contract.

**Tests.** `tests/test_records_by_asset_class_contract.py` and
`tests/test_record_detail_contract.py` (11 tests). The summary test
specifically asserts that the body text ("Powell said") never appears in any
list response.

## P0 · Live polling buffering

**Risk.** The Feed's auto-refresh re-orders rows while a record is open in
the detail drawer, destroying the analyst's reading position.

**Fix.** Two layers:

- New pure helpers in `frontend/src/lib/feedMerge.ts`:
  - `mergeByCaptureId(oldRows, newRows)` — dedupes by capture_id, newer copy
    wins, stable sort newer-first.
  - `newCaptureIds(baseline, incoming)` — diffs which IDs are new.
  - `isIncompleteRecord(r)` — flags rows missing capture_id/doc_id.
- `FeedPage` was rewritten to use a `stableRows` snapshot. While a drawer is
  open (`captureId` in URL), incoming rows that are NEW are pushed to
  `bufferedRows` and surfaced as a clickable "N new items available" banner.
  The user explicitly applies them; the viewport never reflows under them.

**Tests.** `frontend/src/tests/feedMerge.test.ts` — 6 unit tests for the
pure helpers covering dedup, sort, new-id diff, incomplete-record flagging,
empty inputs.

## P1 · Security headers

**Fix.** New ASGI middleware in `create_app` adds on every response:

- `Content-Security-Policy` — `default-src 'self'`, `object-src 'none'`,
  `frame-ancestors 'none'`, `base-uri 'self'`, plus `'unsafe-inline'` for
  script and style (documented compromise: both apps ship inline scripts
  that build DOM via `createElement`/`textContent` only — the actual XSS
  protection lives in the safe-rendering code, not in CSP for inline scripts).
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-Frame-Options: DENY`

**Tests.** `tests/test_dashboard_xss_fixture.py` — 6 tests verifying the
headers are present, the legacy dashboard avoids risky DOM sinks (the
`inner` + `HTML` family plus `insert` + `AdjacentHTML` and `document.wri` + `te`),
every `target="_blank"` carries `rel="noopener"`, the API returns JSON
(not HTML) for record routes, and a malicious record's URL is preserved
verbatim so the frontend's `safeHref` can refuse it.

## P1 · Golden benchmark schema versioning

**Fix.** `golden.py` adds:

- `GOLDEN_SCHEMA_VERSION = 1` and `REQUIRED_GOLDEN_FIELDS`
- `validate_golden_row(row)` — raises `ValueError` on missing required
  fields, wrong types, malformed lists.
- `load_extended(path, strict=True)` — defaults to LOUD failure on bad JSONL
  rows so a truncated extended file does not silently degrade the benchmark.
- `BenchmarkReport.to_dict()` now includes `schema_version`, `generated_at`,
  `dataset_name`.

**Tests.** `tests/test_golden_schema_and_history.py` — 16 tests covering
validation, strict/lax loaders, schema-version stamping, hard-negative
coverage (art-restitution / sports / celebrity / recipe / human-rescue
remain pinned in the synthetic set).

## P1 · Duplicate + incomplete records

**Verified behavior** (no code change needed; tests added to lock it down):

- `Storage.insert_record` uses `INSERT OR REPLACE`, so the same `capture_id`
  cannot accumulate duplicate rows.
- On overwrite, the inverted label index is rebuilt — stale label rows are
  deleted first.
- `parse_capture_line` returns `None` for garbage / non-dict / missing-field
  inputs — the JSONL reader skips them and the API keeps serving.
- The DLQ table captures `(capture_id, error, payload_excerpt, ts)` rows on
  pipeline failures.

**Tests.** `tests/test_duplicate_and_incomplete_records.py` — 6 tests.

## P1 · Timezone-safe timestamps

**Verified behavior + tests** in `tests/test_timestamp_handling.py` (5 tests):

- `AwarenessCaptureView` coerces naive datetimes to UTC via its
  `@field_validator`.
- ISO string timestamps round-trip through the parser.
- Missing `published_ts` is allowed; storage stores NULL and the API serializes
  it as `null`.
- Stored timestamps come back as ISO 8601 strings that
  `datetime.fromisoformat` can re-parse.

## P1 · Metrics contract

**Fix.** `/metrics` now always includes `generated_at` (ISO 8601) and pins
`diagnostic_enabled: false` in production_safe.

**Tests.** `tests/test_metrics_contract.py` — 4 tests covering required keys,
ISO timestamps, and the prod-safe diagnostic pin.

## P2 · Documentation

This file. Plus RUNBOOK and TEST_MATRIX updated to point here.

## What was checked and NOT found

- `final_best.pt` mutation — N/A, file does not exist locally.
- Awareness/merged_news repo modifications — none. Both upstream repos
  untouched by this branch.
- Forbidden NewsImpact training/runner imports — static check still passes
  (`test_no_catchem_call_into_v7_runner_training_path`).
- Production-safe → diagnostic adapter escape — covered by guard redaction
  tests.
