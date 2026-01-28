//! Main application state and core logic.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use egui::Color32;
use tracing::{debug, error, info};

use crate::commands::{ZenohCommand, ZenohEvent};
use crate::theme::ExplorerColors;
use crate::types::*;
use crate::utils::*;
use crate::worker::{message_buffer_thread, zenoh_worker};

/// Main application state.
pub struct ZenohExplorer {
    pub detail_view: DetailView,
    pub connection_status: ConnectionStatus,
    pub discovered_peers: usize,
    pub discovered_routers: usize,
    pub selected_topic: Option<String>,
    pub connect_transport: String,
    pub connect_address: String,
    pub connect_port: String,
    pub listen_port: String,
    pub connection_mode: String,
    pub config_json: String,
    pub subscribe_key: String,
    pub subscribe_reliability: String,
    pub subscribe_mode: String,
    pub publish_key: String,
    pub publish_payload: String,
    pub publish_payload_bytes: Option<Vec<u8>>,
    pub publish_payload_filename: Option<String>,
    pub publish_payload_expanded: bool,
    pub import_memory_bytes: usize,
    pub publish_encoding: String,
    pub query_selector: String,
    pub query_value: String,
    pub query_timeout: String,
    pub messages: VecDeque<ZenohMessage>,
    pub subscriptions: Vec<Subscription>,
    pub browse_tree: Arc<RwLock<ZenohNode>>,
    pub command_sender: Option<Sender<ZenohCommand>>,
    pub tree_filter: String,
    pub event_receiver: Option<Receiver<ZenohEvent>>,
    pub dark_mode: bool,
    pub max_messages: usize,
    pub max_memory_mb: usize,
    pub current_memory_bytes: usize,
    pub message_filter: String,
    pub auto_scroll: bool,
    pub query_alert: Option<String>,
    pub messages_dropped: usize,
    pub rate_limiter: RateLimiter,
    pub rate_limit_drops: usize,
    pub memory_warning_shown: bool,
    pub last_health_check: Instant,
    pub worker_healthy: bool,
    pub message_hashes: HashMap<u64, Instant>,
    pub dedup_ttl: Duration,
    pub dedup_enabled: bool,
    pub messages_deduped: usize,
    #[allow(dead_code)]
    pub local_kvstore: Arc<RwLock<HashMap<String, (String, String)>>>,
    pub queryable_enabled: bool,
    pub queryable_pattern: String,
    pub paused_keys: HashSet<String>,
    pub json_parse_cache: HashMap<u64, Option<String>>,
    pub expanded_payloads: HashSet<String>,
    pub payload_store: Arc<RwLock<HashMap<String, (Vec<u8>, chrono::DateTime<chrono::Utc>)>>>,
}

impl Default for ZenohExplorer {
    fn default() -> Self {
        Self::new()
    }
}

impl ZenohExplorer {
    pub fn new() -> Self {
        let (command_sender, command_receiver) = std::sync::mpsc::channel();
        let (worker_event_sender, buffer_receiver) = std::sync::mpsc::channel();
        let (ui_sender, event_receiver) = std::sync::mpsc::channel();

        let local_kvstore = Arc::new(RwLock::new(HashMap::new()));
        let kvstore_clone = local_kvstore.clone();

        std::thread::spawn(move || {
            message_buffer_thread(buffer_receiver, ui_sender);
        });

        std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                zenoh_worker(command_receiver, worker_event_sender, kvstore_clone).await;
            });
        });

        info!("ZenohExplorer initialized");

        Self {
            detail_view: DetailView::TopicDetails,
            connection_status: ConnectionStatus::Disconnected,
            discovered_peers: 0,
            discovered_routers: 0,
            selected_topic: None,
            connect_transport: "tcp".to_string(),
            connect_address: String::new(),
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
            query_value: String::new(),
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
            messages_dropped: 0,
            rate_limiter: RateLimiter::new(1000),
            rate_limit_drops: 0,
            memory_warning_shown: false,
            last_health_check: Instant::now(),
            worker_healthy: true,
            message_hashes: HashMap::new(),
            dedup_ttl: Duration::from_secs(60),
            dedup_enabled: true,
            messages_deduped: 0,
            local_kvstore: Arc::new(RwLock::new(HashMap::new())),
            queryable_enabled: false,
            queryable_pattern: "**".to_string(),
            paused_keys: HashSet::new(),
            json_parse_cache: HashMap::new(),
            expanded_payloads: HashSet::new(),
            payload_store: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    // Theme helpers
    pub fn background_color(&self) -> Color32 {
        if self.dark_mode { ExplorerColors::DARK_BACKGROUND } else { ExplorerColors::BACKGROUND }
    }

    #[allow(dead_code)]
    pub fn card_background_color(&self) -> Color32 {
        if self.dark_mode { ExplorerColors::DARK_CARD_BACKGROUND } else { ExplorerColors::CARD_BACKGROUND }
    }

    pub fn text_color(&self) -> Color32 {
        if self.dark_mode { ExplorerColors::DARK_TEXT_PRIMARY } else { ExplorerColors::TEXT_PRIMARY }
    }

    pub fn text_secondary_color(&self) -> Color32 {
        if self.dark_mode { ExplorerColors::DARK_TEXT_SECONDARY } else { ExplorerColors::TEXT_SECONDARY }
    }

    pub fn text_tertiary_color(&self) -> Color32 {
        if self.dark_mode { ExplorerColors::DARK_TEXT_TERTIARY } else { ExplorerColors::TEXT_TERTIARY }
    }

    pub fn animate_fade_in(&self, ctx: &egui::Context, id: &str, target: f32) -> f32 {
        ctx.animate_value_with_time(egui::Id::new(id), target, 0.001)
    }

    pub fn animate_pulse(&self, ctx: &egui::Context, _id: &str) -> f32 {
        let time = ctx.input(|i| i.time) as f32;
        0.85 + (time * 3.0).sin() * 0.15
    }

    pub fn is_duplicate(&mut self, key: &str, payload: &str) -> bool {
        if !self.dedup_enabled {
            return false;
        }

        let hash = compute_message_hash(key, payload);
        let now = Instant::now();

        if self.message_hashes.len() % 100 == 0 {
            self.message_hashes.retain(|_, &mut timestamp| {
                now.duration_since(timestamp) < self.dedup_ttl
            });
        }

        if let Some(&last_seen) = self.message_hashes.get(&hash) {
            if now.duration_since(last_seen) < self.dedup_ttl {
                return true;
            }
        }

        self.message_hashes.insert(hash, now);
        false
    }

    pub fn get_cached_json(&mut self, payload: &str) -> Option<String> {
        if payload.len() > MAX_UI_DISPLAY_SIZE {
            return None;
        }

        let hash = compute_payload_hash(payload);

        if let Some(cached) = self.json_parse_cache.get(&hash) {
            return cached.clone();
        }

        let result = if let Ok(json_value) = serde_json::from_str::<serde_json::Value>(payload) {
            if let Ok(pretty) = serde_json::to_string_pretty(&json_value) {
                if pretty.len() > MAX_UI_DISPLAY_SIZE {
                    let safe_end = safe_truncate_index(&pretty, MAX_UI_DISPLAY_SIZE);
                    let mut truncated = pretty[..safe_end].to_string();
                    truncated.push_str(&format!("\n... [+{} bytes]", pretty.len() - safe_end));
                    Some(truncated)
                } else {
                    Some(pretty)
                }
            } else {
                None
            }
        } else {
            None
        };

        if self.json_parse_cache.len() > 100 {
            self.json_parse_cache.clear();
        }

        self.json_parse_cache.insert(hash, result.clone());
        result
    }

    pub fn process_events(&mut self) {
        let events: Vec<ZenohEvent> = if let Some(receiver) = &self.event_receiver {
            let mut events = Vec::new();
            while let Ok(event) = receiver.try_recv() {
                debug!("Received event from worker: {:?}", event);
                events.push(event);
            }
            if !events.is_empty() {
                debug!("Processing {} events", events.len());
            }
            events
        } else {
            Vec::new()
        };

        for event in events {
            match event {
                ZenohEvent::Connected => {
                    info!("GUI received Connected event (legacy)");
                    self.connection_status = ConnectionStatus::Connected;
                }
                ZenohEvent::PublishingConnected => {
                    info!("GUI received PublishingConnected event");
                    self.connection_status = ConnectionStatus::ConnectingMonitor;
                }
                ZenohEvent::MonitorConnected => {
                    info!("GUI received MonitorConnected event - fully connected");
                    self.connection_status = ConnectionStatus::Connected;
                }
                ZenohEvent::Disconnected => {
                    self.connection_status = ConnectionStatus::Disconnected;
                    self.discovered_peers = 0;
                    self.discovered_routers = 0;
                    self.subscriptions.clear();
                }
                ZenohEvent::DiscoveryUpdate { peers, routers } => {
                    self.discovered_peers = peers;
                    self.discovered_routers = routers;
                }
                ZenohEvent::ConnectionError(err) => {
                    self.connection_status = ConnectionStatus::Error(err);
                }
                ZenohEvent::MessageReceived(message) => {
                    self.handle_message_received(message);
                }
                ZenohEvent::MessageBatch(messages) => {
                    for message in messages {
                        self.handle_message_received(message);
                    }
                }
                ZenohEvent::SubscriptionCreated { id, key_expr } => {
                    self.subscriptions.push(Subscription {
                        id,
                        key_expr,
                        reliability: self.subscribe_reliability.clone(),
                        mode: self.subscribe_mode.clone(),
                    });
                }
                ZenohEvent::SubscriptionRemoved { id } => {
                    self.subscriptions.retain(|s| s.id != id);
                }
                ZenohEvent::QueryNoResponses { selector } => {
                    self.query_alert = Some(format!(
                        "No queryables available for '{}'.\n\nQueries require active services (queryables) to respond.\nTry using Subscribe instead to monitor data.",
                        selector
                    ));
                }
                ZenohEvent::Pong => {
                    self.worker_healthy = true;
                    self.last_health_check = Instant::now();
                }
            }
        }

        // Health check
        if self.last_health_check.elapsed() > Duration::from_secs(5) {
            if let Some(sender) = &self.command_sender {
                debug!("Sending health check ping");
                if let Err(e) = sender.send(ZenohCommand::Ping) {
                    error!("Failed to send ping: {:?}", e);
                    self.worker_healthy = false;
                }
            }
            if self.last_health_check.elapsed() > Duration::from_secs(15) {
                self.worker_healthy = false;
            }
        }
    }

    fn handle_message_received(&mut self, message: ZenohMessage) {
        // Query reply dedup logic
        if message.message_type == MessageType::QueryReply {
            if let Some(idx) = self.messages.iter().position(|m|
                m.key == message.key && m.message_type == MessageType::QueryReply
            ) {
                if message.is_local && !self.messages[idx].is_local {
                    self.messages[idx] = message.clone();
                    return;
                } else if !message.is_local && self.messages[idx].is_local {
                    return;
                }
            }
        }

        if message.message_type != MessageType::QueryReply
            && self.is_duplicate(&message.key, &message.payload)
        {
            self.messages_deduped += 1;
            return;
        }

        if self.paused_keys.contains(&message.key) {
            return;
        }

        if self.rate_limiter.check_and_update() {
            let is_query_reply = message.message_type == MessageType::QueryReply;
            self.add_message_to_browse_tree(&message);
            self.add_message_with_limits(message);
            if is_query_reply {
                self.query_alert = None;
            }
        } else {
            self.rate_limit_drops += 1;
        }
    }

    pub fn add_message_to_browse_tree(&self, message: &ZenohMessage) {
        if let Ok(mut tree) = self.browse_tree.write() {
            let parts: Vec<&str> = message.key.split('/').collect();
            let mut current_node = &mut *tree;

            for part in parts {
                if !part.is_empty() {
                    let part_string = part.to_string();
                    current_node = current_node
                        .children
                        .entry(part_string.clone())
                        .or_insert_with(|| ZenohNode::new(part_string));
                }
            }

            let payload_len = message.payload.len();
            let payload_for_tree = if payload_len > PAYLOAD_PREVIEW_SIZE {
                let safe_end = safe_truncate_index(&message.payload, PAYLOAD_PREVIEW_SIZE);
                let mut truncated = String::with_capacity(safe_end + 64);
                truncated.push_str(&message.payload[..safe_end]);
                truncated.push_str(&format!(
                    "\n... [+{} bytes]",
                    payload_len - safe_end
                ));
                truncated
            } else {
                message.payload.clone()
            };

            let mark_as_local =
                message.is_local && message.message_type == MessageType::Publish;
            current_node.update_data(payload_for_tree, message.encoding.clone(), mark_as_local);
        }
    }

    pub fn add_message_with_limits(&mut self, mut message: ZenohMessage) {
        const MAX_STORED_PAYLOAD: usize = 10 * 1024;
        const MAX_EXPORT_PAYLOAD: usize = 4 * 1024 * 1024 * 1024;

        let raw_bytes = message
            .payload_bytes
            .take()
            .unwrap_or_else(|| message.payload.as_bytes().to_vec());
        let payload_len = raw_bytes.len();

        // Store full payload bytes for export
        if payload_len <= MAX_EXPORT_PAYLOAD {
            if let Ok(mut store) = self.payload_store.write() {
                if store.len() >= 500 {
                    if let Some(key) = store.keys().next().cloned() {
                        store.remove(&key);
                    }
                }
                store.insert(message.key.clone(), (raw_bytes, message.timestamp));
            } else {
                error!("Failed to acquire payload_store lock for key: {}", message.key);
            }
        }

        if message.payload.len() > MAX_STORED_PAYLOAD {
            let safe_end = safe_truncate_index(&message.payload, MAX_STORED_PAYLOAD);
            message.payload = message.payload[..safe_end].to_string();
            message.payload.push_str("... [truncated]");
            message.payload.shrink_to_fit();
        }

        message.size_bytes = message.calculate_size();
        let message_size = message.size_bytes;
        let max_memory_bytes = self.max_memory_mb * 1024 * 1024;

        while !self.messages.is_empty()
            && (self.current_memory_bytes + message_size > max_memory_bytes
                || self.messages.len() >= self.max_messages)
        {
            if let Some(old_msg) = self.messages.pop_front() {
                self.current_memory_bytes =
                    self.current_memory_bytes.saturating_sub(old_msg.size_bytes);
                self.messages_dropped += 1;
            }
        }

        self.current_memory_bytes += message_size;
        self.messages.push_back(message);
    }

    pub fn find_node<'a>(&self, node: &'a ZenohNode, path: &str) -> Option<&'a ZenohNode> {
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

    pub fn has_matching_descendant(&self, node: &ZenohNode, filter: &str, current_path: &str) -> bool {
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

    pub fn export_payload_to_file(&self, topic: &str, payload: &[u8]) {
        let suggested_name = topic.replace('/', "_");
        let suggested_name = if suggested_name.is_empty() {
            "payload.bin".to_string()
        } else {
            format!("{}.bin", suggested_name)
        };

        if let Some(path) = rfd::FileDialog::new()
            .set_file_name(&suggested_name)
            .add_filter("Binary Files", &["bin"])
            .add_filter("Text Files", &["txt"])
            .add_filter("JSON Files", &["json"])
            .add_filter("All Files", &["*"])
            .save_file()
        {
            match std::fs::write(&path, payload) {
                Ok(_) => {
                    info!("Exported {} bytes to: {}", payload.len(), path.display());
                }
                Err(e) => {
                    error!("Failed to export payload: {}", e);
                }
            }
        }
    }
}
