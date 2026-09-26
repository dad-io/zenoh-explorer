# P4 · File Transfer Protocol v2 Implementation Plan (concurrent lanes)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Only modify the files listed under your task's "Owns"**. Another task may be running at the same time on every other file.

**Goal:** Replace the push-and-reassemble `__chunk` file transfer with a pull protocol. The new protocol has a manifest, BLAKE3 verification, bounded memory on both sides, resume, cancel from either side, and one source of truth for transfer state in the UI.

**Architecture (summary; full rationale in [Architecture](#architecture)):**
- **Sender:** announces a JSON manifest at `{key}/@xfer/{id}/manifest`. It serves chunk *i* on request from a Queryable at `{key}/@xfer/{id}/chunk?i=<i>`, reading the file from disk with positioned reads, and holds a liveliness token at `{key}/@xfer/{id}`.
- **Receiver:** discovers offers with `**/@xfer/*/manifest`. It pulls chunks through a `Querier` with 4 requests in flight, verifies each chunk against the manifest's BLAKE3 hash, writes into a spool file in the OS temp dir, verifies the whole file, and then offers Save.
- **Invisible to other traffic:** the `@xfer` chunk is verbatim, so no `**` subscription (including the explorer's own monitor) ever sees transfer traffic. Transfer events flow to the UI as `ZenohEvent::Transfer`, outside the sample ingest, rate limiter and dedup.
- **UI state:** a `TransferRegistry` replaces both the tree's `TransferState` and the export store's chunk entries.

**Tech Stack:**
- Rust 2021.
- zenoh 1.10.1 (`unstable` feature, as P1 left it): `Session::declare_querier`, `Queryable`, `liveliness()`, and `QoS` on the querier.
- blake3 1.8.
- tempfile 3.
- fs4 1.1 for free disk space.
- rand 0.8 for transfer IDs (already in the tree through zenoh).
- serde + serde_json.
- tokio (`JoinSet`, `watch`).
- egui/eframe 0.36, rfd 0.17 and egui_kittest 0.36 (after P3).

**Spec:** `docs/superpowers/reviews/2026-09-25-zenoh-explorer-deep-review.md`, "Deferred to later plans" → "File-transfer protocol redesign: manifest, transfer ID, BLAKE3, Querier pull". This plan implements that item.

**Programme:** this is P4 of five, run in order: P1 correctness (`2026-09-25-correctness-and-hardening.md`), P2 CI/release (`2026-09-25-p2-ci-release-hardening.md`), P3 egui 0.36 port, P4 (this plan), then P5 explorer features.

**Decisions:** none recorded (`docs/memex` has 0). **Kind of change:** one feature, the transfer protocol, together with the removal of the protocol it replaces. There are no other features and no unrelated upgrades.

**State this plan assumes (after P1, P2 and P3):**
- **Worker:** `src/worker/{mod,state,pipeline,session,subscribe,query,publish,queryable,connect,samples}.rs`.
  - `WorkerState` (in `state.rs`, `#[derive(Default)]`) has `async fn teardown(&mut self)`.
  - `WorkerCtx { event_sender: EventTx, local_kvstore, sample_drops }`.
  - `pipeline::EventTx = std::sync::mpsc::SyncSender<ZenohEvent>`.
  - `session::handle_connect(st, ctx, …)`.
  - `publish::handle_publish` with `PublishShape { Single, Chunked { chunks } }`.
- **Types:** `src/types/{mod,message,commands,tree,limits,store}.rs`. `ZenohEvent::OperationFailed { op: FailedOp, error }` and a manual payload-free `Debug` for `ZenohCommand`.
- **Events:** `src/events/{mod,ingest,json_cache}.rs`.
- **App:** `src/app/{mod,layout,theme}.rs`. `ZenohExplorer::new(ctx)` and `#[cfg(test)] ZenohExplorer::test_app() -> (Self, std::sync::mpsc::Sender<ZenohEvent>)`.
  - `ui_alert: Option<UiAlert>` (P1 T21), with `crate::app::UiAlert::{Success, Warning, Error}(String)`. Every assignment in this plan wraps its text in one of the variants; a bare `String` does not compile.
- **Transfer:** `src/transfer.rs` still holds the v1 code: `CHUNK_SIZE` (64 MiB), `parse_chunk_key`, `ChunkMeta`, `chunk_progress`, `insert_payload`, `get_payload_for_export`, `stored_filename` (P3 T13; its fallback calls `chunk_progress` and `parse_chunk_key`), `sanitize_filename`, `suggested_export_filename`, `format_size`, `MAX_PLAIN_BYTES`, `gc_stale_transfers`. P3 T13 deleted `export_payload_to_file`.
- **UI (P3):** egui 0.36. File dialogs use `rfd::AsyncFileDialog` and file I/O happens off the UI thread. There is an egui_kittest harness. P3's kittest tests live inside the binary crate in `src/ui/tests/{mod,shell,tree,a11y,views,snapshots}.rs`, not under `tests/ui/`, because an integration test under `tests/` cannot reach the internals of a bin-only crate.
  - `save_topic_to_file` is a method of the `crate::ui::file_jobs::FileJobsUI` trait (P3 T3), no longer of `TopicTreeUI`. A caller needs `use crate::ui::file_jobs::FileJobsUI;`.
  - `crate::dialogs::spawn_import(ctx, max_bytes)` and `IMPORT_MAX_BYTES` (4 GiB) come from P3 T2. P3 T12's Import button passes `IMPORT_MAX_BYTES`. A file over the cap is refused with P3's `import_size_error` text, which prints both sizes with `format_size` (for example "64.00 MB").
  - P3 T16's wgpu snapshot tests (`ui::tests::snapshots::{topics_dark,publish_light,tree_filtered_dark}`) compare against `tests/snapshots/*.png`. They are `#[ignore]`d, and they run under `cargo test -- --ignored` on macOS and in the `ui-snapshots` CI job.
- **Line numbers:** every line number below is a **pre-P1** number from commit `cf9fb6c`, because P1–P3 move code. Find the code by the symbol named next to each number.

## Global Constraints

- After every task, run `cargo build`, `cargo test`, `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`, and all must pass.
- A task may create or modify only the files in its **Owns** list. If a step seems to need another file, stop and report instead of editing it.
- **New crates:** exactly `blake3 = "1.8"`, `fs4 = "1.1"`, `tempfile = "3"` and `rand = "0.8"`, plus `serde` with `derive` (already a transitive dependency). No other crate is added. `cargo audit` must stay at 0 unignored vulnerabilities.
- zenoh stays at the version P1–P3 left (`Cargo.lock` resolves `zenoh 1.10.1`). Every zenoh API below was checked against the 1.10.1 source and docs.rs.
- **No whole-file buffers, anywhere:**
  - The sender reads one chunk at a time.
  - The receiver holds at most `in_flight × chunk_size` bytes per transfer (16 MiB by default).
  - Save copies file to file.
- Transfer traffic never enters `events/ingest.rs`, the rate limiter or the deduper.
- Network data is never `unwrap()`ed outside `#[cfg(test)]`.
- **Tests that open Zenoh sessions:**
  - Use `#[tokio::test(flavor = "multi_thread", worker_threads = 4)]`, multicast scouting disabled, session A listening on `tcp/127.0.0.1:<port>` and session B connecting to it.
  - Every such test is `#[ignore = "opens network sessions"]` and uses its own port from the port table in T1. The task's Done-when runs them explicitly.
- Do not touch `colors.rs`, `app/theme.rs` or the tree's `plus_minus_icon`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Zero-byte file offered.** Expected: a manifest with `chunk_count: 0`. The fetch completes with no requests, and Save writes an empty file. Pinned by T1 `chunk_math_handles_empty_exact_and_partial`, T2 `prepare_empty_file` and T5 `zero_byte_file_completes`.
- **File modified or truncated on disk after it was offered.** Expected: the sender replies `source-changed`, and the receiver fails fast with `SourceChanged` instead of retrying until timeout. Pinned by T2 `read_chunk_detects_source_change` and T5 `source_changed_fails_fast`.
- **Receiving your own offer.** One app is both sender and discoverer on the same session, so discovery hears its own manifest. Expected: one outgoing entry, no duplicate incoming entry. Pinned by T7 `own_offer_is_not_duplicated_as_incoming`.
- **Sender leaves after the file is already verified or saved.** Expected: the entry stays Verified or Saved, and "sender gone" must not turn a finished file into a failure. Pinned by T7 `sender_gone_after_verified_keeps_verified`.
- **Offer key with a wildcard or an `@` chunk** (`demo/*`, `@/x`, `a/@b`). Discovery can never see such offers. Expected: refused before any network traffic, with an inline reason. Pinned by T1 `offer_key_rejects_wildcards_and_verbatim` and T9 `offer_button_disabled_for_bad_key`.

## Tasks

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Lane Contract · owns Cargo.toml, Cargo.lock, src/transfer.rs→src/transfer/export.rs, src/transfer/{mod,manifest,event,limits,tasks,registry,test_support,loopback_tests}.rs, src/transfer/sender/{mod,prepare,serve}.rs, src/transfer/receiver/{mod,spool,fetch,discovery}.rs, src/types/commands.rs, src/worker/{mod,state,session}.rs, src/events/mod.rs, src/app/mod.rs] Protocol contract and scaffolding: new crates; the `transfer/` module tree; the complete `Manifest`/`TransferId`/key-space/chunk math; `TransferEvent`, `TransferCommand`, `FailReason`, `TransferLimits`; `TaskHandle`/`TransferTasks`/`dispatch`; registry data types; worker, event and app wiring; test support. The four lane entry points are no-op-safe stubs with final signatures | — | `cargo test transfer::` passes, including `keyexpr_semantics_for_xfer_space`, `manifest_*`, `chunk_math_*`, `offer_key_rejects_wildcards_and_verbatim` and `tasks::*`. `cargo test -- --ignored session_pair_connects` passes. `cargo audit` shows 0 unignored vulnerabilities. |
| T2 | [Wave 1 · Lane A · owns src/transfer/sender/prepare.rs] Offer preparation: stream-hash a file into a `Manifest` (per-chunk and whole-file BLAKE3), and `read_chunk` with positioned reads and source-change detection | T1 | `cargo test sender::prepare` passes (6 tests, including `prepare_empty_file` and `read_chunk_detects_source_change`). |
| T3 | [Wave 2 · Lane A · owns src/transfer/sender/serve.rs] `spawn_offer`: liveliness token, manifest queryable and put, chunk queryable with bounded concurrent serving, progress, idle expiry, and withdraw on cancel | T2 | `cargo test -- --ignored sender::serve` passes (4 tests, including `offer_is_invisible_to_star_star_subscribers` and `cancel_withdraws_manifest_and_token`). |
| T4 | [Wave 1 · Lane B · owns src/transfer/receiver/spool.rs] Spool: preallocated `.part` with a manifest sidecar, per-chunk verify-and-write, resume scan, whole-file verify, admission (size and disk space), atomic save via `tempfile`, spool GC, and `spawn_save` | T1 | `cargo test receiver::spool` passes (9 tests, including `write_chunk_rejects_corrupt_and_wrong_length` and `reopen_resumes_verified_chunks`). |
| T5 | [Wave 2 · Lane B · owns src/transfer/receiver/fetch.rs] `spawn_fetch`: presence check, a `Querier` with DataLow/Block QoS, N in flight, per-request timeout, retry with backoff, liveliness abort, inactivity timeout, throttled progress, and a final hash check | T4 | `cargo test -- --ignored receiver::fetch` passes (10 tests: dropped chunk retried, corrupt chunk rejected, resume after restart, sender gone, zero-byte file, …). |
| T6 | [Wave 2 · Lane B · owns src/transfer/receiver/discovery.rs] `spawn_discovery`: `**/@xfer/*/manifest` subscriber plus an initial GET, liveliness `**/@xfer/*`, strict announcement parsing, withdraw and sender-gone events, spool GC at start | T4 | `cargo test receiver::discovery` passes, and `cargo test -- --ignored receiver::discovery` passes (5 loopback tests, including `wildcard_manifest_subscription_matches_verbatim_chunk`). |
| T7 | [Wave 1 · Lane C · owns src/transfer/registry.rs] `TransferRegistry` state machine: `apply`, `mark_fetch_requested`, `on_disconnected`, `gc`, `for_key`, `badge_for_key`, `progress_fraction` and `status_label` | T1 | `cargo test transfer::registry` passes (10 tests). |
| T8 | [Wave 2 · Lane C · owns src/events/mod.rs, src/events/ingest.rs, src/types/tree.rs, src/ui/topic_tree.rs] Wire the registry in: announced keys become tree nodes, a tree badge comes from the registry, and `TransferState`/`record_chunk`/chunk-progress UI are removed | T7 | Tests `announced_transfer_creates_tree_node_without_message_count`, `transfer_events_bypass_rate_limit_and_dedup` and `disconnect_fails_active_transfers` pass. `grep -rn 'TransferState\|record_chunk\|chunk_progress' src/types src/ui src/events` prints nothing. |
| T9 | [Wave 3 · Lane C · owns src/ui/transfers.rs, src/ui/mod.rs, src/ui/topic_tree.rs, src/ui/publish.rs, tests/snapshots/publish_light.png] Transfers panel in Topic Details (Fetch / Cancel / Save… / Dismiss, progress, max-size setting); "Offer File as Transfer…" in Publish; Import passes `PLAIN_PUBLISH_MAX` to the P3 import size check, so plain publish is capped at it (UI review F-T15-5); the `publish_light` snapshot reference is regenerated for the new Publish row | T8 | Tests `ui::transfers::` pass (`action_rules`, `offer_button_disabled_for_bad_key`, and kittest `fetch_button_emits_fetch_action`), and `ui::publish::tests::import_cap_is_plain_publish_max` passes. `grep -A3 'spawn_import(' src/ui/publish.rs` shows `import_cap()` and no `IMPORT_MAX_BYTES`. On macOS, `cargo test ui::tests::snapshots -- --ignored` passes (3 tests) with the regenerated `publish_light.png`. Manual check in T13. |
| T10 | [Wave 3 · Lane D · owns src/transfer/export.rs, src/worker/publish.rs, src/events/ingest.rs, src/transfer/registry.rs] v1 `__chunk`: remove sending (always); receiving is **removed (Option B, recommended default)** or **kept read-only for one release (Option A)**, per open question Q1 | T8 | Always: `publish_shape_rejects_above_plain_max` passes, and `grep -n 'Chunked' src/worker/publish.rs` prints nothing. **B:** `v1_chunk_keys_are_ordinary_topics` passes, and `grep -rnw 'CHUNK_SIZE\|parse_chunk_key\|ChunkMeta\|chunk_progress' src` prints nothing (`-w`, so the v2 constants `MIN_`/`MAX_`/`DEFAULT_CHUNK_SIZE` do not match). **A:** `legacy_chunks_feed_registry` and `legacy_chunk_messages_reach_registry` pass. |
| T11 | [Wave 1 · Lane E · owns README.md, src/ui/help.rs, src/transfer/README.md] Docs: README "File transfer" section, Help tab text (the Publish step in P1 T26's `HELP_SECTIONS` and a "File transfers" section), and the `src/transfer/` module README (purpose, interfaces, invariants, tests) | T1 | `ui::help::tests::help_points_large_files_to_transfers` passes. `grep -n '@xfer' README.md src/transfer/README.md` shows the key-space table. `grep -n 'Offer File as Transfer' src/ui/help.rs` matches. Under Q1 Option B, the test also asserts that no Help line mentions `file chunks`. `cargo build` passes. |
| T12 | [Wave 3 · Lane F · owns src/transfer/loopback_tests.rs] End-to-end loopback with the real sender, discovery and fetcher: a 20 MiB round trip, the sender disappearing, and two concurrent same-size transfers on one key | T3, T5, T6 | `cargo test -- --ignored transfer::loopback_tests` passes (3 tests). |
| T13 | [Wave 4 · Integration · owns src/transfer/mod.rs] Remove the Wave 0 `#![allow(dead_code)]`, run the full suite plus ignored tests plus audit, and do a two-instance manual smoke run | T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12 | Every command in T13 passes and its output is pasted into the evidence. `grep -n 'allow(dead_code)' src/transfer/mod.rs` prints nothing. |

## Lanes and waves

```
Wave 0   T1 (contract: crates, module tree, protocol types, wiring, stubs)
           │
           ├──────────────┬───────────────┬────────────────┬──────────────┐
Wave 1   A: T2 prepare   B: T4 spool     C: T7 registry   E: T11 docs    │
           │              ├──────┐        │                               │
Wave 2   A: T3 serve     B: T5 fetch     C: T8 wiring                     │
                         B: T6 discovery  ├───────────┐                   │
Wave 3   F: T12 e2e (T3,T5,T6)           C: T9 UI    D: T10 v1 compat    │
           │                              │           │                   │
Wave 4   T13 integration (all) ◄──────────┴───────────┴───────────────────┘
```

The maximum width is 4 tasks at once, in Wave 1 (T2, T4, T7 and T11) and in Wave 2 (T3, T5, T6 and T8). Lanes: **A** sender, **B** receiver, **C** registry and UI, **D** v1 compatibility, **E** docs, **F** end-to-end tests.

**Q1 gate.** Settle open question Q1 before Wave 1 starts, and do not change it afterwards. T11 (Wave 1) writes the Q1 outcome into Help and the README, and T10 (Wave 3) implements it in code. Both tasks use the same default: with no answer, Option B. If Q1 changes after T11 has merged, T10 cannot repair the docs because it does not own `help.rs` or `README.md`, so open a fix task on Lane E (T11). T13 Step 3 checks that the docs and the code agree.

**File ownership matrix.** A file listed for several tasks is always reached through a dependency chain, so no two of them run at the same time.

| File | Tasks (in dependency order) |
|---|---|
| Cargo.toml, Cargo.lock | T1 |
| src/transfer.rs → src/transfer/export.rs | T1 (move) → T10 |
| src/transfer/mod.rs | T1 → T13 |
| src/transfer/manifest.rs, event.rs, limits.rs, tasks.rs, test_support.rs | T1 |
| src/transfer/sender/mod.rs, receiver/mod.rs | T1 |
| src/transfer/sender/prepare.rs | T1 (empty) → T2 |
| src/transfer/sender/serve.rs | T1 (stub) → T3 (via T2) |
| src/transfer/receiver/spool.rs | T1 (stub) → T4 |
| src/transfer/receiver/fetch.rs | T1 (stub) → T5 (via T4) |
| src/transfer/receiver/discovery.rs | T1 (stub) → T6 (via T4) |
| src/transfer/registry.rs | T1 (types) → T7 → T10 (via T8) |
| src/transfer/loopback_tests.rs | T1 (empty) → T12 |
| src/types/commands.rs | T1 |
| src/worker/mod.rs, state.rs, session.rs | T1 |
| src/worker/publish.rs | T10 |
| src/app/mod.rs | T1 |
| src/events/mod.rs | T1 → T8 (via T7) |
| src/events/ingest.rs | T8 → T10 |
| src/types/tree.rs | T8 |
| src/ui/topic_tree.rs | T8 → T9 |
| src/ui/transfers.rs, src/ui/mod.rs, src/ui/publish.rs | T9 |
| tests/snapshots/publish_light.png (P3 T16 reference) | T9 |
| README.md, src/ui/help.rs, src/transfer/README.md | T11 |

Concurrency check for Wave 2: T3 {serve.rs}, T5 {fetch.rs}, T6 {discovery.rs} and T8 {events/*, types/tree.rs, ui/topic_tree.rs} are disjoint. For Wave 3: T9 {ui/*, tests/snapshots/publish_light.png}, T10 {export.rs, worker/publish.rs, events/ingest.rs, registry.rs} and T12 {loopback_tests.rs} are disjoint. T10 needs `ingest.rs` after T8 and `registry.rs` after T7, and both are ancestors of T10. T11 touches only docs and help, so it can run alongside anything.

**Merging:** each task commits only its owned files. Wave 1 tasks branch from the T1 commit. Because owned files are disjoint, their merges are conflict-free by construction.

## Architecture

### Key space (verified)

| Resource | Key expression | Declared by | Used by |
|---|---|---|---|
| Liveliness token | `{key}/@xfer/{id}` | sender | receiver: sender presence and loss |
| Manifest | `{key}/@xfer/{id}/manifest` (put once, plus a queryable for late joiners, deleted on withdraw) | sender | discovery |
| Chunks | `{key}/@xfer/{id}/chunk`, selector parameter `i=<index>` | sender Queryable | receiver Querier |
| Discovery of manifests | `**/@xfer/*/manifest` | receiver subscriber and GET | — |
| Discovery of tokens | `**/@xfer/*` | receiver liveliness subscriber | — |

- `{id}` is a random `u128`, written as 32 lowercase hex characters.
- `{key}` must be a non-wild key with no `@`-prefixed chunk (`offer_key_error`).

**Why `@xfer`.** The key-expression RFC (https://github.com/eclipse-zenoh/roadmap/blob/main/rfcs/ALL/Key%20Expressions.md) defines chunks that start with `@` as *verbatim*: a wildcard never matches them, and only the identical chunk does.
- This was checked against the `zenoh-keyexpr 1.10.1` source: `intersect/classical.rs:69-100` and `intersect/mod.rs:84-100`, the `has_verbatim` and `has_direct_verbatim` functions.
- It was also checked by running `keyexpr::intersects` in a scratch crate on 2026-09-25:

| a | b | intersects |
|---|---|---|
| `**` | `demo/f/@xfer/abc/manifest` | **false** |
| `demo/**` | `demo/f/@xfer/abc/manifest` | **false** |
| `**/@xfer/*/manifest` | `demo/f/@xfer/abc/manifest` | **true** |
| `**/@xfer/*/manifest` | `@xfer/abc/manifest` | true |
| `**/@xfer/*/manifest` | `demo/f/@xfer/abc/chunk` | false |
| `**/@xfer/*/manifest` | `a/@b/@xfer/abc/manifest` | **false**, so keys with an `@` chunk cannot be offered |
| `**/@xfer/**` | `demo/f/@xfer/abc` | true |
| `*` | `@xfer` | false |

What this means in practice:
- The explorer's own monitor subscription `**` and any third-party `**` subscriber never receive manifest or chunk traffic. This ends the v1 `__chunk` hijack and the "all chunks flood every monitor" problem.
- Discovery works only because its subscription names the literal `@xfer` chunk.
- T1 pins this table as the unit test `keyexpr_semantics_for_xfer_space`. T6 pins it on the wire as `wildcard_manifest_subscription_matches_verbatim_chunk`.

**Why the chunk index is a selector parameter and not `chunk/{i}`.**
- A `Querier` is bound to one key expression when it is declared (`Session::declare_querier(key_expr)`, https://docs.rs/zenoh/1.10.1/zenoh/struct.Session.html#method.declare_querier; `Querier::get() -> QuerierGetBuilder`, https://docs.rs/zenoh/1.10.1/zenoh/query/struct.Querier.html). Per-index keys would therefore need a fresh `Session::get` per chunk and would lose the querier's reusable QoS and matching status.
- The index travels as `QuerierGetBuilder::parameters("i=5")`, which takes `P: Into<Parameters>` (`String` and `&str` both qualify). The sender reads it back with `Query::parameters().get("i")` (https://docs.rs/zenoh/1.10.1/zenoh/query/struct.Query.html#method.parameters).
- The sender replies on the concrete key `{key}/@xfer/{id}/chunk`.

### Pull over push: why a Querier and not zenoh-ext AdvancedPublisher

`zenoh-ext 1.10.1` offers `AdvancedPublisher` and `AdvancedSubscriber`: a publisher-side cache (`CacheConfig::max_samples`), `sample_miss_detection`, and subscriber recovery with a miss listener. Sources: https://docs.rs/zenoh-ext/1.10.1/zenoh_ext/struct.AdvancedPublisherBuilder.html, https://docs.rs/zenoh-ext/1.10.1/zenoh_ext/struct.CacheConfig.html and https://docs.rs/zenoh-ext/1.10.1/zenoh_ext/struct.AdvancedSubscriberBuilder.html. It was rejected for five reasons:

1. **Memory.** Recovery can only resend what is in the publisher cache. That cache is an in-memory store of the last `max_samples` samples per resource. Recovering any chunk of a 10 GiB file would mean caching 10 GiB, or losing recovery of early chunks. The pull sender reads chunk *i* from disk on demand, so its memory is O(concurrent requests × chunk).
2. **Flow control.** Push sends at the sender's pace to every matching subscriber. The pull receiver sets its own pace (N requests in flight), so a slow receiver cannot make the sender block the DataLow queue for a fast one.
3. **Opt-in.** With push, every receiver that subscribes gets every file. With pull, bytes move only when a user clicks Fetch, and discovery costs one manifest per offer.
4. **Resume and cancel** reduce to "request the missing indices" and "stop requesting". Push recovery is bounded by the cache window and is not addressable per index.
5. **Stability.** Every `AdvancedPublisherBuilder` method and `CacheConfig` is marked **unstable** on docs.rs 1.10.1. `declare_querier`, `Querier::get`, `Querier::matching_status`, `declare_queryable` and `liveliness()` are not gated behind `unstable` in `zenoh-1.10.1/src/api/{session,querier}.rs`.

The trade-off is one round trip per chunk. At 4 MiB chunks with 4 in flight, that round trip is hidden behind the transfer of the other three chunks.

### Manifest (JSON, `application/json`)

```json
{"version":2,"transfer_id":"<32 hex>","key":"demo/files/big","filename":"big.iso",
 "total_size":1073741824,"chunk_size":4194304,"chunk_count":256,
 "file_hash":"<64 hex blake3>","chunk_hashes":["<64 hex>", …],"encoding":"application/octet-stream"}
```

**Per-chunk hashes live in the manifest, not in a reply attachment.**
- An attachment travels with the chunk it protects, so it cannot authenticate that chunk. Manifest hashes let each chunk be verified the moment it arrives and retried alone.
- The resume scan needs them to re-verify chunks already on disk.
- The cost is at most `MAX_CHUNKS = 16_384` × about 67 bytes, roughly 1.1 MB, well under the receiver's `MAX_MANIFEST_BYTES = 2 MiB` parse cap.

**Chunk size is 4 MiB by default**, within bounds of 64 KiB to 64 MiB:
- Large enough that the per-request round trip is small next to the transfer time, even on a LAN.
- Small enough that one retry costs little, and that per-transfer receiver memory (4 × 4 MiB) and chunk count (16 GiB default cap / 4 MiB = 4096) stay modest.
- Far below the 1 GiB default `max_message_size` that P1 restored, so no transport fragmentation limit applies.

**BLAKE3** (https://docs.rs/blake3/1.8.2/blake3/struct.Hash.html, `Hash::from_hex` and `to_hex`) is fast enough, over 1 GB/s on one core, that hashing at offer time and verifying at the end are not bottlenecks. It is an integrity check, **not authentication**. Anyone on the network can announce a manifest, and authenticating senders is out of scope, as it is for every other key in the explorer.

### QoS

- **Chunks:** the receiver's `Querier` is declared with `.priority(Priority::DataLow)`, `.congestion_control(CongestionControl::Block)`, `.target(QueryTarget::BestMatching)` and `.consolidation(ConsolidationMode::None)`.
  - Replies inherit the query's QoS. In `zenoh-1.10.1/src/api/builders/reply.rs:146-157`, `ReplyBuilder::priority`/`congestion_control` are `#[deprecated = "calling this function has no impact, replies will use the query priority"]`. So the sender must **not** call them, which would also break `-D warnings`.
- **Manifest put:** `Priority::DataLow` and `Block`.
- **Why this protects telemetry:** Zenoh keeps separate transmit queues per priority. P1 kept `queue.size.data_low = 16`. Telemetry at the default `Data` priority is therefore never queued behind transfer chunks, and `Block` back-pressure applies only to the DataLow queue.

### Threads and data flow

```
UI thread (egui)                         worker tokio runtime (publishing session)
─────────────────                        ─────────────────────────────────────────
TransferRegistry ◄─ ZenohEvent::Transfer ─┐  TransferTasks { by_id, background, discovery }
  (single source of truth)                 │    ├─ sender::spawn_offer   ── Queryable(manifest, chunk), token, put
UI actions ── ZenohCommand::Transfer ────► dispatch()  ├─ receiver::spawn_fetch ── Querier, liveliness sub, Spool (spawn_blocking I/O)
                                           │    ├─ receiver::spawn_save  ── tempfile persist (spawn_blocking)
                                           └─── └─ receiver::spawn_discovery ── sub **/@xfer/*/manifest, GET, liveliness **/@xfer/*
```

- **Event sink:** `EventSink = Arc<dyn Fn(TransferEvent) + Send + Sync>`.
  - In production it is `sink_from(EventTx)`. `Progress` uses `try_send`, so it can be dropped under back-pressure. Every other event uses `send`.
  - In tests it collects into a tokio channel.
- **Buffer thread:** already forwards every non-sample event unchanged, so transfer events skip batching, the rate limiter and dedup.

### State machine (UI registry)

```
Incoming: Available ─Fetch─► Fetching{done} ─Completed─► Verified ─Saved─► Saved
              │                   │  ▲                       │
              │                   └──┴─ Failed(retryable) ◄──┘ (only save errors set last_error; status stays Verified)
              └─ Failed(SenderGone | Cancelled{by_sender})     (ignored once Verified/Saved)
Outgoing: Preparing ─Announced─► Offering{served} ─Failed(Cancelled|OfferExpired|…)─► Failed
```

The first `Failed` wins, and later `Failed` events for the same ID are ignored. This matters because a sender that withdraws produces both `Cancelled{by_sender}` (manifest delete) and `SenderGone` (token drop), in either order.

### Resume, cancel, garbage collection and limits

- **Spool:** `{spool_dir}/{id}.part` is preallocated with `set_len(total_size)`, and `{id}.manifest.json` holds a byte-identical copy of the manifest.
  - On fetch start, if the sidecar equals the new manifest and the `.part` length matches, every chunk region is re-hashed and verified chunks are skipped.
  - No progress bitmap is stored, so a crash can never leave a bitmap that lies.
- **Cancel:**
  - By the user (receiver): stop requesting and delete the spool.
  - By the user (sender): `session.delete(manifest_key)`, then drop the token and queryables. Discovery reports `Cancelled{by_sender:true}` to everyone.
  - Disconnect or shutdown: tasks are cancelled with `CancelReason::Shutdown`. The spool is kept for resume, and the registry marks active entries `NotConnected`.
- **Timeouts:**
  - Each request times out after `request_timeout` (30 s).
  - Each chunk gets `max_retries` (5) with linear backoff of 100 ms × attempt, capped at 1 s.
  - With no verified chunk for `inactivity_timeout` (120 s), the fetch fails with `Stalled`.
  - A sender with no chunk request for `offer_idle_timeout` (30 min) withdraws with `OfferExpired`.
  - Spool files older than `SPOOL_MAX_AGE` (24 h) are removed when discovery starts.
- **Limits:**
  - At most 4 active fetches and 4 active offers.
  - `max_file_size` defaults to 16 GiB and can be edited in the UI.
  - Disk check before accepting: `fs4::available_space(spool_dir) ≥ remaining + 64 MiB margin`. The signature is `pub fn available_space<P: AsRef<Path>>(path: P) -> std::io::Result<u64>`, https://docs.rs/fs4/1.1.0/fs4/fn.available_space.html.
  - Filenames go through P1's `sanitize_filename`, and the manifest is rejected unless its filename is already sanitized.
  - Spool paths are built only from the hex ID, never from network strings.
- **Save:** `tempfile::NamedTempFile::new_in(dest.parent())`, then copy, `sync_all`, and `persist(dest)`. The rename happens within one directory, so it is atomic and never crosses a filesystem.

### Event and command variants (why six events, not four)

The brief lists `Announced / Progress / Completed / Failed`. Two more are needed:
- `Preparing`, so a multi-GB offer is visible while it is being hashed.
- `Saved`, because Save is a separate user action that can succeed or fail after `Completed`.

Commands are grouped into one `ZenohCommand::Transfer(TransferCommand)` variant, so `types/commands.rs` and the worker's match change once, in Wave 0.

### v1 `__chunk` compatibility

**Sending v1 is removed in all cases.** Files over `PLAIN_PUBLISH_MAX` (64 MiB) must use "Offer File as Transfer". Q1 decides what happens to v1 *receiving*. T10 implements both options behind one clearly named task.

---

### Task T1: Protocol contract and scaffolding (Wave 0)

**Owns:**
- `Cargo.toml`, `Cargo.lock`
- `src/transfer.rs` (moved with `git mv` to `src/transfer/export.rs`, content unchanged)
- New: `src/transfer/{mod,manifest,event,limits,tasks,registry,test_support,loopback_tests}.rs`, `src/transfer/sender/{mod,prepare,serve}.rs`, `src/transfer/receiver/{mod,spool,fetch,discovery}.rs`
- `src/types/commands.rs`, `src/worker/mod.rs`, `src/worker/state.rs`, `src/worker/session.rs`, `src/events/mod.rs`, `src/app/mod.rs`

**Interfaces (produces; every later task consumes these exact names):**
- `transfer::manifest`:
  - Constants: `PROTOCOL_VERSION: u32 = 2`, `XFER_CHUNK = "@xfer"`, `CHUNK_PARAM = "i"`, `ERR_SOURCE_CHANGED = "source-changed"`, `DISCOVERY_MANIFESTS = "**/@xfer/*/manifest"`, `DISCOVERY_TOKENS = "**/@xfer/*"`, `MIN_CHUNK_SIZE`, `MAX_CHUNK_SIZE`, `DEFAULT_CHUNK_SIZE`, `MAX_CHUNKS`, `MAX_MANIFEST_BYTES`.
  - `TransferId(pub u128)`: `random()`, `Display` (32 hex), `FromStr`, serde as a string.
  - `Manifest { version, transfer_id, key, filename, total_size: u64, chunk_size: u32, chunk_count: u32, file_hash: String, chunk_hashes: Vec<String>, encoding: String }`, with a payload-free `Debug`, `validate() -> Result<(), String>`, `chunk_range(i) -> Option<(u64, usize)>` and `chunk_hash(i) -> Option<blake3::Hash>`.
  - Functions: `chunk_count_for(total: u64, chunk_size: u32) -> Option<u32>`, `offer_key_error(&str) -> Option<String>`, `token_key`, `manifest_key`, `chunk_key` (each `(key: &str, id: TransferId) -> String`), and `parse_xfer_key(&str) -> Option<(&str, TransferId, XferLeaf)>`.
  - `XferLeaf { Token, Manifest, Chunk }`.
- `transfer::event`:
  - `Direction { Incoming, Outgoing }`.
  - `FailReason` (see code) with `is_retryable()`.
  - `TransferEvent { Preparing, Announced, Progress, Completed, Saved, Failed }`.
  - `TransferCommand { Offer, Fetch, Cancel, Save }`.
  - `EventSink` and `sink_from(EventTx) -> EventSink`.
- `transfer::limits`: `TransferLimits` (`Default`), `fs_available_space`, `PLAIN_PUBLISH_MAX`, `SPOOL_MAX_AGE`, `PROGRESS_INTERVAL`, `RETRY_BACKOFF`, `PRESENCE_TIMEOUT`, `DISCOVERY_QUERY_TIMEOUT`, `SERVE_CONCURRENCY`.
- `transfer::tasks`: `CancelReason { User, Shutdown }`, `CancelRx`, `TaskHandle::{spawn, cancel, is_finished, abort, direction}`, `TransferTasks::{active, contains, insert, push_background, cancel, set_discovery, shutdown}`, and `dispatch(tasks, session, limits, cmd, sink)`.
- `transfer::registry` (types only): `EntryKind { V2, LegacyV1 }`, `TransferStatus`, `TransferEntry`, `TransferBadge`, and `TransferRegistry` (`Default`) with `apply(&mut self, TransferEvent)`. `apply` is a no-op in T1 and is implemented in T7.
- Lane entry points with their final signatures:
  - `sender::spawn_offer(session: Arc<Session>, id: TransferId, key: String, path: PathBuf, limits: TransferLimits, sink: EventSink) -> TaskHandle`
  - `receiver::spawn_fetch(session: Arc<Session>, manifest: Manifest, limits: TransferLimits, sink: EventSink) -> TaskHandle`
  - `receiver::spawn_discovery(session: Arc<Session>, limits: TransferLimits, sink: EventSink) -> TaskHandle`
  - `receiver::spawn_save(spool_dir: PathBuf, id: TransferId, dest: PathBuf, sink: EventSink) -> TaskHandle`
- `ZenohCommand::Transfer(TransferCommand)`, `ZenohEvent::Transfer(TransferEvent)`.
- New `ZenohExplorer` fields `transfers: TransferRegistry` and `transfer_max_file_gib: u32` (default 16).
- New `WorkerState` fields `transfers: TransferTasks` and `transfer_limits: TransferLimits`.
- `transfer::test_support` (`#[cfg(test)]`): `session_pair(port)`, `wait_for_queryable`, `wait_for_token`, `collecting_sink`, `next_event`, `test_bytes`, `manifest_from_bytes`, `test_limits`.

**Test port table** (each network test uses only its own port):

| Port | Test |
|---|---|
| 27700 | T1 `session_pair_connects` |
| 27701–27704 | T3 sender tests |
| 27711–27720 | T5 fetch tests |
| 27731–27735 | T6 discovery tests |
| 27751–27753 | T12 end-to-end tests |

- [ ] **Step 1: Add the crates.** In `Cargo.toml` `[dependencies]`, add:

```toml
serde = { version = "1", features = ["derive"] }
blake3 = "1.8"
fs4 = "1.1"
tempfile = "3"
rand = "0.8"
```

  - If `egui_kittest` is not already under `[dev-dependencies]` (P3 adds it), add `egui_kittest = "0.36"` there.
  - Run: `cargo update -p blake3 -p fs4 -p tempfile && cargo tree -i rand@0.8.5 --depth 1 && cargo tree -i zenoh --depth 0`
  - Expected: `rand v0.8.5` is used by `zenoh` and `zenoh-explorer`, which proves no new rand version was added, and `zenoh v1.10.1`.
  - If zenoh is not 1.10.1, run `cargo update -p zenoh --precise 1.10.1`.

- [ ] **Step 2: Move the v1 module.**

```bash
mkdir -p src/transfer/sender src/transfer/receiver
git mv src/transfer.rs src/transfer/export.rs
```

- [ ] **Step 3: Write the failing tests for the contract.** Create `src/transfer/manifest.rs` containing only this test module for now:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use zenoh::key_expr::keyexpr;

    fn ke(s: &str) -> &keyexpr {
        keyexpr::new(s).unwrap()
    }

    #[test]
    fn keyexpr_semantics_for_xfer_space() {
        let m = ke("demo/f/@xfer/0123456789abcdef0123456789abcdef/manifest");
        assert!(!ke("**").intersects(m), "** must not see transfer traffic");
        assert!(!ke("demo/**").intersects(m));
        assert!(ke(DISCOVERY_MANIFESTS).intersects(m));
        assert!(!ke(DISCOVERY_MANIFESTS).intersects(ke("demo/f/@xfer/abc/chunk")));
        assert!(ke(DISCOVERY_TOKENS).intersects(ke("demo/f/@xfer/abc")));
        assert!(!ke(DISCOVERY_MANIFESTS).intersects(ke("a/@b/@xfer/abc/manifest")));
    }

    #[test]
    fn transfer_id_round_trips_as_32_hex() {
        let id = TransferId(0xab);
        assert_eq!(id.to_string(), "000000000000000000000000000000ab");
        assert_eq!("000000000000000000000000000000ab".parse::<TransferId>(), Ok(id));
        assert!("AB".parse::<TransferId>().is_err());
        assert!("000000000000000000000000000000AB".parse::<TransferId>().is_err());
        assert_ne!(TransferId::random(), TransferId::random());
    }

    #[test]
    fn keys_build_and_parse() {
        let id = TransferId(7);
        assert_eq!(token_key("a/b", id), format!("a/b/@xfer/{id}"));
        assert_eq!(parse_xfer_key(&manifest_key("a/b", id)), Some(("a/b", id, XferLeaf::Manifest)));
        assert_eq!(parse_xfer_key(&chunk_key("a/b", id)), Some(("a/b", id, XferLeaf::Chunk)));
        assert_eq!(parse_xfer_key(&token_key("a/b", id)), Some(("a/b", id, XferLeaf::Token)));
        assert_eq!(parse_xfer_key("a/b/@xfer/zz/manifest"), None);
        assert_eq!(parse_xfer_key("a/b/@xfer/00000000000000000000000000000007/other"), None);
        assert_eq!(parse_xfer_key("a/b"), None);
    }

    #[test]
    fn chunk_math_handles_empty_exact_and_partial() {
        assert_eq!(chunk_count_for(0, 1024), Some(0));
        assert_eq!(chunk_count_for(2048, 1024), Some(2));
        assert_eq!(chunk_count_for(2049, 1024), Some(3));
        let m = sample_manifest(2049, 1024);
        assert_eq!(m.chunk_range(0), Some((0, 1024)));
        assert_eq!(m.chunk_range(2), Some((2048, 1)));
        assert_eq!(m.chunk_range(3), None);
    }

    #[test]
    fn offer_key_rejects_wildcards_and_verbatim() {
        assert!(offer_key_error("demo/files/big").is_none());
        for bad in ["demo/*", "demo/**", "@/x", "a/@b", "a//b", "a/", ""] {
            assert!(offer_key_error(bad).is_some(), "{bad} must be rejected");
        }
    }

    #[test]
    fn manifest_serde_round_trip_and_validate() {
        let m = sample_manifest(3000, MIN_CHUNK_SIZE);
        let json = serde_json::to_string(&m).unwrap();
        assert!(json.contains(&format!("\"transfer_id\":\"{}\"", m.transfer_id)));
        let back: Manifest = serde_json::from_str(&json).unwrap();
        assert_eq!(back, m);
        assert_eq!(m.validate(), Ok(()));
    }

    #[test]
    fn manifest_validate_rejects_inconsistent_fields() {
        let good = sample_manifest(3000, MIN_CHUNK_SIZE);
        let mut m = good.clone();
        m.version = 1;
        assert!(m.validate().is_err());
        let mut m = good.clone();
        m.chunk_count += 1;
        assert!(m.validate().is_err());
        let mut m = good.clone();
        m.chunk_hashes.push(m.chunk_hashes[0].clone());
        assert!(m.validate().is_err());
        let mut m = good.clone();
        m.filename = "../etc/passwd".into();
        assert!(m.validate().is_err());
        let mut m = good.clone();
        m.key = "demo/*".into();
        assert!(m.validate().is_err());
        let mut m = good.clone();
        m.file_hash = "zz".into();
        assert!(m.validate().is_err());
        let mut m = good;
        m.chunk_size = MIN_CHUNK_SIZE - 1;
        assert!(m.validate().is_err());
    }

    #[test]
    fn manifest_debug_is_short() {
        let m = sample_manifest(u64::from(MIN_CHUNK_SIZE) * 1000, MIN_CHUNK_SIZE);
        assert!(format!("{m:?}").len() < 300);
    }

    fn sample_manifest(total: u64, chunk: u32) -> Manifest {
        let n = chunk_count_for(total, chunk).unwrap();
        let h = blake3::hash(b"x").to_hex().to_string();
        Manifest {
            version: PROTOCOL_VERSION,
            transfer_id: TransferId(42),
            key: "demo/file".into(),
            filename: "file.bin".into(),
            total_size: total,
            chunk_size: chunk,
            chunk_count: n,
            file_hash: h.clone(),
            chunk_hashes: vec![h; n as usize],
            encoding: "application/octet-stream".into(),
        }
    }
}
```

  - `chunk_math_handles_empty_exact_and_partial` builds a manifest with a 1024-byte chunk size. `chunk_range` does not validate, so this is fine even though 1024 is below `MIN_CHUNK_SIZE`.

- [ ] **Step 4: Implement `manifest.rs`** above the tests:

```rust
//! Transfer protocol v2: manifest, transfer ID, key space and chunk math.
//! See src/transfer/README.md for the protocol description.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

pub const PROTOCOL_VERSION: u32 = 2;
/// Verbatim key chunk: wildcards never match it (key-expression RFC).
pub const XFER_CHUNK: &str = "@xfer";
/// Selector parameter carrying the chunk index.
pub const CHUNK_PARAM: &str = "i";
/// Error reply payload when the offered file changed on disk.
pub const ERR_SOURCE_CHANGED: &str = "source-changed";
pub const DISCOVERY_MANIFESTS: &str = "**/@xfer/*/manifest";
pub const DISCOVERY_TOKENS: &str = "**/@xfer/*";
pub const MIN_CHUNK_SIZE: u32 = 64 * 1024;
pub const MAX_CHUNK_SIZE: u32 = 64 * 1024 * 1024;
pub const DEFAULT_CHUNK_SIZE: u32 = 4 * 1024 * 1024;
/// Bounds the manifest to about 1.1 MB of chunk hashes.
pub const MAX_CHUNKS: u32 = 16_384;
/// Receivers refuse to parse larger manifests.
pub const MAX_MANIFEST_BYTES: usize = 2 * 1024 * 1024;

/// Random 128-bit transfer identifier, written as 32 lowercase hex chars.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct TransferId(pub u128);

impl TransferId {
    pub fn random() -> Self {
        Self(rand::random::<u128>())
    }
}

impl fmt::Display for TransferId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:032x}", self.0)
    }
}

impl FromStr for TransferId {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        if s.len() != 32 || !s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
            return Err(format!("invalid transfer id '{s}'"));
        }
        u128::from_str_radix(s, 16).map(Self).map_err(|e| e.to_string())
    }
}

impl TryFrom<String> for TransferId {
    type Error = String;
    fn try_from(s: String) -> Result<Self, Self::Error> {
        s.parse()
    }
}

impl From<TransferId> for String {
    fn from(id: TransferId) -> String {
        id.to_string()
    }
}

/// What a transfer offer promises. Published as JSON on `manifest_key`.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32,
    pub transfer_id: TransferId,
    pub key: String,
    pub filename: String,
    pub total_size: u64,
    pub chunk_size: u32,
    pub chunk_count: u32,
    pub file_hash: String,
    pub chunk_hashes: Vec<String>,
    pub encoding: String,
}

/// Summary only: chunk_hashes can be ~1 MB.
impl fmt::Debug for Manifest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Manifest")
            .field("transfer_id", &self.transfer_id.to_string())
            .field("key", &self.key)
            .field("filename", &self.filename)
            .field("total_size", &self.total_size)
            .field("chunk_size", &self.chunk_size)
            .field("chunk_count", &self.chunk_count)
            .finish_non_exhaustive()
    }
}

impl Manifest {
    /// Reject anything a hostile or buggy sender could use to drive bad
    /// allocations, path traversal or undiscoverable keys.
    pub fn validate(&self) -> Result<(), String> {
        if self.version != PROTOCOL_VERSION {
            return Err(format!("unsupported transfer protocol version {}", self.version));
        }
        if let Some(e) = offer_key_error(&self.key) {
            return Err(e);
        }
        if crate::transfer::sanitize_filename(&self.filename).as_deref() != Some(self.filename.as_str()) {
            return Err("filename is not a plain file name".into());
        }
        if !(MIN_CHUNK_SIZE..=MAX_CHUNK_SIZE).contains(&self.chunk_size) {
            return Err(format!("chunk size {} out of range", self.chunk_size));
        }
        let expected = chunk_count_for(self.total_size, self.chunk_size)
            .ok_or("file too large for its chunk size")?;
        if self.chunk_count != expected || self.chunk_count > MAX_CHUNKS {
            return Err(format!("chunk count {} invalid (expected {expected}, max {MAX_CHUNKS})", self.chunk_count));
        }
        if self.chunk_hashes.len() != self.chunk_count as usize {
            return Err("chunk hash list length does not match chunk count".into());
        }
        let bad_hash = |h: &String| blake3::Hash::from_hex(h).is_err();
        if bad_hash(&self.file_hash) || self.chunk_hashes.iter().any(bad_hash) {
            return Err("malformed BLAKE3 hash".into());
        }
        Ok(())
    }

    /// Byte offset and length of chunk `i`, or None when out of range.
    pub fn chunk_range(&self, i: u32) -> Option<(u64, usize)> {
        if i >= self.chunk_count {
            return None;
        }
        let off = u64::from(i) * u64::from(self.chunk_size);
        let len = (self.total_size - off).min(u64::from(self.chunk_size));
        Some((off, len as usize))
    }

    pub fn chunk_hash(&self, i: u32) -> Option<blake3::Hash> {
        blake3::Hash::from_hex(self.chunk_hashes.get(i as usize)?).ok()
    }
}

pub fn chunk_count_for(total: u64, chunk_size: u32) -> Option<u32> {
    if chunk_size == 0 {
        return None;
    }
    u32::try_from(total.div_ceil(u64::from(chunk_size))).ok()
}

/// Why `key` cannot carry a transfer, or None when it can. Wildcards and
/// `@` chunks are refused because discovery (`**/@xfer/...`) cannot see them.
pub fn offer_key_error(key: &str) -> Option<String> {
    let ke = match zenoh::key_expr::keyexpr::new(key) {
        Ok(k) => k,
        Err(e) => return Some(format!("invalid key expression: {e}")),
    };
    if ke.is_wild() {
        return Some("transfer key must not contain wildcards".into());
    }
    if ke.as_str().split('/').any(|c| c.starts_with('@')) {
        return Some("transfer key must not contain '@' chunks (reserved)".into());
    }
    None
}

pub fn token_key(key: &str, id: TransferId) -> String {
    format!("{key}/{XFER_CHUNK}/{id}")
}
pub fn manifest_key(key: &str, id: TransferId) -> String {
    format!("{key}/{XFER_CHUNK}/{id}/manifest")
}
pub fn chunk_key(key: &str, id: TransferId) -> String {
    format!("{key}/{XFER_CHUNK}/{id}/chunk")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum XferLeaf {
    Token,
    Manifest,
    Chunk,
}

/// Split `{key}/@xfer/{id}[/manifest|/chunk]` into its parts.
pub fn parse_xfer_key(k: &str) -> Option<(&str, TransferId, XferLeaf)> {
    let (topic, rest) = k.split_once("/@xfer/")?;
    if topic.is_empty() {
        return None;
    }
    let mut parts = rest.split('/');
    let id: TransferId = parts.next()?.parse().ok()?;
    let leaf = match (parts.next(), parts.next()) {
        (None, _) => XferLeaf::Token,
        (Some("manifest"), None) => XferLeaf::Manifest,
        (Some("chunk"), None) => XferLeaf::Chunk,
        _ => return None,
    };
    Some((topic, id, leaf))
}
```

  - `keyexpr::new`, `intersects` and `is_wild` are in `zenoh-keyexpr-1.10.1/src/key_expr/borrowed.rs:59,82,133` and are re-exported as `zenoh::key_expr::keyexpr` (https://docs.rs/zenoh/1.10.1/zenoh/key_expr/struct.keyexpr.html).

- [ ] **Step 5: Create `src/transfer/limits.rs`.**

```rust
//! Tunables for transfer v2. Defaults are justified in the P4 plan.

use std::path::{Path, PathBuf};
use std::time::Duration;

use super::manifest::DEFAULT_CHUNK_SIZE;

/// Plain (non-transfer) publishes above this are refused; use a transfer.
pub const PLAIN_PUBLISH_MAX: usize = 64 * 1024 * 1024;
pub const DEFAULT_MAX_FILE_SIZE: u64 = 16 * 1024 * 1024 * 1024;
pub const SPOOL_MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);
pub const PROGRESS_INTERVAL: Duration = Duration::from_millis(100);
pub const RETRY_BACKOFF: Duration = Duration::from_millis(100);
pub const PRESENCE_TIMEOUT: Duration = Duration::from_secs(3);
pub const DISCOVERY_QUERY_TIMEOUT: Duration = Duration::from_secs(5);
/// Chunk requests served at once by one offer.
pub const SERVE_CONCURRENCY: usize = 8;

#[derive(Debug, Clone)]
pub struct TransferLimits {
    pub chunk_size: u32,
    pub max_file_size: u64,
    pub max_active_fetches: usize,
    pub max_active_offers: usize,
    pub in_flight: usize,
    pub request_timeout: Duration,
    pub max_retries: u32,
    pub inactivity_timeout: Duration,
    pub offer_idle_timeout: Duration,
    pub spool_dir: PathBuf,
    pub disk_margin: u64,
    /// Injected so tests can simulate a full disk.
    pub available_space: fn(&Path) -> std::io::Result<u64>,
}

pub fn fs_available_space(p: &Path) -> std::io::Result<u64> {
    fs4::available_space(p)
}

impl Default for TransferLimits {
    fn default() -> Self {
        Self {
            chunk_size: DEFAULT_CHUNK_SIZE,
            max_file_size: DEFAULT_MAX_FILE_SIZE,
            max_active_fetches: 4,
            max_active_offers: 4,
            in_flight: 4,
            request_timeout: Duration::from_secs(30),
            max_retries: 5,
            inactivity_timeout: Duration::from_secs(120),
            offer_idle_timeout: Duration::from_secs(30 * 60),
            spool_dir: std::env::temp_dir().join("zenoh-explorer-transfers"),
            disk_margin: 64 * 1024 * 1024,
            available_space: fs_available_space,
        }
    }
}
```

- [ ] **Step 6: Create `src/transfer/event.rs`.**

```rust
//! Transfer events (worker → UI) and commands (UI → worker).

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;

use super::manifest::{Manifest, TransferId};
use crate::types::ZenohEvent;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Direction {
    Incoming,
    Outgoing,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FailReason {
    NotConnected,
    TooManyTransfers { limit: usize },
    TooLarge { size: u64, max: u64 },
    InsufficientSpace { need: u64, available: u64 },
    InvalidOffer(String),
    SourceChanged,
    SenderGone,
    Cancelled { by_sender: bool },
    OfferExpired,
    Timeout { index: u32 },
    ChunkHashMismatch { index: u32 },
    FileHashMismatch,
    Stalled,
    Io(String),
    Zenoh(String),
}

impl FailReason {
    /// Whether "Fetch" again (which resumes) can reasonably succeed.
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::NotConnected
                | Self::TooManyTransfers { .. }
                | Self::InsufficientSpace { .. }
                | Self::Cancelled { by_sender: false }
                | Self::Timeout { .. }
                | Self::ChunkHashMismatch { .. }
                | Self::FileHashMismatch
                | Self::Stalled
                | Self::Io(_)
                | Self::Zenoh(_)
        )
    }
}

impl fmt::Display for FailReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotConnected => write!(f, "not connected"),
            Self::TooManyTransfers { limit } => write!(f, "too many active transfers (limit {limit})"),
            Self::TooLarge { size, max } => write!(f, "file is {size} bytes, over the {max}-byte limit"),
            Self::InsufficientSpace { need, available } => {
                write!(f, "not enough disk space: need {need} bytes, {available} available")
            }
            Self::InvalidOffer(e) => write!(f, "invalid offer: {e}"),
            Self::SourceChanged => write!(f, "the sender's file changed on disk"),
            Self::SenderGone => write!(f, "sender is gone"),
            Self::Cancelled { by_sender: true } => write!(f, "withdrawn by the sender"),
            Self::Cancelled { by_sender: false } => write!(f, "cancelled"),
            Self::OfferExpired => write!(f, "offer expired (no requests)"),
            Self::Timeout { index } => write!(f, "chunk {index} timed out"),
            Self::ChunkHashMismatch { index } => write!(f, "chunk {index} failed verification"),
            Self::FileHashMismatch => write!(f, "file failed BLAKE3 verification"),
            Self::Stalled => write!(f, "no progress for too long"),
            Self::Io(e) => write!(f, "I/O error: {e}"),
            Self::Zenoh(e) => write!(f, "zenoh error: {e}"),
        }
    }
}

#[derive(Debug, Clone)]
pub enum TransferEvent {
    /// Outgoing offer is hashing its file.
    Preparing { id: TransferId, key: String, filename: String, total_size: u64 },
    /// Outgoing: offer is live. Incoming: discovery saw a valid manifest.
    Announced { manifest: Box<Manifest>, direction: Direction },
    /// Incoming: verified chunks. Outgoing: chunk requests served.
    Progress { id: TransferId, chunks: u64 },
    /// Incoming: every chunk and the whole file verified in the spool.
    Completed { id: TransferId },
    Saved { id: TransferId, path: PathBuf },
    Failed { id: TransferId, reason: FailReason },
}

#[derive(Debug)]
pub enum TransferCommand {
    Offer { key: String, path: PathBuf },
    Fetch { manifest: Box<Manifest>, max_file_size: u64 },
    Cancel { id: TransferId },
    Save { id: TransferId, dest: PathBuf },
}

pub type EventSink = Arc<dyn Fn(TransferEvent) + Send + Sync>;

/// Production sink: Progress is droppable under back-pressure; every other
/// event is delivered.
pub fn sink_from(tx: crate::worker::pipeline::EventTx) -> EventSink {
    Arc::new(move |ev: TransferEvent| {
        let droppable = matches!(ev, TransferEvent::Progress { .. });
        let wrapped = ZenohEvent::Transfer(ev);
        if droppable {
            let _ = tx.try_send(wrapped);
        } else {
            let _ = tx.send(wrapped);
        }
    })
}
```

  - `EventTx` is `SyncSender` after P1 T11, so `try_send` exists. Check with `grep -n 'type EventTx' src/worker/pipeline.rs`, which is expected to show `SyncSender`.

- [ ] **Step 7: Create `src/transfer/tasks.rs`** with its tests first (TDD). The tests go at the bottom of the file:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::test_support::{collecting_sink, next_event};

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancel_reaches_task() {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let h = TaskHandle::spawn(Direction::Incoming, move |mut c| async move {
            let _ = c.changed().await;
            let r = *c.borrow();
            let _ = tx.send(r);
        });
        h.cancel(CancelReason::User);
        assert_eq!(rx.await.unwrap(), Some(CancelReason::User));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn active_counts_running_tasks_by_direction() {
        let mut t = TransferTasks::default();
        let forever = |d| TaskHandle::spawn(d, |mut c| async move { let _ = c.changed().await; });
        t.insert(TransferId(1), forever(Direction::Incoming));
        t.insert(TransferId(2), forever(Direction::Outgoing));
        t.insert(TransferId(3), TaskHandle::spawn(Direction::Incoming, |_c| async {}));
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert_eq!(t.active(Direction::Incoming), 1);
        assert_eq!(t.active(Direction::Outgoing), 1);
        assert!(t.cancel(TransferId(1), CancelReason::User));
        assert!(!t.cancel(TransferId(99), CancelReason::User));
        t.shutdown();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert_eq!(t.active(Direction::Outgoing), 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn dispatch_without_session_reports_not_connected() {
        let mut t = TransferTasks::default();
        let (sink, mut rx) = collecting_sink();
        let limits = TransferLimits::default();
        dispatch(&mut t, None, &limits, TransferCommand::Offer { key: "a/b".into(), path: "/tmp/x.bin".into() }, sink);
        assert!(matches!(next_event(&mut rx, 2, |_| true).await, TransferEvent::Preparing { .. }));
        assert!(matches!(
            next_event(&mut rx, 2, |_| true).await,
            TransferEvent::Failed { reason: FailReason::NotConnected, .. }
        ));
    }
}
```

  Then implement above the tests:

```rust
//! Worker-side bookkeeping of running transfer tasks and command dispatch.

use std::collections::HashMap;
use std::future::Future;
use std::path::Path;
use std::sync::Arc;

use tokio::sync::watch;
use zenoh::Session;

use super::event::{Direction, EventSink, FailReason, TransferCommand, TransferEvent};
use super::limits::TransferLimits;
use super::manifest::TransferId;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancelReason {
    /// The user asked: clean up (receiver deletes its spool; sender withdraws).
    User,
    /// Disconnect or app exit: keep the spool so the transfer can resume.
    Shutdown,
}

pub type CancelRx = watch::Receiver<Option<CancelReason>>;

pub struct TaskHandle {
    direction: Direction,
    cancel: watch::Sender<Option<CancelReason>>,
    join: tokio::task::JoinHandle<()>,
}

impl TaskHandle {
    pub fn spawn<F, Fut>(direction: Direction, f: F) -> Self
    where
        F: FnOnce(CancelRx) -> Fut,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let (cancel, rx) = watch::channel(None);
        Self { direction, cancel, join: tokio::spawn(f(rx)) }
    }
    pub fn cancel(&self, reason: CancelReason) {
        let _ = self.cancel.send(Some(reason));
    }
    pub fn is_finished(&self) -> bool {
        self.join.is_finished()
    }
    pub fn abort(&self) {
        self.join.abort();
    }
    pub fn direction(&self) -> Direction {
        self.direction
    }
}

#[derive(Default)]
pub struct TransferTasks {
    by_id: HashMap<TransferId, TaskHandle>,
    background: Vec<TaskHandle>,
    discovery: Option<TaskHandle>,
}

impl TransferTasks {
    fn reap(&mut self) {
        self.by_id.retain(|_, h| !h.is_finished());
        self.background.retain(|h| !h.is_finished());
    }
    pub fn active(&mut self, d: Direction) -> usize {
        self.reap();
        self.by_id.values().filter(|h| h.direction() == d).count()
    }
    pub fn contains(&mut self, id: TransferId) -> bool {
        self.reap();
        self.by_id.contains_key(&id)
    }
    pub fn insert(&mut self, id: TransferId, h: TaskHandle) {
        self.reap();
        if let Some(old) = self.by_id.insert(id, h) {
            old.cancel(CancelReason::Shutdown);
            old.abort();
        }
    }
    pub fn push_background(&mut self, h: TaskHandle) {
        self.reap();
        self.background.push(h);
    }
    pub fn cancel(&mut self, id: TransferId, reason: CancelReason) -> bool {
        self.reap();
        match self.by_id.get(&id) {
            Some(h) => {
                h.cancel(reason);
                true
            }
            None => false,
        }
    }
    pub fn set_discovery(&mut self, h: TaskHandle) {
        if let Some(old) = self.discovery.replace(h) {
            old.cancel(CancelReason::Shutdown);
            old.abort();
        }
    }
    /// Stop everything. Idempotent. Spools are kept (resume).
    pub fn shutdown(&mut self) {
        let all = self
            .by_id
            .drain()
            .map(|(_, h)| h)
            .chain(self.background.drain(..))
            .chain(self.discovery.take());
        for h in all {
            h.cancel(CancelReason::Shutdown);
            h.abort();
        }
    }
}

fn display_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "file".into())
}

/// Route one UI transfer command. Admission limits live here; size and disk
/// checks live in the receiver.
pub fn dispatch(
    tasks: &mut TransferTasks,
    session: Option<Arc<Session>>,
    limits: &TransferLimits,
    cmd: TransferCommand,
    sink: EventSink,
) {
    match cmd {
        TransferCommand::Offer { key, path } => {
            let id = TransferId::random();
            let reject = |reason: FailReason| {
                sink(TransferEvent::Preparing { id, key: key.clone(), filename: display_name(&path), total_size: 0 });
                sink(TransferEvent::Failed { id, reason });
            };
            let Some(s) = session else { return reject(FailReason::NotConnected) };
            if tasks.active(Direction::Outgoing) >= limits.max_active_offers {
                return reject(FailReason::TooManyTransfers { limit: limits.max_active_offers });
            }
            let h = super::sender::spawn_offer(s, id, key.clone(), path.clone(), limits.clone(), sink.clone());
            tasks.insert(id, h);
        }
        TransferCommand::Fetch { manifest, max_file_size } => {
            let id = manifest.transfer_id;
            let Some(s) = session else {
                return sink(TransferEvent::Failed { id, reason: FailReason::NotConnected });
            };
            if tasks.contains(id) {
                return;
            }
            if tasks.active(Direction::Incoming) >= limits.max_active_fetches {
                return sink(TransferEvent::Failed {
                    id,
                    reason: FailReason::TooManyTransfers { limit: limits.max_active_fetches },
                });
            }
            let l = TransferLimits { max_file_size, ..limits.clone() };
            tasks.insert(id, super::receiver::spawn_fetch(s, *manifest, l, sink));
        }
        TransferCommand::Cancel { id } => {
            tasks.cancel(id, CancelReason::User);
        }
        TransferCommand::Save { id, dest } => {
            tasks.push_background(super::receiver::spawn_save(limits.spool_dir.clone(), id, dest, sink));
        }
    }
}
```

- [ ] **Step 8: Create `src/transfer/registry.rs`** (types only; T7 implements the behaviour):

```rust
//! UI-side single source of truth for transfer state. Updated only from
//! `TransferEvent`s and explicit UI actions.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use super::event::{Direction, FailReason, TransferEvent};
use super::manifest::{Manifest, TransferId};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    V2,
    /// v1 `__chunk` reception (only if Q1 keeps it; see T10).
    LegacyV1,
}

#[derive(Debug, Clone, PartialEq)]
pub enum TransferStatus {
    Preparing,
    Offering { served: u64 },
    Available,
    Fetching { done: u32 },
    Verified,
    Saved { path: PathBuf },
    Failed(FailReason),
}

#[derive(Debug, Clone)]
pub struct TransferEntry {
    pub id: TransferId,
    pub kind: EntryKind,
    pub direction: Direction,
    pub key: String,
    pub filename: String,
    pub total_size: u64,
    pub chunk_count: u32,
    /// Shared so per-frame clones are cheap; None while Preparing and for v1.
    pub manifest: Option<Arc<Manifest>>,
    pub status: TransferStatus,
    /// Non-fatal error (e.g. a failed Save) shown under the status.
    pub last_error: Option<String>,
    pub updated: Instant,
}

/// Small per-row summary for the topic tree.
#[derive(Debug, Clone, PartialEq)]
pub struct TransferBadge {
    pub direction: Direction,
    pub fraction: Option<f32>,
    pub label: String,
}

#[derive(Debug, Default)]
pub struct TransferRegistry {
    entries: HashMap<TransferId, TransferEntry>,
}

impl TransferRegistry {
    /// Apply a worker event. The state machine is implemented by P4 T7.
    pub fn apply(&mut self, ev: TransferEvent) {
        let _ = ev;
    }
}
```

- [ ] **Step 9: Create the lane files with their final signatures.**
  - `src/transfer/sender/mod.rs`:

```rust
//! Offering side of transfer v2.
pub mod prepare;
pub mod serve;
pub use serve::spawn_offer;
```

  - `src/transfer/sender/prepare.rs`: `//! Offer preparation: hash a file into a Manifest and read chunks back.`
  - `src/transfer/sender/serve.rs`:

```rust
//! Serving an offer: liveliness token, manifest and chunk queryables.

use std::path::PathBuf;
use std::sync::Arc;

use zenoh::Session;

use crate::transfer::event::{Direction, EventSink, FailReason, TransferEvent};
use crate::transfer::limits::TransferLimits;
use crate::transfer::manifest::TransferId;
use crate::transfer::tasks::TaskHandle;

/// Offer the file at `path` under `key`. Wave 0 stub: reports failure.
pub fn spawn_offer(
    session: Arc<Session>,
    id: TransferId,
    key: String,
    path: PathBuf,
    limits: TransferLimits,
    sink: EventSink,
) -> TaskHandle {
    let _ = (session, key, path, limits);
    TaskHandle::spawn(Direction::Outgoing, move |_c| async move {
        sink(TransferEvent::Failed { id, reason: FailReason::Zenoh("offer engine not built yet".into()) });
    })
}
```

  - `src/transfer/receiver/mod.rs`:

```rust
//! Receiving side of transfer v2.
pub mod discovery;
pub mod fetch;
pub mod spool;
pub use discovery::spawn_discovery;
pub use fetch::spawn_fetch;
pub use spool::spawn_save;
```

  - `src/transfer/receiver/fetch.rs`: the same stub pattern as `serve.rs`, with the signature `pub fn spawn_fetch(session: Arc<Session>, manifest: Manifest, limits: TransferLimits, sink: EventSink) -> TaskHandle`. It emits `Failed { id: manifest.transfer_id, reason: FailReason::Zenoh("fetch engine not built yet".into()) }`.
  - `src/transfer/receiver/spool.rs`: `pub fn spawn_save(spool_dir: PathBuf, id: TransferId, dest: PathBuf, sink: EventSink) -> TaskHandle` emits `Failed { id, reason: FailReason::Io("save not built yet".into()) }`.
  - `src/transfer/receiver/discovery.rs`: `pub fn spawn_discovery(session: Arc<Session>, limits: TransferLimits, sink: EventSink) -> TaskHandle` returns `TaskHandle::spawn(Direction::Incoming, |_c| async {})` (a no-op) after `let _ = (session, limits, sink);`.
  - `src/transfer/loopback_tests.rs`: `//! End-to-end loopback tests for transfer v2 (real sender, discovery, fetcher).`

- [ ] **Step 10: Create `src/transfer/test_support.rs`.**

```rust
//! Test helpers shared by transfer tests.

use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::mpsc::UnboundedReceiver;
use zenoh::Session;

use super::event::{EventSink, TransferEvent};
use super::limits::TransferLimits;
use super::manifest::{chunk_count_for, Manifest, TransferId, PROTOCOL_VERSION};

/// Session A listens on 127.0.0.1:`port`; B connects to it. Multicast off.
pub(crate) async fn session_pair(port: u16) -> (Arc<Session>, Arc<Session>) {
    let ep = format!(r#"["tcp/127.0.0.1:{port}"]"#);
    let mut ca = zenoh::Config::default();
    ca.insert_json5("scouting/multicast/enabled", "false").unwrap();
    ca.insert_json5("listen/endpoints", &ep).unwrap();
    let a = zenoh::open(ca).await.unwrap();
    let mut cb = zenoh::Config::default();
    cb.insert_json5("scouting/multicast/enabled", "false").unwrap();
    cb.insert_json5("listen/endpoints", "[]").unwrap();
    cb.insert_json5("connect/endpoints", &ep).unwrap();
    let b = zenoh::open(cb).await.unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while b.info().peers_zid().await.count() == 0 {
        assert!(Instant::now() < deadline, "session B never connected to A on port {port}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    (Arc::new(a), Arc::new(b))
}

/// Wait until `s` can route queries on `key` to a queryable.
pub(crate) async fn wait_for_queryable(s: &Session, key: &str) {
    let q = s.declare_querier(key.to_string()).await.unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !q.matching_status().await.unwrap().matching() {
        assert!(Instant::now() < deadline, "no queryable for {key}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// Wait until `s` sees the liveliness token `key`.
pub(crate) async fn wait_for_token(s: &Session, key: &str) {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let replies = s.liveliness().get(key.to_string()).await.unwrap();
        if matches!(replies.recv_async().await, Ok(r) if r.result().is_ok()) {
            return;
        }
        assert!(Instant::now() < deadline, "token {key} never visible");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

pub(crate) fn collecting_sink() -> (EventSink, UnboundedReceiver<TransferEvent>) {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
    (Arc::new(move |ev| { let _ = tx.send(ev); }), rx)
}

/// Next event matching `pred` within `secs`, skipping the rest.
pub(crate) async fn next_event(
    rx: &mut UnboundedReceiver<TransferEvent>,
    secs: u64,
    pred: impl Fn(&TransferEvent) -> bool,
) -> TransferEvent {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
    loop {
        match tokio::time::timeout_at(deadline, rx.recv()).await {
            Ok(Some(ev)) if pred(&ev) => return ev,
            Ok(Some(_)) => continue,
            Ok(None) => panic!("event sink closed"),
            Err(_) => panic!("no matching transfer event within {secs}s"),
        }
    }
}

/// Deterministic, non-repeating-per-chunk test data.
pub(crate) fn test_bytes(len: usize, seed: u8) -> Vec<u8> {
    (0..len).map(|i| ((i.wrapping_mul(31) ^ (i >> 11)) as u8).wrapping_add(seed)).collect()
}

pub(crate) fn manifest_from_bytes(key: &str, filename: &str, bytes: &[u8], chunk_size: u32) -> Manifest {
    let n = chunk_count_for(bytes.len() as u64, chunk_size).unwrap();
    Manifest {
        version: PROTOCOL_VERSION,
        transfer_id: TransferId::random(),
        key: key.into(),
        filename: filename.into(),
        total_size: bytes.len() as u64,
        chunk_size,
        chunk_count: n,
        file_hash: blake3::hash(bytes).to_hex().to_string(),
        chunk_hashes: bytes.chunks(chunk_size as usize).map(|c| blake3::hash(c).to_hex().to_string()).collect(),
        encoding: "application/octet-stream".into(),
    }
}

pub(crate) fn test_limits(spool: &Path) -> TransferLimits {
    TransferLimits {
        chunk_size: 64 * 1024,
        request_timeout: Duration::from_secs(2),
        inactivity_timeout: Duration::from_secs(10),
        max_retries: 3,
        spool_dir: spool.to_path_buf(),
        available_space: |_| Ok(u64::MAX),
        ..TransferLimits::default()
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opens network sessions"]
async fn session_pair_connects() {
    let (a, b) = session_pair(27700).await;
    assert!(a.info().peers_zid().await.count() >= 1);
    drop(b);
}
```

  - APIs used, all in zenoh 1.10.1:
    - `Config::insert_json5` is the same API P1 T3 uses.
    - `Session::declare_querier` and `Querier::matching_status() -> MatchingStatus::matching()` are in `src/api/querier.rs:228` and `src/api/matching.rs:83`.
    - `Session::liveliness().get()` is in `src/api/liveliness.rs:193`.
  - `test_bytes` must not repeat per chunk, so that a chunk served for the wrong index fails its hash. With 64 KiB chunks, `i >> 11` changes across chunks.

- [ ] **Step 11: Create `src/transfer/mod.rs`.**

```rust
//! File transfer. `export` holds the plain-value export helpers (and, until
//! T10 decides, the v1 `__chunk` code); everything else is protocol v2.
//! See README.md in this directory.
#![allow(dead_code)] // Wave 0 scaffolding; removed in P4 T13 once every item is wired.

pub mod event;
pub mod export;
pub mod limits;
pub mod manifest;
pub mod receiver;
pub mod registry;
pub mod sender;
pub mod tasks;

#[cfg(test)]
mod loopback_tests;
#[cfg(test)]
pub(crate) mod test_support;

pub use export::*;
```

  - `pub use export::*` keeps every existing `crate::transfer::X` path working (`format_size`, `sanitize_filename`, `CHUNK_SIZE`, …).

- [ ] **Step 12: Wire the command and event variants.**
  - In `src/types/commands.rs`, add to `ZenohCommand`: `/// Transfer v2 commands (see transfer::event).` and `Transfer(crate::transfer::event::TransferCommand),`.
  - Add to `ZenohEvent`: `/// Transfer v2 state changes; bypass sample ingest entirely.` and `Transfer(crate::transfer::event::TransferEvent),`.
  - In the manual `impl Debug for ZenohCommand` (P1 T3), add the arm `ZenohCommand::Transfer(c) => write!(f, "Transfer({c:?})"),`. It stays payload-free because `Manifest`'s `Debug` is a summary.

- [ ] **Step 13: Wire the worker.**
  - `src/worker/state.rs`: add these fields to `WorkerState`:

```rust
    pub(crate) transfers: crate::transfer::tasks::TransferTasks,
    pub(crate) transfer_limits: crate::transfer::limits::TransferLimits,
```

    Make the first line of `teardown()` `self.transfers.shutdown();`.
  - `src/worker/mod.rs`: in the dispatch `match command`, add:

```rust
ZenohCommand::Transfer(cmd) => crate::transfer::tasks::dispatch(
    &mut st.transfers,
    st.publishing_session.clone(),
    &st.transfer_limits,
    cmd,
    crate::transfer::event::sink_from(ctx.event_sender.clone()),
),
```

  - `src/worker/session.rs` `handle_connect`: directly after `st.publishing_session = Some(session_arc.clone());` (pre-P1 `zenoh_worker.rs:124`), add:

```rust
st.transfers.set_discovery(crate::transfer::receiver::spawn_discovery(
    session_arc.clone(),
    st.transfer_limits.clone(),
    crate::transfer::event::sink_from(ctx.event_sender.clone()),
));
```

- [ ] **Step 14: Wire the app.**
  - `src/app/mod.rs`: add the fields `pub(crate) transfers: crate::transfer::registry::TransferRegistry` and `pub(crate) transfer_max_file_gib: u32`. Initialise them in `new()` to `Default::default()` and `16`.
  - `src/events/mod.rs` `process_events`: add the arm `ZenohEvent::Transfer(ev) => self.transfers.apply(ev),`.

- [ ] **Step 15: Run the tests.**
  - Run: `cargo test transfer:: && cargo test -- --ignored session_pair_connects`
  - Expected: PASS. That covers 8 manifest tests and 3 tasks tests, plus the ignored pair test.
  - Then run: `cargo build && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check && cargo audit`
  - Expected: clean, and the audit shows 0 unignored vulnerabilities.
  - If `ke.is_wild()` does not resolve, use `ke.as_str().contains('*')`. `is_wild` is present at `zenoh-keyexpr-1.10.1/src/key_expr/borrowed.rs:133`.

- [ ] **Step 16: Commit.**

```bash
git add Cargo.toml Cargo.lock src/transfer src/types/commands.rs src/worker/mod.rs src/worker/state.rs \
  src/worker/session.rs src/events/mod.rs src/app/mod.rs
git commit -m "feat(transfer): v2 protocol contract, module skeleton and wiring

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T2: Offer preparation (Lane A)

**Owns:** `src/transfer/sender/prepare.rs`

**Interfaces:**
- Consumes `Manifest`, `chunk_count_for`, `offer_key_error`, `MIN_CHUNK_SIZE`, `MAX_CHUNK_SIZE`, `MAX_CHUNKS`, `PROTOCOL_VERSION`, `TransferId`, `FailReason` and `crate::transfer::sanitize_filename`.
- Produces:
  - `pub struct PreparedFile { pub manifest: Manifest, pub path: PathBuf, len: u64, modified: Option<SystemTime> }`
  - `pub fn prepare(path: &Path, key: &str, id: TransferId, chunk_size: u32, max_file_size: u64) -> Result<PreparedFile, FailReason>`
  - `impl PreparedFile { pub fn read_chunk(&self, index: u32) -> Result<Vec<u8>, FailReason> }`

- [ ] **Step 1: Write the failing tests** at the bottom of `prepare.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::manifest::MIN_CHUNK_SIZE;
    use crate::transfer::test_support::test_bytes;

    fn file_with(dir: &tempfile::TempDir, name: &str, bytes: &[u8]) -> PathBuf {
        let p = dir.path().join(name);
        std::fs::write(&p, bytes).unwrap();
        p
    }

    #[test]
    fn prepare_hashes_every_chunk() {
        let dir = tempfile::tempdir().unwrap();
        let cs = MIN_CHUNK_SIZE;
        let data = test_bytes(2 * cs as usize + 1, 1);
        let p = file_with(&dir, "a b.bin", &data);
        let pf = prepare(&p, "demo/f", TransferId(9), cs, u64::MAX).unwrap();
        let m = &pf.manifest;
        assert_eq!((m.chunk_count, m.total_size, m.filename.as_str()), (3, data.len() as u64, "a b.bin"));
        for (i, c) in data.chunks(cs as usize).enumerate() {
            assert_eq!(m.chunk_hashes[i], blake3::hash(c).to_hex().to_string());
        }
        assert_eq!(m.file_hash, blake3::hash(&data).to_hex().to_string());
        assert_eq!(m.validate(), Ok(()));
    }

    #[test]
    fn prepare_empty_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = file_with(&dir, "empty.txt", b"");
        let m = prepare(&p, "demo/e", TransferId(1), MIN_CHUNK_SIZE, u64::MAX).unwrap().manifest;
        assert_eq!((m.chunk_count, m.total_size), (0, 0));
        assert_eq!(m.file_hash, blake3::hash(b"").to_hex().to_string());
    }

    #[test]
    fn prepare_rejects_bad_inputs() {
        let dir = tempfile::tempdir().unwrap();
        let p = file_with(&dir, "x.bin", &[0u8; 100]);
        assert!(matches!(prepare(&p, "demo/f", TransferId(1), MIN_CHUNK_SIZE, 10), Err(FailReason::TooLarge { .. })));
        assert!(matches!(prepare(&p, "demo/*", TransferId(1), MIN_CHUNK_SIZE, u64::MAX), Err(FailReason::InvalidOffer(_))));
        assert!(matches!(prepare(dir.path(), "demo/f", TransferId(1), MIN_CHUNK_SIZE, u64::MAX), Err(FailReason::InvalidOffer(_))));
        assert!(matches!(prepare(&p, "demo/f", TransferId(1), 1024, u64::MAX), Err(FailReason::InvalidOffer(_))));
    }

    #[test]
    fn read_chunk_returns_exact_bytes_and_short_last_chunk() {
        let dir = tempfile::tempdir().unwrap();
        let cs = MIN_CHUNK_SIZE as usize;
        let data = test_bytes(cs + 10, 2);
        let pf = prepare(&file_with(&dir, "r.bin", &data), "demo/r", TransferId(2), cs as u32, u64::MAX).unwrap();
        assert_eq!(pf.read_chunk(0).unwrap(), &data[..cs]);
        assert_eq!(pf.read_chunk(1).unwrap(), &data[cs..]);
    }

    #[test]
    fn read_chunk_out_of_range() {
        let dir = tempfile::tempdir().unwrap();
        let pf = prepare(&file_with(&dir, "o.bin", b"abc"), "demo/o", TransferId(3), MIN_CHUNK_SIZE, u64::MAX).unwrap();
        assert!(matches!(pf.read_chunk(1), Err(FailReason::InvalidOffer(_))));
    }

    #[test]
    fn read_chunk_detects_source_change() {
        let dir = tempfile::tempdir().unwrap();
        let p = file_with(&dir, "c.bin", &test_bytes(1000, 3));
        let pf = prepare(&p, "demo/c", TransferId(4), MIN_CHUNK_SIZE, u64::MAX).unwrap();
        use std::io::Write;
        std::fs::OpenOptions::new().append(true).open(&p).unwrap().write_all(b"more").unwrap();
        assert_eq!(pf.read_chunk(0), Err(FailReason::SourceChanged));
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test sender::prepare`
  - Expected: FAIL to compile (`prepare` and `PreparedFile` are not found).

- [ ] **Step 3: Implement** below the module doc:

```rust
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::transfer::event::FailReason;
use crate::transfer::manifest::{
    chunk_count_for, offer_key_error, Manifest, TransferId, MAX_CHUNKS, MAX_CHUNK_SIZE, MIN_CHUNK_SIZE,
    PROTOCOL_VERSION,
};

fn io(e: std::io::Error) -> FailReason {
    FailReason::Io(e.to_string())
}

/// A hashed file ready to serve. Holds no file contents.
#[derive(Debug)]
pub struct PreparedFile {
    pub manifest: Manifest,
    pub path: PathBuf,
    len: u64,
    modified: Option<SystemTime>,
}

/// Stream the file once, one chunk buffer at a time, computing per-chunk and
/// whole-file BLAKE3. Blocking: call from `spawn_blocking`.
pub fn prepare(
    path: &Path,
    key: &str,
    id: TransferId,
    chunk_size: u32,
    max_file_size: u64,
) -> Result<PreparedFile, FailReason> {
    if let Some(e) = offer_key_error(key) {
        return Err(FailReason::InvalidOffer(e));
    }
    if !(MIN_CHUNK_SIZE..=MAX_CHUNK_SIZE).contains(&chunk_size) {
        return Err(FailReason::InvalidOffer(format!("chunk size {chunk_size} out of range")));
    }
    let meta = fs::metadata(path).map_err(io)?;
    if !meta.is_file() {
        return Err(FailReason::InvalidOffer(format!("{} is not a regular file", path.display())));
    }
    let len = meta.len();
    if len > max_file_size {
        return Err(FailReason::TooLarge { size: len, max: max_file_size });
    }
    let chunk_count = chunk_count_for(len, chunk_size)
        .filter(|c| *c <= MAX_CHUNKS)
        .ok_or_else(|| FailReason::InvalidOffer(format!("file needs more than {MAX_CHUNKS} chunks")))?;
    let filename = path
        .file_name()
        .and_then(|n| n.to_str())
        .and_then(crate::transfer::sanitize_filename)
        .unwrap_or_else(|| "file.bin".to_string());

    let mut f = File::open(path).map_err(io)?;
    let mut whole = blake3::Hasher::new();
    let mut chunk_hashes = Vec::with_capacity(chunk_count as usize);
    let mut buf = vec![0u8; chunk_size as usize];
    let mut remaining = len;
    while remaining > 0 {
        let n = remaining.min(u64::from(chunk_size)) as usize;
        f.read_exact(&mut buf[..n]).map_err(io)?;
        whole.update(&buf[..n]);
        chunk_hashes.push(blake3::hash(&buf[..n]).to_hex().to_string());
        remaining -= n as u64;
    }
    let after = fs::metadata(path).map_err(io)?;
    if after.len() != len || after.modified().ok() != meta.modified().ok() {
        return Err(FailReason::SourceChanged);
    }
    let manifest = Manifest {
        version: PROTOCOL_VERSION,
        transfer_id: id,
        key: key.to_string(),
        filename,
        total_size: len,
        chunk_size,
        chunk_count,
        file_hash: whole.finalize().to_hex().to_string(),
        chunk_hashes,
        encoding: "application/octet-stream".into(),
    };
    manifest.validate().map_err(FailReason::InvalidOffer)?;
    Ok(PreparedFile { manifest, path: path.to_path_buf(), len, modified: meta.modified().ok() })
}

impl PreparedFile {
    /// Positioned read of one chunk. Fails with SourceChanged when the file's
    /// length or mtime differ from what was hashed. Blocking.
    pub fn read_chunk(&self, index: u32) -> Result<Vec<u8>, FailReason> {
        let (off, len) = self
            .manifest
            .chunk_range(index)
            .ok_or_else(|| FailReason::InvalidOffer(format!("chunk {index} out of range")))?;
        let meta = fs::metadata(&self.path).map_err(io)?;
        if meta.len() != self.len || meta.modified().ok() != self.modified {
            return Err(FailReason::SourceChanged);
        }
        let mut f = File::open(&self.path).map_err(io)?;
        f.seek(SeekFrom::Start(off)).map_err(io)?;
        let mut buf = vec![0u8; len];
        f.read_exact(&mut buf).map_err(io)?;
        Ok(buf)
    }
}
```

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test sender::prepare && cargo clippy --all-targets -- -D warnings`
  - Expected: 6 passed, clippy clean.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/sender/prepare.rs
git commit -m "feat(transfer): stream-hash offers into a v2 manifest; positioned chunk reads

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T3: Serving an offer (Lane A)

**Owns:** `src/transfer/sender/serve.rs`

**Interfaces:**
- Consumes `prepare`, `PreparedFile`, the key builders, `CHUNK_PARAM`, `ERR_SOURCE_CHANGED`, `TaskHandle`, `CancelReason`, `EventSink`, `SERVE_CONCURRENCY` and `PROGRESS_INTERVAL`.
- Produces the real body of `spawn_offer` (same signature as in T1).
- **Behaviour:**
  1. Emit `Preparing`.
  2. Run `prepare` in `spawn_blocking`.
  3. Declare the token, the manifest queryable and the chunk queryable.
  4. Put the manifest.
  5. Emit `Announced(Outgoing)`.
  6. Serve requests. Emit `Progress { chunks: served }` at most every 100 ms.
  7. On a user cancel or idle expiry, delete the manifest key and then emit `Failed`.
  8. On shutdown, exit silently.
- **zenoh APIs** (https://docs.rs/zenoh/1.10.1/zenoh/):
  - `Session::liveliness().declare_token`: liveliness/struct.Liveliness.html#method.declare_token
  - `Session::declare_queryable`
  - `Query::reply(key, bytes).encoding(..)`, `Query::reply_err(..)` and `Query::parameters()`: query/struct.Query.html
  - `Session::put(..).encoding().priority().congestion_control()` and `Session::delete`
  - Do **not** call `.priority()`/`.congestion_control()` on replies: they are deprecated and have no effect.

- [ ] **Step 1: Write the failing loopback tests** at the bottom of `serve.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::manifest::*;
    use crate::transfer::test_support::*;
    use zenoh::sample::SampleKind;

    async fn offer_file(
        a: &Arc<Session>,
        port_tag: &str,
        bytes: &[u8],
    ) -> (tempfile::TempDir, TaskHandle, Manifest, tokio::sync::mpsc::UnboundedReceiver<TransferEvent>) {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("f.bin");
        std::fs::write(&p, bytes).unwrap();
        let (sink, mut rx) = collecting_sink();
        let h = spawn_offer(a.clone(), TransferId::random(), format!("t/{port_tag}"), p, test_limits(dir.path()), sink);
        let m = match next_event(&mut rx, 20, |e| matches!(e, TransferEvent::Announced { .. })).await {
            TransferEvent::Announced { manifest, direction } => {
                assert_eq!(direction, Direction::Outgoing);
                *manifest
            }
            _ => unreachable!(),
        };
        (dir, h, m, rx)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn offer_serves_manifest_and_chunks() {
        let (a, b) = session_pair(27701).await;
        let data = test_bytes(1024 * 1024, 5);
        let (_d, _h, m, _rx) = offer_file(&a, "serve", &data).await;
        wait_for_queryable(&b, &chunk_key(&m.key, m.transfer_id)).await;
        let replies = b.get(DISCOVERY_MANIFESTS).await.unwrap();
        let s = replies.recv_async().await.unwrap().into_result().unwrap();
        let got: Manifest = serde_json::from_slice(&s.payload().to_bytes()).unwrap();
        assert_eq!(got, m);
        let q = b.declare_querier(chunk_key(&m.key, m.transfer_id)).await.unwrap();
        let r = q.get().parameters(format!("{CHUNK_PARAM}=3")).await.unwrap();
        let bytes = r.recv_async().await.unwrap().into_result().unwrap().payload().to_bytes().into_owned();
        let (off, len) = m.chunk_range(3).unwrap();
        assert_eq!(bytes, &data[off as usize..off as usize + len]);
        assert_eq!(Some(blake3::hash(&bytes)), m.chunk_hash(3));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn offer_is_invisible_to_star_star_subscribers() {
        let (a, b) = session_pair(27702).await;
        let all = b.declare_subscriber("**").await.unwrap();
        let disc = b.declare_subscriber(DISCOVERY_MANIFESTS).await.unwrap();
        let (_d, _h, m, _rx) = offer_file(&a, "hidden", &test_bytes(200_000, 1)).await;
        let s = tokio::time::timeout(std::time::Duration::from_secs(10), disc.recv_async()).await.unwrap().unwrap();
        assert_eq!(s.key_expr().as_str(), manifest_key(&m.key, m.transfer_id));
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        while let Ok(Some(s)) = all.try_recv() {
            assert!(!s.key_expr().as_str().contains("@xfer"), "** saw {}", s.key_expr());
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn cancel_withdraws_manifest_and_token() {
        let (a, b) = session_pair(27703).await;
        let tokens = b.liveliness().declare_subscriber(DISCOVERY_TOKENS).await.unwrap();
        let manifests = b.declare_subscriber(DISCOVERY_MANIFESTS).await.unwrap();
        let (_d, h, m, mut rx) = offer_file(&a, "cancel", &test_bytes(10_000, 2)).await;
        wait_for_token(&b, &token_key(&m.key, m.transfer_id)).await;
        h.cancel(crate::transfer::tasks::CancelReason::User);
        let wait = std::time::Duration::from_secs(10);
        loop {
            let s = tokio::time::timeout(wait, manifests.recv_async()).await.unwrap().unwrap();
            if s.kind() == SampleKind::Delete {
                break;
            }
        }
        loop {
            let s = tokio::time::timeout(wait, tokens.recv_async()).await.unwrap().unwrap();
            if s.kind() == SampleKind::Delete {
                assert_eq!(s.key_expr().as_str(), token_key(&m.key, m.transfer_id));
                break;
            }
        }
        assert!(matches!(
            next_event(&mut rx, 5, |e| matches!(e, TransferEvent::Failed { .. })).await,
            TransferEvent::Failed { reason: FailReason::Cancelled { by_sender: false }, .. }
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn bad_index_gets_error_reply() {
        let (a, b) = session_pair(27704).await;
        let (_d, _h, m, _rx) = offer_file(&a, "badidx", &test_bytes(1000, 3)).await;
        wait_for_queryable(&b, &chunk_key(&m.key, m.transfer_id)).await;
        let q = b.declare_querier(chunk_key(&m.key, m.transfer_id)).await.unwrap();
        for params in ["i=99", "i=x", ""] {
            let r = q.get().parameters(params).await.unwrap();
            assert!(r.recv_async().await.unwrap().into_result().is_err(), "{params}");
        }
    }
}
```

  - Liveliness subscribers without `.history(true)` see only changes. The test subscribes before the offer, so the first sample is the Put and the Delete follows.
  - `Subscriber::try_recv` returns `ZResult<Option<Sample>>` on the default FIFO handler. If the compiler reports a different shape, use `while let Ok(Some(s))` → `while let Ok(s) = all.try_recv()` accordingly (see https://docs.rs/zenoh/1.10.1/zenoh/handlers/struct.FifoChannelHandler.html#method.try_recv).

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test -- --ignored sender::serve`
  - Expected: FAIL. The stub emits `Failed` and never `Announced`, so the tests panic with "no matching transfer event".

- [ ] **Step 3: Implement.** Replace the stub body and the imports in `serve.rs`:

```rust
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use zenoh::bytes::Encoding;
use zenoh::qos::{CongestionControl, Priority};
use zenoh::query::Query;
use zenoh::Session;

use super::prepare::{prepare, PreparedFile};
use crate::transfer::event::{Direction, EventSink, FailReason, TransferEvent};
use crate::transfer::limits::{TransferLimits, PROGRESS_INTERVAL, SERVE_CONCURRENCY};
use crate::transfer::manifest::{chunk_key, manifest_key, token_key, TransferId, CHUNK_PARAM, ERR_SOURCE_CHANGED};
use crate::transfer::tasks::{CancelReason, CancelRx, TaskHandle};

fn zerr(e: impl std::fmt::Display) -> FailReason {
    FailReason::Zenoh(e.to_string())
}

pub fn spawn_offer(
    session: Arc<Session>,
    id: TransferId,
    key: String,
    path: PathBuf,
    limits: TransferLimits,
    sink: EventSink,
) -> TaskHandle {
    TaskHandle::spawn(Direction::Outgoing, move |cancel| async move {
        if let Err(reason) = run_offer(&session, id, &key, path, &limits, &sink, cancel).await {
            sink(TransferEvent::Failed { id, reason });
        }
    })
}

async fn run_offer(
    session: &Session,
    id: TransferId,
    key: &str,
    path: PathBuf,
    limits: &TransferLimits,
    sink: &EventSink,
    mut cancel: CancelRx,
) -> Result<(), FailReason> {
    let total_size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    let filename = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    sink(TransferEvent::Preparing { id, key: key.to_string(), filename, total_size });

    let (k, cs, max) = (key.to_string(), limits.chunk_size, limits.max_file_size);
    let prepared: Arc<PreparedFile> = Arc::new(
        tokio::task::spawn_blocking(move || prepare(&path, &k, id, cs, max))
            .await
            .map_err(|e| FailReason::Io(e.to_string()))??,
    );
    let m = &prepared.manifest;
    let json = serde_json::to_vec(m).map_err(|e| FailReason::Io(e.to_string()))?;
    let mkey = manifest_key(key, id);
    let ckey = chunk_key(key, id);

    let token = session.liveliness().declare_token(token_key(key, id)).await.map_err(zerr)?;
    let manifest_q = session.declare_queryable(mkey.clone()).await.map_err(zerr)?;
    let chunk_q = session.declare_queryable(ckey.clone()).await.map_err(zerr)?;
    session
        .put(mkey.clone(), json.clone())
        .encoding(Encoding::APPLICATION_JSON)
        .priority(Priority::DataLow)
        .congestion_control(CongestionControl::Block)
        .await
        .map_err(zerr)?;
    sink(TransferEvent::Announced { manifest: Box::new(m.clone()), direction: Direction::Outgoing });

    let served = Arc::new(AtomicU64::new(0));
    let permits = Arc::new(tokio::sync::Semaphore::new(SERVE_CONCURRENCY));
    let mut reported = 0u64;
    let mut last_request = Instant::now();
    let mut tick = tokio::time::interval(PROGRESS_INTERVAL);

    let outcome: Option<FailReason> = loop {
        tokio::select! {
            _ = cancel.changed() => {
                break match *cancel.borrow() {
                    Some(CancelReason::User) => Some(FailReason::Cancelled { by_sender: false }),
                    _ => None,
                };
            }
            q = manifest_q.recv_async() => {
                let Ok(q) = q else { break Some(FailReason::Zenoh("manifest queryable closed".into())) };
                let _ = q.reply(mkey.clone(), json.clone()).encoding(Encoding::APPLICATION_JSON).await;
            }
            q = chunk_q.recv_async() => {
                let Ok(q) = q else { break Some(FailReason::Zenoh("chunk queryable closed".into())) };
                last_request = Instant::now();
                let permit = permits.clone().acquire_owned().await.expect("semaphore is never closed");
                let (prep, served, reply_key) = (prepared.clone(), served.clone(), ckey.clone());
                tokio::spawn(async move {
                    let _permit = permit;
                    if serve_chunk(q, prep, reply_key).await {
                        served.fetch_add(1, Ordering::Relaxed);
                    }
                });
            }
            _ = tick.tick() => {
                let now = served.load(Ordering::Relaxed);
                if now != reported {
                    reported = now;
                    sink(TransferEvent::Progress { id, chunks: now });
                }
                if last_request.elapsed() >= limits.offer_idle_timeout {
                    break Some(FailReason::OfferExpired);
                }
            }
        }
    };

    if outcome.is_some() {
        // Deliberate end: tell every receiver before the token disappears.
        let _ = session.delete(mkey).priority(Priority::DataLow).await;
    }
    drop(chunk_q);
    drop(manifest_q);
    drop(token);
    if let Some(reason) = outcome {
        sink(TransferEvent::Failed { id, reason });
    }
    Ok(())
}

/// Serve one chunk request. Returns true when data was sent.
async fn serve_chunk(q: Query, prep: Arc<PreparedFile>, reply_key: String) -> bool {
    let Some(index) = q.parameters().get(CHUNK_PARAM).and_then(|v| v.parse::<u32>().ok()) else {
        let _ = q.reply_err("bad-index").await;
        return false;
    };
    match tokio::task::spawn_blocking(move || prep.read_chunk(index)).await {
        Ok(Ok(bytes)) => q.reply(reply_key, bytes).encoding(Encoding::APPLICATION_OCTET_STREAM).await.is_ok(),
        Ok(Err(FailReason::SourceChanged)) => {
            let _ = q.reply_err(ERR_SOURCE_CHANGED).await;
            false
        }
        Ok(Err(e)) => {
            let _ = q.reply_err(e.to_string()).await;
            false
        }
        Err(e) => {
            let _ = q.reply_err(format!("internal: {e}")).await;
            false
        }
    }
}
```

  - The manifest queryable replies on the **concrete** `mkey`, never on `q.key_expr()`, because a discovery GET arrives with the wildcard `**/@xfer/*/manifest`.
  - If `session.delete(..).priority(..)` does not compile on 1.10.1, drop `.priority(..)`. The delete is small.

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test -- --ignored sender::serve && cargo clippy --all-targets -- -D warnings`
  - Expected: 4 passed, clippy clean with no deprecation warnings.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/sender/serve.rs
git commit -m "feat(transfer): serve v2 offers via queryables with liveliness and withdraw

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T4: Spool, admission and save (Lane B)

**Owns:** `src/transfer/receiver/spool.rs`

**Interfaces:**
- Produces:
  - `pub struct Spool` with `pub fn paths(dir: &Path, id: TransferId) -> (PathBuf, PathBuf)`, `pub fn open(dir: &Path, m: &Manifest) -> io::Result<(Spool, Vec<bool>)>`, `pub fn write_chunk(&self, i: u32, bytes: &[u8]) -> Result<(), ChunkError>`, `pub fn verify_file(&self) -> io::Result<bool>` and `pub fn remove(&self)`
  - `pub enum ChunkError { Length { expected: usize, got: usize }, Hash, Io(io::Error) }`
  - `pub fn admit(m: &Manifest, max_file_size: u64, available: u64, existing_part: u64, margin: u64) -> Result<(), FailReason>`
  - `pub fn save_verified(spool_dir: &Path, id: TransferId, dest: &Path) -> io::Result<()>`
  - `pub fn gc_spool(dir: &Path, max_age: Duration, now: SystemTime) -> usize`
  - The real body of `spawn_save`.
- `tempfile::NamedTempFile::new_in` and `persist`: https://docs.rs/tempfile/3/tempfile/struct.NamedTempFile.html#method.persist. `File::set_modified` is stable since Rust 1.75 and is used in the GC test.

- [ ] **Step 1: Write the failing tests** at the bottom of `spool.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::manifest::MIN_CHUNK_SIZE;
    use crate::transfer::test_support::{collecting_sink, manifest_from_bytes, next_event, test_bytes};

    const CS: u32 = MIN_CHUNK_SIZE;

    fn fixture(chunks: usize) -> (tempfile::TempDir, Vec<u8>, Manifest) {
        let dir = tempfile::tempdir().unwrap();
        let data = test_bytes(chunks * CS as usize - 7, 4);
        let m = manifest_from_bytes("t/spool", "s.bin", &data, CS);
        (dir, data, m)
    }
    fn chunk<'a>(data: &'a [u8], m: &Manifest, i: u32) -> &'a [u8] {
        let (o, l) = m.chunk_range(i).unwrap();
        &data[o as usize..o as usize + l]
    }

    #[test]
    fn open_fresh_preallocates_and_writes_manifest() {
        let (dir, _d, m) = fixture(3);
        let (_s, have) = Spool::open(dir.path(), &m).unwrap();
        assert_eq!(have, vec![false; 3]);
        let (part, meta) = Spool::paths(dir.path(), m.transfer_id);
        assert_eq!(std::fs::metadata(part).unwrap().len(), m.total_size);
        assert_eq!(serde_json::from_slice::<Manifest>(&std::fs::read(meta).unwrap()).unwrap(), m);
    }

    #[test]
    fn write_chunk_rejects_corrupt_and_wrong_length() {
        let (dir, data, m) = fixture(2);
        let (s, _) = Spool::open(dir.path(), &m).unwrap();
        let mut bad = chunk(&data, &m, 0).to_vec();
        bad[10] ^= 1;
        assert!(matches!(s.write_chunk(0, &bad), Err(ChunkError::Hash)));
        assert!(matches!(s.write_chunk(0, &bad[..5]), Err(ChunkError::Length { .. })));
        assert!(s.write_chunk(0, chunk(&data, &m, 0)).is_ok());
    }

    #[test]
    fn reopen_resumes_verified_chunks() {
        let (dir, data, m) = fixture(4);
        let (s, _) = Spool::open(dir.path(), &m).unwrap();
        s.write_chunk(0, chunk(&data, &m, 0)).unwrap();
        s.write_chunk(2, chunk(&data, &m, 2)).unwrap();
        drop(s);
        let (_s, have) = Spool::open(dir.path(), &m).unwrap();
        assert_eq!(have, vec![true, false, true, false]);
    }

    #[test]
    fn reopen_with_different_manifest_starts_fresh() {
        let (dir, data, m) = fixture(2);
        let (s, _) = Spool::open(dir.path(), &m).unwrap();
        s.write_chunk(0, chunk(&data, &m, 0)).unwrap();
        let mut other = m.clone();
        other.filename = "other.bin".into();
        let (_s, have) = Spool::open(dir.path(), &other).unwrap();
        assert_eq!(have, vec![false, false]);
    }

    #[test]
    fn verify_file_detects_mismatch() {
        let (dir, data, m) = fixture(2);
        let (s, _) = Spool::open(dir.path(), &m).unwrap();
        for i in 0..2 {
            s.write_chunk(i, chunk(&data, &m, i)).unwrap();
        }
        assert!(s.verify_file().unwrap());
        let (part, _) = Spool::paths(dir.path(), m.transfer_id);
        let mut bytes = std::fs::read(&part).unwrap();
        bytes[3] ^= 1;
        std::fs::write(&part, bytes).unwrap();
        assert!(!s.verify_file().unwrap());
    }

    #[test]
    fn empty_manifest_verifies() {
        let dir = tempfile::tempdir().unwrap();
        let m = manifest_from_bytes("t/empty", "e.bin", b"", CS);
        let (s, have) = Spool::open(dir.path(), &m).unwrap();
        assert!(have.is_empty());
        assert!(s.verify_file().unwrap());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn save_moves_into_destination_dir() {
        let (dir, data, m) = fixture(2);
        let (s, _) = Spool::open(dir.path(), &m).unwrap();
        for i in 0..2 {
            s.write_chunk(i, chunk(&data, &m, i)).unwrap();
        }
        let dest_dir = tempfile::tempdir().unwrap();
        let dest = dest_dir.path().join("out.bin");
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_save(dir.path().to_path_buf(), m.transfer_id, dest.clone(), sink);
        assert!(matches!(next_event(&mut rx, 10, |_| true).await, TransferEvent::Saved { .. }));
        assert_eq!(std::fs::read(&dest).unwrap(), data);
        assert!(!Spool::paths(dir.path(), m.transfer_id).0.exists());
    }

    #[test]
    fn admit_rejects_too_large_and_no_space() {
        let (_dir, _d, m) = fixture(2);
        assert!(matches!(admit(&m, m.total_size - 1, u64::MAX, 0, 0), Err(FailReason::TooLarge { .. })));
        assert!(matches!(admit(&m, u64::MAX, m.total_size, 0, 1), Err(FailReason::InsufficientSpace { .. })));
        assert!(admit(&m, u64::MAX, 1, m.total_size, 1).is_ok(), "resume needs only the margin");
    }

    #[test]
    fn gc_spool_removes_only_old_spool_files() {
        let (dir, _d, m) = fixture(1);
        let _ = Spool::open(dir.path(), &m).unwrap();
        std::fs::write(dir.path().join("unrelated.txt"), b"x").unwrap();
        let (part, meta) = Spool::paths(dir.path(), m.transfer_id);
        let old = SystemTime::now() - Duration::from_secs(3 * 24 * 3600);
        for p in [&part, &meta] {
            std::fs::File::options().write(true).open(p).unwrap().set_modified(old).unwrap();
        }
        assert_eq!(gc_spool(dir.path(), Duration::from_secs(24 * 3600), SystemTime::now()), 2);
        assert!(dir.path().join("unrelated.txt").exists());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test receiver::spool`
  - Expected: FAIL to compile (`Spool` is not found).

- [ ] **Step 3: Implement.** Replace the file body above the tests:

```rust
//! Receiver spool: `{id}.part` (preallocated) + `{id}.manifest.json`.
//! Resume re-verifies chunk regions against the manifest; no bitmap is stored.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use crate::transfer::event::{Direction, EventSink, FailReason, TransferEvent};
use crate::transfer::manifest::{Manifest, TransferId};
use crate::transfer::tasks::TaskHandle;

#[derive(Debug)]
pub enum ChunkError {
    Length { expected: usize, got: usize },
    Hash,
    Io(io::Error),
}

pub struct Spool {
    part: PathBuf,
    meta: PathBuf,
    manifest: Manifest,
}

impl Spool {
    pub fn paths(dir: &Path, id: TransferId) -> (PathBuf, PathBuf) {
        (dir.join(format!("{id}.part")), dir.join(format!("{id}.manifest.json")))
    }

    /// Open or create the spool for `m`. Returns which chunks are already
    /// verified on disk. Blocking.
    pub fn open(dir: &Path, m: &Manifest) -> io::Result<(Spool, Vec<bool>)> {
        fs::create_dir_all(dir)?;
        let (part, meta) = Self::paths(dir, m.transfer_id);
        let same_manifest = fs::read(&meta)
            .ok()
            .and_then(|b| serde_json::from_slice::<Manifest>(&b).ok())
            .is_some_and(|old| &old == m);
        let right_len = fs::metadata(&part).is_ok_and(|md| md.len() == m.total_size);
        let spool = Spool { part, meta, manifest: m.clone() };
        let have = if same_manifest && right_len {
            spool.scan()?
        } else {
            let f = OpenOptions::new().create(true).write(true).truncate(true).open(&spool.part)?;
            f.set_len(m.total_size)?;
            fs::write(&spool.meta, serde_json::to_vec(m).map_err(io::Error::other)?)?;
            vec![false; m.chunk_count as usize]
        };
        Ok((spool, have))
    }

    fn scan(&self) -> io::Result<Vec<bool>> {
        let mut f = File::open(&self.part)?;
        let mut buf = vec![0u8; self.manifest.chunk_size as usize];
        (0..self.manifest.chunk_count)
            .map(|i| {
                let (off, len) = self.manifest.chunk_range(i).expect("i < chunk_count");
                f.seek(SeekFrom::Start(off))?;
                f.read_exact(&mut buf[..len])?;
                Ok(Some(blake3::hash(&buf[..len])) == self.manifest.chunk_hash(i))
            })
            .collect()
    }

    /// Verify length and BLAKE3 against the manifest, then write in place. Blocking.
    pub fn write_chunk(&self, i: u32, bytes: &[u8]) -> Result<(), ChunkError> {
        let (off, len) = self.manifest.chunk_range(i).ok_or(ChunkError::Length { expected: 0, got: bytes.len() })?;
        if bytes.len() != len {
            return Err(ChunkError::Length { expected: len, got: bytes.len() });
        }
        if Some(blake3::hash(bytes)) != self.manifest.chunk_hash(i) {
            return Err(ChunkError::Hash);
        }
        let mut f = OpenOptions::new().write(true).open(&self.part).map_err(ChunkError::Io)?;
        f.seek(SeekFrom::Start(off)).map_err(ChunkError::Io)?;
        f.write_all(bytes).map_err(ChunkError::Io)
    }

    /// Stream the whole part file through BLAKE3 and compare. Blocking.
    pub fn verify_file(&self) -> io::Result<bool> {
        let mut f = File::open(&self.part)?;
        let mut h = blake3::Hasher::new();
        let mut buf = vec![0u8; 1024 * 1024];
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            h.update(&buf[..n]);
        }
        Ok(blake3::Hash::from_hex(&self.manifest.file_hash).is_ok_and(|want| want == h.finalize()))
    }

    pub fn remove(&self) {
        let _ = fs::remove_file(&self.part);
        let _ = fs::remove_file(&self.meta);
    }
}

/// Size and disk-space admission. `existing_part` bytes are already allocated.
pub fn admit(m: &Manifest, max_file_size: u64, available: u64, existing_part: u64, margin: u64) -> Result<(), FailReason> {
    if m.total_size > max_file_size {
        return Err(FailReason::TooLarge { size: m.total_size, max: max_file_size });
    }
    let need = m.total_size.saturating_sub(existing_part).saturating_add(margin);
    if available < need {
        return Err(FailReason::InsufficientSpace { need, available });
    }
    Ok(())
}

/// Copy the verified part into `dest` atomically (temp file in the same
/// directory, then rename), then drop the spool. Blocking.
pub fn save_verified(spool_dir: &Path, id: TransferId, dest: &Path) -> io::Result<()> {
    let (part, meta) = Spool::paths(spool_dir, id);
    let parent = dest.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    io::copy(&mut File::open(&part)?, tmp.as_file_mut())?;
    tmp.as_file().sync_all()?;
    tmp.persist(dest).map_err(|e| e.error)?;
    let _ = fs::remove_file(part);
    let _ = fs::remove_file(meta);
    Ok(())
}

/// Delete spool files (`<32 hex>.part` / `.manifest.json`) older than `max_age`.
pub fn gc_spool(dir: &Path, max_age: Duration, now: SystemTime) -> usize {
    let Ok(rd) = fs::read_dir(dir) else { return 0 };
    let mut removed = 0;
    for entry in rd.flatten() {
        let p = entry.path();
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let ours = (name.ends_with(".part") || name.ends_with(".manifest.json"))
            && name.split('.').next().is_some_and(|s| s.parse::<TransferId>().is_ok());
        if !ours {
            continue;
        }
        let old = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| now.duration_since(t).ok())
            .is_some_and(|age| age > max_age);
        if old && fs::remove_file(&p).is_ok() {
            removed += 1;
        }
    }
    removed
}

pub fn spawn_save(spool_dir: PathBuf, id: TransferId, dest: PathBuf, sink: EventSink) -> TaskHandle {
    TaskHandle::spawn(Direction::Incoming, move |_c| async move {
        let d = dest.clone();
        let res = tokio::task::spawn_blocking(move || save_verified(&spool_dir, id, &d)).await;
        match res {
            Ok(Ok(())) => sink(TransferEvent::Saved { id, path: dest }),
            Ok(Err(e)) => sink(TransferEvent::Failed { id, reason: FailReason::Io(e.to_string()) }),
            Err(e) => sink(TransferEvent::Failed { id, reason: FailReason::Io(e.to_string()) }),
        }
    })
}
```

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test receiver::spool && cargo clippy --all-targets -- -D warnings`
  - Expected: 9 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/receiver/spool.rs
git commit -m "feat(transfer): verified spool with resume scan, admission, atomic save and GC

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T5: Fetcher (Lane B)

**Owns:** `src/transfer/receiver/fetch.rs`

**Interfaces:**
- Consumes `Spool`, `ChunkError`, `admit`, the key builders, `CHUNK_PARAM`, `ERR_SOURCE_CHANGED`, `TransferLimits`, `PRESENCE_TIMEOUT`, `PROGRESS_INTERVAL`, `RETRY_BACKOFF` and the tasks types.
- Produces the real body of `spawn_fetch`.
- **Outcomes:**
  - Success: `Completed { id }`.
  - Failure: `Failed { id, reason }`.
  - `CancelReason::Shutdown`: no event, and the spool is kept.
  - User cancel: `Failed(Cancelled{by_sender:false})`, and the spool is removed.
- **zenoh APIs** (https://docs.rs/zenoh/1.10.1/zenoh/):
  - `Session::declare_querier(..)` with `.priority(Priority::DataLow)`, `.congestion_control(CongestionControl::Block)`, `.target(QueryTarget::BestMatching)`, `.consolidation(ConsolidationMode::None)` and `.timeout(d)`: `QuerierBuilder`, source `api/builders/querier.rs:92-168`.
  - `Querier::get().parameters(String)`.
  - `Reply::into_result()` and `ReplyError::payload()`.
  - `liveliness().declare_subscriber(..).history(true)` (`builders/liveliness.rs:234`) and `liveliness().get(..).timeout(..)`.

- [ ] **Step 1: Write the failing tests** at the bottom of `fetch.rs`. Every test is a two-session loopback: session A runs a scripted **fake sender**, so faults can be injected, and session B runs the real fetcher.

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::manifest::*;
    use crate::transfer::receiver::spool::Spool;
    use crate::transfer::test_support::*;
    use std::collections::{BTreeSet, HashMap};
    use std::sync::Mutex;
    use std::time::Duration;

    #[derive(Clone, Copy)]
    enum Fault {
        Serve,
        Drop,
        Corrupt,
        Error(&'static str),
        Delay(Duration),
    }

    struct FakeSender {
        hits: Arc<Mutex<HashMap<u32, u32>>>,
        token: Option<zenoh::liveliness::LivelinessToken>,
        task: tokio::task::JoinHandle<()>,
    }
    impl FakeSender {
        fn gone(&mut self) {
            self.task.abort();
            self.token.take();
        }
        fn hits(&self, i: u32) -> u32 {
            *self.hits.lock().unwrap().get(&i).unwrap_or(&0)
        }
        fn requested(&self) -> BTreeSet<u32> {
            self.hits.lock().unwrap().keys().copied().collect()
        }
    }

    async fn fake_sender(
        s: &Arc<Session>,
        m: &Manifest,
        data: Arc<Vec<u8>>,
        fault: impl Fn(u32, u32) -> Fault + Send + Sync + 'static,
    ) -> FakeSender {
        let token = s.liveliness().declare_token(token_key(&m.key, m.transfer_id)).await.unwrap();
        let q = s.declare_queryable(chunk_key(&m.key, m.transfer_id)).await.unwrap();
        let hits = Arc::new(Mutex::new(HashMap::new()));
        let (h2, m2) = (hits.clone(), m.clone());
        let task = tokio::spawn(async move {
            while let Ok(query) = q.recv_async().await {
                let i: u32 = query.parameters().get(CHUNK_PARAM).unwrap().parse().unwrap();
                let attempt = {
                    let mut h = h2.lock().unwrap();
                    let n = h.entry(i).or_insert(0);
                    *n += 1;
                    *n
                };
                let (off, len) = m2.chunk_range(i).unwrap();
                let mut bytes = data[off as usize..off as usize + len].to_vec();
                match fault(i, attempt) {
                    Fault::Serve => {}
                    Fault::Drop => continue, // dropping the Query finalizes it with no reply
                    Fault::Corrupt => bytes[0] ^= 0xff,
                    Fault::Error(msg) => {
                        let _ = query.reply_err(msg).await;
                        continue;
                    }
                    Fault::Delay(d) => tokio::time::sleep(d).await,
                }
                let _ = query.reply(chunk_key(&m2.key, m2.transfer_id), bytes).await;
            }
        });
        FakeSender { hits, token: Some(token), task }
    }

    struct Rig {
        a: Arc<Session>,
        b: Arc<Session>,
        spool: tempfile::TempDir,
        data: Arc<Vec<u8>>,
        m: Manifest,
    }

    async fn rig(port: u16, len: usize) -> Rig {
        let (a, b) = session_pair(port).await;
        let data = Arc::new(test_bytes(len, (port % 251) as u8));
        let m = manifest_from_bytes(&format!("t/f{port}"), "f.bin", &data, 64 * 1024);
        Rig { a, b, spool: tempfile::tempdir().unwrap(), data, m }
    }

    async fn ready(r: &Rig) {
        wait_for_queryable(&r.b, &chunk_key(&r.m.key, r.m.transfer_id)).await;
        wait_for_token(&r.b, &token_key(&r.m.key, r.m.transfer_id)).await;
    }

    fn part(r: &Rig) -> Vec<u8> {
        std::fs::read(Spool::paths(r.spool.path(), r.m.transfer_id).0).unwrap()
    }

    fn is_done(e: &TransferEvent) -> bool {
        matches!(e, TransferEvent::Completed { .. } | TransferEvent::Failed { .. })
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn fetch_happy_path_writes_verified_file() {
        let r = rig(27711, 2 * 1024 * 1024 + 5).await;
        let _f = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Serve).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        assert!(matches!(next_event(&mut rx, 30, is_done).await, TransferEvent::Completed { .. }));
        assert_eq!(part(&r), *r.data);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn dropped_chunk_is_retried() {
        let r = rig(27712, 640 * 1024).await;
        let f = fake_sender(&r.a, &r.m, r.data.clone(), |i, a| if i == 5 && a == 1 { Fault::Drop } else { Fault::Serve }).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        assert!(matches!(next_event(&mut rx, 30, is_done).await, TransferEvent::Completed { .. }));
        assert_eq!(f.hits(5), 2);
        assert_eq!(part(&r), *r.data);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn corrupted_chunk_is_rejected_then_refetched() {
        let r = rig(27713, 640 * 1024).await;
        let f = fake_sender(&r.a, &r.m, r.data.clone(), |i, a| if i == 3 && a == 1 { Fault::Corrupt } else { Fault::Serve }).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        assert!(matches!(next_event(&mut rx, 30, is_done).await, TransferEvent::Completed { .. }));
        assert_eq!(f.hits(3), 2);
        assert_eq!(part(&r), *r.data);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn persistent_corruption_fails_with_chunk_hash_mismatch() {
        let r = rig(27714, 640 * 1024).await;
        let _f = fake_sender(&r.a, &r.m, r.data.clone(), |i, _| if i == 3 { Fault::Corrupt } else { Fault::Serve }).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let limits = TransferLimits { max_retries: 2, ..test_limits(r.spool.path()) };
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), limits, sink);
        assert!(matches!(
            next_event(&mut rx, 30, is_done).await,
            TransferEvent::Failed { reason: FailReason::ChunkHashMismatch { index: 3 }, .. }
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn source_changed_fails_fast() {
        let r = rig(27715, 640 * 1024).await;
        let f = fake_sender(&r.a, &r.m, r.data.clone(), |i, _| if i == 2 { Fault::Error(ERR_SOURCE_CHANGED) } else { Fault::Serve }).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        assert!(matches!(
            next_event(&mut rx, 30, is_done).await,
            TransferEvent::Failed { reason: FailReason::SourceChanged, .. }
        ));
        assert_eq!(f.hits(2), 1, "source-changed must not be retried");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn resume_after_restart_skips_verified_chunks() {
        let r = rig(27716, 16 * 64 * 1024).await;
        let limits = TransferLimits { max_retries: 1000, ..test_limits(r.spool.path()) };
        let mut f1 = fake_sender(&r.a, &r.m, r.data.clone(), |i, _| if i < 8 { Fault::Serve } else { Fault::Drop }).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let h = spawn_fetch(r.b.clone(), r.m.clone(), limits.clone(), sink);
        next_event(&mut rx, 30, |e| matches!(e, TransferEvent::Progress { chunks, .. } if *chunks >= 8)).await;
        h.cancel(CancelReason::Shutdown); // "receiver restart": spool kept
        for _ in 0..100 {
            if h.is_finished() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(h.is_finished());
        f1.gone();
        let f2 = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Serve).await;
        ready(&r).await;
        let (sink2, mut rx2) = collecting_sink();
        let _h2 = spawn_fetch(r.b.clone(), r.m.clone(), limits, sink2);
        assert!(matches!(next_event(&mut rx2, 30, is_done).await, TransferEvent::Completed { .. }));
        assert!(f2.requested().iter().all(|i| *i >= 8), "re-requested verified chunks: {:?}", f2.requested());
        assert_eq!(part(&r), *r.data);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn sender_disappearing_fails_with_sender_gone() {
        let r = rig(27717, 64 * 64 * 1024).await;
        let mut f = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Delay(Duration::from_millis(100))).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let limits = TransferLimits { max_retries: 1000, ..test_limits(r.spool.path()) };
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), limits, sink);
        next_event(&mut rx, 30, |e| matches!(e, TransferEvent::Progress { chunks, .. } if *chunks >= 2)).await;
        f.gone();
        assert!(matches!(
            next_event(&mut rx, 15, is_done).await,
            TransferEvent::Failed { reason: FailReason::SenderGone, .. }
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn user_cancel_removes_spool() {
        let r = rig(27718, 64 * 64 * 1024).await;
        let _f = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Delay(Duration::from_millis(100))).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        next_event(&mut rx, 30, |e| matches!(e, TransferEvent::Progress { chunks, .. } if *chunks >= 1)).await;
        h.cancel(CancelReason::User);
        assert!(matches!(
            next_event(&mut rx, 10, is_done).await,
            TransferEvent::Failed { reason: FailReason::Cancelled { by_sender: false }, .. }
        ));
        assert!(!Spool::paths(r.spool.path(), r.m.transfer_id).0.exists());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn zero_byte_file_completes() {
        let r = rig(27719, 0).await;
        let f = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Serve).await;
        wait_for_token(&r.b, &token_key(&r.m.key, r.m.transfer_id)).await;
        let (sink, mut rx) = collecting_sink();
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), test_limits(r.spool.path()), sink);
        assert!(matches!(next_event(&mut rx, 30, is_done).await, TransferEvent::Completed { .. }));
        assert!(part(&r).is_empty());
        assert!(f.requested().is_empty());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn oversize_manifest_is_rejected_before_any_request() {
        let r = rig(27720, 640 * 1024).await;
        let f = fake_sender(&r.a, &r.m, r.data.clone(), |_, _| Fault::Serve).await;
        ready(&r).await;
        let (sink, mut rx) = collecting_sink();
        let limits = TransferLimits { max_file_size: 1000, ..test_limits(r.spool.path()) };
        let _h = spawn_fetch(r.b.clone(), r.m.clone(), limits, sink);
        assert!(matches!(
            next_event(&mut rx, 10, is_done).await,
            TransferEvent::Failed { reason: FailReason::TooLarge { .. }, .. }
        ));
        assert!(f.requested().is_empty());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test -- --ignored receiver::fetch`
  - Expected: FAIL. The stub emits `Failed(Zenoh("fetch engine not built yet"))`.

- [ ] **Step 3: Implement.** Replace the stub:

```rust
//! Pull a v2 transfer: Querier with N requests in flight, verify, spool.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::time::Duration;

use tokio::task::JoinSet;
use tokio::time::Instant;
use zenoh::qos::{CongestionControl, Priority};
use zenoh::query::{ConsolidationMode, Querier, QueryTarget};
use zenoh::sample::SampleKind;
use zenoh::Session;

use super::spool::{admit, ChunkError, Spool};
use crate::transfer::event::{Direction, EventSink, FailReason, TransferEvent};
use crate::transfer::limits::{TransferLimits, PRESENCE_TIMEOUT, PROGRESS_INTERVAL, RETRY_BACKOFF};
use crate::transfer::manifest::{chunk_key, token_key, Manifest, CHUNK_PARAM, ERR_SOURCE_CHANGED};
use crate::transfer::tasks::{CancelReason, CancelRx, TaskHandle};

#[derive(Debug)]
enum ChunkFailure {
    Timeout,
    NoReply,
    Corrupt,
    SourceChanged,
    SenderError(String),
    Io(String),
}

impl From<ChunkError> for ChunkFailure {
    fn from(e: ChunkError) -> Self {
        match e {
            ChunkError::Length { .. } | ChunkError::Hash => Self::Corrupt,
            ChunkError::Io(e) => Self::Io(e.to_string()),
        }
    }
}

/// `Err(None)` = shutdown (silent, spool kept); `Err(Some(r))` = failure.
type FetchResult = Result<(), Option<FailReason>>;

fn zfail(e: impl std::fmt::Display) -> Option<FailReason> {
    Some(FailReason::Zenoh(e.to_string()))
}
fn iofail(e: impl std::fmt::Display) -> Option<FailReason> {
    Some(FailReason::Io(e.to_string()))
}

pub fn spawn_fetch(session: Arc<Session>, manifest: Manifest, limits: TransferLimits, sink: EventSink) -> TaskHandle {
    TaskHandle::spawn(Direction::Incoming, move |cancel| async move {
        let id = manifest.transfer_id;
        match fetch(&session, &manifest, &limits, &sink, cancel).await {
            Ok(()) => sink(TransferEvent::Completed { id }),
            Err(None) => {}
            Err(Some(reason)) => sink(TransferEvent::Failed { id, reason }),
        }
    })
}

async fn request_chunk(
    q: Arc<Querier<'static>>,
    index: u32,
    attempt: u32,
    timeout: Duration,
) -> (u32, Result<Vec<u8>, ChunkFailure>) {
    if attempt > 0 {
        tokio::time::sleep(RETRY_BACKOFF * attempt.min(10)).await;
    }
    let replies = match q.get().parameters(format!("{CHUNK_PARAM}={index}")).await {
        Ok(r) => r,
        Err(e) => return (index, Err(ChunkFailure::SenderError(e.to_string()))),
    };
    let res = match tokio::time::timeout(timeout, replies.recv_async()).await {
        Err(_) => Err(ChunkFailure::Timeout),
        Ok(Err(_)) => Err(ChunkFailure::NoReply),
        Ok(Ok(reply)) => match reply.into_result() {
            Ok(sample) => Ok(sample.payload().to_bytes().into_owned()),
            Err(err) => {
                let msg = String::from_utf8_lossy(&err.payload().to_bytes()).into_owned();
                if msg == ERR_SOURCE_CHANGED {
                    Err(ChunkFailure::SourceChanged)
                } else {
                    Err(ChunkFailure::SenderError(msg))
                }
            }
        },
    };
    (index, res)
}

async fn fetch(session: &Session, m: &Manifest, limits: &TransferLimits, sink: &EventSink, mut cancel: CancelRx) -> FetchResult {
    let id = m.transfer_id;
    m.validate().map_err(|e| Some(FailReason::InvalidOffer(e)))?;
    std::fs::create_dir_all(&limits.spool_dir).map_err(iofail)?;
    let (part, _) = Spool::paths(&limits.spool_dir, id);
    let existing = std::fs::metadata(&part).map(|md| md.len()).unwrap_or(0);
    let available = (limits.available_space)(&limits.spool_dir).map_err(iofail)?;
    admit(m, limits.max_file_size, available, existing, limits.disk_margin).map_err(Some)?;

    let (dir, mc) = (limits.spool_dir.clone(), m.clone());
    let (spool, mut have) = tokio::task::spawn_blocking(move || Spool::open(&dir, &mc))
        .await
        .map_err(iofail)?
        .map_err(iofail)?;
    let spool = Arc::new(spool);

    // Subscribe first, then check presence: a Delete after this point is seen.
    let token = token_key(&m.key, id);
    let alive = session.liveliness().declare_subscriber(token.clone()).history(true).await.map_err(zfail)?;
    let present = {
        let replies = session.liveliness().get(token).timeout(PRESENCE_TIMEOUT).await.map_err(zfail)?;
        matches!(replies.recv_async().await, Ok(r) if r.result().is_ok())
    };
    if !present {
        return Err(Some(FailReason::SenderGone));
    }

    let querier: Arc<Querier<'static>> = Arc::new(
        session
            .declare_querier(chunk_key(&m.key, id))
            .priority(Priority::DataLow)
            .congestion_control(CongestionControl::Block)
            .target(QueryTarget::BestMatching)
            .consolidation(ConsolidationMode::None)
            .timeout(limits.request_timeout)
            .await
            .map_err(zfail)?,
    );

    let mut pending: VecDeque<u32> = (0..m.chunk_count).filter(|i| !have[*i as usize]).collect();
    let mut attempts: HashMap<u32, u32> = HashMap::new();
    let mut inflight: JoinSet<(u32, Result<Vec<u8>, ChunkFailure>)> = JoinSet::new();
    let mut done = have.iter().filter(|h| **h).count() as u64;
    let mut reported = done;
    sink(TransferEvent::Progress { id, chunks: done });
    let mut last_progress = Instant::now();
    let mut tick = tokio::time::interval(PROGRESS_INTERVAL);

    loop {
        while inflight.len() < limits.in_flight.max(1) {
            let Some(i) = pending.pop_front() else { break };
            let attempt = attempts.get(&i).copied().unwrap_or(0);
            inflight.spawn(request_chunk(querier.clone(), i, attempt, limits.request_timeout));
        }
        if inflight.is_empty() {
            break;
        }
        tokio::select! {
            _ = cancel.changed() => {
                return match *cancel.borrow() {
                    Some(CancelReason::User) => {
                        spool.remove();
                        Err(Some(FailReason::Cancelled { by_sender: false }))
                    }
                    _ => Err(None),
                };
            }
            s = alive.recv_async() => match s {
                Ok(s) if s.kind() == SampleKind::Delete => return Err(Some(FailReason::SenderGone)),
                Ok(_) => {}
                Err(_) => return Err(Some(FailReason::SenderGone)),
            },
            _ = tokio::time::sleep_until(last_progress + limits.inactivity_timeout) => {
                return Err(Some(FailReason::Stalled));
            }
            _ = tick.tick() => {
                if done != reported {
                    reported = done;
                    sink(TransferEvent::Progress { id, chunks: done });
                }
            }
            Some(joined) = inflight.join_next() => {
                let (i, res) = joined.map_err(iofail)?;
                let verdict = match res {
                    Ok(bytes) => {
                        let sp = spool.clone();
                        tokio::task::spawn_blocking(move || sp.write_chunk(i, &bytes))
                            .await
                            .map_err(iofail)?
                            .map_err(ChunkFailure::from)
                    }
                    Err(f) => Err(f),
                };
                match verdict {
                    Ok(()) => {
                        have[i as usize] = true;
                        done += 1;
                        last_progress = Instant::now();
                    }
                    Err(ChunkFailure::SourceChanged) => return Err(Some(FailReason::SourceChanged)),
                    Err(ChunkFailure::Io(e)) => return Err(Some(FailReason::Io(e))),
                    Err(f) => {
                        let n = attempts.entry(i).or_insert(0);
                        *n += 1;
                        if *n > limits.max_retries {
                            return Err(Some(match f {
                                ChunkFailure::Corrupt => FailReason::ChunkHashMismatch { index: i },
                                ChunkFailure::SenderError(e) => FailReason::Zenoh(e),
                                _ => FailReason::Timeout { index: i },
                            }));
                        }
                        pending.push_back(i);
                    }
                }
            }
        }
    }

    if done != reported {
        sink(TransferEvent::Progress { id, chunks: done });
    }
    let sp = spool.clone();
    let ok = tokio::task::spawn_blocking(move || sp.verify_file()).await.map_err(iofail)?.map_err(iofail)?;
    if !ok {
        spool.remove();
        return Err(Some(FailReason::FileHashMismatch));
    }
    Ok(())
}
```

  - `Querier` is `'static` when the key is an owned `String`, because `QuerierBuilder<'_, 'b>` takes `TryInto<KeyExpr<'b>>` and `String` gives `KeyExpr<'static>`. It holds a `WeakSession`, not a borrow (`api/querier.rs:76-84`). If the compiler rejects `Querier<'static>` in `JoinSet::spawn`, keep `querier` local and use `session.get(format!("{}?{CHUNK_PARAM}={i}", chunk_key(..)))` with the same QoS via `SessionGetBuilder`. That is the documented fallback, and it has identical wire behaviour.

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test -- --ignored receiver::fetch && cargo clippy --all-targets -- -D warnings`
  - Expected: 10 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/receiver/fetch.rs
git commit -m "feat(transfer): querier-based fetch with retries, resume, liveliness abort and verify

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T6: Discovery (Lane B)

**Owns:** `src/transfer/receiver/discovery.rs`

**Interfaces:**
- Produces `pub fn parse_announcement(key: &str, payload: &[u8]) -> Result<Manifest, String>` and the real body of `spawn_discovery`.
- **Events:**
  - A valid manifest Put or GET reply gives `Announced(Incoming)`, once per ID.
  - A manifest Delete gives `Failed(Cancelled{by_sender:true})`.
  - A token Delete gives `Failed(SenderGone)`.
- **zenoh APIs:** `Session::declare_subscriber`, `Session::get(..).target(QueryTarget::All).consolidation(ConsolidationMode::None).timeout(..)` and `liveliness().declare_subscriber(DISCOVERY_TOKENS)`, all from https://docs.rs/zenoh/1.10.1/zenoh/struct.Session.html.

- [ ] **Step 1: Write the failing tests** at the bottom of `discovery.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::manifest::*;
    use crate::transfer::test_support::*;

    fn valid() -> Manifest {
        manifest_from_bytes("t/disc", "d.bin", &test_bytes(100_000, 1), 64 * 1024)
    }

    #[test]
    fn parse_announcement_accepts_valid() {
        let m = valid();
        let k = manifest_key(&m.key, m.transfer_id);
        assert_eq!(parse_announcement(&k, &serde_json::to_vec(&m).unwrap()), Ok(m));
    }

    #[test]
    fn parse_announcement_rejects_mismatch_oversize_and_garbage() {
        let m = valid();
        let json = serde_json::to_vec(&m).unwrap();
        let other_id = manifest_key(&m.key, TransferId(1));
        assert!(parse_announcement(&other_id, &json).is_err());
        assert!(parse_announcement(&manifest_key("t/other", m.transfer_id), &json).is_err());
        assert!(parse_announcement(&chunk_key(&m.key, m.transfer_id), &json).is_err());
        assert!(parse_announcement(&manifest_key(&m.key, m.transfer_id), b"{nope").is_err());
        let big = vec![b' '; MAX_MANIFEST_BYTES + 1];
        assert!(parse_announcement(&manifest_key(&m.key, m.transfer_id), &big).is_err());
    }

    async fn announce(a: &Session, m: &Manifest) -> zenoh::liveliness::LivelinessToken {
        let t = a.liveliness().declare_token(token_key(&m.key, m.transfer_id)).await.unwrap();
        a.put(manifest_key(&m.key, m.transfer_id), serde_json::to_vec(m).unwrap()).await.unwrap();
        t
    }

    async fn start(b: &Arc<Session>) -> (TaskHandle, tokio::sync::mpsc::UnboundedReceiver<TransferEvent>) {
        let (sink, rx) = collecting_sink();
        let spool = std::env::temp_dir().join("zx-disc-test");
        let h = spawn_discovery(b.clone(), test_limits(&spool), sink);
        tokio::time::sleep(std::time::Duration::from_millis(300)).await; // subscriber declared
        (h, rx)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn wildcard_manifest_subscription_matches_verbatim_chunk() {
        let (a, b) = session_pair(27731).await;
        let (_h, mut rx) = start(&b).await;
        let m = valid();
        let _t = announce(&a, &m).await;
        match next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Announced { .. })).await {
            TransferEvent::Announced { manifest, direction } => {
                assert_eq!(*manifest, m);
                assert_eq!(direction, Direction::Incoming);
            }
            _ => unreachable!(),
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn discovery_finds_offer_made_before_connect() {
        let (a, b) = session_pair(27732).await;
        let m = valid();
        let mk = manifest_key(&m.key, m.transfer_id);
        let q = a.declare_queryable(mk.clone()).await.unwrap();
        let json = serde_json::to_vec(&m).unwrap();
        let (mk2, j2) = (mk.clone(), json.clone());
        let _srv = tokio::spawn(async move {
            while let Ok(query) = q.recv_async().await {
                let _ = query.reply(mk2.clone(), j2.clone()).await;
            }
        });
        wait_for_queryable(&b, &mk).await;
        let (_h, mut rx) = start(&b).await;
        let ev = next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Announced { .. })).await;
        assert!(matches!(ev, TransferEvent::Announced { manifest, .. } if manifest.transfer_id == m.transfer_id));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn discovery_reports_withdraw() {
        let (a, b) = session_pair(27733).await;
        let (_h, mut rx) = start(&b).await;
        let m = valid();
        let _t = announce(&a, &m).await;
        next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Announced { .. })).await;
        a.delete(manifest_key(&m.key, m.transfer_id)).await.unwrap();
        assert!(matches!(
            next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Failed { .. })).await,
            TransferEvent::Failed { reason: FailReason::Cancelled { by_sender: true }, .. }
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn discovery_reports_sender_gone() {
        let (a, b) = session_pair(27734).await;
        let (_h, mut rx) = start(&b).await;
        let m = valid();
        let t = announce(&a, &m).await;
        next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Announced { .. })).await;
        drop(t);
        assert!(matches!(
            next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Failed { .. })).await,
            TransferEvent::Failed { reason: FailReason::SenderGone, .. }
        ));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opens network sessions"]
    async fn discovery_announces_each_offer_once() {
        let (a, b) = session_pair(27735).await;
        let (_h, mut rx) = start(&b).await;
        let m = valid();
        let _t = announce(&a, &m).await;
        a.put(manifest_key(&m.key, m.transfer_id), serde_json::to_vec(&m).unwrap()).await.unwrap();
        next_event(&mut rx, 10, |e| matches!(e, TransferEvent::Announced { .. })).await;
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        while let Ok(ev) = rx.try_recv() {
            assert!(!matches!(ev, TransferEvent::Announced { .. }), "duplicate announcement");
        }
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test receiver::discovery; cargo test -- --ignored receiver::discovery`
  - Expected: FAIL (`parse_announcement` is not found).

- [ ] **Step 3: Implement.** Replace the stub:

```rust
//! Discover v2 offers: manifests under `**/@xfer/*/manifest` (subscription +
//! one GET for offers made before we connected) and sender liveliness.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::SystemTime;

use zenoh::query::{ConsolidationMode, QueryTarget};
use zenoh::sample::{Sample, SampleKind};
use zenoh::Session;

use super::spool::gc_spool;
use crate::transfer::event::{Direction, EventSink, FailReason, TransferEvent};
use crate::transfer::limits::{TransferLimits, DISCOVERY_QUERY_TIMEOUT, SPOOL_MAX_AGE};
use crate::transfer::manifest::{
    parse_xfer_key, Manifest, TransferId, XferLeaf, DISCOVERY_MANIFESTS, DISCOVERY_TOKENS, MAX_MANIFEST_BYTES,
};
use crate::transfer::tasks::TaskHandle;

/// Parse and fully validate an announcement; the key must agree with it.
pub fn parse_announcement(key: &str, payload: &[u8]) -> Result<Manifest, String> {
    if payload.len() > MAX_MANIFEST_BYTES {
        return Err(format!("manifest is {} bytes (limit {MAX_MANIFEST_BYTES})", payload.len()));
    }
    let (topic, id, leaf) = parse_xfer_key(key).ok_or("not a transfer key")?;
    if leaf != XferLeaf::Manifest {
        return Err("not a manifest key".into());
    }
    let m: Manifest = serde_json::from_slice(payload).map_err(|e| format!("manifest JSON: {e}"))?;
    m.validate()?;
    if m.transfer_id != id || m.key != topic {
        return Err("manifest does not match its key".into());
    }
    Ok(m)
}

fn announce(s: &Sample, seen: &mut HashSet<TransferId>, sink: &EventSink) {
    match parse_announcement(s.key_expr().as_str(), &s.payload().to_bytes()) {
        Ok(m) => {
            if seen.insert(m.transfer_id) {
                sink(TransferEvent::Announced { manifest: Box::new(m), direction: Direction::Incoming });
            }
        }
        Err(e) => tracing::debug!("ignoring transfer announcement on {}: {e}", s.key_expr()),
    }
}

pub fn spawn_discovery(session: Arc<Session>, limits: TransferLimits, sink: EventSink) -> TaskHandle {
    TaskHandle::spawn(Direction::Incoming, move |mut cancel| async move {
        let dir = limits.spool_dir.clone();
        let _ = tokio::task::spawn_blocking(move || gc_spool(&dir, SPOOL_MAX_AGE, SystemTime::now())).await;
        let manifests = match session.declare_subscriber(DISCOVERY_MANIFESTS).await {
            Ok(s) => s,
            Err(e) => return tracing::warn!("transfer discovery disabled: {e}"),
        };
        let tokens = match session.liveliness().declare_subscriber(DISCOVERY_TOKENS).await {
            Ok(s) => s,
            Err(e) => return tracing::warn!("transfer liveliness disabled: {e}"),
        };
        let mut seen: HashSet<TransferId> = HashSet::new();
        if let Ok(replies) = session
            .get(DISCOVERY_MANIFESTS)
            .target(QueryTarget::All)
            .consolidation(ConsolidationMode::None)
            .timeout(DISCOVERY_QUERY_TIMEOUT)
            .await
        {
            while let Ok(reply) = replies.recv_async().await {
                if let Ok(s) = reply.result() {
                    announce(s, &mut seen, &sink);
                }
            }
        }
        loop {
            tokio::select! {
                _ = cancel.changed() => break,
                s = manifests.recv_async() => {
                    let Ok(s) = s else { break };
                    match s.kind() {
                        SampleKind::Put => announce(&s, &mut seen, &sink),
                        SampleKind::Delete => {
                            if let Some((_, id, XferLeaf::Manifest)) = parse_xfer_key(s.key_expr().as_str()) {
                                seen.remove(&id);
                                sink(TransferEvent::Failed { id, reason: FailReason::Cancelled { by_sender: true } });
                            }
                        }
                    }
                }
                s = tokens.recv_async() => {
                    let Ok(s) = s else { break };
                    if s.kind() == SampleKind::Delete {
                        if let Some((_, id, XferLeaf::Token)) = parse_xfer_key(s.key_expr().as_str()) {
                            seen.remove(&id);
                            sink(TransferEvent::Failed { id, reason: FailReason::SenderGone });
                        }
                    }
                }
            }
        }
    })
}
```

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test receiver::discovery && cargo test -- --ignored receiver::discovery && cargo clippy --all-targets -- -D warnings`
  - Expected: 2 unit and 5 loopback tests pass.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/receiver/discovery.rs
git commit -m "feat(transfer): discover v2 offers via @xfer manifests, GET and liveliness

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T7: Registry state machine (Lane C)

**Owns:** `src/transfer/registry.rs`

**Interfaces:**
- Produces:
  - `TransferRegistry::apply(&mut self, ev)`
  - `mark_fetch_requested(&mut self, id) -> Option<Arc<Manifest>>`
  - `on_disconnected(&mut self)`
  - `gc(&mut self, now: Instant)`
  - `get(&self, id) -> Option<&TransferEntry>`
  - `remove(&mut self, id)`
  - `for_key(&self, key) -> Vec<&TransferEntry>` (newest first)
  - `badge_for_key(&self, key) -> Option<TransferBadge>`
  - `TransferEntry::progress_fraction(&self) -> Option<f32>` and `TransferEntry::status_label(&self) -> String`
  - Constants `TERMINAL_TTL` (10 min) and `MAX_ENTRIES` (200).
- **Rules:** see [State machine](#state-machine-ui-registry). In short: first `Failed` wins; `SenderGone` and `Cancelled{by_sender:true}` are ignored once Verified or Saved; other failures in that state only set `last_error`; an incoming Announced for an ID we are offering is ignored; Progress never goes backwards.

- [ ] **Step 1: Write the failing tests** at the bottom of `registry.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::test_support::{manifest_from_bytes, test_bytes};

    fn manifest(key: &str) -> Box<Manifest> {
        Box::new(manifest_from_bytes(key, "f.bin", &test_bytes(300_000, 1), 64 * 1024))
    }
    fn announced_in(m: &Manifest) -> TransferEvent {
        TransferEvent::Announced { manifest: Box::new(m.clone()), direction: Direction::Incoming }
    }

    #[test]
    fn incoming_lifecycle() {
        let m = manifest("t/a");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&m));
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Available);
        assert!(r.mark_fetch_requested(id).is_some());
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Fetching { done: 0 });
        r.apply(TransferEvent::Progress { id, chunks: 3 });
        r.apply(TransferEvent::Progress { id, chunks: 2 });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Fetching { done: 3 });
        r.apply(TransferEvent::Completed { id });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Verified);
        r.apply(TransferEvent::Saved { id, path: "/x/f.bin".into() });
        assert!(matches!(r.get(id).unwrap().status, TransferStatus::Saved { .. }));
    }

    #[test]
    fn own_offer_is_not_duplicated_as_incoming() {
        let m = manifest("t/own");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(TransferEvent::Preparing { id, key: "t/own".into(), filename: "f.bin".into(), total_size: 1 });
        r.apply(TransferEvent::Announced { manifest: m.clone(), direction: Direction::Outgoing });
        r.apply(announced_in(&m));
        let all = r.for_key("t/own");
        assert_eq!(all.len(), 1);
        assert_eq!(all[0].direction, Direction::Outgoing);
        assert_eq!(all[0].status, TransferStatus::Offering { served: 0 });
    }

    #[test]
    fn outgoing_lifecycle_and_stopped_label() {
        let m = manifest("t/out");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(TransferEvent::Preparing { id, key: "t/out".into(), filename: "f.bin".into(), total_size: 1 });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Preparing);
        r.apply(TransferEvent::Announced { manifest: m, direction: Direction::Outgoing });
        r.apply(TransferEvent::Progress { id, chunks: 7 });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Offering { served: 7 });
        r.apply(TransferEvent::Failed { id, reason: FailReason::Cancelled { by_sender: false } });
        assert_eq!(r.get(id).unwrap().status_label(), "Stopped");
    }

    #[test]
    fn sender_gone_after_verified_keeps_verified() {
        let m = manifest("t/v");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&m));
        r.mark_fetch_requested(id);
        r.apply(TransferEvent::Completed { id });
        r.apply(TransferEvent::Failed { id, reason: FailReason::SenderGone });
        r.apply(TransferEvent::Failed { id, reason: FailReason::Cancelled { by_sender: true } });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Verified);
        r.apply(TransferEvent::Failed { id, reason: FailReason::Io("disk full".into()) });
        let e = r.get(id).unwrap();
        assert_eq!(e.status, TransferStatus::Verified);
        assert!(e.last_error.as_deref().unwrap().contains("disk full"));
    }

    #[test]
    fn first_failure_wins() {
        let m = manifest("t/ff");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&m));
        r.mark_fetch_requested(id);
        r.apply(TransferEvent::Failed { id, reason: FailReason::Cancelled { by_sender: true } });
        r.apply(TransferEvent::Failed { id, reason: FailReason::SenderGone });
        assert_eq!(r.get(id).unwrap().status, TransferStatus::Failed(FailReason::Cancelled { by_sender: true }));
    }

    #[test]
    fn two_transfers_same_key_same_size_are_distinct() {
        let (a, b) = (manifest("t/same"), manifest("t/same"));
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&a));
        r.apply(announced_in(&b));
        assert_eq!(r.for_key("t/same").len(), 2);
        r.mark_fetch_requested(a.transfer_id);
        r.apply(TransferEvent::Completed { id: a.transfer_id });
        assert_eq!(r.get(b.transfer_id).unwrap().status, TransferStatus::Available);
    }

    #[test]
    fn events_for_unknown_ids_are_ignored() {
        let mut r = TransferRegistry::default();
        r.apply(TransferEvent::Progress { id: TransferId(1), chunks: 1 });
        r.apply(TransferEvent::Completed { id: TransferId(1) });
        r.apply(TransferEvent::Failed { id: TransferId(1), reason: FailReason::SenderGone });
        assert!(r.get(TransferId(1)).is_none());
    }

    #[test]
    fn disconnect_fails_active_and_drops_available() {
        let (a, b) = (manifest("t/d1"), manifest("t/d2"));
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&a));
        r.apply(announced_in(&b));
        r.mark_fetch_requested(a.transfer_id);
        r.on_disconnected();
        assert_eq!(r.get(a.transfer_id).unwrap().status, TransferStatus::Failed(FailReason::NotConnected));
        assert!(r.get(b.transfer_id).is_none());
    }

    #[test]
    fn gc_drops_old_terminal_entries() {
        let m = manifest("t/gc");
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&m));
        r.mark_fetch_requested(id);
        r.apply(TransferEvent::Failed { id, reason: FailReason::Stalled });
        r.gc(Instant::now());
        assert!(r.get(id).is_some());
        r.gc(Instant::now() + TERMINAL_TTL + std::time::Duration::from_secs(1));
        assert!(r.get(id).is_none());
    }

    #[test]
    fn progress_fraction_labels_and_badge() {
        let m = manifest("t/p"); // 300_000 bytes / 64 KiB = 5 chunks
        let id = m.transfer_id;
        let mut r = TransferRegistry::default();
        r.apply(announced_in(&m));
        assert_eq!(r.get(id).unwrap().progress_fraction(), None);
        assert!(r.get(id).unwrap().status_label().starts_with("Available"));
        r.mark_fetch_requested(id);
        r.apply(TransferEvent::Progress { id, chunks: 2 });
        assert_eq!(r.get(id).unwrap().progress_fraction(), Some(0.4));
        let badge = r.badge_for_key("t/p").unwrap();
        assert_eq!((badge.direction, badge.fraction), (Direction::Incoming, Some(0.4)));
        assert!(r.badge_for_key("t/none").is_none());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test transfer::registry`
  - Expected: FAIL to compile (`get`, `mark_fetch_requested`, … are not found).

- [ ] **Step 3: Implement.** Replace the T1 `impl TransferRegistry` block and add the methods:

```rust
pub const TERMINAL_TTL: std::time::Duration = std::time::Duration::from_secs(10 * 60);
pub const MAX_ENTRIES: usize = 200;

impl TransferEntry {
    fn is_terminal(&self) -> bool {
        matches!(self.status, TransferStatus::Failed(_) | TransferStatus::Saved { .. })
    }

    pub fn progress_fraction(&self) -> Option<f32> {
        match self.status {
            TransferStatus::Fetching { done } => {
                Some(if self.chunk_count == 0 { 1.0 } else { done as f32 / self.chunk_count as f32 })
            }
            TransferStatus::Verified | TransferStatus::Saved { .. } => Some(1.0),
            _ => None,
        }
    }

    pub fn status_label(&self) -> String {
        let size = crate::transfer::format_size(self.total_size as usize);
        let base = match (&self.status, self.direction) {
            (TransferStatus::Preparing, _) => "Preparing (hashing file)…".to_string(),
            (TransferStatus::Offering { served }, _) => format!("Offering · {served} chunk requests served"),
            (TransferStatus::Available, _) => format!("Available · {size}"),
            (TransferStatus::Fetching { done }, _) => format!("Fetching {done}/{} chunks · {size}", self.chunk_count),
            (TransferStatus::Verified, _) if self.kind == EntryKind::LegacyV1 => {
                "Complete (v1, not verified) · ready to save".to_string()
            }
            (TransferStatus::Verified, _) => "Verified (BLAKE3) · ready to save".to_string(),
            (TransferStatus::Saved { path }, _) => format!("Saved to {}", path.display()),
            (TransferStatus::Failed(FailReason::Cancelled { by_sender: false }), Direction::Outgoing) => {
                "Stopped".to_string()
            }
            (TransferStatus::Failed(r), _) => format!("Failed: {r}"),
        };
        match &self.last_error {
            Some(e) => format!("{base} (last error: {e})"),
            None => base,
        }
    }
}

impl TransferRegistry {
    pub fn get(&self, id: TransferId) -> Option<&TransferEntry> {
        self.entries.get(&id)
    }

    pub fn remove(&mut self, id: TransferId) {
        self.entries.remove(&id);
    }

    pub fn apply(&mut self, ev: TransferEvent) {
        let now = Instant::now();
        match ev {
            TransferEvent::Preparing { id, key, filename, total_size } => {
                self.entries.entry(id).or_insert_with(|| TransferEntry {
                    id,
                    kind: EntryKind::V2,
                    direction: Direction::Outgoing,
                    key,
                    filename,
                    total_size,
                    chunk_count: 0,
                    manifest: None,
                    status: TransferStatus::Preparing,
                    last_error: None,
                    updated: now,
                });
            }
            TransferEvent::Announced { manifest, direction } => {
                let id = manifest.transfer_id;
                match self.entries.get_mut(&id) {
                    Some(e) if e.direction == Direction::Outgoing && direction == Direction::Outgoing => {
                        e.total_size = manifest.total_size;
                        e.chunk_count = manifest.chunk_count;
                        e.filename = manifest.filename.clone();
                        e.manifest = Some(Arc::from(manifest));
                        e.status = TransferStatus::Offering { served: 0 };
                        e.updated = now;
                    }
                    Some(_) => {} // known already (incl. the echo of our own offer)
                    None => {
                        let status = match direction {
                            Direction::Incoming => TransferStatus::Available,
                            Direction::Outgoing => TransferStatus::Offering { served: 0 },
                        };
                        self.entries.insert(
                            id,
                            TransferEntry {
                                id,
                                kind: EntryKind::V2,
                                direction,
                                key: manifest.key.clone(),
                                filename: manifest.filename.clone(),
                                total_size: manifest.total_size,
                                chunk_count: manifest.chunk_count,
                                manifest: Some(Arc::from(manifest)),
                                status,
                                last_error: None,
                                updated: now,
                            },
                        );
                    }
                }
            }
            TransferEvent::Progress { id, chunks } => {
                if let Some(e) = self.entries.get_mut(&id) {
                    match e.status {
                        TransferStatus::Offering { .. } => e.status = TransferStatus::Offering { served: chunks },
                        TransferStatus::Fetching { done } => {
                            let c = u32::try_from(chunks).unwrap_or(u32::MAX);
                            e.status = TransferStatus::Fetching { done: done.max(c) };
                        }
                        _ => return,
                    }
                    e.updated = now;
                }
            }
            TransferEvent::Completed { id } => {
                if let Some(e) = self.entries.get_mut(&id) {
                    if matches!(e.status, TransferStatus::Fetching { .. }) {
                        e.status = TransferStatus::Verified;
                        e.updated = now;
                    }
                }
            }
            TransferEvent::Saved { id, path } => {
                if let Some(e) = self.entries.get_mut(&id) {
                    e.status = TransferStatus::Saved { path };
                    e.last_error = None;
                    e.updated = now;
                }
            }
            TransferEvent::Failed { id, reason } => {
                if let Some(e) = self.entries.get_mut(&id) {
                    match e.status {
                        TransferStatus::Failed(_) => {}
                        TransferStatus::Verified | TransferStatus::Saved { .. } => {
                            if !matches!(reason, FailReason::SenderGone | FailReason::Cancelled { by_sender: true }) {
                                e.last_error = Some(reason.to_string());
                            }
                        }
                        _ => {
                            e.status = TransferStatus::Failed(reason);
                            e.updated = now;
                        }
                    }
                }
            }
        }
    }

    /// UI clicked Fetch/Retry. Returns the manifest to send to the worker.
    pub fn mark_fetch_requested(&mut self, id: TransferId) -> Option<Arc<Manifest>> {
        let e = self.entries.get_mut(&id)?;
        let allowed = e.direction == Direction::Incoming
            && e.kind == EntryKind::V2
            && match &e.status {
                TransferStatus::Available => true,
                TransferStatus::Failed(r) => r.is_retryable(),
                _ => false,
            };
        if !allowed {
            return None;
        }
        e.status = TransferStatus::Fetching { done: 0 };
        e.last_error = None;
        e.updated = Instant::now();
        e.manifest.clone()
    }

    /// Worker sessions closed: active work failed; offers we only saw are
    /// dropped (discovery re-announces them after reconnect).
    pub fn on_disconnected(&mut self) {
        self.entries.retain(|_, e| !(e.direction == Direction::Incoming && e.status == TransferStatus::Available));
        for e in self.entries.values_mut() {
            if matches!(e.status, TransferStatus::Preparing | TransferStatus::Offering { .. } | TransferStatus::Fetching { .. }) {
                e.status = TransferStatus::Failed(FailReason::NotConnected);
                e.updated = Instant::now();
            }
        }
    }

    pub fn gc(&mut self, now: Instant) {
        self.entries.retain(|_, e| !(e.is_terminal() && now.saturating_duration_since(e.updated) > TERMINAL_TTL));
        if self.entries.len() > MAX_ENTRIES {
            let mut terminal: Vec<(Instant, TransferId)> =
                self.entries.values().filter(|e| e.is_terminal()).map(|e| (e.updated, e.id)).collect();
            terminal.sort();
            for (_, id) in terminal.into_iter().take(self.entries.len() - MAX_ENTRIES) {
                self.entries.remove(&id);
            }
        }
    }

    pub fn for_key(&self, key: &str) -> Vec<&TransferEntry> {
        let mut v: Vec<&TransferEntry> = self.entries.values().filter(|e| e.key == key).collect();
        v.sort_by(|a, b| b.updated.cmp(&a.updated).then(a.id.cmp(&b.id)));
        v
    }

    pub fn badge_for_key(&self, key: &str) -> Option<TransferBadge> {
        let e = *self.for_key(key).first()?;
        Some(TransferBadge { direction: e.direction, fraction: e.progress_fraction(), label: e.status_label() })
    }
}
```

  - `format_size` takes `usize` (P1 `transfer.rs`, re-exported through `export`). On 64-bit targets the cast is lossless.

- [ ] **Step 4: Run the tests.**
  - Run: `cargo test transfer::registry && cargo clippy --all-targets -- -D warnings`
  - Expected: 10 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/transfer/registry.rs
git commit -m "feat(transfer): registry state machine as the single source of transfer state

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T8: Wire the registry into events, tree and topic view (Lane C)

**Owns:** `src/events/mod.rs`, `src/events/ingest.rs`, `src/types/tree.rs`, `src/ui/topic_tree.rs`

**Interfaces:**
- Consumes `TransferRegistry::{apply, on_disconnected, gc, badge_for_key}` and `TransferBadge`.
- **Removes:**
  - `TransferState` (pre-P1 `types.rs:47-61`).
  - `ZenohNode.transfer` (77-78, 93).
  - `ZenohNode::record_chunk` (145-165).
  - The test `transfer_state_resets_on_new_generation` (665-686).
  - `render_transfer_progress` (pre-P1 `topic_tree.rs:98-139`).
- **Produces:** `fn render_transfer_badge(ui: &mut egui::Ui, b: &TransferBadge, dark_mode: bool, secondary: egui::Color32)` (private to `topic_tree.rs`).
- **Intermediate state:** after T8, v1 chunk samples are still stored in the export store, but they are neither shown in the tree nor saveable. T10 then removes them (Option B) or routes them to the registry (Option A).

- [ ] **Step 1: Write the failing tests** in the `src/events/mod.rs` test module (P1 T3 created it):

```rust
    fn announced(key: &str) -> (crate::transfer::manifest::TransferId, ZenohEvent) {
        use crate::transfer::event::{Direction, TransferEvent};
        let m = crate::transfer::test_support::manifest_from_bytes(key, "f.bin", b"hello", 64 * 1024);
        (m.transfer_id, ZenohEvent::Transfer(TransferEvent::Announced { manifest: Box::new(m), direction: Direction::Incoming }))
    }

    #[test]
    fn announced_transfer_creates_tree_node_without_message_count() {
        let (mut app, tx) = ZenohExplorer::test_app();
        let (id, ev) = announced("files/big");
        tx.send(ev).unwrap();
        app.process_events();
        assert!(app.transfers.get(id).is_some());
        let tree = app.browse_tree.read().unwrap();
        let node = &tree.children["files"].children["big"];
        assert_eq!(node.message_count, 0);
        assert!(app.messages.is_empty());
    }

    #[test]
    fn transfer_events_bypass_rate_limit_and_dedup() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(0);
        let ids: Vec<_> = (0..5)
            .map(|i| {
                let (id, ev) = announced(&format!("files/f{i}"));
                tx.send(ev).unwrap();
                id
            })
            .collect();
        app.process_events();
        assert!(ids.iter().all(|id| app.transfers.get(*id).is_some()));
        assert_eq!((app.rate_limit_drops, app.messages_deduped), (0, 0));
    }

    #[test]
    fn disconnect_fails_active_transfers() {
        use crate::transfer::event::FailReason;
        use crate::transfer::registry::TransferStatus;
        let (mut app, tx) = ZenohExplorer::test_app();
        let (id, ev) = announced("files/active");
        tx.send(ev).unwrap();
        app.process_events();
        app.transfers.mark_fetch_requested(id);
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert_eq!(app.transfers.get(id).unwrap().status, TransferStatus::Failed(FailReason::NotConnected));
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test events::`
  - Expected: `announced_transfer_creates_tree_node_without_message_count` fails with a missing `files` key, and `disconnect_fails_active_transfers` fails because the status is still `Fetching`.

- [ ] **Step 3: Update `events/mod.rs`.**
  - Replace the T1 arm with:

```rust
ZenohEvent::Transfer(ev) => {
    if let crate::transfer::event::TransferEvent::Announced { manifest, .. } = &ev {
        if let Ok(mut tree) = self.browse_tree.write() {
            tree.insert_path(&manifest.key);
        }
        self.tree_version = self.tree_version.wrapping_add(1);
    }
    self.transfers.apply(ev);
}
```

  - In the `Disconnected` arm (pre-P1 `events.rs:103-108`), add `self.transfers.on_disconnected();`.
  - After the event loop, add `self.transfers.gc(std::time::Instant::now());`.

- [ ] **Step 4: Remove the tree transfer state.**
  - In `src/types/tree.rs`, delete `TransferState`, the `transfer` field and its initialiser, `record_chunk`, and the `transfer_state_resets_on_new_generation` test.
  - In `src/events/ingest.rs` `add_message_to_browse_tree` (pre-P1 `events.rs:232-243`), replace the chunk branch body with a plain skip:

```rust
if crate::transfer::parse_chunk_key(&message.key).is_some() {
    return; // v1 chunk traffic: not a tree node (P4 T10 decides its fate)
}
```

- [ ] **Step 5: Update `ui/topic_tree.rs`.**
  - Delete `render_transfer_progress` (pre-P1 98-139) and add:

```rust
/// Inline transfer badge for a tree row, from the transfer registry.
fn render_transfer_badge(
    ui: &mut egui::Ui,
    b: &crate::transfer::registry::TransferBadge,
    dark_mode: bool,
    secondary: egui::Color32,
) {
    if let Some(f) = b.fraction {
        ui.add(egui::ProgressBar::new(f).desired_width(120.0).show_percentage());
    }
    let color = if b.fraction == Some(1.0) {
        if dark_mode { ExplorerColors::DARK_SUCCESS } else { ExplorerColors::SUCCESS }
    } else {
        secondary
    };
    ui.label(RichText::new(&b.label).size(TEXT_SMALL_SIZE).color(color));
}
```

  - **Leaf rows** (pre-P1 671-719). At the top of the leaf closure, compute `let badge = self.transfers.badge_for_key(&full_path);`. Then:
    - The icon becomes `match badge.as_ref().map(|b| b.direction) { Some(Direction::Incoming) => "📥", Some(Direction::Outgoing) => "📤", None => leaf_icon(…) }`.
    - `if let Some(t) = &node.transfer {…}` becomes `if let Some(b) = &badge { render_transfer_badge(ui, b, dark_mode, secondary_color) }`.
    - `node.transfer.is_none()` becomes `badge.is_none()`.
    - `exportable` keeps only the `payload_store.contains_key` term.
  - **Branch rows** (pre-P1 744-792): `transfer_snapshot` becomes `let badge = self.transfers.badge_for_key(&full_path);`, rendered with `render_transfer_badge`.
  - **`show_topic_details`:**
    - In the Save availability block (pre-P1 316-340), the `None =>` arm becomes `None => (false, None, "No payload stored yet".to_string())`, so the direct payload is the only source.
    - Delete the chunk-info block (pre-P1 421-464). T9 adds the Transfers panel.
  - Add `use crate::transfer::event::Direction;`.

- [ ] **Step 6: Run the tests.**
  - Run: `cargo test && cargo clippy --all-targets -- -D warnings && grep -rn 'TransferState\|record_chunk\|chunk_progress' src/types src/ui src/events`
  - Expected: tests pass, and grep prints nothing.

- [ ] **Step 7: Commit.**

```bash
git add src/events/mod.rs src/events/ingest.rs src/types/tree.rs src/ui/topic_tree.rs
git commit -m "refactor(ui): transfer state comes only from the registry; drop tree TransferState

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T9: Transfers panel and Offer action (Lane C)

**Owns:** `src/ui/transfers.rs` (new), `src/ui/mod.rs`, `src/ui/topic_tree.rs`, `src/ui/publish.rs`, `tests/snapshots/publish_light.png` (P3 T16's reference image; Step 5 adds a row to the Publish tab it renders)

**Interfaces:**
- Produces:
  - `pub enum TransferAction { Fetch(TransferId), Cancel(TransferId), Save(TransferId), Dismiss(TransferId) }`, with a private `label(&self, e: &TransferEntry) -> (&'static str, &'static str)` returning the button text and the hover text.
  - `pub fn offer_disabled_reason(key: &str, connected: bool) -> Option<String>`
  - `pub fn available_actions(e: &TransferEntry, max_file_size: u64) -> Vec<TransferAction>`
  - `pub fn transfer_panel(ui: &mut egui::Ui, entries: &[TransferEntry], max_file_size: u64) -> Vec<TransferAction>`
  - `impl ZenohExplorer { pub(crate) fn handle_transfer_action(&mut self, a: TransferAction); pub(crate) fn offer_file_dialog(&mut self) }`
- **Button labels** are plain ASCII so egui_kittest can find them: "Fetch", "Retry", "Cancel", "Stop offering", "Save…", "Dismiss".
- **egui_kittest 0.36:** `Harness::new_ui_state(app: impl FnMut(&mut Ui, &mut State), state)`, `get_by_label`, `Node::click`, `run()` and `state()`, from https://docs.rs/egui_kittest/0.36.2/egui_kittest/struct.Harness.html.
- **Save dialog:** the `rfd::AsyncFileDialog` future is created on the UI thread and awaited on a helper thread, following rfd's guidance that dialogs be *spawned* from the main thread (`rfd-0.17.2/src/lib.rs:83-86`).
  - If P3 already added a helper for this (`grep -rn 'AsyncFileDialog' src/ui src/app`), reuse it instead of the thread below.
  - If the compiler says the dialog future is not `Send`, the P3 helper is mandatory.

- [ ] **Step 1: Write the failing tests** in `src/ui/transfers.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::transfer::event::FailReason;
    use crate::transfer::registry::{EntryKind, TransferStatus};
    use egui_kittest::kittest::Queryable;
    use std::time::Instant;

    fn entry(direction: Direction, status: TransferStatus, total: u64) -> TransferEntry {
        let m = crate::transfer::test_support::manifest_from_bytes("t/x", "x.bin", b"abc", 64 * 1024);
        TransferEntry {
            id: m.transfer_id,
            kind: EntryKind::V2,
            direction,
            key: "t/x".into(),
            filename: "x.bin".into(),
            total_size: total,
            chunk_count: 1,
            manifest: Some(std::sync::Arc::new(m)),
            status,
            last_error: None,
            updated: Instant::now(),
        }
    }

    #[test]
    fn action_rules() {
        let a = |d, s, t| available_actions(&entry(d, s, t), 1000);
        use Direction::*;
        assert!(matches!(a(Incoming, TransferStatus::Available, 10)[..], [TransferAction::Fetch(_), TransferAction::Dismiss(_)]));
        assert!(matches!(a(Incoming, TransferStatus::Available, 5000)[..], [TransferAction::Dismiss(_)]), "over max: no Fetch");
        assert!(matches!(a(Incoming, TransferStatus::Fetching { done: 0 }, 10)[..], [TransferAction::Cancel(_)]));
        assert!(matches!(a(Incoming, TransferStatus::Verified, 10)[..], [TransferAction::Save(_), TransferAction::Dismiss(_)]));
        assert!(matches!(a(Incoming, TransferStatus::Failed(FailReason::Stalled), 10)[..], [TransferAction::Fetch(_), TransferAction::Dismiss(_)]));
        assert!(matches!(a(Incoming, TransferStatus::Failed(FailReason::SenderGone), 10)[..], [TransferAction::Dismiss(_)]));
        assert!(matches!(a(Outgoing, TransferStatus::Offering { served: 0 }, 10)[..], [TransferAction::Cancel(_)]));
        assert!(matches!(a(Outgoing, TransferStatus::Failed(FailReason::OfferExpired), 10)[..], [TransferAction::Dismiss(_)]));
    }

    #[test]
    fn fetch_button_emits_fetch_action() {
        let e = entry(Direction::Incoming, TransferStatus::Available, 10);
        let id = e.id;
        let mut harness = egui_kittest::Harness::new_ui_state(
            |ui, acts: &mut Vec<TransferAction>| acts.extend(transfer_panel(ui, std::slice::from_ref(&e), 1000)),
            Vec::new(),
        );
        harness.get_by_label("Fetch").click();
        harness.run();
        assert!(harness.state().contains(&TransferAction::Fetch(id)));
    }

    #[test]
    fn offer_button_disabled_for_bad_key() {
        assert!(offer_disabled_reason("demo/*", true).is_some());
        assert!(offer_disabled_reason("@/x", true).is_some());
        assert!(offer_disabled_reason("demo/file", false).is_some(), "disconnected");
        assert!(offer_disabled_reason("demo/file", true).is_none());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.**
  - Run: `cargo test ui::transfers`
  - Expected: FAIL to compile.

- [ ] **Step 3: Implement `src/ui/transfers.rs`** above the tests, and add `pub mod transfers;` to `src/ui/mod.rs`:

```rust
//! Transfers panel (Topic Details) and transfer actions.

use egui::RichText;

use crate::app::{UiAlert, ZenohExplorer};
use crate::transfer::event::{Direction, TransferCommand};
use crate::transfer::manifest::TransferId;
use crate::transfer::registry::{EntryKind, TransferEntry, TransferStatus};
use crate::types::{ZenohCommand, TEXT_SMALL_SIZE};
use crate::ui::file_jobs::FileJobsUI; // save_topic_to_file (P3 T3)

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TransferAction {
    Fetch(TransferId),
    Cancel(TransferId),
    Save(TransferId),
    Dismiss(TransferId),
}

impl TransferAction {
    fn label(&self, e: &TransferEntry) -> (&'static str, &'static str) {
        match (self, e.direction, &e.status) {
            (Self::Fetch(_), _, TransferStatus::Failed(_)) => ("Retry", "Fetch again; verified chunks are kept"),
            (Self::Fetch(_), _, _) => ("Fetch", "Download, verify with BLAKE3, then offer Save"),
            (Self::Cancel(_), Direction::Outgoing, _) => ("Stop offering", "Withdraw this offer from the network"),
            (Self::Cancel(_), _, _) => ("Cancel", "Stop fetching and delete the partial file"),
            (Self::Save(_), _, _) => ("Save…", "Choose where to save the verified file"),
            (Self::Dismiss(_), _, _) => ("Dismiss", "Remove from this list"),
        }
    }
}

pub fn available_actions(e: &TransferEntry, max_file_size: u64) -> Vec<TransferAction> {
    use TransferAction::*;
    let id = e.id;
    match (e.direction, &e.status) {
        (Direction::Outgoing, TransferStatus::Preparing | TransferStatus::Offering { .. }) => vec![Cancel(id)],
        (Direction::Outgoing, _) => vec![Dismiss(id)],
        (Direction::Incoming, TransferStatus::Available) if e.total_size <= max_file_size => vec![Fetch(id), Dismiss(id)],
        (Direction::Incoming, TransferStatus::Available) => vec![Dismiss(id)],
        (Direction::Incoming, TransferStatus::Fetching { .. }) if e.kind == EntryKind::V2 => vec![Cancel(id)],
        (Direction::Incoming, TransferStatus::Fetching { .. }) => vec![],
        (Direction::Incoming, TransferStatus::Verified) => vec![Save(id), Dismiss(id)],
        (Direction::Incoming, TransferStatus::Failed(r)) if r.is_retryable() && e.kind == EntryKind::V2 => {
            vec![Fetch(id), Dismiss(id)]
        }
        (Direction::Incoming, _) => vec![Dismiss(id)],
    }
}

/// Why the Offer button is disabled, or None when it is enabled.
pub fn offer_disabled_reason(key: &str, connected: bool) -> Option<String> {
    if !connected {
        return Some("Connect first".into());
    }
    crate::transfer::manifest::offer_key_error(key)
}

pub fn transfer_panel(ui: &mut egui::Ui, entries: &[TransferEntry], max_file_size: u64) -> Vec<TransferAction> {
    let mut actions = Vec::new();
    for e in entries {
        ui.group(|ui| {
            ui.horizontal(|ui| {
                let arrow = if e.direction == Direction::Incoming { "📥" } else { "📤" };
                ui.label(RichText::new(format!("{arrow} {}", e.filename)).strong());
                ui.label(RichText::new(crate::transfer::format_size(e.total_size as usize)).size(TEXT_SMALL_SIZE));
            });
            if let Some(f) = e.progress_fraction() {
                ui.add(egui::ProgressBar::new(f).desired_width(240.0).show_percentage());
            }
            ui.label(RichText::new(e.status_label()).size(TEXT_SMALL_SIZE));
            ui.horizontal(|ui| {
                for a in available_actions(e, max_file_size) {
                    let (text, hint) = a.label(e);
                    if ui.button(text).on_hover_text(hint).clicked() {
                        actions.push(a);
                    }
                }
            });
        });
    }
    actions
}

impl ZenohExplorer {
    fn send_transfer(&mut self, cmd: TransferCommand) {
        let sent = self.command_sender.as_ref().is_some_and(|s| s.send(ZenohCommand::Transfer(cmd)).is_ok());
        if !sent {
            self.ui_alert = Some(UiAlert::Error("Transfer command failed: worker not running".into()));
        }
    }

    pub(crate) fn handle_transfer_action(&mut self, action: TransferAction) {
        match action {
            TransferAction::Fetch(id) => {
                if let Some(m) = self.transfers.mark_fetch_requested(id) {
                    let max = u64::from(self.transfer_max_file_gib) << 30;
                    self.send_transfer(TransferCommand::Fetch { manifest: Box::new((*m).clone()), max_file_size: max });
                }
            }
            TransferAction::Cancel(id) => self.send_transfer(TransferCommand::Cancel { id }),
            TransferAction::Dismiss(id) => self.transfers.remove(id),
            TransferAction::Save(id) => {
                let Some(e) = self.transfers.get(id) else { return };
                if e.kind == EntryKind::LegacyV1 {
                    let key = e.key.clone();
                    self.save_topic_to_file(&key);
                    return;
                }
                let dialog = rfd::AsyncFileDialog::new().set_file_name(e.filename.clone()).save_file();
                let tx = self.command_sender.clone();
                std::thread::spawn(move || {
                    let Ok(rt) = tokio::runtime::Builder::new_current_thread().build() else { return };
                    if let (Some(handle), Some(tx)) = (rt.block_on(dialog), tx) {
                        let dest = handle.path().to_path_buf();
                        let _ = tx.send(ZenohCommand::Transfer(TransferCommand::Save { id, dest }));
                    }
                });
            }
        }
    }

    pub(crate) fn offer_file_dialog(&mut self) {
        let key = self.publish_key.clone();
        let dialog = rfd::AsyncFileDialog::new().pick_file();
        let tx = self.command_sender.clone();
        std::thread::spawn(move || {
            let Ok(rt) = tokio::runtime::Builder::new_current_thread().build() else { return };
            if let (Some(handle), Some(tx)) = (rt.block_on(dialog), tx) {
                let path = handle.path().to_path_buf();
                let _ = tx.send(ZenohCommand::Transfer(TransferCommand::Offer { key, path }));
            }
        });
    }
}
```

  - `rfd::FileHandle::path()` exists on desktop targets in rfd 0.17.

- [ ] **Step 4: Add the Transfers section to Topic Details.** In `src/ui/topic_tree.rs` `show_topic_details`, directly after P1 T24's received-count row, `"Received: {n} (since app start)"` (pre-P1 416-419), add the block below. Find that row with `grep -n 'Received:' src/ui/topic_tree.rs`. P1 T24 renamed this figure (it read `Messages:` before P1), and P1's "Renamed UI strings" table says later plans must not look up or paste back the older wording.

```rust
let entries: Vec<crate::transfer::registry::TransferEntry> =
    self.transfers.for_key(topic).into_iter().cloned().collect();
if !entries.is_empty() {
    ui.separator();
    ui.horizontal(|ui| {
        ui.label(RichText::new("Transfers").strong());
        ui.add(egui::DragValue::new(&mut self.transfer_max_file_gib).range(1..=4096).suffix(" GB max"))
            .on_hover_text("Largest incoming file this app will fetch");
    });
    let max = u64::from(self.transfer_max_file_gib) << 30;
    for a in crate::ui::transfers::transfer_panel(ui, &entries, max) {
        self.handle_transfer_action(a);
    }
}
```

  Cloning is cheap because `manifest` is an `Arc`.

  The suffix reads " GB", not " GiB". `format_size` prints sizes with the labels "GB" and "MB" (binary divisors, so 1 "GB" is the `<< 30` above), and the transfer panel shows each file's size that way. T11's README states this cap as "16 GB". One limit uses one unit within one flow (the F-T13-4 class).

- [ ] **Step 5: Update the Publish tab** (`src/ui/publish.rs`).
  - Below the Publish button (pre-P1 208-254), add:

```rust
ui.horizontal(|ui| {
    let connected = matches!(self.connection_status, ConnectionStatus::Connected);
    let reason = crate::ui::transfers::offer_disabled_reason(&self.publish_key, connected);
    let resp = ui.add_enabled(reason.is_none(), egui::Button::new("📤 Offer File as Transfer…"));
    let resp = match reason {
        Some(r) => resp.on_disabled_hover_text(r),
        None => resp.on_hover_text("Receivers fetch it on demand; BLAKE3-verified and resumable"),
    };
    if resp.clicked() {
        self.offer_file_dialog();
    }
});
```

  - **Import size check (UI review F-T15-5).** P3 T2's `dialogs::spawn_import(ctx, max_bytes)` refuses a picked file over `max_bytes` before reading it, and P3 T12 calls it with `dialogs::IMPORT_MAX_BYTES` (4 GiB). A file between 64 MiB and 4 GiB would then be read into memory only to be refused by `publish_shape` (T10). Pass the plain-publish cap instead, so Import refuses it up front.
    - Test first. Add to the `ui::publish::tests` module (P3 T12 created it):

```rust
    #[test]
    fn import_cap_is_plain_publish_max() {
        let plain = crate::transfer::limits::PLAIN_PUBLISH_MAX as u64;
        assert_eq!(import_cap(), plain);
        assert!(import_cap() <= crate::dialogs::IMPORT_MAX_BYTES);
    }
```

    - Run `cargo test ui::publish::tests::import_cap_is_plain_publish_max`. Expected: FAIL to compile (no `import_cap`).
    - Implement it in `src/ui/publish.rs`, outside the `impl` block:

```rust
/// Import reads the whole file into memory, and a plain publish sends it in one
/// put, so the smaller of the two caps applies (UI review F-T15-5).
pub(crate) fn import_cap() -> u64 {
    crate::dialogs::IMPORT_MAX_BYTES.min(crate::transfer::limits::PLAIN_PUBLISH_MAX as u64)
}
```

      `IMPORT_MAX_BYTES` stays referenced, so the binary crate has no dead-code warning under `clippy -D warnings`.
    - In the Import button block from P3 T12, change the `spawn_import` call to `crate::dialogs::spawn_import(ui.ctx().clone(), import_cap())`, and add `.on_hover_text("Files up to 64 MB. For larger files use Offer File as Transfer")` to the button's response chain, next to its existing `.on_disabled_hover_text(…)`.
      - Write the size as "64 MB", not "64 MiB". The refusal the user sees next comes from P3's `import_size_error`, which prints the cap with `format_size` ("… up to 64.00 MB into memory"), and T10's `TooLarge` message does the same. One limit must use one unit within one flow (the F-T13-4 class). T11's Help text uses "64 MB" for the same reason.
      - The refusal text itself is not changed here: it is built in `dialogs.rs` (P3, not owned by P4) and reaches `apply_import` as a plain `String`, so adding the Offer hint there would mean classifying the error by its text. The hover text above carries the hint instead.
    - Check: `grep -A3 'spawn_import(' src/ui/publish.rs` shows `import_cap()` and no `IMPORT_MAX_BYTES`. The `-A3` is needed because `cargo fmt` splits a call this long across lines: P3 T12's call becomes `spawn_import(` / `ui.ctx().clone(),` / `crate::dialogs::IMPORT_MAX_BYTES,`, so a one-line pattern such as `spawn_import(.*IMPORT_MAX_BYTES` never matches, before or after this step.
  - Because Import now refuses a file over `PLAIN_PUBLISH_MAX` before reading it, the plain Publish button needs no separate size condition. T10's `publish_shape` → `TooLarge` remains the worker-side guard.

- [ ] **Step 6: Run the tests.**
  - Run: `cargo test ui::transfers && cargo test ui::publish::tests::import_cap_is_plain_publish_max && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check && grep -A3 'spawn_import(' src/ui/publish.rs | grep -q 'import_cap()' && ! grep -A3 'spawn_import(' src/ui/publish.rs | grep -q 'IMPORT_MAX_BYTES'`
  - Expected: 3 `ui::transfers` tests and `import_cap_is_plain_publish_max` pass, everything else is green, and the whole chain exits 0. The last two greps run on the formatted file: the call passes `import_cap()`, and `IMPORT_MAX_BYTES` is not within three lines of `spawn_import(` (the `!` turns grep's no-match exit status 1 into success).
  - **Snapshot reference (macOS, needs a wgpu adapter).** Step 5's Offer row changes the Publish tab that P3 T16's `publish_light` snapshot renders at 1000×700, so the existing reference no longer matches.
    - Run: `cargo test ui::tests::snapshots::publish_light -- --ignored`. Expected: FAIL, and it writes `tests/snapshots/publish_light.diff.png`.
    - Open the diff image. The only change must be the new "📤 Offer File as Transfer…" row (disabled, because `test_app` is not connected) below the Publish button. Any other difference is a defect; fix it before regenerating.
    - Run: `UPDATE_SNAPSHOTS=1 cargo test ui::tests::snapshots::publish_light -- --ignored && cargo test ui::tests::snapshots -- --ignored`. Expected: the reference is rewritten, then all 3 snapshot tests pass.
    - Do not commit the `*.new.png` or `*.diff.png` files.

- [ ] **Step 7: Commit.**

```bash
git add src/ui/transfers.rs src/ui/mod.rs src/ui/topic_tree.rs src/ui/publish.rs tests/snapshots/publish_light.png
git commit -m "feat(ui): transfers panel with fetch/cancel/save and Offer File as Transfer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T10: v1 `__chunk`: remove sending, then remove or keep receiving (Lane D)

**Owns:** `src/transfer/export.rs`, `src/worker/publish.rs`, `src/events/ingest.rs`, `src/transfer/registry.rs`

**Before starting:** read the user's answer to **Q1**. If there is no answer, implement **Option B** (the recommended default). Do exactly one option, and state which one in the commit message.

**Interfaces:**
- Produces `PublishShape { Single, TooLarge }`, which replaces `Chunked`.
- Option A adds `TransferRegistry::record_legacy_chunk(&mut self, topic: &str, total_size: u64, total_chunks: u32, index: u32, filename: Option<&str>)`.

- [ ] **Step 1 (both options): Remove v1 sending.**
  - In the `src/worker/publish.rs` tests, replace P1's `publish_shape_chunks_above_chunk_size` with:

```rust
    #[test]
    fn publish_shape_rejects_above_plain_max() {
        let c = crate::transfer::limits::PLAIN_PUBLISH_MAX;
        assert_eq!(publish_shape(0), PublishShape::Single);
        assert_eq!(publish_shape(c), PublishShape::Single);
        assert_eq!(publish_shape(c + 1), PublishShape::TooLarge);
    }
```

  - Run `cargo test publish_shape`. Expected: FAIL to compile (no `TooLarge`).
  - Implement it:

```rust
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PublishShape {
    Single,
    TooLarge,
}

/// Plain publishes are capped; larger files use a v2 transfer.
pub(crate) fn publish_shape(len: usize) -> PublishShape {
    if len > crate::transfer::limits::PLAIN_PUBLISH_MAX {
        PublishShape::TooLarge
    } else {
        PublishShape::Single
    }
}
```

  - In `handle_publish`, replace the whole `PublishShape::Chunked { .. }` branch (the chunk loop, pre-P1 `zenoh_worker.rs:518-574`) with:

```rust
PublishShape::TooLarge => {
    let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
        op: FailedOp::Publish,
        error: format!(
            "{} is over the {} plain-publish limit; use \"Offer File as Transfer\"",
            crate::transfer::format_size(payload_len),
            crate::transfer::format_size(crate::transfer::limits::PLAIN_PUBLISH_MAX)
        ),
    });
}
```

  - Run: `cargo test publish_shape && grep -n 'Chunked\|__chunk' src/worker/publish.rs`
  - Expected: pass, and grep prints nothing.

- [ ] **Step 2 — Option B (recommended): remove v1 receiving.**
  - Write the failing test in the `src/events/ingest.rs` tests:

```rust
    #[test]
    fn v1_chunk_keys_are_ordinary_topics() {
        let (mut app, tx) = ZenohExplorer::test_app();
        let m = ZenohMessage::new_with_bytes(
            "t/__chunk/3/1/0".into(), "abc".into(), b"abc".to_vec(), "text/plain".into(),
            chrono::Utc::now(), MessageType::Subscribe, false, MessageSource::MonitorSession,
        );
        tx.send(ZenohEvent::MessageReceived(m)).unwrap();
        app.process_events();
        assert!(app.browse_tree.read().unwrap().children["t"].children.contains_key("__chunk"));
        assert!(app.payload_store.read().unwrap().contains_key("t/__chunk/3/1/0"));
        assert_eq!(app.messages.len(), 1);
    }
```

  - Run `cargo test v1_chunk_keys`. Expected: FAIL, because the chunk key is skipped.
  - In `events/ingest.rs`, delete the T8 skip in `add_message_to_browse_tree`. In `process_single_message`, delete `is_chunk` (pre-P1 `events.rs:218-219`) so that `let display = !self.paused_keys.contains(&message.key);`.
  - In `src/transfer/export.rs`, delete:
    - `CHUNK_SIZE`, `ChunkMeta` and its `is_sane`, `parse_chunk_key`
    - the chunk branch of `insert_payload`
    - `ChunkProgress`, `chunk_progress`
    - `STALE_TRANSFER_AGE`, `gc_stale_transfers`
    - every test that uses them, including P3 T13's `stored_filename_from_first_chunk_of_newest_group` (it builds `__chunk` keys from `CHUNK_SIZE`). Keep `stored_filename_direct`.
  - Also in `export.rs`:
    - In `insert_payload`, keep only the plain-key branch, and drop the `!k.contains("/__chunk/")` filters so every entry counts toward the caps.
    - Reduce `get_payload_for_export` to its direct-lookup part:

```rust
pub fn get_payload_for_export(store: &PayloadStoreMap, topic: &str) -> Result<ExportPayload, String> {
    store
        .get(topic)
        .map(|e| ExportPayload { bytes: e.bytes.clone(), filename: e.filename.clone() })
        .ok_or_else(|| format!("No payload stored for '{}'", topic))
}
```

    - Reduce P3 T13's `stored_filename` to its direct lookup too. Its chunk fallback calls `chunk_progress` and `parse_chunk_key`, which this step deletes, so it would not compile otherwise. `FileJobsUI::save_topic_to_file` (P3 T13, `src/ui/file_jobs.rs`) keeps calling it unchanged:

```rust
/// Transmitted filename for a topic's stored payload, without copying bytes.
pub fn stored_filename(store: &PayloadStoreMap, topic: &str) -> Option<String> {
    store.get(topic).and_then(|e| e.filename.clone())
}
```

    - Change the module doc to: `//! Export of plain (non-transfer) payloads: store with byte budget, safe filenames, save dialog. Large files use transfer v2.`
    - Keep `sanitize_filename`, `suggested_export_filename`, `stored_filename` (reduced above), `format_size`, `MAX_PLAIN_ENTRIES` and `MAX_PLAIN_BYTES`. `export_payload_to_file` no longer exists: P3 T13 deleted it.
  - `registry.rs`: no change. `EntryKind::LegacyV1` stays; it is unused and allowed by the module-level `allow(dead_code)`, and T13 removes it if still unused.
  - Run: `cargo test && ! grep -rnw 'CHUNK_SIZE\|parse_chunk_key\|ChunkMeta\|chunk_progress' src && grep -rn '__chunk' src`
  - Expected: tests pass (including `stored_filename_direct`). The first grep prints nothing; `-w` matches whole words only, so the v2 constants `MIN_CHUNK_SIZE`, `MAX_CHUNK_SIZE` and `DEFAULT_CHUNK_SIZE` in `transfer/manifest.rs` do not count. The second prints only the `"t/__chunk/3/1/0"` literals in `v1_chunk_keys_are_ordinary_topics`.

- [ ] **Step 2 — Option A (only if Q1 says keep): read-only v1 for one release.**
  - Write the failing tests. In `registry.rs` tests:

```rust
    #[test]
    fn legacy_chunks_feed_registry() {
        let mut r = TransferRegistry::default();
        r.record_legacy_chunk("t/old", 100, 2, 0, Some("../a.bin"));
        let e = r.for_key("t/old")[0].clone();
        assert_eq!((e.kind, e.filename.as_str(), e.status.clone()), (EntryKind::LegacyV1, "a.bin", TransferStatus::Fetching { done: 1 }));
        r.record_legacy_chunk("t/old", 100, 2, 1, None);
        assert_eq!(r.for_key("t/old")[0].status, TransferStatus::Verified);
        r.record_legacy_chunk("t/old", 200, 3, 0, None); // new generation supersedes
        assert_eq!(r.for_key("t/old").len(), 1);
    }
```

    In the `ingest.rs` tests:

```rust
    #[test]
    fn legacy_chunk_messages_reach_registry() {
        let (mut app, tx) = ZenohExplorer::test_app();
        let m = ZenohMessage::new_with_bytes(
            "t/__chunk/3/1/0".into(), "abc".into(), b"abc".to_vec(), "application/octet-stream".into(),
            chrono::Utc::now(), MessageType::Subscribe, false, MessageSource::MonitorSession,
        );
        tx.send(ZenohEvent::MessageReceived(m)).unwrap();
        app.process_events();
        assert_eq!(app.transfers.for_key("t").len(), 1);
        assert!(app.messages.is_empty());
    }
```

  - In `registry.rs`, add the field `legacy_received: HashMap<TransferId, std::collections::HashSet<u32>>` to `TransferRegistry` (the derive stays `Default`), and add:

```rust
fn legacy_id(topic: &str, size: u64, count: u32) -> TransferId {
    use std::hash::Hasher;
    let mut h = seahash::SeaHasher::new();
    h.write(topic.as_bytes());
    h.write(&[0xff]);
    h.write(&size.to_le_bytes());
    h.write(&count.to_le_bytes());
    TransferId((u128::from(h.finish()) << 64) | 0x7631)
}

impl TransferRegistry {
    /// v1 compatibility (one release): progress for `{topic}/__chunk/...`.
    pub fn record_legacy_chunk(&mut self, topic: &str, total_size: u64, total_chunks: u32, index: u32, filename: Option<&str>) {
        let id = legacy_id(topic, total_size, total_chunks);
        self.entries.retain(|eid, e| !(e.kind == EntryKind::LegacyV1 && e.key == topic && *eid != id));
        self.legacy_received.retain(|eid, _| *eid == id || self.entries.contains_key(eid));
        let got = self.legacy_received.entry(id).or_default();
        got.insert(index);
        let done = got.len() as u32;
        let e = self.entries.entry(id).or_insert_with(|| TransferEntry {
            id,
            kind: EntryKind::LegacyV1,
            direction: Direction::Incoming,
            key: topic.to_string(),
            filename: filename
                .and_then(crate::transfer::sanitize_filename)
                .unwrap_or_else(|| crate::transfer::suggested_export_filename(topic, None)),
            total_size,
            chunk_count: total_chunks,
            manifest: None,
            status: TransferStatus::Fetching { done: 0 },
            last_error: None,
            updated: Instant::now(),
        });
        e.status = if done >= total_chunks { TransferStatus::Verified } else { TransferStatus::Fetching { done } };
        e.updated = Instant::now();
    }
}
```

    In `gc`, after the retains, add `self.legacy_received.retain(|id, _| self.entries.contains_key(id));`.
  - In `ingest.rs`, replace the T8 skip with:

```rust
if let Some((topic, meta)) = crate::transfer::parse_chunk_key(&message.key) {
    if let (true, Ok(n), Ok(i)) = (meta.is_sane(), u32::try_from(meta.total_chunks), u32::try_from(meta.index)) {
        let topic = topic.to_string();
        self.transfers.record_legacy_chunk(&topic, meta.total_size as u64, n, i, message.filename.as_deref());
        if let Ok(mut tree) = self.browse_tree.write() {
            tree.insert_path(&topic);
        }
        self.tree_version = self.tree_version.wrapping_add(1);
    }
    return;
}
```

  - In `export.rs`, change only the module doc to: `//! Plain-payload export, plus read-only v1 __chunk reassembly kept for one release (removed in 0.11; see P4 Q1).` Saving goes through T9's `LegacyV1` → `FileJobsUI::save_topic_to_file` path, which still reassembles through `get_payload_for_export`. `stored_filename` stays as P3 T13 left it, chunk fallback included.
  - Run: `cargo test legacy_ && cargo test`
  - Expected: pass.

- [ ] **Step 3: Verify.**
  - Run: `cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check`
  - Expected: clean.

- [ ] **Step 4: Commit.** Use exactly one of these messages.

```bash
git add src/transfer/export.rs src/worker/publish.rs src/events/ingest.rs src/transfer/registry.rs
git commit -m "feat(transfer)!: remove v1 __chunk send and receive (P4 Q1 option B)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
# or: "feat(transfer): remove v1 __chunk send; keep v1 receive read-only for one release (P4 Q1 option A)"
```

---

### Task T11: Documentation (Lane E)

**Owns:** `README.md`, `src/ui/help.rs`, `src/transfer/README.md`

This satisfies the module-README requirement in `docs/bearhug/WORKFLOW.md` for the new `src/transfer/` module.

**Before starting:** read the user's answer to **Q1** (see the Q1 gate under [Lanes and waves](#lanes-and-waves)). If there is no answer, write the **Option B** text, the same default T10 implements. T11 runs in Wave 1 but T10 only in Wave 3, so Q1 must not change between them. Steps 2 and 3 are the only Q1-dependent parts, and T13 Step 3 checks that they agree with T10.

- [ ] **Step 1: README feature bullet.** In `README.md`, replace line 28 (pre-P1, `**File import support**: … 5GB+ file support`) with:

```markdown
  - **File transfers (v2)**: offer any file on a key; receivers fetch it on demand in verified chunks (BLAKE3), with resume and cancel. Plain publishes are limited to 64 MB.
```

- [ ] **Step 2: README section.** Insert a new section after `### Connection Options` (before `## Installation`):

```markdown
## File Transfer

Publish tab → **Offer File as Transfer…** offers a file under the publish key. Other explorers see it in the topic tree (📥). Selecting that topic shows a **Transfers** section, where **Fetch** downloads it and **Save…** writes it once verified.

| Resource | Key expression |
|---|---|
| Sender presence (liveliness) | `{key}/@xfer/{id}` |
| Manifest (JSON) | `{key}/@xfer/{id}/manifest` |
| Chunk *i* (queryable) | `{key}/@xfer/{id}/chunk?i=<i>` |

- `@xfer` is a verbatim chunk: `**` subscribers (including other tools) never receive transfer traffic.
- Chunks are 4 MiB, fetched 4 at a time at `data_low` priority, so live telemetry is not starved.
- Every chunk and the whole file are checked against BLAKE3 hashes in the manifest. Hashes detect corruption; they do not authenticate the sender.
- Partial downloads live in the OS temp directory (`zenoh-explorer-transfers/`) and resume after a restart; they are removed after 24 h.
- Limits: 4 concurrent fetches and 4 offers; incoming files up to 16 GB by default (editable); disk space is checked before fetching.
- Earlier versions' `__chunk` transfers are not compatible with this protocol.
```

  If Q1 chose Option A, replace the last bullet with: `- Receiving 0.9.x \`__chunk\` transfers still works in this release (not verified); sending them was removed.`

- [ ] **Step 3: Help tab.** P1 T26 rewrites `src/ui/help.rs` so that all text lives in `pub(crate) const HELP_SECTIONS: &[(&str, &[&str])]`, with tests in `ui::help::tests` (`help_names_only_real_places`, `help_claims_match_limits`). Edit that data, not line numbers.
  - Test first. Add to the `ui::help::tests` module (it already has the `all_text()` helper):

```rust
    #[test]
    fn help_points_large_files_to_transfers() {
        let t = all_text();
        // "MB", the unit format_size prints in the Import refusal ("64.00 MB").
        assert!(t.contains("up to 64 MB"), "Publish step states the plain-publish cap");
        // "Topic Details" is only the internal DetailView variant, not an on-screen label (F-T18-1).
        assert!(!t.contains("Topic Details"), "Help names a place that has no label on screen");
        assert!(t.contains("Offer File as Transfer"));
        assert!(HELP_SECTIONS.iter().any(|(h, _)| *h == "File transfers"));
        // Option B only (Q1): v1 `__chunk` keys become ordinary topics, so Help must not
        // claim they are hidden. Omit this line if Q1 chose Option A.
        assert!(!t.contains("file chunks"), "All Messages line no longer mentions file chunks");
    }
```

  If Q1 chose Option A, leave out the `file chunks` assertion (the three lines starting with the `// Option B only` comment).

  - Run `cargo test ui::help::tests::help_points_large_files_to_transfers`. Expected: FAIL (no "up to 64 MB").
  - Find the Publish step: `grep -n '"5. Publish:' src/ui/help.rs`. After P1 T26 it reads `"5. Publish: send text, or import a file (it is read into memory)."`. If the number has changed, anchor on the text `Publish: send text`. Replace that one string with:

```rust
            "5. Publish: send text, or import a file of up to 64 MB (it is read into memory). For larger files use Offer File as Transfer (Publish tab).",
```

  - Q1-conditional, like the README bullet in Step 2. P1 T26 writes the All Messages step as `"4. All Messages (Topics view with no topic selected) lists recent messages from your subscriptions. Paused topics and file chunks are not listed."`. That is true only while `src/events` hides keys that `parse_chunk_key` matches. T10 Option B removes `parse_chunk_key`, after which v1 `__chunk` keys are ordinary topics and appear in All Messages (T10's `v1_chunk_keys_are_ordinary_topics`). Anchor on the text `Paused topics and file chunks are not listed`:
    - Option B (recommended): replace `Paused topics and file chunks are not listed.` with `Paused topics are not listed.` in that one string.
    - Option A: keep the string unchanged.
  - Add a section to `HELP_SECTIONS` directly after the `"Key expressions"` entry:

```rust
    ("File transfers", &[
        "Publish tab → Offer File as Transfer… offers a file under the publish key.",
        "Receivers see it in the tree (📥); select that topic and use Fetch in its Transfers section.",
        "Chunks and the whole file are BLAKE3-verified; interrupted fetches resume.",
        "Transfer keys use {key}/@xfer/…, which ** subscriptions never match.",
    ]),
```

  - Run `cargo test ui::help`. Expected: 3 tests pass (P1 T26's two and `help_points_large_files_to_transfers`, which also checks that no Help line names "Topic Details" and, under Option B, that none mentions `file chunks`). None of the new lines contain a phrase that `help_claims_match_limits` forbids.

- [ ] **Step 4: Module README.** Create `src/transfer/README.md`:

````markdown
# `src/transfer/` — file transfer

**Purpose:** move files between Zenoh Explorer instances without whole-file buffers, with integrity checks, resume and cancel. Also hosts plain-payload export (`export.rs`).

## Responsibilities and files
| File | Responsibility |
|---|---|
| `manifest.rs` | Protocol v2 contract: `Manifest`, `TransferId`, key space (`@xfer`), chunk math, validation |
| `event.rs` | `TransferEvent` (worker→UI), `TransferCommand` (UI→worker), `FailReason`, `EventSink` |
| `limits.rs` | Tunables (`TransferLimits`, chunk size, timeouts, `PLAIN_PUBLISH_MAX`) |
| `tasks.rs` | Worker-side task handles, admission limits, `dispatch` |
| `sender/prepare.rs` | Hash a file into a manifest; positioned chunk reads; source-change detection |
| `sender/serve.rs` | Liveliness token + manifest/chunk queryables; withdraw on cancel |
| `receiver/spool.rs` | `.part` spool, per-chunk verify+write, resume scan, admission, atomic save, GC |
| `receiver/fetch.rs` | Querier pull with N in flight, retries, liveliness abort, final verify |
| `receiver/discovery.rs` | `**/@xfer/*/manifest` subscription + GET, sender liveliness |
| `registry.rs` | UI-side single source of truth for transfer state |
| `export.rs` | Plain-payload export store and save helpers |

## Key space
| Resource | Key expression |
|---|---|
| Token | `{key}/@xfer/{id}` |
| Manifest | `{key}/@xfer/{id}/manifest` |
| Chunk | `{key}/@xfer/{id}/chunk?i=<index>` |
| Discovery | `**/@xfer/*/manifest`, liveliness `**/@xfer/*` |

## Interfaces and dependencies
- The worker calls `tasks::dispatch` for `ZenohCommand::Transfer`, starts `receiver::spawn_discovery` on connect, and calls `TransferTasks::shutdown` on teardown.
- The UI applies `ZenohEvent::Transfer` to `TransferRegistry` (`events/mod.rs`) and renders it in `ui/transfers.rs` and `ui/topic_tree.rs`.
- Depends on zenoh 1.10 (Querier, Queryable, liveliness), blake3, tempfile, fs4, rand.

## Invariants
- No `**` subscription can observe transfer traffic (`@xfer` is verbatim); offer keys must be non-wild and contain no `@` chunk.
- Memory per fetch ≤ `in_flight × chunk_size`; the sender never loads the file.
- Spool paths derive only from the hex transfer ID.
- Replies inherit the query QoS (DataLow, Block); never set QoS on replies.
- Registry: first failure wins; a Verified/Saved entry never becomes Failed.

## How to test
- Unit: `cargo test transfer::` and `cargo test ui::transfers`.
- Loopback (binds 127.0.0.1 ports 27700–27753): `cargo test -- --ignored transfer::`.
````

- [ ] **Step 5: Verify.**
  - Run: `cargo test ui::help && cargo build && grep -n '@xfer' README.md src/transfer/README.md && grep -n 'Offer File as Transfer' src/ui/help.rs`
  - Expected: 3 `ui::help` tests pass, the build passes, and both greps match.

- [ ] **Step 6: Commit.**

```bash
git add README.md src/ui/help.rs src/transfer/README.md
git commit -m "docs(transfer): README section, help text and module README for transfer v2

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T12: End-to-end loopback tests (Lane F)

**Owns:** `src/transfer/loopback_tests.rs`

**Interfaces:** consumes the real `sender::spawn_offer`, `receiver::{spawn_discovery, spawn_fetch, spawn_save}`, `Spool::paths` and `test_support`.

- [ ] **Step 1: Write the tests** (the implementation already exists, from T3, T5 and T6):

```rust
//! End-to-end loopback tests for transfer v2 (real sender, discovery, fetcher).

use super::event::{Direction, FailReason, TransferEvent};
use super::limits::TransferLimits;
use super::manifest::{chunk_key, Manifest, TransferId};
use super::receiver::spool::Spool;
use super::test_support::*;
use super::{receiver, sender};

async fn discover(rx: &mut tokio::sync::mpsc::UnboundedReceiver<TransferEvent>, id: TransferId) -> Manifest {
    match next_event(rx, 20, |e| matches!(e, TransferEvent::Announced { manifest, direction: Direction::Incoming } if manifest.transfer_id == id)).await {
        TransferEvent::Announced { manifest, .. } => *manifest,
        _ => unreachable!(),
    }
}

fn limits(spool: &std::path::Path) -> TransferLimits {
    TransferLimits { chunk_size: 256 * 1024, request_timeout: std::time::Duration::from_secs(10), ..test_limits(spool) }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opens network sessions"]
async fn e2e_20_mib_round_trip_hash_matches() {
    let (a, b) = session_pair(27751).await;
    let (src, spool, out) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
    let data = test_bytes(20 * 1024 * 1024, 7);
    let path = src.path().join("payload.bin");
    std::fs::write(&path, &data).unwrap();
    let (disc_sink, mut disc_rx) = collecting_sink();
    let _disc = receiver::spawn_discovery(b.clone(), limits(spool.path()), disc_sink);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let (a_sink, _a_rx) = collecting_sink();
    let id = TransferId::random();
    let _offer = sender::spawn_offer(a.clone(), id, "e2e/file".into(), path, limits(src.path()), a_sink);
    let m = discover(&mut disc_rx, id).await;
    wait_for_queryable(&b, &chunk_key(&m.key, id)).await;
    let (sink, mut rx) = collecting_sink();
    let _f = receiver::spawn_fetch(b.clone(), m.clone(), limits(spool.path()), sink);
    assert!(matches!(
        next_event(&mut rx, 120, |e| matches!(e, TransferEvent::Completed { .. } | TransferEvent::Failed { .. })).await,
        TransferEvent::Completed { .. }
    ));
    let part = std::fs::read(Spool::paths(spool.path(), id).0).unwrap();
    assert_eq!(blake3::hash(&part).to_hex().to_string(), m.file_hash);
    let dest = out.path().join(&m.filename);
    let (s_sink, mut s_rx) = collecting_sink();
    let _s = receiver::spawn_save(spool.path().to_path_buf(), id, dest.clone(), s_sink);
    assert!(matches!(next_event(&mut s_rx, 30, |_| true).await, TransferEvent::Saved { .. }));
    assert_eq!(std::fs::read(dest).unwrap(), data);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opens network sessions"]
async fn e2e_sender_disappears_gives_sender_gone() {
    let (a, b) = session_pair(27752).await;
    let (src, spool) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
    let path = src.path().join("big.bin");
    std::fs::write(&path, test_bytes(64 * 1024 * 1024, 9)).unwrap();
    let (a_sink, mut a_rx) = collecting_sink();
    let id = TransferId::random();
    let _offer = sender::spawn_offer(a.clone(), id, "e2e/gone".into(), path, limits(src.path()), a_sink);
    let m = match next_event(&mut a_rx, 60, |e| matches!(e, TransferEvent::Announced { .. })).await {
        TransferEvent::Announced { manifest, .. } => *manifest,
        _ => unreachable!(),
    };
    wait_for_queryable(&b, &chunk_key(&m.key, id)).await;
    let (sink, mut rx) = collecting_sink();
    let l = TransferLimits { in_flight: 1, max_retries: 1000, ..limits(spool.path()) };
    let _f = receiver::spawn_fetch(b.clone(), m, l, sink);
    next_event(&mut rx, 30, |e| matches!(e, TransferEvent::Progress { chunks, .. } if *chunks >= 1)).await;
    a.close().await.unwrap(); // Session::close(&self), api/session.rs:959; the offer task still holds a clone
    assert!(matches!(
        next_event(&mut rx, 20, |e| matches!(e, TransferEvent::Completed { .. } | TransferEvent::Failed { .. })).await,
        TransferEvent::Failed { reason: FailReason::SenderGone, .. }
    ));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "opens network sessions"]
async fn e2e_two_concurrent_transfers_same_topic_same_size_do_not_mix() {
    let (a, b) = session_pair(27753).await;
    let (src, spool) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
    let (d1, d2) = (test_bytes(3 * 1024 * 1024, 1), test_bytes(3 * 1024 * 1024, 2));
    assert_ne!(d1, d2);
    let (p1, p2) = (src.path().join("one.bin"), src.path().join("two.bin"));
    std::fs::write(&p1, &d1).unwrap();
    std::fs::write(&p2, &d2).unwrap();
    let (disc_sink, mut disc_rx) = collecting_sink();
    let _disc = receiver::spawn_discovery(b.clone(), limits(spool.path()), disc_sink);
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let (sa, _ra) = collecting_sink();
    let (id1, id2) = (TransferId::random(), TransferId::random());
    let _o1 = sender::spawn_offer(a.clone(), id1, "e2e/same".into(), p1, limits(src.path()), sa.clone());
    let _o2 = sender::spawn_offer(a.clone(), id2, "e2e/same".into(), p2, limits(src.path()), sa);
    let (m1, m2) = (discover(&mut disc_rx, id1).await, discover(&mut disc_rx, id2).await);
    wait_for_queryable(&b, &chunk_key("e2e/same", id1)).await;
    wait_for_queryable(&b, &chunk_key("e2e/same", id2)).await;
    let (s1, mut r1) = collecting_sink();
    let (s2, mut r2) = collecting_sink();
    let _f1 = receiver::spawn_fetch(b.clone(), m1, limits(spool.path()), s1);
    let _f2 = receiver::spawn_fetch(b.clone(), m2, limits(spool.path()), s2);
    let done = |e: &TransferEvent| matches!(e, TransferEvent::Completed { .. } | TransferEvent::Failed { .. });
    assert!(matches!(next_event(&mut r1, 60, done).await, TransferEvent::Completed { .. }));
    assert!(matches!(next_event(&mut r2, 60, done).await, TransferEvent::Completed { .. }));
    assert_eq!(std::fs::read(Spool::paths(spool.path(), id1).0).unwrap(), d1);
    assert_eq!(std::fs::read(Spool::paths(spool.path(), id2).0).unwrap(), d2);
}
```

- [ ] **Step 2: Run the tests.**
  - Run: `cargo test -- --ignored transfer::loopback_tests`
  - Expected: 3 passed.
  - If `e2e_sender_disappears` reports `Timeout { .. }` instead of `SenderGone`, raise `max_retries` further. Do not accept `Timeout`, because the liveliness Delete is what must end the fetch.

- [ ] **Step 3: Commit.**

```bash
git add src/transfer/loopback_tests.rs
git commit -m "test(transfer): end-to-end loopback — 20 MiB round trip, sender loss, same-size isolation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T13: Integration verification

**Owns:** `src/transfer/mod.rs` (only to remove the Wave 0 `#![allow(dead_code)]`). If any other check fails, open a fix task on the owning lane instead of editing here.

- [ ] **Step 1: Remove the scaffolding allow.**
  - Delete the `#![allow(dead_code)]` line from `src/transfer/mod.rs`.
  - Run: `cargo clippy --all-targets -- -D warnings`
  - Expected: clean.
  - If an item is reported unused:
    - `EntryKind::LegacyV1` under Option B: open a one-line fix task on registry.rs's owner (Lane D, T10) to delete it and its match arms.
    - Anything else: report it as a lane defect.

- [ ] **Step 2: Full suite.**

```bash
cargo build --locked && cargo test --locked && cargo test --locked -- --ignored \
  && cargo clippy --all-targets --locked -- -D warnings && cargo fmt --all -- --check && cargo audit
```

  Expected: all pass, and audit reports 0 unignored vulnerabilities.
  - Run this on macOS: `-- --ignored` includes P3 T16's wgpu snapshot tests, which need a GPU adapter. `publish_light` passes against the reference T9 regenerated. A snapshot failure is a defect for the lane that owns the changed view (the Publish tab is T9's).
  - The suite also cross-checks Q1 between code and Help. Under Option B, T10's `v1_chunk_keys_are_ordinary_topics` shows that All Messages lists a chunk key, and T11's `help_points_large_files_to_transfers` shows that Help no longer says file chunks are hidden. Under Option A, T10's `legacy_chunk_messages_reach_registry` shows that chunks stay out of All Messages, matching the unchanged Help line.

- [ ] **Step 3: Residue greps.**

```bash
grep -rn 'TransferState\|record_chunk' src              # expect: nothing
grep -rn 'Chunked' src/worker                            # expect: nothing
grep -rnw 'CHUNK_SIZE\|parse_chunk_key' src              # Option B: nothing; Option A: only src/transfer/export.rs and events/ingest.rs (-w: v2's MIN_/MAX_/DEFAULT_CHUNK_SIZE do not match)
grep -n 'reply' src/transfer/sender/serve.rs | grep 'priority\|congestion'   # expect: nothing (replies inherit query QoS)
grep -n 'file chunks are not listed' src/ui/help.rs      # Q1 docs match code. Option B: nothing; Option A: one match (P1 T26's All Messages step)
grep -n 'not compatible with this protocol' README.md    # Option B: one match; Option A: nothing (T11 Step 2's last bullet)
```

  If either Q1 line disagrees with the option T10 committed (its commit message names it), open a fix task on Lane E (T11) for the docs. Do not change T10's code to match the docs.

- [ ] **Step 4: Architecture docs.**
  - Run: `scripts/bin/bearhug-arch refresh && scripts/bin/bearhug-arch status`
  - Expected: `src/transfer/` is covered by `src/transfer/README.md`. Paste the status.

- [ ] **Step 5: Two-instance smoke run.** Paste the observations for each step into the evidence.
  1. Terminal 1: `cargo run --release`. Connect in peer mode with listen port 7447. Terminal 2: `cargo run --release`, with listen port 7457 and a connect locator `tcp/127.0.0.1:7447`.
  2. Make a test file with `head -c 200000000 /dev/urandom > /tmp/p4.bin` (200 MB).
  3. In instance A, set Publish key `demo/files/p4` and use **Offer File as Transfer…** on `/tmp/p4.bin`. A shows "Preparing…", then "Offering".
  4. In instance B, the tree shows `demo/files/p4` with 📥. Topic Details → Transfers shows "Available · 190.73 MB". Click **Fetch**; the progress rises.
  5. While it is fetching, publish text on `demo/tick` from A several times. B's tree updates `demo/tick` promptly, so telemetry is not starved.
  6. In B, click **Save…** to `/tmp/p4-out.bin`. Run `cmp /tmp/p4.bin /tmp/p4-out.bin`, which is expected to print nothing.
  7. Offer again, fetch in B, and quit A mid-fetch. B shows "Failed: sender is gone" (or "withdrawn by the sender") within about 5 s.
  8. Offer again, fetch, then quit **B** mid-fetch and restart it. The offer reappears as Available; **Fetch** resumes from the existing chunks (the progress starts above 0), and the Saved file matches under `cmp`.
  9. Subscribe `**` in B. No `@xfer` keys appear in the tree or in All Messages.
  10. In A, Publish tab → **Import File** on `/tmp/p4.bin` (200 MB, over `PLAIN_PUBLISH_MAX`). The import is refused at once with an error naming both sizes, and the draft is unchanged (T9, UI review F-T15-5).

- [ ] **Step 6: Commit** (only if Step 1 changed `mod.rs`):

```bash
git add src/transfer/mod.rs
git commit -m "chore(transfer): drop Wave 0 dead_code allowance

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Open questions

1. **Q1: v1 `__chunk` receive compatibility.**
   - **Recommended default: remove (Option B).** Sending v1 is removed either way, so 0.10 ↔ 0.9 interoperability is one-way at best. Keeping receive also keeps the `/__chunk/` key hijack and the whole-file reassembly path that this plan exists to retire. Under Option B, v1 chunk samples show up as ordinary keys, bounded by P1's export budget.
   - **Option A** keeps read-only receive for one release, if users are likely to run mixed 0.9 and 0.10 fleets during the upgrade.
2. **Q2: Fetch policy.**
   - **Recommended: click-to-fetch.** Nothing downloads without a user action, which matches the pull design and avoids surprise disk use.
   - **Alternative:** auto-fetch offers under a threshold (for example 16 MiB). That would be a small addition to `events/mod.rs`, which calls `mark_fetch_requested` on `Announced` when `total_size ≤ threshold`.
3. **Q3: Spool location and default size cap.**
   - **Recommended:** the OS temp dir (`$TMPDIR/zenoh-explorer-transfers`), a 16 GiB default cap and 24 h spool GC.
   - **Caveat:** on some Linux systems `/tmp` is a RAM-backed tmpfs. The disk-space check protects against running out, but a per-user cache directory (`dirs::cache_dir()`, which would add one crate) may be preferable there.
