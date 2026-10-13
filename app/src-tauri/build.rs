//! Tauri's build step: embeds the configuration and the page, and generates
//! the permissions for the commands the page may call.

fn main() {
    tauri_build::build()
}
