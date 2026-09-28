//! Application struct, construction, theme helpers, and eframe::App implementation.

#[cfg(test)]
pub(crate) mod headless;
mod layout;
pub(crate) mod theme;

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::AtomicUsize;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, RwLock};
use std::time::Instant;
use tracing::info;

use crate::types::*;
use crate::worker;

/// The idle repaint tick, in seconds: it keeps time-based readouts current.
pub(crate) const IDLE_REPAINT_SECS: u64 = 1;

/// A message for the global alert banner, typed by its severity.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum UiAlert {
    Success(String),
    #[allow(dead_code)] // no P1 producer; P3-P5 raise warnings
    Warning(String),
    Error(String),
}

impl UiAlert {
    /// The alert's text, without any severity prefix.
    pub fn text(&self) -> &str {
        match self {
            UiAlert::Success(t) | UiAlert::Warning(t) | UiAlert::Error(t) => t,
        }
    }
}

/// How full the message history is, by one threshold set that drives both the
/// readout's words and the memory warning.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum MemLevel {
    Ok,
    High,
    Critical,
}

/// The history percentage at which the level becomes `High`.
pub(crate) const HIGH_PCT: f64 = 70.0;
/// The history percentage at which the level becomes `Critical`.
pub(crate) const CRITICAL_PCT: f64 = 90.0;

const MIB: f64 = 1024.0 * 1024.0;

/// The history's share of its limit, in percent, capped at 100.
fn history_percent(list_bytes: usize, limit_mb: usize) -> f64 {
    let limit = limit_mb as f64 * MIB;
    if limit <= 0.0 {
        return 100.0;
    }
    (list_bytes as f64 / limit * 100.0).min(100.0)
}

impl MemLevel {
    fn of_percent(pct: f64) -> Self {
        if pct >= CRITICAL_PCT {
            MemLevel::Critical
        } else if pct >= HIGH_PCT {
            MemLevel::High
        } else {
            MemLevel::Ok
        }
    }
}

/// The header's memory readout: the message history against its limit, then the
/// stored payloads. A staged import is part of neither figure.
pub(crate) fn memory_readout(
    list_bytes: usize,
    limit_mb: usize,
    stored_bytes: usize,
) -> (String, MemLevel) {
    let level = MemLevel::of_percent(history_percent(list_bytes, limit_mb));
    let suffix = match level {
        MemLevel::Ok => "",
        MemLevel::High => " (high)",
        MemLevel::Critical => " (critical)",
    };
    let mut text = format!(
        "History {:.1} MB / {} MB{}",
        list_bytes as f64 / MIB,
        limit_mb,
        suffix
    );
    if stored_bytes > 0 {
        text.push_str(&format!(
            " · Stored payloads {}",
            crate::transfer::format_size(stored_bytes)
        ));
    }
    (text, level)
}

/// The peers and routers the publishing session is linked to, in words.
pub(crate) fn peers_text(peers: usize, routers: usize) -> String {
    fn count(n: usize, one: &str, many: &str) -> String {
        if n == 1 {
            format!("1 {one}")
        } else {
            format!("{n} {many}")
        }
    }
    match (routers, peers) {
        (0, 0) => "no peers".to_string(),
        (0, p) => count(p, "peer", "peers"),
        (r, 0) => count(r, "router", "routers"),
        (r, p) => format!(
            "{} · {}",
            count(r, "router", "routers"),
            count(p, "peer", "peers")
        ),
    }
}

/// The header's short form of a connection error: its first clause.
pub(crate) fn header_error_clause(e: &str) -> String {
    let cut = [": ", ". ", " ("]
        .iter()
        .filter_map(|s| e.find(s))
        .min()
        .unwrap_or(e.len());
    let clause = e[..cut].trim_end_matches('.');
    if clause.chars().count() <= 40 {
        return clause.to_string();
    }
    let head: String = clause.chars().take(40).collect();
    match head.rfind(' ') {
        Some(i) => format!("{}…", &head[..i]),
        None => format!("{head}…"),
    }
}

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
    pub(crate) ui_alert: Option<UiAlert>,
    pub(crate) messages_dropped: usize,
    pub(crate) rate_limiter: RateLimiter,
    pub(crate) rate_limit_drops: usize,
    /// The history-size warning shown beside the memory readout.
    pub(crate) memory_alert: Option<String>,
    /// False when the background `**` monitor failed to start.
    pub(crate) monitor_ok: bool,
    /// Bytes held in `payload_store`, and when they were last summed.
    pub(crate) stored_bytes_cache: (Instant, usize),
    pub(crate) last_health_check: Instant,
    pub(crate) worker_healthy: bool,
    /// When the unanswered Ping was sent; None once a Pong arrives.
    pub(crate) ping_sent_at: Option<Instant>,
    /// When `health_tick` last ran, to tell a UI stall from a silent worker.
    pub(crate) last_tick_at: Instant,
    /// The command channel is closed: the worker thread has exited.
    pub(crate) worker_gone: bool,
    pub(crate) deduper: Deduper,
    pub(crate) messages_deduped: usize,
    /// Samples the worker → buffer → UI pipeline could not accept.
    pub(crate) sample_drops: Arc<AtomicUsize>,
    /// The same store the worker's queryable serves from.
    pub(crate) local_kvstore: Arc<RwLock<LocalKvStore>>,
    pub(crate) queryable_enabled: bool,
    pub(crate) queryable_pattern: String,
    pub(crate) paused_keys: std::collections::HashSet<String>,
    pub(crate) json_parse_cache: std::collections::HashMap<u64, Option<String>>,
    pub(crate) expanded_payloads: std::collections::HashSet<String>,
    pub(crate) payload_store: Arc<RwLock<PayloadStoreMap>>,
    /// Monotonic counter incremented whenever the browse tree changes; used by
    /// the filter cache to detect staleness.
    pub(crate) tree_version: u64,
    /// Cache for the visible-path set: (lowercased filter, tree_version,
    /// computed at, visible set). Populated and read by filter rendering;
    /// stored here so it survives frames.
    pub(crate) tree_filter_cache: Option<(String, u64, Instant, HashSet<String>)>,
    /// What the Publish view shows under its button (T23 renders it).
    pub(crate) publish_status: Option<PublishStatus>,
    /// When the current connect attempt started (T12 fills it).
    pub(crate) connect_started: Option<Instant>,
    /// Where the current connect attempt is going (T12 fills it).
    pub(crate) connect_target: String,
    /// Set when `process_events` stopped at its per-frame budget with events
    /// still queued, so the next frame should come soon.
    pub(crate) events_pending: bool,
    /// Keys whose Subscribe was sent and not yet answered.
    pub(crate) pending_subscribes: HashSet<String>,
    /// The alert `ui_alert` held when it was first seen, and when; the
    /// banner's expiry reads it (Success 6 s, Warning 10 s).
    pub(crate) ui_alert_since: Option<(UiAlert, Instant)>,
    /// A Help heading to scroll into view once; set by `help_link`.
    pub(crate) help_target: Option<&'static str>,
    /// (leaf topics whose path matches the filter, all leaf topics), computed
    /// with the filter cache; None when not filtering.
    pub(crate) tree_filter_counts: Option<(usize, usize)>,
}

impl ZenohExplorer {
    /// Creates a new instance of the Zenoh Explorer application.
    /// Sets up communication channels and spawns the Zenoh worker thread.
    /// The buffer thread wakes the UI through `ctx` whenever it forwards events.
    pub fn new(ctx: egui::Context) -> Self {
        // Create channels for worker/buffer/ui
        let (command_sender, command_receiver) = mpsc::channel();
        let (worker_event_sender, buffer_receiver) =
            worker::pipeline::event_channel(worker::pipeline::WORKER_EVENT_CAPACITY);
        let (ui_sender, event_receiver) =
            worker::pipeline::event_channel(worker::pipeline::UI_EVENT_CAPACITY);

        // Samples the pipeline could not accept (shared with the worker)
        let sample_drops = Arc::new(AtomicUsize::new(0));
        let drops_clone = sample_drops.clone();

        // Create shared key-value store for queryable; the app keeps the same Arc
        let local_kvstore = Arc::new(RwLock::new(LocalKvStore::new()));
        let kvstore_clone = local_kvstore.clone();

        // Start message buffer thread; it requests a repaint after each forward
        let repaint_ctx = ctx.clone();
        std::thread::spawn(move || {
            worker::pipeline::message_buffer_thread(buffer_receiver, ui_sender, move || {
                repaint_ctx.request_repaint()
            });
        });

        // Start the Zenoh worker in a separate async task
        std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                worker::zenoh_worker(
                    command_receiver,
                    worker_event_sender,
                    kvstore_clone,
                    drops_clone,
                )
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
            max_messages: 50_000,
            max_memory_mb: 100,
            current_memory_bytes: 0,
            message_filter: String::new(),
            auto_scroll: true,
            query_alert: None,
            ui_alert: None,
            messages_dropped: 0,
            rate_limiter: RateLimiter::new(1000),
            rate_limit_drops: 0,
            memory_alert: None,
            monitor_ok: true,
            stored_bytes_cache: (
                Instant::now()
                    .checked_sub(std::time::Duration::from_secs(1))
                    .unwrap_or_else(Instant::now),
                0,
            ),
            last_health_check: Instant::now(),
            worker_healthy: true,
            ping_sent_at: None,
            last_tick_at: Instant::now(),
            worker_gone: false,
            deduper: Deduper::new(DEDUP_WINDOW),
            messages_deduped: 0,
            sample_drops,
            local_kvstore,
            queryable_enabled: false,
            queryable_pattern: "**".to_string(),
            paused_keys: std::collections::HashSet::new(),
            json_parse_cache: std::collections::HashMap::new(),
            expanded_payloads: std::collections::HashSet::new(),
            payload_store: Arc::new(RwLock::new(HashMap::new())),
            tree_version: 0,
            tree_filter_cache: None,
            publish_status: None,
            connect_started: None,
            connect_target: String::new(),
            events_pending: false,
            pending_subscribes: HashSet::new(),
            ui_alert_since: None,
            help_target: None,
            tree_filter_counts: None,
        }
    }
}

impl ZenohExplorer {
    /// Sets or clears the history-size warning; it never touches `query_alert`.
    pub(crate) fn update_memory_alert(&mut self) {
        let pct = history_percent(self.current_memory_bytes, self.max_memory_mb);
        self.memory_alert = if MemLevel::of_percent(pct) == MemLevel::Ok {
            None
        } else {
            Some(format!(
                "History is {pct:.0}% of its limit: the oldest rows will leave the list (they stay in the tree)"
            ))
        };
    }

    /// The connection status as the header shows it.
    pub(crate) fn header_status_text(&self) -> String {
        match &self.connection_status {
            ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
                let elapsed = self.connect_started.map_or(0, |t| t.elapsed().as_secs());
                format!("Connecting to {} … {} s", self.connect_target, elapsed)
            }
            ConnectionStatus::Connected if !self.monitor_ok => {
                "Connected · monitor off".to_string()
            }
            ConnectionStatus::Error(e) => format!("Error: {}", header_error_clause(e)),
            other => other.text().to_string(),
        }
    }
}

#[cfg(test)]
impl ZenohExplorer {
    /// App wired to a test-controlled event channel.
    pub(crate) fn test_app() -> (Self, std::sync::mpsc::Sender<ZenohEvent>) {
        let mut app = Self::new(egui::Context::default());
        let (tx, rx) = std::sync::mpsc::channel();
        app.event_receiver = Some(rx);
        (app, tx)
    }
}

#[cfg(test)]
mod readout_tests {
    use super::*;

    #[test]
    fn memory_readout_names_scope_and_counts_stored_payloads() {
        let (text, level) = memory_readout(1024 * 1024, 100, 3 * 1024 * 1024 * 1024);
        assert!(text.contains("History 1.0 MB / 100 MB"), "{text}");
        assert!(text.contains("Stored payloads 3.00 GB"), "{text}"); // transfer::format_size wording
        assert!(matches!(level, MemLevel::Ok));
    }

    #[test]
    fn memory_level_uses_one_threshold_set() {
        let mb = 1024 * 1024;
        assert!(matches!(memory_readout(69 * mb, 100, 0).1, MemLevel::Ok));
        let (t, l) = memory_readout(70 * mb, 100, 0);
        assert!(matches!(l, MemLevel::High) && t.contains("(high)"), "{t}");
        let (t, l) = memory_readout(95 * mb, 100, 0);
        assert!(
            matches!(l, MemLevel::Critical) && t.contains("(critical)"),
            "{t}"
        );
    }

    #[test]
    fn memory_warning_does_not_touch_query_alert() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.current_memory_bytes = 85 * 1024 * 1024;
        app.import_memory_bytes = 500 * 1024 * 1024; // staged import: not history
        app.update_memory_alert();
        assert!(app.query_alert.is_none());
        assert!(app.memory_alert.as_deref().unwrap_or("").contains("85"));
    }

    #[test]
    fn peer_count_is_worded_and_shown_at_zero() {
        assert_eq!(peers_text(0, 0), "no peers");
        assert_eq!(peers_text(1, 0), "1 peer");
        assert_eq!(peers_text(1, 2), "2 routers · 1 peer");
    }

    #[test]
    fn monitor_failure_shows_in_header() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::PublishingConnected).unwrap();
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Monitor,
            error: "x".into(),
        })
        .unwrap();
        tx.send(ZenohEvent::MonitorConnected).unwrap();
        app.process_events();
        assert!(app.header_status_text().contains("monitor off"));
    }

    #[test]
    fn header_error_clause_keeps_first_clause() {
        // K1: the three connect texts, with an IP locator that must not be cut at its dots.
        assert_eq!(
            header_error_clause(
                "Could not connect in client mode: Unable to connect to any of [tcp/10.0.0.5:7447]"
            ),
            "Could not connect in client mode"
        );
        assert_eq!(
            header_error_clause("Client mode needs a router address. Enter one, or switch Mode to Peer. (No peer specified)"),
            "Client mode needs a router address"
        );
        assert_eq!(
            header_error_clause("Connection timeout in client mode: Unable to establish connection within 30 seconds"),
            "Connection timeout in client mode"
        );
        assert_eq!(
            header_error_clause("abcdefghij abcdefghij abcdefghij abcdefghij abc"),
            "abcdefghij abcdefghij abcdefghij…"
        );
    }
}
