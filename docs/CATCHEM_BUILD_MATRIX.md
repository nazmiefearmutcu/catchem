# Catchem Build Matrix

Reference for what's required on a clean macOS box to build Catchem from source. Verified on the `feature/catchem-hardening` branch.

## Toolchain

| Layer        | Tool / Package                                                                | Why                                                            | Install                                                                  | Notes                                                                          |
|--------------|-------------------------------------------------------------------------------|----------------------------------------------------------------|--------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| Python       | Python 3.12+                                                                  | Backend + sidecar                                              | `brew install python@3.13` or pyenv                                      | 3.13.3 is the current dev target. Torch on 3.13 still has rough edges — see ML_FALLBACK.md if you opt into HF models. |
| Python pkg   | uv                                                                            | Fast env / install / lock                                      | `pip install uv` or `brew install uv`                                    | bootstrap script uses it; falls back to plain `pip` if absent.                |
| Python pkg   | pyinstaller                                                                   | Release sidecar packaging                                      | `uv pip install pyinstaller`                                             | Only needed for release builds. `--onedir` is the supported layout.           |
| Frontend     | Node ≥ 20                                                                     | React + Vite + Tailwind                                        | `brew install node` or nvm                                               | Dev box currently runs Node 25.6.1. Lockfile pinned to npm 11.                |
| Frontend QA  | Playwright                                                                    | e2e smoke                                                      | `cd frontend && npx playwright install --with-deps`                      | CI optional. Headless mode works in dev; no GUI needed.                       |
| Rust         | Rust ≥ 1.77                                                                   | Tauri 2 shell                                                  | `rustup default stable`                                                  | Dev box: 1.95.0. Set in Cargo.toml `rust-version`.                            |
| Desktop      | tauri-cli 2.x                                                                 | `cargo tauri build` / `cargo tauri dev`                        | `cargo install tauri-cli --version '^2.0' --locked`                       | Dev box: 2.11.2. Pin via `--locked` to avoid surprise major bumps.            |
| macOS        | Xcode CLT (Command Line Tools)                                                | codesign, notarytool, stapler, dyld                            | `xcode-select --install`                                                 | Full Xcode NOT required for the build; CLT suffices for signing + bundling.    |
| ML (opt-in)  | transformers, sentence-transformers, huggingface_hub, torch                   | Real models instead of stubs                                   | `uv pip install -e '.[ml]'`                                              | Stub-mode default — install only if you want real model output.                |
| Kaggle (opt) | kaggle CLI                                                                    | Download benchmark datasets                                    | `pip install kaggle`                                                     | Exit-0 if creds absent; bootstrap won't block.                                 |
| Signing      | codesign, notarytool, stapler                                                 | Release distribution                                           | shipped with Xcode CLT                                                   | Unsigned builds work; Gatekeeper requires right-click → Open on first launch. |

## One-time setup

```bash
# 1. Project tools
cd ~/Desktop/Projeler/proje/fusion_stack
python3 -m venv .venv
. .venv/bin/activate
uv pip install -e .

# 2. Frontend + boot-shim deps
(cd frontend && npm install --no-audit --no-fund)
(cd desktop/catchem/web && npm install --no-audit --no-fund)

# 3. Tauri toolchain (one-time)
cargo install tauri-cli --version '^2.0' --locked

# 4. Optional: ML extras + Kaggle
uv pip install -e '.[ml]'                  # opt-in HF models
pip install kaggle && bash scripts/download_kaggle_assets.sh    # opt-in datasets
```

## Build flows

| Goal                              | Command                                                                                 | What you get                                                  | Time                  |
|-----------------------------------|-----------------------------------------------------------------------------------------|---------------------------------------------------------------|-----------------------|
| Local dev loop                    | `bash scripts/fusion_bootstrap_and_run.sh`                                              | FastAPI on :8087 + React UI rebuilt                            | ~30 s warm            |
| Local dev with real ML            | `bash scripts/fusion_bootstrap_and_run.sh --with-ml`                                    | Same, but loads HF models                                      | +60-180 s cold model load |
| Sidecar standalone                | `python -m fusion_stack.cli serve`                                                       | FastAPI only, no SPA build                                     | ~3 s                  |
| Catchem dev .app                  | `bash desktop/catchem/scripts/build_catchem_dev.sh`                                     | `cargo tauri dev` → window opens                               | ~5-12 min cold, ~10 s warm |
| Sidecar PyInstaller bundle        | `bash desktop/catchem/scripts/build_sidecar.sh`                                         | `desktop/catchem/sidecar-out/fusion-stack-sidecar/`            | ~90 s                 |
| Catchem release .app + .dmg       | `bash desktop/catchem/scripts/build_catchem_release.sh`                                  | `.app` + `.dmg` under `target/release/bundle/`                 | ~6-10 min cold        |
| Install to /Applications          | `bash desktop/catchem/scripts/install_catchem.sh`                                       | `/Applications/Catchem.app` with quarantine xattr stripped     | <5 s                  |
| Notarize + staple (with creds)    | release script reads `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD`         | Notarized DMG                                                  | ~5-15 min (apple side)|

If `APPLE_DEVELOPER_IDENTITY` is not set, the release script still produces an unsigned `.app`; Gatekeeper then requires right-click → Open on first launch.

## Test matrix

| Layer            | Command                                                  | Expected         |
|------------------|----------------------------------------------------------|------------------|
| Python contracts | `pytest tests -q`                                        | 222 passed, 1 skipped |
| Rust shell       | `(cd desktop/catchem/src-tauri && cargo test --lib -q)`  | 8 passed         |
| Frontend         | `(cd frontend && npm test)`                              | 31 passed        |
| Smoke flow       | `bash scripts/fusion_bootstrap_and_run.sh --skip-frontend-build --no-api --max=20` | exits 0         |
| Dev window       | `bash desktop/catchem/scripts/build_catchem_dev.sh`      | Window opens, boot stages animate, UI loads after /healthz |

The 1-skip in pytest is `tests/test_existing_repo_regressions.py:51` — gated on `final_best.pt` being present; the prompt forbids touching that file, so the skip is permanent.
