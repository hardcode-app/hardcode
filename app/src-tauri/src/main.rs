//! The executable: everything is in the library (`lib.rs`).

// No extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    hardcode_app_lib::run()
}
