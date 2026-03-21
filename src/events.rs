//! Event processing, deduplication, hashing, and message storage.
//!
//! Handles the flow of ZenohEvents from the worker thread into the GUI state:
//! dedup checks, rate limiting, browse tree updates, and message storage with limits.

use std::time::{Duration, Instant};
use tracing::{debug, error, info};

use crate::types::*;
use crate::app::ZenohExplorer;

impl ZenohExplorer {
    /// Compute a hash for message deduplication (key + partial payload).
    pub(crate) fn compute_message_hash(key: &str, payload: &str) -> u64 {
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        use std::hash::{Hash, Hasher};
        key.hash(&mut hasher);

        // Hash as bytes to avoid UTF-8 char boundary issues with binary data
        let bytes = payload.as_bytes();
        bytes.len().hash(&mut hasher);

        if bytes.len() > MAX_HASH_BYTES * 2 {
            // Large payload: hash first 4KB + last 4KB
            bytes[..MAX_HASH_BYTES].hash(&mut hasher);
            bytes[bytes.len() - MAX_HASH_BYTES..].hash(&mut hasher);
        } else if bytes.len() > MAX_HASH_BYTES {
            // Medium payload: hash first 4KB
            bytes[..MAX_HASH_BYTES].hash(&mut hasher);
        } else {
            // Small payload: hash entire thing
            bytes.hash(&mut hasher);
        }
        hasher.finish()
    }

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

    /// Check if message is duplicate and update dedup cache
    pub(crate) fn is_duplicate(&mut self, key: &str, payload: &str) -> bool {
        if !self.dedup_enabled {
            return false;
        }

        let hash = Self::compute_message_hash(key, payload);
        let now = Instant::now();

        // Clean old hashes periodically
        if self.message_hashes.len().is_multiple_of(100) {
            self.message_hashes.retain(|_, &mut timestamp| {
                now.duration_since(timestamp) < self.dedup_ttl
            });
        }

        // Check if we've seen this message recently
        if let Some(&last_seen) = self.message_hashes.get(&hash) {
            if now.duration_since(last_seen) < self.dedup_ttl {
                return true; // Duplicate
            }
        }

        // Not a duplicate, record it
        self.message_hashes.insert(hash, now);
        false
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
                    self.messages[idx] = message.clone();
                    return;
                } else if !message.is_local && self.messages[idx].is_local {
                    return;
                }
            }
        }

        // Apply deduplication check (but not for query replies, we want to see those every time)
        if message.message_type != MessageType::QueryReply
            && self.is_duplicate(&message.key, &message.payload)
        {
            self.messages_deduped += 1;
            return;
        }

        // Skip paused keys (don't display updates for paused keys)
        if self.paused_keys.contains(&message.key) {
            return;
        }

        // Apply rate limiting
        if self.rate_limiter.check_and_update() {
            // Clear query alert if we received a query reply
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

    /// Adds a received message to the hierarchical browse tree.
    /// Creates parent nodes as needed to maintain the tree structure.
    pub(crate) fn add_message_to_browse_tree(&self, message: &ZenohMessage) {
        if let Ok(mut tree) = self.browse_tree.write() {
            // Split the key into path segments
            let parts: Vec<&str> = message.key.split('/').collect();
            let mut current_node = &mut *tree;

            // Navigate through the tree, creating nodes as needed
            for part in parts {
                if !part.is_empty() {
                    // We need to work around the borrow checker here
                    let part_string = part.to_string();
                    current_node = current_node
                        .children
                        .entry(part_string.clone())
                        .or_insert_with(|| ZenohNode::new(part_string));
                }
            }

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
    }

    /// Add a message while respecting memory and count limits
    /// For large payloads: stores full in payload_store, truncates for messages list
    pub(crate) fn add_message_with_limits(&mut self, mut message: ZenohMessage) {
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
            // Use blocking write() to ensure raw bytes are always stored
            if let Ok(mut store) = self.payload_store.write() {
                if store.len() >= 500 {
                    if let Some(key) = store.keys().next().cloned() {
                        store.remove(&key);
                    }
                }
                // Store raw bytes for export
                store.insert(message.key.clone(), (raw_bytes, message.timestamp));
            } else {
                error!(
                    "Failed to acquire payload_store lock for key: {}",
                    message.key
                );
            }
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
