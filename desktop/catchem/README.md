# Catchem

Catchem is a Tauri 2 macOS desktop app that wraps the local `catchem`
FastAPI/React stack. See [`../../docs/CATCHEM_APP.md`](../../docs/CATCHEM_APP.md)
for architecture and [`../../docs/CATCHEM_RELEASE.md`](../../docs/CATCHEM_RELEASE.md)
for packaging.

```bash
# Once
bash ../../scripts/catchem_bootstrap_and_run.sh         # creates .venv + builds bundle
cargo install create-tauri-app tauri-cli --version '^2.0' --locked

# Then
bash scripts/install_catchem.sh                         # build + install to /Applications
                                                        #   then launch /Applications/Catchem.app
                                                        #   click Allow on the first TCC prompt
                                                        #   subsequent launches do not re-prompt
bash scripts/build_catchem_dev.sh                       # dev hot-reload (Tauri dev mode)
bash scripts/build_catchem_release.sh                   # signed/notarized .app + .dmg
```

The `install_catchem.sh` path is the recommended dev workflow — it produces a stable `/Applications/Catchem.app` so macOS only asks for Desktop folder access once. See [`../../docs/CATCHEM_RELEASE.md`](../../docs/CATCHEM_RELEASE.md) for the TCC details.
