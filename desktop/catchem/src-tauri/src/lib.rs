//! Catchem — macOS desktop wrapper for catchem.
//!
//! This crate boots Tauri, spawns the local catchem sidecar, waits for
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
        // No plugins registered — see Cargo.toml for the rationale. The
        // capability set in `capabilities/default.json` is intentionally
        // narrow (core:default, core:window:default, core:event:default)
        // and JS doesn't call invoke() anywhere, so the plugin surface
        // would be pure overhead.
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
            //
            // The dev / release split also drives the cwd choice. Dev builds
            // run from the repo so the analyst can `git diff` outputs.
            // Release builds MUST write to ~/Library/Application Support/
            // because the .app bundle is read-only (Gatekeeper + codesign).
            let dev_root = paths::dev_repo_root();
            let release_mode = dev_root.is_none();
            let python_path: PathBuf = paths::dev_python().unwrap_or_else(|| {
                let resource = app
                    .path()
                    .resource_dir()
                    .expect("resource_dir")
                    .to_path_buf();
                paths::bundled_sidecar(&resource).unwrap_or_else(|| PathBuf::from("python3"))
            });
            let cwd: PathBuf = dev_root.unwrap_or_else(paths::app_data_dir);

            let cfg = SidecarConfig {
                python: python_path.clone(),
                cwd: cwd.clone(),
                host: DEFAULT_HOST.to_string(),
                port: DEFAULT_PORT,
                release_mode,
            };

            log::info!(
                "catchem boot: python={} cwd={} endpoint={} release={}",
                python_path.display(),
                cwd.display(),
                cfg.endpoint(),
                release_mode
            );

            let state = AppState::new(cfg.clone());

            // Start the sidecar BEFORE creating the window so the webview
            // can navigate straight to the FastAPI UI.
            if let Err(e) = state.sidecar.start(&cfg, false) {
                log::error!("sidecar start failed: {e}");
            }

            // Block briefly for sidecar readiness — production-safe stack
            // boots in ~500-1500ms. Cap at 30s; if it fails, the window opens
            // anyway pointing at the URL and shows a native "can't connect"
            // page which the user can retry by reloading from the menu.
            let cfg_clone = cfg.clone();
            tauri::async_runtime::block_on(async move {
                let outcome = crate::sidecar::wait_for_health(
                    &cfg_clone,
                    std::time::Duration::from_secs(30),
                ).await;
                if outcome.healthy {
                    log::info!("sidecar healthy in {}ms", outcome.elapsed_ms);
                } else {
                    log::warn!(
                        "sidecar not healthy after {}ms (status={:?} err={:?})",
                        outcome.elapsed_ms,
                        outcome.last_status,
                        outcome.last_error
                    );
                }
            });

            // Build the main window pointing at the local boot-shim. The
            // shim shows the 5-stage startup state machine (checking →
            // spawning → waiting → bundle → ready) and, once /healthz
            // returns 200, does `window.location.replace(<sidecar>/)` to
            // hand the window to the React UI. The on_navigation guard
            // below allows that cross-origin jump because the target is
            // `is_allowed_internal_url(127.0.0.1:8087)`.
            //
            // The block_on(wait_for_health) above is still useful: even if
            // the boot shim never loads (rare — e.g., bundle missing) the
            // log records sidecar health, and the FastAPI URL is ready by
            // the time the shim's first fetch fires.
            let nav_host = DEFAULT_HOST.to_string();
            let nav_port = DEFAULT_PORT;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Catchem")
                .inner_size(1280.0, 820.0)
                .min_inner_size(980.0, 640.0)
                .center()
                .resizable(true)
                .on_navigation(move |url| {
                    let url_str = url.as_str();
                    match security::classify_navigation(url_str, &nav_host, nav_port) {
                        security::NavigationDecision::AllowInWebview => true,
                        security::NavigationDecision::OpenExternal => {
                            let url_owned = url_str.to_string();
                            // Spawn a thread so the closure can return
                            // immediately — system browser launch may take
                            // ~100ms on cold start.
                            std::thread::spawn(move || {
                                if let Err(e) = open::that_detached(&url_owned) {
                                    log::warn!("open_external failed url={url_owned} err={e}");
                                }
                            });
                            false
                        }
                        security::NavigationDecision::Block => {
                            log::info!("blocked navigation: {url_str}");
                            false
                        }
                    }
                })
                .build()?;

            // Native menu bar.
            let menu = menu::build_menu(&app.handle().clone())?;
            app.set_menu(menu)?;

            // Menu event router. Navigation menu items call
            // `webview.navigate(<sidecar>/<route>)` directly — the browser
            // sees a normal URL change, the on_navigation classifier OKs
            // the same-origin jump, and React Router picks it up. Doing it
            // this way avoids the "emit a JS event no one listens to"
            // dead path the original implementation shipped with — frontend/
            // has zero `@tauri-apps/api` imports, so `handle.emit()` had
            // no receiver. The legacy emit() calls below stay for any
            // future tauri:// page (boot shim, etc.) that wants to listen.
            let app_handle = app.handle().clone();
            let sidecar_endpoint = cfg.endpoint();
            let go_to = move |handle: &tauri::AppHandle, route: &str| {
                if let Some(win) = handle.get_webview_window("main") {
                    let url_str = format!("{sidecar_endpoint}{route}");
                    match tauri::Url::parse(&url_str) {
                        Ok(url) => {
                            if let Err(e) = win.navigate(url) {
                                log::warn!("menu navigate failed: {e}");
                            }
                        }
                        Err(e) => log::warn!("bad menu url {url_str}: {e}"),
                    }
                }
            };
            app.on_menu_event(move |handle, ev| {
                let id = ev.id().0.as_str();
                let route = match id {
                    "nav_overview" => Some("/"),
                    "nav_feed" => Some("/feed"),
                    "nav_replay" => Some("/replay"),
                    "nav_analysis" => Some("/map"),
                    "nav_model" => Some("/model-controls"),
                    "help_open" => Some("/help"),
                    "file_new_paste" => Some("/replay"),
                    "sidecar_health" => Some("/model-controls"),
                    "help_logs" => Some("/model-controls"),
                    _ => None,
                };
                if let Some(r) = route {
                    go_to(handle, r);
                    // Legacy emit kept for any tauri:// page that may later
                    // listen (e.g. an updated boot shim). No-op for the
                    // external-origin React UI.
                    let _ = handle.emit("catchem:nav", r);
                    return;
                }
                match id {
                    "file_open" => {
                        let _ = app_handle.emit("catchem:file-open", ());
                    }
                    "sidecar_restart" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let cfg = state.sidecar_config.read().unwrap().clone();
                        let _ = state.sidecar.restart(&cfg);
                    }
                    "sidecar_stop" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let _ = state.sidecar.stop();
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
