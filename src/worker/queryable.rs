//! The local queryable that answers from the local kvstore.

use std::sync::{Arc, RwLock};

use tracing::info;
use zenoh::Session;

use super::pipeline::{send_event, EventTx};
use super::state::{WorkerCtx, WorkerState};
use crate::types::{FailedOp, LocalKvStore, StoredValue, ZenohEvent};

/// Entries this queryable may serve for `query`: keys inside the queryable's own
/// `scope` that intersect `query`, using Zenoh's own key-expression semantics
/// (chunk boundaries, `$*`, verbatim `@` chunks).
pub(crate) fn matching_entries(
    store: &LocalKvStore,
    scope: &zenoh::key_expr::keyexpr,
    query: &zenoh::key_expr::keyexpr,
) -> Vec<(String, StoredValue)> {
    store
        .iter()
        .filter(|(k, _)| {
            zenoh::key_expr::KeyExpr::try_from(k.as_str())
                .map(|ke| scope.includes(&ke) && query.intersects(&ke))
                .unwrap_or(false)
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Serve `store` on `key_expr` until `cancel_rx` fires or the session closes.
pub(crate) async fn serve_queryable(
    sess: Arc<Session>,
    key_expr: String,
    store: Arc<RwLock<LocalKvStore>>,
    events: EventTx,
    mut cancel_rx: tokio::sync::mpsc::Receiver<()>,
) {
    let queryable = match sess.declare_queryable(&key_expr).await {
        Ok(q) => q,
        Err(e) => {
            send_event(
                &events,
                ZenohEvent::OperationFailed {
                    op: FailedOp::Queryable,
                    error: e.to_string(),
                },
            )
            .await;
            return;
        }
    };
    info!("Queryable declared on {}", key_expr);
    loop {
        tokio::select! {
            _ = cancel_rx.recv() => break,
            query = queryable.recv_async() => {
                let Ok(query) = query else { break };
                let matches = match store.read() {
                    Ok(s) => matching_entries(&s, queryable.key_expr(), query.key_expr()),
                    Err(_) => Vec::new(),
                };
                for (key, value) in matches {
                    let _ = query
                        .reply(key.as_str(), value.bytes)
                        .encoding(value.encoding.as_str())
                        .await;
                }
            }
        }
    }
}

/// EnableQueryable arm: replaces any running queryable with one on `key_expr`.
pub(crate) async fn handle_enable(st: &mut WorkerState, ctx: &WorkerCtx, key_expr: String) {
    if let Some(ref sess) = st.publishing_session {
        // Cancel existing queryable if any
        if let Some((handle, cancel_tx)) = st.queryable_task.take() {
            let _ = cancel_tx.send(()).await;
            handle.abort();
        }

        let (cancel_tx, cancel_rx) = tokio::sync::mpsc::channel::<()>(1);
        let handle = tokio::spawn(serve_queryable(
            sess.clone(),
            key_expr,
            ctx.local_kvstore.clone(),
            ctx.event_sender.clone(),
            cancel_rx,
        ));

        st.queryable_task = Some((handle, cancel_tx));
    }
}

/// DisableQueryable arm: stops the running queryable, if any.
pub(crate) async fn handle_disable(st: &mut WorkerState) {
    if let Some((handle, cancel_tx)) = st.queryable_task.take() {
        let _ = cancel_tx.send(()).await;
        handle.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn store_with(keys: &[&str]) -> LocalKvStore {
        keys.iter()
            .map(|k| {
                (
                    k.to_string(),
                    StoredValue {
                        bytes: k.as_bytes().to_vec(),
                        encoding: "text/plain".into(),
                    },
                )
            })
            .collect()
    }

    fn matched_in(store: &LocalKvStore, scope: &str, q: &str) -> Vec<String> {
        let scope = zenoh::key_expr::KeyExpr::try_from(scope).unwrap();
        let ke = zenoh::key_expr::KeyExpr::try_from(q).unwrap();
        let mut v: Vec<String> = matching_entries(store, &scope, &ke)
            .into_iter()
            .map(|(k, _)| k)
            .collect();
        v.sort();
        v
    }

    fn matched(store: &LocalKvStore, q: &str) -> Vec<String> {
        matched_in(store, "**", q)
    }

    #[test]
    fn matching_respects_chunk_boundaries() {
        let s = store_with(&["demo/a", "demo/a/b", "demonstration/x"]);
        assert_eq!(matched(&s, "demo/**"), vec!["demo/a", "demo/a/b"]);
    }

    #[test]
    fn matching_single_star_and_subchunk() {
        let s = store_with(&["demo/a", "demo/ab", "demo/a/b"]);
        assert_eq!(matched(&s, "demo/*"), vec!["demo/a", "demo/ab"]);
        assert_eq!(matched(&s, "demo/a$*"), vec!["demo/a", "demo/ab"]);
    }

    #[test]
    fn matching_excludes_verbatim_chunks() {
        let s = store_with(&["@/x", "y"]);
        assert_eq!(matched(&s, "**"), vec!["y"]);
    }

    /// G1-2: `**` sent to a `demo/**` queryable must not return `other/x`.
    #[test]
    fn matching_stays_inside_queryable_pattern() {
        let s = store_with(&["demo/a", "other/x"]);
        assert_eq!(matched_in(&s, "demo/**", "**"), vec!["demo/a"]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn queryable_replies_full_payload() {
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let s = Arc::new(zenoh::open(c).await.unwrap());
        let payload: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        store.write().unwrap().insert(
            "q/big".into(),
            StoredValue {
                bytes: payload.clone(),
                encoding: "application/octet-stream".into(),
            },
        );
        let (_cancel_tx, cancel_rx) = tokio::sync::mpsc::channel(1);
        let (events, _rx) = crate::worker::pipeline::event_channel(16);
        tokio::spawn(serve_queryable(
            s.clone(),
            "q/**".into(),
            store,
            events,
            cancel_rx,
        ));
        tokio::time::sleep(Duration::from_millis(200)).await;
        let replies = s.get("q/**").await.unwrap();
        let reply = tokio::time::timeout(Duration::from_secs(5), replies.recv_async())
            .await
            .unwrap()
            .unwrap();
        let sample = reply.result().unwrap();
        assert_eq!(sample.payload().to_bytes().as_ref(), payload.as_slice());
        assert_eq!(sample.encoding().to_string(), "application/octet-stream");
    }
}
