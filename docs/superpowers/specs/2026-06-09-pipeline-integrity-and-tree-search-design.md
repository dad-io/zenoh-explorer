# Design: Transfer Pipeline Integrity + i3x-Style Tree Search

**Date:** 2026-06-09
**Status:** Approved
**Origin:** Port of tree/search principles from i3X-Explorer PR #36
(ace-technologies-inc/i3X-Explorer, "Tree view: chevron accuracy, filter,
counts, icon system, leader lines") plus fixes for four data-corruption
paths found in a full application review.

## Goals

1. Make the file-transfer pipeline (the app's core value) trustworthy:
   no silent data loss, no corrupted exports, no transfers that can never
   complete.
2. Bring the topic tree up to the i3X-Explorer PR #36 standard: fast
   case-insensitive deep-match filtering, auto-expand on search, counts at
   every level with leader lines, a five-icon system, and chunk-aware
   transfer nodes.
3. Make saving a received file obvious and lossless: a prominent Save
   action wherever a payload is exportable, and original filenames /
   extensions preserved end-to-end instead of force-appending `.bin`.

## Non-Goals (recommended follow-ups, separate efforts)

- Worker command-loop refactor (large publishes currently block Ping /
  Disconnect / cancel for minutes and trigger a false "Worker Unresponsive").
- Send-progress events (`TransferProgress` / `TransferComplete` /
  `TransferFailed`), cancel, speed/ETA display.
- Drag-and-drop import (documented in CLAUDE.md but not implemented).
- The Zenoh Explorer → SendIT rename.
- Infra fixes: retired `macos-13` runner in release.yml, committing
  `Cargo.lock`, using `scripts/bundle-macos.sh` in releases, wrong
  `repository` URL in Cargo.toml — **except** the `.gitignore` lines that
  Phase 1e depends on (see below).

## Phase 1 — Transfer pipeline integrity

### 1a. Payload-store eviction (`src/events.rs`)

Today: at 500 entries, `store.keys().next()` evicts an arbitrary HashMap
key. Consequences: chunked transfers needing >500 chunk entries (~32 GB at
64 MB/chunk) can never complete; live chunks of in-progress transfers can
be silently deleted.

New policy:

- **Non-chunk entries:** evicted oldest-first by the `DateTime` already
  stored in the value tuple, once the non-chunk entry count exceeds the cap
  (500).
- **Chunk entries** (keys containing `/__chunk/`): exempt from the generic
  cap. Grouped per parent topic by `(total_size, total_chunks)` parsed from
  the key. When a chunk arrives whose `(total_size, total_chunks)` differs
  from the group currently stored for that topic, the stale group's entries
  are purged first. Invariant: at most one chunk group per topic, bounded
  by its own `total_chunks`.

### 1b. Reassembly validation (`src/transfer.rs`)

Today: `get_payload_for_export` collects all `{topic}/__chunk/*` keys
regardless of generation, checks completeness as `chunks.len() ==
total_chunks` taken from `chunks[0]`, never verifies indices or final
length, and returns `None` silently. `get_chunk_info` shares the
group-mixing bug (progress can read "12/8 chunks").

New behavior for both functions:

- Group chunks by `(total_size, total_chunks)`; operate on the newest group
  only (consistent with 1a's invariant; defensive against pre-1a state).
- Completeness = the set of chunk indices is exactly `0..total_chunks`
  (duplicates and out-of-range indices are detected, not just counted).
- After concatenation, `reassembled.len()` must equal `total_size`.
- Sanity-cap `total_size` (`total_size <= total_chunks * CHUNK_SIZE`)
  before `Vec::with_capacity` — the value comes from a network-controlled
  key string.
- Failures return a reason (`Result<Vec<u8>, String>` or equivalent)
  surfaced in the UI as an alert; export must never silently no-op.

### 1c. Dedup correctness (`src/events.rs`)

Today: the dedup hash covers key + length + first/last 4 KB only
(`DefaultHasher`/SipHash — CLAUDE.md's seahash claim is currently false),
so payloads differing only in the middle are dropped as duplicates within
the 60 s window, leaving stale bytes in `payload_store` for export.
Additionally, the hash is recorded *before* the rate-limiter drops a
message, so a retransmit of a rate-limited message is misclassified as a
duplicate and permanently lost.

New behavior:

- Hash the **full payload content** with `seahash` (dependency already
  declared; this makes CLAUDE.md true again). Keep the 60 s window.
- Record the hash only after the message is **accepted** (past the rate
  limiter), so dropped messages can be retransmitted.

### 1d. Pause semantics (`src/events.rs`)

Today: pausing a topic returns from `process_single_message` before any
storage, contradicting the tooltip ("messages still received, just not
displayed") — chunks arriving while paused are lost.

New behavior: pause skips only the messages-list display. `payload_store`
writes, browse-tree updates, and transfer-progress tracking continue.

### 1e. Tests + .gitignore prerequisite

Unit tests (pure logic, no GUI):

- Reassembly: complete transfer ordering; missing chunk → incomplete;
  stale-group chunks present → newest group wins; length mismatch → error;
  malformed `__chunk` keys ignored; `total_size` sanity cap.
- Eviction: oldest-first for non-chunk entries; incomplete chunk groups
  never evicted; stale group purged on new-generation chunk arrival.
- Dedup: identical payload within window → duplicate; same head/tail,
  different middle → NOT duplicate; rate-limited message's retransmit →
  not poisoned.

Prerequisite: remove the `tests/`, `test_*.rs`, and `Cargo.lock` lines from
`.gitignore` (currently they silently untrack any test suite and make
builds unreproducible) and commit `Cargo.lock`.

## Phase 2 — Tree & search port (`src/ui/topic_tree.rs`, `src/events.rs`, `src/types.rs`)

### 2a. Filter mechanics

- Case-insensitive substring match: query and node paths both lowercased.
  No debounce (matching i3x; recompute is cheap once non-quadratic).
- Replace per-node recursive `has_matching_descendant` (O(n²) per frame)
  with a precomputed **visible-path set**: one walk over the tree per
  (query, tree-version) change. Every node whose full path matches inserts
  its own path and its full ancestor chain into a `HashSet<String>`. The
  render pass is a set-membership check per node.
- The browse tree gains a version counter (bumped on every tree mutation in
  `events.rs`) so the visible set memoizes on `(query, version)`.

### 2b. Auto-expand on search

While the filter is non-empty, every branch with at least one visible
descendant renders expanded; egui's persisted `CollapsingState` is bypassed
(not overwritten). Clearing the filter restores the user's prior expansion
state. (Improvement over i3x, which expands only the three root folders and
never restores.)

### 2c. Counts + leader lines

- Branch nodes show cumulative descendant-leaf count, maintained
  incrementally at insert time on `ZenohNode` (not recomputed per frame).
  Leaf nodes keep message counts. Counts show unfiltered cardinality even
  while a filter is active (i3x convention).
- Between label and count: a leader line spanning the remaining row width —
  `Shape::dashed_line` at low opacity when collapsed, solid `line_segment`
  at higher opacity when expanded. Muted theme color (both palettes), count
  in tabular numerals without brackets, rendered only when count > 0.

### 2d. Icon system

Icons are deliberately NOT the i3X-Explorer set (user request 2026-06-09):
they lean zenoh / robotics / embedded / realtime-KV / physical automation.

| Icon | Applies to |
| ---- | ---------- |
| 🌐 | Top-level root nodes (network namespaces, depth 0) |
| 📡 | Branch nodes (gateways/hubs aggregating topics) |
| 💾 | Binary-payload leaves (firmware/blobs) |
| 🏷 | Text/JSON leaves (live key-value telemetry) |
| 🛠 | System topics (`@/...` zenoh admin space) |
| 📥 | Transfer nodes (incoming chunked file transfer) |

If a glyph renders as a tofu box in egui's emoji font, fall back per-icon:
🌐→🛰→🌍, 📡→🗼, 💾→🤖, 🏷→📟, 🛠→🔧, 📥→⬇.

Leaf bucketing prefers the message encoding, falling back to a payload
heuristic (valid UTF-8 → text). Chevron accuracy needs no resolver here:
`children.is_empty()` is already authoritative, and 2e removes the one
case that lies (chunk subtrees).

### 2e. Chunk-aware transfer node

- `__chunk` keys stop materializing tree nodes. `events.rs` routes them to
  a `TransferState` on the parent topic's node: `total_size`,
  `total_chunks`, received-index set, last-activity timestamp. Raw chunk
  bytes still go to `payload_store` for reassembly (unchanged contract
  with `transfer.rs`).
- The tree renders one node per in-flight/completed transfer: inline
  progress bar, "N/M chunks · X.XX GB", ✓ when complete, Export action on
  the node (wired to the validated 1b path).
- `__chunk` messages are excluded from the messages list (today a 128-chunk
  transfer floods the 500-message display).

## Phase 3 — Export UX & filename persistence (`src/transfer.rs`, `src/ui/topic_tree.rs`, `src/zenoh_worker.rs`, `src/events.rs`)

### 3a. Filename transmission

The sender already knows the imported file's name
(`publish_payload_filename`, captured at import) but never transmits it, so
receivers can only guess from the topic key.

- On publish of an imported file, attach the original filename as a Zenoh
  **attachment** on the `put` (zenoh 1.0 supports attachments). For chunked
  publishes, the attachment rides on every chunk (cheap, makes any chunk
  sufficient to learn the name).
- The receive path reads `sample.attachment()` and stores the filename
  alongside the bytes. `payload_store`'s value becomes a small struct
  (`PayloadEntry { bytes, received_at, filename: Option<String> }`) —
  coordinated with 1a, which already restructures this storage for
  eviction.

### 3b. Suggested filename & extension preservation

Export must never blindly append `.bin`. Suggested-name resolution order:

1. Original filename from the transmitted attachment (3a).
2. If the topic's last segment contains an extension (e.g.
   `files/report.pdf`), use that segment verbatim.
3. Fallback only: `topic_with_underscores.bin`.

The save-dialog filter list is built dynamically: when an extension is
known, the first filter matches it (e.g. "PDF files (*.pdf)") followed by
"All Files" — never a leading "Binary Files (*.bin)" filter that platforms
use to force-append `.bin`.

### 3c. Export discoverability

Today export is a plain, unlabeled-by-intent "Export Payload" button inside
the topic-details panel only, and chunk completion says "click Export to
reassemble" without offering the button.

- Replace it with a prominent primary-styled **"💾 Save File (X.XX GB)"**
  button (accent fill, size included in the label) at the top of topic
  details. When the payload is unavailable or chunks are incomplete, the
  button is disabled with a tooltip explaining why ("waiting for 12 more
  chunks").
- The "✓ All chunks received" completion row gets an inline Save button.
- Tree rows that have an exportable payload (and Phase 2e transfer nodes)
  show a small 💾 action on hover/selection, so saving doesn't require
  finding the details panel.
- Failures (incomplete, length mismatch, fs error) surface as alerts per
  Phase 1b — a failed save must never look identical to a successful one.

## Error handling summary

- Export failures (incomplete group, length mismatch, fs write error)
  surface as UI alerts with the reason; never silent.
- Eviction and group-purge events are logged at debug level.
- All parsing of `__chunk` key metadata is defensive: malformed keys are
  ignored, never panic (the values are network-controlled).

## Testing summary

Phase 1 logic is covered by the unit tests in 1e. Phase 2's visible-set
computation gets unit tests (match → ancestors visible; no match → hidden;
case-insensitivity). Phase 3's suggested-name resolution is pure logic and
gets unit tests (attachment wins; last-segment extension preserved without
`.bin`; underscore fallback only when no extension is known). Rendering and
the attachment round-trip are verified manually (`cargo run` against a
local zenoh router with a chunked transfer).
