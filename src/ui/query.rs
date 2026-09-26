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
        // Say why the Query button is disabled, in neutral text
        if let Some(notice) = connection_notice(&self.connection_status) {
            ui.label(RichText::new(notice).color(self.text_secondary_color()));
            ui.separator();
        }

        // Explain query functionality
        ui.label(
            RichText::new(
                "Asks every queryable that matches the selector. With no match the answer comes back at once; a matching queryable that stays silent is reported when the timeout expires.",
            )
            .color(self.text_secondary_color())
            .size(TEXT_SMALL_SIZE),
        );
        // What this app's own queryable would answer
        let served = self
            .local_kvstore
            .read()
            .map_or(0, |s| served_count(&s, &self.queryable_pattern));
        let summary = queryable_summary(self.queryable_enabled, &self.queryable_pattern, served);
        ui.label(
            RichText::new(summary)
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
            let selector_err = crate::validation::selector_error(&self.query_selector);
            if let Some(err) = &selector_err {
                ui.colored_label(ExplorerColors::ERROR, err);
            }
            ui.horizontal(|ui| {
                ui.label("Value (optional):");
                ui.text_edit_singleline(&mut self.query_value);
            });
            ui.horizontal(|ui| {
                ui.label("Timeout (ms):");
                ui.text_edit_singleline(&mut self.query_timeout);
            });
            let timeout_err = crate::validation::timeout_error(&self.query_timeout);
            if let Some(err) = &timeout_err {
                ui.colored_label(ExplorerColors::ERROR, err);
            }
            // Query button - only enabled when connected and the inputs are valid
            let button = egui::Button::new("Query");
            if ui
                .add_enabled(
                    matches!(self.connection_status, ConnectionStatus::Connected)
                        && selector_err.is_none()
                        && timeout_err.is_none(),
                    button,
                )
                .clicked()
            {
                if let Some(sender) = &self.command_sender {
                    let timeout = self.query_timeout.trim().parse().expect("validated");
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
                                        RichText::new(format_local_time(
                                            &message.timestamp,
                                            &chrono::Utc::now(),
                                        ))
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

/// Why the network actions are unavailable, or None when connected.
/// Neutral wording: being disconnected is a state, not an error.
pub(crate) fn connection_notice(status: &ConnectionStatus) -> Option<&'static str> {
    match status {
        ConnectionStatus::Connected => None,
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
            Some("Connecting… available once connected")
        }
        ConnectionStatus::Disconnected => Some("Not connected"),
        ConnectionStatus::Error(_) => {
            Some("Not connected: the last connection attempt failed (see Connection Settings)")
        }
    }
}

/// One line saying what this app's own queryable answers.
fn queryable_summary(enabled: bool, pattern: &str, stored: usize) -> String {
    if !enabled {
        return "This app's queryable is off (Publish tab)".to_string();
    }
    match stored {
        0 => format!(
            "This app answers {pattern} but has published nothing under it yet (Publish tab)"
        ),
        1 => format!("This app answers {pattern} from 1 value it published (Publish tab)"),
        n => format!("This app answers {pattern} from {n} values it published (Publish tab)"),
    }
}

/// How many stored keys the queryable pattern includes, which is what the
/// queryable serves. An unparsable pattern serves nothing.
fn served_count(store: &LocalKvStore, pattern: &str) -> usize {
    let Ok(pattern) = zenoh::key_expr::keyexpr::new(pattern) else {
        return 0;
    };
    store
        .keys()
        .filter(|k| zenoh::key_expr::keyexpr::new(k.as_str()).is_ok_and(|k| pattern.includes(k)))
        .count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connection_notice_matches_state() {
        assert_eq!(connection_notice(&ConnectionStatus::Connected), None);
        assert_eq!(
            connection_notice(&ConnectionStatus::ConnectingMonitor),
            Some("Connecting… available once connected")
        );
        assert_eq!(
            connection_notice(&ConnectionStatus::Disconnected),
            Some("Not connected")
        );
        assert_eq!(
            connection_notice(&ConnectionStatus::Error("x".into())),
            Some("Not connected: the last connection attempt failed (see Connection Settings)")
        );
    }

    #[test]
    fn queryable_summary_words() {
        assert_eq!(
            queryable_summary(false, "**", 0),
            "This app's queryable is off (Publish tab)"
        );
        assert_eq!(
            queryable_summary(true, "demo/**", 3),
            "This app answers demo/** from 3 values it published (Publish tab)"
        );
        assert_eq!(
            queryable_summary(true, "demo/**", 1),
            "This app answers demo/** from 1 value it published (Publish tab)"
        );
        assert_eq!(
            queryable_summary(true, "demo/**", 0),
            "This app answers demo/** but has published nothing under it yet (Publish tab)"
        );
        // The count is what the pattern serves (G1-2 follow-on). It stays in this
        // test: P5's `cargo test -- query_book:: ui::query` expects 12 tests.
        let store: LocalKvStore = ["demo/a", "other/x"]
            .iter()
            .map(|k| {
                (
                    k.to_string(),
                    StoredValue {
                        bytes: vec![],
                        encoding: "text/plain".into(),
                    },
                )
            })
            .collect();
        assert_eq!(served_count(&store, "demo/**"), 1);
        assert_eq!(served_count(&store, "**"), 2);
        assert_eq!(
            served_count(&store, "demo/"),
            0,
            "an invalid pattern serves nothing"
        );
    }
}
