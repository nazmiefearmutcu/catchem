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

use std::fs::OpenOptions;
use std::io::Write;
use std::time::Duration;
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

use crate::sidecar::SidecarConfig;
use crate::state::AppState;

const DEFAULT_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8087;

/// File-based boot breadcrumb — survives launchd-mediated stderr discard.
///
/// `env_logger` writes to stderr, but when a release-built `.app` is launched
/// via Finder/Spotlight/`open`, stderr is silently discarded (launchd does
/// not pipe it anywhere observable, not even to the unified `log show`). The
/// effect is that `log::error!("sidecar start failed: {e}")` looks like it
/// fires but produces nothing the user or a debugger can see.
///
/// `boot_log` appends a timestamped line to `~/Library/Logs/Catchem/boot.log`
/// instead. The file is opened in append mode so successive launches stack,
/// and the helper swallows its own errors so a logging failure can never
/// crash the host process.
fn boot_log(stage: &str, msg: &str) {
    let path = paths::log_dir().join("boot.log");
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        let _ = writeln!(f, "[{now}] {stage}: {msg}");
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    boot_log("run", "entered");

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
            commands::open_secondary_window,
        ])
        .setup(|app| {
            boot_log("setup", "closure invoked");
            // Resolve sidecar python: dev = repo .venv, release = bundled
            // PyInstaller binary under the .app's Resources/sidecar/.
            //
            // The dev / release split also drives the cwd choice. Dev builds
            // run from the repo so the analyst can `git diff` outputs.
            // Release builds MUST write to ~/Library/Application Support/
            // because the .app bundle is read-only (Gatekeeper + codesign).
            let resource_dir = app
                .path()
                .resource_dir()
                .expect("resource_dir")
                .to_path_buf();
            boot_log("setup", &format!("resource_dir={}", resource_dir.display()));
            let resolved = match paths::resolve_sidecar(
                &resource_dir,
                paths::env_flag("CATCHEM_DESKTOP_DEV"),
            ) {
                Ok(resolved) => resolved,
                Err(e) => {
                    log::error!("sidecar resolution failed: {e}");
                    boot_log("setup", &format!("resolve_sidecar ERROR: {e}"));
                    return Err(std::io::Error::new(std::io::ErrorKind::NotFound, e).into());
                }
            };
            boot_log(
                "setup",
                &format!(
                    "resolved python={} cwd={} release={}",
                    resolved.executable.display(),
                    resolved.cwd.display(),
                    resolved.release_mode
                ),
            );

            let cfg = SidecarConfig {
                python: resolved.executable.clone(),
                cwd: resolved.cwd.clone(),
                host: DEFAULT_HOST.to_string(),
                port: DEFAULT_PORT,
                release_mode: resolved.release_mode,
            };

            log::info!(
                "catchem boot: python={} cwd={} endpoint={} release={}",
                cfg.python.display(),
                cfg.cwd.display(),
                cfg.endpoint(),
                cfg.release_mode
            );

            let state = AppState::new(cfg.clone());

            // Start the sidecar BEFORE creating the window so the webview
            // can navigate straight to the FastAPI UI.
            boot_log("setup", "calling sidecar.start()");
            match state.sidecar.start(&cfg, false) {
                Ok(()) => boot_log("setup", "sidecar.start() OK"),
                Err(e) => {
                    log::error!("sidecar start failed: {e}");
                    boot_log("setup", &format!("sidecar.start() ERROR: {e}"));
                }
            }

            // Block briefly for sidecar readiness — production-safe stack
            // boots in ~500-1500ms. Cap at 30s; if it fails, the window opens
            // anyway pointing at the URL and shows a native "can't connect"
            // page which the user can retry by reloading from the menu.
            let cfg_clone = cfg.clone();
            tauri::async_runtime::block_on(async move {
                let outcome = crate::sidecar::wait_for_health(
                    &cfg_clone,
                    DEFAULT_HEALTH_TIMEOUT,
                ).await;
                if outcome.healthy {
                    log::info!("sidecar healthy in {}ms", outcome.elapsed_ms);
                    boot_log(
                        "setup",
                        &format!("wait_for_health: HEALTHY ({}ms)", outcome.elapsed_ms),
                    );
                } else {
                    log::warn!(
                        "sidecar not healthy after {}ms (status={:?} err={:?})",
                        outcome.elapsed_ms,
                        outcome.last_status,
                        outcome.last_error
                    );
                    boot_log(
                        "setup",
                        &format!(
                            "wait_for_health: NOT HEALTHY after {}ms (status={:?} err={:?})",
                            outcome.elapsed_ms,
                            outcome.last_status,
                            outcome.last_error
                        ),
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
            // no receiver.
            //
            // Removed (v34): two `handle.emit("catchem:nav", r)` and
            // `app_handle.emit("catchem:menu", id)` calls that were firing
            // into a void. They were originally retained "in case a future
            // tauri:// boot shim wants to listen," but the boot shim is now
            // its own static page (see `frontend/boot/`) that does a hard
            // `window.location.replace` once /healthz returns 200, so no
            // long-lived listener exists. If we ever need IPC again, add it
            // back explicitly along with the matching `@tauri-apps/api`
            // listener on the JS side — dead emits are just noise.
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
                // 1. Navigation entries -> webview.navigate().
                if let Some(r) = menu::nav_route_for(id) {
                    go_to(handle, r);
                    return;
                }
                // 2. Frontend-delegated entries -> CustomEvent into webview.
                if menu::is_frontend_menu_id(id) {
                    menu::dispatch_frontend_menu(handle, id);
                    return;
                }
                // 3. Rust-only entries -> sidecar lifecycle + webview reload
                //    + secondary-window creation.
                match id {
                    "reload" => menu::reload_main_webview(handle),
                    "sidecar_restart" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let cfg = state.sidecar_config.read().unwrap().clone();
                        let _ = state.sidecar.restart(&cfg);
                    }
                    "sidecar_stop" => {
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let _ = state.sidecar.stop();
                    }
                    "new_window" => {
                        // Pull the live sidecar config so port/host overrides
                        // (e.g. CATCHEM_DESKTOP_DEV) are honoured.
                        let state: tauri::State<std::sync::Arc<AppState>> = app_handle.state();
                        let cfg = state.sidecar_config.read().unwrap().clone();
                        if let Err(e) = menu::open_secondary_window(
                            handle,
                            &cfg.endpoint(),
                            &cfg.host,
                            cfg.port,
                        ) {
                            log::warn!("menu new_window open failed: {e}");
                        }
                    }
                    _ => {}
                }
            });

            app.manage(state);
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                // Only stop the sidecar when the MAIN window closes —
                // closing a secondary analyst dashboard (v30) used to
                // kill the sidecar out from under the still-open main
                // window. The sidecar is the FastAPI backend; tearing
                // it down on every CloseRequested broke the main UI as
                // soon as the user closed any secondary window.
                if window.label() == "main" {
                    let state: tauri::State<std::sync::Arc<AppState>> = window.app_handle().state();
                    let _ = state.sidecar.stop();
                }
            }
        })
        .run(tauri::generate_context!())
        .unwrap_or_else(|e| {
            // .expect would panic into stderr — which launchd discards for
            // bundled apps. Surface the error into boot.log so we can see
            // what went wrong on the next launch.
            boot_log("run", &format!("tauri runtime ERROR: {e}"));
            panic!("error while running tauri application: {e}");
        });
    boot_log("run", "exited (normal)");
}

// Dev helper: how long to wait for sidecar /healthz before showing the
// "sidecar unreachable" banner in the UI.
pub const DEFAULT_HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
