//! Event processing, deduplication, hashing, and message storage.
//!
//! Handles the flow of ZenohEvents from the worker thread into the GUI state:
//! dedup checks, rate limiting, browse tree updates, and message storage with limits.

use std::time::{Duration, Instant};
use tracing::{debug, error, info};

use crate::app::ZenohExplorer;
use crate::types::*;

impl ZenohExplorer {
    /// Compute a hash for payload caching (first 4KB only).
    pub(crate) fn compute_payload_hash(payload: &str) -> u64 {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        use std::hash::{Hash, Hasher};
        let bytes = payload.as_bytes();
        let hash_slice = if bytes.len() > MAX_HASH_BYTES {
            &bytes[..MAX_HASH_BYTES]
        } else {
            bytes
        };
        hash_slice.hash(&mut hasher);
        hasher.finish()
    }

    /// Get formatted JSON from cache or parse and cache it
    pub(crate) fn get_cached_json(&mut self, payload: &str) -> Option<String> {
        // Skip JSON parsing for very large payloads
        if payload.len() > MAX_UI_DISPLAY_SIZE {
            return None; // Will fall back to raw text display
        }

        let hash = Self::compute_payload_hash(payload);

        // Check cache first
        if let Some(cached) = self.json_parse_cache.get(&hash) {
            return cached.clone();
        }

        // Parse JSON and cache the result
        let result = if let Ok(json_value) = serde_json::from_str::<serde_json::Value>(payload) {
            if let Ok(pretty) = serde_json::to_string_pretty(&json_value) {
                // Truncate formatted JSON if still too large
                if pretty.len() > MAX_UI_DISPLAY_SIZE {
                    let safe_end = safe_truncate_index(&pretty, MAX_UI_DISPLAY_SIZE);
                    let mut truncated = pretty[..safe_end].to_string();
                    truncated.push_str(&format!(
                        "\n... [+{} bytes of JSON hidden]",
                        pretty.len() - safe_end
                    ));
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

        // keep last 100 entries
        if self.json_parse_cache.len() > 100 {
            self.json_parse_cache.clear();
        }

        self.json_parse_cache.insert(hash, result.clone());
        result
    }

    /// Processes all pending events from the Zenoh worker thread.
    /// This is called on each frame to keep the UI in sync with network activity.
    pub(crate) fn process_events(&mut self) {
        // Collect all pending events without blocking
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

        // Process each event and update UI state accordingly
        for event in events {
            match event {
                ZenohEvent::PublishingConnected => {
                    // Publishing session connected, waiting for monitor session
                    info!("GUI received PublishingConnected event");
                    self.connection_status = ConnectionStatus::ConnectingMonitor;
                }
                ZenohEvent::MonitorConnected => {
                    // Both sessions are now connected
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
                    self.process_single_message(message);
                }
                ZenohEvent::MessageBatch(messages) => {
                    // Process batch of messages efficiently
                    for message in messages {
                        self.process_single_message(message);
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
                        "No queryables available for '{}'. \n\nQueries require active services (queryables) to respond. \nTry using Subscribe instead to monitor data.",
                        selector
                    ));
                }
                ZenohEvent::Pong => {
                    // Worker is alive, update health status
                    self.worker_healthy = true;
                    self.last_health_check = Instant::now();
                }
            }
        }

        // Send periodic health checks
        if self.last_health_check.elapsed() > Duration::from_secs(5) {
            if let Some(sender) = &self.command_sender {
                debug!("Sending health check ping");
                if let Err(e) = sender.send(ZenohCommand::Ping) {
                    error!("Failed to send ping: {:?}", e);
                    self.worker_healthy = false;
                }
            }
            // Mark as potentially unhealthy only after a longer timeout
            // This prevents false positives during startup
            if self.last_health_check.elapsed() > Duration::from_secs(15) {
                self.worker_healthy = false;
            }
        }
    }

    /// Process a single message through dedup, rate limiting, and storage.
    fn process_single_message(&mut self, message: ZenohMessage) {
        // For query replies, handle "local wins" logic
        if message.message_type == MessageType::QueryReply {
            let existing_idx = self
                .messages
                .iter()
                .position(|m| m.key == message.key && m.message_type == MessageType::QueryReply);

            if let Some(idx) = existing_idx {
                if message.is_local && !self.messages[idx].is_local {
                    self.messages[idx] = message;
                    return;
                } else if !message.is_local && self.messages[idx].is_local {
                    return;
                }
            }
        }

        // Dedup check on FULL content (query replies exempt — we want every reply)
        let dedup_hash = (self.deduper.enabled && message.message_type != MessageType::QueryReply)
            .then(|| {
                let bytes = message
                    .payload_bytes
                    .as_deref()
                    .unwrap_or(message.payload.as_bytes());
                Deduper::hash_message(&message.key, bytes)
            });
        if let Some(h) = dedup_hash {
            if self.deduper.seen_recently(h) {
                self.messages_deduped += 1;
                return;
            }
        }

        // Rate limiting BEFORE the hash is recorded: a dropped message's
        // retransmit must not be classified as a duplicate.
        if !self.rate_limiter.check_and_update() {
            self.rate_limit_drops += 1;
            return;
        }
        if let Some(h) = dedup_hash {
            self.deduper.record(h);
        }

        let is_query_reply = message.message_type == MessageType::QueryReply;

        // Chunk messages are excluded from the messages list; their bytes still
        // go to payload_store via add_message_with_limits (display=false path).
        // Pause skips only DISPLAY (the messages list); storage and tree
        // updates continue so no data is lost while paused.
        let is_chunk = crate::transfer::parse_chunk_key(&message.key).is_some();
        let display = !is_chunk && !self.paused_keys.contains(&message.key);

        self.add_message_to_browse_tree(&message);
        self.add_message_with_limits(message, display);

        if is_query_reply {
            self.query_alert = None;
        }
    }

    /// Adds a received message to the hierarchical browse tree.
    /// Creates parent nodes as needed to maintain the tree structure.
    pub(crate) fn add_message_to_browse_tree(&mut self, message: &ZenohMessage) {
        // Chunk traffic: update the parent topic's transfer state instead of
        // materializing a 4-level __chunk subtree per chunk.
        if let Some((topic, meta)) = crate::transfer::parse_chunk_key(&message.key) {
            if meta.is_sane() {
                let topic_owned = topic.to_string();
                if let Ok(mut tree) = self.browse_tree.write() {
                    tree.record_chunk(&topic_owned, meta);
                }
                self.tree_version = self.tree_version.wrapping_add(1);
            }
            return;
        }

        if let Ok(mut tree) = self.browse_tree.write() {
            let current_node = tree.insert_path(&message.key);

            // DUAL-PATH STORAGE:
            // 1. Full payload -> payload_store (for export)
            // 2. Truncated preview -> browse_tree (for UI display)

            let payload_len = message.payload.len();

            // FAST PATH: Create truncated preview first (10KB max)
            // Use safe_truncate_index to handle UTF-8 boundaries correctly
            let payload_for_tree = if payload_len > PAYLOAD_PREVIEW_SIZE {
                let safe_end = safe_truncate_index(&message.payload, PAYLOAD_PREVIEW_SIZE);
                let mut truncated = String::with_capacity(safe_end + 64);
                truncated.push_str(&message.payload[..safe_end]);
                truncated.push_str(&format!(
                    "\n... [+{} bytes - use Export for full]",
                    payload_len - safe_end
                ));
                truncated
            } else {
                message.payload.clone()
            };

            // Note: Full payload storage moved to add_message_with_limits() which owns the message

            // Update the leaf node with the message data
            // Only mark tree node as local if this is a Publish message (not query replies)
            let mark_as_local = message.is_local && message.message_type == MessageType::Publish;
            current_node.update_data(payload_for_tree, message.encoding.clone(), mark_as_local);
        }
        self.tree_version = self.tree_version.wrapping_add(1);
    }

    /// Add a message while respecting memory and count limits
    /// For large payloads: stores full in payload_store, truncates for messages list
    pub(crate) fn add_message_with_limits(&mut self, mut message: ZenohMessage, display: bool) {
        const MAX_STORED_PAYLOAD: usize = 10 * 1024; // 10KB max in messages list
        const MAX_EXPORT_PAYLOAD: usize = 4 * 1024 * 1024 * 1024; // 4GB max for export store

        // Get raw bytes for storage - prefer payload_bytes if available, otherwise use payload string as UTF-8
        let raw_bytes = message
            .payload_bytes
            .take()
            .unwrap_or_else(|| message.payload.as_bytes().to_vec());
        let payload_len = raw_bytes.len();

        // Store full payload bytes for export
        if payload_len <= MAX_EXPORT_PAYLOAD {
            if let Ok(mut store) = self.payload_store.write() {
                crate::transfer::insert_payload(
                    &mut store,
                    message.key.clone(),
                    PayloadEntry {
                        bytes: raw_bytes,
                        received_at: message.timestamp,
                        filename: message.filename.clone(),
                    },
                );
            } else {
                error!(
                    "Failed to acquire payload_store lock for key: {}",
                    message.key
                );
            }
        }

        if !display {
            return; // stored above; paused traffic doesn't hit the messages list
        }

        // Truncate display payload for messages list
        if message.payload.len() > MAX_STORED_PAYLOAD {
            let safe_end = safe_truncate_index(&message.payload, MAX_STORED_PAYLOAD);
            message.payload = message.payload[..safe_end].to_string();
            message
                .payload
                .push_str("... [truncated - use Export for full]");
            message.payload.shrink_to_fit();
        }

        message.size_bytes = message.calculate_size();
        let message_size = message.size_bytes;
        let max_memory_bytes = self.max_memory_mb * 1024 * 1024;

        // Check if adding this message would exceed memory limit
        if self.current_memory_bytes + message_size > max_memory_bytes && !self.messages.is_empty()
        {
            // Remove oldest messages until we have space
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
        }

        // Also check count limit
        if self.messages.len() >= self.max_messages {
            if let Some(old_msg) = self.messages.pop_front() {
                self.current_memory_bytes =
                    self.current_memory_bytes.saturating_sub(old_msg.size_bytes);
                self.messages_dropped += 1;
            }
        }

        // Add the new message
        self.current_memory_bytes += message_size;
        self.messages.push_back(message);
    }
}
