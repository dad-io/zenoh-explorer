//! Commands from the GUI to the worker, and events from the worker to the GUI.

use super::ZenohMessage;

// ── Enums ────────────────────────────────────────────────────────────────────

/// Commands sent from the GUI thread to the Zenoh worker thread.
pub enum ZenohCommand {
    Connect {
        locators: String,
        listen_port: String, // Port to listen on in peer mode
        mode: String,
        config_json: String,
    },
    Disconnect,
    Subscribe {
        key_expr: String,
        #[allow(dead_code)] // TODO: Implement reliability configuration
        reliability: String,
        #[allow(dead_code)] // TODO: Implement mode configuration
        mode: String,
    },
    Unsubscribe {
        subscription_id: String,
    },
    Publish {
        key: String,
        payload: Vec<u8>, // Raw bytes
        encoding: String,
        from_import: bool, // If true, don't store payload after publish (imported files are ephemeral)
        /// Original filename of an imported file; transmitted as a Zenoh
        /// attachment so receivers can restore the name + extension on save.
        filename: Option<String>,
    },
    Query {
        selector: String,
        value: String,
        timeout_ms: u64,
    },
    /// Enable queryable on a key expression pattern
    EnableQueryable {
        key_expr: String,
    },
    DisableQueryable,
    /// Health check ping to verify worker thread is alive
    Ping,
}

impl std::fmt::Debug for ZenohCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ZenohCommand::Connect {
                locators,
                listen_port,
                mode,
                ..
            } => f
                .debug_struct("Connect")
                .field("locators", locators)
                .field("listen_port", listen_port)
                .field("mode", mode)
                .finish_non_exhaustive(),
            ZenohCommand::Disconnect => f.write_str("Disconnect"),
            ZenohCommand::Subscribe { key_expr, .. } => f
                .debug_struct("Subscribe")
                .field("key_expr", key_expr)
                .finish_non_exhaustive(),
            ZenohCommand::Unsubscribe { subscription_id } => f
                .debug_struct("Unsubscribe")
                .field("subscription_id", subscription_id)
                .finish(),
            ZenohCommand::Publish {
                key,
                payload,
                encoding,
                from_import,
                filename,
            } => f
                .debug_struct("Publish")
                .field("key", key)
                .field("payload_len", &payload.len())
                .field("encoding", encoding)
                .field("from_import", from_import)
                .field("filename", filename)
                .finish(),
            ZenohCommand::Query {
                selector,
                timeout_ms,
                ..
            } => f
                .debug_struct("Query")
                .field("selector", selector)
                .field("timeout_ms", timeout_ms)
                .finish_non_exhaustive(),
            ZenohCommand::EnableQueryable { key_expr } => f
                .debug_struct("EnableQueryable")
                .field("key_expr", key_expr)
                .finish(),
            ZenohCommand::DisableQueryable => f.write_str("DisableQueryable"),
            ZenohCommand::Ping => f.write_str("Ping"),
        }
    }
}

/// Which worker operation failed, so the UI can route the error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailedOp {
    Subscribe,
    Publish,
    Query,
    Queryable,
    Monitor,
}

/// Events sent from the Zenoh worker thread back to the GUI thread based on network activity
#[derive(Debug)]
pub enum ZenohEvent {
    Disconnected,
    DiscoveryUpdate {
        peers: usize,
        routers: usize,
    },
    ConnectionError(String),
    MessageReceived(ZenohMessage),
    /// Batch messages for efficient UI updates
    MessageBatch(Vec<ZenohMessage>),
    SubscriptionCreated {
        id: String,
        key_expr: String,
    },
    SubscriptionRemoved {
        id: String,
    },
    QueryNoResponses {
        selector: String,
    },
    /// Health check pong response
    Pong,
    /// Publishing session connected (first phase of dual-session connection)
    PublishingConnected,
    /// Monitor session connected (second phase of dual-session connection)
    MonitorConnected,
    /// A worker operation failed; the UI routes the error by `op`.
    OperationFailed {
        op: FailedOp,
        error: String,
    },
    /// A put the worker completed (every Ok arm of the publish handler, T7).
    Published {
        key: String,
        bytes: usize,
    },
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::*;

    #[test]
    fn debug_output_omits_payload_bytes() {
        let cmd = ZenohCommand::Publish {
            key: "k".into(),
            payload: vec![7u8; 1 << 20],
            encoding: "application/octet-stream".into(),
            from_import: true,
            filename: None,
        };
        let s = format!("{:?}", cmd);
        assert!(s.len() < 300, "{}", &s[..300.min(s.len())]);
        assert!(s.contains("1048576"));
        let msg = ZenohMessage::new_with_bytes(
            "k".into(),
            "p".into(),
            vec![7u8; 1 << 20],
            "x".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        );
        assert!(format!("{:?}", msg).len() < 400);
    }
}
