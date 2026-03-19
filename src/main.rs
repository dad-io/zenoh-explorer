// Hide console window on Windows in release builds
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Zenoh Explorer - A native GUI application for exploring and debugging Zenoh networks.
//!
//! This application provides real-time monitoring, message inspection, publishing,
//! querying, and browsing capabilities for Zenoh networks. It uses a dual-thread
//! architecture with egui/eframe for the GUI and Tokio for async Zenoh operations.

mod app;
mod colors;
mod events;
mod transfer;
mod types;
mod ui;
mod zenoh_worker;

use app::ZenohExplorer;
use eframe::egui;
use tracing::info;

/// Application entry point.
/// Initializes logging, configures the native window, and launches the GUI.
fn main() -> eframe::Result<()> {
    // Set up panic hook to log crashes on Windows (since there's no console)
    #[cfg(target_os = "windows")]
    {
        std::panic::set_hook(Box::new(|panic_info| {
            let msg = format!("Zenoh Explorer crashed: {}\n", panic_info);
            if let Ok(exe_path) = std::env::current_exe() {
                let log_path = exe_path.with_file_name("crash.log");
                let _ = std::fs::write(&log_path, &msg);
            }
            if let Some(home) = std::env::var_os("USERPROFILE") {
                let log_path = std::path::PathBuf::from(home).join("zenoh-explorer-crash.log");
                let _ = std::fs::write(log_path, &msg);
            }
        }));
    }

    // Initialize tracing for debug logging
    tracing_subscriber::fmt::init();

    info!("Zenoh Explorer starting...");

    // Configure the native window with appropriate size and title
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1400.0, 900.0])
            .with_min_inner_size([1000.0, 600.0])
            .with_title("Zenoh Explorer")
            .with_visible(true)
            .with_active(true),
        renderer: eframe::Renderer::Glow,
        hardware_acceleration: eframe::HardwareAcceleration::Preferred,
        ..Default::default()
    };

    // Launch the application
    info!("Launching eframe application...");
    eframe::run_native(
        "Zenoh Explorer",
        options,
        Box::new(|_cc| {
            info!("Creating Zenoh Explorer instance...");
            Ok(Box::new(ZenohExplorer::new()))
        }),
    )
}
