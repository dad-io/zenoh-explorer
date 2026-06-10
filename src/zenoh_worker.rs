use chrono::Utc;
use std::collections::HashMap;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, RwLock};
use tokio::sync::oneshot;
use tracing::{debug, error, info};
use zenoh::config::WhatAmI;
use zenoh::Session;

use crate::types::*;

pub fn message_buffer_thread(buffer_receiver: Receiver<ZenohEvent>, ui_sender: Sender<ZenohEvent>) {
    info!("Message buffer thread started");

    let batch_interval = std::time::Duration::from_millis(16); // ~60fps
    let mut message_buffer: Vec<ZenohMessage> = Vec::with_capacity(100);

    loop {
        let deadline = std::time::Instant::now() + batch_interval;

        // Collect messages until deadline or batch size limit
        while std::time::Instant::now() < deadline {
            match buffer_receiver.recv_timeout(std::time::Duration::from_millis(1)) {
                Ok(event) => {
                    match event {
                        ZenohEvent::MessageReceived(msg) => {
                            message_buffer.push(msg);
                            // Flush if batch gets large
                            if message_buffer.len() >= 50 {
                                break;
                            }
                        }
                        // Pass through non-message events immediately
                        other_event => {
                            // Flush any pending messages first
                            if !message_buffer.is_empty() {
                                let batch =
                                    std::mem::replace(&mut message_buffer, Vec::with_capacity(100));
                                let _ = ui_sender.send(ZenohEvent::MessageBatch(batch));
                            }
                            let _ = ui_sender.send(other_event);
                        }
                    }
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    // Timeout, flush batch
                    break;
                }
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    // Worker disconnected, flush and exit
                    if !message_buffer.is_empty() {
                        let batch = std::mem::take(&mut message_buffer);
                        let _ = ui_sender.send(ZenohEvent::MessageBatch(batch));
                    }
                    info!("Message buffer thread exiting - worker disconnected");
                    return;
                }
            }
        }

        // Flush accumulated messages as batch
        if !message_buffer.is_empty() {
            let batch = std::mem::replace(&mut message_buffer, Vec::with_capacity(100));
            if ui_sender.send(ZenohEvent::MessageBatch(batch)).is_err() {
                info!("Message buffer thread exiting - UI disconnected");
                return;
            }
        }
    }
}

/// Worker function that handles all Zenoh operations in a separate async task.
/// This prevents blocking the GUI thread and enables clean cancellation of operations.
///
/// # Arguments
/// * `command_receiver` - Channel to receive commands from the GUI thread
/// * `event_sender` - Channel to send events back to the GUI thread
/// * `local_kvstore` - Shared key-value store for queryable responses
pub async fn zenoh_worker(
    command_receiver: Receiver<ZenohCommand>,
    event_sender: Sender<ZenohEvent>,
    local_kvstore: Arc<RwLock<HashMap<String, (String, String)>>>,
) {
    info!("Zenoh worker thread started");

    // Dual session architecture:
    // - publishing_session: handles user's explicit subscribe/publish/query operations
    // - monitor_session: auto-subscribes to ** to observe actual wire traffic
    let mut publishing_session: Option<Arc<Session>> = None;
    let mut monitor_session: Option<Arc<Session>> = None;
    // Map of active subscriptions by ID for management (user subscriptions on publishing session)
    let mut active_subscriptions: HashMap<String, ActiveSubscription> = HashMap::new();
    // Monitor session's ** subscription (background traffic observation)
    let mut monitor_subscription: Option<ActiveSubscription> = None;
    // Active queryable and its associated task
    let mut queryable_task: Option<(tokio::task::JoinHandle<()>, tokio::sync::mpsc::Sender<()>)> =
        None;

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
                        listen_port,
                        mode,
                        config_json,
                    } => {
                        info!(
                            "Worker processing connect command - mode: {}, locators: {}, listen_port: {}",
                            mode, locators, listen_port
                        );

                        // Phase 1: Connect the publishing session
                        match connect_zenoh(&locators, &listen_port, &mode, &config_json).await {
                            Ok(new_session) => {
                                info!("Worker successfully created publishing session");
                                let session_arc = Arc::new(new_session);
                                publishing_session = Some(session_arc.clone());

                                // Send PublishingConnected event
                                match event_sender.send(ZenohEvent::PublishingConnected) {
                                    Ok(_) => {
                                        info!("Successfully sent PublishingConnected event to GUI")
                                    }
                                    Err(e) => {
                                        error!("Failed to send PublishingConnected event: {:?}", e)
                                    }
                                }

                                // Spawn a separate discovery thread to monitor peers/routers
                                let discovery_session = session_arc.clone();
                                let discovery_sender = event_sender.clone();
                                std::thread::spawn(move || {
                                    // Zenoh requires multi-thread runtime
                                    let rt = tokio::runtime::Builder::new_multi_thread()
                                        .worker_threads(1)
                                        .enable_all()
                                        .build()
                                        .unwrap();

                                    rt.block_on(async {
                                        loop {
                                            // Count peers
                                            let mut peers_count = 0;
                                            let mut peers_iter =
                                                discovery_session.info().peers_zid().await;
                                            while peers_iter.next().is_some() {
                                                peers_count += 1;
                                            }

                                            // Count routers
                                            let mut routers_count = 0;
                                            let mut routers_iter =
                                                discovery_session.info().routers_zid().await;
                                            while routers_iter.next().is_some() {
                                                routers_count += 1;
                                            }

                                            // Send update to UI
                                            if discovery_sender
                                                .send(ZenohEvent::DiscoveryUpdate {
                                                    peers: peers_count,
                                                    routers: routers_count,
                                                })
                                                .is_err()
                                            {
                                                // Channel closed, exit thread
                                                break;
                                            }

                                            // Poll every 2 seconds
                                            tokio::time::sleep(std::time::Duration::from_secs(2))
                                                .await;
                                        }
                                    });
                                });

                                // Phase 2: Connect the monitor session with scouting disabled
                                // Use listen_port + 1000 to avoid port conflicts in peer mode
                                let monitor_port =
                                    listen_port.parse::<u16>().unwrap_or(7447) + 1000;
                                info!("Connecting monitor session on port {}", monitor_port);

                                match connect_zenoh_monitor(
                                    &locators,
                                    &monitor_port.to_string(),
                                    &mode,
                                )
                                .await
                                {
                                    Ok(mon_session) => {
                                        info!("Worker successfully created monitor session");
                                        let mon_session_arc = Arc::new(mon_session);
                                        monitor_session = Some(mon_session_arc.clone());

                                        // Auto-subscribe monitor to ** (all topics)
                                        match mon_session_arc.declare_subscriber("**").await {
                                            Ok(subscriber) => {
                                                info!("Monitor session subscribed to **");
                                                let event_sender_clone = event_sender.clone();
                                                let (cancel_sender, mut cancel_receiver) =
                                                    oneshot::channel();

                                                // Spawn task to handle monitor subscription messages
                                                let task_handle = tokio::spawn(async move {
                                                    loop {
                                                        tokio::select! {
                                                            _ = &mut cancel_receiver => {
                                                                info!("Monitor subscription cancelled");
                                                                break;
                                                            }
                                                            result = subscriber.recv_async() => {
                                                                match result {
                                                                    Ok(sample) => {
                                                                        debug!("Monitor received sample on key: {}", sample.key_expr());
                                                                        let raw_bytes: Vec<u8> = sample.payload().to_bytes().to_vec();

                                                                        let payload_display =
                                                                            match sample.payload().try_to_string() {
                                                                                Ok(s) => s.into_owned(),
                                                                                Err(_) => {
                                                                                    let hex: Vec<String> = raw_bytes.iter().take(256).map(|b| format!("{:02x}", b)).collect();
                                                                                    if raw_bytes.len() > 256 {
                                                                                        format!("[binary {} bytes] {}...", raw_bytes.len(), hex.join(" "))
                                                                                    } else {
                                                                                        format!("[binary {} bytes] {}", raw_bytes.len(), hex.join(" "))
                                                                                    }
                                                                                }
                                                                            };

                                                                        let filename = sample
                                                                            .attachment()
                                                                            .and_then(|a| a.try_to_string().ok())
                                                                            .map(|s| s.into_owned());

                                                                        let message = ZenohMessage::new_with_bytes(
                                                                            sample.key_expr().to_string(),
                                                                            payload_display,
                                                                            raw_bytes,
                                                                            "text/plain".to_string(),
                                                                            Utc::now(),
                                                                            MessageType::Subscribe,
                                                                            false,  // Monitor messages are always from remote
                                                                            MessageSource::MonitorSession,
                                                                        )
                                                                        .with_filename(filename);

                                                                        let _ = event_sender_clone
                                                                            .send(ZenohEvent::MessageReceived(message));
                                                                    }
                                                                    Err(e) => {
                                                                        error!("Monitor subscriber recv error: {:?}", e);
                                                                        break;
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                });

                                                monitor_subscription = Some(ActiveSubscription {
                                                    key_expr: "**".to_string(),
                                                    task_handle,
                                                    cancel_sender,
                                                });
                                            }
                                            Err(e) => {
                                                error!("Failed to subscribe monitor to **: {}", e);
                                            }
                                        }

                                        // Send MonitorConnected event (both sessions now ready)
                                        match event_sender.send(ZenohEvent::MonitorConnected) {
                                            Ok(_) => info!(
                                                "Successfully sent MonitorConnected event to GUI"
                                            ),
                                            Err(e) => error!(
                                                "Failed to send MonitorConnected event: {:?}",
                                                e
                                            ),
                                        }
                                    }
                                    Err(e) => {
                                        // Monitor session failed, but publishing session is still usable
                                        error!("Failed to connect monitor session: {}", e);
                                        // Still send Connected since publishing session works
                                        match event_sender.send(ZenohEvent::MonitorConnected) {
                                            Ok(_) => info!("Sent MonitorConnected event (monitor failed but publishing works)"),
                                            Err(send_err) => error!("Failed to send MonitorConnected: {:?}", send_err),
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                error!("Worker failed to connect publishing session: {}", e);
                                match event_sender.send(ZenohEvent::ConnectionError(e.to_string()))
                                {
                                    Ok(_) => info!("Sent ConnectionError event to GUI"),
                                    Err(send_err) => error!(
                                        "Failed to send ConnectionError event: {:?}",
                                        send_err
                                    ),
                                }
                            }
                        }
                    }
                    ZenohCommand::Disconnect => {
                        // Clean shutdown process for dual-session architecture:

                        // 1. Cancel monitor subscription first
                        if let Some(sub) = monitor_subscription.take() {
                            let _ = sub.cancel_sender.send(());
                            sub.task_handle.abort();
                            info!("Monitor subscription cancelled");
                        }

                        // 2. Cancel all active user subscriptions gracefully
                        for (_, subscription) in active_subscriptions.drain() {
                            // Send cancellation signal (ignore if already cancelled)
                            let _ = subscription.cancel_sender.send(());
                            // Abort the task as backup
                            subscription.task_handle.abort();
                        }

                        // 3. Close the monitor session first (it depends on nothing)
                        if let Some(s) = monitor_session.take() {
                            let _ = s.close().await;
                            info!("Monitor session closed");
                        }

                        // 4. Close the publishing session
                        if let Some(s) = publishing_session.take() {
                            let _ = s.close().await;
                            info!("Publishing session closed");
                        }

                        // 5. Notify GUI of disconnection
                        let _ = event_sender.send(ZenohEvent::Disconnected);
                    }
                    ZenohCommand::Subscribe {
                        key_expr,
                        reliability: _, // TODO: Implement reliability configuration
                        mode: _,        // TODO: Implement mode configuration
                    } => {
                        if let Some(ref sess) = publishing_session {
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
                                                            info!("Subscriber received sample on key: {}", sample.key_expr());
                                                            // Get raw bytes for export
                                                            let raw_bytes: Vec<u8> = sample.payload().to_bytes().to_vec();

                                                            // Attempt to decode payload as string for display
                                                            // Fall back to hex dump for binary data
                                                            let payload_display =
                                                                match sample.payload().try_to_string() {
                                                                    Ok(s) => s.into_owned(),
                                                                    Err(_) => {
                                                                        // Show hex bytes for binary data
                                                                        let hex: Vec<String> = raw_bytes.iter().take(256).map(|b| format!("{:02x}", b)).collect();
                                                                        if raw_bytes.len() > 256 {
                                                                            format!("[binary {} bytes] {}...", raw_bytes.len(), hex.join(" "))
                                                                        } else {
                                                                            format!("[binary {} bytes] {}", raw_bytes.len(), hex.join(" "))
                                                                        }
                                                                    }
                                                                };

                                                            let filename = sample
                                                                .attachment()
                                                                .and_then(|a| a.try_to_string().ok())
                                                                .map(|s| s.into_owned());

                                                            let message = ZenohMessage::new_with_bytes(
                                                                sample.key_expr().to_string(),
                                                                payload_display,
                                                                raw_bytes,
                                                                "text/plain".to_string(),
                                                                Utc::now(),
                                                                MessageType::Subscribe,
                                                                false,  // Subscription messages are always from remote
                                                                MessageSource::PublishingSession,
                                                            )
                                                            .with_filename(filename);

                                                            match event_sender_clone
                                                                .send(ZenohEvent::MessageReceived(message)) {
                                                                Ok(_) => info!("Sent MessageReceived event to GUI"),
                                                                Err(e) => error!("Failed to send MessageReceived: {:?}", e),
                                                            }
                                                        }
                                                        Err(e) => {
                                                            error!("Subscriber recv error: {:?}", e);
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
                        from_import,
                        filename,
                    } => {
                        if let Some(ref sess) = publishing_session {
                            // Generate display string - only preview, never clone full payload
                            let payload_str = {
                                let preview_len = payload.len().min(256);
                                match std::str::from_utf8(&payload[..preview_len]) {
                                    Ok(text) if payload.len() <= 256 => text.to_string(),
                                    Ok(text) => format!(
                                        "{}... [+{} bytes]",
                                        text,
                                        payload.len() - preview_len
                                    ),
                                    Err(_) => {
                                        // Binary - show hex preview
                                        let hex: Vec<String> = payload[..preview_len]
                                            .iter()
                                            .map(|b| format!("{:02x}", b))
                                            .collect();
                                        if payload.len() > 256 {
                                            format!(
                                                "[binary {} bytes] {}...",
                                                payload.len(),
                                                hex.join(" ")
                                            )
                                        } else {
                                            format!(
                                                "[binary {} bytes] {}",
                                                payload.len(),
                                                hex.join(" ")
                                            )
                                        }
                                    }
                                }
                            };

                            // Store in local kvstore for queryable responses (only store small payloads)
                            // Skip storage for imported files - they are ephemeral and shouldn't persist in memory
                            if !from_import && payload.len() <= 10 * 1024 * 1024 {
                                // 10MB limit for kvstore
                                if let Ok(mut store) = local_kvstore.write() {
                                    store.insert(
                                        key.clone(),
                                        (payload_str.clone(), encoding.clone()),
                                    );
                                }
                            }

                            // Publish raw bytes to the Zenoh network
                            // Use Block congestion control for large payloads to ensure delivery
                            let payload_len = payload.len();

                            // Zenoh's Put codec uses Zenoh080Bounded::<u32> for payload size encoding,
                            // which limits payloads to u32::MAX (~4GB). For payloads >= 4GB,
                            // we must chunk them at the application level.
                            // Using 64MB chunks to avoid overwhelming zenoh's transport queue.
                            // Larger chunks cause "Unable to push non droppable network message" errors.
                            const CHUNK_SIZE: usize = 64 * 1024 * 1024; // 64MB chunks
                            const MAX_SINGLE_PAYLOAD: usize = 0xFFFF_FFFF; // u32::MAX (~4GB)

                            if payload_len > MAX_SINGLE_PAYLOAD {
                                // Large payload - send in chunks
                                let total_chunks = payload_len.div_ceil(CHUNK_SIZE);
                                info!(
                                    "Chunking {} byte payload into {} chunks of {}MB each",
                                    payload_len,
                                    total_chunks,
                                    CHUNK_SIZE / 1024 / 1024
                                );

                                let mut chunk_num = 0;
                                let mut offset = 0;
                                let mut all_ok = true;

                                while offset < payload_len {
                                    let end = std::cmp::min(offset + CHUNK_SIZE, payload_len);
                                    let chunk = payload[offset..end].to_vec();
                                    let chunk_key = format!(
                                        "{}/__chunk/{}/{}/{}",
                                        key, payload_len, total_chunks, chunk_num
                                    );

                                    let mut put = sess
                                        .put(&chunk_key, chunk)
                                        .encoding(&encoding as &str)
                                        .congestion_control(zenoh::qos::CongestionControl::Block);
                                    if let Some(ref name) = filename {
                                        put = put.attachment(name.as_bytes().to_vec());
                                    }
                                    match put.await {
                                        Ok(_) => info!(
                                            "Published chunk {}/{} ({} bytes) to {}",
                                            chunk_num + 1,
                                            total_chunks,
                                            end - offset,
                                            chunk_key
                                        ),
                                        Err(e) => {
                                            error!(
                                                "Failed to publish chunk {} to {}: {}",
                                                chunk_num, chunk_key, e
                                            );
                                            all_ok = false;
                                            break;
                                        }
                                    }

                                    offset = end;
                                    chunk_num += 1;
                                }

                                if all_ok {
                                    info!(
                                        "Successfully published all {} chunks for {}",
                                        total_chunks, key
                                    );
                                }
                            } else if payload_len > 100 * 1024 * 1024 {
                                // 100MB threshold for no-echo
                                let mut put = sess
                                    .put(&key, payload) // Move directly, no clone
                                    .encoding(&encoding as &str)
                                    .congestion_control(zenoh::qos::CongestionControl::Block);
                                if let Some(ref name) = filename {
                                    put = put.attachment(name.as_bytes().to_vec());
                                }
                                match put.await {
                                    Ok(_) => info!(
                                        "Published {} bytes to {} (large payload, no echo)",
                                        payload_len, key
                                    ),
                                    Err(e) => error!("Failed to publish to {}: {}", key, e),
                                }
                                // Don't echo - too large. Subscription will receive it.
                            } else if from_import {
                                // Imported file - publish but don't store/echo to free memory immediately
                                let mut put = sess
                                    .put(&key, payload) // Move directly, no clone needed
                                    .encoding(&encoding as &str)
                                    .congestion_control(zenoh::qos::CongestionControl::Block);
                                if let Some(ref name) = filename {
                                    put = put.attachment(name.as_bytes().to_vec());
                                }
                                match put.await {
                                    Ok(_) => info!(
                                        "Published {} bytes to {} (imported file, no storage)",
                                        payload_len, key
                                    ),
                                    Err(e) => error!("Failed to publish to {}: {}", key, e),
                                }
                                // Don't echo back - imported files are ephemeral, memory freed after publish
                            } else {
                                let mut put = sess
                                    .put(&key, payload.clone())
                                    .encoding(&encoding as &str)
                                    .congestion_control(zenoh::qos::CongestionControl::Block);
                                if let Some(ref name) = filename {
                                    put = put.attachment(name.as_bytes().to_vec());
                                }
                                match put.await {
                                    Ok(_) => info!("Published {} bytes to {}", payload_len, key),
                                    Err(e) => error!("Failed to publish to {}: {}", key, e),
                                }

                                // Echo the published message back to the UI with raw bytes preserved
                                let message = ZenohMessage::new_with_bytes(
                                    key.clone(),
                                    payload_str,
                                    payload, // Move, not clone
                                    encoding,
                                    Utc::now(),
                                    MessageType::Publish,
                                    true, // Published from this app, so it's local
                                    MessageSource::LocalEcho,
                                )
                                .with_filename(filename.clone());

                                let _ = event_sender.send(ZenohEvent::MessageReceived(message));
                            }
                        }
                    }
                    ZenohCommand::Query {
                        selector,
                        value,
                        timeout_ms,
                    } => {
                        if let Some(ref sess) = publishing_session {
                            info!("Sending query for selector: {}", selector);
                            let mut get_builder = sess.get(&selector);

                            if !value.is_empty() {
                                get_builder = get_builder.payload(value);
                            }

                            // Use All target with no consolidation to get all replies including local
                            get_builder = get_builder
                                .target(zenoh::query::QueryTarget::All)
                                .consolidation(zenoh::query::ConsolidationMode::None);

                            info!("Calling get_builder.timeout().await...");
                            match get_builder
                                .timeout(std::time::Duration::from_millis(timeout_ms))
                                .await
                            {
                                Ok(replies) => {
                                    info!("Query sent successfully (target=All, consolidation=None), waiting for replies...");

                                    let event_sender_query = event_sender.clone();
                                    let selector_clone = selector.clone();
                                    tokio::spawn(async move {
                                        let mut received_replies = false;
                                        while let Ok(reply) = replies.recv_async().await {
                                            info!("Received a reply from query");
                                            match reply.result() {
                                                Ok(sample) => {
                                                    received_replies = true;
                                                    info!(
                                                        "Query reply OK: key={}",
                                                        sample.key_expr()
                                                    );

                                                    // Check if reply is from local queryable via attachment
                                                    let is_local = sample
                                                        .attachment()
                                                        .and_then(|att| att.try_to_string().ok())
                                                        .map(|att_str| {
                                                            att_str.contains("source:local")
                                                        })
                                                        .unwrap_or(false);

                                                    info!("Query reply is_local={}", is_local);

                                                    // Get raw bytes for export
                                                    let raw_bytes: Vec<u8> =
                                                        sample.payload().to_bytes().to_vec();

                                                    let payload =
                                                        match sample.payload().try_to_string() {
                                                            Ok(s) => s.into_owned(),
                                                            Err(_) => {
                                                                // Show hex bytes for binary data
                                                                let hex: Vec<String> = raw_bytes
                                                                    .iter()
                                                                    .take(256)
                                                                    .map(|b| format!("{:02x}", b))
                                                                    .collect();
                                                                if raw_bytes.len() > 256 {
                                                                    format!(
                                                                        "[binary {} bytes] {}...",
                                                                        raw_bytes.len(),
                                                                        hex.join(" ")
                                                                    )
                                                                } else {
                                                                    format!(
                                                                        "[binary {} bytes] {}",
                                                                        raw_bytes.len(),
                                                                        hex.join(" ")
                                                                    )
                                                                }
                                                            }
                                                        };

                                                    let message = ZenohMessage::new_with_bytes(
                                                        sample.key_expr().to_string(),
                                                        payload,
                                                        raw_bytes,
                                                        "text/plain".to_string(),
                                                        Utc::now(),
                                                        MessageType::QueryReply,
                                                        is_local,
                                                        MessageSource::PublishingSession,
                                                    );

                                                    let _ = event_sender_query
                                                        .send(ZenohEvent::MessageReceived(message));
                                                }
                                                Err(e) => {
                                                    error!("Query error: {}", e);
                                                }
                                            }
                                        }

                                        info!(
                                            "Query reply loop ended, received_replies={}",
                                            received_replies
                                        );

                                        // If no replies were received, send alert
                                        if !received_replies {
                                            let _ = event_sender_query.send(
                                                ZenohEvent::QueryNoResponses {
                                                    selector: selector_clone,
                                                },
                                            );
                                        }
                                    });
                                }
                                Err(e) => {
                                    error!("Failed to send query: {}", e);
                                }
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
                    ZenohCommand::EnableQueryable { key_expr } => {
                        if let Some(ref sess) = publishing_session {
                            // Cancel existing queryable if any
                            if let Some((handle, cancel_tx)) = queryable_task.take() {
                                let _ = cancel_tx.send(()).await;
                                handle.abort();
                            }

                            // Create queryable
                            let sess_clone = sess.clone();
                            let kvstore_clone = local_kvstore.clone();
                            let (cancel_tx, mut cancel_rx) = tokio::sync::mpsc::channel::<()>(1);

                            let handle = tokio::spawn(async move {
                                match sess_clone.declare_queryable(&key_expr).await {
                                    Ok(queryable) => {
                                        info!("Queryable declared on {}", key_expr);

                                        loop {
                                            tokio::select! {
                                                _ = cancel_rx.recv() => {
                                                    info!("Queryable cancelled");
                                                    break;
                                                }
                                                query = queryable.recv_async() => {
                                                    match query {
                                                        Ok(query) => {
                                                            let selector = query.selector();
                                                            info!("Received query with selector: {}", selector);

                                                            // Get the key expression from the selector
                                                            let key_expr = selector.key_expr().as_str();
                                                            info!("Query key_expr: {}", key_expr);

                                                            // Collect matching entries while holding the lock
                                                            let matches: Vec<(String, String, String)> = {
                                                                if let Ok(store) = kvstore_clone.read() {
                                                                    info!("Checking kvstore for query: {}, store has {} keys", key_expr, store.len());

                                                                    // Check for exact match first
                                                                    if let Some((payload, encoding)) = store.get(key_expr) {
                                                                        info!("Found exact match for {}", key_expr);
                                                                        vec![(key_expr.to_string(), payload.clone(), encoding.clone())]
                                                                    } else {
                                                                        // Pattern matching
                                                                        let mut results = Vec::new();
                                                                        for (stored_key, (payload, encoding)) in store.iter() {
                                                                            if key_expr.contains("**") {
                                                                                let prefix = key_expr.trim_end_matches("**").trim_end_matches('/');
                                                                                if prefix.is_empty() || stored_key.starts_with(prefix) {
                                                                                    info!("Pattern ** match: {} matches {}", stored_key, key_expr);
                                                                                    results.push((stored_key.clone(), payload.clone(), encoding.clone()));
                                                                                }
                                                                            } else if key_expr.contains('*') {
                                                                                let parts: Vec<&str> = key_expr.split('/').collect();
                                                                                let key_parts: Vec<&str> = stored_key.split('/').collect();

                                                                                if parts.len() == key_parts.len() {
                                                                                    let matches = parts.iter().zip(key_parts.iter())
                                                                                        .all(|(p, kp)| p == &"*" || p == kp);

                                                                                    if matches {
                                                                                        info!("Pattern * match: {} matches {}", stored_key, key_expr);
                                                                                        results.push((stored_key.clone(), payload.clone(), encoding.clone()));
                                                                                    }
                                                                                }
                                                                            }
                                                                        }
                                                                        results
                                                                    }
                                                                } else {
                                                                    Vec::new()
                                                                }
                                                            }; // Lock dropped here

                                                            // Now respond without holding the lock
                                                            if matches.is_empty() {
                                                                info!("No matching local keys for query: {}", key_expr);
                                                            } else {
                                                                for (key, payload, encoding) in matches {
                                                                    info!("Responding for key {} to query {}", key, key_expr);
                                                                    // Mark reply as local by adding simple attachment
                                                                    let _ = query
                                                                        .reply(key.as_str(), payload)
                                                                        .encoding(encoding.as_str())
                                                                        .attachment("source:local")
                                                                        .await;
                                                                }
                                                            }
                                                        }
                                                        Err(_) => {
                                                            error!("Error receiving query");
                                                            break;
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                    Err(e) => {
                                        error!("Failed to declare queryable: {}", e);
                                    }
                                }
                            });

                            queryable_task = Some((handle, cancel_tx));
                        }
                    }
                    ZenohCommand::DisableQueryable => {
                        if let Some((handle, cancel_tx)) = queryable_task.take() {
                            let _ = cancel_tx.send(()).await;
                            handle.abort();
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
pub async fn connect_zenoh(
    locators: &str,
    listen_port: &str,
    mode: &str,
    config_json: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect - mode: {}, locators: {}, listen_port: {}",
        mode,
        if locators.is_empty() {
            "(none - using discovery)"
        } else {
            locators
        },
        listen_port
    );

    let mut config = zenoh::config::Config::default();

    // Set max_message_size to 100GB to allow very large payloads (default is 1GB)
    // This is needed for publishing files larger than 1GB
    let max_message_size: usize = 100 * 1024 * 1024 * 1024; // 100GB
    config
        .transport
        .link
        .rx
        .set_max_message_size(max_message_size)
        .unwrap();
    info!("Set max_message_size to {} bytes (100GB)", max_message_size);

    // Extract protocol from first locator for listen endpoint
    // Default to "tcp" if locators is empty (multicast discovery mode)
    let first_locator = locators.split(',').next().unwrap_or("").trim();
    let protocol = if first_locator.is_empty() {
        "tcp"
    } else {
        first_locator.split('/').next().unwrap_or("tcp")
    };
    let is_udp_based = protocol == "udp" || protocol == "quic";

    // Set batch size based on protocol
    // UDP: Use MTU-safe size (1472 = 1500 - 20 IP - 8 UDP headers) to avoid IP fragmentation
    // TCP: Use maximum (65535) since TCP handles segmentation reliably
    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config.transport.link.tx.set_batch_size(batch_size).unwrap();

    // Increase RX buffer size for handling high-throughput (default is 65535)
    // Set to 16MB to handle large fragmented messages over UDP
    config
        .transport
        .link
        .rx
        .set_buffer_size(16 * 1024 * 1024)
        .unwrap();

    info!(
        "Set batch_size to {} bytes, rx_buffer to 16MB (protocol: {})",
        batch_size, protocol
    );

    // Increase queue sizes to handle large payload bursts (default is 2, max is 16)
    // This allows more batches to be queued before back-pressure kicks in
    config.transport.link.tx.queue.size.set_data(16).unwrap();
    config
        .transport
        .link
        .tx
        .queue
        .size
        .set_data_high(16)
        .unwrap();
    config
        .transport
        .link
        .tx
        .queue
        .size
        .set_data_low(16)
        .unwrap();

    // Send immediately without waiting to batch
    config
        .transport
        .link
        .tx
        .queue
        .batching
        .set_enabled(false)
        .unwrap();

    // Increase wait_before_close timeout for Block congestion control (default: 5 seconds)
    // Set to 5 minutes (300 seconds = 300_000_000 microseconds) to allow large transfers
    config
        .transport
        .link
        .tx
        .queue
        .congestion_control
        .block
        .set_wait_before_close(300_000_000)
        .unwrap();
    info!("Set queue sizes to 16, batching disabled, wait_before_close to 300 seconds");

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
    if mode == "peer" {
        info!("Setting peer mode");
        config.set_mode(Some(WhatAmI::Peer)).unwrap();

        // Enable scouting to allow peers for discovery via multicast
        info!("Peer mode - configuring scouting");
        config.scouting.multicast.set_enabled(Some(true)).unwrap();
        config.scouting.gossip.set_enabled(Some(true)).unwrap();

        // Set default multicast address
        config
            .scouting
            .multicast
            .set_address(Some("224.0.0.224:7446".parse().unwrap()))
            .unwrap();

        // Enable local routing
        // Note: routing.peer.mode is private in zenoh 1.0, skip this configuration

        // Add listening endpoints for peer mode
        // Each peer on the same machine should use a different listen port
        // Use [::] for IPv6 (default)
        // so_sndbuf/so_rcvbuf options only work for TCP/TLS, not UDP
        let port = listen_port.parse::<u16>().unwrap_or(7447);
        let listen_endpoint = format!("{}/[::]:{}", protocol, port);
        info!("Peer mode, listening on {}", listen_endpoint);
        config
            .listen
            .endpoints
            .set(vec![listen_endpoint.parse().unwrap()])
            .unwrap();
    } else {
        info!("Setting client mode");
        config.set_mode(Some(WhatAmI::Client)).unwrap();
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
    }

    // Parse the locator strings into endpoints
    // Supports multiple endpoints separated by commas
    if !locators.is_empty() {
        debug!("Parsing locators: {}", locators);

        // Parse endpoints - socket buffer options (so_sndbuf/so_rcvbuf) only work for TCP/TLS
        let endpoints: Vec<_> = locators
            .split(',')
            .map(|s| s.trim().to_string())
            .map(|s| s.parse())
            .collect::<Result<Vec<_>, _>>()?;

        // Apply the endpoints to the configuration
        config.connect.endpoints.set(endpoints.clone()).unwrap();
        info!("Set {} endpoints", endpoints.len());
    } else {
        info!("No locators, using only multicast discovery");
    }

    // Open the Zenoh session with the configured settings
    info!("Opening Zenoh session with mode: {:?}", mode);
    info!(
        "Final config - connect endpoints: {:?}",
        config.connect.endpoints
    );
    if mode == "peer" {
        info!(
            "Peer mode - listen endpoints: {:?}",
            config.listen.endpoints
        );
        info!(
            "Peer mode - multicast enabled: {:?}",
            config.scouting.multicast.enabled()
        );
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
            Err(format!(
                "Connection timeout in {} mode: Unable to establish connection within 30 seconds",
                mode
            )
            .into())
        }
    }
}

/// Connect a monitor session for observing all network traffic.
/// This session has scouting disabled to prevent interference with the publishing session
/// and uses a different port to avoid CONNECTION_TO_SELF errors.
///
/// # Arguments
/// * `locators` - Connection endpoints (same as publishing session)
/// * `monitor_port` - Port to listen on (typically listen_port + 1000)
/// * `mode` - Connection mode (peer or client)
pub async fn connect_zenoh_monitor(
    locators: &str,
    monitor_port: &str,
    mode: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect monitor session - mode: {}, locators: {}, monitor_port: {}",
        mode,
        if locators.is_empty() {
            "(none - using discovery)"
        } else {
            locators
        },
        monitor_port
    );

    let mut config = zenoh::config::Config::default();

    // Set max_message_size to 100GB to allow very large payloads
    let max_message_size: usize = 100 * 1024 * 1024 * 1024;
    config
        .transport
        .link
        .rx
        .set_max_message_size(max_message_size)
        .unwrap();

    // Extract protocol from first locator for listen endpoint
    // Default to "tcp" if locators is empty (multicast discovery mode)
    let first_locator = locators.split(',').next().unwrap_or("").trim();
    let protocol = if first_locator.is_empty() {
        "tcp"
    } else {
        first_locator.split('/').next().unwrap_or("tcp")
    };
    let is_udp_based = protocol == "udp" || protocol == "quic";

    // Set batch size based on protocol
    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config.transport.link.tx.set_batch_size(batch_size).unwrap();
    config
        .transport
        .link
        .rx
        .set_buffer_size(16 * 1024 * 1024)
        .unwrap();

    // Configure the connection mode
    if mode == "peer" {
        info!("Monitor session: Setting peer mode with scouting DISABLED");
        config.set_mode(Some(WhatAmI::Peer)).unwrap();

        // CRITICAL: Disable scouting on monitor session to prevent interference
        // This prevents the monitor from participating in peer discovery
        // which could cause CONNECTION_TO_SELF errors
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
        config.scouting.gossip.set_enabled(Some(false)).unwrap();

        // Add listening endpoint with different port
        let port = monitor_port.parse::<u16>().unwrap_or(8447);
        let listen_endpoint = format!("{}/[::]:{}", protocol, port);
        info!("Monitor session: listening on {}", listen_endpoint);
        config
            .listen
            .endpoints
            .set(vec![listen_endpoint.parse().unwrap()])
            .unwrap();
    } else {
        info!("Monitor session: Setting client mode");
        config.set_mode(Some(WhatAmI::Client)).unwrap();
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
    }

    // Parse the locator strings into endpoints (connect to same endpoints as publishing session)
    if !locators.is_empty() {
        let endpoints: Vec<_> = locators
            .split(',')
            .map(|s| s.trim().to_string())
            .map(|s| s.parse())
            .collect::<Result<Vec<_>, _>>()?;

        config.connect.endpoints.set(endpoints.clone()).unwrap();
        info!("Monitor session: Set {} connect endpoints", endpoints.len());
    }

    // Open the monitor session with a shorter timeout
    info!("Opening monitor Zenoh session...");

    match tokio::time::timeout(std::time::Duration::from_secs(15), zenoh::open(config)).await {
        Ok(Ok(session)) => {
            info!("Successfully connected monitor session in {} mode", mode);
            // Brief stabilization delay
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
            Ok(session)
        }
        Ok(Err(e)) => {
            error!("Monitor session failed to connect: {}", e);
            Err(format!("Monitor connection failed: {}", e).into())
        }
        Err(_) => {
            error!("Monitor session connection timeout");
            Err("Monitor connection timeout after 15 seconds".into())
        }
    }
}
