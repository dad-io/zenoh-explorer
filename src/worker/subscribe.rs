//! Subscribe and Unsubscribe, and the task that forwards a subscriber's samples.

use std::sync::atomic::AtomicUsize;
use std::sync::Arc;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tracing::{debug, error, info};
use zenoh::handlers::FifoChannelHandler;
use zenoh::pubsub::Subscriber;
use zenoh::sample::Sample;

use super::pipeline::{send_sample, EventTx};
use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

static NEXT_SUB_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

/// Process-unique subscription id (the old `sub_{ms}_{len}` could collide after an unsubscribe).
pub(crate) fn next_subscription_id() -> String {
    format!(
        "sub_{}",
        NEXT_SUB_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    )
}

/// The source that tags one user subscription's samples (X1): overlapping
/// subscriptions are different sources, so dedup keeps one copy.
pub(crate) fn subscription_source(id: &str) -> MessageSource {
    MessageSource::UserSubscription(Arc::from(id))
}

/// Subscribe arm: declares the subscriber on the publishing session and
/// registers its sample task. `id` is `Some` when a reconnect re-declares a
/// kept subscription, which keeps its id; a new subscription gets a fresh one.
pub(crate) async fn handle_subscribe(
    st: &mut WorkerState,
    ctx: &WorkerCtx,
    key_expr: String,
    id: Option<String>,
) {
    if let Some(ref sess) = st.publishing_session {
        match sess.declare_subscriber(&key_expr).await {
            Ok(subscriber) => {
                let sub_id = id.unwrap_or_else(next_subscription_id);
                let key_expr_clone = key_expr.clone();

                // Spawn a dedicated task to handle incoming messages
                // This allows multiple subscriptions to run concurrently
                let (task_handle, cancel_sender) = spawn_sample_task(
                    subscriber,
                    subscription_source(&sub_id),
                    ctx.event_sender.clone(),
                    ctx.sample_drops.clone(),
                    true,
                );

                // Store the subscription with its handle and cancellation sender
                st.active_subscriptions.insert(
                    sub_id.clone(),
                    ActiveSubscription {
                        key_expr: key_expr.clone(),
                        task_handle,
                        cancel_sender,
                    },
                );

                let _ = ctx.event_sender.send(ZenohEvent::SubscriptionCreated {
                    id: sub_id,
                    key_expr: key_expr_clone,
                });
            }
            Err(e) => {
                error!("Failed to create subscriber: {}", e);
                if let Some(id) = id {
                    // A kept subscription that cannot come back: remove its row.
                    let _ = ctx
                        .event_sender
                        .send(ZenohEvent::SubscriptionRemoved { id });
                }
                let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                    op: FailedOp::Subscribe,
                    error: e.to_string(),
                });
            }
        }
    }
}

/// Unsubscribe arm: stops the subscription's task and tells the GUI.
pub(crate) fn handle_unsubscribe(st: &mut WorkerState, ctx: &WorkerCtx, subscription_id: String) {
    if let Some(subscription) = st.active_subscriptions.remove(&subscription_id) {
        // Send cancellation signal (ignore if already cancelled)
        let _ = subscription.cancel_sender.send(());
        // Abort the task as backup
        subscription.task_handle.abort();
        let _ = ctx.event_sender.send(ZenohEvent::SubscriptionRemoved {
            id: subscription_id,
        });
    }
}

/// Spawns the task that turns a subscriber's samples into `MessageReceived`
/// events, and returns its handle and cancellation sender.
///
/// `source` tags every message. `verbose` selects the user-subscription logging
/// (lines starting with `Subscriber`); without it the task logs like the
/// monitor subscription (lines starting with `Monitor`). Per-sample lines are
/// `debug!`. Samples go through `send_sample`, which counts pipeline drops in
/// `drops`.
pub(crate) fn spawn_sample_task(
    subscriber: Subscriber<FifoChannelHandler<Sample>>,
    source: MessageSource,
    tx: EventTx,
    drops: Arc<AtomicUsize>,
    verbose: bool,
) -> (JoinHandle<()>, oneshot::Sender<()>) {
    let (cancel_sender, mut cancel_receiver) = oneshot::channel();

    let task_handle = tokio::spawn(async move {
        // Use tokio::select! for clean cancellation
        loop {
            tokio::select! {
                // Handle cancellation signal
                _ = &mut cancel_receiver => {
                    if !verbose {
                        info!("Monitor subscription cancelled");
                    }
                    break;
                }
                // Handle incoming messages
                result = subscriber.recv_async() => {
                    match result {
                        Ok(sample) => {
                            if verbose {
                                debug!("Subscriber received sample on key: {}", sample.key_expr());
                            } else {
                                debug!("Monitor received sample on key: {}", sample.key_expr());
                            }
                            let message = super::samples::message_from_sample(&sample, MessageType::Subscribe, false, source.clone());

                            send_sample(&tx, &drops, message);
                        }
                        Err(e) => {
                            if verbose {
                                error!("Subscriber recv error: {:?}", e);
                            } else {
                                error!("Monitor subscriber recv error: {:?}", e);
                            }
                            // Subscriber closed or error, exit loop
                            break;
                        }
                    }
                }
            }
        }
    });

    (task_handle, cancel_sender)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscription_ids_are_unique() {
        let a = super::next_subscription_id();
        let b = super::next_subscription_id();
        assert_ne!(a, b);
    }

    /// X1 (T16 regression): two subscriptions of this session that match one key
    /// both receive each sample. They are two sources, so the list keeps one copy.
    /// A value the publisher repeats still counts every time.
    #[test]
    fn overlapping_subscriptions_list_one_copy() {
        use crate::app::ZenohExplorer;
        let (mut app, tx) = ZenohExplorer::test_app();
        app.deduper.ttl = std::time::Duration::from_secs(60); // do not race the 250 ms window
        let copy = |source: MessageSource| {
            ZenohEvent::MessageReceived(ZenohMessage::new_with_bytes(
                "demo/x".into(),
                "v".into(),
                b"v".to_vec(),
                "text/plain".into(),
                chrono::Utc::now(),
                MessageType::Subscribe,
                false,
                source,
            ))
        };
        for _ in 0..2 {
            // demo/** and demo/x on the publishing session, then the monitor's **
            tx.send(copy(super::subscription_source("sub_1"))).unwrap();
            tx.send(copy(super::subscription_source("sub_2"))).unwrap();
            tx.send(copy(MessageSource::MonitorSession)).unwrap();
        }
        app.process_events();
        assert_eq!(
            app.messages.len(),
            2,
            "one row per sample, not one per subscription"
        );
        assert_eq!(app.messages_deduped, 4);
        let count = app.browse_tree.read().unwrap().children["demo"].children["x"].message_count;
        assert_eq!(count, 2, "the repeated value counts twice");
    }

    /// G1-5 (K8 b): a kept subscription whose re-declare fails loses its row.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failed_redeclare_removes_the_row() {
        use std::sync::RwLock;
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
        // "demo/" is not a valid key expression, so the declare fails.
        super::handle_subscribe(&mut st, &ctx, "demo/".into(), Some("sub_9".into())).await;
        assert!(
            matches!(rx.try_recv(), Ok(ZenohEvent::SubscriptionRemoved { id }) if id == "sub_9")
        );
        assert!(matches!(
            rx.try_recv(),
            Ok(ZenohEvent::OperationFailed {
                op: FailedOp::Subscribe,
                ..
            })
        ));
        assert!(st.active_subscriptions.is_empty());
    }
}
