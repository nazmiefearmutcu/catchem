# CI

`/.github/workflows/ci.yml` runs three jobs on every push and PR to `main` /
`feat/**` / `fix/**`:

## `backend`
1. Install `catchem[dev]` + `build`.
2. Synthesize a quarantined `governance_index.json` fixture (CI does not have
   the real `merged_news` repo on disk).
3. Run `scripts/verify_newsimpact_guard.py` against the fixture — must exit 0.
4. Run the full `pytest tests` suite.
5. **Wheel canary**: `python -m build --wheel`, open the resulting `.whl`,
   assert `catchem/static/dashboard.html` is inside the package payload.
6. Run the golden benchmark and assert `schema_version == 1` and
   `relevance.f1 >= 0.83`.

## `frontend`
1. Install via `npm ci` (falls back to `npm install` if no lock-file).
2. `tsc -b --noEmit` typecheck.
3. `vitest run`.
4. Production `vite build`.
5. Assert the built bundle landed at `src/catchem/static/app/index.html`
   + `.../assets/`.

## `api-smoke` (needs both)
1. Start `catchem serve` in the background.
2. Poll `/healthz` until it answers.
3. Assert the security headers are present
   (`Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`).
4. Validate `/metrics` JSON.
5. Confirm `/ui/guards` payload does NOT contain `governance_index_path`.
6. Run the **paste-news demo** end-to-end against `docs/examples/news_fed.txt`.
7. Tail API log on failure.

## Environment

CI runs with `CATCHEM_MODE=production_safe` and `CATCHEM_MODELS__USE_ML_STUBS=true`
— deterministic stubs, no network reaches Hugging Face or Kaggle. Permissions
are read-only on `contents`. Nothing publishes or promotes.

## Local equivalent

```bash
make test
(cd frontend && npm test && npm run build)
catchem serve &
catchem demo --title "Fed hikes 25 bps" --text-file docs/examples/news_fed.txt
```
