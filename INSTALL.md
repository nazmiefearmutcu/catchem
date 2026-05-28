# Installing Catchem

This guide walks through a clean install of the **Catchem** desktop app
on macOS. The end state is `/Applications/Catchem.app` launching a native
window that loads the local FastAPI sidecar.

For a non-desktop install (sidecar only, accessed at `http://127.0.0.1:8087/`
in a browser), stop after **Step 4**.

## 1. System prerequisites

- **macOS 14 (Sonoma) or newer** — Tauri 2 needs the modern WebKit.
- **Xcode command line tools**
  ```bash
  xcode-select --install
  ```
- ~2 GB free disk space (each install bundles a ~360 MB sidecar; the
  install script auto-rotates and keeps the last two).

## 2. Toolchain

### Python 3.11+

```bash
python3 --version          # must be 3.11 or higher
```

If you don't have a modern Python, install via Homebrew:

```bash
brew install python@3.13
```

### uv (recommended)

`uv` resolves and installs the Python deps an order of magnitude faster
than pip. The bootstrap script uses it when available and falls back to
`python -m venv` otherwise.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Node 20+ and npm

```bash
node --version             # must be 20 or higher
npm --version
```

If missing:

```bash
brew install node@20
```

### Rust + cargo-tauri

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
cargo install create-tauri-app tauri-cli --version '^2.0' --locked
```

## 3. Clone + bootstrap

```bash
git clone https://github.com/nazmiefearmutcu/catchem.git
cd catchem

# Idempotent — creates .venv, installs catchem[dev], installs Awareness if
# available, runs the NewsImpact guard verifier, seeds data/, builds the
# React bundle into src/catchem/static/app.
bash scripts/catchem_bootstrap_and_run.sh --skip-run
```

Flags worth knowing:

- `--with-ml` — also install the optional `ml` extra (torch, transformers,
  sentence-transformers, safetensors, huggingface_hub) and warm caches.
  Skip unless you actually need real classifier output; the default stubs
  are deterministic and CPU-friendly.
- `--mode=production_safe|replay_existing|live_tail|research_diagnostic`
- `--skip-frontend-build` — reuse the existing bundle for faster iteration.

## 4. First build (release `.app`)

```bash
bash desktop/catchem/scripts/build_catchem_release.sh
```

This produces a fully self-contained `.app` and `.dmg` under
`desktop/catchem/src-tauri/target/release/bundle/`. The script:

1. Builds the React bundle (`frontend/`) into `src/catchem/static/app`.
2. Builds the Tauri boot shim (`desktop/catchem/web/`).
3. Packages the Python sidecar with PyInstaller (`--onedir`).
4. Stages the sidecar at `src-tauri/resources/sidecar/` so Tauri bundles
   it inside `Contents/Resources/`.
5. Runs `cargo tauri build` (release profile).
6. Optionally signs + notarizes the bundle if `APPLE_DEVELOPER_IDENTITY`,
   `APPLE_ID`, `APPLE_TEAM_ID`, and `APPLE_APP_SPECIFIC_PASSWORD` are set.

## 5. Install + first launch

```bash
bash desktop/catchem/scripts/install_catchem.sh --release
open /Applications/Catchem.app
```

### What you'll see

1. A short boot screen polls `/healthz` while the sidecar starts.
2. The first launch triggers a macOS **TCC prompt** for Desktop folder
   access — required so the sidecar can read Awareness JSONL captures.
   Click **Allow** once; the cdhash is stable across re-launches from
   `/Applications`, so you will not be re-prompted.
3. When `/healthz` returns 200, the webview loads the Overview page.

The install script keeps the previous two `.app` bundles in a sibling
backup directory so a bad release can be rolled back manually.

## 6. DeepSeek reviewer (optional)

DeepSeek provides a hosted-LLM second opinion on a sampled fraction of
captures. Off by default. To enable:

```bash
# In .env or your shell environment
CATCHEM_REVIEWERS__DEEPSEEK__ENABLED=true
CATCHEM_REVIEWERS__DEEPSEEK__API_KEY=sk-...
CATCHEM_REVIEWERS__DEEPSEEK__SAMPLING_RATE=0.10
CATCHEM_REVIEWERS__DEEPSEEK__USD_CAP=9.50
```

Notes:

- Sampling is deterministic per `capture_id` (SHA-256 derived) — replays
  do not respend budget on different rows.
- Once `usd_cap` is reached, new calls fail fast with `budget_exceeded`.
  Restart the sidecar after raising the cap if you want to resume.
- Settings → DeepSeek card surfaces enabled / spent / cap / remaining at
  a glance and supports a live API key save.

## Troubleshooting

### "Sidecar is offline" banner stays up

1. `curl http://127.0.0.1:8087/healthz` — should return `{"ok":true,...}`.
2. Check the log file: `~/Library/Logs/Catchem/sidecar.log`. The Tauri
   shell redirects all sidecar stdio there.
3. If the log shows TCC denial errors, the `.app` is missing the
   `NSDesktopFolderUsageDescription` plist entry — rebuild via the
   release script (which calls `desktop/catchem/scripts/inject_info_plist.sh`).

### macOS keeps re-prompting for Desktop access

You are launching the app from a build directory, not `/Applications`.
Each `cargo tauri build` produces a new cdhash, which resets TCC. Use:

```bash
bash desktop/catchem/scripts/install_catchem.sh --release
open /Applications/Catchem.app
```

### "Bundle missing" / "sidecar binary not found"

`SKIP_BUILD=1` was set when running `install_catchem.sh` without a prior
release build. Run the full release script:

```bash
bash desktop/catchem/scripts/build_catchem_release.sh
```

### No news arriving in the Feed

1. Confirm the poller is on: `CATCHEM_NEWS__POLLER_ENABLED` must not be
   `false`.
2. Check outbound HTTPS — the poller fetches 53 public RSS endpoints
   (BBC, Reuters, SEC, CoinDesk, etc.). Corporate firewalls and VPNs
   sometimes block these.
3. Visit `/logs` in the app, or `tail -f ~/Library/Logs/Catchem/sidecar.log`,
   and look for `news_poll_*` entries.

### Disk pressure

Each install lays down ~360 MB. The install script keeps the last two
`.app` bundles for rollback. To prune:

```bash
ls /Applications/Catchem.app.backup.*    # list backups
rm -rf /Applications/Catchem.app.backup.<timestamp>
```

### "Guard suite failed" during bootstrap

The NewsImpact governance gate has flipped. Inspect:

```bash
python scripts/verify_newsimpact_guard.py
catchem validate-guards
```

Do not bypass — the gate is the system-of-truth for whether the
diagnostic adapter is allowed to run.

## Uninstall

```bash
rm -rf /Applications/Catchem.app
rm -rf ~/Library/Logs/Catchem
rm -rf ~/Library/Application\ Support/Catchem
```

Removing the repo itself has zero effect on Awareness or NewsImpact — the
catchem workspace is reversible by design.
