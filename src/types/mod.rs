//! Shared constants, helpers and the data types used by the GUI and worker.

mod commands;
mod limits;
mod message;
mod store;
mod tree;

pub use {commands::*, limits::*, message::*, store::*, tree::*};

// ── Constants ────────────────────────────────────────────────────────────────

/// UI preview size for browse tree nodes
pub const PAYLOAD_PREVIEW_SIZE: usize = 10 * 1024;

/// Message truncation size for display
pub const MAX_UI_DISPLAY_SIZE: usize = 50 * 1024;

// Font sizes
pub const HEADING_LARGE_SIZE: f32 = 24.0; // Main app title
pub const HEADING_MEDIUM_SIZE: f32 = 18.0; // Section headings
pub const TEXT_SMALL_SIZE: f32 = 13.0; // Secondary info
pub const TOPIC_PREVIEW_TEXT_SIZE: f32 = 13.0; // Topic preview in tree
pub const SUBSCRIPTION_TEXT_SIZE: f32 = 13.0; // Subscription list items

// ── Helper functions ─────────────────────────────────────────────────────────

/// Safely find a valid UTF-8 char boundary at or before the given index
pub fn safe_truncate_index(s: &str, max_len: usize) -> usize {
    if max_len >= s.len() {
        return s.len();
    }
    // Find a valid char boundary at or before max_len
    let bytes = s.as_bytes();
    let mut end = max_len;
    // UTF-8 continuation bytes start with 10xxxxxx (0x80-0xBF)
    while end > 0 && end < bytes.len() && (bytes[end] & 0b11000000) == 0b10000000 {
        end -= 1;
    }
    end
}
