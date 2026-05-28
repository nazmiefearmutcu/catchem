import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { OPEN_SHORTCUT_OVERLAY_EVENT } from "@/components/CommandPalette";
import { useTheme } from "@/hooks/useTheme";

// Action ids the Rust menu router dispatches via
// `window.dispatchEvent(new CustomEvent('catchem:menu', { detail: <id> }))`.
// Kept as a string union so this hook's switch stays in sync with the Rust
// allowlist in `desktop/catchem/src-tauri/src/menu.rs::FRONTEND_MENU_IDS`.
//
// `new_window` is special — Rust handles the menu (⌘N) item directly via
// `menu::open_secondary_window()`, so the Rust side does NOT dispatch this
// id through the CustomEvent bridge. The CommandPalette action redispatches
// the same event id so users who reach for ⌘K instead of File→New Window
// get a consistent code path. When fired from a plain browser (vite
// preview, vitest) the hook falls back to `window.open()` so the action is
// observable; in the Tauri shell the menu accelerator is the canonical
// trigger and the palette dispatch becomes a no-op (browser pop-up blocker
// + cross-origin policy combine to suppress it).
export type MenuAction =
  | "export_db"
  | "import_db"
  | "toggle_theme"
  | "show_shortcuts"
  | "api_docs"
  | "file_open"
  | "new_window";

/** Public for tests — the DOM event name Rust dispatches into. */
export const MENU_EVENT = "catchem:menu";

/**
 * Bridge native macOS menu actions -> React behaviour.
 *
 * Wired in `Shell.tsx` so it lives one node up from `<Outlet/>`; any
 * route can therefore trigger nav/theme/help without registering its own
 * listener.
 *
 * Why `CustomEvent` instead of `@tauri-apps/api/event`? `withGlobalTauri:
 * false` (see `tauri.conf.json`) keeps `window.__TAURI__` undefined and
 * the JS bundle has zero `@tauri-apps/api` imports anywhere. Rust injects
 * the CustomEvent directly so this hook works in dev (vite preview), in
 * the bundled .app, AND in a plain browser test environment.
 */
export function useTauriMenu(): void {
  const navigate = useNavigate();
  const { toggle } = useTheme();

  useEffect(() => {
    const handler = (raw: Event) => {
      const evt = raw as CustomEvent<string>;
      const action = (evt.detail || "") as MenuAction | "";
      switch (action) {
        case "export_db":
          // Same-origin GET — sidecar streams the SQLite blob as a download.
          window.open("/api/db/export", "_blank");
          break;
        case "import_db":
          // Settings page hosts the file-picker UX (see v26 task #98).
          navigate("/settings#database");
          break;
        case "toggle_theme":
          toggle();
          break;
        case "show_shortcuts":
          // The existing ShortcutOverlay already listens for this DOM
          // event when the user hits "?" — reuse it.
          window.dispatchEvent(new Event(OPEN_SHORTCUT_OVERLAY_EVENT));
          break;
        case "api_docs":
          // FastAPI's auto-generated Swagger UI. on_navigation in lib.rs
          // routes the new-tab open through the system browser so this
          // works in both dev and bundle.
          window.open("/api/docs", "_blank");
          break;
        case "file_open":
          // Drop the user on the upload page. The Tauri file picker dialog
          // plugin is intentionally not bundled (see Cargo.toml comment),
          // so this hand-off is the most honest behaviour.
          navigate("/replay");
          break;
        case "new_window":
          // In the Tauri shell, this event normally never reaches the
          // hook — the File→New Window menu (⌘N) is wired directly in
          // Rust (`menu::open_secondary_window` via `on_menu_event` in
          // lib.rs). The CommandPalette redispatches the same id for
          // ⌘K users, so we mirror the behaviour here as a best-effort
          // fallback: `window.open(window.location.href)` opens a new
          // browser tab in dev / preview, which gives the analyst the
          // same dashboard URL even when the menu accelerator isn't
          // available. In the Tauri shell, pop-up policy + the
          // `on_navigation` classifier in `lib.rs` will route this
          // through the system browser if it fires; the Rust-side menu
          // remains the canonical multi-window entry point.
          try {
            window.open(window.location.href, "_blank", "noopener");
          } catch {
            /* pop-up blockers + Tauri navigation policy — best effort */
          }
          break;
        default:
          // Unknown action — log in dev, ignore in prod.
          if (import.meta.env?.DEV) {
            // eslint-disable-next-line no-console
            console.warn("[useTauriMenu] unknown menu action:", action);
          }
      }
    };
    window.addEventListener(MENU_EVENT, handler as EventListener);
    return () => window.removeEventListener(MENU_EVENT, handler as EventListener);
  }, [navigate, toggle]);
}
