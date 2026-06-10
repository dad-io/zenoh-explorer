//! Application struct, construction, theme helpers, and eframe::App implementation.

use eframe::egui;
use egui::{Color32, Margin, RichText};
use std::collections::{HashMap, VecDeque};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};
use tracing::{error, info};

use crate::colors::ExplorerColors;
use crate::types::*;
use crate::ui::topic_tree::TopicTreeUI;
use crate::zenoh_worker;

/// Main application state, contains all UI state, configuration, and communication channels.
pub struct ZenohExplorer {
    pub(crate) detail_view: DetailView,
    pub(crate) connection_status: ConnectionStatus,
    pub(crate) discovered_peers: usize,
    pub(crate) discovered_routers: usize,
    pub(crate) selected_topic: Option<String>,
    pub(crate) connect_transport: String,
    pub(crate) connect_address: String,
    pub(crate) connect_port: String,
    pub(crate) listen_port: String,
    pub(crate) connection_mode: String,
    pub(crate) config_json: String,
    pub(crate) subscribe_key: String,
    pub(crate) subscribe_reliability: String,
    pub(crate) subscribe_mode: String,
    pub(crate) publish_key: String,
    pub(crate) publish_payload: String,
    pub(crate) publish_payload_bytes: Option<Vec<u8>>,
    pub(crate) publish_payload_filename: Option<String>,
    pub(crate) publish_payload_expanded: bool,
    pub(crate) import_memory_bytes: usize,
    pub(crate) publish_encoding: String,
    pub(crate) query_selector: String,
    pub(crate) query_value: String,
    pub(crate) query_timeout: String,
    pub(crate) messages: VecDeque<ZenohMessage>,
    pub(crate) subscriptions: Vec<Subscription>,
    pub(crate) browse_tree: Arc<RwLock<ZenohNode>>,
    pub(crate) command_sender: Option<Sender<ZenohCommand>>,
    pub(crate) tree_filter: String,
    pub(crate) event_receiver: Option<Receiver<ZenohEvent>>,
    pub(crate) dark_mode: bool,
    pub(crate) max_messages: usize,
    pub(crate) max_memory_mb: usize,
    pub(crate) current_memory_bytes: usize,
    pub(crate) message_filter: String,
    pub(crate) auto_scroll: bool,
    pub(crate) query_alert: Option<String>,
    pub(crate) ui_alert: Option<String>,
    pub(crate) messages_dropped: usize,
    pub(crate) rate_limiter: RateLimiter,
    pub(crate) rate_limit_drops: usize,
    pub(crate) memory_warning_shown: bool,
    pub(crate) last_health_check: Instant,
    pub(crate) worker_healthy: bool,
    pub(crate) deduper: Deduper,
    pub(crate) messages_deduped: usize,
    #[allow(dead_code)]
    pub(crate) local_kvstore: Arc<RwLock<HashMap<String, (String, String)>>>,
    pub(crate) queryable_enabled: bool,
    pub(crate) queryable_pattern: String,
    pub(crate) paused_keys: std::collections::HashSet<String>,
    pub(crate) json_parse_cache: std::collections::HashMap<u64, Option<String>>,
    pub(crate) expanded_payloads: std::collections::HashSet<String>,
    pub(crate) payload_store: Arc<RwLock<PayloadStoreMap>>,
}

impl Default for ZenohExplorer {
    fn default() -> Self {
        Self::new()
    }
}

impl ZenohExplorer {
    /// Creates a new instance of the Zenoh Explorer application.
    /// Sets up communication channels and spawns the Zenoh worker thread.
    pub fn new() -> Self {
        // Create channels for worker/buffer/ui
        let (command_sender, command_receiver) = mpsc::channel();
        let (worker_event_sender, buffer_receiver) = mpsc::channel();
        let (ui_sender, event_receiver) = mpsc::channel();

        // Create shared key-value store for queryable
        let local_kvstore = Arc::new(RwLock::new(HashMap::new()));
        let kvstore_clone = local_kvstore.clone();

        // Start message buffer thread
        std::thread::spawn(move || {
            zenoh_worker::message_buffer_thread(buffer_receiver, ui_sender);
        });

        // Start the Zenoh worker in a separate async task
        std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                zenoh_worker::zenoh_worker(command_receiver, worker_event_sender, kvstore_clone)
                    .await;
            });
        });

        info!("ZenohExplorer initialized with worker and buffer threads");

        Self {
            detail_view: DetailView::TopicDetails,
            connection_status: ConnectionStatus::Disconnected,
            discovered_peers: 0,
            discovered_routers: 0,
            selected_topic: None,
            connect_transport: "tcp".to_string(),
            connect_address: "".to_string(),
            connect_port: "7447".to_string(),
            listen_port: "7447".to_string(),
            connection_mode: "peer".to_string(),
            config_json: "{}".to_string(),
            subscribe_key: "demo/**".to_string(),
            subscribe_reliability: "reliable".to_string(),
            subscribe_mode: "push".to_string(),
            publish_key: "demo/test".to_string(),
            publish_payload: "Hello Zenoh!".to_string(),
            publish_payload_bytes: None,
            publish_payload_filename: None,
            publish_payload_expanded: false,
            import_memory_bytes: 0,
            publish_encoding: "text/plain".to_string(),
            query_selector: "demo/**".to_string(),
            query_value: "".to_string(),
            query_timeout: "10000".to_string(),
            messages: VecDeque::new(),
            subscriptions: Vec::new(),
            browse_tree: Arc::new(RwLock::new(ZenohNode::new("root".to_string()))),
            command_sender: Some(command_sender),
            tree_filter: String::new(),
            event_receiver: Some(event_receiver),
            dark_mode: true,
            max_messages: 1000000,
            max_memory_mb: 100,
            current_memory_bytes: 0,
            message_filter: String::new(),
            auto_scroll: true,
            query_alert: None,
            ui_alert: None,
            messages_dropped: 0,
            rate_limiter: RateLimiter::new(1000),
            rate_limit_drops: 0,
            memory_warning_shown: false,
            last_health_check: Instant::now(),
            worker_healthy: true,
            deduper: Deduper::new(Duration::from_secs(60)),
            messages_deduped: 0,
            local_kvstore: Arc::new(RwLock::new(HashMap::new())),
            queryable_enabled: false,
            queryable_pattern: "**".to_string(),
            paused_keys: std::collections::HashSet::new(),
            json_parse_cache: std::collections::HashMap::new(),
            expanded_payloads: std::collections::HashSet::new(),
            payload_store: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    pub(crate) fn background_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_BACKGROUND
        } else {
            ExplorerColors::BACKGROUND
        }
    }

    /// Returns the appropriate button style for the current theme
    pub(crate) fn apply_theme(&self, ctx: &egui::Context) {
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
                style.visuals.widgets.noninteractive.fg_stroke.color =
                    ExplorerColors::DARK_TEXT_PRIMARY;

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

    #[allow(dead_code)]
    pub(crate) fn card_background_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_CARD_BACKGROUND
        } else {
            ExplorerColors::CARD_BACKGROUND
        }
    }

    pub(crate) fn text_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_PRIMARY
        } else {
            ExplorerColors::TEXT_PRIMARY
        }
    }

    pub(crate) fn text_secondary_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_SECONDARY
        } else {
            ExplorerColors::TEXT_SECONDARY
        }
    }

    pub(crate) fn text_tertiary_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_TERTIARY
        } else {
            ExplorerColors::TEXT_TERTIARY
        }
    }

    /// Create smooth fade animation for UI elements
    pub(crate) fn animate_fade_in(&self, ctx: &egui::Context, id: &str, target: f32) -> f32 {
        ctx.animate_value_with_time(egui::Id::new(id), target, 0.001)
    }

    /// Create pulsing animation for warning indicators
    pub(crate) fn animate_pulse(&self, ctx: &egui::Context, _id: &str) -> f32 {
        let time = ctx.input(|i| i.time) as f32;
        0.85 + (time * 3.0).sin() * 0.15
    }
}

/// Implementation of the eframe App trait for the main application.
/// This is called on each frame to update the UI.
impl eframe::App for ZenohExplorer {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // First frame debug message and ensure window is visible
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            info!("First UI update frame - window should be visible now");
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        });

        // Process any pending events from the Zenoh worker
        self.process_events();

        // Apply theme styling
        self.apply_theme(ctx);

        // Render the main UI panel
        egui::CentralPanel::default()
            .frame(
                egui::Frame::default()
                    .fill(self.background_color())
                    .inner_margin(Margin::same(8.0)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new("Zenoh Explorer")
                            .size(HEADING_LARGE_SIZE)
                            .color(self.text_color()),
                    );

                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        // Dark mode toggle
                        if ui
                            .button(if self.dark_mode { "☀" } else { "🌙" })
                            .clicked()
                        {
                            self.dark_mode = !self.dark_mode;
                        }

                        ui.separator();

                        // Worker health indicator with pulsing animation
                        if !self.worker_healthy {
                            let pulse = self.animate_pulse(ui.ctx(), "worker_health_pulse");
                            let error_color = ExplorerColors::ERROR;
                            let pulsing_color = egui::Color32::from_rgba_unmultiplied(
                                error_color.r(),
                                error_color.g(),
                                error_color.b(),
                                (255.0 * pulse) as u8,
                            );
                            ui.label(
                                RichText::new("Worker Unresponsive")
                                    .color(pulsing_color)
                                    .size(TEXT_SMALL_SIZE),
                            );
                            ui.separator();
                            ui.ctx().request_repaint_after(
                                std::time::Duration::from_millis(66),
                            );
                        }

                        // Connection status with loading indicator
                        if matches!(
                            self.connection_status,
                            ConnectionStatus::ConnectingPublishing
                                | ConnectionStatus::ConnectingMonitor
                        ) {
                            ui.spinner();
                        }
                        ui.label(
                            RichText::new(format!("● {}", self.connection_status.text()))
                                .color(self.connection_status.color()),
                        );

                        // Show discovered peers/routers when connected
                        if matches!(self.connection_status, ConnectionStatus::Connected)
                            && (self.discovered_peers > 0 || self.discovered_routers > 0)
                        {
                            let mut parts = Vec::new();
                            if self.discovered_routers > 0 {
                                parts.push(format!("{}R", self.discovered_routers));
                            }
                            if self.discovered_peers > 0 {
                                parts.push(format!("{}P", self.discovered_peers));
                            }
                            ui.label(
                                RichText::new(format!("({})", parts.join(" ")))
                                    .color(self.text_tertiary_color())
                                    .size(TEXT_SMALL_SIZE),
                            );
                        }

                        // Memory usage indicator
                        let total_memory_bytes =
                            self.current_memory_bytes + self.import_memory_bytes;
                        if !self.messages.is_empty()
                            || self.messages_dropped > 0
                            || self.import_memory_bytes > 0
                        {
                            ui.separator();

                            let memory_mb = total_memory_bytes as f32 / (1024.0 * 1024.0);
                            let memory_percent =
                                (memory_mb / self.max_memory_mb as f32 * 100.0).min(100.0);

                            if memory_percent > 80.0 && !self.memory_warning_shown {
                                self.memory_warning_shown = true;
                                self.query_alert = Some("⚠ Memory usage is high (>80%). Messages may start being dropped soon.".to_string());
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
                                let import_mb =
                                    self.import_memory_bytes as f32 / (1024.0 * 1024.0);
                                format!(
                                    "Memory: {:.1}MB/{:.0}MB (+{:.1}MB import)",
                                    self.current_memory_bytes as f32 / (1024.0 * 1024.0),
                                    self.max_memory_mb as f32,
                                    import_mb
                                )
                            } else {
                                format!(
                                    "Memory: {:.1}MB/{:.0}MB",
                                    memory_mb, self.max_memory_mb as f32
                                )
                            };

                            ui.label(
                                RichText::new(memory_text)
                                    .color(memory_color)
                                    .size(TEXT_SMALL_SIZE),
                            );

                            if self.messages_dropped > 0 || self.rate_limit_drops > 0 {
                                let drop_text = if self.rate_limit_drops > 0 {
                                    format!(
                                        "({} dropped, {} rate limited)",
                                        self.messages_dropped, self.rate_limit_drops
                                    )
                                } else {
                                    format!("({} dropped)", self.messages_dropped)
                                };
                                ui.label(
                                    RichText::new(drop_text)
                                        .color(ExplorerColors::WARNING)
                                        .size(TEXT_SMALL_SIZE),
                                );
                            }
                        }
                    });
                });

                ui.separator();

                // Compact connection panel in toolbar
                if matches!(
                    self.connection_status,
                    ConnectionStatus::Disconnected | ConnectionStatus::Error(_)
                ) {
                    ui.group(|ui| {
                        ui.label("Connection Settings");
                        ui.horizontal(|ui| {
                            ui.label("Transport:");
                            egui::ComboBox::from_id_salt("connect_transport")
                                .width(60.0)
                                .selected_text(&self.connect_transport)
                                .show_ui(ui, |ui| {
                                    ui.selectable_value(
                                        &mut self.connect_transport,
                                        "tcp".to_string(),
                                        "tcp",
                                    );
                                    ui.selectable_value(
                                        &mut self.connect_transport,
                                        "udp".to_string(),
                                        "udp",
                                    );
                                    ui.selectable_value(
                                        &mut self.connect_transport,
                                        "quic".to_string(),
                                        "quic",
                                    );
                                    ui.selectable_value(
                                        &mut self.connect_transport,
                                        "ws".to_string(),
                                        "ws",
                                    );
                                    ui.selectable_value(
                                        &mut self.connect_transport,
                                        "tls".to_string(),
                                        "tls",
                                    );
                                });

                            ui.label("Address:");
                            ui.add(
                                egui::TextEdit::singleline(&mut self.connect_address)
                                    .desired_width(120.0),
                            );

                            ui.label("Port:");
                            ui.add(
                                egui::TextEdit::singleline(&mut self.connect_port)
                                    .desired_width(50.0),
                            );
                        });
                        ui.horizontal(|ui| {
                            let locator_preview = if self.connect_address.is_empty() {
                                "(multicast discovery)".to_string()
                            } else {
                                format!(
                                    "{}/{}:{}",
                                    self.connect_transport,
                                    self.connect_address,
                                    self.connect_port
                                )
                            };
                            ui.label(
                                RichText::new(format!("→ {}", locator_preview))
                                    .size(TEXT_SMALL_SIZE - 1.0)
                                    .italics()
                                    .color(self.text_tertiary_color()),
                            );
                        });
                        ui.horizontal(|ui| {
                            ui.label("Mode:");
                            egui::ComboBox::from_id_salt("connection_mode")
                                .selected_text(&self.connection_mode)
                                .show_ui(ui, |ui| {
                                    ui.selectable_value(
                                        &mut self.connection_mode,
                                        "client".to_string(),
                                        "Client",
                                    );
                                    ui.selectable_value(
                                        &mut self.connection_mode,
                                        "peer".to_string(),
                                        "Peer",
                                    );
                                });
                        });

                        if self.connection_mode == "peer" {
                            ui.horizontal(|ui| {
                                ui.label("Listen Port:");
                                ui.add(
                                    egui::TextEdit::singleline(&mut self.listen_port)
                                        .desired_width(60.0),
                                );
                            });
                        }

                        if self.connection_mode == "peer" {
                            ui.label(
                                RichText::new("Peer mode: Use different listen ports for each peer on same machine (e.g., 7447 and 7448)")
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        } else {
                            ui.label(
                                RichText::new(
                                    "Client mode: Connects to Zenoh router. Default: tcp/localhost:7447",
                                )
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                            );
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
                                    format!(
                                        "{}/{}:{}",
                                        self.connect_transport,
                                        self.connect_address,
                                        self.connect_port
                                    )
                                };

                                info!(
                                    "GUI sending Connect command - mode: {}, locators: {}, listen_port: {}",
                                    self.connection_mode, locators, self.listen_port
                                );
                                match sender.send(ZenohCommand::Connect {
                                    locators,
                                    listen_port: self.listen_port.clone(),
                                    mode: self.connection_mode.clone(),
                                    config_json: self.config_json.clone(),
                                }) {
                                    Ok(_) => info!("Connect command sent successfully"),
                                    Err(e) => {
                                        error!("Failed to send Connect command: {:?}", e)
                                    }
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

                ui.separator();

                // Global alert banner (export errors, warnings) — visible on every tab
                if let Some(alert_text) = self.ui_alert.clone() {
                    egui::TopBottomPanel::top("alert_banner").show_inside(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.label(
                                RichText::new(format!("⚠ {}", alert_text))
                                    .color(ExplorerColors::WARNING),
                            );
                            if ui.small_button("✖").clicked() {
                                self.ui_alert = None;
                            }
                        });
                    });
                }

                // Main split-panel layout
                egui::TopBottomPanel::top("toolbar").show_inside(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label("Quick Actions:");
                        if ui
                            .selectable_label(
                                self.detail_view == DetailView::TopicDetails,
                                "📊 Topics",
                            )
                            .clicked()
                        {
                            self.detail_view = DetailView::TopicDetails;
                        }
                        if ui
                            .selectable_label(
                                self.detail_view == DetailView::Publish,
                                "📤 Publish",
                            )
                            .clicked()
                        {
                            self.detail_view = DetailView::Publish;
                        }
                        if ui
                            .selectable_label(
                                self.detail_view == DetailView::Query,
                                "🔍 Query",
                            )
                            .clicked()
                        {
                            self.detail_view = DetailView::Query;
                        }
                        if ui
                            .selectable_label(
                                self.detail_view == DetailView::Help,
                                "❓ Help",
                            )
                            .clicked()
                        {
                            self.detail_view = DetailView::Help;
                        }
                    });
                });

                // Split panel layout
                egui::SidePanel::left("tree_panel")
                    .default_width(400.0)
                    .min_width(250.0)
                    .resizable(true)
                    .show_inside(ui, |ui| {
                        self.show_tree_panel(ui);
                    });

                // Right panel shows details based on selected view
                egui::CentralPanel::default().show_inside(ui, |ui| {
                    self.show_detail_panel(ui);
                });
            });

        // repaint for real-time message updates (throttled to ~15fps)
        ctx.request_repaint_after(std::time::Duration::from_millis(66));
    }
}
