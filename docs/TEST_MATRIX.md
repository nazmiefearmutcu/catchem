# Test Matrix

Every test below has an explicit role. Markers are: `guard` (must always pass),
`regression` (source-repo safety), `smoke` (end-to-end), `integration` (touches
storage), `ml` (requires the optional ML extra).

## Group A — Source-repo regression safety

| File | What it proves |
|---|---|
| `test_existing_repo_regressions.py::test_awareness_doccapture_schema_unmodified` | The Awareness `doc.py` field list is intact. |
| `test_existing_repo_regressions.py::test_awareness_jsonl_writer_signature_unmodified` | The JSONL writer's public surface has not been mutated by fusion_stack. |
| `test_existing_repo_regressions.py::test_newsimpact_final_best_pt_not_modified_by_fusion_run` | Replays a tiny batch and reasserts `final_best.pt` (if present) is byte-identical. |
| `test_existing_repo_regressions.py::test_no_fusion_call_into_v7_runner_training_path` | Static check: no fusion_stack module references `v[3-7]_runner` or `pipeline_v7`. |
| `test_doccapture_contract.py::test_view_validates_real_jsonl` | Parse a real Awareness JSONL row through fusion_stack's view. |
| `test_doccapture_contract.py::test_view_is_not_strict_about_extras` | New optional fields on the upstream side don't break us. |
| `test_awareness_post_commit_consumption.py::test_skip_tmp_files` | We never read `.jsonl.tmp` (in-flight) chunks. |
| `test_awareness_post_commit_consumption.py::test_fusion_stack_does_not_import_awareness_internals` | We didn't accidentally import `awareness.workers` / `awareness.dedup`. |
| `test_awareness_post_commit_consumption.py::test_settings_default_mode_is_production_safe` | Default mode out of the box is production_safe. |

## Group B — Unit tests

| File | Coverage |
|---|---|
| `test_finance_filter.py` | Stage A: prefilter, cashtag, sport rejection, default prior. |
| `test_zero_shot_taxonomy.py` | Stage B stub: Fed → central_bank/rates/inflation, empty input, fallback. |
| `test_sentiment.py` | Stage C stub: positive / negative / neutral triage. |
| `test_embeddings.py` | Stage D stub: determinism, cosine sanity, vector index round-trip. |
| `test_reranker.py` | Stage E stub: rapidfuzz ranking. |
| `test_entity_linker.py` | Stage F.a: cashtags, lex hits, company aliases, empty input. |
| `test_symbol_mapper.py` | Stage F.b: internal registry, cashtag, missing NewsImpact root, fuzzy bypass for long text. |
| `test_chart_context.py` | Stage G: unavailable on missing root, synthetic artifact extraction, metadata-only label. |
| `test_scoring_and_evidence.py` | Stage H: floor, signal-on, negative-class veto, evidence top-K, entity density. |
| `test_storage_and_api.py::test_storage_round_trip` | SQLite + label inverted index round-trip. |

## Group C — Guard tests (must always pass)

| File | Coverage |
|---|---|
| `test_newsimpact_guard.py::test_real_newsimpact_quarantine_state_is_expected` | Live governance_index pinned to QUARANTINED_REGRESSIVE_MULTIMODAL + release_gate=false. |
| `test_newsimpact_guard.py::test_production_safe_refuses_diagnostic_adapter` | Production-safe mode hard-refuses to construct the adapter even with the flag on. |
| `test_newsimpact_guard.py::test_research_diagnostic_with_flag_off_refuses` | Flag must be explicit. |
| `test_newsimpact_guard.py::test_research_diagnostic_with_flag_on_and_quarantined_works` | Allowed path produces a labeled, override-forbidden payload. |
| `test_newsimpact_guard.py::test_release_gate_flip_refuses_loading` | Adapter refuses if release_gate is mutated to true. |
| `test_newsimpact_guard.py::test_verify_script_returns_zero_on_real_repo` | Standalone verifier exits 0 on the real repo. |
| `test_newsimpact_guard.py::test_verify_script_fails_on_flipped_gate` | Standalone verifier exits non-zero on a tampered fake. |
| `test_newsimpact_guard.py::test_service_in_production_safe_never_loads_diagnostic_adapter` | End-to-end check inside `build_service`. |
| `test_source_of_truth.py::test_source_of_truth_yaml_invariants` | YAML and code agree on guard invariants. |
| `test_source_of_truth.py::test_source_of_truth_doc_references_yaml` | Doc references the machine-readable form. |
| `test_bootstrap.py::test_bootstrap_runs_guard_verifier_against_real_repo` | Bootstrap invokes the guard verifier. |
| `test_bootstrap.py::test_bootstrap_kaggle_skipped_without_credentials` | Bootstrap survives no-credentials case. |
| `test_bootstrap.py::test_bootstrap_creates_required_directories` | Bootstrap is idempotent + creates expected dirs. |

## Group D — Integration / smoke

| File | Coverage |
|---|---|
| `test_service_replay_mode.py::test_replay_produces_records` | End-to-end replay over synthetic captures; finance vs sports triage; evidence present. |
| `test_service_replay_mode.py::test_replay_idempotent` | Re-running replay does not double-process. |
| `test_service_live_mode_smoke.py::test_tail_picks_up_new_files` | Drop a JSONL file mid-run, tail consumes it. |
| `test_single_command_smoke.py::test_bootstrap_shell_runs_end_to_end` | `fusion_bootstrap_and_run.sh` exits 0 from a clean state. |
| `test_storage_and_api.py::test_api_healthz_and_recent` | API up, dashboard endpoints respond. |
| `test_storage_and_api.py::test_api_process_one_and_lookup` | API `/process-one` end-to-end. |

## Group E — /ui aggregation + legacy preservation

| File | Coverage |
|---|---|
| `test_ui_endpoints.py::test_root_serves_html_or_fallback` | `GET /` returns SPA shell or the friendly placeholder. |
| `test_ui_endpoints.py::test_legacy_dashboard_still_served` | `/legacy` and `/legacy-dashboard` still serve the vanilla page. |
| `test_ui_endpoints.py::test_ui_summary_shape` | `/ui/summary` returns the documented payload keys. |
| `test_ui_endpoints.py::test_ui_facets_returns_paired_arrays` | Facet entries are `[label, count]` tuples. |
| `test_ui_endpoints.py::test_ui_timeline_buckets_have_total_and_relevant` | Timeline series shape. |
| `test_ui_endpoints.py::test_ui_top_symbols_and_reasons` | Leaderboards expose `symbol`/`reason` + `count`. |
| `test_ui_endpoints.py::test_ui_matrix` | Asset×reason matrix is square. |
| `test_ui_endpoints.py::test_ui_guards_reflects_real_state` | Live guard snapshot intact (gate=false, quarantine). |
| `test_ui_endpoints.py::test_ui_benchmark_latest` | Golden-set runs and reports relevance F1. |
| `test_ui_endpoints.py::test_ui_benchmark_history_is_safe_when_missing` | No history file ⇒ empty list, not 500. |
| `test_ui_endpoints.py::test_ui_symbol_aggregation` | `/ui/symbol/{sym}` returns the aggregated shape. |
| `test_ui_endpoints.py::test_diagnostic_flag_in_summary_when_research_mode` | Research mode reports `diagnostic_allowed=true`. |
| `test_ui_endpoints.py::test_production_safe_summary_refuses_diagnostic` | Even with env flag on, prod-safe reports `diagnostic_allowed=false`. |

## Group F — frontend Vitest

| File | Coverage |
|---|---|
| `frontend/src/tests/safeHref.test.ts` | `safeHref` accepts http/https only, rejects `javascript:` / `file:` / null. |
| `frontend/src/tests/StatusBanner.test.tsx` | Banner shows quarantine + release_gate, warns in diagnostic mode, reds out on guard error. |
| `frontend/src/tests/feedMerge.test.ts` | `mergeByCaptureId` dedupes + sorts; `newCaptureIds` diff; `isIncompleteRecord`; fallback to `created_at` when `published_ts` is missing; empty-input tolerance. |

## Group G — UI hardening / bug-hunt remediation (Python)

| File | Coverage |
|---|---|
| `test_static_dashboard_packaged_install.py` | importlib.resources lookup; FUSION_STATIC_DIR override + traversal rejection; wheel canary that builds the wheel and opens its ZIP to confirm `fusion_stack/static/dashboard.html` is packaged. |
| `test_settings_live_env_override.py` | env > YAML > defaults for nested fields (`live.poll_seconds`, `live.tail_max_per_tick`, `replay.batch_size`, `mode`, `use_ml_stubs`); invalid values reject; unknown keys ignored. |
| `test_guard_redaction_in_production.py` | `diagnostic_multimodal_*` scrubbed on every list/detail/summary endpoint; `/ui/guards` never leaks filesystem paths; `/metrics` diagnostic pinned False; diagnostic adapter never constructed in production_safe (mock-verified). |
| `test_records_by_asset_class_contract.py` | List routes return COMPACT summary shape; `text_excerpt`/`evidence_sentences`/`component_scores`/`model_versions`/`processing_mode`/`diagnostic_multimodal_result` forbidden in summaries. |
| `test_record_detail_contract.py` | Detail routes return full rich shape including `component_scores`, `evidence_sentences`, `model_versions`, `processing_mode`. |
| `test_dashboard_xss_fixture.py` | Security headers (CSP, XCTO, Referrer-Policy, XFO) present; legacy dashboard avoids unsafe DOM sinks; `safeHref` helper present; every `target=_blank` carries `rel=noopener`; API returns JSON not HTML. |
| `test_golden_schema_and_history.py` | `validate_golden_row` rejects malformed rows loudly; `load_extended` strict-mode raises, lax-mode skips; `BenchmarkReport.to_dict()` carries `schema_version`, `generated_at`, `dataset_name`; hard-negative golden items remain pinned. |
| `test_metrics_contract.py` | Stable JSON contract for `/metrics` and `/ui/summary` — required keys, ISO timestamps, prod-safe diagnostic pin. |
| `test_duplicate_and_incomplete_records.py` | `insert_record` dedupes by `capture_id`; label inverted index rebuilds on overwrite; JSONL reader skips garbage; DLQ records failures. |
| `test_timestamp_handling.py` | Naive datetimes coerced to UTC; ISO strings round-trip; missing `published_ts` allowed; storage serializes as ISO 8601. |

## Running

```bash
make test               # full Python suite (163 tests)
make test-fast          # skip ml + smoke + integration
make test-guards        # guard only (red here = stop the world)
make test-smoke         # end-to-end shell
(cd frontend && npm test)   # Vitest UI tests (13 tests)
```

The `ml` marker is opt-in and runs only when `pip install -e ".[ml]"` has been
applied. CI doesn't depend on it.
