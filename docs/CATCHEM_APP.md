# Catchem — macOS Desktop App

Catchem is a Tauri 2 + Rust shell that wraps the local `catchem`
FastAPI/React stack into a native macOS application. It exists to make the
paste-news → analyze flow feel like a real analyst tool, not a developer
console — without re-implementing the pipeline.

## Architecture

```
Catchem.app (Tauri shell, Rust)
├── spawns sidecar  →  catchem FastAPI on 127.0.0.1:8087
│                       └── serves the React premium UI from /
└── loads webview pointed at  http://127.0.0.1:8087/
```

The webview IS the existing catchem premium UI — same Vite bundle,
same `/feed`, `/replay`, `/model-controls`, `/help` routes. The Tauri shell
adds:

- Native menu bar with macOS shortcuts (⌘1–⌘5 for tabs, ⌘N for new paste, ⌘R for restart sidecar)
- Sidecar lifecycle (start / stop / restart / wait-healthy)
- Boot-screen with /healthz polling
- External-link safety (`safeHref` + system browser hand-off)
- Lock the webview to the local API origin (no arbitrary navigation)

## Project layout

```
desktop/catchem/
├── README.md
├── src-tauri/                Rust crate
│   ├── Cargo.toml            tauri 2 + plugins
│   ├── build.rs              tauri-build
│   ├── tauri.conf.json       window, security CSP, bundle config
│   ├── Entitlements.plist    hardened-runtime + sandbox entitlements
│   ├── capabilities/         per-window permission set
│   ├── icons/                placeholder icons (replace before release)
│   └── src/
│       ├── main.rs           thin shim
│       ├── lib.rs            Tauri setup, sidecar spawn, menu wiring
│       ├── sidecar.rs        Child-process manager + wait_for_health
│       ├── commands.rs       invoke() handlers exposed to webview
│       ├── menu.rs           native macOS menu bar
│       ├── paths.rs          dev (.venv) vs release (bundled) resolver
│       ├── security.rs       URL safety primitives (3 unit tests)
│       └── state.rs          process-wide AppState
├── web/                      Tauri's "frontendDist" boot shim
│   ├── package.json
│   ├── vite.config.ts
│   └── index.html            health-poll + redirect to FastAPI UI
└── scripts/
    ├── build_catchem_dev.sh         dev hot-reload
    ├── build_sidecar.sh             PyInstaller --onedir
    ├── build_catchem_release.sh     full .app + .dmg
    └── make_dmg.sh                  re-DMG a signed .app
```

## Running in dev

```bash
# Prereqs (once)
cd ~/Desktop/Projeler/proje/catchem
bash scripts/catchem_bootstrap_and_run.sh        # creates .venv + builds React bundle
cargo install create-tauri-app tauri-cli --version '^2.0' --locked

# Run Catchem
bash desktop/catchem/scripts/build_catchem_dev.sh
```

The first cargo build takes 2–5 minutes (Tauri + 250 transitive crates).
Subsequent runs use cargo's incremental cache (~5–10s rebuild).

## Sidecar lifecycle

The Rust shell owns the FastAPI process:

| User action | Rust call | Effect |
|---|---|---|
| App launch | `SidecarState::start(&cfg, false)` | spawn `.venv/bin/python -m catchem.cli serve` |
| Menu → Sidecar → Restart | `SidecarState::restart(&cfg)` | kill + spawn |
| Menu → Sidecar → Stop | `SidecarState::stop()` | kill |
| Window close | `SidecarState::stop()` | no orphan FastAPI |

The sidecar is always launched with:

```
CATCHEM_MODE=production_safe
CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED=false
CATCHEM_USE_ML_STUBS=true
CATCHEM_API_HOST=127.0.0.1
CATCHEM_API_PORT=8087
```

The shell **cannot** enable diagnostic mode. There is no UI surface for it.
To run in `research_diagnostic`, the user must launch the bare CLI manually
outside Catchem.

## Security posture

| Surface | Enforcement |
|---|---|
| CSP in webview | `default-src 'self' http://127.0.0.1:8087`; no external connect-src |
| External links | All routed through `is_safe_external_url()` (http/https only) + `open::that_detached()` to system browser |
| In-app navigation | Locked to local API origin via `is_allowed_internal_url()` |
| Production-safe mode | Pinned at sidecar spawn — desktop never sets `research_diagnostic` |
| Diagnostic data | Server-side redaction (`redaction.py`) already scrubs `diagnostic_multimodal_*` before responses leave the API |
| File upload | 5 MB cap + whitelist (`.txt/.md/.html/.jsonl/.json`) — extracted text via stdlib (no `lxml` CVE surface) |

## Tests

```bash
# Rust unit tests (security primitives)
cd desktop/catchem/src-tauri && cargo test --lib

# Python backend (192 tests including 17 Catchem-specific)
cd /Users/nazmi/Desktop/Projeler/proje/catchem && pytest tests

# Frontend (13 vitest tests including DropZone-relevant feedMerge logic)
cd frontend && npm test
```

## Endpoints added for Catchem

| Endpoint | Purpose | Response model |
|---|---|---|
| `POST /ui/demo/paste` | paste-news → score | `DemoRunResponse` |
| `POST /ui/demo/upload` | upload file → score | `DemoRunResponse` |
| `GET /ui/app-info` | version, commit, branch, mode, model_versions | `AppInfoResponse` |
| `GET /ui/sidecar-status` | healthy, pid, uptime, records, dlq | `SidecarStatusResponse` |
| `GET /ui/log-tail?lines=N` | last N lines of `data/logs/catchem.log` | `LogTailResponse` |
| `GET /replay` (SPA fallback) | serves the bundle for the `/replay` client-side route | HTML |

All five preserve the production-safe redaction contract — see
`docs/UI_HARDENING.md` for the underlying guarantees.

## Known limitations

- Icons are placeholder solid-color PNGs. Replace `desktop/catchem/src-tauri/icons/` with branded assets before any release.
- The `.app` build needs ~2 GB of disk (PyInstaller dehydrates the venv) and 2–5 min wall time. The dev build avoids both.
- Tauri 2 macos-private-api requires Xcode command-line tools; no fallback for sandboxed CI.
- Signing/notarization is documented in `CATCHEM_RELEASE.md`; without Apple credentials the build is unsigned (Gatekeeper will require right-click → Open on first launch).
