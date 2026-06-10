# Pipeline Integrity + i3x Tree Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four data-corruption paths in the file-transfer pipeline, port the i3X-Explorer PR #36 tree/search principles (deep-match filter, auto-expand, counts, leader lines, icons, chunk-aware transfer nodes), make file export prominent with original filenames preserved, fix cross-platform CI, then port everything to the sibling `send_it` repo.

**Architecture:** All pipeline logic moves into pure, unit-testable functions (`transfer.rs`, `types.rs`) operating on plain maps/nodes; `events.rs` becomes thin orchestration; UI changes live in `topic_tree.rs`/`app.rs`. Filenames travel as Zenoh attachments. Spec: `docs/superpowers/specs/2026-06-09-pipeline-integrity-and-tree-search-design.md`.

**Tech Stack:** Rust, egui/eframe 0.29, zenoh 1.0 (attachments), seahash, rfd, GitHub Actions.

**Branch:** `feat/pipeline-integrity-tree-search` (already created; spec committed).

**File map:**

| File | Changes |
| ---- | ------- |
| `.gitignore`, `.github/workflows/ci.yml`, `.github/workflows/release.yml` | Hygiene + macos-13 fix (Task 1) |
| `src/transfer.rs` | `parse_chunk_key`, `insert_payload` eviction, `chunk_progress`, validated export, suggested filename, dialog filters (Tasks 2,4,5,7,19) |
| `src/types.rs` | `PayloadEntry`/`PayloadStoreMap`, `Deduper`, `ZenohNode.insert_path`+`cumulative_leaves`+`transfer`, `TransferState`, `compute_visible_paths`, `ZenohMessage.filename`, `ZenohCommand::Publish.filename` (Tasks 3,8,10,11,12,17,18) |
| `src/events.rs` | Thin orchestration: dedup ordering, pause routing, chunk routing, tree version (Tasks 8,9,11) |
| `src/app.rs` | Field changes, `ui_alert` banner (Tasks 3,6,8,12) |
| `src/ui/topic_tree.rs` | Filter render, auto-expand, leader lines, icons, transfer node, Save UX (Tasks 13–16,19) |
| `src/ui/messages.rs` | `deduper.enabled` reference (Task 8) |
| `src/ui/publish.rs` | Pass filename in Publish command (Task 17) |
| `src/zenoh_worker.rs` | Attachment on puts, read attachment on receive (Tasks 17,18) |
| `CLAUDE.md` | Sync docs (Task 20) |

**Conventions for every task:** run `cargo test` and `cargo clippy -- -D warnings` before each commit; commit messages use the `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.

---

### Task 1: Repo hygiene + cross-platform CI fix

**Files:**
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Fix .gitignore**

Remove these lines from `.gitignore` (they block tests and reproducible builds):

```
Cargo.lock
tests/
test_*.rs
```

(Keep `test_*.sh` and `test_*.md` if present — only the three lines above are removed.)

- [ ] **Step 2: Fix retired macos-13 runner in both workflows**

In BOTH `.github/workflows/ci.yml` and `.github/workflows/release.yml`, change the x86_64 macOS matrix leg:

```yaml
          - target: x86_64-apple-darwin
            os: macos-13
```

to:

```yaml
          - target: x86_64-apple-darwin
            os: macos-14
```

(`dtolnay/rust-toolchain` already installs `targets: ${{ matrix.target }}` and the build already uses `--target`, so cross-compiling x86_64 from an arm64 runner needs no other change.)

- [ ] **Step 3: Add fmt check to CI**

In `ci.yml`, in the `check` job after the "Rust cache" step, add:

```yaml
      - name: Format
        run: cargo fmt --all -- --check
```

Then run `cargo fmt --all` locally so the tree passes it.

- [ ] **Step 4: Remove leftover debug step from release.yml**

Delete this step from `release.yml`:

```yaml
      - name: Debug build output
        if: matrix.os != 'windows-latest'
        run: find target -name "${{ env.BINARY_NAME }}*" -type f 2>/dev/null || echo "No binary found"
```

- [ ] **Step 5: Verify and commit**

Run: `cargo build && cargo fmt --all -- --check && git add -A && git status`
Expected: build OK; `Cargo.lock` now shows as a new tracked file.

```bash
git commit -m "ci: fix retired macos-13 runner, track Cargo.lock, un-ignore tests, fmt check"
```

---

### Task 2: Chunk key parsing (`parse_chunk_key`)

**Files:**
- Modify: `src/transfer.rs` (also add `#[cfg(test)] mod tests` at the bottom)

- [ ] **Step 1: Write failing tests** (bottom of `src/transfer.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_chunk_key_valid() {
        let (topic, m) = parse_chunk_key("demo/file/__chunk/8589934592/128/7").unwrap();
        assert_eq!(topic, "demo/file");
        assert_eq!(
            m,
            ChunkMeta { total_size: 8589934592, total_chunks: 128, index: 7 }
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
        assert!(!ChunkMeta { total_size: 100, total_chunks: 2, index: 2 }.is_sane());
        // zero chunks
        assert!(!ChunkMeta { total_size: 100, total_chunks: 0, index: 0 }.is_sane());
        // total_size exceeds what total_chunks could carry (allocation bomb)
        assert!(!ChunkMeta { total_size: usize::MAX, total_chunks: 2, index: 0 }.is_sane());
        // normal
        assert!(ChunkMeta { total_size: 100 * 1024 * 1024, total_chunks: 2, index: 1 }.is_sane());
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cargo test --lib transfer`
Expected: FAIL — `parse_chunk_key` / `ChunkMeta` not found.

- [ ] **Step 3: Implement** (in `src/transfer.rs`, below the constants/type alias section)

```rust
/// Publish-side chunk size (must match zenoh_worker.rs CHUNK_SIZE).
pub const CHUNK_SIZE: usize = 64 * 1024 * 1024;

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
    Some((topic, ChunkMeta { total_size, total_chunks, index }))
}
```

- [ ] **Step 4: Run tests** — `cargo test --lib transfer` → PASS.

- [ ] **Step 5: Commit** — `git add src/transfer.rs && git commit -m "feat(transfer): parse and sanity-check chunk keys"`

---

### Task 3: `PayloadEntry` struct replaces the store tuple

**Files:**
- Modify: `src/types.rs`, `src/app.rs:72-73,165`, `src/transfer.rs`, `src/events.rs:330-347`

- [ ] **Step 1: Add types** (in `src/types.rs`, after `ZenohNode`)

```rust
/// A stored payload: full raw bytes plus receive metadata.
#[derive(Debug, Clone)]
pub struct PayloadEntry {
    pub bytes: Vec<u8>,
    pub received_at: DateTime<Utc>,
    /// Original filename transmitted by the sender (Zenoh attachment), if any.
    pub filename: Option<String>,
}

/// The export store map. Keyed by full topic (or chunk) key.
pub type PayloadStoreMap = std::collections::HashMap<String, PayloadEntry>;
```

- [ ] **Step 2: Mechanical refactor of all users**

- `src/app.rs:72-73`: field becomes `pub(crate) payload_store: Arc<RwLock<PayloadStoreMap>>,` (drop the `#[allow(clippy::type_complexity)]`).
- `src/app.rs:165`: stays `payload_store: Arc::new(RwLock::new(HashMap::new())),`.
- `src/events.rs:340`: `store.insert(message.key.clone(), (raw_bytes, message.timestamp));` becomes `store.insert(message.key.clone(), PayloadEntry { bytes: raw_bytes, received_at: message.timestamp, filename: None });` (filename wired in Task 18).
- `src/transfer.rs`: change `get_chunk_info` and `get_payload_for_export` signatures from `&Arc<RwLock<HashMap<String, (Vec<u8>, DateTime<Utc>)>>>` to `&Arc<RwLock<PayloadStoreMap>>`, destructure `(data, _ts)` → `entry` and use `entry.bytes` (`store.get(topic)` direct-hit arm: `return Some(entry.bytes.clone());`). Add `use crate::types::PayloadEntry;` imports as needed. (These two functions are fully rewritten in Tasks 5 and 7 — keep this step minimal-but-compiling.)

- [ ] **Step 3: Verify** — `cargo build && cargo test` → compiles, Task 2 tests pass.

- [ ] **Step 4: Commit** — `git commit -am "refactor: PayloadEntry struct for the export store"`

---

### Task 4: Eviction policy (`insert_payload`)

**Files:**
- Modify: `src/transfer.rs`, `src/events.rs:330-347`

- [ ] **Step 1: Failing tests** (in `transfer.rs` tests module)

```rust
    use chrono::TimeZone;

    fn entry(ts_secs: i64) -> PayloadEntry {
        PayloadEntry {
            bytes: vec![1, 2, 3],
            received_at: chrono::Utc.timestamp_opt(ts_secs, 0).unwrap(),
            filename: None,
        }
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
        insert_payload(&mut store, "t/__chunk/9999999999999999/1/0".into(), entry(1));
        assert!(store.is_empty());
    }
```

- [ ] **Step 2: Run** — `cargo test --lib transfer` → FAIL (`insert_payload`, `MAX_PLAIN_ENTRIES` missing).

- [ ] **Step 3: Implement** (in `src/transfer.rs`)

```rust
/// Cap on non-chunk entries in the export store. Chunk entries are exempt:
/// they are bounded per-topic by their own total_chunks and purged by generation.
pub const MAX_PLAIN_ENTRIES: usize = 500;

/// Insert a payload into the export store, enforcing the eviction policy:
/// - chunk keys: purge any stale-generation chunks for the same topic, never
///   evict other entries, drop entries with insane metadata
/// - plain keys: evict the oldest plain entry once the cap is reached
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
                Some((_, m)) => (m.total_size, m.total_chunks) != (meta.total_size, meta.total_chunks),
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
```

- [ ] **Step 4: Wire into events.rs** — replace the body of the store block in `add_message_with_limits` (`src/events.rs:331-347`):

```rust
        // Store full payload bytes for export
        if payload_len <= MAX_EXPORT_PAYLOAD {
            if let Ok(mut store) = self.payload_store.write() {
                crate::transfer::insert_payload(
                    &mut store,
                    message.key.clone(),
                    PayloadEntry {
                        bytes: raw_bytes,
                        received_at: message.timestamp,
                        filename: None,
                    },
                );
            } else {
                error!("Failed to acquire payload_store lock for key: {}", message.key);
            }
        }
```

- [ ] **Step 5: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 6: Commit** — `git commit -am "fix(transfer): timestamp eviction, chunk groups exempt and purged by generation"`

---

### Task 5: `chunk_progress` (newest-group, index-set accurate)

**Files:**
- Modify: `src/transfer.rs` (replace `get_chunk_info` + `ChunkInfo`), `src/ui/topic_tree.rs:238-241`

- [ ] **Step 1: Failing tests**

```rust
    #[test]
    fn chunk_progress_counts_distinct_indices_of_newest_group() {
        let mut store = PayloadStoreMap::new();
        // stale group (older timestamps)
        store.insert("t/__chunk/200000000/3/0".into(), entry(1));
        // newest group, 2 of 5 received
        store.insert("t/__chunk/300000000/5/0".into(), entry(10));
        store.insert("t/__chunk/300000000/5/4".into(), entry(11));
        let p = chunk_progress(&store, "t").unwrap();
        assert_eq!((p.received, p.total_chunks, p.total_size), (2, 5, 300000000));
    }

    #[test]
    fn chunk_progress_none_for_plain_or_other_topics() {
        let mut store = PayloadStoreMap::new();
        store.insert("t".into(), entry(1));
        store.insert("other/__chunk/100/1/0".into(), entry(1));
        assert!(chunk_progress(&store, "t").is_none());
    }
```

- [ ] **Step 2: Run** — FAIL (`chunk_progress` missing).

- [ ] **Step 3: Implement**, deleting `get_chunk_info` and the `ChunkInfo` tuple alias:

```rust
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
    let mut groups: HashMap<(usize, usize), (std::collections::HashSet<usize>, DateTime<Utc>)> =
        HashMap::new();
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
    Some(ChunkProgress { received: indices.len(), total_chunks, total_size })
}
```

- [ ] **Step 4: Update the call site** (`src/ui/topic_tree.rs:238` and the `if let Some((received, total, total_size)) = chunk_info` destructure at `:241`):

```rust
            // Check for chunked payload and show info
            let chunk_info = self
                .payload_store
                .read()
                .ok()
                .and_then(|store| transfer::chunk_progress(&store, topic));

            if let Some(p) = chunk_info {
                let (received, total, total_size) = (p.received, p.total_chunks, p.total_size);
```

(The body below keeps using `received`/`total`/`total_size` unchanged.)

- [ ] **Step 5: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 6: Commit** — `git commit -am "fix(transfer): chunk progress groups by generation and counts distinct indices"`

---

### Task 6: Global alert banner (`ui_alert`)

**Files:**
- Modify: `src/app.rs` (field + render)

- [ ] **Step 1: Add field** — in the `ZenohExplorer` struct after `query_alert`: `pub(crate) ui_alert: Option<String>,` and in `new()` after `query_alert: None,`: `ui_alert: None,`.

- [ ] **Step 2: Render banner** — in `app.rs` `update()`, immediately BEFORE `egui::TopBottomPanel::top("toolbar").show_inside(ui, |ui| {` (app.rs:629), insert:

```rust
                // Global alert banner (export errors, warnings) — visible on every tab
                if let Some(alert_text) = self.ui_alert.clone() {
                    egui::TopBottomPanel::top("alert_banner").show_inside(ui, |ui| {
                        ui.horizontal(|ui| {
                            ui.label(
                                RichText::new(format!("⚠ {}", alert_text))
                                    .color(ExplorerColors::WARNING),
                            );
                            if ui.small_button("✖").clicked() {
                                self.ui_alert = None;
                            }
                        });
                    });
                }
```

- [ ] **Step 3: Verify** — `cargo build` then `cargo run`, briefly set an alert by temporary code if desired, or rely on Task 7's wiring. Build must pass clippy.

- [ ] **Step 4: Commit** — `git commit -am "feat(ui): global alert banner"`

---

### Task 7: Validated export (`get_payload_for_export` → `Result`)

**Files:**
- Modify: `src/transfer.rs`, `src/ui/topic_tree.rs:166-176`

- [ ] **Step 1: Failing tests**

```rust
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
        assert_eq!(get_payload_for_export(&store, "t").unwrap().bytes, vec![9, 9]);
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
```

- [ ] **Step 2: Run** — FAIL (signature mismatch).

- [ ] **Step 3: Replace `get_payload_for_export` entirely:**

```rust
/// A payload ready for export.
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
pub fn get_payload_for_export(store: &PayloadStoreMap, topic: &str) -> Result<ExportPayload, String> {
    if let Some(e) = store.get(topic) {
        return Ok(ExportPayload { bytes: e.bytes.clone(), filename: e.filename.clone() });
    }

    let progress = chunk_progress(store, topic)
        .ok_or_else(|| format!("No payload stored for '{}'", topic))?;

    // Collect the newest group's chunks by index (BTreeMap = sorted, dedup by key).
    let mut by_index: std::collections::BTreeMap<usize, &PayloadEntry> = Default::default();
    for (key, e) in store.iter() {
        if let Some((t, m)) = parse_chunk_key(key) {
            if t == topic
                && m.total_size == progress.total_size
                && m.total_chunks == progress.total_chunks
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

    info!("Reassembled {} chunks into {} bytes", progress.total_chunks, bytes.len());
    Ok(ExportPayload { bytes, filename })
}
```

Note: `by_index` is keyed 0..N and `chunk_progress` guarantees `index < total_chunks` via `is_sane`, so `len == total_chunks` ⇒ the index set is exactly `0..total_chunks`.

- [ ] **Step 4: Update the Export button handler** (`src/ui/topic_tree.rs:166-176`) — errors now surface:

```rust
                if ui
                    .button("Export Payload")
                    .on_hover_text("Save full payload to file (original size)")
                    .clicked()
                {
                    let result = self
                        .payload_store
                        .read()
                        .map_err(|_| "Payload store lock poisoned".to_string())
                        .and_then(|store| transfer::get_payload_for_export(&store, topic));
                    match result {
                        Ok(payload) => transfer::export_payload_to_file(topic, &payload.bytes),
                        Err(e) => self.ui_alert = Some(format!("Export failed: {}", e)),
                    }
                }
```

(`export_payload_to_file` keeps its current `(topic, &[u8])` signature until Task 19.)

- [ ] **Step 5: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 6: Commit** — `git commit -am "fix(transfer): validate chunk reassembly, surface export errors"`

---

### Task 8: `Deduper` — full-content seahash, record-after-accept

**Files:**
- Modify: `src/types.rs` (new struct + tests), `src/app.rs` (fields), `src/events.rs` (remove `compute_message_hash`/`is_duplicate`, rework `process_single_message`), `src/ui/messages.rs:60`

- [ ] **Step 1: Failing tests** (new `#[cfg(test)] mod tests` at the bottom of `src/types.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedup_same_content_within_window() {
        let mut d = Deduper::new(Duration::from_secs(60));
        let h = Deduper::hash_message("k", b"payload");
        assert!(!d.seen_recently(h));
        d.record(h);
        assert!(d.seen_recently(h));
    }

    #[test]
    fn dedup_differs_when_middle_bytes_differ() {
        // Two 16KB payloads: same first/last 4KB, different middle.
        let mut a = vec![0u8; 16 * 1024];
        let mut b = vec![0u8; 16 * 1024];
        a[8000] = 1;
        b[8000] = 2;
        assert_ne!(Deduper::hash_message("k", &a), Deduper::hash_message("k", &b));
    }

    #[test]
    fn dedup_unrecorded_hash_not_seen() {
        // A hash that was checked but never recorded (e.g. rate-limited drop)
        // must not poison the retransmit.
        let mut d = Deduper::new(Duration::from_secs(60));
        let h = Deduper::hash_message("k", b"x");
        assert!(!d.seen_recently(h));
        assert!(!d.seen_recently(h)); // still unseen — check alone records nothing
    }

    #[test]
    fn dedup_expires_after_ttl() {
        let mut d = Deduper::new(Duration::from_millis(1));
        let h = Deduper::hash_message("k", b"x");
        d.record(h);
        std::thread::sleep(Duration::from_millis(5));
        assert!(!d.seen_recently(h));
    }
}
```

- [ ] **Step 2: Run** — `cargo test --lib types` → FAIL.

- [ ] **Step 3: Implement `Deduper`** (in `src/types.rs`, near `RateLimiter`)

```rust
/// Content-based message deduplication over a sliding time window.
///
/// Hashes the FULL payload (seahash) so payloads differing anywhere are never
/// conflated. Checking and recording are separate so a message dropped after
/// the check (e.g. by the rate limiter) doesn't poison its own retransmit.
pub struct Deduper {
    hashes: std::collections::HashMap<u64, Instant>,
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

    /// True if this hash was recorded within the TTL. Does NOT record.
    pub fn seen_recently(&mut self, hash: u64) -> bool {
        if self.last_sweep.elapsed() > self.ttl {
            let ttl = self.ttl;
            self.hashes.retain(|_, t| t.elapsed() < ttl);
            self.last_sweep = Instant::now();
        }
        self.hashes
            .get(&hash)
            .is_some_and(|t| t.elapsed() < self.ttl)
    }

    /// Record a hash as seen now. Call only after the message is accepted.
    pub fn record(&mut self, hash: u64) {
        self.hashes.insert(hash, Instant::now());
    }
}
```

- [ ] **Step 4: Swap app fields** (`src/app.rs`): remove `message_hashes`, `dedup_ttl`, `dedup_enabled` (struct lines 61-63 and init lines 155-157); add field `pub(crate) deduper: Deduper,` and init `deduper: Deduper::new(Duration::from_secs(60)),`. Keep `messages_deduped`.

- [ ] **Step 5: Rework events.rs** — delete `compute_message_hash` (events.rs:14-35) and `is_duplicate` (events.rs:96-122); replace `process_single_message` (events.rs:220-265) with:

```rust
    /// Process a single message through dedup, rate limiting, and storage.
    fn process_single_message(&mut self, message: ZenohMessage) {
        // For query replies, handle "local wins" logic
        if message.message_type == MessageType::QueryReply {
            let existing_idx = self
                .messages
                .iter()
                .position(|m| m.key == message.key && m.message_type == MessageType::QueryReply);

            if let Some(idx) = existing_idx {
                if message.is_local && !self.messages[idx].is_local {
                    self.messages[idx] = message;
                    return;
                } else if !message.is_local && self.messages[idx].is_local {
                    return;
                }
            }
        }

        // Dedup check on FULL content (query replies exempt — we want every reply)
        let dedup_hash = (self.deduper.enabled
            && message.message_type != MessageType::QueryReply)
            .then(|| {
                let bytes = message
                    .payload_bytes
                    .as_deref()
                    .unwrap_or_else(|| message.payload.as_bytes());
                Deduper::hash_message(&message.key, bytes)
            });
        if let Some(h) = dedup_hash {
            if self.deduper.seen_recently(h) {
                self.messages_deduped += 1;
                return;
            }
        }

        // Rate limiting BEFORE the hash is recorded: a dropped message's
        // retransmit must not be classified as a duplicate.
        if !self.rate_limiter.check_and_update() {
            self.rate_limit_drops += 1;
            return;
        }
        if let Some(h) = dedup_hash {
            self.deduper.record(h);
        }

        let is_query_reply = message.message_type == MessageType::QueryReply;

        // Pause skips only DISPLAY (the messages list); storage and tree
        // updates continue so no data is lost while paused. (Task 9 threads
        // `display` through; chunk routing changes in Task 11.)
        let display = !self.paused_keys.contains(&message.key);

        self.add_message_to_browse_tree(&message);
        self.add_message_with_limits(message, display);

        if is_query_reply {
            self.query_alert = None;
        }
    }
```

(Note `self.messages[idx] = message;` — the needless clone is gone. The old standalone pause-skip block at events.rs:247-249 is deleted by this replacement; `add_message_with_limits(message, display)` compiles after Task 9 Step 1 — do Task 9 Step 1 together with this step in one commit.)

- [ ] **Step 6: Update messages.rs** (`src/ui/messages.rs:60`): `ui.checkbox(&mut self.dedup_enabled, "Dedup");` → `ui.checkbox(&mut self.deduper.enabled, "Dedup");`

- [ ] **Step 7:** Apply Task 9 Step 1 (below), then run `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 8: Commit** — `git commit -am "fix(events): full-content seahash dedup recorded after accept; pause keeps storing"`

---

### Task 9: Pause stores, only display skipped

**Files:**
- Modify: `src/events.rs` (`add_message_with_limits`)

- [ ] **Step 1: Add `display` parameter** — change the signature at events.rs:319 to `pub(crate) fn add_message_with_limits(&mut self, mut message: ZenohMessage, display: bool)` and wrap everything AFTER the payload-store block (i.e. from the "Truncate display payload" comment to the end of the function) in:

```rust
        if !display {
            return; // stored above; paused/chunk traffic doesn't hit the messages list
        }
```

- [ ] **Step 2: Update the pause tooltip** (it's now true): no change needed — `topic_tree.rs:192` already says "messages still received, just not displayed".

- [ ] **Step 3:** Covered by Task 8's combined commit (Tasks 8+9 land together since the signature change is interlocked).

---

### Task 10: `ZenohNode::insert_path` with cumulative leaf counts

**Files:**
- Modify: `src/types.rs` (ZenohNode + tests), `src/events.rs` (`add_message_to_browse_tree`)

- [ ] **Step 1: Failing tests** (types.rs tests module)

```rust
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
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** — add field to `ZenohNode`: `pub cumulative_leaves: usize,` (init `1` in `new()` — every node starts as its own leaf), plus the method:

```rust
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
        let bump = divergence.is_some_and(|_| !probe.children.is_empty());

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
```

(Note: after the probe loop breaks at segment `i`, `probe` IS the parent of the first missing segment, so `probe.children.is_empty()` is exactly the leaf-conversion check. The bump applies to existing path nodes at depth ≤ divergence; newly created nodes start at 1.)

- [ ] **Step 4: Use it in events.rs** — in `add_message_to_browse_tree` (events.rs:269-314), replace the manual navigation loop (lines 271-285) with:

```rust
        if let Ok(mut tree) = self.browse_tree.write() {
            let current_node = tree.insert_path(&message.key);
```

(`current_node` replaces the old variable; the preview/update_data code below is unchanged.)

- [ ] **Step 5: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 6: Commit** — `git commit -am "feat(tree): insert_path maintains cumulative leaf counts"`

---

### Task 11: `TransferState` — chunk traffic stops materializing tree nodes

**Files:**
- Modify: `src/types.rs` (TransferState + ZenohNode field), `src/events.rs`

- [ ] **Step 1: Add the type** (types.rs, near ZenohNode) and field:

```rust
/// In-flight or completed chunked file transfer, tracked on the parent topic node.
#[derive(Debug, Clone)]
pub struct TransferState {
    pub total_size: usize,
    pub total_chunks: usize,
    pub received: std::collections::HashSet<usize>,
    pub last_update: Instant,
}

impl TransferState {
    pub fn is_complete(&self) -> bool {
        self.received.len() == self.total_chunks
    }
}
```

`ZenohNode` gains `pub transfer: Option<TransferState>,` (init `None`).

- [ ] **Step 2: Failing test** (types.rs tests)

```rust
    #[test]
    fn transfer_state_resets_on_new_generation() {
        let mut root = ZenohNode::new("root".into());
        let meta_a = crate::transfer::ChunkMeta { total_size: 100, total_chunks: 2, index: 0 };
        let meta_b = crate::transfer::ChunkMeta { total_size: 200, total_chunks: 3, index: 1 };
        root.record_chunk("t", meta_a);
        root.record_chunk("t", crate::transfer::ChunkMeta { index: 1, ..meta_a });
        assert!(root.children["t"].transfer.as_ref().unwrap().is_complete());
        root.record_chunk("t", meta_b); // new generation resets
        let t = root.children["t"].transfer.as_ref().unwrap();
        assert_eq!((t.total_chunks, t.received.len()), (3, 1));
        // no __chunk children materialized
        assert!(root.children["t"].children.is_empty());
    }
```

- [ ] **Step 3: Implement `record_chunk`** on ZenohNode:

```rust
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
```

- [ ] **Step 4: Route chunks in events.rs** — at the top of `add_message_to_browse_tree`:

```rust
        // Chunk traffic: update the parent topic's transfer state instead of
        // materializing a 4-level __chunk subtree per chunk.
        if let Some((topic, meta)) = crate::transfer::parse_chunk_key(&message.key) {
            if meta.is_sane() {
                if let Ok(mut tree) = self.browse_tree.write() {
                    let topic_owned = topic.to_string();
                    tree.record_chunk(&topic_owned, meta);
                }
                self.tree_version = self.tree_version.wrapping_add(1);
            }
            return;
        }
```

And in `process_single_message` (Task 8's version), chunk messages skip the list:

```rust
        let is_chunk = crate::transfer::parse_chunk_key(&message.key).is_some();
        let display = !is_chunk && !self.paused_keys.contains(&message.key);
```

(Replaces the `let display = !self.paused_keys...` line. Payload storage continues for chunks via `add_message_with_limits` — the store path runs regardless of `display`.)

- [ ] **Step 5: Add `tree_version`** — `pub(crate) tree_version: u64,` in app.rs (init `0`), and bump it at the END of `add_message_to_browse_tree`'s non-chunk path too (after `current_node.update_data(...)`): `self.tree_version = self.tree_version.wrapping_add(1);` — note: `add_message_to_browse_tree` takes `&self`; change it to `&mut self` (its only caller is `process_single_message`, which is `&mut self`).

- [ ] **Step 6: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 7: Commit** — `git commit -am "feat(tree): chunk traffic becomes per-topic transfer state, not tree nodes"`

---

### Task 12: Visible-path set (deep-match filter, memoized)

**Files:**
- Modify: `src/types.rs` (function + tests), `src/app.rs` (cache field)

- [ ] **Step 1: Failing tests** (types.rs tests)

```rust
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
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** (types.rs, after ZenohNode impl):

```rust
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
```

- [ ] **Step 4: Cache field** in app.rs: `pub(crate) tree_filter_cache: Option<(String, u64, std::collections::HashSet<String>)>,` (init `None`) — holds (lowercased query, tree_version, visible set).

- [ ] **Step 5: Run** — `cargo test` → PASS. **Commit** — `git commit -am "feat(tree): precomputed visible-path set for deep-match filtering"`

---

### Task 13: Wire filter rendering + auto-expand on search

**Files:**
- Modify: `src/ui/topic_tree.rs` (`show_tree_panel`, `show_tree_node`; delete `has_matching_descendant`)

- [ ] **Step 1: Refresh the cache in `show_tree_panel`** — after the tree clone (topic_tree.rs:102-106), insert:

```rust
            let filter_lower = self.tree_filter.to_lowercase();
            if !filter_lower.is_empty() {
                let stale = self
                    .tree_filter_cache
                    .as_ref()
                    .map_or(true, |(q, v, _)| *q != filter_lower || *v != self.tree_version);
                if stale {
                    let visible = compute_visible_paths(&tree_clone, &filter_lower);
                    self.tree_filter_cache = Some((filter_lower.clone(), self.tree_version, visible));
                }
            } else {
                self.tree_filter_cache = None;
            }
```

(`use crate::types::compute_visible_paths;` — already covered by `use crate::types::*;`.)

- [ ] **Step 2: Replace the filter check in `show_tree_node`** (topic_tree.rs:446-454) with a set lookup:

```rust
        // Apply filter via the precomputed visible-path set (deep match:
        // ancestors of matches and subtrees of matching branches stay visible)
        if let Some((_, _, visible)) = &self.tree_filter_cache {
            if !visible.contains(&full_path) {
                return;
            }
        }
```

Delete `has_matching_descendant` entirely (topic_tree.rs:580-594).

- [ ] **Step 3: Auto-expand while filtering** — in the branch arm (topic_tree.rs:521-527), replace the id/state setup:

```rust
            // While filtering, branches render expanded under a separate ID
            // namespace so the user's normal expand/collapse state is bypassed,
            // not overwritten; clearing the filter restores it.
            let filtering = self.tree_filter_cache.is_some();
            let id = if filtering {
                egui::Id::new(("treenode_filtered", &full_path))
            } else {
                egui::Id::new(("treenode", &full_path))
            };
            let state = egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                filtering,
            );
```

- [ ] **Step 4: Manual verify** — `cargo run`, subscribe to a busy demo, type a filter in mixed case: matching subtrees auto-expand, non-matching siblings disappear, clearing the filter restores prior expansion. `cargo clippy -- -D warnings` passes.

- [ ] **Step 5: Commit** — `git commit -am "feat(tree): case-insensitive deep-match filter with auto-expand"`

---

### Task 14: Counts + leader lines

**Files:**
- Modify: `src/ui/topic_tree.rs`

- [ ] **Step 1: Add a leader-line + count row helper** (private fn in the `TopicTreeUI` impl or free fn in topic_tree.rs):

```rust
/// Draw a leader line (dashed when collapsed, solid when expanded) filling the
/// space between the label and a right-aligned tabular count.
fn leader_line_with_count(
    ui: &mut egui::Ui,
    expanded: bool,
    count: usize,
    text_color: egui::Color32,
) {
    if count == 0 {
        return;
    }
    let count_text = count.to_string();
    let font = egui::FontId::proportional(TEXT_SMALL_SIZE);
    let galley = ui
        .painter()
        .layout_no_wrap(count_text.clone(), font.clone(), text_color);
    let line_w = (ui.available_width() - galley.size().x - 16.0).max(0.0);
    let (rect, _) = ui.allocate_exact_size(
        egui::vec2(line_w, ui.spacing().interact_size.y),
        egui::Sense::hover(),
    );
    let y = rect.center().y;
    let (alpha, dashed) = if expanded { (100, false) } else { (64, true) };
    let stroke = egui::Stroke::new(
        1.0,
        egui::Color32::from_rgba_unmultiplied(
            text_color.r(),
            text_color.g(),
            text_color.b(),
            alpha,
        ),
    );
    let a = egui::pos2(rect.left() + 4.0, y);
    let b = egui::pos2(rect.right() - 4.0, y);
    if rect.width() > 12.0 {
        if dashed {
            for shape in egui::Shape::dashed_line(&[a, b], stroke, 3.0, 3.0) {
                ui.painter().add(shape);
            }
        } else {
            ui.painter().line_segment([a, b], stroke);
        }
    }
    ui.label(egui::RichText::new(count_text).size(TEXT_SMALL_SIZE).color(text_color));
}
```

- [ ] **Step 2: Use it on branch rows** — in the branch header (topic_tree.rs:563-568), replace the `(N)` child-count label with:

```rust
                    let expanded = state.is_open();
                    leader_line_with_count(
                        ui,
                        expanded,
                        node.cumulative_leaves,
                        self.text_tertiary_color(),
                    );
```

(Capture `let expanded = state.is_open();` BEFORE `state.show_header(...)` consumes `state` — bind it right after creating `state` in Task 13's code, then use it inside the closure.)

- [ ] **Step 3: Use it on leaf rows** — replace the `(message_count)` badge (topic_tree.rs:496-503):

```rust
                leader_line_with_count(ui, false, node.message_count, self.text_tertiary_color());
```

(Drop the hardcoded light-mode `ExplorerColors::PRIMARY` — the theme-aware tertiary color is correct in both modes. Keep the payload preview label after it.)

- [ ] **Step 4: Manual verify** — `cargo run`: counts right-aligned with dashed leader on collapsed branches, solid on expanded; numerals aligned; both themes legible. `cargo clippy -- -D warnings`.

- [ ] **Step 5: Commit** — `git commit -am "feat(tree): per-level counts with dashed/solid leader lines"`

---

### Task 15: Five-icon system

**Files:**
- Modify: `src/ui/topic_tree.rs`

- [ ] **Step 1: Failing tests** (new `#[cfg(test)] mod tests` at the bottom of topic_tree.rs)

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn leaf_icons_bucket_correctly() {
        assert_eq!(leaf_icon("@/session/x", None, None), "🛠");
        assert_eq!(leaf_icon("demo/t", Some("application/json"), None), "🏷");
        assert_eq!(leaf_icon("demo/t", Some("text/plain"), Some("hello")), "🏷");
        assert_eq!(leaf_icon("demo/t", Some("application/octet-stream"), None), "💾");
        assert_eq!(leaf_icon("demo/t", None, Some("[binary 1024 bytes] ff 00")), "💾");
        assert_eq!(leaf_icon("demo/t", None, None), "💾");
    }
}
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** (free fn in topic_tree.rs):

```rust
/// Icon bucket for leaf topics — zenoh/embedded/automation themed:
/// 🛠 system (@/ zenoh admin space), 🏷 text/JSON (live KV telemetry),
/// 💾 binary/unknown (firmware/blobs). Prefers the declared encoding, falls
/// back to the payload preview heuristic (binary previews start with "[binary").
pub(crate) fn leaf_icon(
    full_path: &str,
    encoding: Option<&str>,
    last_payload: Option<&str>,
) -> &'static str {
    if full_path.starts_with('@') {
        return "🛠";
    }
    if let Some(enc) = encoding {
        let e = enc.to_ascii_lowercase();
        if e.contains("json") || e.starts_with("text/") {
            return "🏷";
        }
        if e.contains("octet-stream") {
            return "💾";
        }
    }
    match last_payload {
        Some(p) if p.starts_with("[binary") => "💾",
        Some(_) => "🏷",
        None => "💾",
    }
}
```

- [ ] **Step 4: Wire into rendering:**
- Leaf rows (topic_tree.rs:489): `format!("📄 {}", node.key)` becomes (transfer
  nodes get the incoming-transfer icon):

```rust
                let icon = if node.transfer.is_some() {
                    "📥"
                } else {
                    leaf_icon(
                        &full_path,
                        node.last_encoding.as_deref(),
                        node.last_payload.as_deref(),
                    )
                };
                let response = ui.selectable_label(is_selected, format!("{} {}", icon, node.key));
```

- Branch rows (topic_tree.rs:556): `format!("📁 {}", node.key)` becomes:

```rust
                    let icon = if depth == 0 { "🌐" } else { "📡" };
                    let response =
                        ui.selectable_label(is_selected, format!("{} {}", icon, node.key));
```

- Glyph check (manual): run the app once; if any icon renders as a tofu box in
  egui's emoji font, substitute per-icon fallbacks: 🌐→🛰→🌍, 📡→🗼, 💾→🤖,
  🏷→📟, 🛠→🔧, 📥→⬇ (update the tests to match whatever ships).

- [ ] **Step 5: Run + verify** — `cargo test && cargo clippy -- -D warnings`; `cargo run` to eyeball icons.

- [ ] **Step 6: Commit** — `git commit -am "feat(tree): five-icon system (roots, branches, binary, text, system)"`

---

### Task 16: Transfer-node rendering (progress bar, ✓, Save)

**Files:**
- Modify: `src/ui/topic_tree.rs` (leaf arm of `show_tree_node`, plus `show_topic_details` chunk section)

- [ ] **Step 1: Render transfer state on the node row** — in the LEAF arm of `show_tree_node`, after the selectable label + leader line and BEFORE the payload preview, add:

```rust
                // Inline file-transfer progress (chunked transfers)
                if let Some(t) = &node.transfer {
                    let frac = t.received.len() as f32 / t.total_chunks.max(1) as f32;
                    ui.add(
                        egui::ProgressBar::new(frac)
                            .desired_width(120.0)
                            .text(format!("{}/{}", t.received.len(), t.total_chunks)),
                    );
                    if t.is_complete() {
                        ui.label(
                            RichText::new(format!("✓ {}", transfer::format_size(t.total_size)))
                                .size(TEXT_SMALL_SIZE)
                                .color(if self.dark_mode {
                                    ExplorerColors::DARK_SUCCESS
                                } else {
                                    ExplorerColors::SUCCESS
                                }),
                        );
                    } else {
                        ui.label(
                            RichText::new(format!(
                                "⬇ {} of {}",
                                transfer::format_size(
                                    t.received.len().saturating_mul(crate::transfer::CHUNK_SIZE)
                                        .min(t.total_size)
                                ),
                                transfer::format_size(t.total_size)
                            ))
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_secondary_color()),
                        );
                    }
                }
```

NOTE: a node with `transfer: Some(..)` and no children renders in the leaf arm — exactly what we want now that `__chunk` subtrees are gone. Skip the payload-preview label when `node.transfer.is_some()` (chunk bytes aren't previewable):

```rust
                if node.transfer.is_none() {
                    if let Some(ref payload) = node.last_payload { /* existing preview code */ }
                }
```

- [ ] **Step 2: Keep `show_topic_details` consistent** — its chunk section (topic_tree.rs:237-269) already shows progress from `chunk_progress` (Task 5); update the completion line text "click Export to reassemble" → "ready to save" (the inline Save button arrives in Task 19).

- [ ] **Step 3: Manual verify** — `cargo run` with two instances (`RUST_LOG=zenoh_explorer=debug cargo run` twice, peer mode): send a >4GB file or temporarily lower `MAX_SINGLE_PAYLOAD` in zenoh_worker.rs to test chunking with a small file (revert before commit). The tree shows ONE node with a progress bar instead of `__chunk/...` children; messages list stays clean.

- [ ] **Step 4: Commit** — `git commit -am "feat(tree): inline transfer progress node with completion state"`

---

### Task 17: Transmit filenames (command + worker attachments)

**Files:**
- Modify: `src/types.rs` (`ZenohCommand::Publish`), `src/ui/publish.rs:235-240`, `src/zenoh_worker.rs:419-551`

- [ ] **Step 1: Extend the command** (types.rs:186-191):

```rust
    Publish {
        key: String,
        payload: Vec<u8>, // Raw bytes
        encoding: String,
        from_import: bool, // If true, don't store payload after publish (imported files are ephemeral)
        /// Original filename of an imported file; transmitted as a Zenoh
        /// attachment so receivers can restore the name + extension on save.
        filename: Option<String>,
    },
```

- [ ] **Step 2: Send it from the UI** (publish.rs:235-240) — capture the filename BEFORE the clear block (publish.rs:249 sets it to None):

```rust
                    match sender.send(ZenohCommand::Publish {
                        key: self.publish_key.clone(),
                        payload: payload_bytes,
                        encoding: self.publish_encoding.clone(),
                        from_import, // Don't store imported files after publish
                        filename: self.publish_payload_filename.clone(),
                    }) {
```

- [ ] **Step 3: Attach in the worker** — `ZenohCommand::Publish { key, payload, encoding, from_import }` destructure (zenoh_worker.rs:419-424) gains `filename`. Then for EACH of the four put sites (chunked ~480, large ~503, from_import ~515, echo ~526), build the put with a conditional attachment. Pattern (chunked site shown; apply identically to the other three):

```rust
                                    let mut put = sess
                                        .put(&chunk_key, chunk)
                                        .encoding(&encoding as &str)
                                        .congestion_control(zenoh::qos::CongestionControl::Block);
                                    if let Some(ref name) = filename {
                                        put = put.attachment(name.as_bytes().to_vec());
                                    }
                                    match put.await
```

(zenoh 1.0 `PublicationBuilder::attachment` takes `impl Into<ZBytes>`; `Vec<u8>` converts. The chunked path attaches the name to EVERY chunk so any single chunk carries it.)

- [ ] **Step 4: Echo carries the filename too** — the LocalEcho message construction (zenoh_worker.rs:537-546) gains `.with_filename(filename.clone())` after Task 18 introduces it; for THIS task just keep the variable in scope (no other use) so the commit compiles — or land Tasks 17+18 as one commit (recommended; they're two halves of one wire).

- [ ] **Step 5:** `cargo build` → compiles. Combined commit happens in Task 18.

---

### Task 18: Receive filenames (message field + store)

**Files:**
- Modify: `src/types.rs` (ZenohMessage), `src/zenoh_worker.rs` (both subscriber handlers + echo), `src/events.rs` (store filename)

- [ ] **Step 1: Add the field + builder** (types.rs):

In `ZenohMessage`: `pub filename: Option<String>,`. In `new_with_bytes`, initialize `filename: None,`. Add:

```rust
    /// Attach a transmitted original filename (from a Zenoh attachment).
    pub fn with_filename(mut self, filename: Option<String>) -> Self {
        self.filename = filename;
        self
    }
```

Check for any struct-literal constructions of `ZenohMessage` besides `new_with_bytes` (`grep -n "ZenohMessage {" src/`) and add `filename: None,` to each.

- [ ] **Step 2: Read attachments in the worker** — in BOTH subscriber handlers (monitor: zenoh_worker.rs:201-233; publishing: zenoh_worker.rs:347-385), after `let raw_bytes = ...`:

```rust
                                                                        let filename = sample
                                                                            .attachment()
                                                                            .and_then(|a| a.try_to_string().ok())
                                                                            .map(|s| s.into_owned());
```

and chain onto the message construction: `ZenohMessage::new_with_bytes(...).with_filename(filename)`. Do the same for the LocalEcho construction (zenoh_worker.rs:537-546): `.with_filename(filename.clone())` (the `filename` from the Publish command).

If the query-reply and queryable handlers (zenoh_worker.rs:552-790) also construct `ZenohMessage`s from samples, leave them without filenames — file transfer flows through put/subscribe only.

- [ ] **Step 3: Store it** — in `add_message_with_limits` (Task 4's version), the `PayloadEntry` now carries it:

```rust
                    PayloadEntry {
                        bytes: raw_bytes,
                        received_at: message.timestamp,
                        filename: message.filename.clone(),
                    },
```

- [ ] **Step 4: Run** — `cargo test && cargo clippy -- -D warnings` → PASS.

- [ ] **Step 5: Commit (Tasks 17+18)** — `git commit -am "feat(transfer): transmit original filenames as Zenoh attachments end-to-end"`

---

### Task 19: Save UX — suggested filename, dynamic filters, prominent buttons

**Files:**
- Modify: `src/transfer.rs` (suggested name + dialog), `src/ui/topic_tree.rs` (Save buttons)

- [ ] **Step 1: Failing tests** (transfer.rs tests)

```rust
    #[test]
    fn suggested_name_prefers_transmitted() {
        assert_eq!(
            suggested_export_filename("demo/files", Some("report.pdf")),
            "report.pdf"
        );
    }

    #[test]
    fn suggested_name_uses_last_segment_extension() {
        assert_eq!(suggested_export_filename("files/report.pdf", None), "report.pdf");
        assert_eq!(suggested_export_filename("files/archive.tar.gz", None), "archive.tar.gz");
    }

    #[test]
    fn suggested_name_falls_back_to_bin() {
        assert_eq!(suggested_export_filename("demo/data", None), "demo_data.bin");
        assert_eq!(suggested_export_filename("", None), "payload.bin");
        // dot-segment that isn't a real extension (empty stem / weird ext)
        assert_eq!(suggested_export_filename("demo/.hidden", None), "demo_.hidden.bin");
        assert_eq!(suggested_export_filename("v1.2/x", None), "v1.2_x.bin");
    }
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** (transfer.rs):

```rust
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
```

Replace `export_payload_to_file` (transfer.rs:127-155):

```rust
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
```

- [ ] **Step 4: A reusable save action** on the app (topic_tree.rs, inside the `TopicTreeUI` impl):

```rust
    /// Run the full save flow for a topic: fetch/reassemble, native dialog,
    /// write — surfacing any failure in the global alert banner.
    fn save_topic_to_file(&mut self, topic: &str) {
        let result = self
            .payload_store
            .read()
            .map_err(|_| "Payload store lock poisoned".to_string())
            .and_then(|store| transfer::get_payload_for_export(&store, topic));
        match result {
            Ok(payload) => {
                let suggested =
                    transfer::suggested_export_filename(topic, payload.filename.as_deref());
                match transfer::export_payload_to_file(&suggested, &payload.bytes) {
                    Ok(Some(path)) => {
                        self.ui_alert = Some(format!("✓ Saved to {}", path.display()));
                    }
                    Ok(None) => {} // user cancelled
                    Err(e) => self.ui_alert = Some(format!("Save failed: {}", e)),
                }
            }
            Err(e) => self.ui_alert = Some(format!("Save failed: {}", e)),
        }
    }
```

(Add `fn save_topic_to_file(&mut self, topic: &str);` to the `TopicTreeUI` trait declaration.)

- [ ] **Step 5: Prominent Save button in topic details** — replace the Task 7 "Export Payload" button block with:

```rust
                // Save availability: direct payload or a complete chunk set
                let (saveable, size, reason) = {
                    let store = self.payload_store.read().ok();
                    let direct = store
                        .as_ref()
                        .and_then(|s| s.get(topic.as_str()))
                        .map(|e| e.bytes.len());
                    match direct {
                        Some(len) => (true, Some(len), String::new()),
                        None => match store
                            .as_ref()
                            .and_then(|s| transfer::chunk_progress(s, topic))
                        {
                            Some(p) if p.received == p.total_chunks => {
                                (true, Some(p.total_size), String::new())
                            }
                            Some(p) => (
                                false,
                                None,
                                format!(
                                    "Waiting for {} more chunks",
                                    p.total_chunks - p.received
                                ),
                            ),
                            None => (false, None, "No payload stored yet".to_string()),
                        },
                    }
                };
                let label = match size {
                    Some(s) => format!("💾 Save File ({})", transfer::format_size(s)),
                    None => "💾 Save File".to_string(),
                };
                let button = egui::Button::new(
                    RichText::new(&label).color(egui::Color32::WHITE),
                )
                .fill(if self.dark_mode {
                    ExplorerColors::DARK_PRIMARY
                } else {
                    ExplorerColors::PRIMARY
                });
                let response = ui.add_enabled(saveable, button);
                let response = if saveable {
                    response.on_hover_text("Save full payload to file (original size)")
                } else {
                    response.on_disabled_hover_text(reason)
                };
                if response.clicked() {
                    self.save_topic_to_file(&topic.clone());
                }
```

- [ ] **Step 6: Inline Save on the completion row** — in the chunk-info section of `show_topic_details`, the `if received == total` arm becomes:

```rust
                if received == total {
                    ui.horizontal(|ui| {
                        ui.label(
                            RichText::new("✓ All chunks received")
                                .color(ExplorerColors::SUCCESS),
                        );
                        if ui.button("💾 Save").clicked() {
                            self.save_topic_to_file(&topic.clone());
                        }
                    });
                }
```

- [ ] **Step 7: 💾 on tree rows** — in the LEAF arm of `show_tree_node`, after the transfer/preview block:

```rust
                // Quick save on rows with an exportable payload
                let exportable = node.transfer.as_ref().is_some_and(|t| t.is_complete())
                    || self
                        .payload_store
                        .read()
                        .is_ok_and(|s| s.contains_key(&full_path));
                if exportable && ui.small_button("💾").on_hover_text("Save file").clicked() {
                    self.save_topic_to_file(&full_path);
                }
```

- [ ] **Step 8: Run + verify** — `cargo test && cargo clippy -- -D warnings`; manual: import `report.pdf`, publish to `demo/files`, on the receiving instance the save dialog suggests `report.pdf` with a "PDF files" filter; publish text to `files/notes.txt` from another tool → suggests `notes.txt`; un-named binary topic → `topic_path.bin`.

- [ ] **Step 9: Commit** — `git commit -am "feat(export): prominent Save UX, original filenames, no forced .bin"`

---

### Task 20: Documentation sync

**Files:**
- Modify: `CLAUDE.md`, `src/ui/help.rs:33-39,57`

- [ ] **Step 1: CLAUDE.md** — update to match reality:
- Dedup: seahash over FULL content, recorded after accept (replace the "first 4KB + last 4KB" description).
- payload_store: `HashMap<String, PayloadEntry>` (bytes + received_at + filename), eviction policy (oldest plain beyond 500; chunk groups exempt, purged by generation).
- Chunk tracking: `TransferState` on the parent tree node; `__chunk` keys no longer create tree nodes or message-list entries.
- Export: validated reassembly (`Result`), filename via Zenoh attachment, suggested-name resolution order.
- Remove the Docker section (no Dockerfile exists in the repo) and the "No test suite" line (tests exist now); note `cargo test` in Build & Run.
- [ ] **Step 2: help.rs** — fix step numbering (1,2,3,5,6,4 → sequential) and the "trunctation" typo.
- [ ] **Step 3: Commit** — `git commit -am "docs: sync CLAUDE.md and help with implemented behavior"`

---

### Task 21: Final verification

- [ ] **Step 1:** `cargo fmt --all -- --check && cargo clippy -- -D warnings && cargo test` — all clean.
- [ ] **Step 2:** Manual end-to-end (two instances, peer mode): text publish → tree icons/counts/filter behave; import+publish a file with extension → receiver saves with original name; chunked transfer (lower `MAX_SINGLE_PAYLOAD` temporarily to exercise chunking, then revert) → progress node, completion ✓, save, byte-identical file (`shasum` both).
- [ ] **Step 3:** Push branch, open PR against `main` with `gh pr create`, summarizing the three phases. CI must pass on all 5 targets.

---

### Task 22: Port to send_it

The sibling repo `/Users/samelsner/Documents/github/send_it` (GitHub `dad-io/sendit`) shares the same source layout (`app.rs`, `events.rs`, `transfer.rs`, `types.rs`, `zenoh_worker.rs`, `ui/`) and the same `ci.yml`/`release.yml`. Port AFTER the zenoh-explorer PR is approved/merged.

- [ ] **Step 1:** In send_it: `git checkout -b feat/pipeline-integrity-tree-search`.
- [ ] **Step 2:** Diff each changed file against its zenoh-explorer counterpart (`diff src/transfer.rs ../zenoh-explorer/src/transfer.rs`) to gauge divergence. Where files are identical or near-identical, apply the zenoh-explorer commits with `git diff main..feat/pipeline-integrity-tree-search -- <file> | git apply -3` per file; where send_it has diverged (renames, removed debugger UI), re-apply each task's change by hand following this plan's code blocks — the task structure above IS the port checklist.
- [ ] **Step 3:** CI parity: apply Task 1 to send_it's `.gitignore` (if needed — it already tracks Cargo.lock), `ci.yml`, and `release.yml` — in particular the `macos-13` → `macos-14` x86_64 leg, fmt check, and the 5-target matrix (Windows x86_64, macOS aarch64+x86_64, Linux aarch64+x86_64).
- [ ] **Step 4:** `cargo fmt --all -- --check && cargo clippy -- -D warnings && cargo test` in send_it; run the Task 21 manual end-to-end between a send_it instance and a zenoh-explorer instance (cross-app transfer must work — same chunk key format and attachment convention).
- [ ] **Step 5:** Commit per phase (mirroring this plan's commit boundaries), push, open PR on dad-io/sendit.
