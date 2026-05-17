//! Tauri commands exposed to the webview via `invoke()`.

use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tauri::State;

use crate::security::is_safe_external_url;
use crate::sidecar::{wait_for_health, SidecarConfig, SidecarState, WaitForHealthOutcome};
use crate::state::AppState;

#[derive(Serialize)]
pub struct SidecarReport {
    pub pid: Option<u32>,
    pub endpoint: String,
}

#[tauri::command]
pub fn sidecar_status(state: State<'_, Arc<AppState>>) -> SidecarReport {
    let cfg = state.sidecar_config.read().expect("cfg lock").clone();
    SidecarReport {
        pid: state.sidecar.pid(),
        endpoint: cfg.endpoint(),
    }
}

#[tauri::command]
pub fn sidecar_start(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    let cfg = state.sidecar_config.read().expect("cfg lock").clone();
    state.sidecar.start(&cfg, false)
}

#[tauri::command]
pub fn sidecar_stop(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    state.sidecar.stop()
}

#[tauri::command]
pub fn sidecar_restart(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    let cfg = state.sidecar_config.read().expect("cfg lock").clone();
    state.sidecar.restart(&cfg)
}

#[tauri::command]
pub async fn sidecar_wait_healthy(
    state: State<'_, Arc<AppState>>,
    timeout_ms: Option<u64>,
) -> Result<WaitForHealthOutcome, String> {
    let cfg = state.sidecar_config.read().expect("cfg lock").clone();
    let timeout = Duration::from_millis(timeout_ms.unwrap_or(30_000));
    Ok(wait_for_health(&cfg, timeout).await)
}

#[tauri::command]
pub fn endpoint(state: State<'_, Arc<AppState>>) -> String {
    state.sidecar_config.read().expect("cfg lock").endpoint()
}

#[tauri::command]
pub fn open_external(url: String) -> Result<(), String> {
    if !is_safe_external_url(&url) {
        return Err(format!("refused unsafe external url: {url}"));
    }
    // Hand to system browser via tauri-plugin-opener / `open` crate
    open::that_detached(&url).map_err(|e| e.to_string())
}
