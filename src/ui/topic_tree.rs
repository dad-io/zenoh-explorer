//! Topic tree panel and detail view rendering.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::colors::ExplorerColors;
use crate::transfer;
use crate::types::*;
use crate::ui::help::HelpUI;
use crate::ui::messages::MessagesUI;
use crate::ui::publish::PublishUI;
use crate::ui::query::QueryUI;

/// Trait for topic tree and detail rendering.
pub trait TopicTreeUI {
    fn show_tree_panel(&mut self, ui: &mut egui::Ui);
    fn show_detail_panel(&mut self, ui: &mut egui::Ui);
    fn show_topic_details(&mut self, ui: &mut egui::Ui);
    fn find_node<'a>(&self, node: &'a ZenohNode, path: &str) -> Option<&'a ZenohNode>;
    fn show_tree_node(
        &mut self,
        ui: &mut egui::Ui,
        node: &ZenohNode,
        parent_path: String,
        depth: usize,
    );
    fn has_matching_descendant(&self, node: &ZenohNode, filter: &str, current_path: &str) -> bool;
}

impl TopicTreeUI for ZenohExplorer {
    /// Renders the left tree panel (main navigation)
    fn show_tree_panel(&mut self, ui: &mut egui::Ui) {
        ui.vertical(|ui| {
            // Search/filter box
            ui.horizontal(|ui| {
                ui.label("🔍");
                ui.text_edit_singleline(&mut self.tree_filter)
                    .on_hover_text("Filter topics");
                if ui.button("✖").clicked() {
                    self.tree_filter.clear();
                }
            });

            // Clear selection button to return to All Messages view
            if self.selected_topic.is_some() && ui.button("⬅ Back to All Messages").clicked() {
                self.selected_topic = None;
            }

            ui.separator();

            // Subscription controls
            ui.collapsing("Subscribe to Topics", |ui| {
                ui.horizontal(|ui| {
                    ui.label("Key:");
                    ui.text_edit_singleline(&mut self.subscribe_key);
                });
                let button = egui::Button::new("Subscribe");
                if ui
                    .add_enabled(
                        matches!(self.connection_status, ConnectionStatus::Connected)
                            && !self.subscribe_key.is_empty(),
                        button,
                    )
                    .clicked()
                {
                    if let Some(sender) = &self.command_sender {
                        let _ = sender.send(ZenohCommand::Subscribe {
                            key_expr: self.subscribe_key.clone(),
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                }

                // Active subscriptions
                if !self.subscriptions.is_empty() {
                    ui.label(RichText::new("Active:").size(SUBSCRIPTION_TEXT_SIZE));
                    for subscription in &self.subscriptions {
                        ui.horizontal(|ui| {
                            ui.label(
                                RichText::new(&subscription.key_expr).size(SUBSCRIPTION_TEXT_SIZE),
                            );
                            if ui.small_button("✖").clicked() {
                                if let Some(sender) = &self.command_sender {
                                    let _ = sender.send(ZenohCommand::Unsubscribe {
                                        subscription_id: subscription.id.clone(),
                                    });
                                }
                            }
                        });
                    }
                }
            });

            ui.separator();

            // Topic tree
            ui.label(RichText::new("Topics").strong());

            // Clone tree for rendering (necessary to avoid lifetime issues with RwLock)
            let tree_clone = if let Ok(tree) = self.browse_tree.read() {
                tree.clone()
            } else {
                ZenohNode::new("root".to_string())
            };

            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    if tree_clone.children.is_empty() {
                        ui.vertical_centered(|ui| {
                            ui.add_space(32.0);
                            ui.label(
                                RichText::new("No topics yet")
                                    .size(HEADING_MEDIUM_SIZE)
                                    .color(self.text_tertiary_color()),
                            );
                            ui.add_space(8.0);
                            ui.label(
                                RichText::new(
                                    "Subscribe to key expressions to see network activity",
                                )
                                .italics()
                                .color(self.text_secondary_color()),
                            );
                            ui.add_space(4.0);
                            ui.label(
                                RichText::new("💡 Try demo/** or sensor/* in the Subscribe tab")
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_tertiary_color()),
                            );
                            ui.add_space(32.0);
                        });
                    } else {
                        for child in tree_clone.children.values() {
                            self.show_tree_node(ui, child, String::new(), 0);
                        }
                    }
                });
        });
    }

    /// Renders the right detail panel based on current view mode
    fn show_detail_panel(&mut self, ui: &mut egui::Ui) {
        match self.detail_view {
            DetailView::TopicDetails => self.show_topic_details(ui),
            DetailView::Publish => self.show_publish_tab(ui),
            DetailView::Query => self.show_query_tab(ui),
            DetailView::Help => self.show_help_tab(ui),
        }
    }

    /// Shows details for the selected topic
    fn show_topic_details(&mut self, ui: &mut egui::Ui) {
        if let Some(ref topic) = self.selected_topic.clone() {
            ui.heading(topic);

            // Action buttons: Export and Pause/Resume
            ui.horizontal(|ui| {
                // Export button with subtle styling
                // NOTE: Exports FULL payload from payload_store (not truncated tree/UI version)
                // For chunked payloads, reassembles chunks from topic/__chunk/...
                if ui
                    .button("Export Payload")
                    .on_hover_text("Save full payload to file (original size)")
                    .clicked()
                {
                    if let Some(payload) =
                        transfer::get_payload_for_export(&self.payload_store, topic)
                    {
                        transfer::export_payload_to_file(topic, &payload);
                    }
                }

                // Pause/Resume button with animated indicator
                let is_paused = self.paused_keys.contains(topic);
                let button_text = if is_paused { "▶ Resume" } else { "⏸ Pause" };
                let button_color = if is_paused {
                    ExplorerColors::WARNING
                } else {
                    self.text_secondary_color()
                };

                if ui
                    .button(RichText::new(button_text).color(button_color))
                    .on_hover_text(if is_paused {
                        "Resume updates for this topic"
                    } else {
                        "Pause updates for this topic (messages still received, just not displayed)"
                    })
                    .clicked()
                {
                    if is_paused {
                        self.paused_keys.remove(topic);
                    } else {
                        self.paused_keys.insert(topic.clone());
                    }
                }

                // Show paused indicator with subtle animation
                if is_paused {
                    ui.label(
                        RichText::new("⏸ Paused")
                            .color(ExplorerColors::WARNING)
                            .size(TEXT_SMALL_SIZE),
                    );
                }
            });

            ui.separator();

            // Get the node details (extract data first to avoid borrow conflicts)
            let (message_count, payload_opt, encoding_opt) =
                if let Ok(tree) = self.browse_tree.read() {
                    if let Some(node) = self.find_node(&tree, topic) {
                        (
                            node.message_count,
                            node.last_payload.clone(),
                            node.last_encoding.clone(),
                        )
                    } else {
                        (0, None, None)
                    }
                } else {
                    (0, None, None)
                };

            // Show node metadata
            ui.horizontal(|ui| {
                ui.label(RichText::new("Messages:").strong());
                ui.label(message_count.to_string());
            });

            // Check for chunked payload and show info
            let chunk_info = transfer::get_chunk_info(&self.payload_store, topic);

            // Display chunk info if this is a chunked payload
            if let Some((received, total, total_size)) = chunk_info {
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new("📦 Chunked Payload:")
                            .strong()
                            .color(ExplorerColors::SUCCESS),
                    );
                    let size_str = transfer::format_size(total_size);
                    ui.label(format!(
                        "{}/{} chunks received, {} total",
                        received, total, size_str
                    ));
                });
                if received == total {
                    ui.label(
                        RichText::new("✓ All chunks received - click Export to reassemble")
                            .color(ExplorerColors::SUCCESS),
                    );
                } else {
                    ui.label(
                        RichText::new(format!(
                            "⏳ Waiting for {} more chunks...",
                            total - received
                        ))
                        .color(ExplorerColors::WARNING),
                    );
                }
                ui.separator();
            }

            if let Some(payload) = payload_opt {
                ui.separator();
                ui.label(RichText::new("Current Value:").strong());

                // Collapsed: 1024 chars, Expanded: full preview (up to 10KB from tree)
                const COLLAPSED_SIZE: usize = 1024;
                let is_large = payload.len() > COLLAPSED_SIZE;
                let is_expanded = self.expanded_payloads.contains(topic);

                // Show collapse/expand button for payloads > 1KB
                if is_large {
                    let hidden_bytes = payload.len().saturating_sub(COLLAPSED_SIZE);
                    let button_text = if is_expanded {
                        "▼ Collapse".to_string()
                    } else {
                        format!("▶ Expand (+{} bytes)", hidden_bytes)
                    };
                    if ui.button(&button_text).clicked() {
                        if is_expanded {
                            self.expanded_payloads.remove(topic);
                        } else {
                            self.expanded_payloads.insert(topic.clone());
                        }
                    }
                }

                // Determine what to display
                let display_payload = if is_large && !is_expanded {
                    let end = safe_truncate_index(&payload, COLLAPSED_SIZE);
                    format!("{}...", &payload[..end])
                } else {
                    payload.clone()
                };

                // Try to parse and format as JSON (using cache) - skips if > 50KB
                if let Some(pretty) = self.get_cached_json(&display_payload) {
                    egui::ScrollArea::vertical()
                        .id_salt(format!("json_payload_{}", topic))
                        .max_height(400.0)
                        .show(ui, |ui| {
                            ui.label(RichText::new(&pretty).code().color(self.text_color()));
                        });
                } else {
                    egui::ScrollArea::vertical()
                        .id_salt(format!("text_payload_{}", topic))
                        .max_height(400.0)
                        .show(ui, |ui| {
                            ui.label(
                                RichText::new(&display_payload)
                                    .code()
                                    .color(self.text_color()),
                            );
                        });
                }

                if let Some(encoding) = encoding_opt {
                    ui.separator();
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Encoding:").strong());
                        ui.label(encoding);
                    });
                }
            }

            ui.separator();

            // Show message history for this topic
            ui.label(RichText::new("Message History:").strong());
            egui::ScrollArea::vertical().show(ui, |ui| {
                let topic_messages: Vec<_> = self
                    .messages
                    .iter()
                    .filter(|m| m.key == *topic)
                    .rev()
                    .take(50)
                    .collect();

                if topic_messages.is_empty() {
                    ui.vertical_centered(|ui| {
                        ui.add_space(16.0);
                        ui.label(
                            RichText::new("No messages yet")
                                .size(HEADING_MEDIUM_SIZE)
                                .color(self.text_tertiary_color()),
                        );
                        ui.add_space(4.0);
                        ui.label(
                            RichText::new("Waiting for messages on this topic...")
                                .italics()
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                        );
                        ui.add_space(16.0);
                    });
                } else {
                    for message in topic_messages {
                        ui.group(|ui| {
                            ui.horizontal(|ui| {
                                ui.label(
                                    RichText::new(
                                        message.timestamp.format("%H:%M:%S%.3f").to_string(),
                                    )
                                    .color(self.text_secondary_color())
                                    .size(TEXT_SMALL_SIZE),
                                );
                                ui.label(
                                    RichText::new(message.message_type.label())
                                        .background_color(message.message_type.color())
                                        .color(egui::Color32::WHITE)
                                        .size(TEXT_SMALL_SIZE),
                                );
                            });

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
                        });
                    }
                }
            });
        } else {
            // No topic selected - show all messages
            ui.heading("All Messages");
            ui.separator();

            self.show_messages_tab(ui);
        }
    }

    /// Helper to find a node by full path
    fn find_node<'a>(&self, node: &'a ZenohNode, path: &str) -> Option<&'a ZenohNode> {
        let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
        let mut current = node;

        for part in parts {
            if let Some(child) = current.children.get(part) {
                current = child;
            } else {
                return None;
            }
        }

        Some(current)
    }

    /// Renders a tree node with improved MQTT Explorer-style visualization
    fn show_tree_node(
        &mut self,
        ui: &mut egui::Ui,
        node: &ZenohNode,
        parent_path: String,
        depth: usize,
    ) {
        // Build the full path for this node
        let full_path = if parent_path.is_empty() {
            node.key.clone()
        } else {
            format!("{}/{}", parent_path, node.key)
        };

        // Apply filter
        if !self.tree_filter.is_empty() && !full_path.contains(&self.tree_filter) {
            // Check if any children match
            let has_matching_child =
                self.has_matching_descendant(node, &self.tree_filter, &full_path);
            if !has_matching_child {
                return;
            }
        }

        let indent = 12.0 * depth as f32;
        let is_selected = self.selected_topic.as_ref() == Some(&full_path);

        if node.children.is_empty() {
            // Leaf node - show as selectable in horizontal layout
            ui.horizontal(|ui| {
                ui.add_space(indent);

                // Local indicator - subtle filled circle with fade-in animation
                if node.is_local {
                    let fade =
                        self.animate_fade_in(ui.ctx(), &format!("local_leaf_{}", full_path), 1.0);
                    let base_color = if self.dark_mode {
                        ExplorerColors::DARK_SUCCESS
                    } else {
                        ExplorerColors::SUCCESS
                    };
                    let animated_color = egui::Color32::from_rgba_unmultiplied(
                        base_color.r(),
                        base_color.g(),
                        base_color.b(),
                        (255.0 * fade) as u8,
                    );
                    ui.label(RichText::new("●").size(8.0).color(animated_color))
                        .on_hover_text("Published from this app");
                }

                let response = ui.selectable_label(is_selected, format!("📄 {}", node.key));

                if response.clicked() {
                    self.selected_topic = Some(full_path.clone());
                    self.detail_view = DetailView::TopicDetails;
                }

                // Show message count badge
                if node.message_count > 0 {
                    ui.label(
                        RichText::new(format!("({})", node.message_count))
                            .size(TEXT_SMALL_SIZE)
                            .color(ExplorerColors::PRIMARY),
                    );
                }

                // Show preview of last value
                if let Some(ref payload) = node.last_payload {
                    let preview = if payload.len() > 30 {
                        let end = safe_truncate_index(payload, 30);
                        format!("{}...", &payload[..end])
                    } else {
                        payload.clone()
                    };
                    ui.label(
                        RichText::new(preview)
                            .size(TOPIC_PREVIEW_TEXT_SIZE)
                            .color(self.text_secondary_color()),
                    );
                }
            });
        } else {
            // Branch node - collapsible with consistent spacing
            let id = egui::Id::new(format!("treenode_{}", full_path));
            let state = egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                false,
            );

            let header_response = state.show_header(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.add_space(indent);

                    // Local indicator - subtle filled circle with fade-in animation
                    if node.is_local {
                        let fade = self.animate_fade_in(
                            ui.ctx(),
                            &format!("local_branch_{}", full_path),
                            1.0,
                        );
                        let base_color = if self.dark_mode {
                            ExplorerColors::DARK_SUCCESS
                        } else {
                            ExplorerColors::SUCCESS
                        };
                        let animated_color = egui::Color32::from_rgba_unmultiplied(
                            base_color.r(),
                            base_color.g(),
                            base_color.b(),
                            (255.0 * fade) as u8,
                        );
                        ui.label(RichText::new("●").size(8.0).color(animated_color))
                            .on_hover_text("Published from this app");
                    }

                    let response = ui.selectable_label(is_selected, format!("📁 {}", node.key));

                    if response.clicked() {
                        self.selected_topic = Some(full_path.clone());
                        self.detail_view = DetailView::TopicDetails;
                    }

                    // Show child count
                    ui.label(
                        RichText::new(format!("({})", node.children.len()))
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_tertiary_color()),
                    );
                });
            });

            header_response.body(|ui| {
                for child in node.children.values() {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }

    /// Check if node or any descendant matches filter
    fn has_matching_descendant(&self, node: &ZenohNode, filter: &str, current_path: &str) -> bool {
        if current_path.contains(filter) {
            return true;
        }

        for (key, child) in &node.children {
            let child_path = format!("{}/{}", current_path, key);
            if self.has_matching_descendant(child, filter, &child_path) {
                return true;
            }
        }

        false
    }
}
