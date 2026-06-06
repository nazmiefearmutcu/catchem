# Original User Request

## Initial Request — 2026-06-06T00:01:55+03:00

Catchem projesinde kesintisiz ve paralel bir şekilde kod analizi, haber tarama (ingest/poller), frontend/backend derleme (build) ve test doğrulaması süreçlerini asenkron/paralel olarak yürütecek bir otomasyon ve geliştirme altyapısı kurmak.

Working directory: /Volumes/disk 2/Desktop_Migrate_2026-05-28/Projeler/proje/catchem
Integrity mode: development

## Requirements

### R1. Paralel Çalışma ve İş Akışı Otomasyonu
Haber kaynağı taraması (ingest/news_poller/source scan) ile kod analizi (linting/security scans) süreçlerinin, frontend derleme (Vite/TS/React UI build) ve test paket doğrulaması (pytest / `make test`) süreçleriyle tamamen paralel ve birbirini engellemeyecek (non-blocking) şekilde yürütülmesi.

### R2. Canlı İzleme ve Geliştirme Döngüsü (Continuous Watcher)
Projede yapılan değişiklikleri izleyen, değişiklik anında arka planda paralel olarak scan (kod analizi + haber tarama) ve build (derleme + test doğrulaması) işlemlerini başlatan, sonuçları raporlayan/loglayan bir watcher/koordinatör script veya mekanizmanın entegre edilmesi.

## Acceptance Criteria

### Paralel Yürütme ve Doğrulama
- [ ] Watcher veya koordinasyon scripti çalıştırıldığında tarama (scan) ve derleme (build/test) süreçleri asenkron/paralel olarak başlar ve çalışır.
- [ ] Tarama ve derleme süreçleri birbirinin tamamlanmasını beklemeden eşzamanlı olarak yürütülür (CPU/log çıktıları üzerinden doğrulanabilir).
- [ ] Kod analizi (ruff/lint), test doğrulama (pytest) ve frontend derleme süreçlerinin tamamı sıfır hata (exit code 0) ile sonuçlanır.

## Follow-up — 2026-06-06T00:59:22+03:00

An asynchronous, parallel watcher and automation coordinator script (`scripts/catchem_watcher.py`) that monitors the codebase for changes and executes linting (Ruff), backend tests (Pytest), frontend build checks (Vite), and ingestion scans (News Poller) in parallel without blocking each other, logging progress and final execution matrices.

Working directory: /Users/nazmi/Desktop/Projeler/proje/catchem
Integrity mode: development

## Requirements

### R1. Parallel Task Execution
- Execute the following four tasks in parallel using Python's `asyncio` subprocess capabilities:
  - **Ruff check**: By default, `.venv/bin/ruff check src tests scripts`
  - **Pytest**: By default, `.venv/bin/pytest`
  - **Vite Build**: By default, `npm run build` inside the `frontend/` directory
  - **News Poller Ingestion Scan**: By default, `.venv/bin/python3 -m catchem.cli validate-guards`
- The script must allow these task commands to be customizable via CLI arguments (e.g., `--ruff-cmd`, `--pytest-cmd`, `--build-cmd`, `--scan-cmd`).

### R2. Asynchronous Output Streaming
- Stream outputs of these parallel tasks concurrently to stdout/stderr.
- Prefix each line of output with a clean label indicating the task name (e.g. `[RUFF]`, `[PYTEST]`, `[BUILD]`, `[SCAN]`) so the output is readable and not garbled.
- Capture and report the exit codes of all tasks.

### R3. Continuous File Watcher
- Watch the codebase for modifications (files ending in `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.json`, `.css`, `.html`).
- Ignore changes in directories: `.git`, `.venv`, `node_modules`, `frontend/dist`, and `.ralph`.
- Upon file modification, apply a 1-second debounce delay before triggering a new run of the tasks.
- Ensure the watcher itself does not trigger infinite loops from its own outputs or log files.

### R4. Summary Reporting
- Print a clear, formatted summary matrix at the end of each run showing:
  - Task Name
  - Status (Success/Failure)
  - Exit Code
  - Duration (in seconds)
- If not in watch mode (single run), the script must exit with code 0 if all tasks succeed, and code 1 if any task fails.

## Acceptance Criteria

### Execution & Parallelism
- [ ] Running `python scripts/catchem_watcher.py` starts all four tasks concurrently.
- [ ] Task outputs are printed to the console prefixed with their respective task labels.
- [ ] Once all tasks finish, a final summary matrix is printed showing each task name, status, exit code, and duration.
- [ ] Running the script without `--watch` exits with code 0 if all tasks succeed, and code 1 if any task fails.

### Watcher Behavior
- [ ] Running `python scripts/catchem_watcher.py --watch` keeps the process alive in a loop.
- [ ] Modifying any source file in `src/` or `frontend/src/` triggers a new execution of the verification tasks after a 1-second debounce.
- [ ] A dedicated unit/integration test suite (`tests/test_watcher.py`) verifies:
  - Watcher task execution concurrency
  - Task output labeling/prefixing
  - Proper exit codes for task success/failure
  - Debounced file modification triggers
- [ ] All tests in `tests/test_watcher.py` pass and achieve 100% statement and branch coverage.

