//! Help tab rendering.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::types::*;

/// Trait for help tab rendering.
pub trait HelpUI {
    fn show_help_tab(&mut self, ui: &mut egui::Ui);
}

impl HelpUI for ZenohExplorer {
    /// Renders the Help tab UI.
    /// Provides usage instructions and examples for new users.
    fn show_help_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(
            RichText::new("Zenoh Explorer Help")
                .size(HEADING_MEDIUM_SIZE)
                .strong(),
        );
        ui.separator();

        ui.label("This is a Zenoh-based peer & client messaging utility.");
        ui.separator();

        ui.label(RichText::new("Getting Started:").strong());
        ui.label("1. Configure connection settings and click Connect.");
        ui.label("   • For a quick peer mesh, leave as Peer & Address field blank and select the tcp port of your peers (7447 by default)");
        ui.label("   • EARLY VERSION: Only tcp transport and multicast have been tested");
        ui.label("2. Use Subscribe tab to listen to key expressions (e.g., demo/**)");
        ui.label(
            "3. Use Publish tab to send data. Enter text or import files of any size or type.",
        );
        ui.label("4. Use Browse tab to explore the keyspace tree and see live updates");
        ui.label("5. Use Messages tab to see all messaging activity");
        ui.label("6. Enable simple Queryables service (optional, respond to queries for items in keyspace)");

        ui.separator();
        ui.label(RichText::new("Connection Modes:").strong());
        ui.label("• Client Mode: Connect to Zenoh routers");
        ui.label("• Peer Mode: Participate as a peer in a mesh network (EARLY VERSION: requires multicast & open firewalls");

        ui.separator();
        ui.label(RichText::new("Key Expression Examples:").strong());
        ui.label("• ** - Match all keys");
        ui.label("• demo/** - Match all keys under demo/");
        ui.label("• sensor/*/temperature - Match temperature under any sensor");
        ui.label("• device/1/status - Match exact key");

        ui.separator();
        ui.label(RichText::new("Performance Tips:").strong());
        ui.label("• Adjust memory limit in Messages tab (default: 100MB)");
        ui.label("• Older messages are dropped when limits are exceeded");
        ui.label("• All messages greater than 10MB are displayed with truncation");
    }
}
