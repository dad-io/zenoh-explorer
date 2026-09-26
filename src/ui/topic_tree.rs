//! Topic tree panel and detail view rendering.

use egui::RichText;
use std::sync::Arc;
use std::time::Instant;

use crate::app::{UiAlert, ZenohExplorer};
use crate::colors::ExplorerColors;
use crate::transfer;
use crate::types::*;
use crate::ui::help::HelpUI;
use crate::ui::messages::MessagesUI;
use crate::ui::publish::PublishUI;
use crate::ui::query::QueryUI;

/// Right-align a tabular count, with a leader line filling the gap for
/// expandable rows only (`Some(expanded)`: dashed when collapsed, solid when
/// expanded). Non-expandable rows (`None`) keep the spacing but draw no line.
fn leader_line_with_count(
    ui: &mut egui::Ui,
    line: Option<bool>,
    count: usize,
    text_color: egui::Color32,
) -> Option<egui::Response> {
    if count == 0 {
        return None;
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
    if let Some(expanded) = line {
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
    }
    Some(
        ui.label(
            egui::RichText::new(count_text)
                .size(TEXT_SMALL_SIZE)
                .color(text_color),
        ),
    )
}

/// Custom expander: two intersecting pipes. Collapsed shows ＋ (vertical pipe
/// crossing the horizontal); on expansion the vertical pipe rotates 90° to lie
/// over the horizontal one and "hides", leaving −. `openness` is egui's
/// animated 0..=1 open fraction, so the rotation animates on click.
fn plus_minus_icon(ui: &mut egui::Ui, openness: f32, response: &egui::Response) {
    let rect = response.rect;
    let center = rect.center();
    let half = rect.width().min(rect.height()) * 0.4;
    let mut stroke = ui.style().interact(response).fg_stroke;
    stroke.width = 1.5;
    let painter = ui.painter();
    painter.line_segment(
        [
            egui::pos2(center.x - half, center.y),
            egui::pos2(center.x + half, center.y),
        ],
        stroke,
    );
    // Vertical pipe rotates onto the horizontal as openness goes 0 → 1.
    let angle = openness * std::f32::consts::FRAC_PI_2;
    let (sin, cos) = angle.sin_cos();
    let (dx, dy) = (half * sin, half * cos);
    painter.line_segment(
        [
            egui::pos2(center.x - dx, center.y - dy),
            egui::pos2(center.x + dx, center.y + dy),
        ],
        stroke,
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
    fn save_topic_to_file(&mut self, topic: &str);
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
                let key_err = crate::validation::key_expr_error(&self.subscribe_key);
                if let Some(err) = &key_err {
                    ui.colored_label(ExplorerColors::ERROR, err);
                }
                let button = egui::Button::new("Subscribe");
                if ui.add_enabled(self.subscribe_enabled(), button).clicked() {
                    if let Some(sender) = &self.command_sender {
                        let _ = sender.send(ZenohCommand::Subscribe {
                            key_expr: self.subscribe_key.clone(),
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                    self.pending_subscribes
                        .insert(self.subscribe_key.trim().to_string());
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

            // Render from a read guard on a local Arc clone: no per-frame deep copy,
            // and `self` stays free for &mut calls. The UI thread is the only
            // writer (events run on it), so nested read() calls cannot deadlock.
            let tree_arc = Arc::clone(&self.browse_tree);
            let guard = tree_arc.read();
            let fallback;
            let tree: &ZenohNode = match &guard {
                Ok(g) => g,
                Err(_) => {
                    fallback = ZenohNode::new("root".to_string());
                    &fallback
                }
            };

            let filter_lower = self.tree_filter.to_lowercase();
            if !filter_lower.is_empty() {
                let now = Instant::now();
                if filter_cache_is_stale(
                    self.tree_filter_cache
                        .as_ref()
                        .map(|(q, v, at, _)| (q.as_str(), *v, *at)),
                    &filter_lower,
                    self.tree_version,
                    now,
                ) {
                    let visible = compute_visible_paths(tree, &filter_lower);
                    self.tree_filter_cache =
                        Some((filter_lower.clone(), self.tree_version, now, visible));
                }
                // K4: a cache kept only by the throttle repaints when the
                // interval ends, so the filtered tree is not stale until the
                // idle tick after traffic stops.
                if let Some(d) = filter_repaint_after(
                    self.tree_filter_cache
                        .as_ref()
                        .map(|(q, v, at, _)| (q.as_str(), *v, *at)),
                    &filter_lower,
                    self.tree_version,
                    now,
                ) {
                    ui.ctx().request_repaint_after(d);
                }
            } else {
                self.tree_filter_cache = None;
            }

            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    if tree.children.is_empty() {
                        ui.vertical_centered(|ui| {
                            ui.add_space(32.0);
                            ui.label(
                                RichText::new("No topics yet")
                                    .size(HEADING_MEDIUM_SIZE)
                                    .color(self.text_tertiary_color()),
                            );
                            ui.add_space(8.0);
                            ui.label(
                                RichText::new("Topics appear here as this app receives data")
                                    .italics()
                                    .color(self.text_secondary_color()),
                            );
                            ui.add_space(4.0);
                            ui.label(
                                RichText::new(
                                    "💡 Try demo/** or sensor/* in Subscribe to Topics above",
                                )
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_tertiary_color()),
                            );
                            ui.add_space(32.0);
                        });
                    } else {
                        for child in tree.children.values() {
                            self.show_tree_node(ui, child, String::new(), 0);
                        }
                        if self
                            .tree_filter_cache
                            .as_ref()
                            .is_some_and(|(_, _, _, v)| v.is_empty())
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

            // Get the node details (extract data first to avoid borrow conflicts)
            let (message_count, payload_opt, encoding_opt, kind, source_time, summary, child_keys) =
                match self
                    .browse_tree
                    .read()
                    .ok()
                    .as_deref()
                    .and_then(|tree| self.find_node(tree, topic))
                {
                    Some(node) => (
                        node.message_count,
                        node.last_payload.clone(),
                        node.last_encoding.clone(),
                        node.last_kind,
                        node.last_source_time,
                        (!node.children.is_empty()).then(|| node.subtree_summary()),
                        node.children.keys().cloned().collect::<Vec<_>>(),
                    ),
                    None => (0, None, None, SampleKindView::Put, None, None, Vec::new()),
                };
            let now = Instant::now();

            // A branch with no data of its own: summary and child keys only.
            if let (Some(summary), 0) = (summary, message_count) {
                ui.separator();
                ui.label(branch_summary_text(&summary, now));
                ui.separator();
                for child in &child_keys {
                    if ui.selectable_label(false, child).clicked() {
                        self.selected_topic = Some(format!("{}/{}", topic, child));
                    }
                }
                return;
            }

            // Action buttons: Save and Pause/Resume
            ui.horizontal(|ui| {
                // Save availability: direct payload or a complete chunk set
                let (saveable, size, reason) = {
                    let store = self.payload_store.read().ok();
                    let direct = store
                        .as_ref()
                        .and_then(|s| s.get(topic.as_str()))
                        .map(|e| e.bytes.len());
                    match direct {
                        Some(len) => (true, Some(len), String::new()),
                        None => match store
                            .as_ref()
                            .and_then(|s| transfer::chunk_progress(s, topic))
                        {
                            Some(p) if p.received == p.total_chunks => {
                                (true, Some(p.total_size), String::new())
                            }
                            Some(p) => (
                                false,
                                None,
                                format!("Waiting for {} more chunks", p.total_chunks - p.received),
                            ),
                            None => (false, None, "No payload stored yet".to_string()),
                        },
                    }
                };
                let label = match size {
                    Some(s) => format!("💾 Save File ({})", transfer::format_size(s)),
                    None => "💾 Save File".to_string(),
                };
                let button = egui::Button::new(RichText::new(&label).color(egui::Color32::WHITE))
                    .fill(if self.dark_mode {
                        ExplorerColors::DARK_PRIMARY
                    } else {
                        ExplorerColors::PRIMARY
                    });
                let response = ui.add_enabled(saveable, button);
                let response = if saveable {
                    response.on_hover_text("Save full payload to file (original size)")
                } else {
                    response.on_disabled_hover_text(reason)
                };
                if response.clicked() {
                    let topic_owned = topic.clone();
                    self.save_topic_to_file(&topic_owned);
                }

                // Pause/Resume button with animated indicator
                let is_paused = self.paused_keys.contains(topic);
                let button_text = if is_paused {
                    "▶ Resume list"
                } else {
                    "⏸ Pause list"
                };
                let button_color = if is_paused {
                    ExplorerColors::WARNING
                } else {
                    self.text_secondary_color()
                };

                if ui
                    .button(RichText::new(button_text).color(button_color))
                    .on_hover_text(
                        "Stop adding this topic's messages to the lists; its value and count keep updating",
                    )
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
                        RichText::new("Paused (lists only)")
                            .color(ExplorerColors::WARNING)
                            .size(TEXT_SMALL_SIZE),
                    );
                }
            });

            ui.separator();

            // Show node metadata. The tree lives for the whole app run and
            // survives Disconnect, so the count spans connects.
            ui.label(
                RichText::new(format!("Received: {message_count} (since app start)")).strong(),
            );
            if let Some(summary) = &summary {
                ui.label(
                    RichText::new(branch_summary_text(summary, now))
                        .color(self.text_secondary_color()),
                );
            }

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
                    ui.horizontal(|ui| {
                        ui.label(
                            RichText::new("✓ All chunks received — ready to save")
                                .color(ExplorerColors::SUCCESS),
                        );
                        if ui.small_button("💾 Save").clicked() {
                            let topic_owned = topic.clone();
                            self.save_topic_to_file(&topic_owned);
                        }
                    });
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
                if kind == SampleKindView::Delete {
                    ui.separator();
                    ui.label(RichText::new("Last sample: DELETE").strong());
                }
                if let Some(ts) = source_time {
                    ui.separator();
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Source time:").strong());
                        ui.label(format_local_time(&ts, &chrono::Utc::now()));
                    });
                }
            }

            ui.separator();

            // Show message history for this topic
            ui.label(RichText::new("Message History:").strong());
            let paused = self.paused_keys.contains(topic);
            egui::ScrollArea::vertical()
                .id_salt(("history", topic.as_str()))
                .show(ui, |ui| {
                    const HISTORY_SCAN_LIMIT: usize = 20_000;
                    let topic_messages: Vec<_> = self
                        .messages
                        .iter()
                        .rev()
                        .take(HISTORY_SCAN_LIMIT)
                        // Query replies belong to Query Results, not History (G3-12).
                        .filter(|m| m.key == *topic && m.message_type != MessageType::QueryReply)
                        .take(50)
                        .collect();
                    let scan_note = history_scan_note(
                        self.messages.len(),
                        HISTORY_SCAN_LIMIT,
                        topic_messages.len(),
                    );

                    if topic_messages.is_empty() {
                        let (heading, detail) = match &scan_note {
                            Some(note) => ("No messages in the scanned rows", Some(note.as_str())),
                            None => (history_empty_reason(message_count, paused), None),
                        };
                        ui.vertical_centered(|ui| {
                            ui.add_space(16.0);
                            ui.label(
                                RichText::new(heading)
                                    .size(HEADING_MEDIUM_SIZE)
                                    .color(self.text_tertiary_color()),
                            );
                            if let Some(detail) = detail {
                                ui.add_space(4.0);
                                ui.label(
                                    RichText::new(detail)
                                        .italics()
                                        .size(TEXT_SMALL_SIZE)
                                        .color(self.text_secondary_color()),
                                );
                            }
                            ui.add_space(16.0);
                        });
                    } else {
                        let shown = topic_messages.len();
                        if message_count > shown {
                            ui.label(
                                RichText::new(format!(
                                    "Showing the newest {shown} of {message_count}"
                                ))
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                            );
                        }
                        if let Some(note) = scan_note {
                            ui.label(
                                RichText::new(note)
                                    .italics()
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        }
                        let wall_now = chrono::Utc::now();
                        for message in topic_messages {
                            ui.group(|ui| {
                                ui.horizontal(|ui| {
                                    ui.label(
                                        RichText::new(format_local_time(
                                            &message.timestamp,
                                            &wall_now,
                                        ))
                                        .color(self.text_secondary_color())
                                        .size(TEXT_SMALL_SIZE),
                                    )
                                    .on_hover_text("Received time, local");
                                    if let Some(src) = &message.source_timestamp {
                                        ui.label(
                                            RichText::new(format!(
                                                "· source {}",
                                                format_local_time(src, &wall_now)
                                            ))
                                            .color(self.text_secondary_color())
                                            .size(TEXT_SMALL_SIZE),
                                        );
                                    }
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
        if let Some((_, _, _, visible)) = &self.tree_filter_cache {
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
                if self.paused_keys.contains(&full_path) {
                    ui.label(RichText::new("⏸ paused").size(TEXT_SMALL_SIZE));
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

                // Quick save on rows with an exportable payload
                let exportable = node.transfer.as_ref().is_some_and(|t| t.is_complete())
                    || self
                        .payload_store
                        .read()
                        .is_ok_and(|s| s.contains_key(&full_path));
                if exportable && ui.small_button("💾").on_hover_text("Save file").clicked() {
                    self.save_topic_to_file(&full_path);
                }

                // Show message count with leader line (always dashed/collapsed-style for leaves)
                if let Some(r) =
                    leader_line_with_count(ui, None, node.message_count, self.text_tertiary_color())
                {
                    r.on_hover_text(count_hover(false, node.message_count));
                }
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
            let mut state = egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                filtering,
            );
            let expanded = state.is_open();
            let tertiary = self.text_tertiary_color();
            let cumulative_leaves = node.cumulative_leaves;

            // Clone the transfer state before the header row closure to avoid
            // borrow conflicts with &self method calls inside it.
            let transfer_snapshot: Option<TransferState> = node.transfer.clone();
            let dark_mode = self.dark_mode;
            let secondary_color = self.text_secondary_color();

            ui.horizontal(|ui| {
                ui.add_space(indent);

                // Plus/minus expander: a larger, clearer toggle than the
                // default chevron (vertical pipe rotates away on expand).
                state.show_toggle_button(ui, plus_minus_icon);

                {
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
                    // A branch that also carries data shows its own count.
                    if node.message_count > 0 {
                        ui.label(
                            RichText::new(format!("({})", node.message_count))
                                .size(TEXT_SMALL_SIZE)
                                .color(secondary_color),
                        );
                    }
                    if self.paused_keys.contains(&full_path) {
                        ui.label(RichText::new("⏸ paused").size(TEXT_SMALL_SIZE));
                    }

                    // Show transfer progress inline if this branch-topic is receiving chunks
                    if let Some(ref t) = transfer_snapshot {
                        render_transfer_progress(ui, t, dark_mode, secondary_color);
                    }

                    // Show descendant leaf count with leader line
                    if let Some(r) =
                        leader_line_with_count(ui, Some(expanded), cumulative_leaves, tertiary)
                    {
                        r.on_hover_text(count_hover(true, cumulative_leaves));
                    }
                }
            });

            state.show_body_unindented(ui, |ui| {
                for child in node.children.values() {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }

    /// Run the full save flow for a topic: fetch/reassemble, native dialog,
    /// write — surfacing any failure in the global alert banner.
    fn save_topic_to_file(&mut self, topic: &str) {
        let result = self
            .payload_store
            .read()
            .map_err(|_| "Payload store lock poisoned".to_string())
            .and_then(|store| transfer::get_payload_for_export(&store, topic));
        match result {
            Ok(payload) => {
                let suggested =
                    transfer::suggested_export_filename(topic, payload.filename.as_deref());
                match transfer::export_payload_to_file(&suggested, &payload.bytes) {
                    Ok(Some(path)) => {
                        self.ui_alert =
                            Some(UiAlert::Success(format!("Saved to {}", path.display())));
                    }
                    Ok(None) => {} // user cancelled
                    Err(e) => self.ui_alert = Some(UiAlert::Error(format!("Save failed: {}", e))),
                }
            }
            Err(e) => self.ui_alert = Some(UiAlert::Error(format!("Save failed: {}", e))),
        }
    }
}

/// Icon bucket for leaf topics — zenoh/embedded/automation themed:
/// 🛠 system (@/ zenoh admin space), 🏷 text/JSON (live KV telemetry),
/// 💾 binary/unknown (firmware/blobs). Prefers the declared encoding, falls
/// back to the payload preview heuristic (binary previews start with "[binary").
impl ZenohExplorer {
    /// Subscribe is enabled when connected, the key is valid, and no row or
    /// pending Subscribe already has this key. The key is validated as typed,
    /// like the error shown above the button: a surrounding space is an error.
    pub(crate) fn subscribe_enabled(&self) -> bool {
        let key = self.subscribe_key.trim();
        matches!(self.connection_status, ConnectionStatus::Connected)
            && crate::validation::key_expr_error(&self.subscribe_key).is_none()
            && !self.subscriptions.iter().any(|s| s.key_expr == key)
            && !self.pending_subscribes.contains(key)
    }
}

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

/// Message History scans only the newest `scan_limit` list rows and shows at
/// most 50 cards. When the list is longer than the scan and fewer than 50 rows
/// were found, older rows for the key may still be in the list, so History
/// says what it searched instead of reading as "no messages".
fn history_scan_note(list_len: usize, scan_limit: usize, shown: usize) -> Option<String> {
    (list_len > scan_limit && shown < 50)
        .then(|| format!("Searched only the newest {scan_limit} of {list_len} list rows"))
}

/// `"1 {one}"` or `"{n} {many}"`.
fn counted(n: usize, one: &str, many: &str) -> String {
    if n == 1 {
        format!("1 {one}")
    } else {
        format!("{n} {many}")
    }
}

/// Hover text for a tree row's count: leaves below a branch, messages on a leaf.
fn count_hover(is_branch: bool, n: usize) -> String {
    if is_branch {
        counted(n, "leaf topic below", "leaf topics below")
    } else {
        counted(n, "message received", "messages received")
    }
}

/// Why Message History has no card for a topic the list scan fully covered.
fn history_empty_reason(message_count: usize, paused: bool) -> &'static str {
    if paused {
        "Paused: new messages for this topic are not listed"
    } else if message_count == 0 {
        "No messages on this exact key yet"
    } else {
        "Not in the list: cleared, trimmed, rate-limited or received while paused (the count above keeps them)"
    }
}

/// Coarse age: seconds, minutes, hours or days.
fn format_age(d: std::time::Duration) -> String {
    let s = d.as_secs();
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{}m", s / 60),
        3600..=86_399 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86_400),
    }
}

/// One line describing what a branch holds below it.
fn branch_summary_text(s: &SubtreeSummary, now: Instant) -> String {
    let mut text = format!(
        "{} · {}",
        counted(s.topics, "topic below", "topics below"),
        counted(s.messages, "message received", "messages received"),
    );
    if let Some(at) = s.last_seen {
        text.push_str(&format!(
            " · last message {} ago",
            format_age(now.saturating_duration_since(at))
        ));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn double_subscribe_is_ignored_while_pending() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.subscribe_key = "demo/**".to_string();
        assert!(app.subscribe_enabled());
        app.pending_subscribes.insert("demo/**".to_string());
        assert!(
            !app.subscribe_enabled(),
            "a second click before SubscriptionCreated does nothing"
        );
    }

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

    /// Texts painted by one headless frame of `show_topic_details`.
    fn details_texts(app: &mut ZenohExplorer) -> Vec<String> {
        fn collect(shape: &egui::Shape, out: &mut Vec<String>) {
            match shape {
                egui::Shape::Text(t) => out.push(t.galley.text().to_string()),
                egui::Shape::Vec(v) => v.iter().for_each(|s| collect(s, out)),
                _ => {}
            }
        }
        let ctx = egui::Context::default();
        let output = ctx.run(egui::RawInput::default(), |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| app.show_topic_details(ui));
        });
        let mut texts = Vec::new();
        for clipped in &output.shapes {
            collect(&clipped.shape, &mut texts);
        }
        texts
    }

    #[test]
    fn topic_details_show_delete_and_source_time() {
        use chrono::TimeZone;
        let (mut app, _tx) = ZenohExplorer::test_app();
        let ts = chrono::Utc.with_ymd_and_hms(2026, 9, 25, 12, 0, 0).unwrap()
            + chrono::Duration::milliseconds(123);
        app.browse_tree
            .write()
            .unwrap()
            .insert_path("demo/x")
            .update_data(
                String::new(),
                "text/plain".to_string(),
                false,
                SampleKindView::Delete,
                Some(ts),
            );
        app.browse_tree
            .write()
            .unwrap()
            .insert_path("demo/y")
            .update_data(
                "hello".to_string(),
                "text/plain".to_string(),
                false,
                SampleKindView::Put,
                None,
            );

        app.selected_topic = Some("demo/x".to_string());
        let texts = details_texts(&mut app);
        assert!(
            texts.iter().any(|t| t == "Last sample: DELETE"),
            "{texts:?}"
        );
        assert!(texts.iter().any(|t| t == "Source time:"), "{texts:?}");
        assert!(
            texts
                .iter()
                .any(|t| *t == format_local_time(&ts, &chrono::Utc::now())),
            "{texts:?}"
        );

        app.selected_topic = Some("demo/y".to_string());
        let texts = details_texts(&mut app);
        assert!(texts.iter().any(|t| t == "hello"), "{texts:?}");
        assert!(
            !texts.iter().any(|t| t == "Last sample: DELETE"),
            "{texts:?}"
        );
        assert!(!texts.iter().any(|t| t == "Source time:"), "{texts:?}");
    }

    #[test]
    fn history_names_the_scan_window_instead_of_claiming_empty() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.browse_tree
            .write()
            .unwrap()
            .insert_path("slow/a")
            .update_data(
                "tree-val".to_string(),
                "text/plain".to_string(),
                false,
                SampleKindView::Put,
                None,
            );
        let mk = |k: &str, p: &str| {
            ZenohMessage::new_with_bytes(
                k.to_string(),
                p.to_string(),
                p.as_bytes().to_vec(),
                "text/plain".to_string(),
                chrono::Utc::now(),
                MessageType::Subscribe,
                false,
                MessageSource::MonitorSession,
            )
        };
        app.selected_topic = Some("slow/a".to_string());

        // A short list with no row for the key keeps the plain empty state.
        app.messages.push_back(mk("fast/b", "x"));
        let texts = details_texts(&mut app);
        assert!(
            texts.iter().any(|t| *t == history_empty_reason(1, false)),
            "{texts:?}"
        );
        assert!(
            !texts.iter().any(|t| t.starts_with("Searched only")),
            "{texts:?}"
        );

        // The key's only row is older than the scan window, but still listed.
        app.messages.clear();
        app.messages.push_back(mk("slow/a", "hist-val"));
        for _ in 0..20_000 {
            app.messages.push_back(mk("fast/b", "x"));
        }
        assert!(app.messages.len() <= app.max_messages);
        let texts = details_texts(&mut app);
        assert!(
            !texts
                .iter()
                .any(|t| *t == history_empty_reason(1, false)
                    || t.starts_with("Waiting for messages")),
            "{texts:?}"
        );
        assert!(
            texts
                .iter()
                .any(|t| t == "Searched only the newest 20000 of 20001 list rows"),
            "{texts:?}"
        );

        // One row inside the window: its card shows, and the note says why
        // there may be fewer than 50.
        app.messages.push_back(mk("slow/a", "new-val"));
        let texts = details_texts(&mut app);
        assert!(texts.iter().any(|t| t == "new-val"), "{texts:?}");
        assert!(
            texts
                .iter()
                .any(|t| t == "Searched only the newest 20000 of 20002 list rows"),
            "{texts:?}"
        );
    }

    #[test]
    fn history_empty_reason_rules() {
        assert_eq!(
            history_empty_reason(0, false),
            "No messages on this exact key yet"
        );
        assert_eq!(history_empty_reason(360, false), "Not in the list: cleared, trimmed, rate-limited or received while paused (the count above keeps them)");
        assert_eq!(
            history_empty_reason(360, true),
            "Paused: new messages for this topic are not listed"
        );
    }

    #[test]
    fn count_hover_names_unit() {
        assert_eq!(count_hover(true, 6), "6 leaf topics below");
        assert_eq!(count_hover(false, 360), "360 messages received");
        assert_eq!(count_hover(true, 1), "1 leaf topic below");
        assert_eq!(count_hover(false, 1), "1 message received");
    }

    #[test]
    fn history_excludes_query_replies() {
        // G3-12: a reply for the selected key belongs to Query Results, not History.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.browse_tree
            .write()
            .unwrap()
            .insert_path("r/a")
            .update_data(
                "sub-val".to_string(),
                "text/plain".to_string(),
                false,
                SampleKindView::Put,
                None,
            );
        let mk = |p: &str, t: MessageType| {
            ZenohMessage::new_with_bytes(
                "r/a".to_string(),
                p.to_string(),
                p.as_bytes().to_vec(),
                "text/plain".to_string(),
                chrono::Utc::now(),
                t,
                false,
                MessageSource::MonitorSession,
            )
        };
        app.messages
            .push_back(mk("sub-val", MessageType::Subscribe));
        app.messages
            .push_back(mk("reply-val", MessageType::QueryReply));
        app.selected_topic = Some("r/a".to_string());
        let texts = details_texts(&mut app);
        assert!(!texts.iter().any(|t| t.contains("reply-val")), "{texts:?}");
    }
}
