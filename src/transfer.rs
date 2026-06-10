//! File transfer pipeline utilities.
//!
//! Handles chunk reassembly, chunk info scanning, payload export to file,
//! and payload preview generation. These functions encapsulate the critical
//! large file transfer logic that MUST be preserved through refactoring.
//!
//! Chunk key format: `{topic}/__chunk/{total_size}/{total_chunks}/{chunk_index}`
//! - CHUNK_SIZE = 64MB (publish side, in zenoh_worker.rs)
//! - MAX_SINGLE_PAYLOAD = u32::MAX ~4GB (publish side, in zenoh_worker.rs)

use chrono::{DateTime, Utc};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tracing::info;

/// Publish-side chunk size (must match zenoh_worker.rs CHUNK_SIZE).
#[allow(dead_code)]
pub const CHUNK_SIZE: usize = 64 * 1024 * 1024;

/// Metadata parsed from a chunk key `{topic}/__chunk/{total_size}/{total_chunks}/{index}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code)]
pub struct ChunkMeta {
    pub total_size: usize,
    pub total_chunks: usize,
    pub index: usize,
}

impl ChunkMeta {
    /// Values come from a network-controlled key string — reject anything that
    /// could drive an absurd allocation or out-of-range index.
    #[allow(dead_code)]
    pub fn is_sane(&self) -> bool {
        self.total_chunks > 0
            && self.index < self.total_chunks
            && self.total_size <= self.total_chunks.saturating_mul(CHUNK_SIZE)
    }
}

/// Split a chunk key into (topic, meta). Returns None for non-chunk keys.
#[allow(dead_code)]
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

/// Chunk info returned by `get_chunk_info`: (received_count, total_expected, total_file_size)
pub type ChunkInfo = (usize, usize, usize);

/// Scan payload_store for chunk entries matching a topic.
///
/// Looks for keys matching `{topic}/__chunk/{total_size}/{total_chunks}/{chunk_index}`
/// and returns (received_count, total_expected, total_file_size_bytes) or None.
#[allow(clippy::type_complexity)]
pub fn get_chunk_info(
    payload_store: &Arc<RwLock<HashMap<String, (Vec<u8>, DateTime<Utc>)>>>,
    topic: &str,
) -> Option<ChunkInfo> {
    if let Ok(store) = payload_store.read() {
        let chunk_prefix = format!("{}/__chunk/", topic);
        let mut chunks: Vec<(usize, usize, usize)> = Vec::new(); // (total_size, total_chunks, index)

        for (key, _) in store.iter() {
            if key.starts_with(&chunk_prefix) {
                let suffix = &key[chunk_prefix.len()..];
                let parts: Vec<&str> = suffix.split('/').collect();
                if parts.len() == 3 {
                    if let (Ok(total_size), Ok(total_chunks), Ok(chunk_idx)) = (
                        parts[0].parse::<usize>(),
                        parts[1].parse::<usize>(),
                        parts[2].parse::<usize>(),
                    ) {
                        chunks.push((total_size, total_chunks, chunk_idx));
                    }
                }
            }
        }

        if !chunks.is_empty() {
            let (total_size, total_chunks, _) = chunks[0];
            Some((chunks.len(), total_chunks, total_size))
        } else {
            None
        }
    } else {
        None
    }
}

/// Attempt to retrieve and reassemble a payload from the store.
///
/// 1. First tries direct lookup by topic key (non-chunked payloads).
/// 2. Falls back to chunk reassembly: collects all `{topic}/__chunk/` keys,
///    sorts by chunk_index, verifies completeness, concatenates.
///
/// Returns the full payload bytes or None if unavailable/incomplete.
#[allow(clippy::type_complexity)]
pub fn get_payload_for_export(
    payload_store: &Arc<RwLock<HashMap<String, (Vec<u8>, DateTime<Utc>)>>>,
    topic: &str,
) -> Option<Vec<u8>> {
    if let Ok(store) = payload_store.read() {
        // First try direct lookup
        if let Some((payload, _ts)) = store.get(topic) {
            return Some(payload.clone());
        }

        // Check for chunked payload: look for topic/__chunk/{size}/{count}/{index}
        let chunk_prefix = format!("{}/__chunk/", topic);
        let mut chunks: Vec<(usize, usize, usize, &Vec<u8>)> = Vec::new(); // (total_size, total_chunks, index, data)

        for (key, (data, _ts)) in store.iter() {
            if key.starts_with(&chunk_prefix) {
                // Parse: topic/__chunk/{total_size}/{total_chunks}/{chunk_index}
                let suffix = &key[chunk_prefix.len()..];
                let parts: Vec<&str> = suffix.split('/').collect();
                if parts.len() == 3 {
                    if let (Ok(total_size), Ok(total_chunks), Ok(chunk_idx)) = (
                        parts[0].parse::<usize>(),
                        parts[1].parse::<usize>(),
                        parts[2].parse::<usize>(),
                    ) {
                        chunks.push((total_size, total_chunks, chunk_idx, data));
                    }
                }
            }
        }

        if !chunks.is_empty() {
            // Sort by chunk index
            chunks.sort_by_key(|(_, _, idx, _)| *idx);

            let (total_size, total_chunks, _, _) = chunks[0];

            // Verify we have all chunks
            if chunks.len() == total_chunks {
                // Reassemble
                let mut reassembled = Vec::with_capacity(total_size);
                for (_, _, _, data) in &chunks {
                    reassembled.extend_from_slice(data);
                }

                info!(
                    "Reassembled {} chunks into {} bytes",
                    chunks.len(),
                    reassembled.len()
                );
                return Some(reassembled);
            } else {
                info!("Missing chunks: have {}/{}", chunks.len(), total_chunks);
            }
        }
    }
    None
}

/// Export raw payload bytes to a file via native file dialog.
///
/// Opens a platform-native save dialog with a suggested filename derived from the topic
/// (slashes replaced with underscores, .bin extension). Supports Binary, Text, JSON, and All
/// file type filters.
pub fn export_payload_to_file(topic: &str, payload: &[u8]) {
    // Suggest filename based on topic (replace / with _)
    let suggested_name = topic.replace('/', "_");
    let suggested_name = if suggested_name.is_empty() {
        "payload.bin".to_string()
    } else {
        format!("{}.bin", suggested_name)
    };

    // Open native file save dialog
    if let Some(path) = rfd::FileDialog::new()
        .set_file_name(&suggested_name)
        .add_filter("Binary Files", &["bin"])
        .add_filter("Text Files", &["txt"])
        .add_filter("JSON Files", &["json"])
        .add_filter("All Files", &["*"])
        .save_file()
    {
        // Write raw bytes to file
        match std::fs::write(&path, payload) {
            Ok(_) => {
                info!("Exported {} bytes to: {}", payload.len(), path.display());
            }
            Err(e) => {
                info!("Failed to export payload: {}", e);
            }
        }
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
}
