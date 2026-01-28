//! Utility functions and helper types.

use std::time::{Duration, Instant};

// Constants
pub const MAX_HASH_BYTES: usize = 4 * 1024;
pub const PAYLOAD_PREVIEW_SIZE: usize = 10 * 1024;
pub const MAX_UI_DISPLAY_SIZE: usize = 50 * 1024;

/// Safely find a valid UTF-8 char boundary at or before the given index.
pub fn safe_truncate_index(s: &str, max_len: usize) -> usize {
    if max_len >= s.len() {
        return s.len();
    }
    let bytes = s.as_bytes();
    let mut end = max_len;
    while end > 0 && end < bytes.len() && (bytes[end] & 0b11000000) == 0b10000000 {
        end -= 1;
    }
    end
}

/// Tracks message rate to prevent flooding.
pub struct RateLimiter {
    window_start: Instant,
    message_count: usize,
    pub max_messages_per_second: usize,
}

impl RateLimiter {
    pub fn new(max_messages_per_second: usize) -> Self {
        Self {
            window_start: Instant::now(),
            message_count: 0,
            max_messages_per_second,
        }
    }

    pub fn check_and_update(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.window_start);

        if elapsed >= Duration::from_secs(1) {
            self.window_start = now;
            self.message_count = 1;
            true
        } else if self.message_count < self.max_messages_per_second {
            self.message_count += 1;
            true
        } else {
            false
        }
    }
}

/// Compute hash for message deduplication.
pub fn compute_message_hash(key: &str, payload: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    key.hash(&mut hasher);

    let bytes = payload.as_bytes();
    bytes.len().hash(&mut hasher);

    if bytes.len() > MAX_HASH_BYTES * 2 {
        bytes[..MAX_HASH_BYTES].hash(&mut hasher);
        bytes[bytes.len() - MAX_HASH_BYTES..].hash(&mut hasher);
    } else if bytes.len() > MAX_HASH_BYTES {
        bytes[..MAX_HASH_BYTES].hash(&mut hasher);
    } else {
        bytes.hash(&mut hasher);
    }
    hasher.finish()
}

/// Compute hash for payload caching.
pub fn compute_payload_hash(payload: &str) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    let bytes = payload.as_bytes();
    let hash_slice = if bytes.len() > MAX_HASH_BYTES {
        &bytes[..MAX_HASH_BYTES]
    } else {
        bytes
    };
    hash_slice.hash(&mut hasher);
    hasher.finish()
}
