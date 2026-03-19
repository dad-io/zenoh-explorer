//! Messages tab rendering.

use egui::{Color32, RichText};

use crate::types::*;
use crate::app::ZenohExplorer;

/// Trait for messages tab rendering.
pub trait MessagesUI {
    fn show_messages_tab(&mut self, ui: &mut egui::Ui);
}

impl MessagesUI for ZenohExplorer {
    /// Renders the Messages tab UI.
    /// Shows all network activity with filtering and auto-scroll capabilities.
    fn show_messages_tab(&mut self, ui: &mut egui::Ui) {
        // Message controls toolbar
        ui.horizontal(|ui| {
            ui.label("Filter:");
            ui.text_edit_singleline(&mut self.message_filter);
            ui.checkbox(&mut self.auto_scroll, "Auto-scroll");
            if ui.button("Clear").clicked() {
                self.messages.clear();
                self.current_memory_bytes = 0;
                self.messages_dropped = 0;
                self.rate_limit_drops = 0;
            }

            ui.separator();
            ui.label(format!("Messages: {}", self.messages.len()));
        });

        // Memory management controls
        ui.horizontal(|ui| {
            ui.label("Memory Limit (MB):");
            let mut limit_str = self.max_memory_mb.to_string();
            if ui.text_edit_singleline(&mut limit_str).changed() {
                if let Ok(new_limit) = limit_str.parse::<usize>() {
                    self.max_memory_mb = new_limit.max(10).min(1000); // Clamp between 10MB and 1GB
                }
            }

            ui.label("Message Limit:");
            let mut count_str = self.max_messages.to_string();
            if ui.text_edit_singleline(&mut count_str).changed() {
                if let Ok(new_limit) = count_str.parse::<usize>() {
                    self.max_messages = new_limit.max(100).min(50000); // Clamp between 100 and 50k
                }
            }

            ui.label("Rate Limit (msg/s):");
            let mut rate_str = self.rate_limiter.max_messages_per_second.to_string();
            if ui.text_edit_singleline(&mut rate_str).changed() {
                if let Ok(new_rate) = rate_str.parse::<usize>() {
                    self.rate_limiter.max_messages_per_second = new_rate.max(10).min(10000);
                    // 10-10k msg/s
                }
            }

            ui.checkbox(&mut self.dedup_enabled, "Dedup");
            if self.messages_deduped > 0 {
                ui.label(
                    RichText::new(format!("({} deduped)", self.messages_deduped))
                        .color(self.text_secondary_color())
                        .size(TEXT_SMALL_SIZE),
                );
            }
        });

        egui::ScrollArea::vertical()
            .auto_shrink([false; 2])
            .stick_to_bottom(self.auto_scroll)
            .show(ui, |ui| {
                // Only render the last 500 messages to prevent UI lag with very large message counts
                const MAX_RENDERED_MESSAGES: usize = 500;
                let start_idx = self.messages.len().saturating_sub(MAX_RENDERED_MESSAGES);

                for message in self.messages.iter().skip(start_idx) {
                    // OPTIMIZED: Only search first 4KB of payload to avoid O(n) on large payloads
                    let search_end = safe_truncate_index(&message.payload, MAX_HASH_BYTES);
                    let payload_search_slice = &message.payload[..search_end];
                    if self.message_filter.is_empty()
                        || message.key.contains(&self.message_filter)
                        || payload_search_slice.contains(&self.message_filter)
                    {
                        ui.horizontal(|ui| {
                            // Message type badge
                            ui.label(
                                RichText::new(message.message_type.label())
                                    .background_color(message.message_type.color())
                                    .color(Color32::WHITE)
                                    .size(TEXT_SMALL_SIZE),
                            );

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

                        // Payload (truncated)
                        if !message.payload.is_empty() {
                            let display_payload = if message.payload.len() > 200 {
                                let end = safe_truncate_index(&message.payload, 200);
                                format!("{}...", &message.payload[..end])
                            } else {
                                message.payload.clone()
                            };
                            ui.label(
                                RichText::new(display_payload)
                                    .color(self.text_secondary_color())
                                    .size(TEXT_SMALL_SIZE),
                            );
                        }

                        ui.separator();
                    }
                }
            });
    }
}
