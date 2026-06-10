//! Query tab rendering.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::colors::ExplorerColors;
use crate::types::*;

/// Trait for query tab rendering.
pub trait QueryUI {
    fn show_query_tab(&mut self, ui: &mut egui::Ui);
}

impl QueryUI for ZenohExplorer {
    /// Renders the Query tab UI.
    /// Allows users to request data from the network using selectors.
    fn show_query_tab(&mut self, ui: &mut egui::Ui) {
        // Show warning if not connected
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(
                ExplorerColors::ERROR,
                "⚠ Not connected. Please connect first.",
            );
            ui.separator();
        }

        // Explain query functionality
        ui.label(
            RichText::new(
                "Note: Queries require queryables (services) running on the network to respond.",
            )
            .color(self.text_secondary_color())
            .size(TEXT_SMALL_SIZE),
        );
        ui.label(
            RichText::new("If no queryables are running, queries will timeout with no results.")
                .color(self.text_secondary_color())
                .size(TEXT_SMALL_SIZE),
        );
        ui.separator();

        // Show query alert if present
        if self.query_alert.is_some() {
            let mut dismiss = false;
            ui.group(|ui| {
                ui.colored_label(ExplorerColors::WARNING, "Query Alert");
                if let Some(alert) = &self.query_alert {
                    ui.label(alert);
                }
                if ui.button("Dismiss").clicked() {
                    dismiss = true;
                }
            });
            if dismiss {
                self.query_alert = None;
            }
            ui.separator();
        }
        ui.group(|ui| {
            ui.label("Query Data");
            ui.horizontal(|ui| {
                ui.label("Selector:");
                ui.text_edit_singleline(&mut self.query_selector);
            });
            ui.horizontal(|ui| {
                ui.label("Value (optional):");
                ui.text_edit_singleline(&mut self.query_value);
            });
            ui.horizontal(|ui| {
                ui.label("Timeout (ms):");
                ui.text_edit_singleline(&mut self.query_timeout);
            });
            // Query button - only enabled when connected
            let button = egui::Button::new("Query");
            if ui
                .add_enabled(
                    matches!(self.connection_status, ConnectionStatus::Connected)
                        && !self.query_selector.is_empty(),
                    button,
                )
                .clicked()
            {
                if let Some(sender) = &self.command_sender {
                    let timeout = self.query_timeout.parse().unwrap_or(10000);
                    let _ = sender.send(ZenohCommand::Query {
                        selector: self.query_selector.clone(),
                        value: self.query_value.clone(),
                        timeout_ms: timeout,
                    });

                    // Provide immediate feedback that query was sent
                    self.query_alert = Some(format!(
                        "Query sent for '{}'. Waiting for responses...",
                        self.query_selector
                    ));
                }
            }
        });

        ui.add_space(16.0);

        // Show query results
        ui.group(|ui| {
            ui.label(RichText::new("Query Results").strong());
            ui.separator();

            // Filter messages to show only QueryReply type (clone to avoid borrow conflicts)
            let query_replies: Vec<ZenohMessage> = self
                .messages
                .iter()
                .filter(|m| m.message_type == MessageType::QueryReply)
                .rev() // Most recent first
                .take(50) // Limit to last 50 replies
                .cloned()
                .collect();

            if query_replies.is_empty() {
                ui.vertical_centered(|ui| {
                    ui.add_space(16.0);
                    ui.label(
                        RichText::new("No query results yet")
                            .size(HEADING_MEDIUM_SIZE)
                            .color(self.text_tertiary_color()),
                    );
                    ui.add_space(4.0);
                    ui.label(
                        RichText::new("Send a query to see results here")
                            .italics()
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_secondary_color()),
                    );
                    ui.add_space(16.0);
                });
            } else {
                egui::ScrollArea::vertical()
                    .auto_shrink([false; 2])
                    .max_height(400.0)
                    .show(ui, |ui| {
                        for message in &query_replies {
                            ui.group(|ui| {
                                ui.horizontal(|ui| {
                                    // Local indicator
                                    if message.is_local {
                                        ui.label(RichText::new("●").size(8.0).color(
                                            if self.dark_mode {
                                                ExplorerColors::DARK_SUCCESS
                                            } else {
                                                ExplorerColors::SUCCESS
                                            },
                                        ))
                                        .on_hover_text("From local queryable");
                                    }

                                    // Timestamp
                                    ui.label(
                                        RichText::new(
                                            message.timestamp.format("%H:%M:%S%.3f").to_string(),
                                        )
                                        .color(self.text_secondary_color())
                                        .size(TEXT_SMALL_SIZE),
                                    );

                                    // Key
                                    ui.label(RichText::new(&message.key).strong());
                                });

                                // Payload
                                if !message.payload.is_empty() {
                                    let display_payload = if message.payload.len() > 500 {
                                        let end = safe_truncate_index(&message.payload, 500);
                                        format!("{}...", &message.payload[..end])
                                    } else {
                                        message.payload.clone()
                                    };

                                    // Try to parse as JSON for pretty display (using cache)
                                    if let Some(pretty) = self.get_cached_json(&display_payload) {
                                        ui.label(
                                            RichText::new(pretty)
                                                .code()
                                                .color(self.text_color())
                                                .size(TEXT_SMALL_SIZE),
                                        );
                                    } else {
                                        ui.label(
                                            RichText::new(display_payload)
                                                .color(self.text_secondary_color())
                                                .size(TEXT_SMALL_SIZE),
                                        );
                                    }
                                }
                            });
                        }
                    });
            }
        });
    }
}
