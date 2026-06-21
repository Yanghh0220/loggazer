//! LogPilot Desktop — Tauri application library.
//!
//! Registers all Tauri commands from the `logpilot-parser` crate
//! and manages application state.

use logpilot_parser::tauri_commands::{
    categorize_error_command, full_single_pass_command, get_scan_stats,
    hydrate_log_detail_command, parse_log_range_command, scan_log_stage1_command, ParserState,
};

/// Initialize the Tauri application with all commands and state.
pub fn run() {
    tauri::Builder::default()
        // Register our managed state
        .manage(ParserState::default())
        // Register the Tauri shell plugin
        .plugin(tauri_plugin_shell::init())
        // Stage 1: Fast log file scan
        .invoke_handler(tauri::generate_handler![
            scan_log_stage1_command,
            parse_log_range_command,
            hydrate_log_detail_command,
            full_single_pass_command,
            categorize_error_command,
            get_scan_stats,
        ])
        .run(tauri::generate_context!())
        .expect("error while running LogPilot Desktop");
}
