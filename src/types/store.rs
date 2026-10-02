//! Stored payloads and the handle of an active subscription.

use chrono::{DateTime, Utc};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

/// A stored payload: full raw bytes plus receive metadata.
#[derive(Debug, Clone)]
pub struct PayloadEntry {
    pub bytes: Vec<u8>,
    /// When this payload was received from the network.
    pub received_at: DateTime<Utc>,
    /// Original filename transmitted by the sender (Zenoh attachment), if any.
    pub filename: Option<String>,
}

/// The export store map. Keyed by full topic (or chunk) key.
pub type PayloadStoreMap = std::collections::HashMap<String, PayloadEntry>;

/// A locally published value served by the explorer's queryable.
#[derive(Debug, Clone)]
pub struct StoredValue {
    pub bytes: Vec<u8>,
    pub encoding: String,
}

/// Key → last locally published value.
pub type LocalKvStore = std::collections::HashMap<String, StoredValue>;

/// Manages the lifecycle of an active Zenoh subscription.
/// Includes the async task handle and a cancellation mechanism for clean shutdown.
pub struct ActiveSubscription {
    /// The key expression this subscription is listening to
    pub key_expr: String,
    /// Handle to the async task processing messages for this subscription
    pub task_handle: JoinHandle<()>,
    /// Cancellation sender to cleanly stop the subscription
    pub cancel_sender: oneshot::Sender<()>,
}
