//! Background task that owns the Zenoh sessions and runs GUI commands.

pub mod connect;
pub mod pipeline;
pub mod publish;
pub mod query;
pub mod queryable;
pub mod samples;
pub mod session;
pub mod state;
pub mod subscribe;

use std::sync::atomic::AtomicUsize;
use std::sync::mpsc::Receiver;
use std::sync::{Arc, RwLock};
use tracing::{debug, error, info};

use crate::types::*;
use pipeline::EventTx;
use state::{WorkerCtx, WorkerState};

/// Worker function that handles all Zenoh operations in a separate async task.
/// This prevents blocking the GUI thread and enables clean cancellation of operations.
///
/// # Arguments
/// * `command_receiver` - Channel to receive commands from the GUI thread
/// * `event_sender` - Channel to send events back to the GUI thread
/// * `local_kvstore` - Shared key-value store for queryable responses
/// * `sample_drops` - Counter of samples the event pipeline could not accept
pub async fn zenoh_worker(
    command_receiver: Receiver<ZenohCommand>,
    event_sender: EventTx,
    local_kvstore: Arc<RwLock<LocalKvStore>>,
    sample_drops: Arc<AtomicUsize>,
) {
    info!("Zenoh worker thread started");

    // Worker state (see `state::WorkerState`) and shared context
    let mut st = WorkerState::default();
    let ctx = WorkerCtx {
        event_sender,
        local_kvstore,
        sample_drops,
    };

    info!("Worker thread main loop starting...");

    // Main event loop - process commands as they arrive
    loop {
        // Use recv_timeout instead of try_recv to avoid busy waiting
        match command_receiver.recv_timeout(std::time::Duration::from_millis(100)) {
            Ok(command) => {
                debug!("Worker received command: {:?}", command);
                match command {
                    ZenohCommand::Connect {
                        locators,
                        listen_port,
                        mode,
                        config_json,
                    } => {
                        session::handle_connect(
                            &mut st,
                            &ctx,
                            locators,
                            listen_port,
                            mode,
                            config_json,
                        )
                        .await
                    }
                    ZenohCommand::Disconnect => session::handle_disconnect(&mut st, &ctx).await,
                    ZenohCommand::Subscribe {
                        key_expr,
                        reliability: _, // TODO: Implement reliability configuration
                        mode: _,        // TODO: Implement mode configuration
                    } => subscribe::handle_subscribe(&mut st, &ctx, key_expr, None).await,
                    ZenohCommand::Publish {
                        key,
                        payload,
                        encoding,
                        from_import,
                        filename,
                    } => {
                        publish::handle_publish(
                            &mut st,
                            &ctx,
                            key,
                            payload,
                            encoding,
                            from_import,
                            filename,
                        )
                        .await
                    }
                    ZenohCommand::Query {
                        selector,
                        value,
                        timeout_ms,
                    } => query::handle_query(&mut st, &ctx, selector, value, timeout_ms).await,
                    ZenohCommand::Unsubscribe { subscription_id } => {
                        // While disconnected the subscription waits in `resubscribe`, and
                        // `handle_unsubscribe` finds nothing: answer here, because the UI
                        // removes a row only on `SubscriptionRemoved`.
                        let kept = st.resubscribe.len();
                        st.resubscribe.retain(|(id, _)| *id != subscription_id);
                        if st.resubscribe.len() != kept {
                            let _ = ctx.event_sender.send(ZenohEvent::SubscriptionRemoved {
                                id: subscription_id.clone(),
                            });
                        }
                        subscribe::handle_unsubscribe(&mut st, &ctx, subscription_id)
                    }
                    ZenohCommand::EnableQueryable { key_expr } => {
                        queryable::handle_enable(&mut st, &ctx, key_expr).await
                    }
                    ZenohCommand::DisableQueryable => queryable::handle_disable(&mut st).await,
                    ZenohCommand::Ping => {
                        // Respond with pong to indicate we're alive
                        debug!("Worker received ping, sending pong");
                        let _ = ctx.event_sender.send(ZenohEvent::Pong);
                    }
                }
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                // Normal timeout, continue loop
            }
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                error!("Command channel disconnected, worker thread exiting");
                st.teardown(false).await;
                break;
            }
        }
    }
}
