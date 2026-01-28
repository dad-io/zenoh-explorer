//! Core data types for the Zenoh Explorer application.

use chrono::{DateTime, Utc};
use egui::Color32;
use std::collections::BTreeMap;

use crate::theme::ExplorerColors;

/// Represents a node in the hierarchical browse tree.
#[derive(Debug, Clone)]
pub struct ZenohNode {
    pub key: String,
    pub children: BTreeMap<String, ZenohNode>,
    pub last_seen: std::time::Instant,
    pub message_count: usize,
    pub last_payload: Option<String>,
    pub last_encoding: Option<String>,
    pub is_local: bool,
}

impl ZenohNode {
    pub fn new(key: String) -> Self {
        Self {
            key,
            children: BTreeMap::new(),
            last_seen: std::time::Instant::now(),
            message_count: 0,
            last_payload: None,
            last_encoding: None,
            is_local: false,
        }
    }

    pub fn update_data(&mut self, payload: String, encoding: String, is_local: bool) {
        self.last_seen = std::time::Instant::now();
        self.message_count += 1;
        self.last_payload = Some(payload);
        self.last_encoding = Some(encoding);
        if is_local {
            self.is_local = true;
        }
    }
}

/// Types of messages that can flow through the Zenoh network.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageType {
    Subscribe,
    Publish,
    Query,
    QueryReply,
}

impl MessageType {
    pub fn color(&self) -> Color32 {
        match self {
            MessageType::Subscribe => ExplorerColors::PRIMARY,
            MessageType::Publish => ExplorerColors::SUCCESS,
            MessageType::Query => ExplorerColors::WARNING,
            MessageType::QueryReply => ExplorerColors::ERROR,
        }
    }

    pub fn label(&self) -> &str {
        match self {
            MessageType::Subscribe => "SUB",
            MessageType::Publish => "PUT",
            MessageType::Query => "GET",
            MessageType::QueryReply => "REPLY",
        }
    }
}

/// Identifies the source of a message for dual-session architecture.
#[derive(Debug, Clone, PartialEq)]
pub enum MessageSource {
    PublishingSession,
    MonitorSession,
    LocalEcho,
}

/// Represents a message received from or sent to the Zenoh network.
#[derive(Debug, Clone)]
pub struct ZenohMessage {
    pub key: String,
    pub payload: String,
    pub encoding: String,
    pub timestamp: DateTime<Utc>,
    pub message_type: MessageType,
    pub size_bytes: usize,
    pub is_local: bool,
    pub payload_bytes: Option<Vec<u8>>,
    pub source: MessageSource,
}

impl ZenohMessage {
    pub fn calculate_size(&self) -> usize {
        self.key.capacity()
            + self.payload.capacity()
            + self.encoding.capacity()
            + self.payload_bytes.as_ref().map_or(0, |v| v.capacity())
            + std::mem::size_of::<DateTime<Utc>>()
            + std::mem::size_of::<MessageType>()
            + std::mem::size_of::<MessageSource>()
            + std::mem::size_of::<usize>()
            + std::mem::size_of::<Self>()
            + 24 // Heap allocation overhead
    }

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

/// Metadata about an active subscription displayed in the UI.
#[derive(Debug, Clone)]
pub struct Subscription {
    pub id: String,
    pub key_expr: String,
    pub reliability: String,
    pub mode: String,
}

/// View modes for the right panel detail area.
#[derive(PartialEq, Debug, Clone)]
pub enum DetailView {
    TopicDetails,
    Publish,
    Query,
    Help,
}

/// Current status of the Zenoh connection.
#[derive(PartialEq)]
pub enum ConnectionStatus {
    Disconnected,
    ConnectingPublishing,
    ConnectingMonitor,
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
