//! Path resolution for dev + release builds.
//!
//! Dev build: the catchem repo is the workspace 5 levels above the
//! Tauri binary. The sidecar uses `.venv/bin/python` from that repo.
//!
//! Release build: a PyInstaller-built sidecar binary ships inside
//! `Contents/Resources/sidecar/` of the .app bundle.

use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    Dev,
    Packaged,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSidecar {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub release_mode: bool,
}

pub fn env_flag(name: &str) -> bool {
    std::env::var(name)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

/// Runtime mode is decided from the actual bundle/resource context, with an
/// explicit dev override for `cargo tauri dev`. It must not depend on the
/// compile-time source tree path because packaged apps are often built from
/// the same checkout that exists on the developer machine.
pub fn runtime_mode(resource_dir: &Path, explicit_dev: bool) -> RuntimeMode {
    if explicit_dev || !is_macos_app_resource_dir(resource_dir) {
        RuntimeMode::Dev
    } else {
        RuntimeMode::Packaged
    }
}

pub fn is_macos_app_resource_dir(resource_dir: &Path) -> bool {
    resource_dir.file_name().and_then(|s| s.to_str()) == Some("Resources")
        && resource_dir
            .parent()
            .and_then(|p| p.file_name())
            .and_then(|s| s.to_str())
            == Some("Contents")
        && resource_dir
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.extension())
            .and_then(|s| s.to_str())
            == Some("app")
}

pub fn find_repo_root_from(start: &Path) -> Option<PathBuf> {
    let mut current = if start.is_file() { start.parent()? } else { start };
    for _ in 0..8 {
        if current.join("pyproject.toml").exists() {
            return Some(current.to_path_buf());
        }
        current = current.parent()?;
    }
    None
}

/// Look up the dev-mode repo root from runtime state.
pub fn dev_repo_root() -> Option<PathBuf> {
    if let Ok(root) = std::env::var("CATCHEM_REPO_ROOT") {
        let root = PathBuf::from(root);
        if root.join("pyproject.toml").exists() {
            return Some(root);
        }
    }

    if let Ok(cwd) = std::env::current_dir() {
        if let Some(root) = find_repo_root_from(&cwd) {
            return Some(root);
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(root) = find_repo_root_from(&exe) {
            return Some(root);
        }
    }

    None
}

pub fn resolve_sidecar(
    resource_dir: &Path,
    explicit_dev: bool,
) -> Result<ResolvedSidecar, String> {
    resolve_sidecar_with_repo(resource_dir, explicit_dev, dev_repo_root())
}

pub fn resolve_sidecar_with_repo(
    resource_dir: &Path,
    explicit_dev: bool,
    dev_root: Option<PathBuf>,
) -> Result<ResolvedSidecar, String> {
    match runtime_mode(resource_dir, explicit_dev) {
        RuntimeMode::Dev => {
            let repo_root = dev_root.ok_or_else(|| {
                "dev mode requested but Catchem repo root was not found; set CATCHEM_REPO_ROOT"
                    .to_string()
            })?;
            let python = repo_root.join(".venv").join("bin").join("python");
            Ok(ResolvedSidecar {
                executable: if python.exists() {
                    python
                } else {
                    PathBuf::from("python3")
                },
                cwd: repo_root,
                release_mode: false,
            })
        }
        RuntimeMode::Packaged => {
            let executable = bundled_sidecar(resource_dir).ok_or_else(|| {
                format!(
                    "release sidecar missing: expected {}",
                    resource_dir.join("sidecar").join("catchem-sidecar").display()
                )
            })?;
            Ok(ResolvedSidecar {
                executable,
                cwd: app_data_dir(),
                release_mode: true,
            })
        }
    }
}

/// Resolve a sidecar that ships inside the .app bundle. Returns the path if
/// it exists. The PyInstaller build places it at
/// `<resources>/sidecar/catchem-sidecar`.
pub fn bundled_sidecar(resource_dir: &Path) -> Option<PathBuf> {
    let p = resource_dir.join("sidecar").join("catchem-sidecar");
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
///   data/              — catchem output (SQLite, parquet, dlq, live-news)
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

/// `<app_data_dir>/data` — set as `CATCHEM_PATHS__CATCHEM_OUTPUT_DIR` for the
/// release sidecar so SQLite, parquet flushes, and live-news archives all
/// land under Application Support.
pub fn release_catchem_output_dir() -> PathBuf {
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
    use std::fs;

    fn temp_path(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "catchem-tauri-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

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
        let out = release_catchem_output_dir();
        let aw = release_awareness_data_dir();
        assert!(
            out.starts_with(&parent),
            "catchem_output_dir not under app_data_dir: {out:?}"
        );
        assert!(
            aw.starts_with(&parent),
            "awareness_data_dir not under app_data_dir: {aw:?}"
        );
        assert_eq!(out.file_name().and_then(|s| s.to_str()), Some("data"));
        assert_eq!(aw.file_name().and_then(|s| s.to_str()), Some("awareness-data"));
    }

    #[test]
    fn log_dir_is_separate_from_app_data_dir() {
        // Logs go to ~/Library/Logs/Catchem/, NOT under Application Support.
        // Mixing them breaks Console.app's per-app log filtering.
        let logs = log_dir();
        let data = app_data_dir();
        assert!(
            !logs.starts_with(&data),
            "logs leaked into Application Support: {logs:?}"
        );
        assert!(logs.to_string_lossy().contains("Library/Logs/Catchem"));
    }

    #[test]
    fn runtime_mode_uses_resource_context_not_source_checkout() {
        let dev_resource = temp_path("dev-resource").join("target").join("debug");
        fs::create_dir_all(&dev_resource).unwrap();
        let app_resource = temp_path("packaged-resource")
            .join("Catchem.app")
            .join("Contents")
            .join("Resources");
        fs::create_dir_all(&app_resource).unwrap();

        assert_eq!(runtime_mode(&dev_resource, false), RuntimeMode::Dev);
        assert_eq!(runtime_mode(&app_resource, false), RuntimeMode::Packaged);
        assert_eq!(runtime_mode(&app_resource, true), RuntimeMode::Dev);
    }

    #[test]
    fn packaged_release_requires_bundled_sidecar() {
        let resource = temp_path("missing-sidecar")
            .join("Catchem.app")
            .join("Contents")
            .join("Resources");
        fs::create_dir_all(&resource).unwrap();

        let err = resolve_sidecar_with_repo(&resource, false, None).unwrap_err();
        assert!(err.contains("release sidecar missing"), "got: {err}");
        assert!(err.contains("catchem-sidecar"), "got: {err}");
        assert!(
            !err.contains("python3"),
            "release must not fall back to python3: {err}"
        );
    }

    #[test]
    fn packaged_release_uses_bundled_sidecar_when_present() {
        let resource = temp_path("present-sidecar")
            .join("Catchem.app")
            .join("Contents")
            .join("Resources");
        let sidecar = resource.join("sidecar").join("catchem-sidecar");
        fs::create_dir_all(sidecar.parent().unwrap()).unwrap();
        fs::write(&sidecar, b"fake sidecar").unwrap();

        let resolved = resolve_sidecar_with_repo(&resource, false, None).unwrap();
        assert_eq!(resolved.executable, sidecar);
        assert!(resolved.release_mode);
        assert!(resolved.cwd.ends_with("Library/Application Support/Catchem"));
    }

    #[test]
    fn dev_mode_keeps_python3_fallback_for_local_convenience() {
        let resource = temp_path("dev-resource-explicit");
        let repo = temp_path("dev-repo");
        fs::write(repo.join("pyproject.toml"), b"[project]\nname='catchem'\n").unwrap();

        let resolved = resolve_sidecar_with_repo(&resource, true, Some(repo.clone())).unwrap();
        assert_eq!(resolved.executable, PathBuf::from("python3"));
        assert_eq!(resolved.cwd, repo);
        assert!(!resolved.release_mode);
    }
}
