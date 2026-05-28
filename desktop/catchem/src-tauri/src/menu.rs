//! Native menu bar definition for Catchem.
//!
//! Standard macOS layout: App / File / Edit / View / Sidecar / Help.
//! Menu actions split into three categories handled in `lib.rs`:
//!
//! 1. **Navigation items** (`nav_*`, `file_new_paste`, `help_open`,
//!    `sidecar_health`, `help_logs`) — routed via `webview.navigate(URL)` so
//!    React Router picks them up like any same-origin click.
//! 2. **Sidecar lifecycle** (`sidecar_restart`, `sidecar_stop`) — handled
//!    directly in Rust via the `AppState` sidecar manager.
//! 3. **Frontend-delegated actions** (`export_db`, `import_db`, `reload`,
//!    `toggle_theme`, `show_shortcuts`, `api_docs`, `file_open`) — Rust
//!    dispatches a `CustomEvent('catchem:menu', {detail: <id>})` into the
//!    webview using `webview.eval(<hardcoded JS literal>)`. The hook in
//!    `frontend/src/hooks/useTauriMenu.ts` listens for these. The IPC event
//!    bus (`app.emit`) is not used because `withGlobalTauri: false` (see
//!    tauri.conf.json) — JS cannot subscribe without `@tauri-apps/api`
//!    imports, and that surface is intentionally kept at zero (see
//!    Cargo.toml + capabilities). The injected scripts contain only ASCII
//!    literals (no user input), so script injection is not a concern.

use std::time::{SystemTime, UNIX_EPOCH};

use tauri::menu::{Menu, MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, Wry};

/// Frontend-delegated menu ids — the ones handled by the JS hook in
/// `frontend/src/hooks/useTauriMenu.ts`. Listed here as the single source
/// of truth so lib.rs + tests can cross-check.
pub const FRONTEND_MENU_IDS: &[&str] = &[
    "export_db",
    "import_db",
    "toggle_theme",
    "show_shortcuts",
    "api_docs",
    "file_open",
];

/// True when an id should be handed to the frontend (vs. handled in Rust).
pub fn is_frontend_menu_id(id: &str) -> bool {
    FRONTEND_MENU_IDS.contains(&id)
}

/// Map a navigation menu id to its route. Returns `None` for non-nav ids.
/// Kept here so lib.rs doesn't drift from the menu definition.
pub fn nav_route_for(id: &str) -> Option<&'static str> {
    match id {
        "nav_overview" => Some("/"),
        "nav_feed" => Some("/feed"),
        "nav_replay" => Some("/replay"),
        "nav_analysis" => Some("/map"),
        "nav_model" => Some("/model-controls"),
        "help_open" => Some("/help"),
        "file_new_paste" => Some("/replay"),
        "sidecar_health" => Some("/model-controls"),
        "help_logs" => Some("/logs"),
        _ => None,
    }
}

/// Inject a `CustomEvent('catchem:menu', {detail:<id>})` into the main
/// webview so the React hook can pick it up. Uses a per-id hardcoded
/// script literal (no string interpolation from any external source) since
/// `withGlobalTauri: false` blocks the JS-side `app.emit` listener path.
///
/// Returns `true` when the dispatch was attempted (regardless of webview
/// presence); `false` if the id isn't a known frontend-delegated action.
pub fn dispatch_frontend_menu(handle: &AppHandle, id: &str) -> bool {
    // Hardcoded literals — no interpolation from external input.
    let script: &str = match id {
        "export_db" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'export_db'}))}catch(e){}"
        }
        "import_db" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'import_db'}))}catch(e){}"
        }
        "toggle_theme" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'toggle_theme'}))}catch(e){}"
        }
        "show_shortcuts" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'show_shortcuts'}))}catch(e){}"
        }
        "api_docs" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'api_docs'}))}catch(e){}"
        }
        "file_open" => {
            "try{window.dispatchEvent(new CustomEvent('catchem:menu',{detail:'file_open'}))}catch(e){}"
        }
        _ => return false,
    };
    if let Some(win) = handle.get_webview_window("main") {
        if let Err(e) = win.eval(script) {
            log::warn!("menu dispatch_frontend_menu failed id={id} err={e}");
        }
    }
    true
}

/// Reload the main webview (Cmd+R menu item). Kept alongside dispatch so
/// lib.rs stays focused on routing menu events to the right helper.
pub fn reload_main_webview(handle: &AppHandle) {
    if let Some(win) = handle.get_webview_window("main") {
        if let Err(e) = win.eval("window.location.reload()") {
            log::warn!("menu reload_main_webview failed: {e}");
        }
    }
}

/// Per-process counter for cascading offsets so a flurry of ⌘N presses
/// don't pile every new window on top of the last one. Wraps every 8
/// windows to stay on-screen on small displays.
static WINDOW_OFFSET_COUNT: std::sync::atomic::AtomicU32 =
    std::sync::atomic::AtomicU32::new(0);

/// Build a unique label for a secondary window. Uses `SystemTime` instead
/// of pulling `chrono` (see Cargo.toml comment: "Hinnant date.h... Avoids
/// chrono"). Falls back to the offset counter on the wildly unlikely
/// `SystemTime::now() < UNIX_EPOCH` case.
fn next_secondary_label(seq: u32) -> String {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
        .wrapping_add(seq as u64);
    format!("secondary_{ms}")
}

/// Open a new Tauri webview window pointing at the same sidecar URL the
/// main window uses. Each new window cascades 60px down/right from the
/// previous one (mod 8) so consecutive ⌘N presses produce a staggered
/// pile rather than dead-overlap. The new window inherits the SAME
/// `on_navigation` security guard the main window has (only same-origin
/// 127.0.0.1:8087 / localhost:8087 stays in-webview; everything else is
/// classified by `security::classify_navigation`).
///
/// Sidecar is shared — no new backend instance is spawned. Watchlist,
/// theme, and recent commands round-trip via localStorage (same origin,
/// same browser storage), so secondary windows see the main window's
/// state on the next localStorage read.
pub fn open_secondary_window(
    handle: &AppHandle,
    endpoint: &str,
    nav_host: &str,
    nav_port: u16,
) -> tauri::Result<String> {
    let seq = WINDOW_OFFSET_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let label = next_secondary_label(seq);
    // Endpoint is hardcoded at boot in lib.rs (sidecar.endpoint() reads the
    // same SidecarConfig fields the main window used), so this parse arm is
    // defence-in-depth only.
    let url = tauri::Url::parse(endpoint).map_err(tauri::Error::InvalidUrl)?;

    // Cascade: 60px steps, wrap at 8 to stay on-screen.
    let cascade_step = (seq % 8) as f64;
    let base_x = 120.0;
    let base_y = 120.0;
    let pos_x = base_x + cascade_step * 60.0;
    let pos_y = base_y + cascade_step * 60.0;

    let nav_host_owned = nav_host.to_string();
    let win = WebviewWindowBuilder::new(handle, &label, WebviewUrl::External(url))
        .title("Catchem — Secondary Window")
        .inner_size(1200.0, 800.0)
        .min_inner_size(980.0, 640.0)
        .position(pos_x, pos_y)
        .resizable(true)
        .visible(true)
        .on_navigation(move |url| {
            let url_str = url.as_str();
            match crate::security::classify_navigation(url_str, &nav_host_owned, nav_port) {
                crate::security::NavigationDecision::AllowInWebview => true,
                crate::security::NavigationDecision::OpenExternal => {
                    let url_owned = url_str.to_string();
                    std::thread::spawn(move || {
                        if let Err(e) = open::that_detached(&url_owned) {
                            log::warn!(
                                "secondary on_nav open_external failed url={url_owned} err={e}"
                            );
                        }
                    });
                    false
                }
                crate::security::NavigationDecision::Block => {
                    log::info!("secondary blocked navigation: {url_str}");
                    false
                }
            }
        })
        .build()?;

    log::info!(
        "secondary window opened label={} pos=({},{}) url={}",
        win.label(),
        pos_x,
        pos_y,
        endpoint
    );
    Ok(label)
}

/// Build the standard macOS menu bar.
pub fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
    // App submenu — macOS convention: first menu carries the app name and
    // owns the About / Hide / Quit lifecycle.
    let app_submenu = SubmenuBuilder::new(app, "Catchem")
        .item(&PredefinedMenuItem::about(app, Some("About Catchem"), None)?)
        .separator()
        .item(&PredefinedMenuItem::services(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::hide(app, None)?)
        .item(&PredefinedMenuItem::hide_others(app, None)?)
        .item(&PredefinedMenuItem::show_all(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::quit(app, None)?)
        .build()?;

    // File submenu — DB export/import are frontend-delegated (open in new
    // tab / route to Settings#database). Open article + New paste are
    // existing nav entries kept for back-compat. New Window (⌘N) spawns
    // a second analyst dashboard pointing at the same sidecar; "New paste
    // analysis" moves to Cmd+Shift+N to free the canonical ⌘N for window
    // creation (v30 task #112).
    let file_submenu = SubmenuBuilder::new(app, "File")
        .item(
            &MenuItemBuilder::new("New Window")
                .id("new_window")
                .accelerator("CmdOrCtrl+N")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Open article…")
                .id("file_open")
                .accelerator("CmdOrCtrl+O")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("New paste analysis")
                .id("file_new_paste")
                .accelerator("CmdOrCtrl+Shift+N")
                .build(app)?,
        )
        .separator()
        .item(
            &MenuItemBuilder::new("Export Database…")
                .id("export_db")
                .accelerator("CmdOrCtrl+Shift+E")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Import Database…")
                .id("import_db")
                .accelerator("CmdOrCtrl+Shift+I")
                .build(app)?,
        )
        .separator()
        .item(&PredefinedMenuItem::close_window(app, None)?)
        .build()?;

    // Edit submenu — pure system predefineds so the webview's input
    // elements get the expected cut/copy/paste/select-all behaviour.
    let edit_submenu = SubmenuBuilder::new(app, "Edit")
        .item(&PredefinedMenuItem::undo(app, None)?)
        .item(&PredefinedMenuItem::redo(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::cut(app, None)?)
        .item(&PredefinedMenuItem::copy(app, None)?)
        .item(&PredefinedMenuItem::paste(app, None)?)
        .item(&PredefinedMenuItem::select_all(app, None)?)
        .build()?;

    // View submenu — existing nav routes kept; Reload + Toggle Theme +
    // Fullscreen layered on. Reload uses Cmd+R; Sidecar Restart moves to
    // Cmd+Alt+R to avoid the clash.
    let view_submenu = SubmenuBuilder::new(app, "View")
        .item(
            &MenuItemBuilder::new("Reload")
                .id("reload")
                .accelerator("CmdOrCtrl+R")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Toggle Theme")
                .id("toggle_theme")
                .accelerator("CmdOrCtrl+Shift+L")
                .build(app)?,
        )
        .separator()
        .item(
            &MenuItemBuilder::new("Overview")
                .id("nav_overview")
                .accelerator("CmdOrCtrl+1")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Live Feed")
                .id("nav_feed")
                .accelerator("CmdOrCtrl+2")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Replay/Upload")
                .id("nav_replay")
                .accelerator("CmdOrCtrl+3")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Analysis")
                .id("nav_analysis")
                .accelerator("CmdOrCtrl+4")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Model Controls")
                .id("nav_model")
                .accelerator("CmdOrCtrl+5")
                .build(app)?,
        )
        .separator()
        .item(&PredefinedMenuItem::fullscreen(app, None)?)
        .build()?;

    // Sidecar submenu — catchem-specific lifecycle controls. Restart uses
    // Cmd+Alt+R so it doesn't fight Cmd+R (Reload).
    let sidecar_submenu = SubmenuBuilder::new(app, "Sidecar")
        .item(
            &MenuItemBuilder::new("Restart sidecar")
                .id("sidecar_restart")
                .accelerator("CmdOrCtrl+Alt+R")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Stop sidecar")
                .id("sidecar_stop")
                .build(app)?,
        )
        .separator()
        .item(
            &MenuItemBuilder::new("Check health")
                .id("sidecar_health")
                .build(app)?,
        )
        .build()?;

    // Help submenu — Help route, keyboard shortcut overlay, API docs.
    let help_submenu = SubmenuBuilder::new(app, "Help")
        .item(
            &MenuItemBuilder::new("Catchem Help")
                .id("open_help")
                .accelerator("CmdOrCtrl+?")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Keyboard Shortcuts")
                .id("show_shortcuts")
                .build(app)?,
        )
        .separator()
        .item(
            &MenuItemBuilder::new("API Reference")
                .id("api_docs")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("Show logs")
                .id("help_logs")
                .build(app)?,
        )
        .build()?;

    MenuBuilder::new(app)
        .item(&app_submenu)
        .item(&file_submenu)
        .item(&edit_submenu)
        .item(&view_submenu)
        .item(&sidecar_submenu)
        .item(&help_submenu)
        .build()
}
