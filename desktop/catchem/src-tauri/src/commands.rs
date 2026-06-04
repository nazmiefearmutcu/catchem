//! Tauri commands exposed to the webview via `invoke()`.

use serde::Serialize;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, State};

use crate::menu::open_secondary_window as menu_open_secondary_window;
use crate::security::is_safe_external_url;
use crate::sidecar::{wait_for_health, WaitForHealthOutcome};
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

/// Open a secondary analyst dashboard window pointing at the same sidecar.
///
/// Exposed as a Tauri command so a frontend command-palette action can
/// trigger it via `invoke('open_secondary_window')`. The webview at
/// `http://127.0.0.1:8087` is cross-origin from the Tauri runtime, so
/// reaching this command from JS requires either `withGlobalTauri: true`
/// or a remote `dangerousRemoteUrlIpcAccess` grant. Today the canonical
/// pathway is the native menu File→New Window (⌘N), which routes
/// through `lib.rs::on_menu_event` and calls `menu::open_secondary_window`
/// directly without crossing the IPC boundary. This command exists so a
/// future palette wire-up only needs the capability flip, not new Rust.
///
/// Returns the new window's label on success so the caller can correlate
/// it with a `tauri://created` event if desired.
#[tauri::command]
pub fn open_secondary_window(
    app: AppHandle,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let cfg = state.sidecar_config.read().map_err(|e| e.to_string())?.clone();
    menu_open_secondary_window(&app, &cfg.endpoint(), &cfg.host, cfg.port)
        .map_err(|e| e.to_string())
}
