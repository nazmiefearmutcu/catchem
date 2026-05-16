# SOURCE OF TRUTH

This document defines the canonical, non-negotiable invariants for the fusion
stack. It is the authority any reviewer should cite when something below it in
the hierarchy contradicts it.

## Hierarchy

When in doubt, resolve conflicts in this order:

1. The **fusion_stack mission prompt** (the spec under which this stack was built).
2. The current contents of:
   - `awareness/src/awareness/schemas/doc.py` (DocCapture contract)
   - `awareness/src/awareness/storage/jsonl.py` (durability semantics)
   - `merged_news/models/governance_index/governance_index.json` (quarantine state)
   - `merged_news/governance/preflight.py` (preflight policy)
   - `fusion_stack/configs/source_of_truth.yaml` (machine-readable form of this doc)
3. Historical docs and READMEs, for context only. **Do not override safety rules with stale docs.**

## Awareness

- **Role:** stable upstream system of record.
- **Consumption strategy:** post-commit. fusion_stack reads JSONL files that the
  Awareness writer has already atomically renamed from `.tmp` to `.jsonl`.
- **Do not modify** the `DocCapture` schema (it uses `extra="forbid"` — additions
  are explicit and break compatibility with the Iceberg projection).
- **Do not inject** finance logic into the Awareness worker engine. The decoupling
  is what allows independent releases of each system.
- **State / tasks / dedup** semantics are owned by Awareness. fusion_stack only
  *reads* through the JSONL surface.

## NewsImpact

- **Role:** blocked release candidate. Research/diagnostic adapter only.
- **Release gate** (`gate_failure_status.release_gate_passed`) is `false` and
  must remain `false`. The verifier script aborts the bootstrap if this flips
  unexpectedly.
- **Expected governance status:** `QUARANTINED_REGRESSIVE_MULTIMODAL`.
- **Expected fusion verdict class:** `FUSION_REGRESSIVE`.
- **Forbidden operations:** `training`, `promotion`, `benchmark`, `export`.
- **Permitted operations** (only in `research_diagnostic` mode): `eval`, `diagnostic`,
  both with `ALLOW_WITH_WARNING`.
- **Protected artifacts** (must not be touched):
  - `final_best.pt`
  - `models/release_candidates/**`
  - `models/manifests/**`
  - `models/governance_index/**`
  - `governance/**`

The fusion_stack code path that reads these files is strictly read-only and
runs only when the operator explicitly opts into `research_diagnostic` mode.

## Mode invariants

| Mode | NewsImpact diagnostic | Writes to NewsImpact | Writes to Awareness |
|---|---|---|---|
| `production_safe` | ❌ off | ❌ refused | ❌ refused |
| `replay_existing` | ❌ off | ❌ refused | ❌ refused |
| `live_tail`       | ❌ off | ❌ refused | ❌ refused |
| `research_diagnostic` | ✅ on (read-only) | ❌ refused | ❌ refused |

## Why a sidecar workspace

A destructive splice of the two repos was rejected because:

1. Awareness is **release-accepted**; merging into a hybrid loses the clean
   provenance of its commit history.
2. NewsImpact is **quarantined**; merging into a hybrid risks accidentally
   bypassing governance.
3. Independent test suites and CI are easier to keep green when each repo can
   be installed and tested in isolation.

The sibling-workspace approach is reversible: `rm -rf fusion_stack` is a no-op
on Awareness and NewsImpact.

## How this document is enforced

- `scripts/verify_newsimpact_guard.py` reads the governance index on every
  bootstrap and exits non-zero if it has changed shape.
- Pytest markers `guard` and `regression` enumerate tests that pin these rules.
- `configs/source_of_truth.yaml` carries the same statements in machine form;
  the test `test_source_of_truth_matches_doc.py` keeps them in lockstep.
