//! Native menu bar definition for Catchem.

use tauri::menu::{Menu, MenuBuilder, MenuItemBuilder, PredefinedMenuItem, SubmenuBuilder};
use tauri::{AppHandle, Wry};

/// Build the standard macOS menu bar.
pub fn build_menu(app: &AppHandle) -> tauri::Result<Menu<Wry>> {
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

    let file_submenu = SubmenuBuilder::new(app, "File")
        .item(
            &MenuItemBuilder::new("Open article…")
                .id("file_open")
                .accelerator("CmdOrCtrl+O")
                .build(app)?,
        )
        .item(
            &MenuItemBuilder::new("New paste analysis")
                .id("file_new_paste")
                .accelerator("CmdOrCtrl+N")
                .build(app)?,
        )
        .separator()
        .item(&PredefinedMenuItem::close_window(app, None)?)
        .build()?;

    let edit_submenu = SubmenuBuilder::new(app, "Edit")
        .item(&PredefinedMenuItem::undo(app, None)?)
        .item(&PredefinedMenuItem::redo(app, None)?)
        .separator()
        .item(&PredefinedMenuItem::cut(app, None)?)
        .item(&PredefinedMenuItem::copy(app, None)?)
        .item(&PredefinedMenuItem::paste(app, None)?)
        .item(&PredefinedMenuItem::select_all(app, None)?)
        .build()?;

    let view_submenu = SubmenuBuilder::new(app, "View")
        .item(&MenuItemBuilder::new("Overview").id("nav_overview").accelerator("CmdOrCtrl+1").build(app)?)
        .item(&MenuItemBuilder::new("Live Feed").id("nav_feed").accelerator("CmdOrCtrl+2").build(app)?)
        .item(&MenuItemBuilder::new("Replay/Upload").id("nav_replay").accelerator("CmdOrCtrl+3").build(app)?)
        .item(&MenuItemBuilder::new("Analysis").id("nav_analysis").accelerator("CmdOrCtrl+4").build(app)?)
        .item(&MenuItemBuilder::new("Model Controls").id("nav_model").accelerator("CmdOrCtrl+5").build(app)?)
        .separator()
        .item(&PredefinedMenuItem::fullscreen(app, None)?)
        .build()?;

    let sidecar_submenu = SubmenuBuilder::new(app, "Sidecar")
        .item(&MenuItemBuilder::new("Restart sidecar").id("sidecar_restart").accelerator("CmdOrCtrl+R").build(app)?)
        .item(&MenuItemBuilder::new("Stop sidecar").id("sidecar_stop").build(app)?)
        .separator()
        .item(&MenuItemBuilder::new("Check health").id("sidecar_health").build(app)?)
        .build()?;

    let help_submenu = SubmenuBuilder::new(app, "Help")
        .item(&MenuItemBuilder::new("Open Help").id("help_open").accelerator("CmdOrCtrl+?").build(app)?)
        .item(&MenuItemBuilder::new("Show logs").id("help_logs").build(app)?)
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
