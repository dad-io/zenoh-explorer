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

/// Draw a leader line (dashed when collapsed, solid when expanded) filling the
/// space between the label and a right-aligned tabular count.
fn leader_line_with_count(
    ui: &mut egui::Ui,
    expanded: bool,
    count: usize,
    text_color: egui::Color32,
) {
    if count == 0 {
        return;
    }
    let count_text = count.to_string();
    let font = egui::FontId::proportional(TEXT_SMALL_SIZE);
    let galley = ui
        .painter()
        .layout_no_wrap(count_text.clone(), font, text_color);
    let line_w = (ui.available_width() - galley.size().x - 16.0).max(0.0);
    let (rect, _) = ui.allocate_exact_size(
        egui::vec2(line_w, ui.spacing().interact_size.y),
        egui::Sense::hover(),
    );
    let y = rect.center().y;
    let (alpha, dashed) = if expanded { (100, false) } else { (64, true) };
    let stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(
            text_color.r(),
            text_color.g(),
            text_color.b(),
            alpha,
        ),
    );
    let a = egui::pos2(rect.left() + 4.0, y);
    let b = egui::pos2(rect.right() - 4.0, y);
    if rect.width() > 12.0 {
        if dashed {
            for shape in egui::Shape::dashed_line(&[a, b], stroke, 3.0, 3.0) {
                ui.painter().add(shape);
            }
        } else {
            ui.painter().line_segment([a, b], stroke);
        }
    }
    ui.label(
        egui::RichText::new(count_text)
            .size(TEXT_SMALL_SIZE)
            .color(text_color),
    );
}

/// Inline transfer progress: bar + chunk count, ✓+size when complete,
/// byte progress while in flight. Free fn so it can render inside closures
/// that cannot borrow `self`.
fn render_transfer_progress(
    ui: &mut egui::Ui,
    t: &TransferState,
    dark_mode: bool,
    secondary_color: egui::Color32,
) {
    let frac = t.received.len() as f32 / t.total_chunks.max(1) as f32;
    ui.add(
        egui::ProgressBar::new(frac)
            .desired_width(120.0)
            .text(format!("{}/{}", t.received.len(), t.total_chunks)),
    );
    if t.is_complete() {
        ui.label(
            RichText::new(format!("✓ {}", transfer::format_size(t.total_size)))
                .size(TEXT_SMALL_SIZE)
                .color(if dark_mode {
                    ExplorerColors::DARK_SUCCESS
                } else {
                    ExplorerColors::SUCCESS
                }),
        );
    } else {
        ui.label(
            RichText::new(format!(
                "⬇ {} of {}",
                transfer::format_size(
                    t.received
                        .len()
                        .saturating_mul(crate::transfer::CHUNK_SIZE)
                        .min(t.total_size)
                ),
                transfer::format_size(t.total_size)
            ))
            .size(TEXT_SMALL_SIZE)
            .color(secondary_color),
        );
    }
}

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

            let filter_lower = self.tree_filter.to_lowercase();
            if !filter_lower.is_empty() {
                let stale = self
                    .tree_filter_cache
                    .as_ref()
                    .is_none_or(|(q, v, _)| *q != filter_lower || *v != self.tree_version);
                if stale {
                    let visible = compute_visible_paths(&tree_clone, &filter_lower);
                    self.tree_filter_cache =
                        Some((filter_lower.clone(), self.tree_version, visible));
                }
            } else {
                self.tree_filter_cache = None;
            }

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
                        if self
                            .tree_filter_cache
                            .as_ref()
                            .is_some_and(|(_, _, v)| v.is_empty())
                        {
                            ui.vertical_centered(|ui| {
                                ui.add_space(16.0);
                                ui.label(
                                    egui::RichText::new("No topics match the filter")
                                        .italics()
                                        .color(self.text_secondary_color()),
                                );
                            });
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
                    let result = self
                        .payload_store
                        .read()
                        .map_err(|_| "Payload store lock poisoned".to_string())
                        .and_then(|store| transfer::get_payload_for_export(&store, topic));
                    match result {
                        Ok(payload) => transfer::export_payload_to_file(topic, &payload.bytes),
                        Err(e) => self.ui_alert = Some(format!("Export failed: {}", e)),
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
            let chunk_info = self
                .payload_store
                .read()
                .ok()
                .and_then(|store| transfer::chunk_progress(&store, topic));

            // Display chunk info if this is a chunked payload
            if let Some(p) = chunk_info {
                let (received, total, total_size) = (p.received, p.total_chunks, p.total_size);
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
                        RichText::new("✓ All chunks received — ready to save")
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

        // Apply filter via the precomputed visible-path set (deep match:
        // ancestors of matches and subtrees of matching branches stay visible)
        if let Some((_, _, visible)) = &self.tree_filter_cache {
            if !visible.contains(&full_path) {
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

                let icon = if node.transfer.is_some() {
                    "📥"
                } else {
                    leaf_icon(
                        &full_path,
                        node.last_encoding.as_deref(),
                        node.last_payload.as_deref(),
                    )
                };
                let response = ui.selectable_label(is_selected, format!("{} {}", icon, node.key));

                if response.clicked() {
                    self.selected_topic = Some(full_path.clone());
                    self.detail_view = DetailView::TopicDetails;
                }

                if let Some(t) = &node.transfer {
                    let dark_mode = self.dark_mode;
                    let secondary_color = self.text_secondary_color();
                    render_transfer_progress(ui, t, dark_mode, secondary_color);
                }

                // Show preview of last value (before leader line so count sits at right edge)
                // Skip preview when a transfer is active — chunk bytes aren't previewable
                if node.transfer.is_none() {
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
                }

                // Show message count with leader line (always dashed/collapsed-style for leaves)
                leader_line_with_count(ui, false, node.message_count, self.text_tertiary_color());
            });
        } else {
            // Branch node - collapsible with consistent spacing
            // While filtering, branches render expanded under a separate ID
            // namespace so the user's normal expand/collapse state is bypassed,
            // not overwritten; clearing the filter restores it.
            let filtering = self.tree_filter_cache.is_some();
            let id = if filtering {
                egui::Id::new(("treenode_filtered", &full_path))
            } else {
                egui::Id::new(("treenode", &full_path))
            };
            let state = egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                filtering,
            );
            let expanded = state.is_open();
            let tertiary = self.text_tertiary_color();
            let cumulative_leaves = node.cumulative_leaves;

            // Clone the transfer state before the header closure to avoid borrow conflicts:
            // show_header takes a FnOnce(&mut Ui) which prevents calling &self methods inside.
            let transfer_snapshot: Option<TransferState> = node.transfer.clone();
            let dark_mode = self.dark_mode;
            let secondary_color = self.text_secondary_color();

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

                    let icon = if depth == 0 { "🌐" } else { "📡" };
                    let response =
                        ui.selectable_label(is_selected, format!("{} {}", icon, node.key));

                    if response.clicked() {
                        self.selected_topic = Some(full_path.clone());
                        self.detail_view = DetailView::TopicDetails;
                    }

                    // Show transfer progress inline if this branch-topic is receiving chunks
                    if let Some(ref t) = transfer_snapshot {
                        render_transfer_progress(ui, t, dark_mode, secondary_color);
                    }

                    // Show descendant leaf count with leader line
                    leader_line_with_count(ui, expanded, cumulative_leaves, tertiary);
                });
            });

            header_response.body(|ui| {
                for child in node.children.values() {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }
}

/// Icon bucket for leaf topics — zenoh/embedded/automation themed:
/// 🛠 system (@/ zenoh admin space), 🏷 text/JSON (live KV telemetry),
/// 💾 binary/unknown (firmware/blobs). Prefers the declared encoding, falls
/// back to the payload preview heuristic (binary previews start with "[binary").
pub(crate) fn leaf_icon(
    full_path: &str,
    encoding: Option<&str>,
    last_payload: Option<&str>,
) -> &'static str {
    if full_path.starts_with('@') {
        return "🛠";
    }
    if let Some(enc) = encoding {
        let e = enc.to_ascii_lowercase();
        if e.contains("json") || e.starts_with("text/") {
            return "🏷";
        }
        if e.contains("octet-stream") {
            return "💾";
        }
    }
    match last_payload {
        Some(p) if p.starts_with("[binary") => "💾",
        Some(_) => "🏷",
        None => "💾",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn leaf_icons_bucket_correctly() {
        assert_eq!(leaf_icon("@/session/x", None, None), "🛠");
        assert_eq!(leaf_icon("demo/t", Some("application/json"), None), "🏷");
        assert_eq!(leaf_icon("demo/t", Some("text/plain"), Some("hello")), "🏷");
        assert_eq!(
            leaf_icon("demo/t", Some("application/octet-stream"), None),
            "💾"
        );
        assert_eq!(
            leaf_icon("demo/t", None, Some("[binary 1024 bytes] ff 00")),
            "💾"
        );
        assert_eq!(leaf_icon("demo/t", None, None), "💾");
    }
}
