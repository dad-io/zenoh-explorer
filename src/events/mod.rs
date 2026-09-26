//! Event processing, deduplication, hashing, and message storage.
//!
//! Handles the flow of ZenohEvents from the worker thread into the GUI state:
//! dedup checks, rate limiting, browse tree updates, and message storage with limits.

mod ingest;
mod json_cache;

use std::time::{Duration, Instant};
use tracing::{debug, error, info};

use crate::app::{UiAlert, ZenohExplorer, IDLE_REPAINT_SECS};
use crate::types::*;

/// How long after the last Pong (or ping cycle) the next Ping goes out.
pub(crate) const PING_INTERVAL: Duration = Duration::from_secs(5);
/// How long a Ping may go unanswered before the worker counts as not answering.
pub(crate) const WORKER_TIMEOUT: Duration = Duration::from_secs(10);
/// A gap between frames longer than this is the UI thread's own stall. It must
/// stay above the idle repaint interval.
pub(crate) const UI_STALL: Duration = Duration::from_secs(2 * IDLE_REPAINT_SECS);

impl ZenohExplorer {
    /// Processes all pending events from the Zenoh worker thread.
    /// This is called on each frame to keep the UI in sync with network activity.
    pub(crate) fn process_events(&mut self) {
        // Collect all pending events without blocking
        let events: Vec<ZenohEvent> = if let Some(receiver) = &self.event_receiver {
            let mut events = Vec::new();
            let budget = Instant::now() + Duration::from_millis(8);
            self.events_pending = false;
            while let Ok(event) = receiver.try_recv() {
                events.push(event);
                if Instant::now() >= budget {
                    self.events_pending = true;
                    break;
                }
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
                ZenohEvent::PublishingConnected => {
                    // Publishing session connected, waiting for monitor session
                    info!("GUI received PublishingConnected event");
                    self.connection_status = ConnectionStatus::ConnectingMonitor;
                    self.monitor_ok = true;
                }
                ZenohEvent::MonitorConnected => {
                    // Both sessions are now connected
                    info!("GUI received MonitorConnected event - fully connected");
                    self.connection_status = ConnectionStatus::Connected;
                }
                ZenohEvent::Disconnected => {
                    // The old worker session's query ended with it, even when a new
                    // connect is already under way.
                    if let Some(selector) = self
                        .query_alert
                        .as_deref()
                        .and_then(|a| a.strip_prefix("Query sent for '"))
                        .and_then(|rest| rest.split_once('\''))
                        .map(|(sel, _)| sel.to_string())
                    {
                        self.query_alert =
                            Some(format!("Query for '{}' cancelled: disconnected", selector));
                    }
                    self.end_pending_work();
                    // The worker handles commands in order and Disconnect is disabled
                    // while connecting, so a Disconnected seen while connecting belongs
                    // to an earlier Disconnect: it must not cancel the new connect. No
                    // `return` here, which would drop the rest of this batch.
                    if !matches!(
                        self.connection_status,
                        ConnectionStatus::ConnectingPublishing
                            | ConnectionStatus::ConnectingMonitor
                    ) {
                        self.connection_status = ConnectionStatus::Disconnected;
                        self.discovered_peers = 0;
                        self.discovered_routers = 0;
                    }
                    // The worker's teardown killed the queryable either way. The
                    // subscription list stays: the worker re-declares it on reconnect.
                    self.queryable_enabled = false;
                }
                ZenohEvent::DiscoveryUpdate { peers, routers } => {
                    self.discovered_peers = peers;
                    self.discovered_routers = routers;
                }
                ZenohEvent::ConnectionError(err) => {
                    self.end_pending_work();
                    self.connection_status =
                        ConnectionStatus::Error(crate::validation::strip_source_path(&err));
                }
                ZenohEvent::MessageReceived(message) => {
                    self.process_single_message(message);
                }
                ZenohEvent::MessageBatch(messages) => {
                    // Process batch of messages efficiently
                    for message in messages {
                        self.process_single_message(message);
                    }
                }
                ZenohEvent::SubscriptionCreated { id, key_expr } => {
                    self.pending_subscribes.remove(&key_expr);
                    // A row re-declared after reconnect keeps its id (T27 part a).
                    if !self.subscriptions.iter().any(|s| s.id == id) {
                        self.subscriptions.push(Subscription {
                            id,
                            key_expr,
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                }
                ZenohEvent::SubscriptionRemoved { id } => {
                    self.subscriptions.retain(|s| s.id != id);
                }
                ZenohEvent::QueryNoResponses { selector } => {
                    // After a disconnect the query was cancelled, not answered.
                    if matches!(self.connection_status, ConnectionStatus::Connected) {
                        self.query_alert = Some(format!(
                            "No replies for '{}'. No queryable matched it, or none that matched answered.",
                            selector
                        ));
                    }
                }
                ZenohEvent::Pong => {
                    // Worker is alive; the next ping waits a full interval
                    self.worker_healthy = true;
                    self.last_health_check = Instant::now();
                    self.ping_sent_at = None;
                }
                ZenohEvent::OperationFailed { op, error } => {
                    // One strip of zenoh's source path for every worker error.
                    let error = crate::validation::strip_source_path(&error);
                    let msg = format!("{:?} failed: {}", op, error);
                    error!("{}", msg);
                    match op {
                        FailedOp::Query => self.query_alert = Some(msg.clone()),
                        FailedOp::Queryable => self.queryable_enabled = false,
                        FailedOp::Publish => {
                            self.publish_status = Some(PublishStatus::Failed(error.clone()))
                        }
                        FailedOp::Subscribe => self.pending_subscribes.clear(),
                        FailedOp::Monitor => self.monitor_ok = false,
                    }
                    self.ui_alert = Some(UiAlert::Error(msg));
                }
                ZenohEvent::Published { key, bytes } => {
                    self.publish_status = Some(PublishStatus::Published {
                        key,
                        bytes,
                        at: chrono::Utc::now(),
                    })
                }
            }
        }

        self.health_tick(Instant::now());
    }

    /// Nothing will answer work still pending when the worker session ends: a
    /// put still `Sending` (the worker handles commands in order, so any answer
    /// would have come first) or a Subscribe not yet confirmed.
    fn end_pending_work(&mut self) {
        if matches!(self.publish_status, Some(PublishStatus::Sending { .. })) {
            self.publish_status = None;
        }
        self.pending_subscribes.clear();
    }

    /// Sends one Ping per interval and marks the worker unhealthy when a Ping
    /// goes unanswered for `WORKER_TIMEOUT`.
    pub(crate) fn health_tick(&mut self, now: Instant) {
        // A long gap since the last frame is the UI thread's own stall (a modal
        // dialog, a blocking read). Do not blame the worker for it.
        if now.duration_since(self.last_tick_at) > UI_STALL {
            self.last_health_check = now;
            self.ping_sent_at = None;
        }
        self.last_tick_at = now;
        if now.duration_since(self.last_health_check) >= PING_INTERVAL
            && self.ping_sent_at.is_none()
        {
            if let Some(sender) = &self.command_sender {
                debug!("Sending health check ping");
                if sender.send(ZenohCommand::Ping).is_err() {
                    error!("Worker command channel closed");
                    self.worker_gone = true;
                    self.worker_healthy = false;
                    // G2-5: nothing will answer a put still pending in a dead worker.
                    if matches!(self.publish_status, Some(PublishStatus::Sending { .. })) {
                        self.publish_status =
                            Some(PublishStatus::Failed("the worker stopped".into()));
                    }
                }
            }
            self.ping_sent_at = Some(now);
        }
        if let Some(sent) = self.ping_sent_at {
            if now.duration_since(sent) > WORKER_TIMEOUT {
                self.worker_healthy = false;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::app::{UiAlert, ZenohExplorer};
    use crate::types::*;
    use std::time::{Duration, Instant};

    fn capture_commands(app: &mut ZenohExplorer) -> std::sync::mpsc::Receiver<ZenohCommand> {
        let (tx, rx) = std::sync::mpsc::channel();
        app.command_sender = Some(tx);
        rx
    }

    #[test]
    fn operation_failure_is_an_error_alert() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Subscribe,
            error: "bad key".into(),
        })
        .unwrap();
        app.process_events();
        assert!(matches!(app.ui_alert, Some(UiAlert::Error(ref t)) if t.contains("bad key")));
        // G3-6: one strip in this arm covers every source.
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Publish,
            error: "k: boom at /x/y.rs:3.".into(),
        })
        .unwrap();
        app.process_events();
        assert_eq!(
            app.publish_status,
            Some(PublishStatus::Failed("k: boom".into()))
        );
    }

    #[test]
    fn ping_is_sent_once_per_interval() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0;
        for ms in [5_100u64, 5_200, 5_300, 5_400] {
            app.last_tick_at = t0 + Duration::from_millis(ms - 50);
            app.health_tick(t0 + Duration::from_millis(ms));
        }
        assert_eq!(
            cmds.try_iter()
                .filter(|c| matches!(c, ZenohCommand::Ping))
                .count(),
            1
        );
    }

    #[test]
    fn unanswered_ping_marks_unhealthy_after_timeout() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let _cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        let mut t = t0;
        while t < t0 + Duration::from_secs(16) {
            t += Duration::from_millis(500);
            app.last_tick_at = t - Duration::from_millis(500);
            app.health_tick(t);
        }
        assert!(!app.worker_healthy, "10 s after the first unanswered ping");
    }

    #[test]
    fn ui_stall_does_not_mark_worker_unhealthy() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let _cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0;
        app.health_tick(t0 + Duration::from_secs(20)); // first frame after a 20 s modal dialog
        assert!(app.worker_healthy);
    }

    #[test]
    fn no_reply_verdict_does_not_claim_absence() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        tx.send(ZenohEvent::QueryNoResponses {
            selector: "x/**".into(),
        })
        .unwrap();
        app.process_events();
        let a = app.query_alert.unwrap();
        assert!(
            a.contains("No replies")
                && !a.contains("No queryables available")
                && !a.contains("Subscribe instead"),
            "{a}"
        );
    }

    #[test]
    fn disconnect_cancels_waiting_query() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.query_alert = Some("Query sent for 'x/**'. Waiting for responses...".into());
        tx.send(ZenohEvent::Disconnected).unwrap();
        tx.send(ZenohEvent::QueryNoResponses {
            selector: "x/**".into(),
        })
        .unwrap();
        app.process_events();
        assert_eq!(
            app.query_alert.as_deref(),
            Some("Query for 'x/**' cancelled: disconnected")
        );
    }

    #[test]
    fn pending_publish_ends_on_disconnect_or_worker_loss() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.publish_status = Some(PublishStatus::Sending {
            key: "k".into(),
            bytes: 1,
        });
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert_eq!(
            app.publish_status, None,
            "nothing will answer a put still pending at teardown"
        );
        // A closed command channel is a dead worker, not a busy one.
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        drop(cmd_rx);
        app.command_sender = Some(cmd_tx);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0 + Duration::from_millis(4_950);
        app.publish_status = Some(PublishStatus::Sending {
            key: "k".into(),
            bytes: 1,
        }); // G2-5
        app.health_tick(t0 + Duration::from_secs(5));
        assert!(app.worker_gone && !app.worker_healthy);
        assert!(
            !matches!(app.publish_status, Some(PublishStatus::Sending { .. })),
            "a dead worker ends a pending publish"
        );
    }

    #[test]
    fn subscription_rows_merge_by_id() {
        // K8c. sub_1 twice: re-declared after reconnect (T27 part a keeps its id).
        // sub_2: a second worker subscription on the same key.
        let (mut app, tx) = ZenohExplorer::test_app();
        app.pending_subscribes.insert("demo/**".into());
        for id in ["sub_1", "sub_1", "sub_2"] {
            tx.send(ZenohEvent::SubscriptionCreated {
                id: id.into(),
                key_expr: "demo/**".into(),
            })
            .unwrap();
        }
        app.process_events();
        assert!(app.pending_subscribes.is_empty());
        let ids: Vec<&str> = app.subscriptions.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(
            ids,
            ["sub_1", "sub_2"],
            "each worker subscription keeps a removable row"
        );
        app.pending_subscribes.insert("bad/".into());
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Subscribe,
            error: "bad/: invalid".into(),
        })
        .unwrap();
        app.process_events();
        assert!(
            app.pending_subscribes.is_empty(),
            "a failed Subscribe re-enables the button"
        );
    }

    #[test]
    fn failed_query_replaces_waiting_alert() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.query_alert = Some("Query sent for 'x'. Waiting for responses...".into());
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Query,
            error: "bad selector".into(),
        })
        .unwrap();
        app.process_events();
        assert!(app.query_alert.clone().unwrap().contains("bad selector"));
    }

    #[test]
    fn failed_queryable_unchecks_toggle() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.queryable_enabled = true;
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Queryable,
            error: "x".into(),
        })
        .unwrap();
        app.process_events();
        assert!(!app.queryable_enabled);
        assert!(matches!(&app.ui_alert, Some(UiAlert::Error(t)) if t.contains("Queryable")));
    }

    #[test]
    fn stale_disconnected_does_not_cancel_new_connect() {
        let (mut app, tx) = ZenohExplorer::test_app();
        // The worker handles commands in order and Disconnect is disabled while
        // connecting, so a Disconnected seen while connecting belongs to an
        // earlier Disconnect.
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        app.queryable_enabled = true;
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert!(matches!(
            app.connection_status,
            ConnectionStatus::ConnectingPublishing
        ));
        assert!(
            !app.queryable_enabled,
            "the worker's teardown killed the queryable"
        );
        // In one batch, a stale Disconnected must not end the batch early.
        tx.send(ZenohEvent::Disconnected).unwrap();
        tx.send(ZenohEvent::PublishingConnected).unwrap();
        app.process_events();
        assert!(matches!(
            app.connection_status,
            ConnectionStatus::ConnectingMonitor
        ));
    }

    #[test]
    fn publish_outcome_updates_status() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::Published {
            key: "k".into(),
            bytes: 3,
        })
        .unwrap();
        app.process_events();
        assert!(matches!(
            app.publish_status,
            Some(PublishStatus::Published { bytes: 3, .. })
        ));
        tx.send(ZenohEvent::OperationFailed {
            op: FailedOp::Publish,
            error: "k: bad".into(),
        })
        .unwrap();
        app.process_events();
        assert_eq!(
            app.publish_status,
            Some(PublishStatus::Failed("k: bad".into()))
        );
    }
}
