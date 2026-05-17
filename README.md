# fusion_stack

A local-first sidecar workspace that fuses two existing systems:

- **Awareness** — public-text ingestion engine (stable upstream, system of record).
- **NewsImpact** — multimodal candidate that is **currently quarantined** and
  permitted only as a read-only diagnostic.

`fusion_stack` consumes Awareness JSONL captures **after** they are durably
committed and emits one `FinancialImpactRecord` per capture: a multi-label
classification of asset class / impact reason / symbols / sentiment / evidence,
together with the component scores that produced the decision.

It ships with a premium analyst UI (React + TypeScript) served from the same
FastAPI process — no separate frontend runtime.

It also ships with **Catchem**, a Tauri 2 macOS desktop wrapper that gives
the analyst experience native menus, drag-and-drop file analysis, sidecar
lifecycle controls, and a notarizable `.app`/`.dmg` distribution path. See
[`docs/CATCHEM_APP.md`](docs/CATCHEM_APP.md).

This repo never modifies Awareness or NewsImpact source. It is reversible:
deleting it has zero effect on either upstream system.

## Catchem (macOS desktop)

```bash
# Once
bash scripts/fusion_bootstrap_and_run.sh                       # creates .venv + bundle
cargo install create-tauri-app tauri-cli --version '^2.0' --locked

# Run
bash desktop/catchem/scripts/build_catchem_dev.sh              # dev (Cmd+R restarts sidecar)
bash desktop/catchem/scripts/build_catchem_release.sh          # .app + .dmg
```

Catchem boots, spawns the local FastAPI sidecar, waits on `/healthz`, then
loads the existing React UI in a native macOS window. Production-safe is the
only mode the shell ever launches in. See [`docs/CATCHEM_RELEASE.md`](docs/CATCHEM_RELEASE.md)
for signing and notarization.

## One-command bootstrap (web only)

```bash
bash scripts/fusion_bootstrap_and_run.sh
```

What it does (idempotent):

1. creates `.venv` via `uv` (falls back to `python -m venv`)
2. installs `fusion_stack[dev]` editable
3. installs `awareness` editable if available
4. verifies both repo paths
5. runs the NewsImpact guard verifier — **aborts** if the release gate flipped
6. (optional) warms HF model caches when `--with-ml` is set
7. (optional) attempts Kaggle dataset downloads if credentials exist
8. installs frontend `npm` deps + builds the SPA into `src/fusion_stack/static/app`
9. initializes `data/{results,db,logs,cache,vector_index,...}`
10. runs replay mode against Awareness JSONL (default `--max=50`)
11. starts the local API on `127.0.0.1:8087` in the background
12. prints a summary

Open: <http://127.0.0.1:8087/>

Flags:
- `--with-ml` — install + warm the HF model extras
- `--no-api` — skip starting the API
- `--mode=...` — `production_safe` | `replay_existing` (default) | `live_tail` | `research_diagnostic`
- `--max=N` — replay record cap
- `--skip-run` — only do setup, don't run the pipeline
- `--skip-frontend-build` — reuse the existing bundle (faster restarts)
- `--dev-ui` — print the Vite dev-server command

## The premium UI

Routes:

- `/` — Overview (cards, distributions, trend, recent flow, benchmark snapshot)
- `/feed` — Live feed with URL-state filters, drawer-driven detail
- `/map` — Asset-class × reason-code heatmap + stacked trend
- `/symbols` and `/symbols/:sym` — Symbols explorer
- `/benchmark` — Golden-set precision/recall/F1, per-item, history
- `/ops` — System health, guard status, model versions, raw config
- `/settings` — Theme, shortcuts, mode explanations
- `/legacy` — The original vanilla dashboard (kept until full replacement is proven)

Power-user keys: `⌘K` opens the command palette; `g o`/`g f`/`g m`/`g s`/`g b`/`g x`/`g ,` navigate; `Esc` closes drawers.

## Modes

| Mode | Description | NewsImpact diagnostic |
|---|---|---|
| `production_safe` | Default. Pipeline only, no diagnostic adapter. | ❌ never |
| `replay_existing` | Process committed JSONL once. Used by tests. | ❌ |
| `live_tail` | Long-running tail of new JSONL chunks. | ❌ |
| `research_diagnostic` | Same as live, **plus** a read-only diagnostic stamp from NewsImpact governance. | ✅ (read-only, labeled) |

The diagnostic adapter is constructed lazily and refuses to start in any mode
where `guards.newsimpact_diagnostic_enabled` is false or
`gate_failure_status.release_gate_passed` is true.

## API

Local-only, binds to `127.0.0.1:8087`.

| Endpoint | Purpose |
|---|---|
| `GET /` | premium SPA |
| `GET /legacy` | vanilla dashboard fallback |
| `GET /healthz` | liveness |
| `GET /docs` | OpenAPI |
| **Aggregation** ||
| `GET /ui/summary` | one-shot landing payload |
| `GET /ui/facets` | facet counts for filter chips |
| `GET /ui/timeline` | time-bucketed counts |
| `GET /ui/trends` | stacked trend per asset class |
| `GET /ui/matrix` | asset_class × reason_code heatmap |
| `GET /ui/top-symbols` / `/ui/top-reasons` | leaderboards |
| `GET /ui/symbol/{sym}` | aggregated symbol view |
| `GET /ui/guards` | NewsImpact governance snapshot |
| `GET /ui/benchmark/latest` | run + return golden-set report |
| `GET /ui/benchmark/history` | persisted history (jsonl) |
| `GET /ui/stream` | SSE: `summary` + `tick` events |
| **Records** (preserved from v1) ||
| `GET /recent` | recent records |
| `GET /record/{capture_id}` | one record |
| `GET /records/by-symbol/{sym}` | label filter |
| `GET /records/by-asset-class/{ac}` | label filter |
| `GET /records/by-reason/{rc}` | label filter |
| `POST /replay` | run one replay pass |
| `POST /process-one` | run one capture through the pipeline |

## CLI

```bash
fusion-stack run --mode replay_existing
fusion-stack replay --path data/awareness/jsonl/captures/.../X.jsonl
fusion-stack inspect --capture-id <id>
fusion-stack benchmark --golden
fusion-stack validate-guards
fusion-stack status
fusion-stack serve
```

## Tests

```bash
make test              # all Python tests (~7s, 86 tests)
make test-fast         # skip ml/smoke/integration
make test-guards       # guard suite only (must always be green)
make test-smoke        # end-to-end + bootstrap shell
(cd frontend && npm test)   # Vitest UI tests (7 tests)
```

## Layout

```
fusion_stack/
├── configs/                  fusion.yaml, taxonomy.yaml, source_of_truth.yaml
├── docs/                     SYSTEM_OVERVIEW, RUNBOOK, TEST_MATRIX, SOURCE_OF_TRUTH,
│                             UI_OVERVIEW, FRONTEND_ARCHITECTURE, KEYBOARD_SHORTCUTS
├── frontend/                 React + TypeScript + Vite source
├── scripts/                  bootstrap + HF warm + Kaggle + guard verifier
├── src/fusion_stack/
│   ├── settings.py · schemas.py · taxonomy.py
│   ├── storage.py · awareness_reader.py · awareness_replay.py
│   ├── finance_filter.py · zero_shot_classifier.py · sentiment.py
│   ├── embeddings.py · reranker.py · entity_linker.py
│   ├── symbol_mapper.py · channel_mapper.py · chart_context.py
│   ├── evidence.py · scoring.py
│   ├── newsimpact_guarded_adapter.py    ← the safety boundary
│   ├── golden.py                        ← synthetic benchmark
│   ├── service.py · supervisor.py · dashboard_data.py · bootstrap.py
│   ├── api.py                           ← FastAPI + /ui/* + SSE + bundle serving
│   ├── cli.py                           ← Typer
│   └── static/
│       ├── dashboard.html               ← vanilla legacy dashboard
│       └── app/                         ← built React bundle (generated)
└── tests/                    pytest — guard + unit + integration + smoke + /ui
```

## Constraints (non-negotiable)

- **No** training of NewsImpact.
- **No** promotion / publication / release of NewsImpact artifacts.
- **No** writes to `final_best.pt` or anywhere under `models/`.
- **No** destructive merge of the original repos.
- **No** runtime dependency on paid APIs or Kaggle credentials.

See `docs/SOURCE_OF_TRUTH.md` for the full statement of authority.
