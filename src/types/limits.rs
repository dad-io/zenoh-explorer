//! Deduplication and rate limiting of received messages.

use std::time::{Duration, Instant};

use super::MessageSource;

/// How long a recorded sample suppresses the same sample seen by another session.
pub const DEDUP_WINDOW: Duration = Duration::from_millis(250);

/// Collapses the same sample arriving from two sources (two sessions, or two
/// overlapping subscriptions of one session) within a short window.
///
/// Hashes the FULL payload (seahash) so payloads differing anywhere are never
/// conflated, and remembers which source recorded it. Only a match from a
/// *different* source is a duplicate: repeats from one source are real traffic
/// and always count. Checking and recording are separate so a message dropped
/// after the check (e.g. by the rate limiter) doesn't poison its own retransmit.
pub struct Deduper {
    hashes: std::collections::HashMap<u64, (Instant, MessageSource)>,
    last_sweep: Instant,
    pub ttl: Duration,
    pub enabled: bool,
}

impl Deduper {
    pub fn new(ttl: Duration) -> Self {
        Self {
            hashes: Default::default(),
            last_sweep: Instant::now(),
            ttl,
            enabled: true,
        }
    }

    pub fn hash_message(key: &str, payload: &[u8]) -> u64 {
        use std::hash::Hasher;
        let mut h = seahash::SeaHasher::new();
        h.write(key.as_bytes());
        h.write(&[0xff]); // separator: ("ab", "c") must differ from ("a", "bc")
        h.write(payload);
        h.finish()
    }

    /// True if the same (key, payload) was recorded within the TTL by a
    /// *different* source — the same sample from two sources (two sessions, or
    /// two overlapping subscriptions of one session). Repeats from one source
    /// are real traffic and are never suppressed.
    pub fn is_cross_source_duplicate(&mut self, hash: u64, source: &MessageSource) -> bool {
        if self.last_sweep.elapsed() > self.ttl {
            let ttl = self.ttl;
            self.hashes.retain(|_, (t, _)| t.elapsed() < ttl);
            self.last_sweep = Instant::now();
        }
        self.hashes
            .get(&hash)
            .is_some_and(|(t, s)| t.elapsed() < self.ttl && s != source)
    }

    pub fn record(&mut self, hash: u64, source: MessageSource) {
        self.hashes.insert(hash, (Instant::now(), source));
    }
}

/// Tracks message rate to prevent flooding
pub struct RateLimiter {
    pub window_start: Instant,
    pub message_count: usize,
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

    /// Check if we can accept a message, updates the rate limiter state
    pub fn check_and_update(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.window_start);

        // Reset window every second
        if elapsed >= Duration::from_secs(1) {
            self.window_start = now;
            self.message_count = 1;
            true
        } else if self.message_count < self.max_messages_per_second {
            self.message_count += 1;
            true
        } else {
            false // Rate limit exceeded
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedup_differs_when_middle_bytes_differ() {
        // Two 16KB payloads: same first/last 4KB, different middle.
        let mut a = vec![0u8; 16 * 1024];
        let mut b = vec![0u8; 16 * 1024];
        a[8000] = 1;
        b[8000] = 2;
        assert_ne!(
            Deduper::hash_message("k", &a),
            Deduper::hash_message("k", &b)
        );
    }

    #[test]
    fn same_source_repeats_are_not_deduped() {
        let mut d = Deduper::new(Duration::from_secs(60));
        let h = Deduper::hash_message("door/state", b"closed");
        d.record(h, MessageSource::MonitorSession);
        assert!(!d.is_cross_source_duplicate(h, &MessageSource::MonitorSession));
    }

    #[test]
    fn cross_source_duplicate_is_deduped() {
        let mut d = Deduper::new(Duration::from_secs(60));
        let h = Deduper::hash_message("k", b"v");
        d.record(h, MessageSource::LocalEcho);
        assert!(d.is_cross_source_duplicate(h, &MessageSource::MonitorSession));
    }

    #[test]
    fn dedup_expires_after_ttl() {
        let mut d = Deduper::new(Duration::from_millis(1));
        let h = Deduper::hash_message("k", b"x");
        d.record(h, MessageSource::LocalEcho);
        std::thread::sleep(Duration::from_millis(5));
        assert!(!d.is_cross_source_duplicate(h, &MessageSource::MonitorSession));
    }
}
