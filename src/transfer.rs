//! File transfer pipeline utilities.
//!
//! Handles chunk reassembly, chunk info scanning, payload export to file,
//! and payload preview generation. These functions encapsulate the critical
//! large file transfer logic that MUST be preserved through refactoring.
//!
//! Chunk key format: `{topic}/__chunk/{total_size}/{total_chunks}/{chunk_index}`
//! - CHUNK_SIZE = 64MB (publish side, in zenoh_worker.rs)
//! - MAX_SINGLE_PAYLOAD = u32::MAX ~4GB (publish side, in zenoh_worker.rs)

use std::collections::{HashMap, HashSet};
use tracing::info;

use chrono::{DateTime, Utc};

use crate::types::{PayloadEntry, PayloadStoreMap};

/// Publish-side chunk size (must match zenoh_worker.rs CHUNK_SIZE).
pub const CHUNK_SIZE: usize = 64 * 1024 * 1024;

/// Cap on non-chunk entries in the export store. Chunk entries are exempt:
/// they are bounded per-topic by their own total_chunks and purged by generation.
pub const MAX_PLAIN_ENTRIES: usize = 500;

/// Metadata parsed from a chunk key `{topic}/__chunk/{total_size}/{total_chunks}/{index}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChunkMeta {
    pub total_size: usize,
    pub total_chunks: usize,
    pub index: usize,
}

impl ChunkMeta {
    /// Values come from a network-controlled key string — reject anything that
    /// could drive an absurd allocation or out-of-range index.
    pub fn is_sane(&self) -> bool {
        self.total_chunks > 0
            && self.index < self.total_chunks
            && self.total_size <= self.total_chunks.saturating_mul(CHUNK_SIZE)
    }
}

/// Split a chunk key into (topic, meta). Returns None for non-chunk keys.
pub fn parse_chunk_key(key: &str) -> Option<(&str, ChunkMeta)> {
    let (topic, suffix) = key.split_once("/__chunk/")?;
    let mut parts = suffix.split('/');
    let total_size = parts.next()?.parse().ok()?;
    let total_chunks = parts.next()?.parse().ok()?;
    let index = parts.next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some((
        topic,
        ChunkMeta {
            total_size,
            total_chunks,
            index,
        },
    ))
}

/// Insert a payload into the export store, enforcing the eviction policy:
/// - chunk keys: purge any stale-generation chunks for the same topic, never
///   evict other entries, drop entries with insane metadata
/// - plain keys: evict the oldest plain entry once the cap is reached
///
/// Assumes "/__chunk/" is a reserved infix never used in plain topic names.
pub fn insert_payload(store: &mut PayloadStoreMap, key: String, entry: PayloadEntry) {
    if let Some((topic, meta)) = parse_chunk_key(&key) {
        if !meta.is_sane() {
            info!("Dropping chunk with insane metadata: {}", key);
            return;
        }
        let prefix = format!("{}/__chunk/", topic);
        let stale: Vec<String> = store
            .keys()
            .filter(|k| k.starts_with(&prefix))
            .filter(|k| match parse_chunk_key(k) {
                Some((_, m)) => {
                    (m.total_size, m.total_chunks) != (meta.total_size, meta.total_chunks)
                }
                None => true,
            })
            .cloned()
            .collect();
        for k in stale {
            store.remove(&k);
        }
        store.insert(key, entry);
    } else {
        let plain_count = store.keys().filter(|k| !k.contains("/__chunk/")).count();
        if plain_count >= MAX_PLAIN_ENTRIES && !store.contains_key(&key) {
            let oldest = store
                .iter()
                .filter(|(k, _)| !k.contains("/__chunk/"))
                .min_by_key(|(_, e)| e.received_at)
                .map(|(k, _)| k.clone());
            if let Some(k) = oldest {
                store.remove(&k);
            }
        }
        store.insert(key, entry);
    }
}

/// Progress of the newest chunk group for a topic.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChunkProgress {
    pub received: usize,
    pub total_chunks: usize,
    pub total_size: usize,
}

/// Scan the store for chunks of `topic`. Groups by (total_size, total_chunks)
/// and reports the group with the newest entry — stale generations never
/// inflate the count.
pub fn chunk_progress(store: &PayloadStoreMap, topic: &str) -> Option<ChunkProgress> {
    let prefix = format!("{}/__chunk/", topic);
    let mut groups: HashMap<(usize, usize), (HashSet<usize>, DateTime<Utc>)> = HashMap::new();
    for (key, e) in store.iter() {
        if !key.starts_with(&prefix) {
            continue;
        }
        if let Some((t, m)) = parse_chunk_key(key) {
            if t == topic && m.is_sane() {
                let g = groups
                    .entry((m.total_size, m.total_chunks))
                    .or_insert_with(|| (Default::default(), e.received_at));
                g.0.insert(m.index);
                if e.received_at > g.1 {
                    g.1 = e.received_at;
                }
            }
        }
    }
    let ((total_size, total_chunks), (indices, _)) =
        groups.into_iter().max_by_key(|(_, (_, newest))| *newest)?;
    Some(ChunkProgress {
        received: indices.len(),
        total_chunks,
        total_size,
    })
}

/// A payload ready for export.
#[derive(Debug)]
pub struct ExportPayload {
    pub bytes: Vec<u8>,
    pub filename: Option<String>,
}

/// Retrieve and validate a payload for export.
///
/// 1. Direct lookup by topic key (non-chunked payloads).
/// 2. Chunk reassembly of the NEWEST chunk group: index set must be exactly
///    0..total_chunks and the reassembled length must equal total_size.
///
/// Errors carry a human-readable reason for the UI.
pub fn get_payload_for_export(
    store: &PayloadStoreMap,
    topic: &str,
) -> Result<ExportPayload, String> {
    if let Some(e) = store.get(topic) {
        return Ok(ExportPayload {
            bytes: e.bytes.clone(),
            filename: e.filename.clone(),
        });
    }

    let progress =
        chunk_progress(store, topic).ok_or_else(|| format!("No payload stored for '{}'", topic))?;

    // Collect the newest group's chunks by index (BTreeMap = sorted, dedup by key).
    let mut by_index: std::collections::BTreeMap<usize, &PayloadEntry> = Default::default();
    for (key, e) in store.iter() {
        if let Some((t, m)) = parse_chunk_key(key) {
            if t == topic
                && m.total_size == progress.total_size
                && m.total_chunks == progress.total_chunks
                && m.is_sane()
            {
                by_index.insert(m.index, e);
            }
        }
    }

    if by_index.len() != progress.total_chunks {
        return Err(format!(
            "Incomplete transfer: have {} of {} chunks",
            by_index.len(),
            progress.total_chunks
        ));
    }

    let mut bytes = Vec::with_capacity(progress.total_size);
    let mut filename = None;
    for e in by_index.values() {
        bytes.extend_from_slice(&e.bytes);
        if filename.is_none() {
            filename = e.filename.clone();
        }
    }

    if bytes.len() != progress.total_size {
        return Err(format!(
            "Reassembled size mismatch: got {} bytes, expected {}",
            bytes.len(),
            progress.total_size
        ));
    }

    info!(
        "Reassembled {} chunks into {} bytes",
        progress.total_chunks,
        bytes.len()
    );
    Ok(ExportPayload { bytes, filename })
}

/// Resolve the suggested save-dialog filename:
/// 1. the transmitted original filename, if any
/// 2. the topic's last segment, when it carries a plausible extension
/// 3. fallback: topic with '/'→'_' plus ".bin"
pub fn suggested_export_filename(topic: &str, transmitted: Option<&str>) -> String {
    if let Some(name) = transmitted {
        if !name.trim().is_empty() {
            return name.to_string();
        }
    }
    let last = topic.rsplit('/').next().unwrap_or(topic);
    let has_real_ext = last.rsplit_once('.').is_some_and(|(stem, ext)| {
        !stem.is_empty()
            && !ext.is_empty()
            && ext.len() <= 8
            && ext.chars().all(|c| c.is_ascii_alphanumeric())
    });
    if has_real_ext {
        return last.to_string();
    }
    let flat = topic.replace('/', "_");
    if flat.is_empty() {
        "payload.bin".to_string()
    } else {
        format!("{}.bin", flat)
    }
}

/// Save payload bytes via native dialog. The filter list leads with the
/// suggested name's own extension (never a forced "Binary (*.bin)" first
/// filter, which platforms use to append .bin). Returns Ok(None) if the user
/// cancelled.
pub fn export_payload_to_file(
    suggested_name: &str,
    payload: &[u8],
) -> Result<Option<std::path::PathBuf>, String> {
    let ext = std::path::Path::new(suggested_name)
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_string);

    let mut dialog = rfd::FileDialog::new().set_file_name(suggested_name);
    if let Some(ref ext) = ext {
        dialog = dialog.add_filter(format!("{} files", ext.to_uppercase()), &[ext.as_str()]);
    }
    dialog = dialog.add_filter("All files", &["*"]);

    match dialog.save_file() {
        Some(path) => match std::fs::write(&path, payload) {
            Ok(()) => {
                info!("Exported {} bytes to: {}", payload.len(), path.display());
                Ok(Some(path))
            }
            Err(e) => Err(format!("Failed to write {}: {}", path.display(), e)),
        },
        None => Ok(None),
    }
}

/// Format a byte size into a human-readable string (GB/MB/bytes).
pub fn format_size(size: usize) -> String {
    if size >= 1024 * 1024 * 1024 {
        format!("{:.2} GB", size as f64 / (1024.0 * 1024.0 * 1024.0))
    } else if size >= 1024 * 1024 {
        format!("{:.2} MB", size as f64 / (1024.0 * 1024.0))
    } else {
        format!("{} bytes", size)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{PayloadEntry, PayloadStoreMap};
    use chrono::TimeZone;

    fn entry(ts_secs: i64) -> PayloadEntry {
        PayloadEntry {
            bytes: vec![1, 2, 3],
            received_at: chrono::Utc.timestamp_opt(ts_secs, 0).unwrap(),
            filename: None,
        }
    }

    fn entry_with(bytes: Vec<u8>, ts: i64) -> PayloadEntry {
        PayloadEntry {
            bytes,
            received_at: chrono::Utc.timestamp_opt(ts, 0).unwrap(),
            filename: None,
        }
    }

    #[test]
    fn export_direct_hit() {
        let mut store = PayloadStoreMap::new();
        store.insert("t".into(), entry_with(vec![9, 9], 1));
        assert_eq!(
            get_payload_for_export(&store, "t").unwrap().bytes,
            vec![9, 9]
        );
    }

    #[test]
    fn export_reassembles_in_index_order() {
        let mut store = PayloadStoreMap::new();
        store.insert("t/__chunk/6/2/1".into(), entry_with(vec![4, 5, 6], 1));
        store.insert("t/__chunk/6/2/0".into(), entry_with(vec![1, 2, 3], 2));
        assert_eq!(
            get_payload_for_export(&store, "t").unwrap().bytes,
            vec![1, 2, 3, 4, 5, 6]
        );
    }

    #[test]
    fn export_incomplete_reports_progress() {
        let mut store = PayloadStoreMap::new();
        store.insert("t/__chunk/6/2/0".into(), entry_with(vec![1, 2, 3], 1));
        let err = get_payload_for_export(&store, "t").unwrap_err();
        assert!(err.contains("1 of 2"), "got: {err}");
    }

    #[test]
    fn export_size_mismatch_is_error() {
        let mut store = PayloadStoreMap::new();
        store.insert("t/__chunk/99/2/0".into(), entry_with(vec![1, 2, 3], 1));
        store.insert("t/__chunk/99/2/1".into(), entry_with(vec![4, 5, 6], 2));
        let err = get_payload_for_export(&store, "t").unwrap_err();
        assert!(err.contains("size mismatch"), "got: {err}");
    }

    #[test]
    fn export_nothing_stored_is_error() {
        let store = PayloadStoreMap::new();
        assert!(get_payload_for_export(&store, "t").is_err());
    }

    #[test]
    fn evicts_oldest_plain_entry_at_cap() {
        let mut store = PayloadStoreMap::new();
        for i in 0..MAX_PLAIN_ENTRIES {
            store.insert(format!("topic/{}", i), entry(1000 + i as i64));
        }
        insert_payload(&mut store, "topic/new".into(), entry(99999));
        assert_eq!(store.len(), MAX_PLAIN_ENTRIES);
        assert!(!store.contains_key("topic/0")); // oldest evicted
        assert!(store.contains_key("topic/new"));
    }

    #[test]
    fn chunk_entries_exempt_from_plain_cap() {
        let mut store = PayloadStoreMap::new();
        for i in 0..MAX_PLAIN_ENTRIES {
            store.insert(format!("topic/{}", i), entry(1000 + i as i64));
        }
        // 600 chunks of one transfer all fit alongside the plain cap
        for i in 0..600usize {
            insert_payload(
                &mut store,
                format!("big/file/__chunk/{}/600/{}", 600 * CHUNK_SIZE, i),
                entry(2000),
            );
        }
        let chunk_count = store.keys().filter(|k| k.contains("/__chunk/")).count();
        assert_eq!(chunk_count, 600);
        assert_eq!(store.len(), MAX_PLAIN_ENTRIES + 600);
    }

    #[test]
    fn new_chunk_generation_purges_stale_group() {
        let mut store = PayloadStoreMap::new();
        insert_payload(&mut store, "t/__chunk/200000000/3/0".into(), entry(1));
        insert_payload(&mut store, "t/__chunk/200000000/3/1".into(), entry(2));
        // New transfer on same topic with different metadata
        insert_payload(&mut store, "t/__chunk/300000000/5/0".into(), entry(3));
        assert!(!store.contains_key("t/__chunk/200000000/3/0"));
        assert!(!store.contains_key("t/__chunk/200000000/3/1"));
        assert!(store.contains_key("t/__chunk/300000000/5/0"));
        // Other topics' chunks untouched
        insert_payload(&mut store, "other/__chunk/100/1/0".into(), entry(4));
        insert_payload(&mut store, "t/__chunk/300000000/5/1".into(), entry(5));
        assert!(store.contains_key("other/__chunk/100/1/0"));
    }

    #[test]
    fn insane_chunk_keys_dropped() {
        let mut store = PayloadStoreMap::new();
        insert_payload(
            &mut store,
            "t/__chunk/9999999999999999/1/0".into(),
            entry(1),
        );
        assert!(store.is_empty());
    }

    #[test]
    fn parse_chunk_key_valid() {
        let (topic, m) = parse_chunk_key("demo/file/__chunk/8589934592/128/7").unwrap();
        assert_eq!(topic, "demo/file");
        assert_eq!(
            m,
            ChunkMeta {
                total_size: 8589934592,
                total_chunks: 128,
                index: 7
            }
        );
    }

    #[test]
    fn parse_chunk_key_rejects_malformed() {
        assert!(parse_chunk_key("demo/file").is_none());
        assert!(parse_chunk_key("demo/file/__chunk/abc/2/0").is_none());
        assert!(parse_chunk_key("demo/file/__chunk/100/2").is_none()); // missing index
        assert!(parse_chunk_key("demo/file/__chunk/100/2/0/extra").is_none());
    }

    #[test]
    fn chunk_meta_sanity() {
        // index out of range
        assert!(!ChunkMeta {
            total_size: 100,
            total_chunks: 2,
            index: 2
        }
        .is_sane());
        // zero chunks
        assert!(!ChunkMeta {
            total_size: 100,
            total_chunks: 0,
            index: 0
        }
        .is_sane());
        // total_size exceeds what total_chunks could carry (allocation bomb)
        assert!(!ChunkMeta {
            total_size: usize::MAX,
            total_chunks: 2,
            index: 0
        }
        .is_sane());
        // normal
        assert!(ChunkMeta {
            total_size: 100 * 1024 * 1024,
            total_chunks: 2,
            index: 1
        }
        .is_sane());
    }

    #[test]
    fn update_at_cap_does_not_evict() {
        let mut store = PayloadStoreMap::new();
        for i in 0..MAX_PLAIN_ENTRIES {
            store.insert(format!("topic/{}", i), entry(1000 + i as i64));
        }
        // Overwrite topic/0 (already present) — should not evict any other entry
        insert_payload(&mut store, "topic/0".into(), entry(2000));
        assert_eq!(store.len(), MAX_PLAIN_ENTRIES);
        assert!(store.contains_key("topic/0")); // still present, updated
        assert!(store.contains_key("topic/1")); // nothing else evicted
    }

    #[test]
    fn chunk_progress_counts_distinct_indices_of_newest_group() {
        let mut store = PayloadStoreMap::new();
        // stale group (older timestamps)
        store.insert("t/__chunk/200000000/3/0".into(), entry(1));
        // newest group, 2 of 5 received
        store.insert("t/__chunk/300000000/5/0".into(), entry(10));
        store.insert("t/__chunk/300000000/5/4".into(), entry(11));
        let p = chunk_progress(&store, "t").unwrap();
        assert_eq!(
            (p.received, p.total_chunks, p.total_size),
            (2, 5, 300000000)
        );
    }

    #[test]
    fn chunk_progress_none_for_plain_or_other_topics() {
        let mut store = PayloadStoreMap::new();
        store.insert("t".into(), entry(1));
        store.insert("other/__chunk/100/1/0".into(), entry(1));
        assert!(chunk_progress(&store, "t").is_none());
    }

    #[test]
    fn suggested_name_prefers_transmitted() {
        assert_eq!(
            suggested_export_filename("demo/files", Some("report.pdf")),
            "report.pdf"
        );
    }

    #[test]
    fn suggested_name_uses_last_segment_extension() {
        assert_eq!(
            suggested_export_filename("files/report.pdf", None),
            "report.pdf"
        );
        assert_eq!(
            suggested_export_filename("files/archive.tar.gz", None),
            "archive.tar.gz"
        );
    }

    #[test]
    fn suggested_name_falls_back_to_bin() {
        assert_eq!(
            suggested_export_filename("demo/data", None),
            "demo_data.bin"
        );
        assert_eq!(suggested_export_filename("", None), "payload.bin");
        // dot-segment that isn't a real extension (empty stem / weird ext)
        assert_eq!(
            suggested_export_filename("demo/.hidden", None),
            "demo_.hidden.bin"
        );
        assert_eq!(suggested_export_filename("v1.2/x", None), "v1.2_x.bin");
    }

    #[test]
    fn export_ignores_out_of_range_indices() {
        let mut store = PayloadStoreMap::new();
        // direct inserts bypass insert_payload's sanity gate
        store.insert("t/__chunk/6/2/0".into(), entry_with(vec![1, 2, 3], 1));
        store.insert("t/__chunk/6/2/5".into(), entry_with(vec![4, 5, 6], 2)); // index 5 of 2 — insane
        let err = get_payload_for_export(&store, "t").unwrap_err();
        assert!(err.contains("1 of 2"), "got: {err}");
    }
}
