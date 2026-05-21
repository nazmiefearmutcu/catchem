// Prevent extra console window on Windows release builds (no-op on macOS).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    catchem_lib::run()
}
