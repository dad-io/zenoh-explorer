//! The per-frame layout: header, connection panel, alert banner, toolbar and panels.

use eframe::egui;
use egui::{Margin, RichText};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use tracing::{error, info};

use crate::app::theme::icon_button;
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

/// How long an alert stays: Success 6 s, Warning 10 s; an Error stays until ✖.
pub(crate) fn alert_lifetime(alert: &UiAlert) -> Option<Duration> {
    match alert {
        UiAlert::Success(_) => Some(Duration::from_secs(6)),
        UiAlert::Warning(_) => Some(Duration::from_secs(10)),
        UiAlert::Error(_) => None,
    }
}

/// Time left before `alert` clears itself at `age` (zero once due); None for an Error.
pub(crate) fn alert_time_left(alert: &UiAlert, age: Duration) -> Option<Duration> {
    alert_lifetime(alert).map(|life| life.saturating_sub(age))
}

/// Why Connect is disabled, written beside it; None when it is enabled.
fn connect_blocked_reason(
    mode: &str,
    address: &str,
    port: &str,
    listen_port: &str,
) -> Option<&'static str> {
    if connect_port_error(address, port).is_some() {
        Some("Fix the Port field")
    } else if listen_port_error(mode, listen_port).is_some() {
        Some("Fix the Listen Port field")
    } else {
        None
    }
}

/// Why Disconnect is disabled: only while a connect attempt is running.
fn disconnect_blocked_reason(status: &ConnectionStatus) -> Option<&'static str> {
    match status {
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
            Some("Available once connected")
        }
        _ => None,
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
        self.frame_ui(ctx);
    }
}

impl ZenohExplorer {
    /// One frame of the whole window. Tests call this directly: eframe 0.29's
    /// `Frame` cannot be constructed outside eframe.
    pub(crate) fn frame_ui(&mut self, ctx: &egui::Context) {
        // Process any pending events from the Zenoh worker
        self.process_events();
        // Success and Warning alerts leave on their own; wake the UI for it
        self.expire_alert(ctx, Instant::now());
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
                            .add(icon_button(if self.dark_mode { "☀" } else { "🌙" }))
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

                        let error_text = match &self.connection_status {
                            ConnectionStatus::Error(err) => Some(format!("Error: {}", err)),
                            _ => None,
                        };
                        if let Some(error_text) = error_text {
                            ui.colored_label(ExplorerColors::ERROR, error_text);
                            self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);
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

                        let blocked = connect_blocked_reason(
                            &self.connection_mode,
                            &self.connect_address,
                            &self.connect_port,
                            &self.listen_port,
                        );
                        let clicked = ui
                            .horizontal(|ui| {
                                let clicked = ui
                                    .add_enabled(blocked.is_none(), egui::Button::new("Connect"))
                                    .clicked();
                                if let Some(reason) = blocked {
                                    ui.label(
                                        RichText::new(reason)
                                            .size(TEXT_SMALL_SIZE)
                                            .color(self.text_secondary_color()),
                                    );
                                }
                                clicked
                            })
                            .inner;
                        if clicked {
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
                        if let Some(reason) = disconnect_blocked_reason(&self.connection_status) {
                            ui.label(
                                RichText::new(reason)
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        }
                    });
                }

                ui.separator();

                // Global alert banner (export errors, warnings) — visible on every tab
                self.show_alert_banner(ui);

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

    /// Shows `alert` with a fresh timer, even when the same alert is showing.
    pub(crate) fn raise_alert(&mut self, alert: UiAlert) {
        self.ui_alert = Some(alert);
        // expire_alert stamps it this frame; comparing values alone would
        // keep the old stamp for an identical alert
        self.ui_alert_since = None;
    }

    /// Remembers when the current alert appeared (a changed value also counts,
    /// for any path that sets `ui_alert` directly), clears a Success or Warning
    /// whose time is up, and otherwise asks egui to wake the UI when it will
    /// be, so the banner leaves without input (P1 repaints on events only).
    fn expire_alert(&mut self, ctx: &egui::Context, now: Instant) {
        let seen = self.ui_alert_since.as_ref().map(|(a, _)| a);
        if self.ui_alert.as_ref() != seen {
            self.ui_alert_since = self.ui_alert.clone().map(|a| (a, now));
        }
        let (Some(alert), Some((_, since))) = (&self.ui_alert, &self.ui_alert_since) else {
            return;
        };
        let left = alert_time_left(alert, now.saturating_duration_since(*since));
        match left {
            Some(left) if left.is_zero() => {
                self.ui_alert = None;
                self.ui_alert_since = None;
            }
            Some(left) => ctx.request_repaint_after(left),
            None => {}
        }
    }

    /// The banner for `ui_alert`; ✖ dismisses it (the only way for an Error).
    fn show_alert_banner(&mut self, ui: &mut egui::Ui) {
        let Some(alert) = self.ui_alert.clone() else {
            return;
        };
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
                    UiAlert::Error(_) => {
                        (format!("Error: {}", alert.text()), ExplorerColors::ERROR)
                    }
                };
                ui.label(RichText::new(text).color(color));
                if ui.add(icon_button("✖")).on_hover_text("Dismiss").clicked() {
                    self.ui_alert = None;
                }
            });
        });
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

    use crate::app::headless::{click_events, node, nodes, repaint_delay, text, Headless, WIDE};
    use crate::app::theme::MIN_TARGET;
    use crate::ui::help::section;

    fn frame(h: &Headless, app: &mut ZenohExplorer) -> egui::FullOutput {
        h.run(Vec::new(), |ctx| app.frame_ui(ctx))
    }

    fn ago(d: Duration) -> Instant {
        Instant::now().checked_sub(d).expect(
            "test ages must stay under a minute: on Windows, Instant counts from boot and CI machines may be freshly booted",
        )
    }

    #[test]
    fn alert_expiry_rules() {
        let s = Duration::from_secs;
        let left = |a: UiAlert, age| alert_time_left(&a, s(age));
        assert_eq!(left(UiAlert::Success("x".into()), 5), Some(s(1)));
        assert_eq!(left(UiAlert::Success("x".into()), 6), Some(Duration::ZERO));
        assert_eq!(left(UiAlert::Warning("x".into()), 9), Some(s(1)));
        assert_eq!(left(UiAlert::Warning("x".into()), 10), Some(Duration::ZERO));
        assert_eq!(
            left(UiAlert::Error("x".into()), 3600),
            None,
            "errors stay until ✖"
        );
    }

    #[test]
    fn expiry_wakes_the_ui_when_due() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let ctx = egui::Context::default();
        let _ = ctx.run(egui::RawInput::default(), |_| {}); // settle egui's start-up repaint
        let now = Instant::now();
        let ok = UiAlert::Success("ok".into());
        app.ui_alert = Some(ok.clone());
        app.ui_alert_since = Some((ok, now - Duration::from_millis(5_500)));
        let out = ctx.run(egui::RawInput::default(), |ctx| app.expire_alert(ctx, now));
        let d = repaint_delay(&out);
        assert!(
            d <= Duration::from_millis(500) && d >= Duration::from_millis(450),
            "woken when the 6 s are up, not at the 1 s idle tick: {d:?}"
        );
        app.ui_alert = Some(UiAlert::Error("e".into()));
        let out = ctx.run(egui::RawInput::default(), |ctx| app.expire_alert(ctx, now));
        assert!(
            repaint_delay(&out) > Duration::from_secs(3600),
            "an Error schedules nothing"
        );
    }

    #[test]
    fn re_raising_the_same_alert_restarts_its_timer() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let ctx = egui::Context::default();
        let ms = Duration::from_millis;
        let t0 = ago(Duration::from_secs(20));
        let saved = UiAlert::Success("Saved to /tmp/x".into());
        let expire_at = |app: &mut ZenohExplorer, t: Instant| {
            let _ = ctx.run(egui::RawInput::default(), |ctx| app.expire_alert(ctx, t));
        };
        app.raise_alert(saved.clone());
        expire_at(&mut app, t0);
        expire_at(&mut app, t0 + ms(5_900));
        assert_eq!(app.ui_alert, Some(saved.clone()), "still up at 5.9 s");
        // The same topic saved to the same path again
        app.raise_alert(saved.clone());
        expire_at(&mut app, t0 + ms(5_900));
        expire_at(&mut app, t0 + ms(6_500));
        assert_eq!(
            app.ui_alert,
            Some(saved.clone()),
            "the second Saved alert gets its own 6 s"
        );
        expire_at(&mut app, t0 + ms(5_900 + 6_000));
        assert!(app.ui_alert.is_none(), "gone 6 s after the re-raise");
    }

    #[test]
    fn alerts_clear_themselves_in_the_frame() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let saved = UiAlert::Success("Saved to /tmp/x".into());
        app.ui_alert = Some(saved.clone());
        let out = frame(&h, &mut app);
        assert!(text(&out, "Saved to /tmp/x").is_some());
        app.ui_alert_since = Some((saved.clone(), ago(Duration::from_secs(6))));
        let out = frame(&h, &mut app);
        assert!(app.ui_alert.is_none(), "a Success leaves after 6 s");
        assert!(text(&out, "Saved to /tmp/x").is_none());

        let warn = UiAlert::Warning("w".into());
        app.ui_alert = Some(warn.clone());
        app.ui_alert_since = Some((warn.clone(), ago(Duration::from_secs(9))));
        let _ = frame(&h, &mut app);
        assert!(app.ui_alert.is_some(), "a Warning stays 10 s");
        app.ui_alert_since = Some((warn.clone(), ago(Duration::from_secs(10))));
        let _ = frame(&h, &mut app);
        assert!(app.ui_alert.is_none(), "a Warning leaves after 10 s");

        // A new alert replacing an old one restarts the clock.
        app.ui_alert = Some(UiAlert::Success("new".into()));
        app.ui_alert_since = Some((saved, ago(Duration::from_secs(30))));
        let out = frame(&h, &mut app);
        assert!(
            text(&out, "new").is_some(),
            "the old timestamp does not apply"
        );

        let err = UiAlert::Error("e".into());
        app.ui_alert = Some(err.clone());
        app.ui_alert_since = Some((err, ago(Duration::from_secs(30))));
        let out = frame(&h, &mut app);
        assert!(text(&out, "Error: e").is_some(), "an Error stays until ✖");
    }

    #[test]
    fn alert_dismiss_is_24_square_and_clears() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        app.ui_alert = Some(UiAlert::Error("x".into()));
        let _ = frame(&h, &mut app);
        let out = frame(&h, &mut app);
        let row = text(&out, "Error: x").expect("banner").rect;
        let dismiss = nodes(&out)
            .into_iter()
            .find(|n| {
                n.name == "✖" && n.rect.min.y <= row.center().y && row.center().y <= n.rect.max.y
            })
            .expect("✖ beside the banner text");
        assert!(
            dismiss.rect.width() >= MIN_TARGET && dismiss.rect.height() >= MIN_TARGET,
            "{:?}",
            dismiss.rect
        );
        let (press, release) = click_events(dismiss.rect.center());
        let _ = h.run(press, |ctx| app.frame_ui(ctx));
        let _ = h.run(release, |ctx| app.frame_ui(ctx));
        assert!(app.ui_alert.is_none());
    }

    #[test]
    fn header_tabs_banner_and_connect_are_at_least_24() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let _ = frame(&h, &mut app);
        let out = frame(&h, &mut app);
        let n = |name: &str| node(&out, name).unwrap_or_else(|| panic!("{name} is on screen"));
        let theme = n("☀"); // dark mode is the default (src/app/mod.rs:305)
        assert!(
            theme.rect.width() >= MIN_TARGET && theme.rect.height() >= MIN_TARGET,
            "theme toggle {:?}",
            theme.rect
        );
        for name in ["📊 Topics", "📤 Publish", "🔍 Query", "❓ Help", "Connect"] {
            assert!(
                n(name).rect.height() >= MIN_TARGET,
                "{name} {:?}",
                n(name).rect
            );
        }
        app.connection_status = ConnectionStatus::Connected;
        let out = frame(&h, &mut app);
        let d = node(&out, "Disconnect").expect("Disconnect");
        assert!(d.rect.height() >= MIN_TARGET, "{:?}", d.rect);
    }

    #[test]
    fn connect_blocked_reason_rules() {
        assert_eq!(
            connect_blocked_reason("client", "localhost", "7447", "7448"),
            None
        );
        assert_eq!(
            connect_blocked_reason("client", "localhost", "abc", "7448"),
            Some("Fix the Port field")
        );
        assert_eq!(
            connect_blocked_reason("client", "", "abc", "7448"),
            None,
            "Port is unused without an address"
        );
        assert_eq!(
            connect_blocked_reason("peer", "", "7447", "80"),
            Some("Fix the Listen Port field")
        );
        assert_eq!(
            disconnect_blocked_reason(&ConnectionStatus::ConnectingMonitor),
            Some("Available once connected")
        );
        assert_eq!(
            disconnect_blocked_reason(&ConnectionStatus::Connected),
            None
        );
    }

    #[test]
    fn connect_reasons_are_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        app.connect_address = "localhost".into();
        app.connect_port = "abc".into();
        let out = frame(&h, &mut app);
        let reason = text(&out, "Fix the Port field").expect("reason beside Connect");
        let connect = node(&out, "Connect").expect("Connect");
        assert!(
            reason.rect.left() >= connect.rect.right(),
            "right of the button"
        );
        assert!((reason.rect.center().y - connect.rect.center().y).abs() < MIN_TARGET / 2.0);
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        let out = frame(&h, &mut app);
        assert!(text(&out, "Available once connected").is_some());
    }

    #[test]
    fn connection_error_links_troubleshooting() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        // 700 pt tall: the Help view's Troubleshooting heading starts below the fold
        let h = Headless::new(egui::vec2(WIDE.x, 700.0));
        app.connection_status = ConnectionStatus::Error("boom".into());
        app.detail_view = DetailView::Help;
        let out = frame(&h, &mut app);
        assert!(
            text(&out, section::TROUBLESHOOTING).is_none(),
            "precondition: Troubleshooting is below the fold"
        );
        app.detail_view = DetailView::TopicDetails;
        let out = frame(&h, &mut app);
        // after the merge the frame has other "More in Help" links (empty
        // tree, Limits row) and `nodes` comes from a hash map, so pick the
        // one nearest below the error text
        let err = text(&out, "Error: boom").expect("error text").rect;
        let link = nodes(&out)
            .into_iter()
            .filter(|n| n.name == "More in Help" && n.rect.top() >= err.bottom() - 1.0)
            .min_by(|a, b| a.rect.top().total_cmp(&b.rect.top()))
            .expect("link under the error");
        assert!(
            link.rect.top() - err.bottom() < MIN_TARGET,
            "directly under the error"
        );
        assert!(
            (link.rect.left() - err.left()).abs() < MIN_TARGET,
            "in the connection panel, not another view"
        );
        let (press, release) = click_events(link.rect.center());
        let _ = h.run(press, |ctx| app.frame_ui(ctx));
        let _ = h.run(release, |ctx| app.frame_ui(ctx));
        assert_eq!(app.detail_view, DetailView::Help);
        // The link is drawn before the Help view, so Help scrolls to the target
        // and clears `help_target` in the click frame: check the scroll instead.
        assert_eq!(app.help_target, None, "consumed by the Help view");
        let out = frame(&h, &mut app);
        let heading = text(&out, section::TROUBLESHOOTING).expect("its own section in view");
        assert!(heading.rect.max.y <= 700.0, "{:?}", heading.rect);
    }
}
