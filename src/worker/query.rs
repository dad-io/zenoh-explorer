//! Query: sends a get and forwards its replies.

use tracing::{debug, error, info};

use super::pipeline::{send_event, send_sample};
use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

/// Query arm: sends the get on the publishing session and forwards every reply.
pub(crate) async fn handle_query(
    st: &mut WorkerState,
    ctx: &WorkerCtx,
    selector: String,
    value: String,
    timeout_ms: u64,
) {
    if let Some(ref sess) = st.publishing_session {
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
                debug!("Query sent successfully (target=All, consolidation=None), waiting for replies...");

                let event_sender_query = ctx.event_sender.clone();
                let drops = ctx.sample_drops.clone();
                let selector_clone = selector.clone();
                let own_zid = sess.zid();
                tokio::spawn(async move {
                    let mut received_replies = false; // any reply, sample or error
                    let mut samples = 0usize;
                    let mut timeout_reported = false;
                    while let Ok(reply) = replies.recv_async().await {
                        debug!("Received a reply from query");
                        received_replies = true;
                        // Local means this session answered, whatever the reply carries.
                        let is_local = reply.replier_id().is_some_and(|g| g.zid() == own_zid);
                        match reply.result() {
                            Ok(sample) => {
                                samples += 1;
                                debug!(
                                    "Query reply OK: key={} is_local={}",
                                    sample.key_expr(),
                                    is_local
                                );
                                let message = super::samples::message_from_sample(
                                    sample,
                                    MessageType::QueryReply,
                                    is_local,
                                    MessageSource::PublishingSession,
                                );
                                send_sample(&event_sender_query, &drops, message);
                            }
                            Err(e) => {
                                let text = crate::payload::preview(&e.payload().to_bytes(), 1024);
                                if text == "Timeout" {
                                    // Raised by this session or a router, never by a queryable.
                                    if samples == 0 && !timeout_reported {
                                        timeout_reported = true;
                                        send_event(
                                            &event_sender_query,
                                            ZenohEvent::OperationFailed {
                                                op: FailedOp::Query,
                                                error: format!(
                                                    "no answer within {timeout_ms} ms from a matching queryable"
                                                ),
                                            },
                                        )
                                        .await;
                                    } else {
                                        debug!("Query timed out after {} replies", samples);
                                    }
                                } else {
                                    error!("Query error reply: {}", text);
                                    send_event(
                                        &event_sender_query,
                                        ZenohEvent::OperationFailed {
                                            op: FailedOp::Query,
                                            error: format!(
                                                "a queryable answered with an error: {text}"
                                            ),
                                        },
                                    )
                                    .await;
                                }
                            }
                        }
                    }

                    info!(
                        "Query reply loop ended, received_replies={}",
                        received_replies
                    );

                    // If no replies were received, send alert
                    if !received_replies {
                        send_event(
                            &event_sender_query,
                            ZenohEvent::QueryNoResponses {
                                selector: selector_clone,
                            },
                        )
                        .await;
                    }
                });
            }
            Err(e) => {
                error!("Failed to send query: {}", e);
                send_event(
                    &ctx.event_sender,
                    ZenohEvent::OperationFailed {
                        op: FailedOp::Query,
                        error: e.to_string(),
                    },
                )
                .await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::types::*;
    use crate::worker::{pipeline, zenoh_worker};
    use std::sync::{atomic::AtomicUsize, Arc, RwLock};
    use std::time::{Duration, Instant};

    #[test]
    #[ignore = "opens network sessions"]
    fn invalid_selector_reports_query_failure() {
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        let (ev_tx, ev_rx) = pipeline::event_channel(10_000);
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let worker = std::thread::spawn(move || {
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(zenoh_worker(cmd_rx, ev_tx, store, drops))
        });
        let wait_for = |pred: &dyn Fn(&ZenohEvent) -> bool, secs: u64| {
            let end = Instant::now() + Duration::from_secs(secs);
            while Instant::now() < end {
                if let Ok(e) = ev_rx.recv_timeout(Duration::from_millis(200)) {
                    if pred(&e) {
                        return true;
                    }
                }
            }
            false
        };
        cmd_tx
            .send(ZenohCommand::Connect {
                locators: String::new(),
                listen_port: "27501".into(),
                mode: "peer".into(),
                config_json: "{}".into(),
            })
            .unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx
            .send(ZenohCommand::Query {
                selector: "demo/".into(), // trailing slash: not a valid key expression
                value: String::new(),
                timeout_ms: 1000,
            })
            .unwrap();
        assert!(wait_for(
            &|e| matches!(
                e,
                ZenohEvent::OperationFailed {
                    op: FailedOp::Query,
                    ..
                }
            ),
            5
        ));
        drop(cmd_tx);
        worker.join().unwrap();
    }

    /// G1-1: a matching queryable that never answers is a timeout, reported once.
    /// It is not "reply error: Timeout", and no QueryNoResponses follows it.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn silent_queryable_reports_timeout_once() {
        use crate::worker::state::{WorkerCtx, WorkerState};
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let sess = Arc::new(zenoh::open(c).await.unwrap());
        let _silent = sess.declare_queryable("q/silent").await.unwrap(); // matches, never answers
        let mut st = WorkerState {
            publishing_session: Some(sess),
            ..Default::default()
        };
        let (tx, rx) = pipeline::event_channel(64);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        super::handle_query(&mut st, &ctx, "q/silent".into(), String::new(), 300).await;
        match rx.recv_timeout(Duration::from_secs(5)) {
            Ok(ZenohEvent::OperationFailed {
                op: FailedOp::Query,
                error,
            }) => {
                assert_eq!(error, "no answer within 300 ms from a matching queryable")
            }
            other => panic!("expected a query timeout, got {other:?}"),
        }
        assert!(
            rx.recv_timeout(Duration::from_secs(1)).is_err(),
            "one outcome only: no QueryNoResponses after the timeout"
        );
    }
}
