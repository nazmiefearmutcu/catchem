# Runbook

## First-time setup

```bash
cd /Users/nazmi/Desktop/Projeler/proje/fusion_stack
bash scripts/fusion_bootstrap_and_run.sh
```

This is idempotent. You can re-run it any time. The script:

- creates `.venv` via `uv` (or falls back to `venv`)
- installs `fusion_stack[dev]` and `awareness` editable (if available)
- runs the guard verifier — aborts if the NewsImpact release gate has flipped
- installs frontend `npm` deps and builds the SPA into `src/fusion_stack/static/app`
- creates `data/{results,cache,db,logs,vector_index,golden,kaggle,replay}`
- runs one replay pass over Awareness JSONL
- starts the API on `127.0.0.1:8087` in the background
- opens **<http://127.0.0.1:8087/>** for the premium UI; `/legacy` for the old dashboard

Re-runs are cheap. To skip the npm build when you haven't changed `frontend/`:
`bash scripts/fusion_bootstrap_and_run.sh --skip-frontend-build`.

After bootstrap, you can use the CLI directly:

```bash
source .venv/bin/activate
fusion-stack status | python -m json.tool
fusion-stack run --mode replay_existing --max-records 200
fusion-stack serve
```

## Operational modes

### `production_safe` (default)

```bash
FUSION_MODE=production_safe fusion-stack run
```

Diagnostic adapter is hard-refused. The supervisor logs at startup
`diagnostic_enabled: false` regardless of any flag the operator may set.

### `replay_existing`

```bash
fusion-stack run --mode replay_existing --max-records 1000
```

Processes finalized JSONL files in `<awareness>/data/jsonl/captures/Y/M/D/`
once, persists per-file offsets, then exits. Safe to re-run; offsets ensure
exactly-once processing across restarts.

### `live_tail`

```bash
fusion-stack run --mode live_tail
```

Long-running. Re-scans the JSONL root every `live.poll_seconds` and processes
any new committed rows. Stops cleanly on SIGINT.

### `research_diagnostic` (requires explicit opt-in)

```bash
FUSION_MODE=research_diagnostic \
FUSION_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED=true \
fusion-stack run --mode research_diagnostic
```

The diagnostic adapter loads a read-only payload from NewsImpact governance and
attaches it to each record. It cannot override `is_finance_relevant`.

## HF model warming (optional)

```bash
bash scripts/fusion_bootstrap_and_run.sh --with-ml
# or, separately:
python scripts/warm_hf_models.py
```

Downloads to `$HF_HOME` (defaults to `~/.cache/huggingface`). Re-running is a
no-op once cached.

## Kaggle datasets (optional)

```bash
export KAGGLE_USERNAME=...
export KAGGLE_KEY=...
bash scripts/download_optional_kaggle_assets.sh
```

If credentials are absent the script exits 0 with a "skipped" message and the
bootstrap continues.

## Where things live

| Path | Contents |
|---|---|
| `data/db/fusion.sqlite3` | Record index, offsets, DLQ |
| `data/results/records-*.parquet` | Rotated parquet exports |
| `data/results/dlq/` | Failed captures (placeholder) |
| `data/vector_index/<capture_id>.npy` | Per-record embeddings |
| `data/logs/fusion_stack.log` | Structured JSON log |
| `data/logs/api.out` | API server stdout/stderr |
| `data/logs/api.pid` | API PID |
| `data/golden/` | Optional curated regression fixtures |
| `data/kaggle/` | Optional Kaggle downloads |

## API quick reference

```bash
curl -s http://127.0.0.1:8087/healthz
curl -s http://127.0.0.1:8087/ui/summary    | python -m json.tool
curl -s http://127.0.0.1:8087/ui/guards     | python -m json.tool
curl -s http://127.0.0.1:8087/ui/benchmark/latest | python -m json.tool
curl -s http://127.0.0.1:8087/ui/matrix     | python -m json.tool
curl -s http://127.0.0.1:8087/recent?limit=20
curl -s http://127.0.0.1:8087/records/by-asset-class/rates
curl -s "http://127.0.0.1:8087/records/by-symbol/AAPL"
curl -s -X POST -H 'content-type: application/json' \
    -d '{"max_records": 100}' http://127.0.0.1:8087/replay
curl -s -N --max-time 4 -H 'Accept: text/event-stream' http://127.0.0.1:8087/ui/stream
```

## UI dev mode

For hot reload on the React app while iterating:

```bash
# Terminal 1
fusion-stack serve

# Terminal 2
cd frontend && npm install && npm run dev
# → open http://localhost:5173 (Vite proxies /ui/* to the API on :8087)
```

The bundle in `src/fusion_stack/static/app` is **only** rebuilt by
`npm run build` (or the bootstrap script). The Python serving path always
looks at that directory, never at the Vite dev server.

## Stopping the API

```bash
kill "$(cat data/logs/api.pid)"
rm -f data/logs/api.pid
```

## Troubleshooting

| Symptom | Probable cause | Fix |
|---|---|---|
| `bootstrap` exits with `FAIL: governance_index.json missing` | merged_news not present or path wrong | Set `NEWSIMPACT_REPO_PATH` or place the repo at the default path. |
| `release_gate_passed_unexpectedly_true` | Someone (or something) mutated governance metadata. | Investigate. Do **not** suppress the check. |
| `fusion-stack run` exits with `awareness path missing` | Awareness data dir not where expected | `FUSION_PATHS__AWARENESS_DATA_DIR=...` |
| No records appearing | All inputs may be filtered. Run `fusion-stack run --mode replay_existing --max-records 50` and inspect `data/results/dlq/`. |
| `transformers` import error during ML run | venv missing optional extra | `uv pip install -e ".[ml]"` |
| `GET /` returns the placeholder page after `pip install dist/*.whl` | static dir installed via wheel `force-include` regressed | Re-check `[tool.hatch.build.targets.wheel] force-include` and re-run `tests/test_static_dashboard_packaged_install.py::test_wheel_install_smoke_serves_dashboard` |
| `FUSION_LIVE__POLL_SECONDS` ignored | someone reverted `settings_customise_sources` source order | Re-run `tests/test_settings_live_env_override.py` and restore env-above-init ordering |
| Diagnostic data appears in a `/recent` payload during prod-safe | redaction layer skipped at API surface | Re-check `redact_records_for_mode` calls in `api.py` |
| Bundle JS appears at `/static/app/...` instead of `/assets/...` | StaticFiles mount uses the wrong path | Verify `api.py` mounts `/assets` from the resolved `assets` subdir |

## NewsImpact safety: when to be paranoid

If anything in `models/governance_index/governance_index.json` changes (sha256
shift) or the verifier returns non-zero, **stop**. Treat it as a release-gate
incident. Do not "fix forward" — call the owner before re-enabling diagnostic
mode.
