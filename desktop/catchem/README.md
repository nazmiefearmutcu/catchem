# Catchem

Catchem is a Tauri 2 macOS desktop app that wraps the local `fusion_stack`
FastAPI/React stack. See [`../../docs/CATCHEM_APP.md`](../../docs/CATCHEM_APP.md)
for architecture and [`../../docs/CATCHEM_RELEASE.md`](../../docs/CATCHEM_RELEASE.md)
for packaging.

```bash
# Once
bash ../../scripts/fusion_bootstrap_and_run.sh         # creates .venv + builds bundle
cargo install create-tauri-app tauri-cli --version '^2.0' --locked

# Then
bash scripts/build_catchem_dev.sh                       # dev hot-reload
bash scripts/build_catchem_release.sh                   # .app + .dmg
```
