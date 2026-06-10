use chrono::{DateTime, Utc};
use egui::Color32;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::colors::ExplorerColors;

// ── Constants ────────────────────────────────────────────────────────────────

/// Maximum bytes to hash for deduplication (4KB instead of full payload)
pub const MAX_HASH_BYTES: usize = 4 * 1024;

/// UI preview size for browse tree nodes
pub const PAYLOAD_PREVIEW_SIZE: usize = 10 * 1024;

/// Message truncation size for display
pub const MAX_UI_DISPLAY_SIZE: usize = 50 * 1024;

// Font sizes
pub const HEADING_LARGE_SIZE: f32 = 24.0; // Main app title
pub const HEADING_MEDIUM_SIZE: f32 = 18.0; // Section headings
pub const TEXT_SMALL_SIZE: f32 = 13.0; // Secondary info
pub const TOPIC_PREVIEW_TEXT_SIZE: f32 = 13.0; // Topic preview in tree
pub const SUBSCRIPTION_TEXT_SIZE: f32 = 13.0; // Subscription list items

// ── Helper functions ─────────────────────────────────────────────────────────

/// Safely find a valid UTF-8 char boundary at or before the given index
pub fn safe_truncate_index(s: &str, max_len: usize) -> usize {
    if max_len >= s.len() {
        return s.len();
    }
    // Find a valid char boundary at or before max_len
    let bytes = s.as_bytes();
    let mut end = max_len;
    // UTF-8 continuation bytes start with 10xxxxxx (0x80-0xBF)
    while end > 0 && end < bytes.len() && (bytes[end] & 0b11000000) == 0b10000000 {
        end -= 1;
    }
    end
}

// ── Data structures ──────────────────────────────────────────────────────────

/// Represents a node in the hierarchical browse tree.
/// Each node can have children (forming a tree structure) and maintains
/// metadata about the last received message for that key path.
#[derive(Debug, Clone)]
pub struct ZenohNode {
    pub key: String,
    pub children: BTreeMap<String, ZenohNode>,
    pub last_seen: Instant,
    pub message_count: usize,
    pub last_payload: Option<String>,
    pub last_encoding: Option<String>,
    pub is_local: bool, // True if this key was published from this app instance
}

impl ZenohNode {
    /// Creates a new tree node with the given key.
    pub fn new(key: String) -> Self {
        Self {
            key,
            children: BTreeMap::new(), // Use BTreeMap for sorted keys
            last_seen: Instant::now(),
            message_count: 0,
            last_payload: None,
            last_encoding: None,
            is_local: false,
        }
    }

    /// Updates the node with new message data.
    /// Tracks when the data was last seen and increments the message count.
    pub fn update_data(&mut self, payload: String, encoding: String, is_local: bool) {
        self.last_seen = Instant::now();
        self.message_count += 1;
        self.last_payload = Some(payload);
        self.last_encoding = Some(encoding);
        // Local publications take precedence
        if is_local {
            self.is_local = true;
        }
    }
}

/// A stored payload: full raw bytes plus receive metadata.
#[derive(Debug, Clone)]
pub struct PayloadEntry {
    pub bytes: Vec<u8>,
    /// When this payload was received from the network.
    #[allow(dead_code)]
    pub received_at: DateTime<Utc>,
    /// Original filename transmitted by the sender (Zenoh attachment), if any.
    #[allow(dead_code)]
    pub filename: Option<String>,
}

/// The export store map. Keyed by full topic (or chunk) key.
pub type PayloadStoreMap = std::collections::HashMap<String, PayloadEntry>;

/// Manages the lifecycle of an active Zenoh subscription.
/// Includes the async task handle and a cancellation mechanism for clean shutdown.
pub struct ActiveSubscription {
    /// The key expression this subscription is listening to
    #[allow(dead_code)]
    pub key_expr: String,
    /// Handle to the async task processing messages for this subscription
    pub task_handle: JoinHandle<()>,
    /// Cancellation sender to cleanly stop the subscription
    pub cancel_sender: oneshot::Sender<()>,
}

/// Represents a message received from or sent to the Zenoh network.
/// Contains all metadata needed for display and filtering in the UI.
#[derive(Debug, Clone)]
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
    /// Identifies which session this message came from (publishing, monitor, or local echo)
    #[allow(dead_code)]
    pub source: MessageSource,
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
            source,
        };
        msg.size_bytes = msg.calculate_size();
        msg
    }
}

// ── Enums ────────────────────────────────────────────────────────────────────

/// Commands sent from the GUI thread to the Zenoh worker thread.
#[derive(Debug)]
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
    /// Message from user's explicit subscriptions via the publishing session
    PublishingSession,
    /// Message from background ** subscription via the monitor session
    MonitorSession,
    /// Echo of a message published locally by this app instance
    LocalEcho,
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

/// Tracks message rate to prevent flooding
pub struct RateLimiter {
    pub window_start: Instant,
    pub message_count: usize,
    pub max_messages_per_second: usize,
}

impl RateLimiter {
    pub fn new(max_messages_per_second: usize) -> Self {
        Self {
            window_start: Instant::now(),
            message_count: 0,
            max_messages_per_second,
        }
    }

    /// Check if we can accept a message, updates the rate limiter state
    pub fn check_and_update(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.window_start);

        // Reset window every second
        if elapsed >= Duration::from_secs(1) {
            self.window_start = now;
            self.message_count = 1;
            true
        } else if self.message_count < self.max_messages_per_second {
            self.message_count += 1;
            true
        } else {
            false // Rate limit exceeded
        }
    }
}
