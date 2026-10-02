//! Connect and Disconnect: the publishing session, discovery and the monitor session.

use std::sync::Arc;
use tracing::{error, info};

use super::connect::{connect_zenoh, connect_zenoh_monitor};
use super::state::{WorkerCtx, WorkerState};
use super::subscribe;
use crate::types::*;

/// Connect arm: opens the publishing session, starts discovery, then opens the
/// monitor session and subscribes it to `**`.
pub(crate) async fn handle_connect(
    st: &mut WorkerState,
    ctx: &WorkerCtx,
    locators: String,
    listen_port: String,
    mode: String,
    config_json: String,
) {
    st.teardown(true).await;
    info!(
        "Worker processing connect command - mode: {}, locators: {}, listen_port: {}",
        mode, locators, listen_port
    );

    // Phase 1: Connect the publishing session
    match connect_zenoh(&locators, &listen_port, &mode, &config_json).await {
        Ok(new_session) => {
            info!("Worker successfully created publishing session");
            let session_arc = Arc::new(new_session);
            st.publishing_session = Some(session_arc.clone());

            // Send PublishingConnected event
            match ctx.event_sender.send(ZenohEvent::PublishingConnected) {
                Ok(_) => {
                    info!("Successfully sent PublishingConnected event to GUI")
                }
                Err(e) => {
                    error!("Failed to send PublishingConnected event: {:?}", e)
                }
            }

            // Poll peers/routers until `teardown` aborts this task
            let discovery_session = session_arc.clone();
            let discovery_sender = ctx.event_sender.clone();
            st.discovery_task = Some(tokio::spawn(async move {
                loop {
                    let peers = discovery_session.info().peers_zid().await.count();
                    let routers = discovery_session.info().routers_zid().await.count();
                    // Never block here: a full pipeline drops this update and
                    // the next one comes 2 s later, so teardown's abort can reach us.
                    if let Err(std::sync::mpsc::TrySendError::Disconnected(_)) =
                        discovery_sender.try_send(ZenohEvent::DiscoveryUpdate { peers, routers })
                    {
                        break;
                    }
                    tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                }
            }));

            // Phase 2: Connect the monitor session with scouting disabled
            match connect_zenoh_monitor(&locators, &listen_port, &mode).await {
                Ok(mon_session) => {
                    info!("Worker successfully created monitor session");
                    let mon_session_arc = Arc::new(mon_session);
                    st.monitor_session = Some(mon_session_arc.clone());

                    // Auto-subscribe monitor to ** (all topics)
                    match mon_session_arc.declare_subscriber("**").await {
                        Ok(subscriber) => {
                            info!("Monitor session subscribed to **");
                            let (task_handle, cancel_sender) = subscribe::spawn_sample_task(
                                subscriber,
                                MessageSource::MonitorSession,
                                ctx.event_sender.clone(),
                                ctx.sample_drops.clone(),
                                false,
                            );

                            st.monitor_subscription = Some(ActiveSubscription {
                                key_expr: "**".to_string(),
                                task_handle,
                                cancel_sender,
                            });
                        }
                        Err(e) => {
                            error!("Failed to subscribe monitor to **: {}", e);
                            let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                                op: FailedOp::Monitor,
                                error: e.to_string(),
                            });
                        }
                    }

                    // Send MonitorConnected event (both sessions now ready)
                    match ctx.event_sender.send(ZenohEvent::MonitorConnected) {
                        Ok(_) => info!("Successfully sent MonitorConnected event to GUI"),
                        Err(e) => error!("Failed to send MonitorConnected event: {:?}", e),
                    }
                }
                Err(e) => {
                    // Monitor session failed, but publishing session is still usable
                    error!("Failed to connect monitor session: {}", e);
                    let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                        op: FailedOp::Monitor,
                        error: e.to_string(),
                    });
                    // Still send Connected since publishing session works
                    match ctx.event_sender.send(ZenohEvent::MonitorConnected) {
                        Ok(_) => info!(
                            "Sent MonitorConnected event (monitor failed but publishing works)"
                        ),
                        Err(send_err) => error!("Failed to send MonitorConnected: {:?}", send_err),
                    }
                }
            }

            // Declare again the subscriptions the last teardown kept; each keeps its id.
            for (id, key_expr) in std::mem::take(&mut st.resubscribe) {
                subscribe::handle_subscribe(st, ctx, key_expr, Some(id)).await;
            }
        }
        Err(e) => {
            error!("Worker failed to connect publishing session: {}", e);
            match ctx
                .event_sender
                .send(ZenohEvent::ConnectionError(e.to_string()))
            {
                Ok(_) => info!("Sent ConnectionError event to GUI"),
                Err(send_err) => error!("Failed to send ConnectionError event: {:?}", send_err),
            }
        }
    }
}

/// Disconnect arm: stops every task, closes both sessions and tells the GUI.
pub(crate) async fn handle_disconnect(st: &mut WorkerState, ctx: &WorkerCtx) {
    st.teardown(true).await;
    let _ = ctx.event_sender.send(ZenohEvent::Disconnected);
}

#[cfg(test)]
mod tests {
    use crate::types::*;
    use crate::worker::{pipeline, zenoh_worker};
    use std::sync::{atomic::AtomicUsize, Arc, RwLock};
    use std::time::{Duration, Instant};

    #[test]
    #[ignore = "opens network sessions"]
    fn reconnect_then_disconnect_leaves_no_discovery_updates() {
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        let (ev_tx, ev_rx) = pipeline::event_channel(pipeline::WORKER_EVENT_CAPACITY);
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let worker = std::thread::spawn(move || {
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(zenoh_worker(cmd_rx, ev_tx, store, drops))
        });
        let connect = || ZenohCommand::Connect {
            locators: String::new(),
            listen_port: "27601".into(),
            mode: "peer".into(),
            config_json: "{}".into(),
        };
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
        cmd_tx.send(connect()).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx.send(connect()).unwrap(); // reconnect while connected must not leak
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx.send(ZenohCommand::Disconnect).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::Disconnected), 30));
        // Discovery polls every 2 s; 5 s of silence proves every poller stopped.
        assert!(!wait_for(
            &|e| matches!(e, ZenohEvent::DiscoveryUpdate { .. }),
            5
        ));
        drop(cmd_tx);
        worker.join().unwrap();
    }

    /// The first event within `secs` that `pick` maps to `Some`.
    fn next_event<T>(
        rx: &std::sync::mpsc::Receiver<ZenohEvent>,
        secs: u64,
        mut pick: impl FnMut(ZenohEvent) -> Option<T>,
    ) -> Option<T> {
        let end = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < end {
            if let Ok(e) = rx.recv_timeout(Duration::from_millis(200)) {
                if let Some(t) = pick(e) {
                    return Some(t);
                }
            }
        }
        None
    }

    #[test]
    #[ignore = "opens network sessions"]
    fn reconnect_restores_subscriptions() {
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        let (ev_tx, ev_rx) = pipeline::event_channel(pipeline::WORKER_EVENT_CAPACITY);
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let worker = std::thread::spawn(move || {
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(zenoh_worker(cmd_rx, ev_tx, store, drops))
        });
        let connect = || ZenohCommand::Connect {
            locators: String::new(),
            listen_port: "27602".into(),
            mode: "peer".into(),
            config_json: "{}".into(),
        };
        let subscribe = |key: &str| ZenohCommand::Subscribe {
            key_expr: key.into(),
            reliability: "reliable".into(),
            mode: "push".into(),
        };
        let created = |e: ZenohEvent| match e {
            ZenohEvent::SubscriptionCreated { id, key_expr } => Some((id, key_expr)),
            _ => None,
        };

        cmd_tx.send(connect()).unwrap();
        assert!(
            next_event(&ev_rx, 60, |e| matches!(e, ZenohEvent::MonitorConnected)
                .then_some(()))
            .is_some()
        );
        cmd_tx.send(subscribe("t/a/**")).unwrap();
        cmd_tx.send(subscribe("t/b/**")).unwrap();
        let first = next_event(&ev_rx, 10, created).expect("first SubscriptionCreated");
        let second = next_event(&ev_rx, 10, created).expect("second SubscriptionCreated");
        let id_of = |key: &str| {
            [&first, &second]
                .into_iter()
                .find(|(_, k)| k == key)
                .map(|(id, _)| id.clone())
                .unwrap_or_else(|| panic!("no SubscriptionCreated for {key}"))
        };
        let (a_id, b_id) = (id_of("t/a/**"), id_of("t/b/**"));

        cmd_tx.send(ZenohCommand::Disconnect).unwrap();
        assert!(
            next_event(&ev_rx, 30, |e| matches!(e, ZenohEvent::Disconnected)
                .then_some(()))
            .is_some()
        );

        // While disconnected, Unsubscribe must still answer: the UI removes a row only on this event.
        cmd_tx
            .send(ZenohCommand::Unsubscribe {
                subscription_id: b_id.clone(),
            })
            .unwrap();
        let removed = next_event(&ev_rx, 5, |e| match e {
            ZenohEvent::SubscriptionRemoved { id } => Some(id),
            _ => None,
        });
        assert_eq!(removed.as_deref(), Some(b_id.as_str()));

        cmd_tx.send(connect()).unwrap();
        // No subscription may be re-declared before the second MonitorConnected.
        let mut early = Vec::new();
        let monitor = next_event(&ev_rx, 60, |e| match e {
            ZenohEvent::MonitorConnected => Some(()),
            ZenohEvent::SubscriptionCreated { key_expr, .. } => {
                early.push(key_expr);
                None
            }
            _ => None,
        });
        assert!(monitor.is_some(), "second MonitorConnected");
        assert!(
            early.is_empty(),
            "re-declared before MonitorConnected: {early:?}"
        );
        // Every SubscriptionCreated in the next 5 s: exactly one, for t/a/**, with its old id.
        let mut recreated = Vec::new();
        let end = Instant::now() + Duration::from_secs(5);
        while Instant::now() < end {
            if let Ok(e) = ev_rx.recv_timeout(Duration::from_millis(200)) {
                recreated.extend(created(e));
            }
        }
        assert_eq!(
            recreated
                .iter()
                .map(|(_, k)| k.as_str())
                .collect::<Vec<_>>(),
            ["t/a/**"],
            "only the kept subscription is re-declared"
        );
        assert_eq!(
            recreated[0].0, a_id,
            "the re-declared subscription keeps its id"
        );

        drop(cmd_tx);
        worker.join().unwrap();
    }
}
