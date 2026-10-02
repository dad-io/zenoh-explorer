//! Conversion of received Zenoh samples into UI messages.

use crate::types::*;
use chrono::{DateTime, Utc};

/// Convert a received Zenoh sample into a UI message, preserving encoding,
/// kind and the publisher's timestamp. Copies the payload once. The attachment
/// is read as a transmitted filename only for subscription samples.
pub(crate) fn message_from_sample(
    sample: &zenoh::sample::Sample,
    message_type: MessageType,
    is_local: bool,
    source: MessageSource,
) -> ZenohMessage {
    let raw_bytes: Vec<u8> = sample.payload().to_bytes().into_owned();
    let kind = match sample.kind() {
        zenoh::sample::SampleKind::Put => SampleKindView::Put,
        zenoh::sample::SampleKind::Delete => SampleKindView::Delete,
    };
    let display = match kind {
        SampleKindView::Delete => "[DELETE]".to_string(),
        SampleKindView::Put => crate::payload::preview(&raw_bytes, MAX_UI_DISPLAY_SIZE),
    };
    let source_ts = sample
        .timestamp()
        .map(|ts| DateTime::<Utc>::from(ts.get_time().to_system_time()));
    // Only the publish path attaches a filename; a query reply's attachment is
    // other metadata, never a filename.
    let filename = match message_type {
        MessageType::Subscribe => sample
            .attachment()
            .and_then(|a| a.try_to_string().ok())
            .map(|s| s.into_owned()),
        _ => None,
    };
    ZenohMessage::new_with_bytes(
        sample.key_expr().to_string(),
        display,
        raw_bytes,
        sample.encoding().to_string(),
        Utc::now(),
        message_type,
        is_local,
        source,
    )
    .with_filename(filename)
    .with_sample_meta(kind, source_ts)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::time::Duration;

    pub(crate) async fn local_session() -> zenoh::Session {
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false")
            .unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        zenoh::open(c).await.unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn message_from_sample_keeps_encoding_kind_and_timestamp() {
        let s = local_session().await;
        let sub = s.declare_subscriber("t/meta").await.unwrap();
        s.put("t/meta", r#"{"a":1}"#)
            .encoding(zenoh::bytes::Encoding::APPLICATION_JSON)
            .timestamp(s.new_timestamp())
            .await
            .unwrap();
        let put = tokio::time::timeout(Duration::from_secs(5), sub.recv_async())
            .await
            .unwrap()
            .unwrap();
        let m = message_from_sample(
            &put,
            MessageType::Subscribe,
            false,
            MessageSource::PublishingSession,
        );
        assert_eq!(m.encoding, "application/json");
        assert_eq!(m.kind, SampleKindView::Put);
        assert!(m.source_timestamp.is_some());
        assert_eq!(m.payload, r#"{"a":1}"#);

        s.delete("t/meta").await.unwrap();
        let del = tokio::time::timeout(Duration::from_secs(5), sub.recv_async())
            .await
            .unwrap()
            .unwrap();
        let m = message_from_sample(
            &del,
            MessageType::Subscribe,
            false,
            MessageSource::PublishingSession,
        );
        assert_eq!(m.kind, SampleKindView::Delete);
        assert_eq!(m.payload, "[DELETE]");
    }

    /// Only a subscription sample's attachment is a transmitted filename (the
    /// publish path attaches it). A query reply's attachment is other metadata,
    /// never a filename, and must not become the Save File name.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn attachment_is_a_filename_only_on_subscription_samples() {
        let s = local_session().await;

        let sub = s.declare_subscriber("t/named").await.unwrap();
        s.put("t/named", "bytes")
            .attachment("photo.png")
            .await
            .unwrap();
        let put = tokio::time::timeout(Duration::from_secs(5), sub.recv_async())
            .await
            .unwrap()
            .unwrap();
        let m = message_from_sample(
            &put,
            MessageType::Subscribe,
            false,
            MessageSource::PublishingSession,
        );
        assert_eq!(m.filename.as_deref(), Some("photo.png"));

        let queryable = s.declare_queryable("t/reply").await.unwrap();
        let server = tokio::spawn(async move {
            let query = queryable.recv_async().await.unwrap();
            query
                .reply(query.key_expr().clone(), "bytes")
                .attachment("source:local")
                .await
                .unwrap();
        });
        let replies = s
            .get("t/reply")
            .timeout(Duration::from_secs(5))
            .await
            .unwrap();
        let reply = tokio::time::timeout(Duration::from_secs(5), replies.recv_async())
            .await
            .unwrap()
            .unwrap();
        let sample = reply.result().unwrap();
        let m = message_from_sample(
            sample,
            MessageType::QueryReply,
            true,
            MessageSource::PublishingSession,
        );
        assert_eq!(m.filename, None);
        server.await.unwrap();
    }
}
