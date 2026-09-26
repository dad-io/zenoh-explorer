//! The per-frame layout: header, connection panel, alert banner, toolbar and panels.

use eframe::egui;
use egui::{Margin, RichText};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tracing::{error, info};

use crate::app::{memory_readout, peers_text, MemLevel, UiAlert, ZenohExplorer, IDLE_REPAINT_SECS};
use crate::colors::ExplorerColors;
use crate::transfer;
use crate::types::*;
use crate::ui::topic_tree::TopicTreeUI;
use crate::validation;

/// Where egui keeps the `(mode, locators, listen_port)` of the last Connect.
const LAST_ATTEMPT_ID: &str = "last_connect_attempt";

/// The connection form's guidance for `mode`, true of the form as shown.
fn connect_hint(mode: &str, _address: &str) -> &'static str {
    if mode == "client" {
        "Client mode: enter the router's address (for example localhost) and its port (7447)."
    } else {
        "Peer mode: finds peers by multicast. Listen Port is where other peers reach this app. Use a different Listen Port for each copy on one machine. Address is optional."
    }
}

/// What Connect will dial, shown after `→`.
fn locator_preview(mode: &str, transport: &str, address: &str, port: &str) -> String {
    match (address.trim().is_empty(), mode) {
        (true, "client") => "needs an address".to_string(),
        (true, _) => "multicast discovery".to_string(),
        (false, _) => form_locators(transport, address, port),
    }
}

/// The locators Connect sends: empty (multicast discovery) without an address.
/// Inputs are trimmed: `" 7447"` passes the port check, so it must not reach the locator.
fn form_locators(transport: &str, address: &str, port: &str) -> String {
    let address = address.trim();
    if address.is_empty() {
        String::new()
    } else {
        format!("{}/{}:{}", transport.trim(), address, port.trim())
    }
}

/// The Port field's problem; Port is used only when an address is set.
fn connect_port_error(address: &str, port: &str) -> Option<String> {
    if address.trim().is_empty() {
        None
    } else {
        validation::port_error(port, 1..=65535)
    }
}

/// The Listen Port field's problem (peer mode only). After T10 the monitor opens
/// no port of its own, so there is no Listen Port + 1000 ceiling.
fn listen_port_error(mode: &str, listen_port: &str) -> Option<String> {
    if mode == "peer" {
        validation::port_error(listen_port, 1024..=65535)
    } else {
        None
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
        // The per-frame budget left events queued: come back right away
        if self.events_pending {
            ctx.request_repaint();
        }
        // Keep the connecting spinner and elapsed seconds moving
        if matches!(
            self.connection_status,
            ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor
        ) {
            ctx.request_repaint_after(std::time::Duration::from_millis(100));
        }

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
                            let secs_since_ping =
                                self.ping_sent_at.map_or(0, |t| t.elapsed().as_secs());
                            let not_answering =
                                format!("Worker not answering ({} s)", secs_since_ping);
                            let health_text = if self.worker_gone {
                                not_answering
                            } else if matches!(
                                self.publish_status,
                                Some(PublishStatus::Sending { .. })
                            ) {
                                "Worker busy: publishing".to_string()
                            } else if matches!(
                                self.connection_status,
                                ConnectionStatus::ConnectingPublishing
                                    | ConnectionStatus::ConnectingMonitor
                            ) {
                                "Worker busy: connecting".to_string()
                            } else {
                                not_answering
                            };
                            ui.label(
                                RichText::new(health_text)
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
                        let status = ui.label(
                            RichText::new(format!("● {}", self.header_status_text()))
                                .color(self.connection_status.color()),
                        );
                        let connected =
                            matches!(self.connection_status, ConnectionStatus::Connected);
                        if connected && !self.monitor_ok {
                            status.on_hover_text(
                                "The background ** monitor could not start, so only your subscriptions fill the tree",
                            );
                        }

                        // Peers and routers, shown at zero too
                        if connected {
                            ui.label(
                                RichText::new(peers_text(
                                    self.discovered_peers,
                                    self.discovered_routers,
                                ))
                                .color(self.text_tertiary_color())
                                .size(TEXT_SMALL_SIZE),
                            )
                            .on_hover_text(
                                "Zenoh peers and routers the publishing session is linked to",
                            );
                        }

                        // Stored payloads: summed at most once a second
                        if self.stored_bytes_cache.0.elapsed() >= Duration::from_secs(1) {
                            let stored = self
                                .payload_store
                                .read()
                                .map(|m| m.values().map(|e| e.bytes.len()).sum())
                                .unwrap_or(self.stored_bytes_cache.1);
                            self.stored_bytes_cache = (Instant::now(), stored);
                        }
                        let stored_bytes = self.stored_bytes_cache.1;

                        // Memory readout: history, stored payloads, staged import
                        if self.current_memory_bytes > 0
                            || stored_bytes > 0
                            || self.import_memory_bytes > 0
                        {
                            ui.separator();
                            self.update_memory_alert();
                            let (memory_text, level) = memory_readout(
                                self.current_memory_bytes,
                                self.max_memory_mb,
                                stored_bytes,
                            );
                            let memory_color = match level {
                                MemLevel::Critical => ExplorerColors::ERROR,
                                MemLevel::High => ExplorerColors::WARNING,
                                MemLevel::Ok => ExplorerColors::SUCCESS,
                            };
                            ui.label(
                                RichText::new(memory_text)
                                    .color(memory_color)
                                    .size(TEXT_SMALL_SIZE),
                            );
                            if let Some(alert) = &self.memory_alert {
                                ui.label(
                                    RichText::new(alert)
                                        .color(memory_color)
                                        .size(TEXT_SMALL_SIZE),
                                );
                            }
                            if self.import_memory_bytes > 0 {
                                ui.label(
                                    RichText::new(format!(
                                        "Staged import {}",
                                        transfer::format_size(self.import_memory_bytes)
                                    ))
                                    .color(self.text_tertiary_color())
                                    .size(TEXT_SMALL_SIZE),
                                );
                            }
                        } else {
                            self.memory_alert = None;
                        }

                        let sample_drops = self.sample_drops.load(Ordering::Relaxed);
                        if self.messages_dropped > 0
                            || self.rate_limit_drops > 0
                            || sample_drops > 0
                        {
                            let drop_text = format!(
                                "({} trimmed from list, {} not listed (rate), {} pipeline)",
                                self.messages_dropped, self.rate_limit_drops, sample_drops
                            );
                            ui.label(
                                RichText::new(drop_text)
                                    .color(ExplorerColors::WARNING)
                                    .size(TEXT_SMALL_SIZE),
                            );
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
                            if let Some(err) =
                                connect_port_error(&self.connect_address, &self.connect_port)
                            {
                                ui.colored_label(ExplorerColors::ERROR, err);
                            }
                        });
                        ui.horizontal(|ui| {
                            let preview = locator_preview(
                                &self.connection_mode,
                                &self.connect_transport,
                                &self.connect_address,
                                &self.connect_port,
                            );
                            ui.label(
                                RichText::new(format!("→ {}", preview))
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
                                if let Some(err) =
                                    listen_port_error(&self.connection_mode, &self.listen_port)
                                {
                                    ui.colored_label(ExplorerColors::ERROR, err);
                                }
                            });
                        }

                        ui.label(
                            RichText::new(connect_hint(
                                &self.connection_mode,
                                &self.connect_address,
                            ))
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_secondary_color()),
                        );

                        // An error belongs to the inputs it was tried with: once they
                        // change, it no longer describes this form.
                        let attempt_id = egui::Id::new(LAST_ATTEMPT_ID);
                        if matches!(self.connection_status, ConnectionStatus::Error(_)) {
                            let last = ui.ctx().data(|d| {
                                d.get_temp::<(String, String, String)>(attempt_id)
                            });
                            let current = (
                                self.connection_mode.clone(),
                                form_locators(
                                    &self.connect_transport,
                                    &self.connect_address,
                                    &self.connect_port,
                                ),
                                self.listen_port.clone(),
                            );
                            if last.is_some_and(|l| l != current) {
                                self.connection_status = ConnectionStatus::Disconnected;
                            }
                        }

                        if let ConnectionStatus::Error(ref err) = self.connection_status {
                            ui.colored_label(ExplorerColors::ERROR, format!("Error: {}", err));
                        }

                        let kept = self.subscriptions.len();
                        if kept > 0 {
                            let note = if kept == 1 {
                                "1 subscription resumes when you reconnect".to_string()
                            } else {
                                format!("{kept} subscriptions resume when you reconnect")
                            };
                            ui.label(
                                RichText::new(note)
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        }

                        let ports_ok =
                            connect_port_error(&self.connect_address, &self.connect_port)
                                .is_none()
                                && listen_port_error(&self.connection_mode, &self.listen_port)
                                    .is_none();
                        if ui
                            .add_enabled(ports_ok, egui::Button::new("Connect"))
                            .clicked()
                        {
                            if let Some(sender) = &self.command_sender {
                                let locators = form_locators(
                                    &self.connect_transport,
                                    &self.connect_address,
                                    &self.connect_port,
                                );
                                ui.ctx().data_mut(|d| {
                                    d.insert_temp(
                                        attempt_id,
                                        (
                                            self.connection_mode.clone(),
                                            locators.clone(),
                                            self.listen_port.clone(),
                                        ),
                                    )
                                });
                                self.connect_started = Some(Instant::now());
                                self.connect_target = if locators.is_empty() {
                                    "multicast discovery".to_string()
                                } else {
                                    locators.clone()
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
                                    Ok(_) => {
                                        self.connection_status =
                                            ConnectionStatus::ConnectingPublishing;
                                        info!("Connect command sent successfully")
                                    }
                                    Err(e) => {
                                        error!("Failed to send Connect command: {:?}", e);
                                        self.connection_status = ConnectionStatus::Error(
                                            format!("worker not running: {e}"),
                                        );
                                    }
                                }
                            }
                        }
                    });
                } else {
                    ui.horizontal(|ui| {
                        if ui
                            .add_enabled(
                                matches!(self.connection_status, ConnectionStatus::Connected),
                                egui::Button::new("Disconnect"),
                            )
                            .clicked()
                        {
                            self.connection_status = ConnectionStatus::Disconnected;
                            if let Some(sender) = &self.command_sender {
                                let _ = sender.send(ZenohCommand::Disconnect);
                            }
                        }
                    });
                }

                ui.separator();

                // Global alert banner (export errors, warnings) — visible on every tab
                if let Some(alert) = self.ui_alert.clone() {
                    egui::TopBottomPanel::top("alert_banner").show_inside(ui, |ui| {
                        ui.horizontal(|ui| {
                            let (text, color) = match &alert {
                                UiAlert::Success(_) => (
                                    alert.text().to_string(),
                                    if self.dark_mode {
                                        ExplorerColors::DARK_SUCCESS
                                    } else {
                                        ExplorerColors::SUCCESS
                                    },
                                ),
                                UiAlert::Warning(_) => (
                                    format!("Warning: {}", alert.text()),
                                    ExplorerColors::WARNING,
                                ),
                                UiAlert::Error(_) => (
                                    format!("Error: {}", alert.text()),
                                    ExplorerColors::ERROR,
                                ),
                            };
                            ui.label(RichText::new(text).color(color));
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

        // Worker events wake the UI through the buffer thread; this slow tick
        // only keeps time-based readouts (health, elapsed times) current
        ctx.request_repaint_after(std::time::Duration::from_secs(IDLE_REPAINT_SECS));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connection_hints_match_the_form() {
        assert!(connect_hint("client", "").contains("enter the router's address"));
        assert!(!connect_hint("client", "").contains("Default: tcp/localhost:7447"));
        assert!(connect_hint("peer", "").contains("different Listen Port for each copy"));
        assert!(
            !connect_hint("peer", "").contains("+ 1000"),
            "T10's client-mode monitor opens no second port"
        );
        assert_eq!(
            locator_preview("client", "tcp", "", "7447"),
            "needs an address"
        );
        assert_eq!(
            locator_preview("peer", "tcp", "", "7447"),
            "multicast discovery"
        );
    }

    #[test]
    fn form_locators_trim_inputs() {
        assert_eq!(
            form_locators("tcp", " localhost ", " 7447"),
            "tcp/localhost:7447"
        );
        assert_eq!(form_locators("tcp", "  ", "7447"), "");
        assert_eq!(
            locator_preview("client", "tcp", " ", "7447"),
            "needs an address"
        );
        assert_eq!(
            connect_port_error(" ", "abc"),
            None,
            "Port is unused without an address"
        );
    }
}
