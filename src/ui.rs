//! UI rendering implementation for ZenohExplorer.

use egui::{Color32, Margin, RichText};
use tracing::{error, info};

use crate::app::ZenohExplorer;
use crate::commands::ZenohCommand;
use crate::theme::*;
use crate::types::*;
use crate::utils::*;

impl eframe::App for ZenohExplorer {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            info!("First UI update frame");
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        });

        self.process_events();
        self.apply_theme(ctx);

        egui::CentralPanel::default()
            .frame(
                egui::Frame::default()
                    .fill(self.background_color())
                    .inner_margin(Margin::same(8.0)),
            )
            .show(ctx, |ui| {
                self.show_header(ui);
                ui.separator();
                self.show_connection_panel(ui);
                ui.separator();
                self.show_main_content(ui);
            });

        ctx.request_repaint_after(std::time::Duration::from_millis(66));
    }
}

impl ZenohExplorer {
    pub fn apply_theme(&self, ctx: &egui::Context) {
        ctx.request_repaint_after(std::time::Duration::from_millis(66));

        ctx.style_mut(|style| {
            style.animation_time = 0.001;
            if self.dark_mode {
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;
                style.visuals.window_fill = ExplorerColors::DARK_BACKGROUND;
                style.visuals.panel_fill = ExplorerColors::DARK_CARD_BACKGROUND;
                style.visuals.extreme_bg_color = ExplorerColors::DARK_SURFACE;
                style.visuals.faint_bg_color = ExplorerColors::DARK_SIDEBAR;
                style.visuals.widgets.inactive.bg_fill = ExplorerColors::DARK_SURFACE;
                style.visuals.widgets.hovered.bg_fill = Color32::from_gray(70);
                style.visuals.widgets.active.bg_fill = ExplorerColors::DARK_SURFACE;
                style.visuals.widgets.inactive.bg_stroke.color = Color32::from_gray(100);
                style.visuals.widgets.hovered.bg_stroke.color = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.active.bg_stroke.color = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.inactive.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.widgets.hovered.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.widgets.active.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.widgets.noninteractive.bg_fill = ExplorerColors::DARK_CARD_BACKGROUND;
                style.visuals.widgets.noninteractive.fg_stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.code_bg_color = Color32::from_gray(30);
                style.visuals.selection.bg_fill = ExplorerColors::DARK_SELECTED_BACKGROUND;
                style.visuals.selection.stroke.color = ExplorerColors::DARK_TEXT_PRIMARY;
                style.visuals.override_text_color = Some(ExplorerColors::DARK_TEXT_PRIMARY);
            } else {
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;
                style.visuals.window_fill = ExplorerColors::BACKGROUND;
                style.visuals.panel_fill = ExplorerColors::CARD_BACKGROUND;
                style.visuals.extreme_bg_color = ExplorerColors::SURFACE;
                style.visuals.faint_bg_color = ExplorerColors::SIDEBAR;
                style.visuals.widgets.inactive.bg_fill = Color32::WHITE;
                style.visuals.widgets.hovered.bg_fill = Color32::from_gray(250);
                style.visuals.widgets.active.bg_fill = Color32::WHITE;
                style.visuals.widgets.inactive.bg_stroke.color = Color32::from_gray(200);
                style.visuals.widgets.hovered.bg_stroke.color = ExplorerColors::PRIMARY;
                style.visuals.widgets.active.bg_stroke.color = ExplorerColors::PRIMARY;
                style.visuals.widgets.inactive.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.widgets.hovered.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.widgets.active.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.widgets.noninteractive.bg_fill = ExplorerColors::CARD_BACKGROUND;
                style.visuals.widgets.noninteractive.fg_stroke.color = ExplorerColors::TEXT_PRIMARY;
                style.visuals.code_bg_color = Color32::from_gray(240);
                style.visuals.selection.bg_fill = ExplorerColors::SELECTED_BACKGROUND;
                style.visuals.selection.stroke.color = Color32::WHITE;
                style.visuals.override_text_color = Some(ExplorerColors::TEXT_PRIMARY);
            }
        });
    }

    fn show_header(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label(
                RichText::new("Zenoh Explorer")
                    .size(HEADING_LARGE_SIZE)
                    .color(self.text_color()),
            );

            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                if ui.button(if self.dark_mode { "☀" } else { "🌙" }).clicked() {
                    self.dark_mode = !self.dark_mode;
                }

                ui.separator();

                if !self.worker_healthy {
                    let pulse = self.animate_pulse(ui.ctx(), "worker_health_pulse");
                    let error_color = ExplorerColors::ERROR;
                    let pulsing_color = Color32::from_rgba_unmultiplied(
                        error_color.r(), error_color.g(), error_color.b(), (255.0 * pulse) as u8,
                    );
                    ui.label(RichText::new("Worker Unresponsive").color(pulsing_color).size(TEXT_SMALL_SIZE));
                    ui.separator();
                }

                if matches!(self.connection_status, ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor) {
                    ui.spinner();
                }
                ui.label(RichText::new(format!("● {}", self.connection_status.text())).color(self.connection_status.color()));

                if matches!(self.connection_status, ConnectionStatus::Connected) {
                    if self.discovered_peers > 0 || self.discovered_routers > 0 {
                        let mut parts = Vec::new();
                        if self.discovered_routers > 0 { parts.push(format!("{}R", self.discovered_routers)); }
                        if self.discovered_peers > 0 { parts.push(format!("{}P", self.discovered_peers)); }
                        ui.label(RichText::new(format!("({})", parts.join(" "))).color(self.text_tertiary_color()).size(TEXT_SMALL_SIZE));
                    }
                }

                let total_memory_bytes = self.current_memory_bytes + self.import_memory_bytes;
                if !self.messages.is_empty() || self.messages_dropped > 0 || self.import_memory_bytes > 0 {
                    ui.separator();
                    let memory_mb = total_memory_bytes as f32 / (1024.0 * 1024.0);
                    let memory_percent = (memory_mb / self.max_memory_mb as f32 * 100.0).min(100.0);

                    if memory_percent > 80.0 && !self.memory_warning_shown {
                        self.memory_warning_shown = true;
                    } else if memory_percent < 70.0 {
                        self.memory_warning_shown = false;
                    }

                    let memory_color = if memory_percent > 90.0 {
                        ExplorerColors::ERROR
                    } else if memory_percent > 70.0 {
                        ExplorerColors::WARNING
                    } else {
                        ExplorerColors::SUCCESS
                    };

                    let memory_text = if self.import_memory_bytes > 0 {
                        format!("Memory: {:.1}MB/{:.0}MB (+{:.1}MB import)",
                            self.current_memory_bytes as f32 / (1024.0 * 1024.0),
                            self.max_memory_mb as f32,
                            self.import_memory_bytes as f32 / (1024.0 * 1024.0))
                    } else {
                        format!("Memory: {:.1}MB/{:.0}MB", memory_mb, self.max_memory_mb as f32)
                    };

                    ui.label(RichText::new(memory_text).color(memory_color).size(TEXT_SMALL_SIZE));

                    if self.messages_dropped > 0 || self.rate_limit_drops > 0 {
                        let drop_text = if self.rate_limit_drops > 0 {
                            format!("({} dropped, {} rate limited)", self.messages_dropped, self.rate_limit_drops)
                        } else {
                            format!("({} dropped)", self.messages_dropped)
                        };
                        ui.label(RichText::new(drop_text).color(ExplorerColors::WARNING).size(TEXT_SMALL_SIZE));
                    }
                }
            });
        });
    }

    fn show_connection_panel(&mut self, ui: &mut egui::Ui) {
        if matches!(self.connection_status, ConnectionStatus::Disconnected | ConnectionStatus::Error(_)) {
            ui.group(|ui| {
                ui.label("Connection Settings");
                ui.horizontal(|ui| {
                    ui.label("Transport:");
                    egui::ComboBox::from_id_salt("connect_transport")
                        .width(60.0)
                        .selected_text(&self.connect_transport)
                        .show_ui(ui, |ui| {
                            ui.selectable_value(&mut self.connect_transport, "tcp".to_string(), "tcp");
                            ui.selectable_value(&mut self.connect_transport, "udp".to_string(), "udp");
                            ui.selectable_value(&mut self.connect_transport, "quic".to_string(), "quic");
                            ui.selectable_value(&mut self.connect_transport, "ws".to_string(), "ws");
                            ui.selectable_value(&mut self.connect_transport, "tls".to_string(), "tls");
                        });
                    ui.label("Address:");
                    ui.add(egui::TextEdit::singleline(&mut self.connect_address).desired_width(120.0));
                    ui.label("Port:");
                    ui.add(egui::TextEdit::singleline(&mut self.connect_port).desired_width(50.0));
                });

                ui.horizontal(|ui| {
                    let locator_preview = if self.connect_address.is_empty() {
                        "(multicast discovery)".to_string()
                    } else {
                        format!("{}/{}:{}", self.connect_transport, self.connect_address, self.connect_port)
                    };
                    ui.label(RichText::new(format!("→ {}", locator_preview)).size(TEXT_SMALL_SIZE - 1.0).italics().color(self.text_tertiary_color()));
                });

                ui.horizontal(|ui| {
                    ui.label("Mode:");
                    egui::ComboBox::from_id_salt("connection_mode")
                        .selected_text(&self.connection_mode)
                        .show_ui(ui, |ui| {
                            ui.selectable_value(&mut self.connection_mode, "client".to_string(), "Client");
                            ui.selectable_value(&mut self.connection_mode, "peer".to_string(), "Peer");
                        });
                });

                if self.connection_mode == "peer" {
                    ui.horizontal(|ui| {
                        ui.label("Listen Port:");
                        ui.add(egui::TextEdit::singleline(&mut self.listen_port).desired_width(60.0));
                    });
                    ui.label(RichText::new("Peer mode: Use different listen ports for each peer").size(TEXT_SMALL_SIZE).color(self.text_secondary_color()));
                } else {
                    ui.label(RichText::new("Client mode: Connects to Zenoh router").size(TEXT_SMALL_SIZE).color(self.text_secondary_color()));
                }

                if let ConnectionStatus::Error(ref err) = self.connection_status {
                    ui.colored_label(ExplorerColors::ERROR, format!("Error: {}", err));
                }

                if ui.button("Connect").clicked() {
                    if let Some(sender) = &self.command_sender {
                        self.connection_status = ConnectionStatus::ConnectingPublishing;
                        let locators = if self.connect_address.is_empty() {
                            String::new()
                        } else {
                            format!("{}/{}:{}", self.connect_transport, self.connect_address, self.connect_port)
                        };
                        info!("GUI sending Connect command - mode: {}, locators: {}, listen_port: {}", 
                              self.connection_mode, locators, self.listen_port);
                        match sender.send(ZenohCommand::Connect {
                            locators,
                            listen_port: self.listen_port.clone(),
                            mode: self.connection_mode.clone(),
                            config_json: self.config_json.clone(),
                        }) {
                            Ok(_) => info!("Connect command sent successfully"),
                            Err(e) => error!("Failed to send Connect command: {:?}", e),
                        }
                    }
                }
            });
        } else {
            ui.horizontal(|ui| {
                if ui.button("Disconnect").clicked() {
                    self.connection_status = ConnectionStatus::Disconnected;
                    self.subscriptions.clear();
                    if let Some(sender) = &self.command_sender {
                        let _ = sender.send(ZenohCommand::Disconnect);
                    }
                }
            });
        }
    }

    fn show_main_content(&mut self, ui: &mut egui::Ui) {
        egui::TopBottomPanel::top("toolbar").show_inside(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label("Quick Actions:");
                if ui.selectable_label(self.detail_view == DetailView::TopicDetails, "📊 Topics").clicked() {
                    self.detail_view = DetailView::TopicDetails;
                }
                if ui.selectable_label(self.detail_view == DetailView::Publish, "📤 Publish").clicked() {
                    self.detail_view = DetailView::Publish;
                }
                if ui.selectable_label(self.detail_view == DetailView::Query, "🔍 Query").clicked() {
                    self.detail_view = DetailView::Query;
                }
                if ui.selectable_label(self.detail_view == DetailView::Help, "❓ Help").clicked() {
                    self.detail_view = DetailView::Help;
                }
            });
        });

        egui::SidePanel::left("tree_panel")
            .default_width(400.0)
            .min_width(250.0)
            .resizable(true)
            .show_inside(ui, |ui| {
                self.show_tree_panel(ui);
            });

        egui::CentralPanel::default().show_inside(ui, |ui| {
            self.show_detail_panel(ui);
        });
    }

    pub fn show_tree_panel(&mut self, ui: &mut egui::Ui) {
        ui.vertical(|ui| {
            ui.horizontal(|ui| {
                ui.label("🔍");
                ui.text_edit_singleline(&mut self.tree_filter).on_hover_text("Filter topics");
                if ui.button("✖").clicked() {
                    self.tree_filter.clear();
                }
            });

            if self.selected_topic.is_some() {
                if ui.button("⬅ Back to All Messages").clicked() {
                    self.selected_topic = None;
                }
            }

            ui.separator();

            ui.collapsing("Subscribe to Topics", |ui| {
                ui.horizontal(|ui| {
                    ui.label("Key:");
                    ui.text_edit_singleline(&mut self.subscribe_key);
                });
                if ui.add_enabled(
                    matches!(self.connection_status, ConnectionStatus::Connected) && !self.subscribe_key.is_empty(),
                    egui::Button::new("Subscribe"),
                ).clicked() {
                    if let Some(sender) = &self.command_sender {
                        let _ = sender.send(ZenohCommand::Subscribe {
                            key_expr: self.subscribe_key.clone(),
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                }

                if !self.subscriptions.is_empty() {
                    ui.label(RichText::new("Active:").size(SUBSCRIPTION_TEXT_SIZE));
                    for subscription in &self.subscriptions {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(&subscription.key_expr).size(SUBSCRIPTION_TEXT_SIZE));
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
            ui.label(RichText::new("Topics").strong());

            let tree_clone = if let Ok(tree) = self.browse_tree.read() {
                tree.clone()
            } else {
                ZenohNode::new("root".to_string())
            };

            egui::ScrollArea::vertical().auto_shrink([false; 2]).show(ui, |ui| {
                if tree_clone.children.is_empty() {
                    ui.vertical_centered(|ui| {
                        ui.add_space(32.0);
                        ui.label(RichText::new("No topics yet").size(HEADING_MEDIUM_SIZE).color(self.text_tertiary_color()));
                        ui.add_space(8.0);
                        ui.label(RichText::new("Subscribe to key expressions to see network activity").italics().color(self.text_secondary_color()));
                    });
                } else {
                    for (_, child) in &tree_clone.children {
                        self.show_tree_node(ui, child, String::new(), 0);
                    }
                }
            });
        });
    }

    pub fn show_tree_node(&mut self, ui: &mut egui::Ui, node: &ZenohNode, parent_path: String, depth: usize) {
        let full_path = if parent_path.is_empty() {
            node.key.clone()
        } else {
            format!("{}/{}", parent_path, node.key)
        };

        if !self.tree_filter.is_empty() && !full_path.contains(&self.tree_filter) {
            if !self.has_matching_descendant(node, &self.tree_filter, &full_path) {
                return;
            }
        }

        let indent = 12.0 * depth as f32;
        let is_selected = self.selected_topic.as_ref().map_or(false, |t| t == &full_path);

        if node.children.is_empty() {
            ui.horizontal(|ui| {
                ui.add_space(indent);
                if node.is_local {
                    let fade = self.animate_fade_in(ui.ctx(), &format!("local_leaf_{}", full_path), 1.0);
                    let base_color = if self.dark_mode { ExplorerColors::DARK_SUCCESS } else { ExplorerColors::SUCCESS };
                    let animated_color = Color32::from_rgba_unmultiplied(base_color.r(), base_color.g(), base_color.b(), (255.0 * fade) as u8);
                    ui.label(RichText::new("●").size(8.0).color(animated_color)).on_hover_text("Published from this app");
                }
                if ui.selectable_label(is_selected, format!("📄 {}", node.key)).clicked() {
                    self.selected_topic = Some(full_path.clone());
                    self.detail_view = DetailView::TopicDetails;
                }
                if node.message_count > 0 {
                    ui.label(RichText::new(format!("({})", node.message_count)).size(TEXT_SMALL_SIZE).color(ExplorerColors::PRIMARY));
                }
                if let Some(ref payload) = node.last_payload {
                    let preview = if payload.len() > 30 {
                        let end = safe_truncate_index(payload, 30);
                        format!("{}...", &payload[..end])
                    } else {
                        payload.clone()
                    };
                    ui.label(RichText::new(preview).size(TOPIC_PREVIEW_TEXT_SIZE).color(self.text_secondary_color()));
                }
            });
        } else {
            let id = egui::Id::new(format!("treenode_{}", full_path));
            let state = egui::collapsing_header::CollapsingState::load_with_default_open(ui.ctx(), id, false);

            state.show_header(ui, |ui| {
                ui.horizontal(|ui| {
                    ui.add_space(indent);
                    if node.is_local {
                        let fade = self.animate_fade_in(ui.ctx(), &format!("local_branch_{}", full_path), 1.0);
                        let base_color = if self.dark_mode { ExplorerColors::DARK_SUCCESS } else { ExplorerColors::SUCCESS };
                        let animated_color = Color32::from_rgba_unmultiplied(base_color.r(), base_color.g(), base_color.b(), (255.0 * fade) as u8);
                        ui.label(RichText::new("●").size(8.0).color(animated_color));
                    }
                    if ui.selectable_label(is_selected, format!("📁 {}", node.key)).clicked() {
                        self.selected_topic = Some(full_path.clone());
                        self.detail_view = DetailView::TopicDetails;
                    }
                    ui.label(RichText::new(format!("({})", node.children.len())).size(TEXT_SMALL_SIZE).color(self.text_tertiary_color()));
                });
            }).body(|ui| {
                for (_, child) in &node.children {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }

    fn show_detail_panel(&mut self, ui: &mut egui::Ui) {
        match self.detail_view {
            DetailView::TopicDetails => self.show_topic_details(ui),
            DetailView::Publish => self.show_publish_tab(ui),
            DetailView::Query => self.show_query_tab(ui),
            DetailView::Help => self.show_help_tab(ui),
        }
    }

    fn show_topic_details(&mut self, ui: &mut egui::Ui) {
        if let Some(ref topic) = self.selected_topic.clone() {
            ui.heading(topic);

            ui.horizontal(|ui| {
                // Export button - handles both direct payloads and chunked payloads
                if ui.button("Export Payload").on_hover_text("Save full payload to file (original size)").clicked() {
                    if let Ok(store) = self.payload_store.read() {
                        // First try direct lookup
                        if let Some((payload, _ts)) = store.get(topic) {
                            self.export_payload_to_file(topic, payload);
                        } else {
                            // Check for chunked payload: look for topic/__chunk/{size}/{count}/{index}
                            let chunk_prefix = format!("{}/__chunk/", topic);
                            let mut chunks: Vec<(usize, usize, usize, &Vec<u8>)> = Vec::new();

                            for (key, (data, _ts)) in store.iter() {
                                if key.starts_with(&chunk_prefix) {
                                    let suffix = &key[chunk_prefix.len()..];
                                    let parts: Vec<&str> = suffix.split('/').collect();
                                    if parts.len() == 3 {
                                        if let (Ok(total_size), Ok(total_chunks), Ok(chunk_idx)) = (
                                            parts[0].parse::<usize>(),
                                            parts[1].parse::<usize>(),
                                            parts[2].parse::<usize>(),
                                        ) {
                                            chunks.push((total_size, total_chunks, chunk_idx, data));
                                        }
                                    }
                                }
                            }

                            if !chunks.is_empty() {
                                // Sort by chunk index
                                chunks.sort_by_key(|(_, _, idx, _)| *idx);
                                let (total_size, total_chunks, _, _) = chunks[0];

                                // Verify we have all chunks
                                if chunks.len() == total_chunks {
                                    // Reassemble
                                    let mut reassembled = Vec::with_capacity(total_size);
                                    for (_, _, _, data) in &chunks {
                                        reassembled.extend_from_slice(data);
                                    }
                                    info!("Reassembled {} chunks into {} bytes", chunks.len(), reassembled.len());
                                    self.export_payload_to_file(topic, &reassembled);
                                } else {
                                    info!("Missing chunks: have {}/{}", chunks.len(), total_chunks);
                                }
                            }
                        }
                    }
                }

                let is_paused = self.paused_keys.contains(topic);
                let button_text = if is_paused { "▶ Resume" } else { "⏸ Pause" };
                let button_color = if is_paused { ExplorerColors::WARNING } else { self.text_secondary_color() };

                if ui.button(RichText::new(button_text).color(button_color)).clicked() {
                    if is_paused {
                        self.paused_keys.remove(topic);
                    } else {
                        self.paused_keys.insert(topic.clone());
                    }
                }

                if is_paused {
                    ui.label(RichText::new("⏸ Paused").color(ExplorerColors::WARNING).size(TEXT_SMALL_SIZE));
                }
            });

            ui.separator();

            // Check for chunked payload and show info
            let chunk_info = if let Ok(store) = self.payload_store.read() {
                let chunk_prefix = format!("{}/__chunk/", topic);
                let mut chunks: Vec<(usize, usize, usize)> = Vec::new();

                for (key, _) in store.iter() {
                    if key.starts_with(&chunk_prefix) {
                        let suffix = &key[chunk_prefix.len()..];
                        let parts: Vec<&str> = suffix.split('/').collect();
                        if parts.len() == 3 {
                            if let (Ok(total_size), Ok(total_chunks), Ok(chunk_idx)) = (
                                parts[0].parse::<usize>(),
                                parts[1].parse::<usize>(),
                                parts[2].parse::<usize>(),
                            ) {
                                chunks.push((total_size, total_chunks, chunk_idx));
                            }
                        }
                    }
                }

                if !chunks.is_empty() {
                    let (total_size, total_chunks, _) = chunks[0];
                    Some((chunks.len(), total_chunks, total_size))
                } else {
                    None
                }
            } else {
                None
            };

            // Display chunk info if this is a chunked payload
            if let Some((received, total, total_size)) = chunk_info {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("📦 Chunked Payload:").strong().color(ExplorerColors::SUCCESS));
                    let size_str = if total_size >= 1024 * 1024 * 1024 {
                        format!("{:.2} GB", total_size as f64 / (1024.0 * 1024.0 * 1024.0))
                    } else if total_size >= 1024 * 1024 {
                        format!("{:.2} MB", total_size as f64 / (1024.0 * 1024.0))
                    } else {
                        format!("{} bytes", total_size)
                    };
                    ui.label(format!("{}/{} chunks received, {} total", received, total, size_str));
                });
                if received == total {
                    ui.label(RichText::new("✔ All chunks received - click Export to reassemble").color(ExplorerColors::SUCCESS));
                } else {
                    ui.label(RichText::new(format!("⏳ Waiting for {} more chunks...", total - received)).color(ExplorerColors::WARNING));
                }
                ui.separator();
            }

            let (message_count, payload_opt, encoding_opt) = if let Ok(tree) = self.browse_tree.read() {
                if let Some(node) = self.find_node(&tree, topic) {
                    (node.message_count, node.last_payload.clone(), node.last_encoding.clone())
                } else {
                    (0, None, None)
                }
            } else {
                (0, None, None)
            };

            ui.horizontal(|ui| {
                ui.label(RichText::new("Messages:").strong());
                ui.label(message_count.to_string());
            });

            if let Some(payload) = payload_opt {
                ui.separator();
                ui.label(RichText::new("Current Value:").strong());

                const COLLAPSED_SIZE: usize = 1024;
                let is_large = payload.len() > COLLAPSED_SIZE;
                let is_expanded = self.expanded_payloads.contains(topic);

                if is_large {
                    let button_text = if is_expanded {
                        "▼ Collapse".to_string()
                    } else {
                        format!("▶ Expand (+{} bytes)", payload.len() - COLLAPSED_SIZE)
                    };
                    if ui.button(&button_text).clicked() {
                        if is_expanded {
                            self.expanded_payloads.remove(topic);
                        } else {
                            self.expanded_payloads.insert(topic.clone());
                        }
                    }
                }

                let display_payload = if is_large && !is_expanded {
                    let end = safe_truncate_index(&payload, COLLAPSED_SIZE);
                    format!("{}...", &payload[..end])
                } else {
                    payload.clone()
                };

                egui::ScrollArea::vertical()
                    .id_salt(format!("payload_{}", topic))
                    .max_height(400.0)
                    .show(ui, |ui| {
                        if let Some(pretty) = self.get_cached_json(&display_payload) {
                            ui.label(RichText::new(&pretty).code().color(self.text_color()));
                        } else {
                            ui.label(RichText::new(&display_payload).code().color(self.text_color()));
                        }
                    });

                if let Some(encoding) = encoding_opt {
                    ui.separator();
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Encoding:").strong());
                        ui.label(encoding);
                    });
                }
            }

            ui.separator();
            ui.label(RichText::new("Message History:").strong());
            self.show_message_history(ui, topic);
        } else {
            ui.heading("All Messages");
            ui.separator();
            self.show_messages_tab(ui);
        }
    }

    fn show_message_history(&mut self, ui: &mut egui::Ui, topic: &str) {
        egui::ScrollArea::vertical().show(ui, |ui| {
            let topic_messages: Vec<_> = self.messages.iter().filter(|m| m.key == topic).rev().take(50).collect();

            if topic_messages.is_empty() {
                ui.vertical_centered(|ui| {
                    ui.add_space(16.0);
                    ui.label(RichText::new("No messages yet").size(HEADING_MEDIUM_SIZE).color(self.text_tertiary_color()));
                });
            } else {
                for message in topic_messages {
                    ui.group(|ui| {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(message.timestamp.format("%H:%M:%S%.3f").to_string()).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                            ui.label(RichText::new(message.message_type.label()).background_color(message.message_type.color()).color(Color32::WHITE).size(TEXT_SMALL_SIZE));
                        });
                        if !message.payload.is_empty() {
                            let display = if message.payload.len() > 200 {
                                let end = safe_truncate_index(&message.payload, 200);
                                format!("{}...", &message.payload[..end])
                            } else {
                                message.payload.clone()
                            };
                            ui.label(RichText::new(display).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                        }
                    });
                }
            }
        });
    }

    pub fn show_publish_tab(&mut self, ui: &mut egui::Ui) {
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(ExplorerColors::ERROR, "⚠ Not connected. Please connect first.");
            ui.separator();
        }

        ui.group(|ui| {
            ui.label("Publish Data");
            ui.horizontal(|ui| {
                ui.label("Key:");
                ui.text_edit_singleline(&mut self.publish_key);
            });

            ui.horizontal(|ui| {
                ui.label("Payload:");
                if ui.button("Import File").clicked() {
                    if let Some(path) = rfd::FileDialog::new().pick_file() {
                        match std::fs::read(&path) {
                            Ok(bytes) => {
                                self.publish_payload_filename = path.file_name().map(|n| n.to_string_lossy().to_string());
                                self.publish_payload_expanded = false;
                                let total_len = bytes.len();
                                let preview_len = total_len.min(256);

                                self.publish_payload = if let Ok(text) = std::str::from_utf8(&bytes) {
                                    if total_len > preview_len {
                                        let safe_end = safe_truncate_index(text, preview_len);
                                        format!("{}... [+{} bytes]", &text[..safe_end], total_len - safe_end)
                                    } else {
                                        text.to_string()
                                    }
                                } else {
                                    let hex: String = bytes[..preview_len].iter().map(|b| format!("{:02x} ", b)).collect();
                                    format!("{}... [{} bytes total]", hex.trim(), total_len)
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
                if self.publish_payload_bytes.is_some() {
                    if ui.button("✖ Clear").clicked() {
                        self.publish_payload_bytes = None;
                        self.publish_payload_filename = None;
                        self.publish_payload_expanded = false;
                        self.import_memory_bytes = 0;
                        self.publish_payload = "Hello Zenoh!".to_string();
                        self.publish_encoding = "text/plain".to_string();
                    }
                }
            });

            if let Some(ref filename) = self.publish_payload_filename.clone() {
                let bytes_len = self.publish_payload_bytes.as_ref().map(|b| b.len());
                let was_expanded = self.publish_payload_expanded;
                let mut should_regenerate = false;

                ui.horizontal(|ui| {
                    ui.label(RichText::new(filename).color(self.text_secondary_color()));
                    if let Some(len) = bytes_len {
                        ui.label(RichText::new(format!("({} bytes)", len)).color(self.text_tertiary_color()));

                        // Expand/collapse button for files > 256 bytes
                        if len > 256 {
                            let button_text = if was_expanded { "▼ Collapse" } else { "▶ Expand" };
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
                            if total_len > preview_len {
                                let safe_end = safe_truncate_index(text, preview_len);
                                format!("{}... [+{} bytes]", &text[..safe_end], total_len - safe_end)
                            } else {
                                text.to_string()
                            }
                        } else {
                            let hex: String = bytes[..preview_len].iter().map(|b| format!("{:02x} ", b)).collect();
                            if total_len > preview_len {
                                format!("{}... [+{} bytes, {} total]", hex.trim(), total_len - preview_len, total_len)
                            } else {
                                hex
                            }
                        };
                    }
                }
            }

            let max_height = if self.publish_payload_expanded { 200.0 } else { 80.0 };
            let payload_response = egui::ScrollArea::vertical().max_height(max_height).show(ui, |ui| {
                ui.add(egui::TextEdit::multiline(&mut self.publish_payload)
                    .desired_width(f32::INFINITY)
                    .interactive(self.publish_payload_bytes.is_none())
                    .font(egui::TextStyle::Monospace))
            }).inner;

            // If user edits text, clear file import
            if payload_response.changed() && self.publish_payload_bytes.is_some() {
                self.publish_payload_bytes = None;
                self.publish_payload_filename = None;
                self.publish_payload_expanded = false;
                self.import_memory_bytes = 0;
            }

            ui.horizontal(|ui| {
                ui.label("Encoding:");
                ui.text_edit_singleline(&mut self.publish_encoding);
            });

            if ui.add_enabled(
                matches!(self.connection_status, ConnectionStatus::Connected) && !self.publish_key.is_empty(),
                egui::Button::new("Publish"),
            ).clicked() {
                if let Some(sender) = &self.command_sender {
                    let from_import = self.publish_payload_bytes.is_some();
                    let payload_bytes = self.publish_payload_bytes.take()
                        .unwrap_or_else(|| self.publish_payload.as_bytes().to_vec());

                    let payload_len = payload_bytes.len();
                    info!("GUI: About to send Publish command for {} bytes", payload_len);

                    match sender.send(ZenohCommand::Publish {
                        key: self.publish_key.clone(),
                        payload: payload_bytes,
                        encoding: self.publish_encoding.clone(),
                        from_import,
                    }) {
                        Ok(_) => info!("GUI: Publish command sent successfully for {} bytes", payload_len),
                        Err(e) => error!("GUI: Failed to send Publish command: {:?}", e),
                    }

                    self.publish_payload_filename = None;
                    self.publish_payload = String::new();
                    self.publish_payload_expanded = false;
                    self.import_memory_bytes = 0;
                }
            }
        });

        ui.add_space(16.0);

        ui.group(|ui| {
            ui.label(RichText::new("Queryable").strong());
            ui.label(RichText::new("Respond to queries for locally published keys").size(TEXT_SMALL_SIZE).color(self.text_secondary_color()));

            ui.horizontal(|ui| {
                ui.label("Key Pattern:");
                ui.text_edit_singleline(&mut self.queryable_pattern);
            });

            ui.horizontal(|ui| {
                let was_enabled = self.queryable_enabled;
                ui.checkbox(&mut self.queryable_enabled, "Enable Queryable");

                if self.queryable_enabled {
                    ui.label(RichText::new("Active").color(if self.dark_mode { ExplorerColors::DARK_SUCCESS } else { ExplorerColors::SUCCESS }).size(TEXT_SMALL_SIZE));
                } else {
                    ui.label(RichText::new("Inactive").color(self.text_tertiary_color()).size(TEXT_SMALL_SIZE));
                }

                if was_enabled != self.queryable_enabled {
                    if let Some(sender) = &self.command_sender {
                        if self.queryable_enabled {
                            let _ = sender.send(ZenohCommand::EnableQueryable { key_expr: self.queryable_pattern.clone() });
                        } else {
                            let _ = sender.send(ZenohCommand::DisableQueryable);
                        }
                    }
                }
            });
        });
    }

    pub fn show_query_tab(&mut self, ui: &mut egui::Ui) {
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(ExplorerColors::ERROR, "⚠ Not connected.");
            ui.separator();
        }

        ui.label(RichText::new("Note: Queries require queryables running on the network.").color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
        ui.separator();

        if let Some(alert) = &self.query_alert.clone() {
            ui.group(|ui| {
                ui.colored_label(ExplorerColors::WARNING, "Query Alert");
                ui.label(alert);
                if ui.button("Dismiss").clicked() {
                    self.query_alert = None;
                }
            });
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

            if ui.add_enabled(
                matches!(self.connection_status, ConnectionStatus::Connected) && !self.query_selector.is_empty(),
                egui::Button::new("Query"),
            ).clicked() {
                if let Some(sender) = &self.command_sender {
                    let timeout = self.query_timeout.parse().unwrap_or(10000);
                    let _ = sender.send(ZenohCommand::Query {
                        selector: self.query_selector.clone(),
                        value: self.query_value.clone(),
                        timeout_ms: timeout,
                    });
                    self.query_alert = Some(format!("Query sent for '{}'. Waiting...", self.query_selector));
                }
            }
        });

        ui.add_space(16.0);

        ui.group(|ui| {
            ui.label(RichText::new("Query Results").strong());
            ui.separator();

            let query_replies: Vec<_> = self.messages.iter()
                .filter(|m| m.message_type == MessageType::QueryReply)
                .rev().take(50).cloned().collect();

            if query_replies.is_empty() {
                ui.vertical_centered(|ui| {
                    ui.add_space(16.0);
                    ui.label(RichText::new("No query results yet").size(HEADING_MEDIUM_SIZE).color(self.text_tertiary_color()));
                });
            } else {
                egui::ScrollArea::vertical().max_height(400.0).show(ui, |ui| {
                    for message in &query_replies {
                        ui.group(|ui| {
                            ui.horizontal(|ui| {
                                if message.is_local {
                                    ui.label(RichText::new("●").size(8.0).color(if self.dark_mode { ExplorerColors::DARK_SUCCESS } else { ExplorerColors::SUCCESS }));
                                }
                                ui.label(RichText::new(message.timestamp.format("%H:%M:%S%.3f").to_string()).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                                ui.label(RichText::new(&message.key).strong());
                            });
                            if !message.payload.is_empty() {
                                let display = if message.payload.len() > 500 {
                                    let end = safe_truncate_index(&message.payload, 500);
                                    format!("{}...", &message.payload[..end])
                                } else {
                                    message.payload.clone()
                                };
                                ui.label(RichText::new(display).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                            }
                        });
                    }
                });
            }
        });
    }

    pub fn show_messages_tab(&mut self, ui: &mut egui::Ui) {
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

        ui.horizontal(|ui| {
            ui.label("Memory Limit (MB):");
            let mut limit_str = self.max_memory_mb.to_string();
            if ui.text_edit_singleline(&mut limit_str).changed() {
                if let Ok(new_limit) = limit_str.parse::<usize>() {
                    self.max_memory_mb = new_limit.max(10).min(1000);
                }
            }

            ui.label("Message Limit:");
            let mut count_str = self.max_messages.to_string();
            if ui.text_edit_singleline(&mut count_str).changed() {
                if let Ok(new_limit) = count_str.parse::<usize>() {
                    self.max_messages = new_limit.max(100).min(50000);
                }
            }

            ui.label("Rate (msg/s):");
            let mut rate_str = self.rate_limiter.max_messages_per_second.to_string();
            if ui.text_edit_singleline(&mut rate_str).changed() {
                if let Ok(new_rate) = rate_str.parse::<usize>() {
                    self.rate_limiter.max_messages_per_second = new_rate.max(10).min(10000);
                }
            }

            ui.checkbox(&mut self.dedup_enabled, "Dedup");
            if self.messages_deduped > 0 {
                ui.label(RichText::new(format!("({} deduped)", self.messages_deduped)).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
            }
        });

        egui::ScrollArea::vertical().auto_shrink([false; 2]).stick_to_bottom(self.auto_scroll).show(ui, |ui| {
            const MAX_RENDERED: usize = 500;
            let start_idx = self.messages.len().saturating_sub(MAX_RENDERED);

            for message in self.messages.iter().skip(start_idx) {
                let search_end = safe_truncate_index(&message.payload, MAX_HASH_BYTES);
                let payload_search = &message.payload[..search_end];

                if self.message_filter.is_empty() || message.key.contains(&self.message_filter) || payload_search.contains(&self.message_filter) {
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(message.message_type.label()).background_color(message.message_type.color()).color(Color32::WHITE).size(TEXT_SMALL_SIZE));
                        ui.label(RichText::new(message.timestamp.format("%H:%M:%S%.3f").to_string()).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                        ui.label(RichText::new(&message.key).strong());
                    });

                    if !message.payload.is_empty() {
                        let display = if message.payload.len() > 200 {
                            let end = safe_truncate_index(&message.payload, 200);
                            format!("{}...", &message.payload[..end])
                        } else {
                            message.payload.clone()
                        };
                        ui.label(RichText::new(display).color(self.text_secondary_color()).size(TEXT_SMALL_SIZE));
                    }
                    ui.separator();
                }
            }
        });
    }

    fn show_help_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(RichText::new("Zenoh Explorer Help").size(HEADING_MEDIUM_SIZE).strong());
        ui.separator();

        ui.label("This is a Zenoh-based peer & client messaging utility.");
        ui.separator();

        ui.label(RichText::new("Getting Started:").strong());
        ui.label("1. Configure connection settings and click Connect.");
        ui.label("   • For a quick peer mesh, leave as Peer & Address field blank and select the tcp port of your peers (7447 by default)");
        ui.label("   • EARLY VERSION: Only tcp transport and multicast have been tested");
        ui.label("2. Use Subscribe tab to listen to key expressions (e.g., demo/**)");
        ui.label("3. Use Publish tab to send data. Enter text or import files of any size or type.");
        ui.label("4. Enable simple Queryables service (optional, respond to queries for items in keyspace)");
        ui.label("5. Use Browse tab to explore the keyspace tree and see live updates");
        ui.label("6. Use Messages tab to see all messaging activity");

        ui.separator();
        ui.label(RichText::new("Connection Modes:").strong());
        ui.label("• Client Mode: Connect to Zenoh routers");
        ui.label("• Peer Mode: Participate as a peer in a mesh network (EARLY VERSION: requires multicast & open firewalls)");

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
