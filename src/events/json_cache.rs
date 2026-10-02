//! Payload hashing and the pretty-printed JSON cache.

use crate::app::ZenohExplorer;
use crate::types::*;

impl ZenohExplorer {
    /// Hash of the full payload (seahash), used as the JSON cache key.
    pub(crate) fn compute_payload_hash(payload: &str) -> u64 {
        seahash::hash(payload.as_bytes())
    }

    /// Get formatted JSON from cache or parse and cache it
    pub(crate) fn get_cached_json(&mut self, payload: &str) -> Option<String> {
        // Skip JSON parsing for very large payloads
        if payload.len() > MAX_UI_DISPLAY_SIZE {
            return None; // Will fall back to raw text display
        }

        let hash = Self::compute_payload_hash(payload);

        // Check cache first
        if let Some(cached) = self.json_parse_cache.get(&hash) {
            return cached.clone();
        }

        // Parse JSON and cache the result
        let result = if let Ok(json_value) = serde_json::from_str::<serde_json::Value>(payload) {
            if let Ok(pretty) = serde_json::to_string_pretty(&json_value) {
                // Truncate formatted JSON if still too large
                if pretty.len() > MAX_UI_DISPLAY_SIZE {
                    let safe_end = safe_truncate_index(&pretty, MAX_UI_DISPLAY_SIZE);
                    let mut truncated = pretty[..safe_end].to_string();
                    truncated.push_str(&format!(
                        "\n... [+{} bytes of JSON hidden]",
                        pretty.len() - safe_end
                    ));
                    Some(truncated)
                } else {
                    Some(pretty)
                }
            } else {
                None
            }
        } else {
            None
        };

        // Bounded cache: reset when it grows past 256 distinct payloads.
        if self.json_parse_cache.len() > 256 {
            self.json_parse_cache.clear();
        }

        self.json_parse_cache.insert(hash, result.clone());
        result
    }
}

#[cfg(test)]
mod tests {
    use crate::app::ZenohExplorer;

    #[test]
    fn json_cache_distinguishes_shared_prefix() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let prefix = "1,".repeat(2500); // 5000 bytes, beyond the old 4 KB window
        let a = format!("[{}1]", prefix);
        let b = format!("[{}2]", prefix);
        assert_ne!(
            app.get_cached_json(&a).unwrap(),
            app.get_cached_json(&b).unwrap()
        );
    }
}
