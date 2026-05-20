//! Sidecar manager.
//!
//! Spawns the catchem FastAPI server as a child process, polls
//! /healthz until it answers, and exposes start/stop/status to the
//! Tauri command layer.

use serde::{Deserialize, Serialize};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::paths;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SidecarConfig {
    /// Absolute path to a Python interpreter (`.venv/bin/python` in dev,
    /// the bundled PyInstaller binary in release).
    pub python: PathBuf,
    /// Working directory the process should launch from (repo root in dev).
    pub cwd: PathBuf,
    /// API host + port the server listens on.
    pub host: String,
    pub port: u16,
    /// `true` for the bundled .app build — sidecar must write all output
    /// under `~/Library/Application Support/Catchem/`, NOT relative to
    /// the (read-only, codesigned) bundle. Dev builds keep the repo-local
    /// `data/` layout so analysts can `git diff` outputs.
    #[serde(default)]
    pub release_mode: bool,
}

impl SidecarConfig {
    pub fn endpoint(&self) -> String {
        format!("http://{}:{}", self.host, self.port)
    }
}

#[derive(Default)]
pub struct SidecarState {
    inner: Mutex<Option<Child>>,
}

impl SidecarState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// Spawn the sidecar. If one is already running and `force` is false,
    /// returns Ok(()) without touching it.
    pub fn start(&self, cfg: &SidecarConfig, force: bool) -> Result<(), String> {
        let mut guard = self.inner.lock().map_err(|e| e.to_string())?;
        if guard.is_some() && !force {
            return Ok(());
        }
        if let Some(mut child) = guard.take() {
            // best-effort kill if we were forcing restart
            let _ = child.kill();
            let _ = child.wait();
        }
        let mut cmd = Command::new(&cfg.python);
        cmd.arg("-m").arg("catchem.cli").arg("serve");
        cmd.current_dir(&cfg.cwd);
        // Production-safe is the only acceptable default. Diagnostic must
        // never be enabled from the desktop shell.
        cmd.env("CATCHEM_MODE", "production_safe");
        cmd.env("CATCHEM_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "false");
        cmd.env("CATCHEM_API_HOST", &cfg.host);
        cmd.env("CATCHEM_API_PORT", cfg.port.to_string());
        // Stub default — the user can opt into HF via a separate setup script.
        cmd.env("CATCHEM_USE_ML_STUBS", "true");
        // Force unbuffered Python output so errors surface immediately.
        cmd.env("PYTHONUNBUFFERED", "1");
        // Release-only: pin the writable data root to Application Support.
        // Dev builds intentionally inherit the repo-local `data/` layout
        // so analysts can `git diff` outputs.
        if cfg.release_mode {
            let out_dir = paths::release_catchem_output_dir();
            let aw_dir = paths::release_awareness_data_dir();
            cmd.env("CATCHEM_PATHS__CATCHEM_OUTPUT_DIR", &out_dir);
            cmd.env("CATCHEM_PATHS__AWARENESS_DATA_DIR", &aw_dir);
            log::info!(
                "sidecar release-mode paths: out={} awareness_data={}",
                out_dir.display(),
                aw_dir.display()
            );
        }

        // Redirect stdout+stderr to a log file we can tail. Piped+undrained
        // streams will deadlock the child once the pipe buffer fills, which
        // silently breaks "sidecar isn't binding" debugging.
        let log_path = paths::sidecar_log_path();
        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .map_err(|e| format!("open sidecar log {}: {e}", log_path.display()))?;
        let log_file_err = log_file
            .try_clone()
            .map_err(|e| format!("clone sidecar log fd: {e}"))?;
        // Marker so successive launches are easy to separate in the file.
        {
            let mut banner = log_file
                .try_clone()
                .map_err(|e| format!("clone sidecar log fd: {e}"))?;
            let _ = writeln!(
                banner,
                "\n=== catchem sidecar start {} python={} cwd={} ===",
                chrono_like_now(),
                cfg.python.display(),
                cfg.cwd.display()
            );
        }
        cmd.stdout(Stdio::from(log_file));
        cmd.stderr(Stdio::from(log_file_err));

        let child = cmd
            .spawn()
            .map_err(|e| format!("failed to spawn sidecar: {e}"))?;
        log::info!(
            "sidecar spawned pid={} log={}",
            child.id(),
            log_path.display()
        );
        *guard = Some(child);
        Ok(())
    }

    pub fn stop(&self) -> Result<(), String> {
        let mut guard = self.inner.lock().map_err(|e| e.to_string())?;
        if let Some(mut child) = guard.take() {
            log::info!("sidecar stop requested pid={}", child.id());
            let _ = child.kill();
            let _ = child.wait();
        }
        Ok(())
    }

    pub fn restart(&self, cfg: &SidecarConfig) -> Result<(), String> {
        self.stop()?;
        self.start(cfg, true)
    }

    pub fn pid(&self) -> Option<u32> {
        self.inner.lock().ok().and_then(|g| g.as_ref().map(|c| c.id()))
    }
}

#[derive(Debug, Serialize)]
pub struct WaitForHealthOutcome {
    pub healthy: bool,
    pub elapsed_ms: u128,
    pub last_status: Option<u16>,
    pub last_error: Option<String>,
}

/// ISO-ish timestamp using only stdlib — avoids pulling chrono just for a
/// log banner. Format: `1970-01-01T00:00:00Z` style at second resolution.
fn chrono_like_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    // Quick Y/M/D/H/M/S split using the standard "days since epoch" trick.
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let h = rem / 3600;
    let m = (rem % 3600) / 60;
    let s = rem % 60;
    let (y, mo, d) = days_to_ymd(days);
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", y, mo, d, h, m, s)
}

/// Civil-date conversion from days since 1970-01-01. Algorithm by Howard
/// Hinnant (date.h), public domain. Avoids chrono.
fn days_to_ymd(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Poll `/healthz` until 200 or timeout.
pub async fn wait_for_health(cfg: &SidecarConfig, timeout: Duration) -> WaitForHealthOutcome {
    let url = format!("{}/healthz", cfg.endpoint());
    let started = Instant::now();
    let mut last_status: Option<u16> = None;
    let mut last_error: Option<String> = None;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(800))
        .build()
        .expect("reqwest client");

    while started.elapsed() < timeout {
        match client.get(&url).send().await {
            Ok(resp) => {
                last_status = Some(resp.status().as_u16());
                if resp.status().is_success() {
                    return WaitForHealthOutcome {
                        healthy: true,
                        elapsed_ms: started.elapsed().as_millis(),
                        last_status,
                        last_error: None,
                    };
                }
            }
            Err(e) => {
                last_error = Some(e.to_string());
            }
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    WaitForHealthOutcome {
        healthy: false,
        elapsed_ms: started.elapsed().as_millis(),
        last_status,
        last_error,
    }
}
