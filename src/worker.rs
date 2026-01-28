//! Zenoh worker thread and connection management.

use chrono::Utc;
use std::collections::HashMap;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, RwLock};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tracing::{debug, error, info};
use zenoh::config::WhatAmI;
use zenoh::Session;

use crate::commands::{ZenohCommand, ZenohEvent};
use crate::types::{MessageSource, MessageType, ZenohMessage};

/// Manages the lifecycle of an active Zenoh subscription.
pub struct ActiveSubscription {
    #[allow(dead_code)]
    pub key_expr: String,
    pub task_handle: JoinHandle<()>,
    pub cancel_sender: oneshot::Sender<()>,
}

/// Message buffer thread that batches messages for UI.
pub fn message_buffer_thread(buffer_receiver: Receiver<ZenohEvent>, ui_sender: Sender<ZenohEvent>) {
    info!("Message buffer thread started");

    let batch_interval = std::time::Duration::from_millis(16);
    let mut message_buffer: Vec<ZenohMessage> = Vec::with_capacity(100);

    loop {
        let deadline = std::time::Instant::now() + batch_interval;

        while std::time::Instant::now() < deadline {
            match buffer_receiver.recv_timeout(std::time::Duration::from_millis(1)) {
                Ok(event) => match event {
                    ZenohEvent::MessageReceived(msg) => {
                        message_buffer.push(msg);
                        if message_buffer.len() >= 50 {
                            break;
                        }
                    }
                    other_event => {
                        if !message_buffer.is_empty() {
                            let batch =
                                std::mem::replace(&mut message_buffer, Vec::with_capacity(100));
                            let _ = ui_sender.send(ZenohEvent::MessageBatch(batch));
                        }
                        let _ = ui_sender.send(other_event);
                    }
                },
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => break,
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    if !message_buffer.is_empty() {
                        let batch = std::mem::take(&mut message_buffer);
                        let _ = ui_sender.send(ZenohEvent::MessageBatch(batch));
                    }
                    info!("Message buffer thread exiting - worker disconnected");
                    return;
                }
            }
        }

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
pub async fn zenoh_worker(
    command_receiver: Receiver<ZenohCommand>,
    event_sender: Sender<ZenohEvent>,
    local_kvstore: Arc<RwLock<HashMap<String, (String, String)>>>,
) {
    info!("Zenoh worker thread started");

    let mut publishing_session: Option<Arc<Session>> = None;
    let mut monitor_session: Option<Arc<Session>> = None;
    let mut active_subscriptions: HashMap<String, ActiveSubscription> = HashMap::new();
    let mut monitor_subscription: Option<ActiveSubscription> = None;
    let mut queryable_task: Option<(JoinHandle<()>, tokio::sync::mpsc::Sender<()>)> = None;

    info!("Worker thread main loop starting...");

    loop {
        match command_receiver.recv_timeout(std::time::Duration::from_millis(100)) {
            Ok(command) => {
                info!("Worker received command: {:?}", command);
                handle_command(
                    command,
                    &event_sender,
                    &local_kvstore,
                    &mut publishing_session,
                    &mut monitor_session,
                    &mut active_subscriptions,
                    &mut monitor_subscription,
                    &mut queryable_task,
                )
                .await;
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                error!("Command channel disconnected, worker thread exiting");
                break;
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn handle_command(
    command: ZenohCommand,
    event_sender: &Sender<ZenohEvent>,
    local_kvstore: &Arc<RwLock<HashMap<String, (String, String)>>>,
    publishing_session: &mut Option<Arc<Session>>,
    monitor_session: &mut Option<Arc<Session>>,
    active_subscriptions: &mut HashMap<String, ActiveSubscription>,
    monitor_subscription: &mut Option<ActiveSubscription>,
    queryable_task: &mut Option<(JoinHandle<()>, tokio::sync::mpsc::Sender<()>)>,
) {
    match command {
        ZenohCommand::Connect {
            locators,
            listen_port,
            mode,
            config_json,
        } => {
            handle_connect(
                &locators,
                &listen_port,
                &mode,
                &config_json,
                event_sender,
                publishing_session,
                monitor_session,
                monitor_subscription,
            )
            .await;
        }
        ZenohCommand::Disconnect => {
            handle_disconnect(
                publishing_session,
                monitor_session,
                active_subscriptions,
                monitor_subscription,
                event_sender,
            )
            .await;
        }
        ZenohCommand::Subscribe {
            key_expr,
            reliability: _,
            mode: _,
        } => {
            handle_subscribe(&key_expr, publishing_session, active_subscriptions, event_sender)
                .await;
        }
        ZenohCommand::Unsubscribe { subscription_id } => {
            handle_unsubscribe(&subscription_id, active_subscriptions, event_sender);
        }
        ZenohCommand::Publish {
            key,
            payload,
            encoding,
            from_import,
        } => {
            handle_publish(
                &key,
                payload,
                &encoding,
                from_import,
                publishing_session,
                local_kvstore,
                event_sender,
            )
            .await;
        }
        ZenohCommand::Query {
            selector,
            value,
            timeout_ms,
        } => {
            handle_query(&selector, &value, timeout_ms, publishing_session, event_sender).await;
        }
        ZenohCommand::EnableQueryable { key_expr } => {
            handle_enable_queryable(&key_expr, publishing_session, local_kvstore, queryable_task)
                .await;
        }
        ZenohCommand::DisableQueryable => {
            handle_disable_queryable(queryable_task).await;
        }
        ZenohCommand::Ping => {
            debug!("Worker received ping, sending pong");
            let _ = event_sender.send(ZenohEvent::Pong);
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn handle_connect(
    locators: &str,
    listen_port: &str,
    mode: &str,
    config_json: &str,
    event_sender: &Sender<ZenohEvent>,
    publishing_session: &mut Option<Arc<Session>>,
    monitor_session: &mut Option<Arc<Session>>,
    monitor_subscription: &mut Option<ActiveSubscription>,
) {
    info!(
        "Worker processing connect - mode: {}, locators: {}, listen_port: {}",
        mode,
        if locators.is_empty() { "(none - using discovery)" } else { locators },
        listen_port
    );

    match connect_zenoh(locators, listen_port, mode, config_json).await {
        Ok(new_session) => {
            info!("Worker successfully created publishing session");
            let session_arc = Arc::new(new_session);
            *publishing_session = Some(session_arc.clone());

            match event_sender.send(ZenohEvent::PublishingConnected) {
                Ok(_) => info!("Successfully sent PublishingConnected event to GUI"),
                Err(e) => error!("Failed to send PublishingConnected event: {:?}", e),
            }

            // Spawn discovery thread
            spawn_discovery_thread(session_arc.clone(), event_sender.clone());

            // Connect monitor session with scouting disabled
            let first_locator = locators.split(',').next().unwrap_or("").trim();
            let protocol = if first_locator.is_empty() {
                "tcp"
            } else {
                first_locator.split('/').next().unwrap_or("tcp")
            };
            let monitor_port = listen_port.parse::<u16>().unwrap_or(7447) + 1000;
            info!("Connecting monitor session on port {}", monitor_port);

            match connect_zenoh_monitor(locators, &monitor_port.to_string(), mode, protocol).await {
                Ok(mon_session) => {
                    info!("Worker successfully created monitor session");
                    let mon_session_arc = Arc::new(mon_session);
                    *monitor_session = Some(mon_session_arc.clone());

                    // Auto-subscribe monitor to **
                    match mon_session_arc.declare_subscriber("**").await {
                        Ok(subscriber) => {
                            info!("Monitor session subscribed to **");
                            let event_sender_clone = event_sender.clone();
                            let (cancel_sender, mut cancel_receiver) = oneshot::channel();

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
                                                    let payload_display = match sample.payload().try_to_string() {
                                                        Ok(s) => s.into_owned(),
                                                        Err(_) => format_binary_preview(&raw_bytes),
                                                    };
                                                    let message = ZenohMessage::new_with_bytes(
                                                        sample.key_expr().to_string(),
                                                        payload_display,
                                                        raw_bytes,
                                                        "text/plain".to_string(),
                                                        Utc::now(),
                                                        MessageType::Subscribe,
                                                        false,
                                                        MessageSource::MonitorSession,
                                                    );
                                                    let _ = event_sender_clone.send(ZenohEvent::MessageReceived(message));
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

                            *monitor_subscription = Some(ActiveSubscription {
                                key_expr: "**".to_string(),
                                task_handle,
                                cancel_sender,
                            });
                        }
                        Err(e) => {
                            error!("Failed to subscribe monitor to **: {}", e);
                        }
                    }

                    match event_sender.send(ZenohEvent::MonitorConnected) {
                        Ok(_) => info!("Successfully sent MonitorConnected event to GUI"),
                        Err(e) => error!("Failed to send MonitorConnected event: {:?}", e),
                    }
                }
                Err(e) => {
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
            match event_sender.send(ZenohEvent::ConnectionError(e.to_string())) {
                Ok(_) => info!("Sent ConnectionError event to GUI"),
                Err(send_err) => error!("Failed to send ConnectionError event: {:?}", send_err),
            }
        }
    }
}

fn spawn_discovery_thread(session: Arc<Session>, sender: Sender<ZenohEvent>) {
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();

        rt.block_on(async {
            loop {
                let mut peers_count = 0;
                let mut peers_iter = session.info().peers_zid().await;
                while peers_iter.next().is_some() {
                    peers_count += 1;
                }

                let mut routers_count = 0;
                let mut routers_iter = session.info().routers_zid().await;
                while routers_iter.next().is_some() {
                    routers_count += 1;
                }

                if sender
                    .send(ZenohEvent::DiscoveryUpdate {
                        peers: peers_count,
                        routers: routers_count,
                    })
                    .is_err()
                {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        });
    });
}

async fn handle_disconnect(
    publishing_session: &mut Option<Arc<Session>>,
    monitor_session: &mut Option<Arc<Session>>,
    active_subscriptions: &mut HashMap<String, ActiveSubscription>,
    monitor_subscription: &mut Option<ActiveSubscription>,
    event_sender: &Sender<ZenohEvent>,
) {
    // Cancel monitor subscription first
    if let Some(sub) = monitor_subscription.take() {
        let _ = sub.cancel_sender.send(());
        sub.task_handle.abort();
        info!("Monitor subscription cancelled");
    }

    // Cancel all active user subscriptions
    for (_, subscription) in active_subscriptions.drain() {
        let _ = subscription.cancel_sender.send(());
        subscription.task_handle.abort();
    }

    // Close monitor session
    if let Some(s) = monitor_session.take() {
        let _ = s.close().await;
        info!("Monitor session closed");
    }

    // Close publishing session
    if let Some(s) = publishing_session.take() {
        let _ = s.close().await;
        info!("Publishing session closed");
    }

    let _ = event_sender.send(ZenohEvent::Disconnected);
}

async fn handle_subscribe(
    key_expr: &str,
    publishing_session: &Option<Arc<Session>>,
    active_subscriptions: &mut HashMap<String, ActiveSubscription>,
    event_sender: &Sender<ZenohEvent>,
) {
    if let Some(ref sess) = publishing_session {
        match sess.declare_subscriber(key_expr).await {
            Ok(subscriber) => {
                let sub_id = format!(
                    "sub_{}_{}",
                    chrono::Utc::now().timestamp_millis(),
                    active_subscriptions.len()
                );
                let event_sender_clone = event_sender.clone();
                let key_expr_clone = key_expr.to_string();
                let (cancel_sender, mut cancel_receiver) = oneshot::channel();

                let task_handle = tokio::spawn(async move {
                    loop {
                        tokio::select! {
                            _ = &mut cancel_receiver => {
                                info!("Subscription cancelled");
                                break;
                            }
                            result = subscriber.recv_async() => {
                                match result {
                                    Ok(sample) => {
                                        info!("Subscriber received sample on key: {}", sample.key_expr());
                                        let raw_bytes: Vec<u8> = sample.payload().to_bytes().to_vec();
                                        let payload_display = match sample.payload().try_to_string() {
                                            Ok(s) => s.into_owned(),
                                            Err(_) => format_binary_preview(&raw_bytes),
                                        };
                                        let message = ZenohMessage::new_with_bytes(
                                            sample.key_expr().to_string(),
                                            payload_display,
                                            raw_bytes,
                                            "text/plain".to_string(),
                                            Utc::now(),
                                            MessageType::Subscribe,
                                            false,
                                            MessageSource::PublishingSession,
                                        );
                                        match event_sender_clone.send(ZenohEvent::MessageReceived(message)) {
                                            Ok(_) => info!("Sent MessageReceived event to GUI"),
                                            Err(e) => error!("Failed to send MessageReceived: {:?}", e),
                                        }
                                    }
                                    Err(e) => {
                                        error!("Subscriber recv error: {:?}", e);
                                        break;
                                    }
                                }
                            }
                        }
                    }
                });

                active_subscriptions.insert(
                    sub_id.clone(),
                    ActiveSubscription {
                        key_expr: key_expr.to_string(),
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

fn handle_unsubscribe(
    subscription_id: &str,
    active_subscriptions: &mut HashMap<String, ActiveSubscription>,
    event_sender: &Sender<ZenohEvent>,
) {
    if let Some(subscription) = active_subscriptions.remove(subscription_id) {
        let _ = subscription.cancel_sender.send(());
        subscription.task_handle.abort();
        let _ = event_sender.send(ZenohEvent::SubscriptionRemoved {
            id: subscription_id.to_string(),
        });
    }
}

async fn handle_publish(
    key: &str,
    payload: Vec<u8>,
    encoding: &str,
    from_import: bool,
    publishing_session: &Option<Arc<Session>>,
    local_kvstore: &Arc<RwLock<HashMap<String, (String, String)>>>,
    event_sender: &Sender<ZenohEvent>,
) {
    if let Some(ref sess) = publishing_session {
        let payload_len = payload.len();

        // Generate display string
        let payload_str = {
            let preview_len = payload_len.min(256);
            match std::str::from_utf8(&payload[..preview_len]) {
                Ok(text) if payload_len <= 256 => text.to_string(),
                Ok(text) => format!("{}... [+{} bytes]", text, payload_len - preview_len),
                Err(_) => format_binary_preview(&payload[..preview_len]),
            }
        };

        // Store in kvstore for queryable (skip large payloads and imports)
        if !from_import && payload_len <= 10 * 1024 * 1024 {
            if let Ok(mut store) = local_kvstore.write() {
                store.insert(key.to_string(), (payload_str.clone(), encoding.to_string()));
            }
        }

        const CHUNK_SIZE: usize = 64 * 1024 * 1024; // 64MB chunks
        const MAX_SINGLE_PAYLOAD: usize = 0xFFFF_FFFF; // u32::MAX (~4GB)

        if payload_len > MAX_SINGLE_PAYLOAD {
            // Large payload - send in chunks
            let total_chunks = (payload_len + CHUNK_SIZE - 1) / CHUNK_SIZE;
            info!("Chunking {} byte payload into {} chunks of {}MB each",
                  payload_len, total_chunks, CHUNK_SIZE / 1024 / 1024);

            let mut chunk_num = 0;
            let mut offset = 0;
            let mut all_ok = true;

            while offset < payload_len {
                let end = std::cmp::min(offset + CHUNK_SIZE, payload_len);
                let chunk = payload[offset..end].to_vec();
                let chunk_key = format!("{}/__chunk/{}/{}/{}", key, payload_len, total_chunks, chunk_num);

                match sess
                    .put(&chunk_key, chunk)
                    .encoding(encoding)
                    .congestion_control(zenoh::qos::CongestionControl::Block)
                    .await
                {
                    Ok(_) => info!("Published chunk {}/{} ({} bytes) to {}",
                                  chunk_num + 1, total_chunks, end - offset, chunk_key),
                    Err(e) => {
                        error!("Failed to publish chunk {} to {}: {}", chunk_num, chunk_key, e);
                        all_ok = false;
                        break;
                    }
                }

                offset = end;
                chunk_num += 1;
            }

            if all_ok {
                info!("Successfully published all {} chunks for {}", total_chunks, key);
            }
        } else if payload_len > 100 * 1024 * 1024 {
            // >100MB - no echo
            match sess
                .put(key, payload)
                .encoding(encoding)
                .congestion_control(zenoh::qos::CongestionControl::Block)
                .await
            {
                Ok(_) => info!("Published {} bytes to {} (large payload, no echo)", payload_len, key),
                Err(e) => error!("Failed to publish to {}: {}", key, e),
            }
        } else if from_import {
            // Imported file - no storage/echo
            match sess
                .put(key, payload)
                .encoding(encoding)
                .congestion_control(zenoh::qos::CongestionControl::Block)
                .await
            {
                Ok(_) => info!("Published {} bytes to {} (imported file, no storage)", payload_len, key),
                Err(e) => error!("Failed to publish to {}: {}", key, e),
            }
        } else {
            // Normal publish with echo
            match sess
                .put(key, payload.clone())
                .encoding(encoding)
                .congestion_control(zenoh::qos::CongestionControl::Block)
                .await
            {
                Ok(_) => info!("Published {} bytes to {}", payload_len, key),
                Err(e) => error!("Failed to publish to {}: {}", key, e),
            }

            // Echo back to UI
            let message = ZenohMessage::new_with_bytes(
                key.to_string(),
                payload_str,
                payload,
                encoding.to_string(),
                Utc::now(),
                MessageType::Publish,
                true,
                MessageSource::LocalEcho,
            );
            let _ = event_sender.send(ZenohEvent::MessageReceived(message));
        }
    }
}

async fn handle_query(
    selector: &str,
    value: &str,
    timeout_ms: u64,
    publishing_session: &Option<Arc<Session>>,
    event_sender: &Sender<ZenohEvent>,
) {
    if let Some(ref sess) = publishing_session {
        info!("Sending query for selector: {}", selector);
        let mut get_builder = sess.get(selector);
        if !value.is_empty() {
            get_builder = get_builder.payload(value);
        }
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
                let selector_clone = selector.to_string();

                tokio::spawn(async move {
                    let mut received_replies = false;
                    while let Ok(reply) = replies.recv_async().await {
                        info!("Received a reply from query");
                        match reply.result() {
                            Ok(sample) => {
                                received_replies = true;
                                info!("Query reply OK: key={}", sample.key_expr());

                                let is_local = sample
                                    .attachment()
                                    .and_then(|att| att.try_to_string().ok())
                                    .map(|s| s.contains("source:local"))
                                    .unwrap_or(false);

                                info!("Query reply is_local={}", is_local);

                                let raw_bytes: Vec<u8> = sample.payload().to_bytes().to_vec();
                                let payload = match sample.payload().try_to_string() {
                                    Ok(s) => s.into_owned(),
                                    Err(_) => format_binary_preview(&raw_bytes),
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
                                let _ = event_sender_query.send(ZenohEvent::MessageReceived(message));
                            }
                            Err(e) => {
                                error!("Query error: {}", e);
                            }
                        }
                    }

                    info!("Query reply loop ended, received_replies={}", received_replies);

                    if !received_replies {
                        let _ = event_sender_query.send(ZenohEvent::QueryNoResponses {
                            selector: selector_clone,
                        });
                    }
                });
            }
            Err(e) => {
                error!("Failed to send query: {}", e);
            }
        }
    }
}

async fn handle_enable_queryable(
    key_expr: &str,
    publishing_session: &Option<Arc<Session>>,
    local_kvstore: &Arc<RwLock<HashMap<String, (String, String)>>>,
    queryable_task: &mut Option<(JoinHandle<()>, tokio::sync::mpsc::Sender<()>)>,
) {
    if let Some(ref sess) = publishing_session {
        if let Some((handle, cancel_tx)) = queryable_task.take() {
            let _ = cancel_tx.send(()).await;
            handle.abort();
        }

        let sess_clone = sess.clone();
        let kvstore_clone = local_kvstore.clone();
        let key_expr_owned = key_expr.to_string();
        let (cancel_tx, mut cancel_rx) = tokio::sync::mpsc::channel::<()>(1);

        let handle = tokio::spawn(async move {
            if let Ok(queryable) = sess_clone.declare_queryable(&key_expr_owned).await {
                info!("Queryable declared on {}", key_expr_owned);

                loop {
                    tokio::select! {
                        _ = cancel_rx.recv() => {
                            info!("Queryable cancelled");
                            break;
                        }
                        query = queryable.recv_async() => {
                            if let Ok(query) = query {
                                let selector = query.selector();
                                let key_expr = selector.key_expr().as_str();
                                info!("Received query with selector: {}", selector);

                                // Collect matching entries
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
                                                    // Double wildcard - matches any number of segments
                                                    let prefix = key_expr.trim_end_matches("**").trim_end_matches('/');
                                                    if prefix.is_empty() || stored_key.starts_with(prefix) {
                                                        info!("Pattern ** match: {} matches {}", stored_key, key_expr);
                                                        results.push((stored_key.clone(), payload.clone(), encoding.clone()));
                                                    }
                                                } else if key_expr.contains('*') {
                                                    // Single wildcard - matches single segment
                                                    let parts: Vec<&str> = key_expr.split('/').collect();
                                                    let key_parts: Vec<&str> = stored_key.split('/').collect();

                                                    if parts.len() == key_parts.len() {
                                                        let matches = parts.iter().zip(key_parts.iter())
                                                            .all(|(p, kp)| *p == "*" || p == kp);

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
                                };

                                // Respond without holding the lock
                                if matches.is_empty() {
                                    info!("No matching local keys for query: {}", key_expr);
                                } else {
                                    for (key, payload, encoding) in matches {
                                        info!("Responding for key {} to query {}", key, key_expr);
                                        let _ = query.reply(key.as_str(), payload).encoding(encoding.as_str()).attachment("source:local").await;
                                    }
                                }
                            } else {
                                error!("Error receiving query");
                                break;
                            }
                        }
                    }
                }
            } else {
                error!("Failed to declare queryable on {}", key_expr_owned);
            }
        });

        *queryable_task = Some((handle, cancel_tx));
    }
}

async fn handle_disable_queryable(
    queryable_task: &mut Option<(JoinHandle<()>, tokio::sync::mpsc::Sender<()>)>,
) {
    if let Some((handle, cancel_tx)) = queryable_task.take() {
        let _ = cancel_tx.send(()).await;
        handle.abort();
    }
}

fn format_binary_preview(bytes: &[u8]) -> String {
    let hex: Vec<String> = bytes.iter().take(256).map(|b| format!("{:02x}", b)).collect();
    if bytes.len() > 256 {
        format!("[binary {} bytes] {}...", bytes.len(), hex.join(" "))
    } else {
        format!("[binary {} bytes] {}", bytes.len(), hex.join(" "))
    }
}

/// Establishes a connection to the Zenoh network.
pub async fn connect_zenoh(
    locators: &str,
    listen_port: &str,
    mode: &str,
    config_json: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect - mode: {}, locators: {}, listen_port: {}",
        mode,
        if locators.is_empty() { "(none - using discovery)" } else { locators },
        listen_port
    );

    let mut config = zenoh::config::Config::default();

    // Set max_message_size to 100GB
    let max_message_size: usize = 100 * 1024 * 1024 * 1024;
    config.transport.link.rx.set_max_message_size(max_message_size).unwrap();
    info!("Set max_message_size to {} bytes (100GB)", max_message_size);

    let first_locator = locators.split(',').next().unwrap_or("").trim();
    let protocol = if first_locator.is_empty() { "tcp" } else { first_locator.split('/').next().unwrap_or("tcp") };
    let is_udp_based = protocol == "udp" || protocol == "quic";

    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config.transport.link.tx.set_batch_size(batch_size).unwrap();
    config.transport.link.rx.set_buffer_size(16 * 1024 * 1024).unwrap();

    config.transport.link.tx.queue.size.set_data(16).unwrap();
    config.transport.link.tx.queue.size.set_data_high(16).unwrap();
    config.transport.link.tx.queue.size.set_data_low(16).unwrap();
    config.transport.link.tx.queue.batching.set_enabled(false).unwrap();
    config.transport.link.tx.queue.congestion_control.block.set_wait_before_close(300_000_000).unwrap();
    info!("Set queue sizes to 16, batching disabled, wait_before_close to 300 seconds");

    if !config_json.is_empty() && config_json != "{}" {
        debug!("Parsing additional config: {}", config_json);
        if let Ok(additional_config) = serde_json::from_str::<serde_json::Value>(config_json) {
            if let Ok(zenoh_config) = serde_json::from_value(additional_config) {
                config = zenoh_config;
                debug!("Successfully applied additional config");
            }
        }
    }

    if mode == "peer" {
        info!("Setting peer mode");
        config.set_mode(Some(WhatAmI::Peer)).unwrap();
        info!("Peer mode - configuring scouting");
        config.scouting.multicast.set_enabled(Some(true)).unwrap();
        config.scouting.gossip.set_enabled(Some(true)).unwrap();
        config.scouting.multicast.set_address(Some("224.0.0.224:7446".parse().unwrap())).unwrap();

        let port = listen_port.parse::<u16>().unwrap_or(7447);
        let listen_endpoint = format!("{}/[::]:{}", protocol, port);
        info!("Peer mode, listening on {}", listen_endpoint);
        config.listen.endpoints.set(vec![listen_endpoint.parse().unwrap()]).unwrap();
    } else {
        info!("Setting client mode");
        config.set_mode(Some(WhatAmI::Client)).unwrap();
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
    }

    if !locators.is_empty() {
        debug!("Parsing locators: {}", locators);
        let endpoints: Vec<_> = locators.split(',').map(|s| s.trim().parse()).collect::<Result<Vec<_>, _>>()?;
        config.connect.endpoints.set(endpoints.clone()).unwrap();
        info!("Set {} endpoints", endpoints.len());
    } else {
        info!("No locators, using only multicast discovery");
    }

    info!("Opening Zenoh session with mode: {:?}", mode);
    info!("Final config - connect endpoints: {:?}", config.connect.endpoints);
    if mode == "peer" {
        info!("Peer mode - listen endpoints: {:?}", config.listen.endpoints);
        info!("Peer mode - multicast enabled: {:?}", config.scouting.multicast.enabled());
    }

    info!("Starting Zenoh session open...");
    match tokio::time::timeout(std::time::Duration::from_secs(30), zenoh::open(config)).await {
        Ok(Ok(session)) => {
            info!("Successfully connected to Zenoh network in {} mode", mode);
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

/// Connect a monitor session for observing all network traffic.
async fn connect_zenoh_monitor(
    locators: &str,
    monitor_port: &str,
    mode: &str,
    protocol: &str,
) -> Result<Session, Box<dyn std::error::Error + Send + Sync>> {
    info!(
        "Attempting to connect monitor session - mode: {}, locators: {}, monitor_port: {}",
        mode,
        if locators.is_empty() { "(none - using discovery)" } else { locators },
        monitor_port
    );

    let mut config = zenoh::config::Config::default();

    let max_message_size: usize = 100 * 1024 * 1024 * 1024;
    config.transport.link.rx.set_max_message_size(max_message_size).unwrap();

    let is_udp_based = protocol == "udp" || protocol == "quic";
    let batch_size: u16 = if is_udp_based { 1472 } else { 65535 };
    config.transport.link.tx.set_batch_size(batch_size).unwrap();
    config.transport.link.rx.set_buffer_size(16 * 1024 * 1024).unwrap();

    if mode == "peer" {
        info!("Monitor session: Setting peer mode with scouting DISABLED");
        config.set_mode(Some(WhatAmI::Peer)).unwrap();
        // CRITICAL: Disable scouting on monitor session to prevent interference
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
        config.scouting.gossip.set_enabled(Some(false)).unwrap();

        let port = monitor_port.parse::<u16>().unwrap_or(8447);
        let listen_endpoint = format!("{}/[::]:{}", protocol, port);
        info!("Monitor session: listening on {}", listen_endpoint);
        config.listen.endpoints.set(vec![listen_endpoint.parse().unwrap()]).unwrap();
    } else {
        info!("Monitor session: Setting client mode");
        config.set_mode(Some(WhatAmI::Client)).unwrap();
        config.scouting.multicast.set_enabled(Some(false)).unwrap();
    }

    if !locators.is_empty() {
        let endpoints: Vec<_> = locators.split(',').map(|s| s.trim().parse()).collect::<Result<Vec<_>, _>>()?;
        config.connect.endpoints.set(endpoints.clone()).unwrap();
        info!("Monitor session: Set {} connect endpoints", endpoints.len());
    }

    info!("Opening monitor Zenoh session...");
    match tokio::time::timeout(std::time::Duration::from_secs(15), zenoh::open(config)).await {
        Ok(Ok(session)) => {
            info!("Successfully connected monitor session in {} mode", mode);
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
