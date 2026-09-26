//! The buffer thread that batches worker messages for the UI.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::types::*;

pub const WORKER_EVENT_CAPACITY: usize = 10_000;
pub const UI_EVENT_CAPACITY: usize = 256;
const BATCH_WINDOW: std::time::Duration = std::time::Duration::from_millis(16);
const MAX_BATCH: usize = 50;

pub(crate) type EventTx = std::sync::mpsc::SyncSender<ZenohEvent>;

pub(crate) fn event_channel(capacity: usize) -> (EventTx, std::sync::mpsc::Receiver<ZenohEvent>) {
    std::sync::mpsc::sync_channel(capacity)
}

/// Non-blocking send for data samples: a full pipeline drops and counts.
pub(crate) fn send_sample(tx: &EventTx, drops: &AtomicUsize, msg: ZenohMessage) {
    if let Err(std::sync::mpsc::TrySendError::Full(_)) =
        tx.try_send(ZenohEvent::MessageReceived(msg))
    {
        drops.fetch_add(1, Ordering::Relaxed);
    }
}

/// Send from async code without blocking a runtime thread. When the pipeline
/// is full, the wait moves to the blocking pool. False when the UI side is gone.
pub(crate) async fn send_event(tx: &EventTx, event: ZenohEvent) -> bool {
    match tx.try_send(event) {
        Ok(()) => true,
        Err(std::sync::mpsc::TrySendError::Full(event)) => {
            let tx = tx.clone();
            tokio::task::spawn_blocking(move || tx.send(event).is_ok())
                .await
                .unwrap_or(false)
        }
        Err(std::sync::mpsc::TrySendError::Disconnected(_)) => false,
    }
}

pub fn message_buffer_thread(
    rx: std::sync::mpsc::Receiver<ZenohEvent>,
    ui: EventTx,
    notify: impl Fn() + Send + 'static,
) {
    let mut batch: Vec<ZenohMessage> = Vec::with_capacity(MAX_BATCH);
    let flush = |batch: &mut Vec<ZenohMessage>| -> bool {
        if batch.is_empty() {
            return true;
        }
        let ok = ui
            .send(ZenohEvent::MessageBatch(std::mem::take(batch)))
            .is_ok();
        notify();
        ok
    };
    // Block for the first event: no wake-ups while idle.
    while let Ok(first) = rx.recv() {
        let deadline = std::time::Instant::now() + BATCH_WINDOW;
        let mut next = Some(first);
        loop {
            match next.take() {
                Some(ZenohEvent::MessageReceived(m)) => {
                    batch.push(m);
                    if batch.len() >= MAX_BATCH {
                        break;
                    }
                }
                Some(other) => {
                    if !flush(&mut batch) || ui.send(other).is_err() {
                        return;
                    }
                    notify();
                }
                None => {}
            }
            let now = std::time::Instant::now();
            if now >= deadline {
                break;
            }
            match rx.recv_timeout(deadline - now) {
                Ok(e) => next = Some(e),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => break,
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    flush(&mut batch);
                    return;
                }
            }
        }
        if !flush(&mut batch) {
            return;
        }
    }
    flush(&mut batch);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(i: usize) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            format!("k/{i}"),
            "p".into(),
            vec![],
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        )
    }

    #[test]
    fn buffer_thread_batches_and_preserves_all_messages() {
        let (tx, rx) = event_channel(1000);
        let (utx, urx) = event_channel(1000);
        let h = std::thread::spawn(move || message_buffer_thread(rx, utx, || {}));
        for i in 0..120 {
            tx.send(ZenohEvent::MessageReceived(msg(i))).unwrap();
        }
        drop(tx);
        h.join().unwrap();
        let mut total = 0;
        for ev in urx.try_iter() {
            if let ZenohEvent::MessageBatch(b) = ev {
                assert!(b.len() <= 50);
                total += b.len();
            }
        }
        assert_eq!(total, 120);
    }

    #[test]
    fn send_sample_counts_drops_when_full() {
        let (tx, _rx) = event_channel(1);
        let drops = AtomicUsize::new(0);
        send_sample(&tx, &drops, msg(0));
        send_sample(&tx, &drops, msg(1));
        assert_eq!(drops.load(Ordering::Relaxed), 1);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn send_event_does_not_block_the_runtime() {
        use std::time::{Duration, Instant};
        let (tx, rx) = event_channel(1);
        tx.try_send(ZenohEvent::Pong).unwrap(); // the pipeline is full
        let sender = tokio::spawn({
            let tx = tx.clone();
            async move { send_event(&tx, ZenohEvent::Pong).await }
        });
        let drain = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            (rx.recv().is_ok(), rx.recv().is_ok())
        });
        // The runtime's only thread stays free while the send waits for room.
        let start = Instant::now();
        for _ in 0..5 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "the runtime thread was blocked"
        );
        assert!(sender.await.unwrap());
        assert_eq!(drain.join().unwrap(), (true, true));
    }
}
