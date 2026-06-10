//! Publish tab and queryable section rendering.

use egui::RichText;
use tracing::{error, info};

use crate::app::ZenohExplorer;
use crate::colors::ExplorerColors;
use crate::types::*;

/// Trait for publish tab rendering.
pub trait PublishUI {
    fn show_publish_tab(&mut self, ui: &mut egui::Ui);
}

impl PublishUI for ZenohExplorer {
    /// Renders the Publish tab UI.
    /// Allows users to send data to any key in the Zenoh network.
    fn show_publish_tab(&mut self, ui: &mut egui::Ui) {
        // Show warning if not connected
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(
                ExplorerColors::ERROR,
                "⚠ Not connected. Please connect first.",
            );
            ui.separator();
        }
        ui.group(|ui| {
            ui.label("Publish Data");
            ui.horizontal(|ui| {
                ui.label("Key:");
                ui.text_edit_singleline(&mut self.publish_key);
            });

            // Payload section with file import
            ui.horizontal(|ui| {
                ui.label("Payload:");
                if ui.button("Import File").clicked() {
                    if let Some(path) = rfd::FileDialog::new().pick_file() {
                        match std::fs::read(&path) {
                            Ok(bytes) => {
                                self.publish_payload_filename =
                                    path.file_name().map(|n| n.to_string_lossy().to_string());
                                self.publish_payload_expanded = false; // Start collapsed

                                // Generate initial preview (collapsed = 256 bytes for compact view)
                                let total_len = bytes.len();
                                let preview_len = total_len.min(256);

                                self.publish_payload = if let Ok(text) = std::str::from_utf8(&bytes)
                                {
                                    // Valid UTF-8 text - use safe truncation
                                    if total_len > preview_len {
                                        let safe_end = safe_truncate_index(text, preview_len);
                                        format!(
                                            "{}... [+{} bytes]",
                                            &text[..safe_end],
                                            total_len - safe_end
                                        )
                                    } else {
                                        text.to_string()
                                    }
                                } else {
                                    // Binary data - show hex dump (byte slicing is safe)
                                    let hex: String = bytes[..preview_len]
                                        .iter()
                                        .map(|b| format!("{:02x} ", b))
                                        .collect();
                                    if total_len > preview_len {
                                        format!(
                                            "{}... [+{} bytes, {} total]",
                                            hex.trim(),
                                            total_len - preview_len,
                                            total_len
                                        )
                                    } else {
                                        hex
                                    }
                                };

                                self.import_memory_bytes = bytes.len();
                                self.publish_payload_bytes = Some(bytes);
                                self.publish_encoding = "application/octet-stream".to_string();
                            }
                            Err(e) => {
                                self.publish_payload = format!("Error reading file: {}", e);
                                self.publish_payload_bytes = None;
                                self.publish_payload_filename = None;
                                self.publish_payload_expanded = false;
                                self.import_memory_bytes = 0;
                            }
                        }
                    }
                }
                if self.publish_payload_bytes.is_some() && ui.button("✖ Clear").clicked() {
                    self.publish_payload_bytes = None;
                    self.publish_payload_filename = None;
                    self.publish_payload_expanded = false;
                    self.import_memory_bytes = 0;
                    self.publish_payload = "Hello Zenoh!".to_string();
                    self.publish_encoding = "text/plain".to_string();
                }
            });

            // Show filename and expand/collapse if imported
            if let Some(ref filename) = self.publish_payload_filename.clone() {
                // Get byte info before entering closure
                let bytes_len = self.publish_payload_bytes.as_ref().map(|b| b.len());
                let was_expanded = self.publish_payload_expanded;
                let mut should_regenerate = false;

                ui.horizontal(|ui| {
                    ui.label(RichText::new(filename).color(self.text_secondary_color()));
                    if let Some(len) = bytes_len {
                        ui.label(
                            RichText::new(format!("({} bytes)", len))
                                .color(self.text_tertiary_color()),
                        );

                        // Expand/collapse button for files > 256 bytes
                        if len > 256 {
                            let button_text = if was_expanded {
                                "▼ Collapse"
                            } else {
                                "▶ Expand"
                            };
                            if ui.button(button_text).clicked() {
                                self.publish_payload_expanded = !was_expanded;
                                should_regenerate = true;
                            }
                        }
                    }
                });

                // Regenerate preview if expand state changed
                if should_regenerate {
                    if let Some(ref bytes) = self.publish_payload_bytes {
                        let total_len = bytes.len();
                        let preview_len = if self.publish_payload_expanded {
                            total_len.min(4 * 1024) // 4KB max when expanded
                        } else {
                            total_len.min(256) // 256 bytes when collapsed
                        };

                        self.publish_payload = if let Ok(text) = std::str::from_utf8(bytes) {
                            // Valid UTF-8 text - use safe truncation
                            if total_len > preview_len {
                                let safe_end = safe_truncate_index(text, preview_len);
                                format!(
                                    "{}... [+{} bytes]",
                                    &text[..safe_end],
                                    total_len - safe_end
                                )
                            } else {
                                text.to_string()
                            }
                        } else {
                            // Binary data - show hex dump (byte slicing is safe)
                            let hex: String = bytes[..preview_len]
                                .iter()
                                .map(|b| format!("{:02x} ", b))
                                .collect();
                            if total_len > preview_len {
                                format!(
                                    "{}... [+{} bytes, {} total]",
                                    hex.trim(),
                                    total_len - preview_len,
                                    total_len
                                )
                            } else {
                                hex
                            }
                        };
                    }
                }
            }

            // Payload text area (editable for text, read-only preview for binary)
            // Use fixed max height with scroll to prevent pushing buttons off screen
            let max_height = if self.publish_payload_expanded {
                200.0
            } else {
                80.0
            };
            let payload_response = egui::ScrollArea::vertical()
                .max_height(max_height)
                .show(ui, |ui| {
                    ui.add(
                        egui::TextEdit::multiline(&mut self.publish_payload)
                            .desired_width(f32::INFINITY)
                            .interactive(self.publish_payload_bytes.is_none()) // Read-only if file imported
                            .font(egui::TextStyle::Monospace),
                    )
                })
                .inner;

            // If user edits text, clear file import
            if payload_response.changed() && self.publish_payload_bytes.is_some() {
                self.publish_payload_bytes = None;
                self.publish_payload_filename = None;
                self.publish_payload_expanded = false;
            }

            ui.horizontal(|ui| {
                ui.label("Encoding:");
                ui.text_edit_singleline(&mut self.publish_encoding);
            });

            // Publish button - only enabled when connected
            let button = egui::Button::new("Publish");
            if ui
                .add_enabled(
                    matches!(self.connection_status, ConnectionStatus::Connected)
                        && !self.publish_key.is_empty(),
                    button,
                )
                .clicked()
            {
                if let Some(sender) = &self.command_sender {
                    // Track if this is from file import before taking the bytes
                    let from_import = self.publish_payload_bytes.is_some();

                    // Use raw bytes if imported (take to avoid clone), otherwise convert text to bytes
                    let payload_bytes = self
                        .publish_payload_bytes
                        .take()
                        .unwrap_or_else(|| self.publish_payload.as_bytes().to_vec());

                    let payload_len = payload_bytes.len();
                    info!(
                        "GUI: About to send Publish command for {} bytes",
                        payload_len
                    );

                    match sender.send(ZenohCommand::Publish {
                        key: self.publish_key.clone(),
                        payload: payload_bytes,
                        encoding: self.publish_encoding.clone(),
                        from_import, // Don't store imported files after publish
                        filename: self.publish_payload_filename.clone(),
                    }) {
                        Ok(_) => info!(
                            "GUI: Publish command sent successfully for {} bytes",
                            payload_len
                        ),
                        Err(e) => error!("GUI: Failed to send Publish command: {:?}", e),
                    }

                    // Clear the UI state since we moved the bytes
                    self.publish_payload_filename = None;
                    self.publish_payload = String::new();
                    self.publish_payload_expanded = false;
                    self.import_memory_bytes = 0; // Memory freed after publish
                }
            }
        });

        ui.add_space(16.0);

        // Queryable section
        ui.group(|ui| {
            ui.label(RichText::new("Queryable").strong());
            ui.label(
                RichText::new("Respond to queries for locally published keys")
                    .size(TEXT_SMALL_SIZE)
                    .color(self.text_secondary_color()),
            );

            ui.horizontal(|ui| {
                ui.label("Key Pattern:");
                ui.text_edit_singleline(&mut self.queryable_pattern);
            });

            ui.horizontal(|ui| {
                let was_enabled = self.queryable_enabled;
                ui.checkbox(&mut self.queryable_enabled, "Enable Queryable");

                // Show status
                if self.queryable_enabled {
                    ui.label(
                        RichText::new("Active")
                            .color(if self.dark_mode {
                                ExplorerColors::DARK_SUCCESS
                            } else {
                                ExplorerColors::SUCCESS
                            })
                            .size(TEXT_SMALL_SIZE),
                    );
                } else {
                    ui.label(
                        RichText::new("Inactive")
                            .color(self.text_tertiary_color())
                            .size(TEXT_SMALL_SIZE),
                    );
                }

                // Send command if state changed
                if was_enabled != self.queryable_enabled {
                    if let Some(sender) = &self.command_sender {
                        if self.queryable_enabled {
                            let _ = sender.send(ZenohCommand::EnableQueryable {
                                key_expr: self.queryable_pattern.clone(),
                            });
                        } else {
                            let _ = sender.send(ZenohCommand::DisableQueryable);
                        }
                    }
                }
            });

            ui.label(
                RichText::new(
                    "When enabled, this app will respond to queries for keys you've published",
                )
                .size(TEXT_SMALL_SIZE)
                .color(self.text_tertiary_color())
                .italics(),
            );
        });
    }
}
