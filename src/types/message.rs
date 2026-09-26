//! Messages, their type and source, subscriptions, views and connection status.

use chrono::{DateTime, Utc};
use egui::Color32;

use crate::colors::ExplorerColors;

/// Whether a sample carried a value (PUT) or removed one (DELETE).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleKindView {
    Put,
    Delete,
}

/// Represents a message received from or sent to the Zenoh network.
/// Contains all metadata needed for display and filtering in the UI.
#[derive(Clone)]
pub struct ZenohMessage {
    pub key: String,
    pub payload: String,
    pub encoding: String,
    pub timestamp: DateTime<Utc>,
    pub message_type: MessageType,
    /// Approximate memory size of this message in bytes
    pub size_bytes: usize,
    /// True if this message was published from this app instance
    pub is_local: bool,
    /// Raw payload bytes for export (None = use payload string as UTF-8)
    pub payload_bytes: Option<Vec<u8>>,
    /// Original filename transmitted by the sender (Zenoh attachment), if any.
    pub filename: Option<String>,
    /// Identifies which session this message came from (publishing, monitor, or local echo)
    pub source: MessageSource,
    /// PUT or DELETE, as carried by the sample.
    pub kind: SampleKindView,
    /// The publisher's timestamp, when the sample carried one.
    pub source_timestamp: Option<DateTime<Utc>>,
}

impl ZenohMessage {
    /// Calculate the approximate memory footprint of this message
    pub fn calculate_size(&self) -> usize {
        self.key.capacity()
            + self.payload.capacity()
            + self.encoding.capacity()
            + self.payload_bytes.as_ref().map_or(0, |v| v.capacity())
            + std::mem::size_of::<DateTime<Utc>>()
            + std::mem::size_of::<MessageType>()
            + std::mem::size_of::<MessageSource>()
            + std::mem::size_of::<usize>() // for size_bytes field
            + std::mem::size_of::<Self>() // struct size
            + 24 // Approximate heap allocation overhead per string (3 strings * 8 bytes)
    }

    /// Create a new message with raw bytes
    #[allow(clippy::too_many_arguments)]
    pub fn new_with_bytes(
        key: String,
        payload: String,
        payload_bytes: Vec<u8>,
        encoding: String,
        timestamp: DateTime<Utc>,
        message_type: MessageType,
        is_local: bool,
        source: MessageSource,
    ) -> Self {
        let mut msg = Self {
            key,
            payload,
            encoding,
            timestamp,
            message_type,
            size_bytes: 0,
            is_local,
            payload_bytes: Some(payload_bytes),
            filename: None,
            source,
            kind: SampleKindView::Put,
            source_timestamp: None,
        };
        msg.size_bytes = msg.calculate_size();
        msg
    }

    /// Attach a transmitted original filename (from a Zenoh attachment).
    pub fn with_filename(mut self, filename: Option<String>) -> Self {
        self.filename = filename;
        self
    }

    /// Attach the sample kind and publisher timestamp.
    pub fn with_sample_meta(mut self, kind: SampleKindView, ts: Option<DateTime<Utc>>) -> Self {
        self.kind = kind;
        self.source_timestamp = ts;
        self
    }
}

impl std::fmt::Debug for ZenohMessage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ZenohMessage")
            .field("key", &self.key)
            .field("encoding", &self.encoding)
            .field("kind", &self.kind)
            .field("type", &self.message_type)
            .field(
                "payload_len",
                &self
                    .payload_bytes
                    .as_ref()
                    .map_or(self.payload.len(), Vec::len),
            )
            .finish_non_exhaustive()
    }
}

/// What the Publish view shows under its button (T23 renders it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishStatus {
    Sending {
        key: String,
        bytes: usize,
    },
    Published {
        key: String,
        bytes: usize,
        at: DateTime<Utc>,
    },
    Failed(String),
}

/// Local wall-clock time for display; the date is prefixed when `ts` is not
/// on the same local day as `now`. Every list, history card and query result
/// uses this instead of formatting UTC directly.
pub fn format_local_time(ts: &DateTime<Utc>, now: &DateTime<Utc>) -> String {
    let local = ts.with_timezone(&chrono::Local);
    if local.date_naive() == now.with_timezone(&chrono::Local).date_naive() {
        local.format("%H:%M:%S%.3f").to_string()
    } else {
        local.format("%Y-%m-%d %H:%M:%S%.3f").to_string()
    }
}

/// Types of messages that can flow through the Zenoh network.
/// Each type has associated colors and labels for UI display.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageType {
    Subscribe,
    Publish,
    #[allow(dead_code)] // Zenoh GET operation type — will be used for query message display
    Query,
    QueryReply,
}

impl MessageType {
    /// Returns the color associated with this message type for UI display.
    pub fn color(&self) -> Color32 {
        match self {
            MessageType::Subscribe => ExplorerColors::PRIMARY, // Blue for subscriptions
            MessageType::Publish => ExplorerColors::SUCCESS,   // Green for publishes
            MessageType::Query => ExplorerColors::WARNING,     // Orange for queries
            MessageType::QueryReply => ExplorerColors::ERROR,  // Red for replies
        }
    }

    /// Returns a short label for this message type for compact UI display.
    pub fn label(&self) -> &str {
        match self {
            MessageType::Subscribe => "SUB",    // Subscription message
            MessageType::Publish => "PUT",      // Put/Publish operation
            MessageType::Query => "GET",        // Get/Query operation
            MessageType::QueryReply => "REPLY", // Query response
        }
    }
}

/// Identifies the source of a message for dual-session architecture.
/// This allows distinguishing between user-initiated operations and background monitoring.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageSource {
    /// Query replies received by the publishing session
    PublishingSession,
    /// Message from background ** subscription via the monitor session
    MonitorSession,
    /// Echo of a message published locally by this app instance
    LocalEcho,
    /// Samples of one user subscription on the publishing session, tagged with
    /// its id. Each subscription is its own source, so a sample that two
    /// overlapping subscriptions both receive is a cross-source duplicate.
    UserSubscription(std::sync::Arc<str>),
}

/// Metadata about an active subscription displayed in the UI.
/// This is separate from ActiveSubscription which manages the async task.
#[derive(Debug, Clone)]
pub struct Subscription {
    pub id: String,
    pub key_expr: String,
    #[allow(dead_code)]
    pub reliability: String,
    #[allow(dead_code)]
    pub mode: String,
}

/// View modes for the right panel detail area
#[derive(PartialEq, Debug, Clone)]
pub enum DetailView {
    TopicDetails,
    Publish,
    Query,
    Help,
}

/// Current status of the Zenoh connection.
/// Supports dual-session architecture with separate states for publishing and monitor sessions.
#[derive(PartialEq)]
pub enum ConnectionStatus {
    Disconnected,
    /// Initial connection phase - connecting the publishing session
    ConnectingPublishing,
    /// Second connection phase - connecting the monitor session
    ConnectingMonitor,
    /// Both sessions are connected and ready
    Connected,
    Error(String),
}

impl ConnectionStatus {
    pub fn color(&self) -> Color32 {
        match self {
            ConnectionStatus::Connected => ExplorerColors::SUCCESS,
            ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
                ExplorerColors::WARNING
            }
            ConnectionStatus::Disconnected | ConnectionStatus::Error(_) => ExplorerColors::ERROR,
        }
    }

    pub fn text(&self) -> &str {
        match self {
            ConnectionStatus::Connected => "Connected",
            ConnectionStatus::ConnectingPublishing => "Connecting (publishing)...",
            ConnectionStatus::ConnectingMonitor => "Connecting (monitor)...",
            ConnectionStatus::Disconnected => "Disconnected",
            ConnectionStatus::Error(_) => "Error",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_time_format_marks_other_days() {
        let now = chrono::Utc::now();
        assert_eq!(format_local_time(&now, &now).len(), "12:00:00.000".len());
        let old = now - chrono::Duration::days(2);
        assert!(
            format_local_time(&old, &now).contains('-'),
            "an earlier day shows its date"
        );
    }
}
