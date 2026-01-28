//! Command and event types for communication between GUI and worker threads.

use crate::types::ZenohMessage;

/// Commands sent from the GUI thread to the Zenoh worker thread.
#[derive(Debug)]
pub enum ZenohCommand {
    Connect {
        locators: String,
        listen_port: String,
        mode: String,
        config_json: String,
    },
    Disconnect,
    Subscribe {
        key_expr: String,
        reliability: String,
        mode: String,
    },
    Unsubscribe {
        subscription_id: String,
    },
    Publish {
        key: String,
        payload: Vec<u8>,
        encoding: String,
        from_import: bool,
    },
    Query {
        selector: String,
        value: String,
        timeout_ms: u64,
    },
    EnableQueryable {
        key_expr: String,
    },
    DisableQueryable,
    Ping,
}

/// Events sent from the Zenoh worker thread back to the GUI thread.
#[derive(Debug)]
pub enum ZenohEvent {
    Connected,
    Disconnected,
    DiscoveryUpdate {
        peers: usize,
        routers: usize,
    },
    ConnectionError(String),
    MessageReceived(ZenohMessage),
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
    Pong,
    PublishingConnected,
    MonitorConnected,
}
