//! Publish: preview text, local kvstore, chunked or single put, and the local echo.

use chrono::Utc;
use tracing::{error, info};

use super::pipeline::send_sample;
use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PublishShape {
    Single,
    Chunked { chunks: usize },
}

/// Payloads above one chunk are split so each Zenoh message stays well under
/// a receiver's default 1 GiB max_message_size.
pub(crate) fn publish_shape(len: usize) -> PublishShape {
    if len > crate::transfer::CHUNK_SIZE {
        PublishShape::Chunked {
            chunks: len.div_ceil(crate::transfer::CHUNK_SIZE),
        }
    } else {
        PublishShape::Single
    }
}

/// Publish arm: publishes raw bytes on the publishing session.
///
/// Only a put that zenoh accepted is stored in the local kvstore, echoed and
/// reported as `Published`; a rejected put sends `OperationFailed`.
pub(crate) async fn handle_publish(
    st: &mut WorkerState,
    ctx: &WorkerCtx,
    key: String,
    payload: Vec<u8>,
    encoding: String,
    from_import: bool,
    filename: Option<String>,
) {
    if let Some(ref sess) = st.publishing_session {
        // Generate display string - only preview, never clone full payload
        let payload_str = crate::payload::preview(&payload, 256);

        // Publish raw bytes to the Zenoh network
        // Use Block congestion control for large payloads to ensure delivery
        let payload_len = payload.len();

        let report_failure = |e: &dyn std::fmt::Display| {
            let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                op: FailedOp::Publish,
                error: format!("{}: {}", key, e),
            });
        };

        if let PublishShape::Chunked {
            chunks: total_chunks,
        } = publish_shape(payload_len)
        {
            // Large payload - send in chunks
            info!(
                "Chunking {} byte payload into {} chunks of {}MB each",
                payload_len,
                total_chunks,
                crate::transfer::CHUNK_SIZE / 1024 / 1024
            );

            let mut chunk_num = 0;
            let mut offset = 0;
            let mut all_ok = true;

            while offset < payload_len {
                let end = std::cmp::min(offset + crate::transfer::CHUNK_SIZE, payload_len);
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
                        report_failure(&e);
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
                let _ = ctx.event_sender.send(ZenohEvent::Published {
                    key: key.clone(),
                    bytes: payload_len,
                });
            }
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
                Ok(_) => {
                    info!(
                        "Published {} bytes to {} (imported file, no storage)",
                        payload_len, key
                    );
                    let _ = ctx.event_sender.send(ZenohEvent::Published {
                        key: key.clone(),
                        bytes: payload_len,
                    });
                }
                Err(e) => {
                    error!("Failed to publish to {}: {}", key, e);
                    report_failure(&e);
                }
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
                Ok(_) => {
                    info!("Published {} bytes to {}", payload_len, key);

                    // Store in local kvstore for queryable responses (only store small payloads)
                    // Skip storage for imported files - they are ephemeral and shouldn't persist in memory
                    if !from_import && payload_len <= 10 * 1024 * 1024 {
                        // 10MB limit for kvstore
                        if let Ok(mut store) = ctx.local_kvstore.write() {
                            store.insert(
                                key.clone(),
                                StoredValue {
                                    bytes: payload.clone(),
                                    encoding: encoding.clone(),
                                },
                            );
                        }
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

                    send_sample(&ctx.event_sender, &ctx.sample_drops, message);

                    let _ = ctx.event_sender.send(ZenohEvent::Published {
                        key: key.clone(),
                        bytes: payload_len,
                    });
                }
                Err(e) => {
                    error!("Failed to publish to {}: {}", key, e);
                    report_failure(&e);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publish_shape_chunks_above_chunk_size() {
        let c = crate::transfer::CHUNK_SIZE;
        assert_eq!(publish_shape(0), PublishShape::Single);
        assert_eq!(publish_shape(c), PublishShape::Single);
        assert_eq!(publish_shape(c + 1), PublishShape::Chunked { chunks: 2 });
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failed_put_is_not_echoed_or_stored() {
        use crate::worker::state::{WorkerCtx, WorkerState};
        use std::sync::{atomic::AtomicUsize, Arc, RwLock};
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let mut st = WorkerState {
            publishing_session: Some(Arc::new(zenoh::open(c).await.unwrap())),
            ..Default::default()
        };
        let (tx, rx) = crate::worker::pipeline::event_channel(64);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        // `demo//x` has an empty chunk: zenoh rejects it when the put resolves.
        handle_publish(
            &mut st,
            &ctx,
            "demo//x".into(),
            b"v".to_vec(),
            "text/plain".into(),
            false,
            None,
        )
        .await;
        let events: Vec<ZenohEvent> = rx.try_iter().collect();
        assert!(events.iter().any(|e| matches!(
            e,
            ZenohEvent::OperationFailed {
                op: FailedOp::Publish,
                ..
            }
        )));
        assert!(!events.iter().any(|e| matches!(
            e,
            ZenohEvent::MessageReceived(_) | ZenohEvent::Published { .. }
        )));
        assert!(
            ctx.local_kvstore.read().unwrap().is_empty(),
            "a failed put must not be served by the queryable"
        );
    }
}
