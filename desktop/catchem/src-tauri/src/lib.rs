//! Catchem — macOS desktop wrapper for fusion_stack.
//!
//! This crate boots Tauri, spawns the local fusion_stack sidecar, waits for
//! /healthz, and loads the FastAPI-served React UI in a single webview.

mod commands;
mod menu;
mod paths;
mod security;
mod sidecar;
mod state;

use std::path::PathBuf;
use std::time::Duration;
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

use crate::sidecar::SidecarConfig;
use crate::state::AppState;

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8087;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_process::init())
        .invoke_handler(tauri::generate_handler![
            commands::sidecar_status,
            commands::sidecar_start,
            commands::sidecar_stop,
            commands::sidecar_restart,
            commands::sidecar_wait_healthy,
            commands::endpoint,
            commands::open_external,
        ])
        .setup(|app| {
            // Resolve sidecar python: dev = repo .venv, release = bundled
            // PyInstaller binary under the .app's Resources/sidecar/.
            let python_path: PathBuf = paths::dev_python().unwrap_or_else(|| {
                // Release fallback: look beside the executable for sidecar/
                let resource = app
                    .path()
                    .resource_dir()
                    .expect("resource_dir")
                    .to_path_buf();
                paths::bundled_sidecar(&resource).unwrap_or_else(|| PathBuf::from("python3"))
            });
            let cwd: PathBuf =
                paths::dev_repo_root().unwrap_or_else(|| std::env::current_dir().unwrap());

            let cfg = SidecarConfig {
                python: python_path.clone(),
                cwd: cwd.clone(),
                host: DEFAULT_HOST.to_string(),
                port: DEFAULT_PORT,
            };

            log::info!(
                "catchem boot: python={} cwd={} endpoint={}",
                python_path.display(),
                cwd.display(),
                cfg.endpoint()
            );

            let state = AppState::new(cfg.clone());

            // Start the sidecar; ignore errors so the window still appears
            // (the user can retry from Model Controls).
            if let Err(e) = state.sidecar.start(&cfg, false) {
                log::error!("sidecar start failed: {e}");
            }

            // Build the main window pointing at the sidecar URL.
            let url = format!("{}/", cfg.endpoint())
                .parse()
                .expect("valid url");
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("Catchem")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 640.0)
                .center()
                .build()?;

            // Native menu bar.
            let menu = menu::build_menu(&app.handle().clone())?;
            app.set_menu(menu)?;

            // Menu event router → emit JS events the webview can react to.
            let app_handle = app.handle().clone();
            app.on_menu_event(move |handle, ev| {
                let id = ev.id().0.as_str();
                let route = match id {
                    "nav_overview" => Some("/"),
                    "nav_feed" => Some("/feed"),
                    "nav_replay" => Some("/replay"),
                    "nav_analysis" => Some("/map"),
                    "nav_model" => Some("/model-controls"),
                    "help_open" => Some("/help"),
                    _ => None,
                };
                if let Some(r) = route {
                    let _ = handle.emit("catchem:nav", r);
                    return;
                }
                match id {
                    "file_open" => { let _ = app_handle.emit("catchem:file-open", ()); }
                    "file_new_paste" => { let _ = app_handle.emit("catchem:nav", "/replay"); }
                    "sidecar_restart" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let cfg = state.sidecar_config.read().unwrap().clone();
                        let _ = state.sidecar.restart(&cfg);
                    }
                    "sidecar_stop" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let _ = state.sidecar.stop();
                    }
                    "sidecar_health" => {
                        let _ = app_handle.emit("catchem:nav", "/model-controls");
                    }
                    "help_logs" => {
                        let _ = app_handle.emit("catchem:nav", "/model-controls");
                    }
                    _ => {}
                }
            });

            app.manage(state);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Stop the sidecar on quit so we don't leave an orphan FastAPI.
                let state: tauri::State<std::sync::Arc<AppState>> = window.app_handle().state();
                let _ = state.sidecar.stop();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

// Dev helper: how long to wait for sidecar /healthz before showing the
// "sidecar unreachable" banner in the UI.
pub const DEFAULT_HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
