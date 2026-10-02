//! The browse tree: nodes, chunked-transfer state and the filter walk.

use chrono::{DateTime, Utc};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use super::SampleKindView;

/// In-flight or completed chunked file transfer, tracked on the parent topic node.
#[derive(Debug, Clone)]
pub struct TransferState {
    pub total_size: usize,
    pub total_chunks: usize,
    pub received: std::collections::HashSet<usize>,
    pub last_update: Instant,
}

impl TransferState {
    /// Returns true when all chunks have been received.
    pub fn is_complete(&self) -> bool {
        self.received.len() == self.total_chunks
    }
}

/// What a branch holds below it: topics with data, their messages, and the
/// latest time any of them received one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SubtreeSummary {
    pub topics: usize,
    pub messages: usize,
    pub last_seen: Option<Instant>,
}

/// Represents a node in the hierarchical browse tree.
/// Each node can have children (forming a tree structure) and maintains
/// metadata about the last received message for that key path.
#[derive(Debug, Clone)]
pub struct ZenohNode {
    pub key: String,
    pub children: BTreeMap<String, ZenohNode>,
    pub last_seen: Instant,
    pub message_count: usize,
    pub last_payload: Option<String>,
    pub last_encoding: Option<String>,
    pub is_local: bool, // True if this key was published from this app instance
    /// Number of leaf nodes in the subtree rooted at this node (self counts as 1 when childless).
    pub cumulative_leaves: usize,
    /// In-flight or completed chunked transfer state for this topic node.
    pub transfer: Option<TransferState>,
    /// Kind (PUT or DELETE) of the last sample on this key.
    pub last_kind: SampleKindView,
    /// Publisher timestamp of the last sample on this key, if it carried one.
    pub last_source_time: Option<DateTime<Utc>>,
}

impl ZenohNode {
    /// Creates a new tree node with the given key.
    pub fn new(key: String) -> Self {
        Self {
            key,
            children: BTreeMap::new(), // Use BTreeMap for sorted keys
            last_seen: Instant::now(),
            message_count: 0,
            last_payload: None,
            last_encoding: None,
            is_local: false,
            cumulative_leaves: 1, // Every node starts as its own leaf
            transfer: None,
            last_kind: SampleKindView::Put,
            last_source_time: None,
        }
    }

    /// Insert a key path, creating nodes as needed, and return the leaf node.
    /// Maintains `cumulative_leaves` (count of leaf nodes in each subtree)
    /// incrementally: ancestors gain +1 only when a genuinely new leaf is
    /// attached under a node that already had children. (A leaf converting to
    /// a branch keeps subtree leaf-count unchanged: itself out, new leaf in.)
    pub fn insert_path(&mut self, key: &str) -> &mut ZenohNode {
        let parts: Vec<&str> = key.split('/').filter(|p| !p.is_empty()).collect();

        // Find the first missing segment and whether its parent had children.
        let mut probe: &ZenohNode = self;
        let mut divergence: Option<usize> = None;
        for (i, part) in parts.iter().enumerate() {
            match probe.children.get(*part) {
                Some(child) => probe = child,
                None => {
                    divergence = Some(i);
                    break;
                }
            }
        }
        let bump = divergence.is_some() && !probe.children.is_empty();

        let mut node = self;
        for (i, part) in parts.iter().enumerate() {
            if bump && divergence.is_some_and(|d| i <= d) {
                node.cumulative_leaves += 1;
            }
            node = node
                .children
                .entry(part.to_string())
                .or_insert_with(|| ZenohNode::new(part.to_string()));
        }
        node
    }

    /// Updates the node with new message data.
    /// Tracks when the data was last seen and increments the message count.
    pub fn update_data(
        &mut self,
        payload: String,
        encoding: String,
        is_local: bool,
        kind: SampleKindView,
        source_time: Option<DateTime<Utc>>,
    ) {
        self.last_seen = Instant::now();
        self.message_count += 1;
        self.last_payload = Some(payload);
        self.last_encoding = Some(encoding);
        self.last_kind = kind;
        self.last_source_time = source_time;
        // The marker follows the latest value: a remote value replaces ours.
        self.is_local = is_local;
    }

    /// Summary of the descendants (not this node): how many have data, their
    /// total message count and the latest `last_seen` among them.
    pub fn subtree_summary(&self) -> SubtreeSummary {
        fn walk(node: &ZenohNode, s: &mut SubtreeSummary) {
            for child in node.children.values() {
                if child.message_count > 0 {
                    s.topics += 1;
                    s.messages += child.message_count;
                    s.last_seen = Some(match s.last_seen {
                        Some(t) => t.max(child.last_seen),
                        None => child.last_seen,
                    });
                }
                walk(child, s);
            }
        }
        let mut s = SubtreeSummary {
            topics: 0,
            messages: 0,
            last_seen: None,
        };
        walk(self, &mut s);
        s
    }

    /// Record a received chunk on the parent topic's node (no __chunk subtree
    /// is materialized). A chunk from a different (size, chunks) generation
    /// resets the transfer state.
    pub fn record_chunk(&mut self, topic: &str, meta: crate::transfer::ChunkMeta) {
        let node = self.insert_path(topic);
        let stale = node.transfer.as_ref().is_some_and(|t| {
            (t.total_size, t.total_chunks) != (meta.total_size, meta.total_chunks)
        });
        if stale || node.transfer.is_none() {
            node.transfer = Some(TransferState {
                total_size: meta.total_size,
                total_chunks: meta.total_chunks,
                received: Default::default(),
                last_update: Instant::now(),
            });
        }
        let t = node.transfer.as_mut().expect("just ensured");
        t.received.insert(meta.index);
        t.last_update = Instant::now();
        node.last_seen = Instant::now();
    }
}

/// One walk over the tree computing the set of node paths visible under a
/// (lowercased) substring filter. A node is visible if its full path matches
/// or any descendant's does; since child paths contain the parent path as a
/// prefix, a matching branch automatically keeps its whole subtree visible.
pub fn compute_visible_paths(
    root: &ZenohNode,
    filter_lower: &str,
) -> std::collections::HashSet<String> {
    fn walk(
        node: &ZenohNode,
        path: &str,
        filter: &str,
        out: &mut std::collections::HashSet<String>,
    ) -> bool {
        let mut visible = path.to_lowercase().contains(filter);
        for (key, child) in &node.children {
            let child_path = format!("{}/{}", path, key);
            if walk(child, &child_path, filter, out) {
                visible = true;
            }
        }
        if visible {
            out.insert(path.to_string());
        }
        visible
    }

    let mut out = std::collections::HashSet::new();
    for (key, child) in &root.children {
        walk(child, key, filter_lower, &mut out);
    }
    out
}

/// (leaf topics whose path matches, all leaf topics) for "n of m topics".
pub fn count_filter_matches(root: &ZenohNode, filter_lower: &str) -> (usize, usize) {
    fn walk(node: &ZenohNode, path: &str, filter: &str, out: &mut (usize, usize)) {
        if node.children.is_empty() {
            out.1 += 1;
            if path.to_lowercase().contains(filter) {
                out.0 += 1;
            }
        }
        for (key, child) in &node.children {
            walk(child, &format!("{path}/{key}"), filter, out);
        }
    }
    let mut out = (0, 0);
    for (key, child) in &root.children {
        walk(child, key, filter_lower, &mut out);
    }
    out
}

/// The byte range of `key` that matches `filter_lower`, ignoring case, on
/// char boundaries.
pub fn match_range(key: &str, filter_lower: &str) -> Option<std::ops::Range<usize>> {
    if filter_lower.is_empty() {
        return None;
    }
    for (start, _) in key.char_indices() {
        let mut lowered = String::new();
        for (i, c) in key[start..].char_indices() {
            lowered.extend(c.to_lowercase());
            if lowered == filter_lower {
                return Some(start..start + i + c.len_utf8());
            }
            if !filter_lower.starts_with(lowered.as_str()) {
                break;
            }
        }
    }
    None
}

/// Minimum interval between filter recomputations while data streams in.
pub const FILTER_RECOMPUTE_INTERVAL: Duration = Duration::from_millis(250);

/// A cached visible-path set is stale when the filter text changed, or the
/// tree changed and the throttle interval has elapsed.
pub fn filter_cache_is_stale(
    cached: Option<(&str, u64, Instant)>,
    filter: &str,
    version: u64,
    now: Instant,
) -> bool {
    match cached {
        None => true,
        Some((f, _, _)) if f != filter => true,
        Some((_, v, at)) => v != version && now.duration_since(at) >= FILTER_RECOMPUTE_INTERVAL,
    }
}

/// How long until a cache kept only by the throttle may be recomputed.
pub fn filter_repaint_after(
    cached: Option<(&str, u64, Instant)>,
    filter: &str,
    version: u64,
    now: Instant,
) -> Option<Duration> {
    match cached {
        Some((f, v, at)) if f == filter && v != version => {
            let left = FILTER_RECOMPUTE_INTERVAL.saturating_sub(now.duration_since(at));
            (!left.is_zero()).then_some(left)
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn insert_path_counts_leaves() {
        let mut root = ZenohNode::new("root".into());
        root.insert_path("a/b");
        root.insert_path("a/c");
        root.insert_path("d");
        assert_eq!(root.cumulative_leaves, 3);
        assert_eq!(root.children["a"].cumulative_leaves, 2);
        // repeat message to existing leaf: no change
        root.insert_path("a/b");
        assert_eq!(root.cumulative_leaves, 3);
    }

    #[test]
    fn insert_path_leaf_to_branch_conversion() {
        let mut root = ZenohNode::new("root".into());
        root.insert_path("a");
        root.insert_path("x");
        assert_eq!(root.cumulative_leaves, 2);
        // "a" stops being a leaf; "a/b" becomes the leaf — net zero above "a"
        root.insert_path("a/b");
        assert_eq!(root.cumulative_leaves, 2);
        assert_eq!(root.children["a"].cumulative_leaves, 1);
    }

    #[test]
    fn insert_path_returns_leaf_node() {
        let mut root = ZenohNode::new("root".into());
        let leaf = root.insert_path("x/y/z");
        assert_eq!(leaf.key, "z");
        // empty segments are skipped
        let leaf2 = root.insert_path("x//y/z");
        assert_eq!(leaf2.key, "z");
        assert_eq!(root.cumulative_leaves, 1);
    }

    #[test]
    fn visible_paths_includes_ancestors_case_insensitive() {
        let mut root = ZenohNode::new("root".into());
        root.insert_path("demo/Sensors/Temp1");
        root.insert_path("demo/other");
        root.insert_path("unrelated/x");
        let v = compute_visible_paths(&root, "temp");
        assert!(v.contains("demo"));
        assert!(v.contains("demo/Sensors"));
        assert!(v.contains("demo/Sensors/Temp1"));
        assert!(!v.contains("demo/other"));
        assert!(!v.contains("unrelated"));
    }

    #[test]
    fn visible_paths_branch_match_keeps_descendants() {
        let mut root = ZenohNode::new("root".into());
        root.insert_path("demo/a/b");
        // "demo" matches; descendants' full paths contain "demo" so they're visible too
        let v = compute_visible_paths(&root, "demo");
        assert!(v.contains("demo") && v.contains("demo/a") && v.contains("demo/a/b"));
    }

    #[test]
    fn transfer_state_resets_on_new_generation() {
        let mut root = ZenohNode::new("root".into());
        let meta_a = crate::transfer::ChunkMeta {
            total_size: 100,
            total_chunks: 2,
            index: 0,
        };
        let meta_b = crate::transfer::ChunkMeta {
            total_size: 200,
            total_chunks: 3,
            index: 1,
        };
        root.record_chunk("t", meta_a);
        root.record_chunk("t", crate::transfer::ChunkMeta { index: 1, ..meta_a });
        assert!(root.children["t"].transfer.as_ref().unwrap().is_complete());
        root.record_chunk("t", meta_b); // new generation resets
        let t = root.children["t"].transfer.as_ref().unwrap();
        assert_eq!((t.total_chunks, t.received.len()), (3, 1));
        // no __chunk children materialized
        assert!(root.children["t"].children.is_empty());
    }

    #[test]
    fn filter_cache_staleness_rules() {
        let t0 = Instant::now();
        assert!(filter_cache_is_stale(None, "a", 1, t0));
        assert!(filter_cache_is_stale(Some(("a", 1, t0)), "b", 1, t0));
        assert!(!filter_cache_is_stale(
            Some(("a", 1, t0)),
            "a",
            2,
            t0 + Duration::from_millis(100)
        ));
        assert!(filter_cache_is_stale(
            Some(("a", 1, t0)),
            "a",
            2,
            t0 + Duration::from_millis(300)
        ));
        assert!(!filter_cache_is_stale(
            Some(("a", 2, t0)),
            "a",
            2,
            t0 + Duration::from_secs(9)
        ));
    }

    #[test]
    fn branch_summary_counts_subtree() {
        let mut root = ZenohNode::new("root".into());
        for (p, n) in [("demo/bin/blob", 3), ("demo/bin/x", 2)] {
            let leaf = root.insert_path(p);
            for _ in 0..n {
                leaf.update_data(
                    "v".into(),
                    "text/plain".into(),
                    false,
                    SampleKindView::Put,
                    None,
                );
            }
        }
        let bin = &root.children["demo"].children["bin"];
        let s = bin.subtree_summary();
        assert_eq!((s.topics, s.messages), (2, 5));
        assert!(s.last_seen.is_some());
    }

    #[test]
    fn local_marker_follows_latest_value() {
        let mut n = ZenohNode::new("k".into());
        n.update_data(
            "mine".into(),
            "text/plain".into(),
            true,
            SampleKindView::Put,
            None,
        );
        assert!(n.is_local);
        n.update_data(
            "theirs".into(),
            "text/plain".into(),
            false,
            SampleKindView::Put,
            None,
        );
        assert!(!n.is_local, "a remote value replaced ours");
    }

    #[test]
    fn filter_throttle_schedules_repaint() {
        let t0 = Instant::now();
        let ms = Duration::from_millis;
        // K4: kept only by the throttle, so repaint when the interval ends.
        assert_eq!(
            filter_repaint_after(Some(("a", 1, t0)), "a", 2, t0 + ms(100)),
            Some(ms(150))
        );
        // Up to date, due for a recompute, or a new filter: nothing to schedule.
        assert_eq!(
            filter_repaint_after(Some(("a", 2, t0)), "a", 2, t0 + ms(100)),
            None
        );
        assert_eq!(
            filter_repaint_after(Some(("a", 1, t0)), "a", 2, t0 + ms(300)),
            None
        );
        assert_eq!(
            filter_repaint_after(Some(("a", 1, t0)), "b", 2, t0 + ms(100)),
            None
        );
    }

    #[test]
    fn filter_counts_leaf_topics() {
        let mut root = ZenohNode::new("root".into());
        for p in ["a/x", "a/y", "b/x"] {
            root.insert_path(p);
        }
        assert_eq!(count_filter_matches(&root, "x"), (2, 3));
        assert_eq!(count_filter_matches(&root, "a"), (2, 3));
        assert_eq!(count_filter_matches(&root, ""), (3, 3));
    }

    #[test]
    fn match_range_is_case_insensitive_and_char_safe() {
        assert_eq!(match_range("Temp1", "te"), Some(0..2));
        assert_eq!(match_range("Temp1", "p1"), Some(3..5));
        assert_eq!(match_range("Straße", "aße"), Some(3..7));
        assert_eq!(match_range("Temp1", "zz"), None);
        assert_eq!(match_range("Temp1", ""), None);
    }
}
