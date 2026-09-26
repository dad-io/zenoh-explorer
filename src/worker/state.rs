//! The worker's mutable session state and its shared context.

use std::collections::HashMap;
use std::sync::atomic::AtomicUsize;
use std::sync::{Arc, RwLock};
use zenoh::Session;

use super::pipeline::EventTx;
use crate::types::*;

/// Sessions, subscriptions and the queryable owned by the worker loop.
#[derive(Default)]
pub(crate) struct WorkerState {
    // Dual session architecture:
    // - publishing_session: handles user's explicit subscribe/publish/query operations
    // - monitor_session: auto-subscribes to ** to observe actual wire traffic
    pub publishing_session: Option<Arc<Session>>,
    pub monitor_session: Option<Arc<Session>>,
    // Map of active subscriptions by ID for management (user subscriptions on publishing session)
    pub active_subscriptions: HashMap<String, ActiveSubscription>,
    // Monitor session's ** subscription (background traffic observation)
    pub monitor_subscription: Option<ActiveSubscription>,
    // Active queryable and its associated task
    pub queryable_task: Option<(tokio::task::JoinHandle<()>, tokio::sync::mpsc::Sender<()>)>,
    // The publishing session's peer/router poller (aborted by `teardown`)
    pub discovery_task: Option<tokio::task::JoinHandle<()>>,
    // (id, key_expr) of user subscriptions kept by `teardown`, re-declared on the next connect
    pub resubscribe: Vec<(String, String)>,
}

impl WorkerState {
    /// Stop every task and close both sessions. Idempotent.
    ///
    /// With `keep_subscriptions`, each user subscription's `(id, key_expr)` goes
    /// to `resubscribe` so the next successful connect declares it again; the
    /// final shutdown passes `false`.
    pub(crate) async fn teardown(&mut self, keep_subscriptions: bool) {
        if let Some(t) = self.discovery_task.take() {
            t.abort();
        }
        if let Some((h, tx)) = self.queryable_task.take() {
            let _ = tx.try_send(());
            h.abort();
        }
        if let Some(sub) = self.monitor_subscription.take() {
            let _ = sub.cancel_sender.send(());
            sub.task_handle.abort();
        }
        for (id, sub) in self.active_subscriptions.drain() {
            let _ = sub.cancel_sender.send(());
            sub.task_handle.abort();
            if keep_subscriptions {
                self.resubscribe.push((id, sub.key_expr));
            }
        }
        for s in [self.monitor_session.take(), self.publishing_session.take()]
            .into_iter()
            .flatten()
        {
            if let Err(e) = s.close().await {
                tracing::error!("Session close failed: {}", e);
            }
        }
    }
}

/// The worker's channels and stores, shared by every handler.
pub(crate) struct WorkerCtx {
    pub event_sender: EventTx,
    pub local_kvstore: Arc<RwLock<LocalKvStore>>,
    /// Samples the pipeline could not accept (counted by `send_sample`).
    pub sample_drops: Arc<AtomicUsize>,
}
