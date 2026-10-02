//! Message ingest: dedup, rate limiting, the browse tree and the message list.

use tracing::error;

use crate::app::ZenohExplorer;
use crate::types::*;

impl ZenohExplorer {
    /// Process a single message through dedup, rate limiting, and storage.
    ///
    /// The rate limit thins the message list only: the browse tree and the
    /// Save store see every accepted sample. Query replies are always listed
    /// and never touch the tree counts or the Save store.
    pub(crate) fn process_single_message(&mut self, message: ZenohMessage) {
        let is_query_reply = message.message_type == MessageType::QueryReply;
        if is_query_reply
            && self
                .query_alert
                .as_deref()
                .is_some_and(|a| a.starts_with("Query sent"))
        {
            self.query_alert = None;
        }

        // Dedup check on FULL content (query replies exempt — we want every reply).
        // The sample kind is part of the key: a Delete carries no bytes, like an
        // empty Put, and must not be taken for its duplicate.
        let dedup_hash = (self.deduper.enabled && !is_query_reply).then(|| {
            let bytes = message
                .payload_bytes
                .as_deref()
                .unwrap_or(message.payload.as_bytes());
            let h = Deduper::hash_message(&message.key, bytes);
            if message.kind == SampleKindView::Delete {
                !h
            } else {
                h
            }
        });
        if let Some(h) = dedup_hash {
            if self.deduper.is_cross_source_duplicate(h, &message.source) {
                self.messages_deduped += 1;
                return;
            }
        }

        if let Some(h) = dedup_hash {
            self.deduper.record(h, message.source.clone());
        }
        // Chunk messages are excluded from the messages list; their bytes still
        // go to payload_store via add_message_with_limits (display=false path).
        // Pause skips only DISPLAY (the messages list); storage and tree
        // updates continue so no data is lost while paused.
        let is_chunk = crate::transfer::parse_chunk_key(&message.key).is_some();
        // Only rows that would be listed use the list's rate budget; replies are always listed.
        let display = is_query_reply
            || (!is_chunk && !self.paused_keys.contains(&message.key) && {
                let ok = self.rate_limiter.check_and_update();
                if !ok {
                    self.rate_limit_drops += 1;
                }
                ok
            });

        if !is_query_reply {
            self.add_message_to_browse_tree(&message);
        }
        self.add_message_with_limits(message, display, !is_query_reply);
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
            // The marker counts the raw payload, not the display preview.
            let full_len = message
                .payload_bytes
                .as_ref()
                .map_or(message.payload.len(), Vec::len);

            // FAST PATH: Create truncated preview first (10KB max)
            // Use safe_truncate_index to handle UTF-8 boundaries correctly
            let payload_for_tree = if payload_len > PAYLOAD_PREVIEW_SIZE {
                let safe_end = safe_truncate_index(&message.payload, PAYLOAD_PREVIEW_SIZE);
                let mut truncated = String::with_capacity(safe_end + 64);
                truncated.push_str(&message.payload[..safe_end]);
                truncated.push_str(&format!(
                    "\n... [+{} bytes · Save File writes all of it]",
                    full_len.saturating_sub(safe_end)
                ));
                truncated
            } else {
                message.payload.clone()
            };

            // Note: Full payload storage moved to add_message_with_limits() which owns the message

            // Update the leaf node with the message data
            // Only mark tree node as local if this is a Publish message (not query replies)
            let mark_as_local = message.is_local && message.message_type == MessageType::Publish;
            current_node.update_data(
                payload_for_tree,
                message.encoding.clone(),
                mark_as_local,
                message.kind,
                message.source_timestamp,
            );
        }
        self.tree_version = self.tree_version.wrapping_add(1);
    }

    /// Add a message while respecting memory and count limits
    /// For large payloads: stores full in payload_store, truncates for messages list.
    /// `store` guards only the payload_store insert (false for query replies);
    /// the raw bytes are dropped from the listed message either way.
    pub(crate) fn add_message_with_limits(
        &mut self,
        mut message: ZenohMessage,
        display: bool,
        store: bool,
    ) {
        const MAX_STORED_PAYLOAD: usize = 10 * 1024; // 10KB max in messages list
        const MAX_EXPORT_PAYLOAD: usize = 4 * 1024 * 1024 * 1024; // 4GB max for export store

        // Get raw bytes for storage - prefer payload_bytes if available, otherwise use payload string as UTF-8
        let raw_bytes = message
            .payload_bytes
            .take()
            .unwrap_or_else(|| message.payload.as_bytes().to_vec());
        let payload_len = raw_bytes.len();

        // Store full payload bytes for export
        if store && payload_len <= MAX_EXPORT_PAYLOAD {
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
                .push_str("... [truncated · Save File writes all of it]");
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

#[cfg(test)]
mod tests {
    use crate::app::ZenohExplorer;
    use crate::types::*;

    fn msg(key: &str, source: MessageSource, ty: MessageType, local: bool) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            key.into(),
            "v".into(),
            b"v".to_vec(),
            "text/plain".into(),
            chrono::Utc::now(),
            ty,
            local,
            source,
        )
    }

    #[test]
    fn rate_limited_messages_still_update_tree() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        for _ in 0..3 {
            app.process_single_message(msg(
                "r/x",
                MessageSource::MonitorSession,
                MessageType::Subscribe,
                false,
            ));
        }
        let count = app.browse_tree.read().unwrap().children["r"].children["x"].message_count;
        assert_eq!(count, 3);
        assert_eq!(app.messages.len(), 1);
        assert_eq!(app.rate_limit_drops, 2);
    }

    #[test]
    fn local_and_remote_replies_are_both_kept() {
        // F-T16-8: the old local-wins rule dropped remote replies for a key in
        // every later query. Both replies must be listed, and accounted.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.process_single_message(msg(
            "q/a",
            MessageSource::PublishingSession,
            MessageType::QueryReply,
            true,
        ));
        app.process_single_message(msg(
            "q/a",
            MessageSource::PublishingSession,
            MessageType::QueryReply,
            false,
        ));
        assert_eq!(app.messages.len(), 2);
        let sum: usize = app.messages.iter().map(|m| m.size_bytes).sum();
        assert_eq!(app.current_memory_bytes, sum);
    }

    #[test]
    fn query_replies_skip_tree_and_pause() {
        // F-T16-9: a reply is listed even for a paused key or over the rate
        // limit, clears the waiting alert, and leaves tree counts, the Current
        // Value and the Save store alone.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        app.paused_keys.insert("q/b".into());
        app.query_alert = Some("Query sent for 'q/**'. Waiting for responses...".into());
        for _ in 0..2 {
            app.process_single_message(msg(
                "q/b",
                MessageSource::PublishingSession,
                MessageType::QueryReply,
                false,
            ));
        }
        assert_eq!(app.messages.len(), 2);
        assert_eq!(app.rate_limit_drops, 0);
        assert!(app.query_alert.is_none());
        let tree = app.browse_tree.read().unwrap();
        assert!(tree
            .children
            .get("q")
            .and_then(|q| q.children.get("b"))
            .is_none_or(|n| n.message_count == 0));
        assert!(app.payload_store.read().unwrap().get("q/b").is_none());
    }

    #[test]
    fn paused_topic_does_not_use_rate_budget() {
        // G3-1: paused and chunk rows are never listed, so they must not use
        // the list's rate budget or count as "not listed (rate)".
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        app.paused_keys.insert("p/x".into());
        app.process_single_message(msg(
            "p/x",
            MessageSource::MonitorSession,
            MessageType::Subscribe,
            false,
        ));
        app.process_single_message(msg(
            "r/y",
            MessageSource::MonitorSession,
            MessageType::Subscribe,
            false,
        ));
        assert_eq!(app.messages.len(), 1);
        assert_eq!(app.rate_limit_drops, 0);
    }

    #[test]
    fn tree_marker_counts_raw_bytes() {
        // K5: "+N bytes" counts the raw payload, not the ≤50 KiB display preview.
        let (mut app, _tx) = ZenohExplorer::test_app();
        let bytes = vec![b'a'; 100 * 1024];
        app.process_single_message(ZenohMessage::new_with_bytes(
            "big/x".into(),
            crate::payload::preview(&bytes, MAX_UI_DISPLAY_SIZE),
            bytes,
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        ));
        let tree = app.browse_tree.read().unwrap();
        let shown = tree.children["big"].children["x"]
            .last_payload
            .clone()
            .unwrap();
        assert!(shown.ends_with(&format!(
            "[+{} bytes · Save File writes all of it]",
            100 * 1024 - 10 * 1024
        )));
    }

    #[test]
    fn delete_is_not_a_duplicate_of_empty_put() {
        // K6: a Delete carries no bytes, like an empty Put, so the dedup key
        // must include the sample kind.
        let (mut app, _tx) = ZenohExplorer::test_app();
        let put = ZenohMessage::new_with_bytes(
            "d/x".into(),
            String::new(),
            vec![],
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Publish,
            true,
            MessageSource::LocalEcho,
        );
        let del = ZenohMessage::new_with_bytes(
            "d/x".into(),
            "[DELETE]".into(),
            vec![],
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        )
        .with_sample_meta(SampleKindView::Delete, None);
        app.process_single_message(put);
        app.process_single_message(del);
        let tree = app.browse_tree.read().unwrap();
        let node = &tree.children["d"].children["x"];
        assert_eq!(node.message_count, 2);
        assert_eq!(node.last_kind, SampleKindView::Delete);
    }
}
