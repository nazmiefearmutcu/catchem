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

/// Directory where Catchem writes its sidecar logs. On macOS this is
/// `~/Library/Logs/Catchem/`. Created on first use.
///
/// We always log to a file rather than piping stdio back into the Rust
/// process — an undrained pipe will deadlock the child once the kernel
/// buffer fills (~16-64 KB), which makes "why didn't the sidecar bind?"
/// debugging effectively impossible.
pub fn log_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    let dir = PathBuf::from(home).join("Library").join("Logs").join("Catchem");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// Path to the sidecar stdout/stderr log file.
pub fn sidecar_log_path() -> PathBuf {
    log_dir().join("sidecar.log")
}

/// Persistent application data directory — release builds write here so the
/// .app bundle stays read-only (Apple's Gatekeeper marks bundle contents
/// quarantined; writing inside Catchem.app would either fail or break the
/// codesignature).
///
/// On macOS: `~/Library/Application Support/Catchem/`. Created on first use.
///
/// Layout:
///   data/              — fusion_stack output (SQLite, parquet, dlq, live-news)
///   awareness-data/    — Awareness JSONL inbox (when present)
///
/// Dev builds also call this — it's a no-op for them because lib.rs uses
/// `dev_repo_root()` instead, but having the dir already created means an
/// analyst toggling between dev and release builds sees a stable inbox.
pub fn app_data_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
    let dir = PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("Catchem");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// `<app_data_dir>/data` — set as `FUSION_PATHS__FUSION_OUTPUT_DIR` for the
/// release sidecar so SQLite, parquet flushes, and live-news archives all
/// land under Application Support.
pub fn release_fusion_output_dir() -> PathBuf {
    let dir = app_data_dir().join("data");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

/// `<app_data_dir>/awareness-data` — kept empty by default; if the user
/// drops Awareness JSONL files here the sidecar replay path picks them up.
pub fn release_awareness_data_dir() -> PathBuf {
    let dir = app_data_dir().join("awareness-data");
    let _ = std::fs::create_dir_all(&dir);
    dir
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_data_dir_under_application_support() {
        let dir = app_data_dir();
        let s = dir.to_string_lossy();
        assert!(s.contains("Library/Application Support/Catchem"), "got: {s}");
        assert!(dir.exists(), "app_data_dir should auto-create: {s}");
    }

    #[test]
    fn release_subdirs_live_under_app_data_dir() {
        let parent = app_data_dir();
        let out = release_fusion_output_dir();
        let aw = release_awareness_data_dir();
        assert!(out.starts_with(&parent), "fusion_output_dir not under app_data_dir: {out:?}");
        assert!(aw.starts_with(&parent), "awareness_data_dir not under app_data_dir: {aw:?}");
        assert_eq!(out.file_name().and_then(|s| s.to_str()), Some("data"));
        assert_eq!(aw.file_name().and_then(|s| s.to_str()), Some("awareness-data"));
    }

    #[test]
    fn log_dir_is_separate_from_app_data_dir() {
        // Logs go to ~/Library/Logs/Catchem/, NOT under Application Support.
        // Mixing them breaks Console.app's per-app log filtering.
        let logs = log_dir();
        let data = app_data_dir();
        assert!(!logs.starts_with(&data), "logs leaked into Application Support: {logs:?}");
        assert!(logs.to_string_lossy().contains("Library/Logs/Catchem"));
    }
}
