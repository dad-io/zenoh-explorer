//! Zenoh Explorer - A native GUI application for exploring and debugging Zenoh networks.
//!
//! This application provides real-time monitoring, message inspection, publishing,
//! querying, and browsing capabilities for Zenoh networks. It uses a dual-thread
//! architecture with egui/eframe for the GUI and Tokio for async Zenoh operations.

use anyhow::Result;
use chrono::{DateTime, Utc};
use eframe::egui;
use egui::{Color32, Margin, RichText};
use std::collections::{BTreeMap, HashMap, VecDeque};
// Removed unused imports
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};
use tokio::task::JoinHandle;
use tokio::sync::oneshot;
use tracing::{debug, error, info};
use zenoh::config::WhatAmI;
use zenoh::Session;

/// Color scheme for the Zenoh Explorer UI.
/// Provides both light and dark mode color palettes following modern design principles.
/// Colors are optimized for readability and visual hierarchy.
#[cfg_attr(test, derive(Debug))]
pub struct ExplorerColors;
impl ExplorerColors {
    // Light mode colors
    pub const BACKGROUND: Color32 = Color32::from_rgb(248, 248, 248);
    pub const CARD_BACKGROUND: Color32 = Color32::from_rgb(255, 255, 255);
    pub const SIDEBAR: Color32 = Color32::from_rgb(242, 242, 247);
    pub const PRIMARY: Color32 = Color32::from_rgb(0, 122, 255);
    pub const PRIMARY_HOVER: Color32 = Color32::from_rgb(0, 102, 217);
    pub const SUCCESS: Color32 = Color32::from_rgb(52, 199, 89);
    pub const WARNING: Color32 = Color32::from_rgb(255, 149, 0);
    pub const ERROR: Color32 = Color32::from_rgb(255, 59, 48);
    pub const TEXT_PRIMARY: Color32 = Color32::from_rgb(28, 28, 30);
    pub const TEXT_SECONDARY: Color32 = Color32::from_rgb(99, 99, 102);
    pub const SEPARATOR: Color32 = Color32::from_rgba_premultiplied(0, 0, 0, 26);
    pub const SELECTED_BACKGROUND: Color32 = Color32::from_rgba_premultiplied(0, 122, 255, 25);
    pub const TEXT_TERTIARY: Color32 = Color32::from_rgb(142, 142, 147);
    pub const SURFACE: Color32 = Color32::from_rgb(250, 250, 250);

    // Dark mode colors
    pub const DARK_BACKGROUND: Color32 = Color32::from_rgb(45, 45, 45);
    pub const DARK_CARD_BACKGROUND: Color32 = Color32::from_rgb(75, 75, 75);
    pub const DARK_SIDEBAR: Color32 = Color32::from_rgb(55, 55, 55);
    pub const DARK_PRIMARY: Color32 = Color32::from_rgb(10, 132, 255);
    pub const DARK_PRIMARY_HOVER: Color32 = Color32::from_rgb(64, 156, 255);
    pub const DARK_SUCCESS: Color32 = Color32::from_rgb(48, 209, 88);
    pub const DARK_WARNING: Color32 = Color32::from_rgb(255, 159, 10);
    pub const DARK_ERROR: Color32 = Color32::from_rgb(255, 69, 58);
    pub const DARK_TEXT_PRIMARY: Color32 = Color32::from_rgb(255, 255, 255);
    pub const DARK_TEXT_SECONDARY: Color32 = Color32::from_rgb(180, 180, 180);
    pub const DARK_SEPARATOR: Color32 = Color32::from_rgba_premultiplied(255, 255, 255, 30);
    pub const DARK_SELECTED_BACKGROUND: Color32 =
        Color32::from_rgba_premultiplied(10, 132, 255, 40);
    pub const DARK_TEXT_TERTIARY: Color32 = Color32::from_rgb(160, 160, 160);
    pub const DARK_SURFACE: Color32 = Color32::from_rgb(60, 60, 60);
}

/// Application entry point.
/// Initializes logging, configures the native window, and launches the GUI.
fn main() -> eframe::Result<()> {
    // Initialize tracing for debug logging
    tracing_subscriber::fmt::init();
    
    info!("Zenoh Explorer starting...");

    // Configure the native window with appropriate size and title
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1400.0, 900.0])      // Default window size
            .with_min_inner_size([1000.0, 600.0])  // Minimum size to ensure UI remains usable
            .with_title("Zenoh Explorer")
            .with_visible(true)                    // Ensure window is visible
            .with_active(true),                    // Make window active
        ..Default::default()
    };

    // Launch the application
    info!("Launching eframe application...");
    eframe::run_native(
        "Zenoh Explorer",
        options,
        Box::new(|_cc| {
            info!("Creating ZenohExplorer instance...");
            Ok(Box::new(ZenohExplorer::new()))
        }),
    )
}

/// Represents a node in the hierarchical browse tree.
/// Each node can have children (forming a tree structure) and maintains
/// metadata about the last received message for that key path.
#[derive(Debug, Clone)]
struct ZenohNode {
    key: String,
    children: BTreeMap<String, ZenohNode>,
    last_seen: std::time::Instant,
    message_count: usize,
    last_payload: Option<String>,
    last_encoding: Option<String>,
}

impl ZenohNode {
    /// Creates a new tree node with the given key.
    fn new(key: String) -> Self {
        Self {
            key,
            children: BTreeMap::new(),              // Use BTreeMap for sorted keys
            last_seen: std::time::Instant::now(),
            message_count: 0,
            last_payload: None,
            last_encoding: None,
        }
    }

    /// Updates the node with new message data.
    /// Tracks when the data was last seen and increments the message count.
    fn update_data(&mut self, payload: String, encoding: String) {
        self.last_seen = std::time::Instant::now();
        self.message_count += 1;
        self.last_payload = Some(payload);
        self.last_encoding = Some(encoding);
    }
}

/// Manages the lifecycle of an active Zenoh subscription.
/// Includes the async task handle and a cancellation mechanism for clean shutdown.
struct ActiveSubscription {
    /// The key expression this subscription is listening to
    #[allow(dead_code)]
    key_expr: String,
    /// Handle to the async task processing messages for this subscription
    task_handle: JoinHandle<()>,
    /// Cancellation sender to cleanly stop the subscription
    cancel_sender: oneshot::Sender<()>,
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
}

impl ZenohMessage {
    /// Calculate the approximate memory footprint of this message
    /// Includes string capacity (not just length) and heap allocation overhead
    fn calculate_size(&self) -> usize {
        // String capacity is more accurate than length for actual memory usage
        self.key.capacity() + 
        self.payload.capacity() + 
        self.encoding.capacity() + 
        std::mem::size_of::<DateTime<Utc>>() + 
        std::mem::size_of::<MessageType>() +
        std::mem::size_of::<usize>() + // for size_bytes field
        std::mem::size_of::<Self>() +  // struct size
        24  // Approximate heap allocation overhead per string (3 strings * 8 bytes)
    }
    
    /// Create a new message and calculate its size
    fn new(key: String, payload: String, encoding: String, timestamp: DateTime<Utc>, message_type: MessageType) -> Self {
        let mut msg = Self {
            key,
            payload,
            encoding,
            timestamp,
            message_type,
            size_bytes: 0,
        };
        msg.size_bytes = msg.calculate_size();
        msg
    }
}

/// Commands sent from the GUI thread to the Zenoh worker thread.
/// This enables thread-safe communication for all Zenoh operations.
#[derive(Debug)]
pub enum ZenohCommand {
    Connect {
        locators: String,
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
        payload: String,
        encoding: String,
    },
    Query {
        selector: String,
        value: String,
        timeout_ms: u64,
    },
    /// Health check ping to verify worker thread is alive
    Ping,
}

/// Events sent from the Zenoh worker thread back to the GUI thread.
/// These events update the UI state based on network activity.
#[derive(Debug)]
pub enum ZenohEvent {
    Connected,
    Disconnected,
    ConnectionError(String),
    MessageReceived(ZenohMessage),
    SubscriptionCreated { id: String, key_expr: String },
    SubscriptionRemoved { id: String },
    QueryNoResponses { selector: String },
    /// Health check pong response
    Pong,
}

/// Types of messages that can flow through the Zenoh network.
/// Each type has associated colors and labels for UI display.
#[derive(Debug, Clone)]
pub enum MessageType {
    Subscribe,
    Publish,
    Query,
    QueryReply,
}

impl MessageType {
    /// Returns the color associated with this message type for UI display.
    pub fn color(&self) -> Color32 {
        match self {
            MessageType::Subscribe => ExplorerColors::PRIMARY,    // Blue for subscriptions
            MessageType::Publish => ExplorerColors::SUCCESS,      // Green for publishes
            MessageType::Query => ExplorerColors::WARNING,        // Orange for queries
            MessageType::QueryReply => ExplorerColors::ERROR,     // Red for replies
        }
    }

    /// Returns a short label for this message type for compact UI display.
    pub fn label(&self) -> &str {
        match self {
            MessageType::Subscribe => "SUB",      // Subscription message
            MessageType::Publish => "PUT",         // Put/Publish operation
            MessageType::Query => "GET",          // Get/Query operation
            MessageType::QueryReply => "REPLY",   // Query response
        }
    }
}

/// Metadata about an active subscription displayed in the UI.
/// This is separate from ActiveSubscription which manages the async task.
#[derive(Debug, Clone)]
pub struct Subscription {
    pub id: String,
    pub key_expr: String,
    pub reliability: String,
    pub mode: String,
}

/// View modes for the right panel detail area
#[derive(PartialEq, Debug, Clone)]
enum DetailView {
    /// Show topic details and message history
    TopicDetails,
    /// Show publish interface
    Publish,
    /// Show query interface
    Query,
    /// Show help information
    Help,
}

/// Current status of the Zenoh connection.
/// Used to update UI elements and control available actions.
enum ConnectionStatus {
    /// Not connected to any Zenoh network
    Disconnected,
    /// Connection attempt in progress
    Connecting,
    /// Successfully connected to Zenoh network
    Connected,
    /// Connection failed with error message
    Error(String),
}

impl ConnectionStatus {
    fn color(&self) -> Color32 {
        match self {
            ConnectionStatus::Connected => ExplorerColors::SUCCESS,
            ConnectionStatus::Connecting => ExplorerColors::WARNING,
            ConnectionStatus::Disconnected | ConnectionStatus::Error(_) => ExplorerColors::ERROR,
        }
    }

    fn text(&self) -> &str {
        match self {
            ConnectionStatus::Connected => "Connected",
            ConnectionStatus::Connecting => "Connecting...",
            ConnectionStatus::Disconnected => "Disconnected",
            ConnectionStatus::Error(_) => "Error",
        }
    }
}

/// Maximum size for a single message payload (10MB)
const MAX_MESSAGE_SIZE: usize = 10 * 1024 * 1024;

/// Tracks message rate to prevent flooding
struct RateLimiter {
    window_start: Instant,
    message_count: usize,
    max_messages_per_second: usize,
}

impl RateLimiter {
    fn new(max_messages_per_second: usize) -> Self {
        Self {
            window_start: Instant::now(),
            message_count: 0,
            max_messages_per_second,
        }
    }
    
    /// Check if we can accept a message, updates the rate limiter state
    fn check_and_update(&mut self) -> bool {
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

/// Main application state for Zenoh Explorer.
/// Contains all UI state, configuration, and communication channels.
struct ZenohExplorer {
    /// Current detail view mode
    detail_view: DetailView,
    connection_status: ConnectionStatus,
    /// Currently selected node path in the tree
    selected_topic: Option<String>,
    locators: String,
    connection_mode: String,
    config_json: String,
    subscribe_key: String,
    subscribe_reliability: String,
    subscribe_mode: String,
    publish_key: String,
    publish_payload: String,
    publish_encoding: String,
    query_selector: String,
    query_value: String,
    query_timeout: String,
    messages: VecDeque<ZenohMessage>,
    subscriptions: Vec<Subscription>,
    browse_tree: Arc<RwLock<ZenohNode>>,
    command_sender: Option<Sender<ZenohCommand>>,
    tree_filter: String,
    event_receiver: Option<Receiver<ZenohEvent>>,
    dark_mode: bool,
    max_messages: usize,
    max_memory_mb: usize,  // Maximum memory usage in MB
    current_memory_bytes: usize,  // Current total memory usage
    message_filter: String,
    auto_scroll: bool,
    query_alert: Option<String>,
    messages_dropped: usize,  // Counter for dropped messages
    rate_limiter: RateLimiter,
    rate_limit_drops: usize,  // Messages dropped due to rate limiting
    memory_warning_shown: bool,  // Track if we've shown the memory warning
    last_health_check: Instant,  // Track worker health
    worker_healthy: bool,  // Worker thread status
}

impl Default for ZenohExplorer {
    fn default() -> Self {
        Self::new()
    }
}

impl ZenohExplorer {
    /// Creates a new instance of the Zenoh Explorer application.
    /// Sets up communication channels and spawns the Zenoh worker thread.
    fn new() -> Self {
        // Create channels for bi-directional communication between GUI and worker threads
        let (command_sender, command_receiver) = mpsc::channel();
        let (event_sender, event_receiver) = mpsc::channel();

        // Start the Zenoh worker in a separate async task
        // This handles all Zenoh operations without blocking the GUI
        std::thread::spawn(move || {
            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                zenoh_worker(command_receiver, event_sender).await;
            });
        });
        
        info!("ZenohExplorer initialized with worker thread");

        Self {
            detail_view: DetailView::TopicDetails,
            connection_status: ConnectionStatus::Disconnected,
            selected_topic: None,
            locators: "tcp/localhost:7447".to_string(),  // Default router endpoint
            connection_mode: "client".to_string(),  // Default to client mode
            config_json: "{}".to_string(),
            subscribe_key: "demo/**".to_string(),
            subscribe_reliability: "reliable".to_string(),
            subscribe_mode: "push".to_string(),
            publish_key: "demo/test".to_string(),
            publish_payload: "Hello Zenoh!".to_string(),
            publish_encoding: "text/plain".to_string(),
            query_selector: "demo/**".to_string(),
            query_value: "".to_string(),
            query_timeout: "10000".to_string(),
            messages: VecDeque::new(),
            subscriptions: Vec::new(),
            browse_tree: Arc::new(RwLock::new(ZenohNode::new("root".to_string()))),
            command_sender: Some(command_sender),
            tree_filter: String::new(),
            event_receiver: Some(event_receiver),
            dark_mode: true,
            max_messages: 1000,
            max_memory_mb: 100,  // Default to 100MB limit
            current_memory_bytes: 0,
            message_filter: String::new(),
            auto_scroll: true,
            query_alert: None,
            messages_dropped: 0,
            rate_limiter: RateLimiter::new(1000), // Default: 1000 messages/second
            rate_limit_drops: 0,
            memory_warning_shown: false,
            last_health_check: Instant::now(),
            worker_healthy: true,
        }
    }

    fn background_color(&self) -> Color32 {
        if  self.dark_mode {
            ExplorerColors::DARK_BACKGROUND
        } else {
            ExplorerColors::BACKGROUND
        }
    }
    
    /// Returns the appropriate button style for the current theme
    fn apply_theme(&self, ctx: &egui::Context) {
        ctx.style_mut(|style| {
            if self.dark_mode {
                // Dark mode button style
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::DARK_PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::DARK_PRIMARY_HOVER;
                style.visuals.widgets.inactive.fg_stroke.color = Color32::WHITE;
                style.visuals.widgets.hovered.fg_stroke.color = Color32::WHITE;
                style.visuals.widgets.active.fg_stroke.color = Color32::WHITE;
            } else {
                // Light mode button style
                style.visuals.widgets.inactive.weak_bg_fill = ExplorerColors::PRIMARY;
                style.visuals.widgets.hovered.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;
                style.visuals.widgets.active.weak_bg_fill = ExplorerColors::PRIMARY_HOVER;
                style.visuals.widgets.inactive.fg_stroke.color = Color32::WHITE;
                style.visuals.widgets.hovered.fg_stroke.color = Color32::WHITE;
                style.visuals.widgets.active.fg_stroke.color = Color32::WHITE;
            }
        });
    }

    #[allow(dead_code)]
    fn card_background_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_CARD_BACKGROUND
        } else {
            ExplorerColors::CARD_BACKGROUND
        }
    }

    fn text_color(&self) -> Color32 {
        if self.dark_mode {
            ExplorerColors::DARK_TEXT_PRIMARY
        } else {
            ExplorerColors::TEXT_PRIMARY
        }
    }

    /// Processes all pending events from the Zenoh worker thread.
    /// This is called on each frame to keep the UI in sync with network activity.
    fn process_events(&mut self) {
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
                ZenohEvent::Connected => {
                    info!("GUI received Connected event");
                    self.connection_status = ConnectionStatus::Connected;
                }
                ZenohEvent::Disconnected => {
                    self.connection_status = ConnectionStatus::Disconnected;
                    self.subscriptions.clear();
                }
                ZenohEvent::ConnectionError(err) => {
                    self.connection_status = ConnectionStatus::Error(err);
                }
                ZenohEvent::MessageReceived(message) => {
                    // Apply rate limiting first
                    if self.rate_limiter.check_and_update() {
                        self.add_message_to_browse_tree(&message);
                        self.add_message_with_limits(message);
                    } else {
                        self.rate_limit_drops += 1;
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

    /// Adds a received message to the hierarchical browse tree.
    /// Creates parent nodes as needed to maintain the tree structure.
    fn add_message_to_browse_tree(&self, message: &ZenohMessage) {
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

            // Update the leaf node with the message data
            current_node.update_data(message.payload.clone(), message.encoding.clone());
        }
    }
    
    /// Add a message while respecting memory and count limits
    fn add_message_with_limits(&mut self, mut message: ZenohMessage) {
        // Truncate oversized payloads for safety
        if message.payload.len() > MAX_MESSAGE_SIZE {
            message.payload.truncate(MAX_MESSAGE_SIZE);
            message.payload.push_str("... [TRUNCATED]");
            // Recalculate size after truncation
            message.size_bytes = message.calculate_size();
        }
        
        let message_size = message.size_bytes;
        let max_memory_bytes = self.max_memory_mb * 1024 * 1024;
        
        // Check if adding this message would exceed memory limit
        if self.current_memory_bytes + message_size > max_memory_bytes && !self.messages.is_empty() {
            // Remove oldest messages until we have space
            while !self.messages.is_empty() && 
                  (self.current_memory_bytes + message_size > max_memory_bytes || 
                   self.messages.len() >= self.max_messages) {
                if let Some(old_msg) = self.messages.pop_front() {
                    self.current_memory_bytes = self.current_memory_bytes.saturating_sub(old_msg.size_bytes);
                    self.messages_dropped += 1;
                }
            }
        }
        
        // Also check count limit
        if self.messages.len() >= self.max_messages {
            if let Some(old_msg) = self.messages.pop_front() {
                self.current_memory_bytes = self.current_memory_bytes.saturating_sub(old_msg.size_bytes);
                self.messages_dropped += 1;
            }
        }
        
        // Add the new message
        self.current_memory_bytes += message_size;
        self.messages.push_back(message);
    }
}

/// Worker function that handles all Zenoh operations in a separate async task.
/// This prevents blocking the GUI thread and enables clean cancellation of operations.
/// 
/// # Arguments
/// * `command_receiver` - Channel to receive commands from the GUI thread
/// * `event_sender` - Channel to send events back to the GUI thread
async fn zenoh_worker(command_receiver: Receiver<ZenohCommand>, event_sender: Sender<ZenohEvent>) {
    info!("Zenoh worker thread started");
    
    // Current Zenoh session (if connected)
    let mut session: Option<Arc<Session>> = None;
    // Map of active subscriptions by ID for management
    let mut active_subscriptions: HashMap<String, ActiveSubscription> = HashMap::new();

    info!("Worker thread main loop starting...");
    
    // Main event loop - process commands as they arrive
    loop {
        // Use recv_timeout instead of try_recv to avoid busy waiting
        match command_receiver.recv_timeout(std::time::Duration::from_millis(100)) {
            Ok(command) => {
                info!("Worker received command: {:?}", command);
                match command {
                ZenohCommand::Connect {
                    locators,
                    mode,
                    config_json,
                } => {
                    info!("Worker processing connect command - mode: {}, locators: {}", mode, locators);
                    match connect_zenoh(&locators, &mode, &config_json).await {
                        Ok(new_session) => {
                            info!("Worker successfully created session");
                            session = Some(Arc::new(new_session));
                            match event_sender.send(ZenohEvent::Connected) {
                                Ok(_) => info!("Successfully sent Connected event to GUI"),
                                Err(e) => error!("Failed to send Connected event: {:?}", e),
                            }
                        }
                        Err(e) => {
                            error!("Worker failed to connect: {}", e);
                            match event_sender.send(ZenohEvent::ConnectionError(e.to_string())) {
                                Ok(_) => info!("Sent ConnectionError event to GUI"),
                                Err(send_err) => error!("Failed to send ConnectionError event: {:?}", send_err),
                            }
                        }
                    }
                }
                ZenohCommand::Disconnect => {
                    // Clean shutdown process:
                    // 1. Cancel all active subscriptions gracefully
                    for (_, subscription) in active_subscriptions.drain() {
                        // Send cancellation signal (ignore if already cancelled)
                        let _ = subscription.cancel_sender.send(());
                        // Abort the task as backup
                        subscription.task_handle.abort();
                    }

                    // 2. Close the Zenoh session
                    if let Some(s) = session.take() {
                        let _ = s.close().await;
                    }
                    
                    // 3. Notify GUI of disconnection
                    let _ = event_sender.send(ZenohEvent::Disconnected);
                }
                ZenohCommand::Subscribe {
                    key_expr,
                    reliability: _,    // TODO: Implement reliability configuration
                    mode: _,           // TODO: Implement mode configuration
                } => {
                    if let Some(ref sess) = session {
                        match sess.declare_subscriber(&key_expr).await {
                            Ok(subscriber) => {
                                // Generate unique subscription ID
                                let sub_id = format!(
                                    "sub_{}_{}",
                                    chrono::Utc::now().timestamp_millis(),
                                    active_subscriptions.len()
                                );
                                let event_sender_clone = event_sender.clone();
                                let key_expr_clone = key_expr.clone();
                                let (cancel_sender, mut cancel_receiver) = oneshot::channel();

                                // Spawn a dedicated task to handle incoming messages
                                // This allows multiple subscriptions to run concurrently
                                let task_handle = tokio::spawn(async move {
                                    // Use tokio::select! for clean cancellation
                                    loop {
                                        tokio::select! {
                                            // Handle cancellation signal
                                            _ = &mut cancel_receiver => {
                                                break;
                                            }
                                            // Handle incoming messages
                                            result = subscriber.recv_async() => {
                                                match result {
                                                    Ok(sample) => {
                                                        // Attempt to decode payload as string
                                                        // Fall back to debug format for binary data
                                                        let payload_raw =
                                                            match sample.payload().try_to_string() {
                                                                Ok(s) => s.into_owned(),
                                                                Err(_) => format!("{:?}", sample.payload()),
                                                            };

                                                        let message = ZenohMessage::new(
                                                            sample.key_expr().to_string(),
                                                            payload_raw,
                                                            "text/plain".to_string(),
                                                            Utc::now(),
                                                            MessageType::Subscribe,
                                                        );

                                                        let _ = event_sender_clone
                                                            .send(ZenohEvent::MessageReceived(message));
                                                    }
                                                    Err(_) => {
                                                        // Subscriber closed or error, exit loop
                                                        break;
                                                    }
                                                }
                                            }
                                        }
                                    }
                                });

                                // Store the subscription with its handle and cancellation sender
                                active_subscriptions.insert(
                                    sub_id.clone(),
                                    ActiveSubscription {
                                        key_expr: key_expr.clone(),
                                        task_handle,
                                        cancel_sender,
                                    },
                                );

                                let _ = event_sender.send(ZenohEvent::SubscriptionCreated {
                                    id: sub_id,
                                    key_expr: key_expr_clone,
                                });
                            }
                            Err(e) => {
                                error!("Failed to create subscriber: {}", e);
                            }
                        }
                    }
                }
                ZenohCommand::Publish {
                    key,
                    payload,
                    encoding,
                } => {
                    if let Some(ref sess) = session {
                        // Publish the data to the Zenoh network
                        let _ = sess
                            .put(&key, payload.clone())
                            .encoding(&encoding as &str)
                            .await;

                        // Echo the published message back to the UI
                        // This provides immediate feedback to the user
                        let message = ZenohMessage::new(
                            key.clone(),
                            payload,
                            encoding,
                            Utc::now(),
                            MessageType::Publish,
                        );

                        let _ = event_sender.send(ZenohEvent::MessageReceived(message));
                    }
                }
                ZenohCommand::Query {
                    selector,
                    value,
                    timeout_ms,
                } => {
                    if let Some(ref sess) = session {
                        let mut get_builder = sess.get(&selector);

                        if !value.is_empty() {
                            get_builder = get_builder.payload(value);
                        }

                        if let Ok(replies) = get_builder
                            .timeout(std::time::Duration::from_millis(timeout_ms))
                            .await
                        {
                            let event_sender_query = event_sender.clone();
                            let selector_clone = selector.clone();
                            tokio::spawn(async move {
                                let mut received_replies = false;
                                while let Ok(reply) = replies.recv_async().await {
                                    match reply.result() {
                                        Ok(sample) => {
                                            received_replies = true;
                                            let payload = match sample.payload().try_to_string() {
                                                Ok(s) => s.into_owned(),
                                                Err(_) => format!("{:?}", sample.payload()),
                                            };

                                            let message = ZenohMessage::new(
                                                sample.key_expr().to_string(),
                                                payload,
                                                "text/plain".to_string(),
                                                Utc::now(),
                                                MessageType::QueryReply,
                                            );

                                            let _ = event_sender_query
                                                .send(ZenohEvent::MessageReceived(message));
                                        }
                                        Err(e) => {
                                            error!("Query error: {}", e);
                                        }
                                    }
                                }
                                
                                // If no replies were received, send alert
                                if !received_replies {
                                    let _ = event_sender_query.send(ZenohEvent::QueryNoResponses {
                                        selector: selector_clone,
                                    });
                                }
                            });
                        }
                    }
                }
                ZenohCommand::Unsubscribe { subscription_id } => {
                    if let Some(subscription) = active_subscriptions.remove(&subscription_id) {
                        // Send cancellation signal (ignore if already cancelled)
                        let _ = subscription.cancel_sender.send(());
                        // Abort the task as backup
                        subscription.task_handle.abort();
                        let _ = event_sender.send(ZenohEvent::SubscriptionRemoved {
                            id: subscription_id,
                        });
                    }
                }
                ZenohCommand::Ping => {
                    // Respond with pong to indicate we're alive
                    debug!("Worker received ping, sending pong");
                    let _ = event_sender.send(ZenohEvent::Pong);
                }
                }
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                // Normal timeout, continue loop
            }
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                error!("Command channel disconnected, worker thread exiting");
                break;
            }
        }
    }
}

/// Establishes a connection to the Zenoh network with the specified configuration.
/// 
/// # Arguments
/// * `locators` - Comma-separated list of endpoints (e.g., "tcp/localhost:7447")
/// * `mode` - Connection mode: "client" or "peer"
/// * `config_json` - Additional Zenoh configuration in JSON format
/// 
/// # Returns
/// A Zenoh session on success, or an error if connection fails
async fn connect_zenoh(
    locators: &str,
    mode: &str,
    config_json: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!("Attempting to connect - mode: {}, locators: {}", mode, if locators.is_empty() { "(none - using discovery)" } else { locators });
    
    let mut config = zenoh::config::Config::default();

    // Parse and apply any additional configuration provided as JSON
    if !config_json.is_empty() && config_json != "{}" {
        debug!("Parsing additional config: {}", config_json);
        if let Ok(additional_config) = serde_json::from_str::<serde_json::Value>(config_json) {
            if let Ok(zenoh_config) = serde_json::from_value(additional_config) {
                config = zenoh_config;
                debug!("Successfully applied additional config");
            }
        }
    }

    // Configure the connection mode
    // Client: connects to existing routers
    // Peer: participates as a peer in the mesh network
    if mode == "peer" {
        info!("Setting peer mode");
        config.set_mode(Some(WhatAmI::Peer)).unwrap();
        
        // For peer mode, we need to enable scouting
        // This allows peers to discover each other via multicast
        info!("Peer mode - configuring scouting");
        config.scouting.multicast.set_enabled(Some(true)).unwrap();
        config.scouting.gossip.set_enabled(Some(true)).unwrap();
        
        // Set default multicast address
        config.scouting.multicast.set_address(Some("224.0.0.224:7446".parse().unwrap())).unwrap();
        
        // Enable local routing
        // Note: routing.peer.mode is private in zenoh 1.0, skip this configuration
        
        // Add listening endpoints for peer mode if no endpoints specified
        if locators.is_empty() {
            info!("Peer mode - adding default listening endpoints");
            config.listen.endpoints.set(vec![
                "tcp/[::]:0".parse().unwrap(),  // Listen on any available port
            ]).unwrap();
        }
    } else {
        info!("Setting client mode");
        config.set_mode(Some(WhatAmI::Client)).unwrap();
        
        // For client mode, disable scouting to speed up connection
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
    }

    // Parse the locator strings into endpoints
    // Supports multiple endpoints separated by commas
    if !locators.is_empty() {
        debug!("Parsing locators: {}", locators);
        let endpoints: Vec<_> = locators
            .split(',')
            .map(|s| s.trim().parse())
            .collect::<Result<Vec<_>, _>>()?;

        // Apply the endpoints to the configuration
        config.connect.endpoints.set(endpoints.clone()).unwrap();
        info!("Set {} endpoints", endpoints.len());
    } else {
        info!("No locators specified - relying on multicast discovery");
    }

    // Open the Zenoh session with the configured settings
    info!("Opening Zenoh session with mode: {:?}", mode);
    info!("Final config - connect endpoints: {:?}", config.connect.endpoints);
    if mode == "peer" {
        info!("Peer mode - listen endpoints: {:?}", config.listen.endpoints);
        info!("Peer mode - multicast enabled: {:?}", config.scouting.multicast.enabled());
    }
    
    // Use tokio timeout to prevent indefinite hanging
    let open_future = zenoh::open(config);
    info!("Starting Zenoh session open...");
    
    match tokio::time::timeout(std::time::Duration::from_secs(30), open_future).await {
        Ok(Ok(session)) => {
            info!("Successfully connected to Zenoh network in {} mode", mode);
            
            // In peer mode, let's give the session a moment to fully establish
            if mode == "peer" {
                info!("Peer mode: waiting for session to stabilize...");
                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            }
            
            Ok(session)
        }
        Ok(Err(e)) => {
            error!("Failed to connect in {} mode: {}", mode, e);
            Err(format!("Connection failed in {} mode: {}", mode, e).into())
        }
        Err(_) => {
            error!("Connection timeout after 30 seconds in {} mode", mode);
            Err(format!("Connection timeout in {} mode: Unable to establish connection within 30 seconds", mode).into())
        }
    }
}

/// Implementation of the eframe App trait for the main application.
/// This is called on each frame to update the UI.
impl eframe::App for ZenohExplorer {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // First frame debug message and ensure window is visible
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            info!("First UI update frame - window should be visible now");
            // Ensure the window is visible on macOS
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        });
        
        // Process any pending events from the Zenoh worker
        self.process_events();
        
        // Apply theme styling
        self.apply_theme(ctx);

        // Render the main UI panel
        egui::CentralPanel::default()
            .frame(
                egui::Frame::default()
                    .fill(self.background_color())
                    .inner_margin(Margin::same(8.0)),
            )
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.label(
                        RichText::new("Zenoh Explorer")
                            .size(24.0)
                            .color(self.text_color()),
                    );

                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        // Dark mode toggle
                        if ui.button(if self.dark_mode { "☀" } else { "🌙" }).clicked() {
                            self.dark_mode = !self.dark_mode;
                        }
                        
                        ui.separator();
                        
                        // Worker health indicator
                        if !self.worker_healthy {
                            ui.label(
                                RichText::new("⚠ Worker Unresponsive")
                                    .color(ExplorerColors::ERROR)
                                    .small(),
                            );
                            ui.separator();
                        }
                        
                        // Connection status
                        ui.label(
                            RichText::new(format!("● {}", self.connection_status.text()))
                                .color(self.connection_status.color()),
                        );
                        
                        // Memory usage indicator
                        if !self.messages.is_empty() || self.messages_dropped > 0 {
                            ui.separator();
                            
                            let memory_mb = self.current_memory_bytes as f32 / (1024.0 * 1024.0);
                            let memory_percent = (memory_mb / self.max_memory_mb as f32 * 100.0).min(100.0);
                            
                            // Show warning when approaching limit
                            if memory_percent > 80.0 && !self.memory_warning_shown {
                                self.memory_warning_shown = true;
                                self.query_alert = Some("⚠ Memory usage is high (>80%). Messages may start being dropped soon.".to_string());
                            } else if memory_percent < 70.0 {
                                self.memory_warning_shown = false;
                            }
                            
                            let memory_color = if memory_percent > 90.0 {
                                ExplorerColors::ERROR
                            } else if memory_percent > 70.0 {
                                ExplorerColors::WARNING
                            } else {
                                ExplorerColors::SUCCESS
                            };
                            
                            ui.label(
                                RichText::new(format!("Memory: {:.1}MB/{:.0}MB", memory_mb, self.max_memory_mb as f32))
                                    .color(memory_color)
                                    .small()
                            );
                            
                            if self.messages_dropped > 0 || self.rate_limit_drops > 0 {
                                let drop_text = if self.rate_limit_drops > 0 {
                                    format!("({} dropped, {} rate limited)", self.messages_dropped, self.rate_limit_drops)
                                } else {
                                    format!("({} dropped)", self.messages_dropped)
                                };
                                ui.label(
                                    RichText::new(drop_text)
                                        .color(ExplorerColors::WARNING)
                                        .small()
                                );
                            }
                        }
                    });
                });

                ui.separator();

                // Compact connection panel in toolbar
                if matches!(
                    self.connection_status,
                    ConnectionStatus::Disconnected | ConnectionStatus::Error(_)
                ) {
                    ui.group(|ui| {
                        ui.label("Connection Settings");
                        ui.horizontal(|ui| {
                            ui.label("Locators:");
                            ui.text_edit_singleline(&mut self.locators);
                            if self.connection_mode == "peer" && self.locators.is_empty() {
                                ui.label(RichText::new("(using multicast discovery)").small().color(ExplorerColors::TEXT_SECONDARY));
                            }
                        });
                        ui.horizontal(|ui| {
                            ui.label("Mode:");
                            egui::ComboBox::from_id_salt("connection_mode")
                                .selected_text(&self.connection_mode)
                                .show_ui(ui, |ui| {
                                    ui.selectable_value(
                                        &mut self.connection_mode,
                                        "client".to_string(),
                                        "Client",
                                    );
                                    ui.selectable_value(
                                        &mut self.connection_mode,
                                        "peer".to_string(),
                                        "Peer",
                                    );
                                });
                        });
                        
                        // Show helpful tips based on mode
                        if self.connection_mode == "peer" {
                            ui.label(RichText::new("💡 Peer mode: Leave locators empty for multicast discovery, or specify endpoints").small().color(ExplorerColors::TEXT_SECONDARY));
                        } else {
                            ui.label(RichText::new("💡 Client mode: Connects to Zenoh router. Default: tcp/localhost:7447").small().color(ExplorerColors::TEXT_SECONDARY));
                        }
                        
                        // Show error details if connection failed
                        if let ConnectionStatus::Error(ref err) = self.connection_status {
                            ui.colored_label(ExplorerColors::ERROR, format!("❌ Error: {}", err));
                        }
                        
                        if ui.button("Connect").clicked() {
                            if let Some(sender) = &self.command_sender {
                                self.connection_status = ConnectionStatus::Connecting;
                                
                                // Use default locator for client mode if empty
                                let locators = if self.connection_mode == "client" && self.locators.is_empty() {
                                    "tcp/localhost:7447".to_string()
                                } else {
                                    self.locators.clone()
                                };
                                
                                info!("GUI sending Connect command - mode: {}, locators: {}", self.connection_mode, locators);
                                match sender.send(ZenohCommand::Connect {
                                    locators,
                                    mode: self.connection_mode.clone(),
                                    config_json: self.config_json.clone(),
                                }) {
                                    Ok(_) => info!("Connect command sent successfully"),
                                    Err(e) => error!("Failed to send Connect command: {:?}", e),
                                }
                            }
                        }
                    });
                } else {
                    ui.horizontal(|ui| {
                        if ui.button("Disconnect").clicked() {
                            // Update UI state immediately for responsive feedback
                            self.connection_status = ConnectionStatus::Disconnected;
                            self.subscriptions.clear(); // Clear subscriptions immediately
                            if let Some(sender) = &self.command_sender {
                                let _ = sender.send(ZenohCommand::Disconnect);
                            }
                        }
                    });
                }

                ui.separator();

                // Main split-panel layout (MQTT Explorer style)
                egui::TopBottomPanel::top("toolbar").show_inside(ui, |ui| {
                    ui.horizontal(|ui| {
                        ui.label("Quick Actions:");
                        if ui.selectable_label(self.detail_view == DetailView::TopicDetails, "📊 Topics").clicked() {
                            self.detail_view = DetailView::TopicDetails;
                        }
                        if ui.selectable_label(self.detail_view == DetailView::Publish, "📤 Publish").clicked() {
                            self.detail_view = DetailView::Publish;
                        }
                        if ui.selectable_label(self.detail_view == DetailView::Query, "🔍 Query").clicked() {
                            self.detail_view = DetailView::Query;
                        }
                        if ui.selectable_label(self.detail_view == DetailView::Help, "❓ Help").clicked() {
                            self.detail_view = DetailView::Help;
                        }
                    });
                });

                // Split panel layout
                egui::SidePanel::left("tree_panel")
                    .default_width(400.0)
                    .min_width(250.0)
                    .resizable(true)
                    .show_inside(ui, |ui| {
                        self.show_tree_panel(ui);
                    });

                // Right panel shows details based on selected view
                egui::CentralPanel::default().show_inside(ui, |ui| {
                    self.show_detail_panel(ui);
                });
            });

        // Request continuous repaint for real-time message updates
        // This ensures the UI stays responsive to incoming messages
        ctx.request_repaint();
    }
}

impl ZenohExplorer {
    /// Renders the left tree panel (main navigation)
    fn show_tree_panel(&mut self, ui: &mut egui::Ui) {
        ui.vertical(|ui| {
            // Search/filter box
            ui.horizontal(|ui| {
                ui.label("🔍");
                ui.text_edit_singleline(&mut self.tree_filter)
                    .on_hover_text("Filter topics");
                if ui.button("✖").clicked() {
                    self.tree_filter.clear();
                }
            });

            ui.separator();

            // Subscription controls
            ui.collapsing("➕ Subscribe to Topics", |ui| {
                ui.horizontal(|ui| {
                    ui.label("Key:");
                    ui.text_edit_singleline(&mut self.subscribe_key);
                });
                let button = egui::Button::new("Subscribe");
                if ui.add_enabled(
                    matches!(self.connection_status, ConnectionStatus::Connected) && !self.subscribe_key.is_empty(),
                    button
                ).clicked() {
                    if let Some(sender) = &self.command_sender {
                        let _ = sender.send(ZenohCommand::Subscribe {
                            key_expr: self.subscribe_key.clone(),
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                }

                // Active subscriptions
                if !self.subscriptions.is_empty() {
                    ui.label(RichText::new("Active:").small());
                    for subscription in &self.subscriptions.clone() {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new(&subscription.key_expr).small());
                            if ui.small_button("✖").clicked() {
                                if let Some(sender) = &self.command_sender {
                                    let _ = sender.send(ZenohCommand::Unsubscribe {
                                        subscription_id: subscription.id.clone(),
                                    });
                                }
                            }
                        });
                    }
                }
            });

            ui.separator();

            // Topic tree
            ui.label(RichText::new("Topics").strong());

            egui::ScrollArea::vertical()
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    let tree_clone = if let Ok(tree) = self.browse_tree.read() {
                        tree.clone()
                    } else {
                        ZenohNode::new("root".to_string())
                    };

                    if tree_clone.children.is_empty() {
                        ui.label(RichText::new("No topics yet. Subscribe to see data.").italics().color(ExplorerColors::TEXT_SECONDARY));
                    } else {
                        for (_, child) in &tree_clone.children {
                            self.show_tree_node(ui, child, String::new(), 0);
                        }
                    }
                });
        });
    }

    /// Renders the right detail panel based on current view mode
    fn show_detail_panel(&mut self, ui: &mut egui::Ui) {
        match self.detail_view {
            DetailView::TopicDetails => self.show_topic_details(ui),
            DetailView::Publish => self.show_publish_tab(ui),
            DetailView::Query => self.show_query_tab(ui),
            DetailView::Help => self.show_help_tab(ui),
        }
    }

    /// Shows details for the selected topic
    fn show_topic_details(&mut self, ui: &mut egui::Ui) {
        if let Some(ref topic) = self.selected_topic.clone() {
            ui.heading(topic);
            ui.separator();

            // Get the node details
            if let Ok(tree) = self.browse_tree.read() {
                if let Some(node) = self.find_node(&tree, topic) {
                    // Show node metadata
                    ui.horizontal(|ui| {
                        ui.label(RichText::new("Messages:").strong());
                        ui.label(node.message_count.to_string());
                    });

                    if let Some(ref payload) = node.last_payload {
                        ui.separator();
                        ui.label(RichText::new("Current Value:").strong());

                        // Try to parse and format as JSON
                        if let Ok(json_value) = serde_json::from_str::<serde_json::Value>(payload) {
                            if let Ok(pretty) = serde_json::to_string_pretty(&json_value) {
                                egui::ScrollArea::vertical().show(ui, |ui| {
                                    ui.code(&pretty);
                                });
                            } else {
                                ui.code(payload);
                            }
                        } else {
                            egui::ScrollArea::vertical().show(ui, |ui| {
                                ui.code(payload);
                            });
                        }

                        if let Some(ref encoding) = node.last_encoding {
                            ui.separator();
                            ui.horizontal(|ui| {
                                ui.label(RichText::new("Encoding:").strong());
                                ui.label(encoding);
                            });
                        }
                    }

                    ui.separator();

                    // Show message history for this topic
                    ui.label(RichText::new("Message History:").strong());
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        let topic_messages: Vec<_> = self.messages.iter()
                            .filter(|m| m.key == *topic)
                            .rev()
                            .take(50)
                            .collect();

                        if topic_messages.is_empty() {
                            ui.label(RichText::new("No messages yet").italics().color(ExplorerColors::TEXT_SECONDARY));
                        } else {
                            for message in topic_messages {
                                ui.group(|ui| {
                                    ui.horizontal(|ui| {
                                        ui.label(
                                            RichText::new(message.timestamp.format("%H:%M:%S%.3f").to_string())
                                                .color(ExplorerColors::TEXT_SECONDARY)
                                                .small(),
                                        );
                                        ui.label(
                                            RichText::new(message.message_type.label())
                                                .background_color(message.message_type.color())
                                                .color(Color32::WHITE)
                                                .small(),
                                        );
                                    });

                                    if !message.payload.is_empty() {
                                        let display_payload = if message.payload.len() > 200 {
                                            format!("{}...", &message.payload[..200])
                                        } else {
                                            message.payload.clone()
                                        };
                                        ui.label(
                                            RichText::new(display_payload)
                                                .color(ExplorerColors::TEXT_SECONDARY)
                                                .small(),
                                        );
                                    }
                                });
                            }
                        }
                    });
                }
            }
        } else {
            // No topic selected - show all messages
            ui.heading("All Messages");
            ui.separator();

            self.show_messages_tab(ui);
        }
    }

    /// Helper to find a node by full path
    fn find_node<'a>(&self, node: &'a ZenohNode, path: &str) -> Option<&'a ZenohNode> {
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

    /// Renders a tree node with improved MQTT Explorer-style visualization
    fn show_tree_node(&mut self, ui: &mut egui::Ui, node: &ZenohNode, parent_path: String, depth: usize) {
        // Build the full path for this node
        let full_path = if parent_path.is_empty() {
            node.key.clone()
        } else {
            format!("{}/{}", parent_path, node.key)
        };

        // Apply filter
        if !self.tree_filter.is_empty() && !full_path.contains(&self.tree_filter) {
            // Check if any children match
            let has_matching_child = self.has_matching_descendant(node, &self.tree_filter, &full_path);
            if !has_matching_child {
                return;
            }
        }

        let indent = 12.0 * depth as f32;
        let is_selected = self.selected_topic.as_ref().map_or(false, |t| t == &full_path);

        if node.children.is_empty() {
            // Leaf node - show as selectable in horizontal layout
            ui.horizontal(|ui| {
                ui.add_space(indent);
                let response = ui.selectable_label(is_selected, format!("📄 {}", node.key));

                if response.clicked() {
                    self.selected_topic = Some(full_path.clone());
                    self.detail_view = DetailView::TopicDetails;
                }

                // Show message count badge
                if node.message_count > 0 {
                    ui.label(
                        RichText::new(format!("({})", node.message_count))
                            .small()
                            .color(ExplorerColors::PRIMARY)
                    );
                }

                // Show preview of last value
                if let Some(ref payload) = node.last_payload {
                    let preview = if payload.len() > 30 {
                        format!("{}...", &payload[..30])
                    } else {
                        payload.clone()
                    };
                    ui.label(
                        RichText::new(preview)
                            .small()
                            .color(ExplorerColors::TEXT_SECONDARY)
                    );
                }
            });
        } else {
            // Branch node - collapsible (NOT inside horizontal layout)
            let id = egui::Id::new(format!("treenode_{}", full_path));
            let state = egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                false
            );

            ui.add_space(indent);
            let header_response = state.show_header(ui, |ui| {
                let response = ui.selectable_label(is_selected, format!("📁 {}", node.key));

                if response.clicked() {
                    self.selected_topic = Some(full_path.clone());
                    self.detail_view = DetailView::TopicDetails;
                }

                // Show child count
                ui.label(
                    RichText::new(format!("({})", node.children.len()))
                        .small()
                        .color(ExplorerColors::TEXT_TERTIARY)
                );
            });

            header_response.body(|ui| {
                for (_, child) in &node.children {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }

    /// Check if node or any descendant matches filter
    fn has_matching_descendant(&self, node: &ZenohNode, filter: &str, current_path: &str) -> bool {
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

    /// Renders the Publish tab UI.
    /// Allows users to send data to any key in the Zenoh network.
    fn show_publish_tab(&mut self, ui: &mut egui::Ui) {
        // Show warning if not connected
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(ExplorerColors::ERROR, "⚠ Not connected. Please connect first.");
            ui.separator();
        }
        ui.group(|ui| {
            ui.label("Publish Data");
            ui.horizontal(|ui| {
                ui.label("Key:");
                ui.text_edit_singleline(&mut self.publish_key);
            });
            ui.horizontal(|ui| {
                ui.label("Payload:");
                ui.text_edit_multiline(&mut self.publish_payload);
            });
            ui.horizontal(|ui| {
                ui.label("Encoding:");
                ui.text_edit_singleline(&mut self.publish_encoding);
            });
            // Publish button - only enabled when connected
            let button = egui::Button::new("Publish");
            if ui.add_enabled(
                matches!(self.connection_status, ConnectionStatus::Connected) && !self.publish_key.is_empty(),
                button
            ).clicked() {
                if let Some(sender) = &self.command_sender {
                    let _ = sender.send(ZenohCommand::Publish {
                        key: self.publish_key.clone(),
                        payload: self.publish_payload.clone(),
                        encoding: self.publish_encoding.clone(),
                    });
                }
            }
        });
    }

    /// Renders the Query tab UI.
    /// Allows users to request data from the network using selectors.
    fn show_query_tab(&mut self, ui: &mut egui::Ui) {
        // Show warning if not connected
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            ui.colored_label(ExplorerColors::ERROR, "⚠ Not connected. Please connect first.");
            ui.separator();
        }
        
        // Explain query functionality
        ui.label(RichText::new("ℹ Note: Queries require queryables (services) running on the network to respond.").color(ExplorerColors::TEXT_SECONDARY).small());
        ui.label(RichText::new("If no queryables are running, queries will timeout with no results.").color(ExplorerColors::TEXT_SECONDARY).small());
        ui.separator();
        
        // Show query alert if present
        if self.query_alert.is_some() {
            let mut dismiss = false;
            ui.group(|ui| {
                ui.colored_label(ExplorerColors::WARNING, "⚠ Query Alert");
                if let Some(alert) = &self.query_alert {
                    ui.label(alert);
                }
                if ui.button("Dismiss").clicked() {
                    dismiss = true;
                }
            });
            if dismiss {
                self.query_alert = None;
            }
            ui.separator();
        }
        ui.group(|ui| {
            ui.label("Query Data");
            ui.horizontal(|ui| {
                ui.label("Selector:");
                ui.text_edit_singleline(&mut self.query_selector);
            });
            ui.horizontal(|ui| {
                ui.label("Value (optional):");
                ui.text_edit_singleline(&mut self.query_value);
            });
            ui.horizontal(|ui| {
                ui.label("Timeout (ms):");
                ui.text_edit_singleline(&mut self.query_timeout);
            });
            // Query button - only enabled when connected
            let button = egui::Button::new("Query");
            if ui.add_enabled(
                matches!(self.connection_status, ConnectionStatus::Connected) && !self.query_selector.is_empty(),
                button
            ).clicked() {
                if let Some(sender) = &self.command_sender {
                    let timeout = self.query_timeout.parse().unwrap_or(10000);
                    let _ = sender.send(ZenohCommand::Query {
                        selector: self.query_selector.clone(),
                        value: self.query_value.clone(),
                        timeout_ms: timeout,
                    });
                }
            }
        });
    }


    /// Renders the Messages tab UI.
    /// Shows all network activity with filtering and auto-scroll capabilities.
    fn show_messages_tab(&mut self, ui: &mut egui::Ui) {
        // Message controls toolbar
        ui.horizontal(|ui| {
            ui.label("Filter:");
            ui.text_edit_singleline(&mut self.message_filter);
            ui.checkbox(&mut self.auto_scroll, "Auto-scroll");
            if ui.button("Clear").clicked() {
                self.messages.clear();
                self.current_memory_bytes = 0;
                self.messages_dropped = 0;
                self.rate_limit_drops = 0;
            }
            
            ui.separator();
            ui.label(format!("Messages: {}", self.messages.len()));
        });
        
        // Memory management controls
        ui.horizontal(|ui| {
            ui.label("Memory Limit (MB):");
            let mut limit_str = self.max_memory_mb.to_string();
            if ui.text_edit_singleline(&mut limit_str).changed() {
                if let Ok(new_limit) = limit_str.parse::<usize>() {
                    self.max_memory_mb = new_limit.max(10).min(1000); // Clamp between 10MB and 1GB
                }
            }
            
            ui.label("Message Limit:");
            let mut count_str = self.max_messages.to_string();
            if ui.text_edit_singleline(&mut count_str).changed() {
                if let Ok(new_limit) = count_str.parse::<usize>() {
                    self.max_messages = new_limit.max(100).min(50000); // Clamp between 100 and 50k
                }
            }
            
            ui.label("Rate Limit (msg/s):");
            let mut rate_str = self.rate_limiter.max_messages_per_second.to_string();
            if ui.text_edit_singleline(&mut rate_str).changed() {
                if let Ok(new_rate) = rate_str.parse::<usize>() {
                    self.rate_limiter.max_messages_per_second = new_rate.max(10).min(10000); // 10-10k msg/s
                }
            }
        });

        egui::ScrollArea::vertical()
            .auto_shrink([false; 2])
            .stick_to_bottom(self.auto_scroll)
            .show(ui, |ui| {
                for message in &self.messages {
                    if self.message_filter.is_empty()
                        || message.key.contains(&self.message_filter)
                        || message.payload.contains(&self.message_filter)
                    {
                        ui.horizontal(|ui| {
                            // Message type badge
                            ui.label(
                                RichText::new(message.message_type.label())
                                    .background_color(message.message_type.color())
                                    .color(Color32::WHITE)
                                    .small(),
                            );

                            // Timestamp
                            ui.label(
                                RichText::new(message.timestamp.format("%H:%M:%S%.3f").to_string())
                                    .color(ExplorerColors::TEXT_SECONDARY)
                                    .small(),
                            );

                            // Key
                            ui.label(RichText::new(&message.key).strong());
                        });

                        // Payload (truncated)
                        if !message.payload.is_empty() {
                            let display_payload = if message.payload.len() > 200 {
                                format!("{}...", &message.payload[..200])
                            } else {
                                message.payload.clone()
                            };
                            ui.label(
                                RichText::new(display_payload)
                                    .color(ExplorerColors::TEXT_SECONDARY)
                                    .small(),
                            );
                        }

                        ui.separator();
                    }
                }
            });
    }

    /// Renders the Help tab UI.
    /// Provides usage instructions and examples for new users.
    fn show_help_tab(&mut self, ui: &mut egui::Ui) {
        ui.label(RichText::new("Zenoh Explorer Help").size(18.0).strong());
        ui.separator();

        ui.label("This is a standalone Zenoh network explorer for debugging and monitoring Zenoh networks.");
        ui.separator();

        ui.label(RichText::new("Getting Started:").strong());
        ui.label("1. Configure connection settings and click Connect");
        ui.label("2. Use Subscribe tab to listen to key expressions");
        ui.label("3. Use Publish tab to send data");
        ui.label("4. Use Query tab to request data (requires queryables)");
        ui.label("5. Use Browse tab to explore the network topology");
        ui.label("6. Use Messages tab to see all network activity");
        
        ui.separator();
        ui.label(RichText::new("Connection Modes:").strong());
        ui.label("• Client Mode: Connect to existing Zenoh routers (recommended)");
        ui.label("• Peer Mode: Participate as a peer in the mesh network");
        
        ui.separator();
        ui.label(RichText::new("About Queries:").strong());
        ui.label("Queries in Zenoh work differently than pub/sub:");
        ui.label("• They require queryable services to be running");
        ui.label("• Queryables actively respond to query requests");
        ui.label("• Without queryables, queries will timeout");
        ui.label("• Use Subscribe instead for passive data monitoring");
        
        ui.separator();
        ui.label(RichText::new("Performance Tips:").strong());
        ui.label("• The explorer limits memory usage to prevent crashes");
        ui.label("• Adjust memory limit in Messages tab (default: 100MB)");
        ui.label("• Older messages are dropped when limits are exceeded");
        ui.label("• Use filters to reduce message volume");
        ui.label("• Large messages are automatically truncated to 10MB");
        ui.label("• Rate limiting prevents flooding (default: 1000 msg/s)");

        ui.separator();
        ui.label(RichText::new("Key Expression Examples:").strong());
        ui.label("• demo/** - Match all keys under demo/");
        ui.label("• sensor/*/temperature - Match temperature under any sensor");
        ui.label("• device/1/status - Match exact key");
    }
}
