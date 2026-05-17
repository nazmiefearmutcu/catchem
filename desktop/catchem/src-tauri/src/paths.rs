//! Path resolution for dev + release builds.
//!
//! Dev build: the fusion_stack repo is the workspace 5 levels above the
//! Tauri binary. The sidecar uses `.venv/bin/python` from that repo.
//!
//! Release build: a PyInstaller-built sidecar binary ships inside
//! `Contents/Resources/sidecar/` of the .app bundle.

use std::path::PathBuf;

/// Look up the dev-mode repo root.
///
/// Starts from the cargo manifest dir, walks up until it finds
/// `pyproject.toml`. Returns None if not found (release build).
pub fn dev_repo_root() -> Option<PathBuf> {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut current = manifest.as_path();
    for _ in 0..8 {
        if current.join("pyproject.toml").exists() {
            return Some(current.to_path_buf());
        }
        current = current.parent()?;
    }
    None
}

/// Path to the dev-mode Python interpreter (the repo's `.venv`).
pub fn dev_python() -> Option<PathBuf> {
    let root = dev_repo_root()?;
    let py = root.join(".venv").join("bin").join("python");
    if py.exists() {
        Some(py)
    } else {
        None
    }
}

/// Resolve a sidecar that ships inside the .app bundle. Returns the path if
/// it exists. The PyInstaller build places it at
/// `<resources>/sidecar/fusion-stack-sidecar`.
pub fn bundled_sidecar(resource_dir: &PathBuf) -> Option<PathBuf> {
    let p = resource_dir.join("sidecar").join("fusion-stack-sidecar");
    if p.exists() {
        Some(p)
    } else {
        None
    }
}
