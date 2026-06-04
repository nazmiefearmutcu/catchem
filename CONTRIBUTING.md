# Contributing to catchem

Thanks for your interest. catchem consumes upstream Awareness captures and
emits multi-labeled `FinancialImpactRecord` events; it is reversible and
non-destructive by design.

## Easiest contributions

- **Taxonomy refinements** — `configs/taxonomy.yaml` defines asset
  classes, impact reasons, and direction labels. PRs that refine these
  with cited rationale (industry standards, academic literature) are
  welcome.
- **Source presets** — the demo-mode poller ships with 6 RSS sources.
  PRs that add a new source preset (region, sector, regulator) with
  documented robots-respecting polling interval are welcome.
- **Bug reports** — if a capture produces a wrong label, open an issue
  with `capture_id` + expected labels + actual labels (a screenshot of
  the capture detail view helps).

## Code contributions

1. Fork the repo and branch from `main`.
2. Bootstrap: `bash scripts/catchem_bootstrap_and_run.sh` (uses `uv`,
   falls back to `python -m venv`).
3. Tests: `make test` (everything) or `make test-guards` (guard suite
   only — this MUST always stay green).
4. **Constraints (non-negotiable):**
   - No training of NewsImpact.
   - No promotion / release of NewsImpact artifacts.
   - No writes to `final_best.pt` or under `models/`.
   - No destructive merge of upstream repos.
   - No runtime dependency on paid APIs or Kaggle credentials.
5. Open the PR with a one-line summary; if you touched the analyst UI,
   include a screenshot.

## Reversibility promise

Deleting this repo has zero effect on Awareness or NewsImpact. PRs that
break that promise will be closed without merge.

## Code of conduct

Be respectful, be specific, be brief. Disagreements are fine; insults are not.
