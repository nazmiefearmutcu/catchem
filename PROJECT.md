# Project: Catchem Automation & Development Infrastructure

## Architecture
The automation infrastructure will consist of:
1. A python-based coordinator/watcher script: `scripts/catchem_watcher.py`.
2. The script will monitor the codebase for changes.
3. Upon change (or initial start), it will run the following checks in parallel (using `asyncio.create_subprocess_exec`):
   - **Ruff check**: `ruff check src tests scripts` (or as configured in `pyproject.toml`)
   - **Pytest**: `pytest` (using python within the active virtualenv or `make test-fast`)
   - **Vite Build**: Running the frontend Vite build (`npm run build` or `npm run build:check` in `frontend/`)
   - **News Poller Ingest Scan**: A dry run or direct invocation of the ingest/poller script (e.g. `python -m catchem.ingest` or check how news poller is run).
4. Results and logs will be collected, streamed, and outputted concurrently.
5. Exit codes will be monitored.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | Discovery & Requirements Analysis | Explore codebase, find how news poller and tests are run | None | DONE |
| 2 | Watcher Design & Spec | Create specifications for `scripts/catchem_watcher.py` | M1 | PLANNED |
| 3 | Implementation | Implement the parallel watcher script | M2 | PLANNED |
| 4 | Verification & Hardening | Run build, tests, linting, and verify exit codes | M3 | PLANNED |
| 5 | Completion | Final handoff and reporting | M4 | PLANNED |

## Interface Contracts
### Watcher CLI
- Command: `python scripts/catchem_watcher.py`
- Arguments:
  - `--watch`: Run in continuous watch mode (default: run once and exit with aggregated exit code).
  - `--exclude`: Folders to exclude (e.g. `.git`, `.venv`, `node_modules`).
- Output: Logs from parallel runs, final execution matrix with statuses and exit codes.
