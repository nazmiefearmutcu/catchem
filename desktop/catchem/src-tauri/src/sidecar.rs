//! Sidecar manager.
//!
//! Spawns the fusion_stack FastAPI server as a child process, polls
//! /healthz until it answers, and exposes start/stop/status to the
//! Tauri command layer.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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
        cmd.arg("-m").arg("fusion_stack.cli").arg("serve");
        cmd.current_dir(&cfg.cwd);
        // Production-safe is the only acceptable default. Diagnostic must
        // never be enabled from the desktop shell.
        cmd.env("FUSION_MODE", "production_safe");
        cmd.env("FUSION_GUARDS__NEWSIMPACT_DIAGNOSTIC_ENABLED", "false");
        cmd.env("FUSION_API_HOST", &cfg.host);
        cmd.env("FUSION_API_PORT", cfg.port.to_string());
        // Stub default — the user can opt into HF via a separate setup script.
        cmd.env("FUSION_USE_ML_STUBS", "true");
        cmd.stdout(Stdio::piped());
        cmd.stderr(Stdio::piped());

        let child = cmd
            .spawn()
            .map_err(|e| format!("failed to spawn sidecar: {e}"))?;
        log::info!("sidecar spawned pid={}", child.id());
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
