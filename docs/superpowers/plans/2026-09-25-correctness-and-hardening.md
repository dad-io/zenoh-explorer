# P1 · Correctness and Hardening Implementation Plan (concurrent lanes)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Only modify the files listed under your task's "Owns"** (in T27 and T28: your part's or step's "Owns"). Another task, part or step may be running at the same time on every other file.

**Goal:** Make Zenoh Explorer show and serve the data the network actually carried, survive hostile or noisy peers, and stay cheap when idle.

**Architecture:**
- **Threads stay as they are:** the UI thread, the batching buffer thread, and the tokio worker thread that owns two Zenoh sessions.
- **Wave 0 is a behaviour-preserving module split plus a shared contract.** Together they let the fixes run as parallel lanes that never edit the same file:
  - The split breaks `zenoh_worker.rs`, `types.rs`, `events.rs` and `app.rs` into modules, one command handler per worker file.
  - The contract adds the new types, event variants, a channel alias and helpers.
- **Lanes:**
  - A: sample ingest.
  - B: publish, queryable and transfer.
  - C: sessions, pipeline and repaint.
  - D: UI and display.
- **Amendment after Wave 3 (scope-sensitive).** Waves 0–3 are done: 14 tasks. The 12 pending tasks are merged into two larger tasks, so the rest of P1 needs two board starts, two completes and two verify rounds instead of twelve:
  - **T27** runs six parts (a–f) as parallel agents. Each part works in its own git worktree on files no other part touches. The parts then merge, one integration check runs, and one verifier reviews each part.
  - **T28** runs four steps in order. Only step 2 splits, into three parallel parts (2a, 2b and 2c) on disjoint files.
  - A replaced task's text lives on as a part or step headed "(was Tn)", so references from P2–P5 stay traceable. The map under the Tasks table lists every old ID.
  - A pre-flight review of the pending tasks is folded in: findings G1–G4, known issues K1–K10, and X1, a regression T16 left behind (overlapping subscriptions list a sample twice).

**Tech Stack:**
- Rust 2021.
- zenoh 1.x (`unstable` feature; the lock resolves to 1.10.x after T1).
- egui/eframe 0.29 with glow. The upgrade is plan P3.
- tokio.
- std `mpsc`.

**Spec:**
- `docs/superpowers/reviews/2026-09-25-zenoh-explorer-deep-review.md`, findings R1–R19.
- The behaviour findings of `docs/superpowers/reviews/2026-09-24-ui-ux-snow-white-review.md` (ids `F-T<n>-<m>`). Tasks T21–T26 were added for them (now T27 parts e and f and T28 steps 1–3), and T3, T5, T6, T7, T10, T12, T14, T17 and T18 were extended. Visual, colour, typography and motion findings are not in this plan; they belong to the Snow White UI plan.

**Programme:** this is P1 of five, run in order: P1 correctness, then P2 CI/release (`2026-09-25-p2-ci-release-hardening.md`), then P3 egui 0.36 port (`…-p3-egui-036-port.md`), then P4 transfer v2 (`…-p4-transfer-protocol-v2.md`), then P5 explorer features (`…-p5-explorer-features.md`).

**Decisions:** none recorded (`docs/memex` has 0). **Kind of change:** correctness and robustness fixes. There are no features and no major-version upgrades.

## Global Constraints

- After every task, run `cargo build`, `cargo test`, `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`, and all must pass. In T27 and T28, each part or step also runs them before it commits, and the merged tree runs them again after parallel parts merge.
- A task, or a part or step of T27 and T28, may create or modify only the files in its **Owns** list. If a step seems to need another file, stop and report instead of editing it.
- egui/eframe stay on `0.29`, rfd stays on `0.14`, and no new crates are added.
- Tests that open Zenoh sessions:
  - Use `#[tokio::test(flavor = "multi_thread", worker_threads = 2)]` with multicast scouting disabled.
  - Tests that bind listen ports or run peer scouting are `#[ignore = "opens network sessions"]`, and the task's Done-when runs them explicitly.
- Do not touch `colors.rs`, `app/theme.rs` or the tree's `plus_minus_icon`. They belong to the Snow White UI plan.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Multi-byte UTF-8 split at the preview cut:** the preview must show text, not hex. Pinned by T3 `preview_keeps_text_when_cut_mid_char`.
- **A stored `@/x` key must not answer a `**` query.** Pinned by T27 part b (was T6) `matching_excludes_verbatim_chunks`. A `demo/**` queryable must not answer `**` with `other/x` either. Pinned by T27 part b `matching_stays_inside_queryable_pattern`.
- **Listen port `abc` or `0`:** an error, not a panic or a silent 7447. `65000` is accepted, because the monitor no longer opens a second port at Listen Port + 1000 (R7). Pinned by T10 `listen_port_rejects_zero_and_garbage`.
- **A third app that only dials this app's listener:** its samples reach the `**` monitor, and the publishing session does not count the monitor as a peer. Pinned by T10 `monitor_sees_third_party_samples` (ignored; T28 step 4 (was T20) runs it). (F-T20-7)
- **Empty payloads:** preview as `""` and are still counted. Pinned by T3 `preview_empty`.
- **A publisher repeating the same value:** every sample counts. Pinned by T16 `same_source_repeats_are_not_deduped`.
- **Two overlapping subscriptions (`demo/**` and `demo/x`):** each sample is listed and counted once, and a repeat through one subscription still counts. Pinned by T27 part a (was T5) `overlapping_subscriptions_list_one_copy`, which checks both. (X1)
- **A delete right after an empty put from another session:** both count. Pinned by T27 part d (was T17) `delete_is_not_a_duplicate_of_empty_put`. (K6)
- **A put that zenoh rejects** (key `demo//x`): no echo, no local-kvstore entry, and an error in the Publish view. Pinned by T7 `failed_put_is_not_echoed_or_stored` and T27 part f (was T23) `publish_status_line_words`. (F-T8-3, F-T15-1)
- **A remote reply that arrives after a local one for the same key:** both are kept, and a reply for a paused key still reaches Query Results. Pinned by T27 part d (was T17) `local_and_remote_replies_are_both_kept` and `query_replies_skip_tree_and_pause`. (F-T16-8, F-T16-9)
- **Disconnect, then an immediate Connect:** the late `Disconnected` must not cancel the new connect, and the subscriptions come back with the ids they had. Pinned by T12 `stale_disconnected_does_not_cancel_new_connect` and `reconnect_restores_subscriptions` (T27 part a changes its id assertion). (F-T8-1, F-T17-7, K8)
- **A 20 s modal dialog on the UI thread:** no "Worker not answering" flash and no ping flood. Pinned by T28 step 1 (was T21) `ui_stall_does_not_mark_worker_unhealthy` and `ping_is_sent_once_per_interval`. (F-T8-7, F-T20-8)
- **Help names only places that exist.** Pinned by T28 step 3 (was T26) `help_names_only_real_places`. (F-T18-1)

## Baseline left for later plans

- **`ui_alert` is typed after T28 step 1 (was T21).** That step changes `ZenohExplorer::ui_alert` from `Option<String>` to `Option<UiAlert>` (`UiAlert::{Success, Warning, Error}(String)`, defined in `app/mod.rs`). Every later assignment, in P2–P5 and in the Snow White plan, must wrap its text, for example `self.ui_alert = Some(UiAlert::Error(format!("Save failed: {e}")))`, and a reader uses `alert.text()` instead of `as_deref()`. Classifying an alert by a text prefix such as `'✓'` is no longer allowed (F-T7-4). P1 raises only `Success` and `Error`; `Warning` keeps an `#[allow(dead_code)]` until a later plan raises one.
- **Renamed UI strings.** Later plans must not look up, assert or paste back the old wording below. A test that finds a widget by label uses the new text, and a snippet that replaces a whole block keeps P1's wording.

| Where | Old text | Text after P1 | Task |
|---|---|---|---|
| Header, worker health | `Worker Unresponsive` | `Worker not answering ({n} s)`, or `Worker busy: publishing` / `Worker busy: connecting` | T28 (was T21) |
| Header, memory | `Memory: {x}MB/{limit}MB` | `History {x} MB / {limit} MB` with ` (high)` / ` (critical)`, then ` · Stored payloads {size}`; imports as `Staged import {size}` | T28 (was T22) |
| Header, drop counter | `({n} dropped, {n} not listed (rate), {n} pipeline)` (T3) | `({n} trimmed from list, {n} not listed (rate), {n} pipeline)` | T28 (was T22) |
| Header, peers | `({r}R {p}P)` | `peers_text`: `no peers`, `1 peer`, `2 routers · 1 peer` | T28 (was T22) |
| Header, status | `Connected` | `Connected`, or `Connected · monitor off`; `Error: {first clause}` | T28 (was T22) |
| Header, memory warning | written into `query_alert` | its own `memory_alert` label | T28 (was T22) |
| Empty tree hint | `💡 Try demo/** or sensor/* in the Subscribe tab` | `💡 Try demo/** or sensor/* in Subscribe to Topics above` | T28 (was T24) |
| Empty tree line | `Subscribe to key expressions to see network activity` | `Topics appear here as this app receives data` | T28 (was T24) |
| Topic details count | `Messages:` {n} | `Received: {n} (since app start)` | T28 (was T24) |
| Topic details Pause | `⏸ Pause` / `▶ Resume`, state `⏸ Paused` | `⏸ Pause list` / `▶ Resume list`, state `Paused (lists only)` | T28 (was T24) |
| All Messages count | `Messages: {n}` | `In list: {n} (limit {max})` | T27 (was T25) |

  Later plans already use this wording: P3 T7's `header_fits_at_720` needles and P3 T10's tree-block replacement follow this table.

## Tasks

T27 and T28 replace the twelve pending tasks. The map under the table says where each old task ID went.

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Lane Deps · owns Cargo.toml, Cargo.lock, .cargo/audit.toml, .github/workflows/ci.yml] Dependency hygiene: lockfile update, manifest trim, MSRV 1.88, eframe platform features, audit config and CI audit job (R1, R2) | — | `cargo audit` reports 0 vulnerabilities, with only RUSTSEC-2023-0071 and RUSTSEC-2026-0041 ignored (reasons in `.cargo/audit.toml`). `Cargo.toml` has no `egui_extras`/`serde`/`anyhow`. Build, test, clippy and fmt pass. |
| T2 | [Wave 0 · Lane Split · owns src/main.rs, src/zenoh_worker.rs→src/worker/*, src/types.rs→src/types/*, src/events.rs→src/events/*, src/app.rs→src/app/*] Behaviour-preserving module split: one worker file per command handler, and the monitor/user subscription loops unified into `subscribe::spawn_sample_task` | — | Old files are deleted and the new module tree matches the T2 layout table. `cargo test` passes with the same 30 tests. `git diff --stat` shows only moves and the handler extraction. |
| T3 | [Wave 0 · Lane Contract · owns src/types/*, src/payload.rs, src/validation.rs, src/main.rs, src/events/mod.rs, src/events/ingest.rs, src/worker/{mod,state,pipeline,session,subscribe,query,publish,queryable}.rs, src/app/mod.rs, src/app/layout.rs, src/ui/topic_tree.rs] Shared contract: new types and fields, `OperationFailed`, payload-free `Debug`, `payload::preview`, the `EventTx` alias with `event_channel` and `send_sample`, `sample_drops`, `LocalKvStore` with raw bytes, `test_app`, log levels (R3 helper, R5 store, R9). Also, for the UI review: the `Published` event, the `PublishStatus` field, the `connect_started`/`connect_target` fields and the `format_local_time` helper (F-T8-4, F-T14-6, F-T17-10) | T2 | Tests `payload::`, `debug_output_omits_payload_bytes`, `failed_query_replaces_waiting_alert`, `failed_queryable_unchecks_toggle`, `publish_outcome_updates_status` and `local_time_format_marks_other_days` pass. `grep -rn 'mpsc::Sender<ZenohEvent>' src/worker` returns nothing. |
| T4 | [Wave 1 · Lane A · owns src/worker/samples.rs, src/worker/subscribe.rs, src/worker/query.rs] `message_from_sample` carries the real encoding, put/delete kind and source timestamp, and is used at every receive site (R3, R4) | T3 | Loopback test `message_from_sample_keeps_encoding_kind_and_timestamp` passes. `grep -rn '"text/plain".to_string()' src/worker` returns nothing. |
| T7 | [Wave 1 · Lane B · owns src/worker/publish.rs] Publish path: `publish_shape` (chunk above 64 MiB), echo only on success, `OperationFailed` on error, drop the dead 100 MB branch (R6, R10). The local kvstore is written only after a successful put, and every successful put sends `Published` (F-T8-3, F-T8-4, F-T15-6) | T3 | Tests `publish_shape_*` and `failed_put_is_not_echoed_or_stored` pass. `grep -n '100 \* 1024 \* 1024' src/worker/publish.rs` returns nothing. |
| T8 | [Wave 0b · Lane B · owns src/transfer.rs] Validate chunk lengths before allocating on export; tighter `is_sane`; `sanitize_filename` (R8) | T2 | Tests `export_rejects_claimed_size_larger_than_chunks` and `sanitize_strips_paths_and_controls` pass, along with all existing transfer tests. |
| T10 | [Wave 1 · Lane C · owns src/worker/connect.rs, src/worker/session.rs] Connection config safety: default `max_message_size`, checked listen port with no `+ 1000` monitor port, no config `unwrap()`, monitor failure reported (R6, R7, R10). Connection errors lose zenoh's source-path suffix and lead with a user sentence; an explicit peer-mode endpoint that cannot be reached fails the connect; the monitor session always runs in client mode and dials the publishing session's own listener (peer mode) or the same routers (client mode), so it sees third-party traffic (F-T17-2, F-T17-3, F-T20-7) | T3 | Tests `listen_port_rejects_zero_and_garbage`, `monitor_endpoints_follow_publishing_mode` and `connect_error_text_has_no_source_path` pass. `cargo test -- --ignored peer_mode_unreachable_endpoint_fails monitor_sees_third_party_samples` passes. `grep -n 'set_max_message_size\|\.unwrap()' src/worker/connect.rs` returns nothing outside tests. `grep -rn 'monitor_port' src/worker` prints nothing. |
| T11 | [Wave 1 · Lane C · owns src/worker/pipeline.rs, src/app/mod.rs, src/events/mod.rs] Bounded channels, a buffer thread that blocks instead of spinning, an 8 ms per-frame event budget (R12) | T3 | Tests `buffer_thread_batches_and_preserves_all_messages` and `send_sample_counts_drops_when_full` pass. The events channel in `app/mod.rs` is `sync_channel`. |
| T12 | [Wave 2 · Lane C · owns src/worker/state.rs, src/worker/session.rs, src/worker/mod.rs, src/app/layout.rs, src/events/mod.rs] Connection lifecycle: `WorkerState::teardown`, an abortable discovery task, clean reconnect, queryable reset, Connect/Disconnect button states (R13). Also: a late `Disconnected` cannot cancel a newer connect; subscriptions survive Disconnect and are re-declared on reconnect; the form validates ports, drops a stale error when an input changes, tells the truth in its hints, and the header names the target and elapsed time while connecting (F-T8-1, F-T17-1, F-T17-4, F-T17-5, F-T17-7, F-T17-10) | T10, T11, T14 | `cargo test -- --ignored reconnect_then_disconnect_leaves_no_discovery_updates reconnect_restores_subscriptions` passes. Tests `stale_disconnected_does_not_cancel_new_connect` and `connection_hints_match_the_form` pass. |
| T13 | [Wave 3 · Lane C · owns src/app/mod.rs, src/app/layout.rs, src/main.rs] Repaint on worker events instead of a 66 ms loop (R14) | T11, T12 | No `from_millis(66)` in `src/app/layout.rs` except the unhealthy-worker pulse. Idle CPU while connected with no traffic is under 3 % over 20 s, with readings in the commit body. |
| T14 | [Wave 1 · Lane D · owns src/validation.rs, src/ui/publish.rs, src/ui/query.rs, src/ui/topic_tree.rs] Validate key expressions and selectors in the UI; publish keeps typed text; the query timeout is validated (R11). Error text drops zenoh's source-path suffix, leading or trailing spaces are rejected, wildcard publish keys get a neutral note, and `port_error` validates ports (F-T15-1, F-T16-3, F-T17-1) | T3 | `cargo test validation::` passes, including `error_text_has_no_source_path`, `surrounding_space_is_rejected` and `port_bounds`. Publish, Subscribe, Query and the Queryable checkbox are disabled with an inline error for invalid input (manual check). |
| T15 | [Wave 2 · Lane D · owns src/ui/topic_tree.rs] Topic Details shows DELETE and the source timestamp (R4 display) | T14 | Manual check: publish then delete `demo/x` from another client, and Topic Details shows "Last sample: DELETE". Build, test and clippy pass. |
| T16 | [Wave 1 · Lane D · owns src/types/limits.rs, src/events/ingest.rs] Dedup collapses only cross-source duplicates, with a 250 ms window (R15) | T3 | Tests `same_source_repeats_are_not_deduped`, `cross_source_duplicate_is_deduped` and `dedup_expires_after_ttl` pass. |
| T19 | [Wave 3 · Lane D · owns src/ui/topic_tree.rs, src/types/tree.rs] Tree per-frame cost: no deep clone, throttled filter recompute, bounded history scan (R18) | T15 | Test `filter_cache_staleness_rules` passes. `grep -n 'tree.clone()' src/ui/topic_tree.rs` returns nothing. |
| T27 | [Wave 4 · Merged · parallel parts a–f · owns src/worker/subscribe.rs, src/worker/query.rs, src/worker/session.rs, src/worker/mod.rs, src/types/message.rs (a); src/worker/queryable.rs (b); src/transfer.rs (c); src/events/ingest.rs (d); src/ui/messages.rs, src/events/json_cache.rs, src/types/mod.rs (e); src/ui/publish.rs, src/ui/query.rs (f)] Worker, export and view fixes. a (was T5): subscribe and query failures reach the UI, and a query timeout is not reported as a queryable's error; subscription ids come from a counter and survive a reconnect; "local" is decided by the replier; each user subscription tags its samples with its own source, so overlapping subscriptions list a sample once (R10, F-T16-7, K8, X1). b (was T6): the queryable matches with `keyexpr::intersects` inside its own pattern, serves raw bytes, and reports a declare failure; its test binds no port (R5, R10, F-T16-7, K10). c (was T9): export store byte budget, and stale-transfer GC that keeps complete transfers (R19). d (was T17): the rate limit thins only rows that would be listed; every query reply is kept and listed but skips tree counts, pause and the Save store; list markers name Save File; a delete is never a duplicate of an empty put (R16, F-T16-8, F-T16-9, F-T14-3, K5, K6). e (was T25, then T18): All Messages tells the truth, then the JSON cache keys on the full payload (R17, F-T13-13, F-T14-7, F-T20-2, F-T7-7, F-T14-6, F-T20-6, K2). f (was T23): Publish and Query views say what happened (F-T8-4, F-T15-3, F-T15-7, F-T15-8, F-T16-2, F-T16-5, F-T16-6, F-T16-11, F-T14-6) | T3, T4, T7, T8, T12, T14, T16 | Each part's tests pass in its own worktree. After the merge, build, test, clippy and fmt pass on the merged tree. Part a: tests `subscription_ids_are_unique`, `overlapping_subscriptions_list_one_copy`, `failed_redeclare_removes_the_row` and `silent_queryable_reports_timeout_once` pass. `cargo test -- --ignored invalid_selector_reports_query_failure reconnect_restores_subscriptions` passes, and the re-declared subscription keeps its id. `grep -n 'source:local' src/worker/query.rs` prints nothing. `grep -n 'a queryable answered with an error' src/worker/query.rs` matches. Part b: tests `matching_*` (including `matching_stays_inside_queryable_pattern`) and `queryable_replies_full_payload` pass. `grep -n 'source:local' src/worker/queryable.rs` prints nothing. `grep -n 'listen/endpoints' src/worker/queryable.rs` matches. Part c: tests `plain_entries_respect_byte_budget`, `stale_incomplete_transfers_are_collected`, `stale_complete_transfer_is_kept` and `oversized_plain_entry_drops_previous_value` pass. Part d: tests `rate_limited_messages_still_update_tree`, `local_and_remote_replies_are_both_kept`, `query_replies_skip_tree_and_pause`, `paused_topic_does_not_use_rate_budget`, `tree_marker_counts_raw_bytes` and `delete_is_not_a_duplicate_of_empty_put` pass. `grep -rn 'use Export' src` prints nothing. Part e: tests `filter_searches_whole_list_case_insensitively`, `paused_note_is_singular_at_one_and_bounded` and `json_cache_distinguishes_shared_prefix` pass. `grep -rn 'MAX_HASH_BYTES' src` prints nothing. Part f: tests `publish_status_line_words`, `publish_button_label_rules`, `connection_notice_matches_state` and `queryable_summary_words` pass. Part f's manual check runs in T28 step 4. `git diff --name-only` from the base commit lists only the 13 owned files, and one verifier per part reports no open finding. |
| T28 | [Wave 5 · Merged · steps 1 → 2a ∥ 2b ∥ 2c → 3 → 4 · owns src/app/mod.rs, src/app/layout.rs, src/events/mod.rs, src/ui/topic_tree.rs, src/validation.rs, src/worker/connect.rs (1); src/app/mod.rs, src/app/layout.rs, src/events/mod.rs (2a); src/ui/topic_tree.rs, src/types/tree.rs (2b); src/worker/session.rs, src/worker/pipeline.rs, src/worker/query.rs, src/worker/queryable.rs (2c); src/ui/help.rs (3); src/types/message.rs, src/types/commands.rs, src/types/store.rs, src/types/limits.rs, src/payload.rs, src/worker/samples.rs, src/worker/publish.rs (4)] UI truth, help and integration. 1 (was T21): typed alerts, honest worker health and truthful query outcomes; one shared strip of zenoh's source path for every worker error, used by `connect.rs` too; subscription rows kept by id, and no second Subscribe while one is pending (F-T7-4, F-T8-7, F-T20-8, F-T16-1, F-T16-11, K7, K8, K9). 2a (was T22): header readouts say what they measure, and the error clause is cut at a clause boundary (F-T20-5, F-T7-1, F-T8-5, F-T15-5, F-T20-6, F-T20-7, F-T17-9, F-T17-3, K1, K8). 2b (was T24): topic details and tree rows tell the truth (F-T20-1, F-T20-2, F-T14-5, F-T20-11, F-T13-13, F-T13-14, F-T13-4, F-T7-7, F-T14-6, F-T20-10, K3, K4). 2c (new): spawned worker tasks never block a runtime thread on a full pipeline (K8d). 3 (was T26): Help matches behaviour (F-T18-1 to F-T18-7, F-T19-3). 4 (was T20): drop stale dead-code markers and stale dedup comments, and stop in-process tests binding a port (K9, K10, X1), then full verification of the merged branch, ignored network tests, audit, and a manual smoke run by the user | T1, T13, T19, T27 | Step 1: tests `operation_failure_is_an_error_alert`, `ping_is_sent_once_per_interval`, `unanswered_ping_marks_unhealthy_after_timeout`, `ui_stall_does_not_mark_worker_unhealthy`, `no_reply_verdict_does_not_claim_absence`, `disconnect_cancels_waiting_query`, `pending_publish_ends_on_disconnect_or_worker_loss`, `strip_source_path_removes_every_suffix`, `user_error_drops_bang_before_space`, `endpoint_parse_error_is_a_user_error`, `connect_error_text_has_no_source_path`, `subscription_rows_merge_by_id` and `double_subscribe_is_ignored_while_pending` pass. `grep -rn "starts_with('✓')" src` prints nothing. `grep -n 'Monitor connection' src/worker/connect.rs` prints nothing. Step 2a: tests `memory_readout_names_scope_and_counts_stored_payloads`, `memory_level_uses_one_threshold_set`, `memory_warning_does_not_touch_query_alert`, `peer_count_is_worded_and_shown_at_zero`, `monitor_failure_shows_in_header`, `header_error_clause_keeps_first_clause` and `form_locators_trim_inputs` pass. Step 2b: tests `branch_summary_counts_subtree`, `local_marker_follows_latest_value`, `history_empty_reason_rules`, `count_hover_names_unit`, `filter_throttle_schedules_repaint`, `history_excludes_query_replies`, `history_names_the_scan_window_instead_of_claiming_empty` and `topic_details_show_delete_and_source_time` pass. `grep -n 'id_salt(("history"' src/ui/topic_tree.rs` matches. `grep -n 'Subscribe tab' src/ui/topic_tree.rs` prints nothing. `grep -n 'filter_repaint_after' src/ui/topic_tree.rs` matches. Step 2c: test `send_event_does_not_block_the_runtime` passes. `grep -n '\.send(ZenohEvent' src/worker/query.rs src/worker/queryable.rs` prints nothing. `grep -n 'try_send(ZenohEvent::DiscoveryUpdate' src/worker/session.rs` matches. Step 3: tests `help_names_only_real_places` and `help_claims_match_limits` pass. Step 4: `grep -rn 'allow(dead_code)' src` lists only `ExplorerColors`, `card_background_color`, `MessageType::Query`, `Subscription::{reliability, mode}`, `ZenohCommand::Subscribe {reliability, mode}` and `UiAlert::Warning`. `grep -n 'listen/endpoints' src/worker/samples.rs src/worker/publish.rs` matches in both files. Every command in step 4 passes (build, test, ignored tests, clippy, fmt, audit and the residue greps), and its output is pasted into the evidence. The user runs the manual checks, including Help scrolled to its last line at 1000×600, and the evidence records what the user reports. |

**Where the old task IDs went.** Later plans that cite one of these IDs (for example "P1 T23") mean the part or step on the right.

| Old ID | Now |
|---|---|
| T5 | T27 part a |
| T6 | T27 part b |
| T9 | T27 part c |
| T17 | T27 part d |
| T25, then T18 | T27 part e |
| T23 | T27 part f |
| T21 | T28 step 1 |
| T22 | T28 step 2a |
| T24 | T28 step 2b |
| T26 | T28 step 3 |
| T20 | T28 step 4 |

## Follow-ups outside P1

The pre-flight review proposed four follow-up tasks. All four fit inside P1 and are folded in:
- Stale `#[allow(dead_code)]` markers, and in-process test sessions that bind a port (K9, K10): T28 step 4, before the verification. `app/mod.rs` and `validation.rs` markers go to T28 step 1, which owns those files.
- Worker error text at the source (the rest of K7): T28 step 1. That step makes `validation::strip_source_path` shared, so it also owns `src/worker/connect.rs` to reuse it there.
- Subscription rows kept by id, and no second Subscribe while one is pending (K8c): T28 step 1, which owns `events/mod.rs` and `ui/topic_tree.rs`.
- Non-blocking worker event sends (K8d): T28 step 2c, at the pre-flight's scope: the discovery loop in `session.rs`, the query reply task and `serve_queryable`. The command loop's sends stay blocking, so their order does not change. The `Disconnected` arm in `events/mod.rs` relies on that order.

One item lies outside P1's files and is left for the plan it belongs to:
- **P4 T10 plan text** (`2026-09-25-p4-transfer-protocol-v2.md`, the `is_chunk` step). It says to delete `is_chunk` so that `let display = !self.paused_keys.contains(&message.key);`. After T27 part d, `display` also covers query replies and the rate budget, so P4 T10 should delete only the `!is_chunk &&` term.

## Lanes and waves

```
Wave 0   T1 (deps) ─────────────────────────────────────────────┐
         T2 (split) ──► T3 (contract) ──┬──► Wave 1 ─────────────┤
                   └──► T8 (transfer)   │                        │
Wave 1   A: T4          B: T7                     C: T10  T11    D: T14  T16
Wave 2                                            C: T12(T10,T11,T14) D: T15(T14)
Wave 3                                            C: T13(T11,T12) D: T19(T15)
         ── Waves 0–3 are done ──
Wave 4   T27 (T3,T4,T7,T8,T12,T14,T16): six parts at once, one worktree each
           a (was T5)  b (was T6)  c (was T9)  d (was T17)  e (was T25, then T18)  f (was T23)
           ──► merge a–f ──► one integration run ──► one verifier per part
Wave 5   T28 (T1,T13,T19,T27): steps in order
           1 (was T21) ──► 2a (was T22) ∥ 2b (was T24) ∥ 2c (new, K8d) ──► merge 2a–2c
           ──► 3 (was T26) ──► 4 (was T20: cleanup, then full verification)
```

The maximum width is now six agents at once, in T27. Part e runs T25 before T18 in one agent, because T18 deletes `MAX_HASH_BYTES`, which `ui/messages.rs` uses until T25 removes that use.

**How T27 runs.** T27's section, under "How to run", has the commands. In short:
1. Start T27 on the board once. Record the base commit: `BASE=$(git rev-parse HEAD)`.
2. Run the six parts as parallel agents, each in its own git worktree on branch `t27-<part>` made from `$BASE`. All worktrees share one `CARGO_TARGET_DIR`, so the dependencies compile once.
3. A part edits only its Owns, runs its tests and the four Global Constraint commands in its worktree, and commits only its files.
4. Check each branch's file list against its Owns. Then merge part a first and parts b to f after it, with `git merge --no-ff`. Owned files are disjoint, so every merge is conflict-free. A conflict means a part edited a file it does not own: stop and report it.
5. Run one integration round on the merged tree: the four Global Constraint commands, the ignored tests and every Done-when item of T27.
6. One read-only verifier per part checks that part's diff against its section. Then complete T27 on the board with the combined evidence, and remove the worktrees.

No part calls a symbol that another part adds or changes. Part a adds a `MessageSource` variant; nothing matches on `MessageSource` exhaustively, so the other parts build unchanged.

**How T28 runs.** T28's section, under "How to run", has the commands. Steps 1, 3 and 4 run in order on the task branch, one agent at a time. Step 4's cleanup (its Step 0) starts only after step 3 has committed. Step 2 runs 2a, 2b and 2c as three parallel agents in worktrees off the step-1 commit, then merges them as T27 does. Only 2c runs ignored network tests, so no two worktrees bind the same test port. Each step runs the four Global Constraint commands before it commits. The user runs step 4's manual checks, and the agent records what the user reports.

**File ownership matrix.** A file listed for several tasks is always reached through a dependency chain, so no two of them run at the same time. Inside T27 each file belongs to exactly one part. Inside T28 only 2a, 2b and 2c run at once, and they share no file.

| File | Tasks (in dependency order) |
|---|---|
| Cargo.toml, Cargo.lock, .cargo/audit.toml, .github/workflows/ci.yml | T1 |
| src/main.rs | T2 → T3 → T13 |
| src/payload.rs | T3 → T28 step 4 (markers) |
| src/validation.rs | T3 (stub) → T14 → T28 step 1 (shared `strip_source_path`, `port_error` marker). T27 part f and T28 step 2a only read it. |
| src/types/mod.rs | T2 → T3 → T27 part e (was T18) |
| src/types/message.rs | T2 → T3 → T27 part a (per-subscription `MessageSource`) → T28 step 4 (markers) |
| src/types/commands.rs, store.rs | T2 → T3 → T28 step 4 (markers) |
| src/types/limits.rs | T2 → T3 → T16 → T28 step 4 (comments) |
| src/types/tree.rs | T2 → T3 → T19 (via T15 → T14 → T3) → T28 step 2b (was T24) |
| src/events/mod.rs | T2 → T3 → T11 → T12 → T28 step 1 (was T21) → T28 step 2a (was T22) |
| src/events/ingest.rs | T2 → T3 → T16 → T27 part d (was T17) |
| src/events/json_cache.rs | T2 → T27 part e (was T18) |
| src/worker/mod.rs | T2 → T3 → T12 (via T10, T11) → T27 part a (was T5) |
| src/worker/state.rs | T2 → T3 → T12 (via T10, T11) |
| src/worker/pipeline.rs | T2 → T3 → T11 → T28 step 2c |
| src/worker/session.rs | T2 → T3 → T10 → T12 → T27 part a (was T5) → T28 step 2c |
| src/worker/connect.rs | T2 → T10 (via T3) → T28 step 1 (was T21) |
| src/worker/subscribe.rs | T2 → T3 → T4 → T27 part a (was T5) |
| src/worker/query.rs | T2 → T3 → T4 → T27 part a (was T5) → T28 step 2c |
| src/worker/samples.rs | T2 (stub) → T4 (via T3) → T28 step 4 (test session, comment) |
| src/worker/publish.rs | T2 → T3 → T7 → T28 step 4 (test session) |
| src/worker/queryable.rs | T2 → T3 → T27 part b (was T6) → T28 step 2c |
| src/transfer.rs | T8 → T27 part c (was T9) |
| src/app/mod.rs | T2 → T3 → T11 → T13 → T28 step 1 (was T21) → T28 step 2a (was T22) |
| src/app/layout.rs | T2 → T3 → T12 → T13 → T28 step 1 (was T21) → T28 step 2a (was T22) |
| src/ui/topic_tree.rs | T3 → T14 → T15 → T19 → T28 step 1 (was T21) → T28 step 2b (was T24) |
| src/ui/publish.rs, query.rs | T14 (via T3) → T27 part f (was T23) |
| src/ui/messages.rs | T3 → T27 part e (was T25) |
| src/ui/help.rs | T28 step 3 (was T26) |

**Merging:** each task, part or step commits only its owned files. T27's parts branch from T27's base commit, and T28's 2a, 2b and 2c branch from the step-1 commit. Because owned files are disjoint, their merges are conflict-free by construction.

---

### Task T1: Dependency hygiene

**Owns:** `Cargo.toml`, `Cargo.lock`, `.cargo/audit.toml`, `.github/workflows/ci.yml`

**Interfaces:** Keeps the tokio features `rt-multi-thread`, `sync`, `time` and `macros`. Tests in other tasks use `#[tokio::test]`, which is already available under `full`, so no task depends on T1 for compilation.

- [ ] **Step 1: Edit `Cargo.toml`.**
  - Add `rust-version = "1.88"` under `[package]`.
  - Replace `[dependencies]` with:

```toml
[dependencies]
zenoh = { version = "1.7", features = ["unstable"] }
egui = "0.29"
eframe = { version = "0.29", default-features = false, features = ["glow", "default_fonts", "wayland", "x11", "accesskit"] }
tokio = { version = "1.0", features = ["rt-multi-thread", "sync", "time", "macros"] }
serde_json = "1.0"
chrono = "0.4"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
seahash = "4.1"
rfd = "0.14"
```

  - Delete the `[[bin]]` block and `[profile.dev]` (both restate defaults).
- [ ] **Step 2: Update the lockfile.** Run `cargo update`. Expected: zenoh 1.10.x, rustls ≥ 0.23.45, rustls-webpki ≥ 0.103.13, quinn-proto ≥ 0.11.15, and webbrowser ≥ 1.2.2 (check with `cargo tree -i <crate>`).
- [ ] **Step 3: Build and test.** Run `cargo build && cargo test && cargo clippy --all-targets -- -D warnings`. Expected: PASS.
- [ ] **Step 4: Create `.cargo/audit.toml`.**

```toml
[advisories]
# rsa Marvin attack: no fixed release exists; pulled in by zenoh-transport for
# RSA auth, which this app does not configure.
# lz4_flex: zenoh-transport 1.10 pins lz4_flex 0.10.x; track upstream.
ignore = ["RUSTSEC-2023-0071", "RUSTSEC-2026-0041"]
```

- [ ] **Step 5: Run the audit.** Run `cargo audit`. Expected: `0 vulnerabilities found` (warnings allowed).
- [ ] **Step 6: Add the CI job.** Append under `jobs:` in `.github/workflows/ci.yml`, with the same indentation as `check`:

```yaml
  audit:
    name: Security audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo install cargo-audit --locked
      - run: cargo audit
```

- [ ] **Step 7: Commit.**

```bash
git add Cargo.toml Cargo.lock .cargo/audit.toml .github/workflows/ci.yml
git commit -m "chore(deps): clear RUSTSEC advisories, trim unused deps, set MSRV 1.88"
```

---

### Task T2: Behaviour-preserving module split

**Owns:**
- `src/main.rs`
- Deletes `src/zenoh_worker.rs`, `src/types.rs`, `src/events.rs` and `src/app.rs`.
- Creates every file in the layout table below.

**Rule:** move code and extract functions only. Do not change logic, log text, constants or tests. Pre-P1 line numbers are cited below.

**Target layout:**

| New file | Content moved in (pre-P1 lines) |
|---|---|
| `src/worker/mod.rs` | `pub mod connect; pub mod pipeline; pub mod publish; pub mod query; pub mod queryable; pub mod samples; pub mod session; pub mod state; pub mod subscribe;`, plus `pub async fn zenoh_worker(...)` reduced to a dispatch loop: `recv_timeout` → `match command { … => session::handle_connect(&mut st, &ctx, …).await, … }`. It keeps the same signature and the same `Ping`→`Pong` arm. |
| `src/worker/state.rs` | `pub(crate) struct WorkerState { publishing_session, monitor_session, active_subscriptions, monitor_subscription, queryable_task }` (the locals from `zenoh_worker.rs:89-97`, `#[derive(Default)]`) and `pub(crate) struct WorkerCtx { pub event_sender: Sender<ZenohEvent>, pub local_kvstore: Arc<RwLock<HashMap<String,(String,String)>>> }` |
| `src/worker/pipeline.rs` | `message_buffer_thread` (12-70), unchanged |
| `src/worker/session.rs` | Connect arm (108-312) → `pub(crate) async fn handle_connect(st, ctx, locators, listen_port, mode, config_json)`. Disconnect arm (313-345) → `pub(crate) async fn handle_disconnect(st, ctx)`. The monitor subscription's spawned loop (211-265) is replaced by a call to `subscribe::spawn_sample_task(subscriber, MessageSource::MonitorSession, ctx.event_sender.clone())`. |
| `src/worker/subscribe.rs` | Subscribe arm (346-452) → `handle_subscribe(st, ctx, key_expr)`. Unsubscribe arm (761-771) → `handle_unsubscribe(st, ctx, id)`. New `pub(crate) fn spawn_sample_task(subscriber, source: MessageSource, tx) -> (JoinHandle<()>, oneshot::Sender<()>)`, holding the select loop that was duplicated at 211-265 and 366-430. It keeps the existing conversion code verbatim, with log level `debug!` for the monitor and `info!` for the user subscription as before (pass a `verbose: bool`). |
| `src/worker/query.rs` | Query arm (639-760) → `handle_query(st, ctx, selector, value, timeout_ms)` |
| `src/worker/publish.rs` | Publish arm (453-638) → `handle_publish(st, ctx, key, payload, encoding, from_import, filename)` |
| `src/worker/queryable.rs` | EnableQueryable (772-879) → `handle_enable(st, ctx, key_expr)`. DisableQueryable (880-885) → `handle_disable(st)`. |
| `src/worker/connect.rs` | `connect_zenoh` (913-1130) and `connect_zenoh_monitor` (1140-1244), unchanged |
| `src/worker/samples.rs` | `//! Conversion of received Zenoh samples into UI messages (filled by P1 T4).` and nothing else |
| `src/types/mod.rs` | Constants and `safe_truncate_index` (1-43), plus `mod message; mod commands; mod tree; mod limits; mod store; pub use {message::*, commands::*, tree::*, limits::*, store::*};` |
| `src/types/tree.rs` | `TransferState`, `ZenohNode` (47-166), `compute_visible_paths` (527-559), and their tests (606-686) |
| `src/types/store.rs` | `PayloadEntry`, `PayloadStoreMap` (168-180), `ActiveSubscription` (182-192) |
| `src/types/message.rs` | `ZenohMessage` (194-264), `MessageType` (341-372), `MessageSource` (374-384), `Subscription` (386-396), `DetailView` (398-405), `ConnectionStatus` (407-441) |
| `src/types/commands.rs` | `ZenohCommand` (268-309), `ZenohEvent` (311-339) |
| `src/types/limits.rs` | `Deduper`, `RateLimiter` (443-525), and the dedup tests (565-604) |
| `src/events/mod.rs` | `mod ingest; mod json_cache;`, plus `process_events` (74-165) in an `impl ZenohExplorer` block |
| `src/events/ingest.rs` | `process_single_message`, `add_message_to_browse_tree`, `add_message_with_limits` (167-358) in an `impl ZenohExplorer` block |
| `src/events/json_cache.rs` | `compute_payload_hash`, `get_cached_json` (13-70) in an `impl ZenohExplorer` block |
| `src/app/mod.rs` | `mod layout; mod theme;`, the struct (16-78), `Default` (80-84), and `new()` (86-171). Replace `use crate::zenoh_worker` with `use crate::worker`. |
| `src/app/theme.rs` | 173-300 (colour getters, `apply_theme`, animations) in an `impl ZenohExplorer` block, **except** the `ctx.request_repaint_after(66ms)` at old line 184 |
| `src/app/layout.rs` | `impl eframe::App for ZenohExplorer` (302-718). Old line 184's `ctx.request_repaint_after(std::time::Duration::from_millis(66));` goes here, directly after the `self.apply_theme(ctx);` call. The same call in the same frame keeps behaviour identical, and it lets T13, which owns `layout.rs`, remove it. |

- [ ] **Step 1: Record the baseline.** Run `cargo test 2>&1 | grep 'test result'`. Expected: `30 passed`.
- [ ] **Step 2: Create the files** in the table and move the code. In `main.rs`, replace `mod zenoh_worker;` with `mod worker;`. Update `use` paths: `crate::worker::pipeline::message_buffer_thread` and `crate::worker::zenoh_worker` in `app/mod.rs`.
- [ ] **Step 3: Extract the handlers.** The handler functions take `st: &mut WorkerState, ctx: &WorkerCtx` and the arm's fields by value. Inside them, replace the locals `publishing_session` etc. with `st.publishing_session` etc., and `event_sender` with `ctx.event_sender`. Keep the bodies otherwise identical.
- [ ] **Step 4: Unify the sample loop.** Build `spawn_sample_task` from the user-subscription loop (366-430). The monitor call site passes `MessageSource::MonitorSession`, `is_local=false` and `verbose=false`; the subscribe call site passes `MessageSource::PublishingSession` and `verbose=true`.
- [ ] **Step 5: Verify.** Run `cargo build && cargo test 2>&1 | grep 'test result' && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check`. Expected: `30 passed`, clippy clean.
- [ ] **Step 6: Smoke run.** Run `cargo run`, connect in peer mode, publish `demo/test`, and confirm it appears in the tree. Close the app.
- [ ] **Step 7: Commit.**

```bash
git add -A src
git commit -m "refactor: split worker/types/events/app into modules (no behaviour change)"
```

---

### Task T3: Shared contract

**Owns:**
- `src/types/*`, `src/payload.rs`, `src/validation.rs`, `src/main.rs`
- `src/events/mod.rs`, `src/events/ingest.rs`
- `src/worker/{mod,state,pipeline,session,subscribe,query,publish,queryable}.rs`
- `src/app/mod.rs`, `src/app/layout.rs`
- `src/ui/topic_tree.rs`

**Interfaces:** Produces everything later lanes consume. The exact signatures:
- `types::SampleKindView { Put, Delete }` (derives `Debug, Clone, Copy, PartialEq, Eq`).
- New `ZenohMessage` fields `kind: SampleKindView` and `source_timestamp: Option<DateTime<Utc>>`, plus `with_sample_meta(self, SampleKindView, Option<DateTime<Utc>>) -> Self`.
- New `ZenohNode` fields `last_kind: SampleKindView` and `last_source_time: Option<DateTime<Utc>>`. `update_data(&mut self, payload: String, encoding: String, is_local: bool, kind: SampleKindView, source_time: Option<DateTime<Utc>>)`.
- `types::FailedOp { Subscribe, Publish, Query, Queryable, Monitor }` (derives `Debug, Clone, Copy, PartialEq, Eq`) and `ZenohEvent::OperationFailed { op: FailedOp, error: String }`.
- `types::StoredValue { bytes: Vec<u8>, encoding: String }` and `type LocalKvStore = HashMap<String, StoredValue>`.
- `types::DEDUP_WINDOW: Duration` (in `limits.rs`, value 60 s for now; T16 changes it).
- Manual payload-free `Debug` for `ZenohCommand` and `ZenohMessage`.
- `payload::preview(bytes: &[u8], max_text: usize) -> String` and `payload::HEX_PREVIEW_BYTES`.
- `validation.rs`: a module doc comment only (T14 fills it).
- In `worker::pipeline`:
  - `pub(crate) type EventTx = std::sync::mpsc::Sender<ZenohEvent>`
  - `pub(crate) fn event_channel(capacity: usize) -> (EventTx, Receiver<ZenohEvent>)`, which ignores capacity until T11
  - `pub(crate) fn send_sample(tx: &EventTx, drops: &AtomicUsize, msg: ZenohMessage)`
  - `message_buffer_thread(rx, ui: EventTx, notify: impl Fn() + Send + 'static)`
- `WorkerCtx` gains `sample_drops: Arc<AtomicUsize>`, and `local_kvstore` becomes `Arc<RwLock<LocalKvStore>>`.
- `zenoh_worker(command_receiver, event_sender: EventTx, local_kvstore: Arc<RwLock<LocalKvStore>>, sample_drops: Arc<AtomicUsize>)`.
- `ZenohExplorer` fields:
  - `sample_drops: Arc<AtomicUsize>`
  - `local_kvstore: Arc<RwLock<LocalKvStore>>`, storing the SAME Arc given to the worker
  - `tree_filter_cache: Option<(String, u64, Instant, HashSet<String>)>`
  - `max_messages` defaulting to `50_000`
- `#[cfg(test)] ZenohExplorer::test_app() -> (Self, std::sync::mpsc::Sender<ZenohEvent>)`.

- [ ] **Step 1: Write the failing tests.**
  - In `src/payload.rs`:

```rust
//! Bounded, allocation-light payload previews for display.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preview_empty() {
        assert_eq!(preview(b"", 10), "");
    }

    #[test]
    fn preview_short_text_is_verbatim() {
        assert_eq!(preview(b"hello", 10), "hello");
    }

    #[test]
    fn preview_long_text_is_cut_with_suffix() {
        assert_eq!(preview(b"hello world", 5), "hello... [+6 bytes]");
    }

    #[test]
    fn preview_keeps_text_when_cut_mid_char() {
        let s = "é".repeat(10); // 20 bytes
        assert_eq!(preview(s.as_bytes(), 5), "éé... [+16 bytes]");
    }

    #[test]
    fn preview_binary_is_hex() {
        assert_eq!(preview(&[0xff, 0x00], 10), "[binary 2 bytes] ff 00");
    }

    #[test]
    fn preview_long_binary_is_capped() {
        let p = preview(&vec![0xffu8; 1000], 10_000);
        assert!(p.starts_with("[binary 1000 bytes] ff"));
        assert!(p.ends_with("..."));
        assert_eq!(p.matches("ff").count(), HEX_PREVIEW_BYTES);
    }
}
```

  - In the `src/types/commands.rs` tests:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::*;

    #[test]
    fn debug_output_omits_payload_bytes() {
        let cmd = ZenohCommand::Publish {
            key: "k".into(),
            payload: vec![7u8; 1 << 20],
            encoding: "application/octet-stream".into(),
            from_import: true,
            filename: None,
        };
        let s = format!("{:?}", cmd);
        assert!(s.len() < 300, "{}", &s[..300.min(s.len())]);
        assert!(s.contains("1048576"));
        let msg = ZenohMessage::new_with_bytes(
            "k".into(), "p".into(), vec![7u8; 1 << 20], "x".into(), chrono::Utc::now(),
            MessageType::Subscribe, false, MessageSource::MonitorSession,
        );
        assert!(format!("{:?}", msg).len() < 400);
    }
}
```

  - In `src/events/mod.rs`:

```rust
#[cfg(test)]
mod tests {
    use crate::app::ZenohExplorer;
    use crate::types::*;

    #[test]
    fn failed_query_replaces_waiting_alert() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.query_alert = Some("Query sent for 'x'. Waiting for responses...".into());
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Query, error: "bad selector".into() }).unwrap();
        app.process_events();
        assert!(app.query_alert.clone().unwrap().contains("bad selector"));
    }

    #[test]
    fn failed_queryable_unchecks_toggle() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.queryable_enabled = true;
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Queryable, error: "x".into() }).unwrap();
        app.process_events();
        assert!(!app.queryable_enabled);
        assert!(app.ui_alert.as_deref().unwrap_or("").contains("Queryable"));
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- payload:: debug_output failed_`. Expected: compile errors for missing items.

- [ ] **Step 3: Implement `payload.rs`** (above its tests), and add `mod payload; mod validation;` to `main.rs`:

```rust
/// Maximum bytes rendered as hex for a binary payload.
pub const HEX_PREVIEW_BYTES: usize = 256;

/// Build a display preview of at most `max_text` bytes of text (plus a size
/// suffix) without copying the whole payload.
pub fn preview(bytes: &[u8], max_text: usize) -> String {
    let head = &bytes[..bytes.len().min(max_text)];
    let text = match std::str::from_utf8(head) {
        Ok(s) => Some(s),
        // Incomplete trailing char (the cut split it): keep the valid prefix.
        Err(e) if e.error_len().is_none() => Some(
            std::str::from_utf8(&head[..e.valid_up_to()]).expect("valid_up_to prefix is UTF-8"),
        ),
        Err(_) => None,
    };
    match text {
        Some(s) if s.len() == bytes.len() => s.to_string(),
        Some(s) => format!("{}... [+{} bytes]", s, bytes.len() - s.len()),
        None => {
            let shown = bytes.len().min(HEX_PREVIEW_BYTES);
            let hex: Vec<String> = bytes[..shown].iter().map(|b| format!("{:02x}", b)).collect();
            let more = if bytes.len() > shown { "..." } else { "" };
            format!("[binary {} bytes] {}{}", bytes.len(), hex.join(" "), more)
        }
    }
}
```

  - `src/validation.rs`: `//! Pre-flight validation of user-typed key expressions and selectors (filled by P1 T14).`

- [ ] **Step 4: Add the types.**
  - `message.rs`: add `SampleKindView`, the two `ZenohMessage` fields (initialised `Put`/`None` in `new_with_bytes`), and:

```rust
    /// Attach the sample kind and publisher timestamp.
    pub fn with_sample_meta(mut self, kind: SampleKindView, ts: Option<DateTime<Utc>>) -> Self {
        self.kind = kind;
        self.source_timestamp = ts;
        self
    }
```

  - `tree.rs`: add the two `ZenohNode` fields (initialised `Put`/`None`), extend `update_data` with `kind` and `source_time`, and assign them.
  - `store.rs`:

```rust
/// A locally published value served by the explorer's queryable.
#[derive(Debug, Clone)]
pub struct StoredValue {
    pub bytes: Vec<u8>,
    pub encoding: String,
}

/// Key → last locally published value.
pub type LocalKvStore = std::collections::HashMap<String, StoredValue>;
```

  - `limits.rs`: `pub const DEDUP_WINDOW: Duration = Duration::from_secs(60);`
  - `commands.rs`: add `FailedOp` with the doc `/// Which worker operation failed, so the UI can route the error.`, and the variant `OperationFailed { op: FailedOp, error: String }` on `ZenohEvent`.

- [ ] **Step 5: Write the manual `Debug` impls.**
  - Remove `Debug` from the derives on `ZenohCommand` and `ZenohMessage`. `ZenohMessage` keeps `#[derive(Clone)]`.
  - Add to `commands.rs`:

```rust
impl std::fmt::Debug for ZenohCommand {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ZenohCommand::Connect { locators, listen_port, mode, .. } => f
                .debug_struct("Connect")
                .field("locators", locators)
                .field("listen_port", listen_port)
                .field("mode", mode)
                .finish_non_exhaustive(),
            ZenohCommand::Disconnect => f.write_str("Disconnect"),
            ZenohCommand::Subscribe { key_expr, .. } => f.debug_struct("Subscribe").field("key_expr", key_expr).finish_non_exhaustive(),
            ZenohCommand::Unsubscribe { subscription_id } => f.debug_struct("Unsubscribe").field("subscription_id", subscription_id).finish(),
            ZenohCommand::Publish { key, payload, encoding, from_import, filename } => f
                .debug_struct("Publish")
                .field("key", key)
                .field("payload_len", &payload.len())
                .field("encoding", encoding)
                .field("from_import", from_import)
                .field("filename", filename)
                .finish(),
            ZenohCommand::Query { selector, timeout_ms, .. } => f.debug_struct("Query").field("selector", selector).field("timeout_ms", timeout_ms).finish_non_exhaustive(),
            ZenohCommand::EnableQueryable { key_expr } => f.debug_struct("EnableQueryable").field("key_expr", key_expr).finish(),
            ZenohCommand::DisableQueryable => f.write_str("DisableQueryable"),
            ZenohCommand::Ping => f.write_str("Ping"),
        }
    }
}
```

  - Add to `message.rs`:

```rust
impl std::fmt::Debug for ZenohMessage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ZenohMessage")
            .field("key", &self.key)
            .field("encoding", &self.encoding)
            .field("kind", &self.kind)
            .field("type", &self.message_type)
            .field("payload_len", &self.payload_bytes.as_ref().map_or(self.payload.len(), Vec::len))
            .finish_non_exhaustive()
    }
}
```

- [ ] **Step 6: Add the pipeline contract** in `worker/pipeline.rs`:

```rust
use std::sync::atomic::{AtomicUsize, Ordering};

/// Sender half used for every worker → buffer → UI event. T11 turns this into
/// a bounded `SyncSender`; all code must go through this alias.
pub(crate) type EventTx = std::sync::mpsc::Sender<ZenohEvent>;

/// Create an event channel. `capacity` is honoured once channels are bounded (T11).
pub(crate) fn event_channel(_capacity: usize) -> (EventTx, std::sync::mpsc::Receiver<ZenohEvent>) {
    std::sync::mpsc::channel()
}

/// Send a data sample. Once channels are bounded (T11) a full pipeline drops
/// and counts the sample instead of growing memory.
pub(crate) fn send_sample(tx: &EventTx, drops: &AtomicUsize, msg: ZenohMessage) {
    if tx.send(ZenohEvent::MessageReceived(msg)).is_err() {
        drops.fetch_add(1, Ordering::Relaxed);
    }
}
```

  - Change `message_buffer_thread`'s signature to `(buffer_receiver: Receiver<ZenohEvent>, ui_sender: EventTx, notify: impl Fn() + Send + 'static)`. Call `notify()` after each successful `ui_sender.send(...)`. The body is otherwise unchanged until T11.

- [ ] **Step 7: Thread the contract through the worker.**
  - `WorkerCtx` gets `sample_drops: Arc<AtomicUsize>`, and `local_kvstore` becomes `Arc<RwLock<LocalKvStore>>`.
  - Change the `zenoh_worker` signature as listed in Interfaces.
  - Every `send(ZenohEvent::MessageReceived(message))` in `subscribe.rs`, `query.rs` and `publish.rs` becomes `send_sample(&tx, &drops, message)`. Pass `ctx.sample_drops.clone()` into spawned tasks.
  - In `publish.rs`, the kvstore insert becomes `store.insert(key.clone(), StoredValue { bytes: payload.clone(), encoding: encoding.clone() })`.
  - In `queryable.rs`, destructure `StoredValue` and reply with `value.bytes.clone()` / `value.encoding.as_str()`. Matching stays as is; T6 replaces it.

- [ ] **Step 8: Update `app/mod.rs`.**
  - Create the channels with `worker::pipeline::event_channel(0)` for the worker→buffer and buffer→UI pairs. Keep the command channel `mpsc::channel()`.
  - Create `let sample_drops = Arc::new(AtomicUsize::new(0));`.
  - `let local_kvstore = Arc::new(RwLock::new(LocalKvStore::new()));`, passing `local_kvstore.clone()` to the worker and storing `local_kvstore` itself on the struct. This fixes the mismatched Arc at old `app.rs:162`.
  - Pass `|| {}` as `notify`.
  - Add the fields listed in Interfaces. Use `deduper: Deduper::new(DEDUP_WINDOW)` and `max_messages: 50_000`.
  - Add the test helper:

```rust
#[cfg(test)]
impl ZenohExplorer {
    /// App wired to a test-controlled event channel.
    pub(crate) fn test_app() -> (Self, std::sync::mpsc::Sender<ZenohEvent>) {
        let mut app = Self::new();
        let (tx, rx) = std::sync::mpsc::channel();
        app.event_receiver = Some(rx);
        (app, tx)
    }
}
```

- [ ] **Step 9: Update events and the tree panel.**
  - `events/ingest.rs`: the `update_data` call gains `message.kind` and `message.source_timestamp`.
  - `events/mod.rs`: add the arm

```rust
                ZenohEvent::OperationFailed { op, error } => {
                    let msg = format!("{:?} failed: {}", op, error);
                    error!("{}", msg);
                    match op {
                        FailedOp::Query => self.query_alert = Some(msg.clone()),
                        FailedOp::Queryable => self.queryable_enabled = false,
                        _ => {}
                    }
                    self.ui_alert = Some(msg);
                }
```

  - In `ui/topic_tree.rs`, the filter-cache code (old 234-247) inserts `(filter_lower.clone(), self.tree_version, Instant::now(), visible)` and reads the four-tuple. The staleness logic is unchanged here; T19 changes it.

- [ ] **Step 10: Update the header and logging.**
  - In `app/layout.rs`, change the header drop label (old 446-458) to show three counts when any is > 0: `"({} dropped, {} not listed (rate), {} pipeline)"` with `self.messages_dropped`, `self.rate_limit_drops` and `self.sample_drops.load(Ordering::Relaxed)`.
  - Logging:
    - In `main.rs`, replace `tracing_subscriber::fmt::init();` with:

```rust
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info,zenoh=warn")),
        )
        .init();
```

    - In every `src/worker/*.rs` file, demote per-sample, per-reply and per-query `info!` lines to `debug!`. That covers the old 378, 417, 663, 670, 674, 688, 800-852 and `"Worker received command"`.

- [ ] **Step 10b: UI-review contract (F-T8-4, F-T14-6, F-T17-10).** Later lanes need these, and none of them owns the shared files.
  - Tests first. In `message.rs` tests:

```rust
    #[test]
    fn local_time_format_marks_other_days() {
        let now = chrono::Utc::now();
        assert_eq!(format_local_time(&now, &now).len(), "12:00:00.000".len());
        let old = now - chrono::Duration::days(2);
        assert!(format_local_time(&old, &now).contains('-'), "an earlier day shows its date");
    }
```

    In the `events/mod.rs` tests:

```rust
    #[test]
    fn publish_outcome_updates_status() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::Published { key: "k".into(), bytes: 3 }).unwrap();
        app.process_events();
        assert!(matches!(app.publish_status, Some(PublishStatus::Published { bytes: 3, .. })));
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Publish, error: "k: bad".into() }).unwrap();
        app.process_events();
        assert_eq!(app.publish_status, Some(PublishStatus::Failed("k: bad".into())));
    }
```

  - Run `cargo test -- local_time_format publish_outcome`. Expected: compile error (`format_local_time`, `PublishStatus` and `ZenohEvent::Published` do not exist yet).
  - Then implement:
    - `commands.rs`: add `ZenohEvent::Published { key: String, bytes: usize }` with the doc `/// A put the worker completed (every Ok arm of the publish handler, T7).`. `ZenohEvent` keeps its derived `Debug` (only `ZenohCommand` and `ZenohMessage` have manual ones), so no `Debug` arm is needed.
    - `message.rs`: add

```rust
/// What the Publish view shows under its button (T23 renders it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishStatus {
    Sending { key: String, bytes: usize },
    Published { key: String, bytes: usize, at: DateTime<Utc> },
    Failed(String),
}

/// Local wall-clock time for display; the date is prefixed when `ts` is not
/// on the same local day as `now`. Every list, history card and query result
/// uses this instead of formatting UTC directly.
pub fn format_local_time(ts: &DateTime<Utc>, now: &DateTime<Utc>) -> String {
    let local = ts.with_timezone(&chrono::Local);
    if local.date_naive() == now.with_timezone(&chrono::Local).date_naive() {
        local.format("%H:%M:%S%.3f").to_string()
    } else {
        local.format("%Y-%m-%d %H:%M:%S%.3f").to_string()
    }
}
```

    - `app/mod.rs`: add the fields `publish_status: Option<PublishStatus>` (init `None`), `connect_started: Option<Instant>` and `connect_target: String` (init `None` / `String::new()`). T23 and T12 fill them.
    - `events/mod.rs`: add the arm `ZenohEvent::Published { key, bytes } => self.publish_status = Some(PublishStatus::Published { key, bytes, at: chrono::Utc::now() }),` and, in the `OperationFailed` arm, `FailedOp::Publish => self.publish_status = Some(PublishStatus::Failed(error.clone())),`.

- [ ] **Step 11: Verify.**

```bash
cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check && grep -rn 'mpsc::Sender<ZenohEvent>' src/worker
```

Expected: tests pass, clippy clean, and grep prints only the `EventTx` alias line in `pipeline.rs`.

- [ ] **Step 12: Commit.**

```bash
git add -A src
git commit -m "feat(contract): shared types, OperationFailed, EventTx, payload preview, payload-free Debug"
```

---

### Task T4: Real sample metadata (Lane A)

**Owns:** `src/worker/samples.rs`, `src/worker/subscribe.rs`, `src/worker/query.rs`

**Interfaces:** Produces `pub(crate) fn message_from_sample(sample: &zenoh::sample::Sample, message_type: MessageType, is_local: bool, source: MessageSource) -> ZenohMessage`.

- [ ] **Step 1: Write the failing test** in `src/worker/samples.rs`:

```rust
#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::time::Duration;

    pub(crate) async fn local_session() -> zenoh::Session {
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        zenoh::open(c).await.unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn message_from_sample_keeps_encoding_kind_and_timestamp() {
        let s = local_session().await;
        let sub = s.declare_subscriber("t/meta").await.unwrap();
        s.put("t/meta", r#"{"a":1}"#)
            .encoding(zenoh::bytes::Encoding::APPLICATION_JSON)
            .timestamp(s.new_timestamp())
            .await
            .unwrap();
        let put = tokio::time::timeout(Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        let m = message_from_sample(&put, MessageType::Subscribe, false, MessageSource::PublishingSession);
        assert_eq!(m.encoding, "application/json");
        assert_eq!(m.kind, SampleKindView::Put);
        assert!(m.source_timestamp.is_some());
        assert_eq!(m.payload, r#"{"a":1}"#);

        s.delete("t/meta").await.unwrap();
        let del = tokio::time::timeout(Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        let m = message_from_sample(&del, MessageType::Subscribe, false, MessageSource::PublishingSession);
        assert_eq!(m.kind, SampleKindView::Delete);
        assert_eq!(m.payload, "[DELETE]");
    }
}
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test message_from_sample`. Expected: compile error (`message_from_sample` not found).
- [ ] **Step 3: Implement** in `samples.rs` (above the tests):

```rust
use chrono::{DateTime, Utc};
use crate::types::*;

/// Convert a received Zenoh sample into a UI message, preserving encoding,
/// kind and the publisher's timestamp. Copies the payload once.
pub(crate) fn message_from_sample(
    sample: &zenoh::sample::Sample,
    message_type: MessageType,
    is_local: bool,
    source: MessageSource,
) -> ZenohMessage {
    let raw_bytes: Vec<u8> = sample.payload().to_bytes().into_owned();
    let kind = match sample.kind() {
        zenoh::sample::SampleKind::Put => SampleKindView::Put,
        zenoh::sample::SampleKind::Delete => SampleKindView::Delete,
    };
    let display = match kind {
        SampleKindView::Delete => "[DELETE]".to_string(),
        SampleKindView::Put => crate::payload::preview(&raw_bytes, MAX_UI_DISPLAY_SIZE),
    };
    let source_ts = sample
        .timestamp()
        .map(|ts| DateTime::<Utc>::from(ts.get_time().to_system_time()));
    let filename = sample
        .attachment()
        .and_then(|a| a.try_to_string().ok())
        .map(|s| s.into_owned());
    ZenohMessage::new_with_bytes(
        sample.key_expr().to_string(),
        display,
        raw_bytes,
        sample.encoding().to_string(),
        Utc::now(),
        message_type,
        is_local,
        source,
    )
    .with_filename(filename)
    .with_sample_meta(kind, source_ts)
}
```

  - Verify `Timestamp::get_time()` and `NTP64::to_system_time()` at https://docs.rs/zenoh/latest/zenoh/time/struct.Timestamp.html. If `to_system_time` is not on `NTP64` in the locked version, use `std::time::UNIX_EPOCH + ts.get_time().to_duration()`.
- [ ] **Step 4: Replace the conversion sites.**
  - In `spawn_sample_task` (`subscribe.rs`), replace everything from `let raw_bytes` through `.with_filename(filename);` with `let message = super::samples::message_from_sample(&sample, MessageType::Subscribe, false, source.clone());`.
  - In `query.rs`, compute `is_local` first, then `message_from_sample(&sample, MessageType::QueryReply, is_local, MessageSource::PublishingSession)`.
- [ ] **Step 5: Verify.**

```bash
cargo test && cargo clippy --all-targets -- -D warnings && grep -rn '"text/plain".to_string()' src/worker
```

Expected: pass, and grep prints nothing.
- [ ] **Step 6: Commit.**

```bash
git add src/worker/samples.rs src/worker/subscribe.rs src/worker/query.rs
git commit -m "fix(worker): keep sample encoding, kind and source timestamp"
```

---

### Task T7: Publish path (Lane B)

**Owns:** `src/worker/publish.rs`

**Interfaces:** `pub(crate) enum PublishShape { Single, Chunked { chunks: usize } }` and `pub(crate) fn publish_shape(len: usize) -> PublishShape`.

- [ ] **Step 1: Write the failing test.**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publish_shape_chunks_above_chunk_size() {
        let c = crate::transfer::CHUNK_SIZE;
        assert_eq!(publish_shape(0), PublishShape::Single);
        assert_eq!(publish_shape(c), PublishShape::Single);
        assert_eq!(publish_shape(c + 1), PublishShape::Chunked { chunks: 2 });
    }
}
```

  - Also add, in the same test module (F-T8-3):

```rust
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failed_put_is_not_echoed_or_stored() {
        use crate::worker::state::{WorkerCtx, WorkerState};
        use std::sync::{atomic::AtomicUsize, Arc, RwLock};
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        let mut st = WorkerState {
            publishing_session: Some(Arc::new(zenoh::open(c).await.unwrap())),
            ..Default::default()
        };
        let (tx, rx) = crate::worker::pipeline::event_channel(64);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        // `demo//x` has an empty chunk: zenoh rejects it when the put resolves.
        handle_publish(&mut st, &ctx, "demo//x".into(), b"v".to_vec(), "text/plain".into(), false, None).await;
        let events: Vec<ZenohEvent> = rx.try_iter().collect();
        assert!(events.iter().any(|e| matches!(e, ZenohEvent::OperationFailed { op: FailedOp::Publish, .. })));
        assert!(!events.iter().any(|e| matches!(e, ZenohEvent::MessageReceived(_) | ZenohEvent::Published { .. })));
        assert!(ctx.local_kvstore.read().unwrap().is_empty(), "a failed put must not be served by the queryable");
    }
```

  If P1 T2 named the `WorkerState`/`WorkerCtx` fields differently, use T2's names.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- publish_shape failed_put_is_not_echoed_or_stored`. Expected: FAIL.
- [ ] **Step 3: Implement.**

```rust
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum PublishShape {
    Single,
    Chunked { chunks: usize },
}

/// Payloads above one chunk are split so each Zenoh message stays well under
/// a receiver's default 1 GiB max_message_size.
pub(crate) fn publish_shape(len: usize) -> PublishShape {
    if len > crate::transfer::CHUNK_SIZE {
        PublishShape::Chunked { chunks: len.div_ceil(crate::transfer::CHUNK_SIZE) }
    } else {
        PublishShape::Single
    }
}
```

  - In `handle_publish`:
    - Replace the `payload_str` block with `let payload_str = crate::payload::preview(&payload, 256);`.
    - Delete the local `CHUNK_SIZE`/`MAX_SINGLE_PAYLOAD` consts, and branch on `publish_shape(payload_len)`. `Chunked` keeps the existing loop using `crate::transfer::CHUNK_SIZE`.
    - Delete the `> 100 * 1024 * 1024` branch.
    - In every `Err(e)` arm, send `OperationFailed { op: FailedOp::Publish, error: format!("{}: {}", key, e) }`.
    - In the echo branch, send the local echo only inside `Ok(_)`.
  - Move the kvstore insert (still `!from_import && len <= 10 MiB`) from before the put into the `Ok(_)` arm of the single-put path, so a rejected put is never served (F-T8-3).
  - In every `Ok(_)` arm (the single put, and after the last chunk of a chunked send), send `ZenohEvent::Published { key: key.clone(), bytes: payload_len }`. Import and chunked paths now commit in the Publish view too (F-T8-4, F-T15-6).
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -n '100 \* 1024 \* 1024' src/worker/publish.rs`. Expected: grep prints nothing.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/publish.rs
git commit -m "fix(publish): chunk above 64 MiB, echo only on success, report failures"
```

---

### Task T8: Safe export (Lane B)

**Owns:** `src/transfer.rs`

- [ ] **Step 1: Write the failing tests** in the existing `transfer.rs` test module, which already has `entry_with(bytes, ts)`:

```rust
    #[test]
    fn export_rejects_claimed_size_larger_than_chunks() {
        let mut store = PayloadStoreMap::new();
        for i in 0..2 {
            insert_payload(&mut store, format!("t/__chunk/{}/2/{}", 2 * CHUNK_SIZE, i), entry_with(vec![], 100 + i as i64));
        }
        let err = get_payload_for_export(&store, "t").unwrap_err();
        assert!(err.contains("chunk"), "{err}");
    }

    #[test]
    fn sanitize_strips_paths_and_controls() {
        assert_eq!(sanitize_filename("../../etc/passwd").as_deref(), Some("passwd"));
        assert_eq!(sanitize_filename("C:\\x\\evil.exe").as_deref(), Some("evil.exe"));
        assert_eq!(sanitize_filename("a\u{202e}fdp.exe").as_deref(), Some("afdp.exe"));
        assert_eq!(sanitize_filename(" .. "), None);
        assert_eq!(sanitize_filename(&"a".repeat(400)).map(|s| s.len()), Some(255));
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- export_rejects sanitize_`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - In `is_sane`, append `&& self.total_size > (self.total_chunks - 1).saturating_mul(CHUNK_SIZE)`.
  - In `get_payload_for_export`, before `Vec::with_capacity`:

```rust
    let last = progress.total_chunks - 1;
    let mut actual = 0usize;
    for (i, e) in &by_index {
        let expected = if *i == last { progress.total_size - last * CHUNK_SIZE } else { CHUNK_SIZE };
        if e.bytes.len() != expected {
            return Err(format!("Corrupt transfer: chunk {} has {} bytes, expected {}", i, e.bytes.len(), expected));
        }
        actual += e.bytes.len();
    }
    if actual != progress.total_size {
        return Err(format!("Corrupt transfer: chunks total {} bytes, expected {}", actual, progress.total_size));
    }
```

  - Add:

```rust
/// Reduce a network-supplied filename to a safe final path component.
pub fn sanitize_filename(name: &str) -> Option<String> {
    let last = name.rsplit(['/', '\\', ':']).next().unwrap_or("");
    let cleaned: String = last
        .chars()
        .filter(|c| !c.is_control() && !matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}'))
        .collect();
    let trimmed = cleaned.trim().trim_matches('.').trim();
    if trimmed.is_empty() {
        return None;
    }
    let mut end = trimmed.len().min(255);
    while !trimmed.is_char_boundary(end) {
        end -= 1;
    }
    Some(trimmed[..end].to_string())
}
```

  - In `suggested_export_filename`, the transmitted branch becomes `if let Some(name) = transmitted.and_then(sanitize_filename) { return name; }`.
  - Update the module doc lines 7-9: chunking happens above `CHUNK_SIZE`, and `CHUNK_SIZE` is shared with `worker::publish`.
  - Fix any existing test whose chunk sizes now violate the tighter `is_sane`, using real chunk-length fixtures. Do not weaken the check.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/transfer.rs
git commit -m "fix(transfer): validate chunk lengths before allocating; sanitize filenames"
```

---

### Task T10: Connection config safety (Lane C)

**Owns:** `src/worker/connect.rs`, `src/worker/session.rs`

**Interfaces:**
- `pub(crate) fn parse_listen_port(listen_port: &str) -> Result<u16, String>` in `connect.rs`. There is no `monitor_port` any more: the monitor runs in client mode and listens on nothing, so the old Listen Port + 1000 port (and its `u16` overflow, R7) is gone.
- `pub(crate) fn monitor_endpoints(locators: &str, listen_port: &str, mode: &str) -> Result<Vec<String>, String>` in `connect.rs`: the endpoints the client-mode monitor dials, from the publishing session's form values.
- `connect_zenoh_monitor(locators: &str, listen_port: &str, mode: &str)` (was `(locators, monitor_port, mode)`): the second parameter is now the publishing session's listen port, and `mode` is the publishing session's mode. The monitor itself always opens in client mode. The return type is unchanged. Later plans that call it (P5) use this order and pass no monitor port.

- [ ] **Step 1: Write the failing test** in `connect.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listen_port_rejects_zero_and_garbage() {
        assert_eq!(parse_listen_port(" 7447 "), Ok(7447));
        assert_eq!(parse_listen_port("65000"), Ok(65000)); // no second port at + 1000 any more
        for bad in ["", "abc", "0", "70000"] {
            assert!(parse_listen_port(bad).is_err(), "{bad}");
        }
    }
}
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test listen_port`. Expected: FAIL. (Step 3 adds `connect_error_text_has_no_source_path`, `monitor_endpoints_follow_publishing_mode` and the two ignored tests before their code, so run each once to see it fail first.)
- [ ] **Step 3: Implement.**

```rust
/// The publishing session's listen port. Port 0 is refused: zenoh would pick a
/// port at open time, and the monitor could not dial it.
pub(crate) fn parse_listen_port(listen_port: &str) -> Result<u16, String> {
    match listen_port.trim().parse::<u16>() {
        Ok(0) | Err(_) => Err(format!("invalid listen port '{listen_port}': use 1 to 65535")),
        Ok(p) => Ok(p),
    }
}
```

  - Delete both `set_max_message_size` blocks.
  - Replace every config-setter/endpoint `.unwrap()` with `.map_err(|e| format!("config error: {e:?}"))?`, for example:

```rust
config.set_mode(Some(WhatAmI::Peer)).map_err(|e| format!("config error: {e:?}"))?;
let ep = listen_endpoint
    .parse()
    .map_err(|e| format!("invalid listen endpoint {listen_endpoint}: {e}"))?;
config.listen.endpoints.set(vec![ep]).map_err(|e| format!("config error: {e:?}"))?;
```

  - In `connect_zenoh`, replace `listen_port.parse::<u16>().unwrap_or(7447)` with `parse_listen_port(listen_port)?`. A bad listen port now fails the connect in peer mode, before any session opens, through the existing `ConnectionError` path. Client mode never reads it.
  - In `session.rs` `handle_connect`, delete the `+ 1000` computation (`let monitor_port = listen_port.parse::<u16>().unwrap_or(7447) + 1000;` and its `info!`) and pass `&listen_port` to `connect_zenoh_monitor`. The F-T20-7 bullet below orders this change.
  - In both monitor-failure arms, send `OperationFailed { op: FailedOp::Monitor, error: e.to_string() }` before the existing `MonitorConnected`.
  - **Readable connection errors (F-T17-3).** zenoh's messages end in ` at <cargo registry path>/<file>.rs:<line>.`, which leaks the build machine's home directory. Add, in `connect.rs`:

```rust
/// Drop zenoh's trailing " at <source path>:<line>." and lead with advice the
/// user can act on.
pub(crate) fn user_error(mode: &str, locators: &str, raw: &str) -> String {
    let clean = match raw.find(" at ") {
        Some(i) if raw[i..].contains(".rs:") => raw[..i].trim_end_matches('!').trim().to_string(),
        _ => raw.trim().to_string(),
    };
    if mode == "client" && locators.is_empty() {
        return format!("Client mode needs a router address. Enter one, or switch Mode to Peer. ({clean})");
    }
    format!("Could not connect in {mode} mode: {clean}")
}
```

    Use it wherever `ConnectionError` text is built (the old `format!("Connection failed in {} mode: {}", …)`), and add the test

```rust
    #[test]
    fn connect_error_text_has_no_source_path() {
        let raw = "Unable to connect to any of [tcp/localhost:7447]! at /home/u/.cargo/registry/src/x/zenoh-1.10.1/src/net/runtime/orchestrator.rs:374.";
        let t = user_error("client", "tcp/localhost:7447", raw);
        assert!(!t.contains(".rs:") && !t.contains("/home/"), "{t}");
        assert!(user_error("client", "", "No peer specified").contains("needs a router address"));
    }
```

  - **An explicit peer endpoint must be reached (F-T17-2).** In peer mode zenoh retries connect endpoints in the background, so `open` succeeds and the header turns green for an unreachable or invalid target. When `locators` is non-empty in peer mode, set the peer's connect timeout and failure policy before `open`:

```rust
config
    .insert_json5("connect/timeout_ms", r#"{ "peer": 10000 }"#)
    .map_err(|e| format!("config error: {e:?}"))?;
config
    .insert_json5("connect/exit_on_failure", r#"{ "peer": true }"#)
    .map_err(|e| format!("config error: {e:?}"))?;
```

    Check both keys against the `DEFAULT_CONFIG.json5` of the locked zenoh before relying on them; if a key has a different shape, stop and report. Add the ignored test `peer_mode_unreachable_endpoint_fails`: `connect_zenoh("tcp/10.255.255.1:7447", "27701", "peer", "{}")` returns `Err` within 15 s.
  - **The monitor must see traffic (F-T20-7).** Today the monitor copies the form's mode. In peer mode with no address it has scouting, gossip and connect endpoints all off, so its `**` subscription is isolated and the tree stays empty while the header says "Connected". Giving a *peer-mode* monitor an endpoint does not fix this. zenoh's default peer routing does not forward one peer's samples to another peer, but a peer or a router does forward them to its clients.
    - **Evidence** (zenoh 1.7.2, the current `Cargo.lock` version; T1 moves the lock to 1.10.x, so the ignored test below proves it again there). The publishing peer S listens, and the monitor dials S's listener with listen, multicast and gossip off. A third peer dials S and publishes. Results:
      - Peer-mode monitor: the third peer's samples were **not** received, not even when the third peer also listened. S's own samples were received.
      - Client-mode monitor: the third peer's samples were received.
      - Also checked on 1.7.2, all received: S listening on `tcp/[::]` with multicast and gossip on (as `connect_zenoh` sets it), with the monitor dialling `[::1]` and `127.0.0.1`; a client S, client monitor and client third party behind one router.
      - S's `info().peers_zid()` does not list the client-mode monitor, so T22's peer count stays true.
    - **So the monitor always opens in client mode**, whatever mode the publishing session uses:
      - **Publishing session in peer mode** (with or without an address): the monitor dials the publishing session's own listener. `connect_zenoh` listens on `<protocol>/[::]:<listen_port>`, with `<protocol>` from the first locator (`tcp` when the address is empty). An unspecified host maps to loopback, and both loopbacks are listed, IPv6 first: `<protocol>/[::1]:<listen_port>`, then `<protocol>/127.0.0.1:<listen_port>`. A client tries its endpoints in order and keeps the first that connects (zenoh `connect_peers_single_link`). `[::1]` comes first because a `[::]` socket is IPv6-only on Windows: zenoh 1.7.2 and 1.10.1 do not set `IPV6_V6ONLY`, so the OS default applies, and the release builds Windows. `127.0.0.1` is the fallback for hosts without IPv6 loopback; on 1.7.2 an IPv4-only listener was reached this way.
      - **Publishing session in client mode** (form mode "client" with an address): the monitor dials the same router locators in client mode, because routers forward to clients. The monitor already copies them today; only its mode changes.
      - **Monitor config:** mode client, `listen/endpoints = []`, multicast and gossip scouting off, and `connect/endpoints` from `monitor_endpoints`. Keep the batch-size and rx-buffer lines. zenoh's client defaults (`connect/timeout_ms` 0, `connect/exit_on_failure` true) make `open` fail at once when nothing answers (under 2 ms on 1.7.2). That error reaches the monitor-failure arm, and the header then reads "monitor off" (T22) instead of a blind "Connected".
    - Plumbing first, with no behaviour change: change `connect_zenoh_monitor` to `(locators, listen_port, mode)` (see Interfaces) and have `session.rs` pass `&listen_port` instead of computing `+ 1000`. Until the fix, the still peer-mode monitor computes its old listen port inside, as `parse_listen_port(listen_port)?.checked_add(1000)`, with an error on `None`.
    - Test next: add the ignored `monitor_sees_third_party_samples` to the `connect.rs` tests and run `cargo test -- --ignored monitor_sees_third_party_samples`. Expected: FAIL, `t/m` not received, because the peer-mode monitor has no endpoint.

```rust
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn monitor_sees_third_party_samples() {
        use std::time::Duration;
        // As the form opens them: peer mode, empty address (multicast on), Listen Port 27802.
        let s = connect_zenoh("", "27802", "peer", "{}").await.expect("publishing session");
        let m = connect_zenoh_monitor("", "27802", "peer").await.expect("monitor session");
        let sub = m.declare_subscriber("**").await.expect("monitor subscriber");
        // A third peer that only dials this app's listener, as another app would.
        let mut c = zenoh::Config::default();
        for (k, v) in [
            ("mode", r#""peer""#),
            ("listen/endpoints", "[]"),
            ("scouting/multicast/enabled", "false"),
            ("connect/endpoints", r#"["tcp/[::1]:27802"]"#),
        ] {
            c.insert_json5(k, v).expect(k);
        }
        let third = zenoh::open(c).await.expect("third session");
        // Declarations propagate asynchronously, so put again until the monitor sees one.
        let received = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                third.put("t/m", "x").await.expect("put");
                let next = tokio::time::timeout(Duration::from_millis(250), sub.recv_async()).await;
                if let Ok(Ok(sample)) = next {
                    if sample.key_expr().as_str() == "t/m" {
                        break;
                    }
                }
            }
        })
        .await
        .is_ok();
        let monitor_is_peer = s.info().peers_zid().await.any(|z| z == m.zid());
        let _ = third.close().await;
        let _ = m.close().await;
        let _ = s.close().await;
        assert!(received, "the monitor's ** subscriber did not get the third peer's t/m within 5 s");
        assert!(!monitor_is_peer, "the monitor must not count as a peer (T22's peer count)");
    }
```

    - Then add this unit test and run `cargo test monitor_endpoints`. Expected: a compile error, because `monitor_endpoints` does not exist yet.

```rust
    #[test]
    fn monitor_endpoints_follow_publishing_mode() {
        assert_eq!(
            monitor_endpoints("", "7447", "peer").unwrap(),
            ["tcp/[::1]:7447", "tcp/127.0.0.1:7447"]
        );
        // Peer mode with an address still dials this app's own listener, on the listener's transport.
        assert_eq!(
            monitor_endpoints("udp/10.0.0.5:7447", "7450", "peer").unwrap(),
            ["udp/[::1]:7450", "udp/127.0.0.1:7450"]
        );
        assert_eq!(
            monitor_endpoints("tcp/r1:7447, tcp/r2:7447", "", "client").unwrap(),
            ["tcp/r1:7447", "tcp/r2:7447"]
        );
        assert!(monitor_endpoints("", "7447", "client").is_err());
        assert!(monitor_endpoints("", "0", "peer").is_err());
    }
```

    - Fix: add the two helpers below. Use `listen_protocol` in `connect_zenoh` too, in place of its inline `first_locator`/`protocol` lines. Rewrite the monitor's mode branch as the monitor config above, with `connect/endpoints` set from `monitor_endpoints(locators, listen_port, mode)?`. Delete the interim `+ 1000` port and the monitor's listen endpoint. Both new tests then pass.

```rust
/// The transport the publishing session listens on: the first locator's, or tcp.
fn listen_protocol(locators: &str) -> &str {
    match locators.split(',').next().unwrap_or("").trim() {
        "" => "tcp",
        first => first.split('/').next().unwrap_or("tcp"),
    }
}

/// Endpoints for the client-mode monitor. A peer-mode publishing session is dialled
/// on its own `[::]` listener through both loopbacks, IPv6 first. A client-mode one
/// shares its routers.
pub(crate) fn monitor_endpoints(locators: &str, listen_port: &str, mode: &str) -> Result<Vec<String>, String> {
    if mode == "client" {
        let routers: Vec<String> = locators
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(String::from)
            .collect();
        if routers.is_empty() {
            return Err("client mode needs a router address".into());
        }
        return Ok(routers);
    }
    let port = parse_listen_port(listen_port)?;
    let proto = listen_protocol(locators);
    Ok(vec![format!("{proto}/[::1]:{port}"), format!("{proto}/127.0.0.1:{port}")])
}
```

    - **If `monitor_sees_third_party_samples` still fails with the client-mode monitor, stop and report. Do not work around it.**
      - First find out which cause it is. Add one put from the publishing session itself (`s.put("t/own", "x")`) to a scratch copy of the test and run it again.
      - If the monitor gets `t/own` but not `t/m`, the link is up and forwarding is missing.
      - If it gets neither, the monitor never connected. Record which of `[::1]` and `127.0.0.1` was tried and the `open` error.
      - Report both results, the zenoh version from `Cargo.lock` and the OS.
      - Do not put the monitor back in peer mode. Do not turn on gossip, multicast or `routing/peer/mode = "linkstate"` for either session, because those change how this app joins the user's network.
      - Choosing between another route and a permanent, honest "monitor off" state (programme Q7) is the user's decision.
- [ ] **Step 4: Verify.** Run `cargo test && cargo test -- --ignored peer_mode_unreachable_endpoint_fails monitor_sees_third_party_samples && cargo clippy --all-targets -- -D warnings && grep -n 'set_max_message_size\|\.unwrap()' src/worker/connect.rs; grep -rn 'monitor_port' src/worker`. Expected: the tests pass, the first grep matches only inside `#[cfg(test)]`, and the second prints nothing.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/connect.rs src/worker/session.rs
git commit -m "fix(security): default max_message_size, checked ports, no config panics"
```

---

### Task T11: Bounded pipeline (Lane C)

**Owns:** `src/worker/pipeline.rs`, `src/app/mod.rs`, `src/events/mod.rs`

**Interfaces:**
- `EventTx` becomes `SyncSender<ZenohEvent>`.
- `event_channel(cap)` returns `sync_channel(cap)`.
- New consts `WORKER_EVENT_CAPACITY = 10_000` and `UI_EVENT_CAPACITY = 256`.
- `send_sample` uses `try_send` and counts `Full`.
- New app field `events_pending: bool`.

- [ ] **Step 1: Write the failing tests** in `pipeline.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::*;

    fn msg(i: usize) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            format!("k/{i}"), "p".into(), vec![], "text/plain".into(), chrono::Utc::now(),
            MessageType::Subscribe, false, MessageSource::MonitorSession,
        )
    }

    #[test]
    fn buffer_thread_batches_and_preserves_all_messages() {
        let (tx, rx) = event_channel(1000);
        let (utx, urx) = event_channel(1000);
        let h = std::thread::spawn(move || message_buffer_thread(rx, utx, || {}));
        for i in 0..120 {
            tx.send(ZenohEvent::MessageReceived(msg(i))).unwrap();
        }
        drop(tx);
        h.join().unwrap();
        let mut total = 0;
        for ev in urx.try_iter() {
            if let ZenohEvent::MessageBatch(b) = ev {
                assert!(b.len() <= 50);
                total += b.len();
            }
        }
        assert_eq!(total, 120);
    }

    #[test]
    fn send_sample_counts_drops_when_full() {
        let (tx, _rx) = event_channel(1);
        let drops = AtomicUsize::new(0);
        send_sample(&tx, &drops, msg(0));
        send_sample(&tx, &drops, msg(1));
        assert_eq!(drops.load(Ordering::Relaxed), 1);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- buffer_thread send_sample`. Expected: `send_sample_counts_drops_when_full` fails, because the unbounded channel never drops.
- [ ] **Step 3: Implement.** Replace the contract stubs and the buffer thread:

```rust
pub const WORKER_EVENT_CAPACITY: usize = 10_000;
pub const UI_EVENT_CAPACITY: usize = 256;
const BATCH_WINDOW: std::time::Duration = std::time::Duration::from_millis(16);
const MAX_BATCH: usize = 50;

pub(crate) type EventTx = std::sync::mpsc::SyncSender<ZenohEvent>;

pub(crate) fn event_channel(capacity: usize) -> (EventTx, std::sync::mpsc::Receiver<ZenohEvent>) {
    std::sync::mpsc::sync_channel(capacity)
}

/// Non-blocking send for data samples: a full pipeline drops and counts.
pub(crate) fn send_sample(tx: &EventTx, drops: &AtomicUsize, msg: ZenohMessage) {
    if let Err(std::sync::mpsc::TrySendError::Full(_)) = tx.try_send(ZenohEvent::MessageReceived(msg)) {
        drops.fetch_add(1, Ordering::Relaxed);
    }
}

pub fn message_buffer_thread(
    rx: std::sync::mpsc::Receiver<ZenohEvent>,
    ui: EventTx,
    notify: impl Fn() + Send + 'static,
) {
    let mut batch: Vec<ZenohMessage> = Vec::with_capacity(MAX_BATCH);
    let flush = |batch: &mut Vec<ZenohMessage>| -> bool {
        if batch.is_empty() {
            return true;
        }
        let ok = ui.send(ZenohEvent::MessageBatch(std::mem::take(batch))).is_ok();
        notify();
        ok
    };
    // Block for the first event: no wake-ups while idle.
    while let Ok(first) = rx.recv() {
        let deadline = std::time::Instant::now() + BATCH_WINDOW;
        let mut next = Some(first);
        loop {
            match next.take() {
                Some(ZenohEvent::MessageReceived(m)) => {
                    batch.push(m);
                    if batch.len() >= MAX_BATCH {
                        break;
                    }
                }
                Some(other) => {
                    if !flush(&mut batch) || ui.send(other).is_err() {
                        return;
                    }
                    notify();
                }
                None => {}
            }
            let now = std::time::Instant::now();
            if now >= deadline {
                break;
            }
            match rx.recv_timeout(deadline - now) {
                Ok(e) => next = Some(e),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => break,
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    flush(&mut batch);
                    return;
                }
            }
        }
        if !flush(&mut batch) {
            return;
        }
    }
    flush(&mut batch);
}
```

  - The borrow checker rejects a closure that captures `ui` immutably while `ui.send(other)` is also called. If so, make `flush` a nested `fn flush(ui: &EventTx, notify: &impl Fn(), batch: &mut Vec<ZenohMessage>) -> bool` and call it with explicit arguments.
  - In `app/mod.rs`:
    - Use `event_channel(worker::pipeline::WORKER_EVENT_CAPACITY)` for worker→buffer and `event_channel(worker::pipeline::UI_EVENT_CAPACITY)` for buffer→UI.
    - Add `events_pending: false`.
    - In `test_app`, keep `std::sync::mpsc::channel()`. `event_receiver` is a `Receiver` either way.
  - In `events/mod.rs`, replace the drain loop with:

```rust
            let budget = Instant::now() + Duration::from_millis(8);
            self.events_pending = false;
            while let Ok(event) = receiver.try_recv() {
                events.push(event);
                if Instant::now() >= budget {
                    self.events_pending = true;
                    break;
                }
            }
```

- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/pipeline.rs src/app/mod.rs src/events/mod.rs
git commit -m "fix(pipeline): bounded channels, blocking buffer thread, per-frame event budget"
```

---

### Task T12: Connection lifecycle (Lane C)

**Owns:** `src/worker/state.rs`, `src/worker/session.rs`, `src/worker/mod.rs`, `src/app/layout.rs`, `src/events/mod.rs`

**Interfaces:** `WorkerState` gains `discovery_task: Option<tokio::task::JoinHandle<()>>` and `pub(crate) async fn teardown(&mut self)`. Step 3b then adds `resubscribe: Vec<(String, String)>` and changes the signature to `teardown(&mut self, keep_subscriptions: bool)` (`true` from `handle_connect`/`handle_disconnect`, `false` on final shutdown). The app gains `connect_hint` and `locator_preview` in `app/layout.rs`.

- [ ] **Step 1: Write the failing test** in `session.rs`:

```rust
#[cfg(test)]
mod tests {
    use crate::types::*;
    use crate::worker::{pipeline, zenoh_worker};
    use std::sync::{atomic::AtomicUsize, Arc, RwLock};
    use std::time::{Duration, Instant};

    #[test]
    #[ignore = "opens network sessions"]
    fn reconnect_then_disconnect_leaves_no_discovery_updates() {
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        let (ev_tx, ev_rx) = pipeline::event_channel(pipeline::WORKER_EVENT_CAPACITY);
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let worker = std::thread::spawn(move || {
            tokio::runtime::Runtime::new().unwrap().block_on(zenoh_worker(cmd_rx, ev_tx, store, drops))
        });
        let connect = || ZenohCommand::Connect {
            locators: String::new(),
            listen_port: "27601".into(),
            mode: "peer".into(),
            config_json: "{}".into(),
        };
        let wait_for = |pred: &dyn Fn(&ZenohEvent) -> bool, secs: u64| {
            let end = Instant::now() + Duration::from_secs(secs);
            while Instant::now() < end {
                if let Ok(e) = ev_rx.recv_timeout(Duration::from_millis(200)) {
                    if pred(&e) {
                        return true;
                    }
                }
            }
            false
        };
        cmd_tx.send(connect()).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx.send(connect()).unwrap(); // reconnect while connected must not leak
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx.send(ZenohCommand::Disconnect).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::Disconnected), 30));
        // Discovery polls every 2 s; 5 s of silence proves every poller stopped.
        assert!(!wait_for(&|e| matches!(e, ZenohEvent::DiscoveryUpdate { .. }), 5));
        drop(cmd_tx);
        worker.join().unwrap();
    }
}
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test -- --ignored reconnect_then_disconnect`. Expected: FAIL, because the old discovery thread keeps sending updates.
- [ ] **Step 3: Implement.** In `state.rs`:

```rust
impl WorkerState {
    /// Stop every task and close both sessions. Idempotent.
    pub(crate) async fn teardown(&mut self) {
        if let Some(t) = self.discovery_task.take() {
            t.abort();
        }
        if let Some((h, tx)) = self.queryable_task.take() {
            let _ = tx.try_send(());
            h.abort();
        }
        if let Some(sub) = self.monitor_subscription.take() {
            let _ = sub.cancel_sender.send(());
            sub.task_handle.abort();
        }
        for (_, sub) in self.active_subscriptions.drain() {
            let _ = sub.cancel_sender.send(());
            sub.task_handle.abort();
        }
        for s in [self.monitor_session.take(), self.publishing_session.take()].into_iter().flatten() {
            if let Err(e) = s.close().await {
                tracing::error!("Session close failed: {}", e);
            }
        }
    }
}
```

  - In `session.rs`:
    - `handle_disconnect` becomes `st.teardown().await; let _ = ctx.event_sender.send(ZenohEvent::Disconnected);`.
    - The first line of `handle_connect` becomes `st.teardown().await;`.
    - Replace the `std::thread::spawn` discovery block with:

```rust
let discovery_session = session_arc.clone();
let discovery_sender = ctx.event_sender.clone();
st.discovery_task = Some(tokio::spawn(async move {
    loop {
        let peers = discovery_session.info().peers_zid().await.count();
        let routers = discovery_session.info().routers_zid().await.count();
        if discovery_sender.send(ZenohEvent::DiscoveryUpdate { peers, routers }).is_err() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}));
```

  - `worker/mod.rs`: the `Disconnect` arm calls `session::handle_disconnect`, and when the command channel disconnects, run `st.teardown().await` before `break`.
  - `events/mod.rs`: the `Disconnected` arm also sets `self.queryable_enabled = false;`.
  - `app/layout.rs`:
    - In the Connect handler, set `ConnectionStatus::ConnectingPublishing` only inside `Ok(_)`. On `Err(e)`, set `ConnectionStatus::Error(format!("worker not running: {e}"))`.
    - Disconnect button: `ui.add_enabled(matches!(self.connection_status, ConnectionStatus::Connected), egui::Button::new("Disconnect"))`.
- [ ] **Step 3b: UI-review lifecycle fixes (F-T8-1, F-T17-1, F-T17-4, F-T17-5, F-T17-7, F-T17-10).** Write these tests first.
  - In `events/mod.rs` tests:

```rust
    #[test]
    fn stale_disconnected_does_not_cancel_new_connect() {
        let (mut app, tx) = ZenohExplorer::test_app();
        // The worker handles commands in order and Disconnect is disabled while
        // connecting, so a Disconnected seen while connecting belongs to an
        // earlier Disconnect.
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        app.queryable_enabled = true;
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert!(matches!(app.connection_status, ConnectionStatus::ConnectingPublishing));
        assert!(!app.queryable_enabled, "the worker's teardown killed the queryable");
        // In one batch, a stale Disconnected must not end the batch early.
        tx.send(ZenohEvent::Disconnected).unwrap();
        tx.send(ZenohEvent::PublishingConnected).unwrap();
        app.process_events();
        assert!(matches!(app.connection_status, ConnectionStatus::ConnectingMonitor));
    }
```

  - In `session.rs` tests, the ignored `reconnect_restores_subscriptions`: connect (listen port 27602), send `Subscribe { key_expr: "t/a/**", .. }` and `Subscribe { key_expr: "t/b/**", .. }` and wait for both `SubscriptionCreated`, send `Disconnect` and wait for `Disconnected`. While disconnected, send `Unsubscribe` with the `t/b/**` id; a `SubscriptionRemoved` for that id must arrive within 5 s (the UI removes a row only on that event). Then send `Connect`; a `SubscriptionCreated` for `t/a/**` must arrive after the second `MonitorConnected`, and none for `t/b/**`.
  - In `app/layout.rs` tests (add a `#[cfg(test)] mod tests`):

```rust
    #[test]
    fn connection_hints_match_the_form() {
        assert!(connect_hint("client", "").contains("enter the router's address"));
        assert!(!connect_hint("client", "").contains("Default: tcp/localhost:7447"));
        assert!(connect_hint("peer", "").contains("different Listen Port for each copy"));
        assert!(!connect_hint("peer", "").contains("+ 1000"), "T10's client-mode monitor opens no second port");
        assert_eq!(locator_preview("client", "tcp", "", "7447"), "needs an address");
        assert_eq!(locator_preview("peer", "tcp", "", "7447"), "multicast discovery");
    }
```

  - Run `cargo test -- stale_disconnected_does_not_cancel_new_connect connection_hints_match_the_form; cargo test -- --ignored reconnect_restores_subscriptions`. Expected: FAIL (compile error for `connect_hint`; the reconnect test times out).
  - Implement:
    - **Stale `Disconnected` (F-T8-1).** In the `Disconnected` arm, guard only the status and peer reset: `if !matches!(self.connection_status, ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor) { self.connection_status = ConnectionStatus::Disconnected; self.discovered_peers = 0; self.discovered_routers = 0; }`. Keep `self.queryable_enabled = false;` (Step 3) unconditional, because the worker's teardown killed the queryable either way; T21's query cancel in this arm is unconditional too. Do **not** `return` from the arm: it sits inside `for event in events`, so a `return` would drop the rest of the frame's batch (for example the new connect's `PublishingConnected`) and skip the health check at the end of `process_events`.
    - **Subscriptions survive Disconnect (F-T17-7).** Worker: add `resubscribe: Vec<(String, String)>` (id, key) to `WorkerState`. `teardown()` pushes `(id, key_expr)` for every drained `active_subscriptions` entry, except when called for the final shutdown (pass `keep_subscriptions: bool`). At the end of a successful `handle_connect`, call `subscribe::handle_subscribe(st, ctx, key, …)` for each taken entry; it emits `SubscriptionCreated` with a new id. In `worker/mod.rs`, the `Unsubscribe` arm first removes the id from `st.resubscribe`, and when an entry was removed it sends `ZenohEvent::SubscriptionRemoved { id: subscription_id.clone() }`: while disconnected `active_subscriptions` is empty, so `handle_unsubscribe` emits nothing, and the UI removes a row only on `SubscriptionRemoved`. UI: the Disconnect handler and the `Disconnected` arm no longer clear `self.subscriptions`; the `SubscriptionCreated` arm replaces the id of an existing entry with the same `key_expr` instead of pushing a duplicate. While disconnected, the connection panel shows `"{n} subscriptions resume when you reconnect"` when `n > 0`.
    - **Ports (F-T17-1).** Before sending `Connect`, check `validation::port_error(&self.connect_port, 1..=65535)` when an address is set, and `validation::port_error(&self.listen_port, 1024..=65535)` in peer mode (after T10 the monitor opens no port of its own, so there is no Listen Port + 1000 ceiling). Show each error next to its field and disable Connect while either is `Some`.
    - **Stale error (F-T17-4).** Remember the `(mode, locators, listen_port)` of the last attempt in a local `egui::Id` temp value; when the form's current values differ and the status is `Error(_)`, set it back to `Disconnected`.
    - **Hints (F-T17-5).** Add `fn connect_hint(mode: &str, address: &str) -> &'static str` and `fn locator_preview(mode, transport, address, port) -> String` and use them for the hint label and the `→` preview. Wording: client, `"Client mode: enter the router's address (for example localhost) and its port (7447)."`; peer, `"Peer mode: finds peers by multicast. Listen Port is where other peers reach this app. Use a different Listen Port for each copy on one machine. Address is optional."`.
    - **Connecting target and time (F-T17-10).** In the Connect handler set `self.connect_started = Some(Instant::now())` and `self.connect_target = locators.clone()` (or `"multicast discovery"`). While connecting, the header status reads `format!("Connecting to {} … {} s", self.connect_target, elapsed)`. A Cancel control is **not** part of this task: the worker awaits the whole connect inside its command loop, so it cannot receive a cancel until the connect ends (F-T8-7's structural fix).
- [ ] **Step 4: Verify.** Run `cargo test && cargo test -- --ignored reconnect_then_disconnect reconnect_restores_subscriptions && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/state.rs src/worker/session.rs src/worker/mod.rs src/app/layout.rs src/events/mod.rs
git commit -m "fix(worker): single teardown path; abortable discovery; clean reconnect"
```

---

### Task T13: Repaint on events (Lane C)

**Owns:** `src/app/mod.rs`, `src/app/layout.rs`, `src/main.rs`

**Interfaces:** `ZenohExplorer::new(ctx: egui::Context) -> Self`. `impl Default` is removed.

- [ ] **Step 1: Change the constructor.** Make it `pub fn new(ctx: egui::Context) -> Self` and give the buffer thread `move || repaint_ctx.request_repaint()`, where `let repaint_ctx = ctx.clone();`.
  - Delete `impl Default for ZenohExplorer`.
  - `test_app()` calls `Self::new(egui::Context::default())`.
- [ ] **Step 2: Update `main.rs`.** Use `Box::new(|cc| Ok(Box::new(ZenohExplorer::new(cc.egui_ctx.clone()))))`.
- [ ] **Step 3: Replace the repaint timers** in `app/layout.rs`:
  - After `self.process_events();`, add `if self.events_pending { ctx.request_repaint(); }`.
  - Change the final `request_repaint_after(66ms)` to `ctx.request_repaint_after(std::time::Duration::from_secs(1));`.
  - While the status is `ConnectingPublishing | ConnectingMonitor`, add `ctx.request_repaint_after(std::time::Duration::from_millis(100));`.
  - Delete the `request_repaint_after(66ms)` that T2 moved from old `app.rs:184` to just after `self.apply_theme(ctx);`.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -rn 'from_millis(66)' src/app`. Expected: only the unhealthy-worker pulse in `layout.rs`.
- [ ] **Step 5: Measure idle CPU.** Run `cargo run --release`, connect in peer mode with no traffic, wait 20 s, then run `ps -o %cpu= -p $(pgrep -n zenoh-explorer)` three times, 5 s apart. Expected: every reading < 3.0. Then publish on `demo/test` from the Publish tab and confirm the tree updates without moving the mouse.
- [ ] **Step 6: Commit** with the three CPU readings in the body.

```bash
git add src/app/mod.rs src/app/layout.rs src/main.rs
git commit -m "perf(ui): repaint on worker events instead of a 66ms loop"
```

---

### Task T14: Validate user input (Lane D)

**Owns:** `src/validation.rs`, `src/ui/publish.rs`, `src/ui/query.rs`, `src/ui/topic_tree.rs`

**Interfaces:** `pub fn key_expr_error(s: &str) -> Option<String>`, `pub fn selector_error(s: &str) -> Option<String>`, and `pub fn timeout_error(s: &str) -> Option<String>`.

- [ ] **Step 1: Write the failing tests** in `validation.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_key_exprs() {
        for bad in ["", "  ", "demo/", "/demo", "a//b", "a/**/**", "a#b", "a?b"] {
            assert!(key_expr_error(bad).is_some(), "accepted {bad:?}");
        }
    }

    #[test]
    fn accepts_valid_key_exprs() {
        for ok in ["demo/**", "demo/*/x", "a/b$*", "@/router/x"] {
            assert_eq!(key_expr_error(ok), None, "rejected {ok:?}");
        }
    }

    #[test]
    fn selectors_allow_parameters() {
        assert_eq!(selector_error("demo/*/x?y=1;_time=[now(-1h)..]"), None);
        assert!(selector_error("demo/?y=1").is_some());
    }

    #[test]
    fn timeout_bounds() {
        assert_eq!(timeout_error("10000"), None);
        assert!(timeout_error("5s").is_some());
        assert!(timeout_error("0").is_some());
        assert!(timeout_error("700000").is_some());
    }
}
```

  - Also add (F-T15-1, F-T16-3, F-T17-1):

```rust
    #[test]
    fn error_text_has_no_source_path() {
        let e = key_expr_error("demo//x").unwrap();
        assert!(!e.contains(".rs:"), "{e}");
        assert!(key_expr_error("$*").unwrap().contains("$*"), "the lone-$* rule is named");
    }

    #[test]
    fn surrounding_space_is_rejected() {
        assert!(key_expr_error(" demo/**").is_some());
        assert!(selector_error("demo/** ").is_some());
    }

    #[test]
    fn port_bounds() {
        assert_eq!(port_error("7447", 1..=65535), None);
        for bad in ["", "abc", "99999", "0"] {
            assert!(port_error(bad, 1..=65535).is_some(), "{bad}");
        }
        assert!(port_error("80", 1024..=65535).is_some());
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test validation::`. Expected: FAIL.
- [ ] **Step 3: Implement.**

```rust
/// None if `s` is a valid canonical key expression, else a short reason.
pub fn key_expr_error(s: &str) -> Option<String> {
    if s.trim().is_empty() {
        return Some("Key expression is empty".into());
    }
    zenoh::key_expr::KeyExpr::try_from(s).err().map(|e| e.to_string())
}

/// None if `s` is a valid selector (`key_expr[?params]`), else a short reason.
pub fn selector_error(s: &str) -> Option<String> {
    let key = s.split_once('?').map_or(s, |(k, _)| k);
    key_expr_error(key).or_else(|| zenoh::query::Selector::try_from(s).err().map(|e| e.to_string()))
}

/// None if `s` is a whole number of milliseconds in 100..=600_000.
pub fn timeout_error(s: &str) -> Option<String> {
    match s.trim().parse::<u64>() {
        Ok(v) if (100..=600_000).contains(&v) => None,
        _ => Some("Timeout must be 100–600000 ms".into()),
    }
}
```

  - Refine `key_expr_error` (F-T15-1, F-T16-3): return `Some("Key has a leading or trailing space")` when `s != s.trim()`; strip zenoh's trailing ` at <path>:<line>.` from the error text (cut at the last `" at "` when the rest contains `.rs:`); and replace zenoh's misleading text for a lone `$*` chunk with ``"`$*` must be joined to other text in its level, e.g. `v$*`"``.
  - Add `pub fn wildcard_note(s: &str) -> Option<&'static str>`, returning `Some("Wildcard key: every matching subscriber receives this")` when a valid key contains `*`. Show it under the Publish key in neutral text.
  - Add `pub fn port_error(s: &str, range: std::ops::RangeInclusive<u16>) -> Option<String>`: `None` when `s.trim()` parses as a `u16` inside `range`, else `Some(format!("Port must be {}–{}", range.start(), range.end()))`. T12 uses it.
- [ ] **Step 4: Wire the checks into the UI.** Use this pattern for the Publish key (`publish.rs`), the Subscribe key (`topic_tree.rs` subscribe section), the Query selector and timeout (`query.rs`), and the Queryable pattern (`publish.rs`):

```rust
let key_err = crate::validation::key_expr_error(&self.publish_key);
if let Some(err) = &key_err {
    ui.colored_label(ExplorerColors::ERROR, err);
}
// … and in the button's enabled condition:
matches!(self.connection_status, ConnectionStatus::Connected) && key_err.is_none()
```

  - Query: the button is enabled only when `selector_error` and `timeout_error` are both `None`. Parse the timeout with `.parse().expect("validated")` inside the click handler.
  - Queryable: `ui.add_enabled(pattern_err.is_none() && connected, egui::Checkbox::new(&mut self.queryable_enabled, "Enable Queryable"))`.
  - Subscribe: also disable when `self.subscriptions.iter().any(|s| s.key_expr == self.subscribe_key.trim())`.
  - Publish keeps typed text. Replace the unconditional clear (old `publish.rs:248-252`) with:

```rust
                    // Imported bytes were moved into the command; typed text stays for re-send.
                    if from_import {
                        self.publish_payload_filename = None;
                        self.publish_payload = String::new();
                        self.publish_payload_expanded = false;
                        self.import_memory_bytes = 0;
                    }
```

- [ ] **Step 5: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`. Then run the manual check: type `demo/` as the publish key and confirm the button is disabled with an error shown.
- [ ] **Step 6: Commit.**

```bash
git add src/validation.rs src/ui/publish.rs src/ui/query.rs src/ui/topic_tree.rs
git commit -m "fix(ui): validate key expressions, selectors and timeout before sending"
```

---

### Task T15: Show kind and source time (Lane D)

**Owns:** `src/ui/topic_tree.rs`

- [ ] **Step 1: Extend the extracted tuple.** In `show_topic_details` (old 396-412), add `node.last_kind` and `node.last_source_time`, defaulting to `(SampleKindView::Put, None)`.
- [ ] **Step 2: Add the rows** after the Encoding row (old 520-528):

```rust
if kind == SampleKindView::Delete {
    ui.separator();
    ui.label(RichText::new("Last sample: DELETE").strong());
}
if let Some(ts) = source_time {
    ui.separator();
    ui.horizontal(|ui| {
        ui.label(RichText::new("Source time:").strong());
        ui.label(ts.to_rfc3339_opts(chrono::SecondsFormat::Millis, true));
    });
}
```

- [ ] **Step 3: Verify.** Run `cargo build && cargo test && cargo clippy --all-targets -- -D warnings`. Then check manually: from a second terminal, `z_put`/`z_delete` (zenoh examples) or a second explorer publishes and deletes `demo/x`, and the details panel shows DELETE.
- [ ] **Step 4: Commit.**

```bash
git add src/ui/topic_tree.rs
git commit -m "feat(ui): show last sample kind and source timestamp in topic details"
```

---

### Task T16: Cross-source dedup (Lane D)

**Owns:** `src/types/limits.rs`, `src/events/ingest.rs`

**Interfaces:** `Deduper::is_cross_source_duplicate(&mut self, hash: u64, source: &MessageSource) -> bool` and `Deduper::record(&mut self, hash: u64, source: MessageSource)`. `DEDUP_WINDOW` becomes 250 ms.

- [ ] **Step 1: Replace the dedup tests** in `limits.rs`:
  - Keep `dedup_differs_when_middle_bytes_differ`.
  - Delete `dedup_same_content_within_window` and `dedup_unrecorded_hash_not_seen`.
  - Add:

```rust
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
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- dedup same_source cross_source`. Expected: FAIL.
- [ ] **Step 3: Implement.** The map becomes `HashMap<u64, (Instant, MessageSource)>`:

```rust
    /// True if the same (key, payload) was recorded within the TTL by a
    /// *different* source — the same sample observed by two sessions. Repeats
    /// from one source are real traffic and are never suppressed.
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
```

  - Delete `seen_recently`. Set `pub const DEDUP_WINDOW: Duration = Duration::from_millis(250);` and update the `Deduper` doc comment.
  - In `ingest.rs`, call `is_cross_source_duplicate(h, &message.source)` and `record(h, message.source.clone())`.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/types/limits.rs src/events/ingest.rs
git commit -m "fix(dedup): only collapse the same sample seen by two sessions"
```

---

### Task T19: Tree per-frame cost (Lane D)

**Owns:** `src/ui/topic_tree.rs`, `src/types/tree.rs`

**Interfaces:** `pub fn filter_cache_is_stale(cached: Option<(&str, u64, Instant)>, filter: &str, version: u64, now: Instant) -> bool` and `pub const FILTER_RECOMPUTE_INTERVAL: Duration` (250 ms).

- [ ] **Step 1: Write the failing test** in `tree.rs` tests:

```rust
    #[test]
    fn filter_cache_staleness_rules() {
        let t0 = Instant::now();
        assert!(filter_cache_is_stale(None, "a", 1, t0));
        assert!(filter_cache_is_stale(Some(("a", 1, t0)), "b", 1, t0));
        assert!(!filter_cache_is_stale(Some(("a", 1, t0)), "a", 2, t0 + Duration::from_millis(100)));
        assert!(filter_cache_is_stale(Some(("a", 1, t0)), "a", 2, t0 + Duration::from_millis(300)));
        assert!(!filter_cache_is_stale(Some(("a", 2, t0)), "a", 2, t0 + Duration::from_secs(9)));
    }
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test filter_cache_staleness_rules`. Expected: FAIL.
- [ ] **Step 3: Implement** in `tree.rs`:

```rust
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
```

- [ ] **Step 4: Render without cloning** in `show_tree_panel`. Replace the `tree_clone` block:

```rust
            // Render from a read guard on a local Arc clone: no per-frame deep copy,
            // and `self` stays free for &mut calls. The UI thread is the only
            // writer (events run on it), so nested read() calls cannot deadlock.
            let tree_arc = self.browse_tree.clone();
            let guard = tree_arc.read();
            let fallback;
            let tree: &ZenohNode = match &guard {
                Ok(g) => g,
                Err(_) => {
                    fallback = ZenohNode::new("root".to_string());
                    &fallback
                }
            };
```

  - Rename the remaining `tree_clone` uses to `tree`, and replace the staleness check with:

```rust
                let now = Instant::now();
                if filter_cache_is_stale(
                    self.tree_filter_cache.as_ref().map(|(q, v, at, _)| (q.as_str(), *v, *at)),
                    &filter_lower,
                    self.tree_version,
                    now,
                ) {
                    let visible = compute_visible_paths(tree, &filter_lower);
                    self.tree_filter_cache = Some((filter_lower.clone(), self.tree_version, now, visible));
                }
```

- [ ] **Step 5: Bound the history scan** in `show_topic_details` (old 534-541):

```rust
                const HISTORY_SCAN_LIMIT: usize = 20_000;
                let topic_messages: Vec<_> = self
                    .messages
                    .iter()
                    .rev()
                    .take(HISTORY_SCAN_LIMIT)
                    .filter(|m| m.key == *topic)
                    .take(50)
                    .collect();
```

- [ ] **Step 6: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -n 'tree.clone()' src/ui/topic_tree.rs`. Expected: grep prints nothing.
- [ ] **Step 7: Commit.**

```bash
git add src/ui/topic_tree.rs src/types/tree.rs
git commit -m "perf(tree): no per-frame tree clone; throttled filter; bounded history scan"
```

---

### Task T27: Worker, export and view fixes

**Replaces:** T5, T6, T9, T17, T25, T18 and T23. Each old section is kept below as a part. Its heading keeps "(was Tn)", so references from P2-P5, the Review Focus list and the Baseline table still resolve.

**Depends on:** T3, T4, T7, T8, T12, T14, T16 (all complete). T12 is listed because part a edits `session.rs` and `worker/mod.rs`, which T12 last owned.

**Why one task:** the seven old tasks edit disjoint files. Inside the set only T18 waited on T25, and one agent now runs both (part e). One board start, one integration run and one completion replace seven of each.

**Owns** (per part; no file is in two parts):

| Part | Was | Owns | Pre-flight items folded in |
|---|---|---|---|
| a | T5 | `src/worker/subscribe.rs`, `src/worker/query.rs`, `src/worker/session.rs`, `src/worker/mod.rs`, `src/types/message.rs` | X1 (T16 regression), G1-1, G1-5 (K8 a and b), K9 markers in `message.rs` |
| b | T6 | `src/worker/queryable.rs` | G1-2, G1-6 (K10 for its own test) |
| c | T9 | `src/transfer.rs` | G1-3, G1-4, optional dead-check removal |
| d | T17 | `src/events/ingest.rs` | G3-1, G3-2 (K5), G3-3 (K6), G3-4 |
| e | T25, then T18 | `src/ui/messages.rs`, then `src/events/json_cache.rs`, `src/types/mod.rs` | G3-9 (K2), G3-10, G3-11 |
| f | T23 | `src/ui/publish.rs`, `src/ui/query.rs` | G3-5, G1-2 follow-on, G3-7, G3-8 |

The union is 13 files. `src/types/message.rs` belongs to part a only. `session.rs` and `worker/mod.rs` last belonged to T12 (complete).

**Interfaces:**
- Produced for T28 and later plans:
  - Part a: `handle_subscribe(st, ctx, key_expr, id: Option<String>)`; a subscription keeps its id across a reconnect; a failed re-declare sends `SubscriptionRemoved { id }` then `OperationFailed { op: Subscribe }`; `MessageSource::UserSubscription(Arc<str>)`; the query outcome texts `"no answer within {timeout_ms} ms from a matching queryable"` and `"a queryable answered with an error: {text}"`.
  - Part b: `matching_entries(store, scope, query)` and `serve_queryable(...)`.
  - Part c: `MAX_PLAIN_BYTES`, `STALE_TRANSFER_AGE`, `gc_stale_transfers`.
  - Part d: `process_single_message` is `pub(crate)`; `add_message_with_limits(message, display, store)`.
  - Part e: `filtered_tail`, `paused_note`; `MAX_HASH_BYTES` is gone.
  - Part f: `publish_status_line`, `publish_button_label`, `connection_notice` (`pub(crate)`), `queryable_summary`, `served_count`.
- Read across parts. No part may change these during T27:
  - `worker::queryable::{handle_enable, handle_disable}` keep their signatures, because `worker/mod.rs` (part a) calls them.
  - `transfer::{parse_chunk_key, insert_payload, format_size, get_payload_for_export, chunk_progress}` keep their signatures. Part d and part f read them.
  - In `types/message.rs`, part a only adds the `UserSubscription` variant, rewords the `PublishingSession` doc and removes three stale markers. `PublishStatus`, `format_local_time`, `SampleKindView` and the unit variants of `MessageSource` stay as they are. Parts d, e and f use them. The `#[allow(dead_code)]` on `PublishStatus::Sending` and on `format_local_time` stays: their first non-test uses arrive with parts e and f, and T28's closing step removes those markers.
  - `get_cached_json(&mut self, &str) -> Option<String>` keeps its signature (part e owns it; part f's `query.rs` calls it).
- New `OperationFailed` texts from parts a and b are raw `e.to_string()`. Do not copy a source-path strip into the worker. T28 step 1 (was T21) strips zenoh's " at <path>.rs:N." suffix centrally (K7).

**How to run:**
- The board tracks T27 as one task: one start, one completion, one evidence entry.
- After the start, record the base commit. Every part branches from it:

```bash
BASE=$(git rev-parse HEAD)
```

- Run parts a-f as six parallel agents. Each agent works in its own git worktree on branch `t27-<part>` made from `$BASE`. Use the agent tool's worktree isolation, or `git worktree add -b t27-a <dir> "$BASE"`.
- Point every worktree at one target directory, so the dependencies compile once. Cargo's lock then runs the builds one at a time:

```bash
export CARGO_TARGET_DIR=<main checkout>/target
```

- Each agent follows only its part's steps, edits only its part's Owns, runs its part's Verify step in its worktree, and commits only its files on its branch. Part e runs T25's steps first, then T18's.
- Before merging, check each branch's file list against its Owns. Any extra file means the part broke ownership: stop and report.

```bash
for p in a b c d e f; do echo "== $p"; git diff --name-only "$BASE" "t27-$p"; done
```

- Merge in this order: a first, because it changes the shared `MessageSource` type, then b, c, d, e and f. The files are disjoint, so no merge can conflict. A conflict means ownership broke: stop and report.

```bash
for p in a b c d e f; do
  git merge --no-ff "t27-$p" -m "merge(t27): part $p" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
done
```

- Run one integration round on the merged tree and paste every output into the evidence:

```bash
cargo build
cargo test
cargo test -- --ignored invalid_selector_reports_query_failure reconnect_restores_subscriptions
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
grep -rn 'use Export' src                                        # prints nothing
grep -rn 'MAX_HASH_BYTES' src                                    # prints nothing
grep -n 'source:local' src/worker/query.rs src/worker/queryable.rs  # prints nothing
grep -n 'a queryable answered with an error' src/worker/query.rs  # matches
grep -n 'listen/endpoints' src/worker/queryable.rs               # matches
git diff --name-only "$BASE" HEAD                                # only the 13 owned files
```

- Then run one read-only verifier per part, six in parallel. Each verifier reads its part's diff (`git diff "$BASE" t27-<part>`) against its steps, the pre-flight items in the table above and the Done-when items, and reads the named tests' results in the integration output. A verifier does not edit code. A finding goes back to that part's agent, who fixes it on the merged branch inside the same Owns; then rerun the integration round.
- A part that cannot finish blocks T27. Finished parts may still be merged, because each part builds on its own.
- Remove the six worktrees after the merge.
- Manual GUI checks are not part of T27's Done-when. Part f's check goes to the user-run list in T28's closing step (was T20, Step 4).

---

#### T27 part a (was T5): Subscribe and query failures reach the UI

**Owns:** `src/worker/subscribe.rs`, `src/worker/query.rs`, `src/worker/session.rs`, `src/worker/mod.rs`, `src/types/message.rs`

- `session.rs` and `worker/mod.rs` are added so a re-declared subscription keeps its id (G1-5, K8 a and b).
- `message.rs` is added for the per-subscription source (X1). No other T27 part owns it.

**Interfaces:**
- `pub(crate) fn next_subscription_id() -> String` and `pub(crate) fn subscription_source(id: &str) -> MessageSource` in `subscribe.rs`.
- `pub(crate) async fn handle_subscribe(st, ctx, key_expr: String, id: Option<String>)`: `None` for a new subscription, `Some(id)` when a reconnect re-declares a kept one.
- `MessageSource::UserSubscription(std::sync::Arc<str>)`: the samples of one user subscription, tagged with its id. `PublishingSession` now tags only query replies.

- [ ] **Step 1: Write the failing tests.**
  - In `subscribe.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscription_ids_are_unique() {
        let a = super::next_subscription_id();
        let b = super::next_subscription_id();
        assert_ne!(a, b);
    }

    /// X1 (T16 regression): two subscriptions of this session that match one key
    /// both receive each sample. They are two sources, so the list keeps one copy.
    /// A value the publisher repeats still counts every time.
    #[test]
    fn overlapping_subscriptions_list_one_copy() {
        use crate::app::ZenohExplorer;
        let (mut app, tx) = ZenohExplorer::test_app();
        app.deduper.ttl = std::time::Duration::from_secs(60); // do not race the 250 ms window
        let copy = |source: MessageSource| {
            ZenohEvent::MessageReceived(ZenohMessage::new_with_bytes(
                "demo/x".into(), "v".into(), b"v".to_vec(), "text/plain".into(),
                chrono::Utc::now(), MessageType::Subscribe, false, source,
            ))
        };
        for _ in 0..2 {
            // demo/** and demo/x on the publishing session, then the monitor's **
            tx.send(copy(super::subscription_source("sub_1"))).unwrap();
            tx.send(copy(super::subscription_source("sub_2"))).unwrap();
            tx.send(copy(MessageSource::MonitorSession)).unwrap();
        }
        app.process_events();
        assert_eq!(app.messages.len(), 2, "one row per sample, not one per subscription");
        assert_eq!(app.messages_deduped, 4);
        let count = app.browse_tree.read().unwrap().children["demo"].children["x"].message_count;
        assert_eq!(count, 2, "the repeated value counts twice");
    }

    /// G1-5 (K8 b): a kept subscription whose re-declare fails loses its row.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn failed_redeclare_removes_the_row() {
        use std::sync::RwLock;
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let mut st = WorkerState {
            publishing_session: Some(Arc::new(zenoh::open(c).await.unwrap())),
            ..Default::default()
        };
        let (tx, rx) = crate::worker::pipeline::event_channel(64);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        // "demo/" is not a valid key expression, so the declare fails.
        super::handle_subscribe(&mut st, &ctx, "demo/".into(), Some("sub_9".into())).await;
        assert!(matches!(rx.try_recv(), Ok(ZenohEvent::SubscriptionRemoved { id }) if id == "sub_9"));
        assert!(matches!(
            rx.try_recv(),
            Ok(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, .. })
        ));
        assert!(st.active_subscriptions.is_empty());
    }
}
```

  - In `query.rs`:

```rust
#[cfg(test)]
mod tests {
    use crate::types::*;
    use crate::worker::{pipeline, zenoh_worker};
    use std::sync::{atomic::AtomicUsize, Arc, RwLock};
    use std::time::{Duration, Instant};

    #[test]
    #[ignore = "opens network sessions"]
    fn invalid_selector_reports_query_failure() {
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        let (ev_tx, ev_rx) = pipeline::event_channel(10_000);
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        let drops = Arc::new(AtomicUsize::new(0));
        let worker = std::thread::spawn(move || {
            tokio::runtime::Runtime::new().unwrap().block_on(zenoh_worker(cmd_rx, ev_tx, store, drops))
        });
        let wait_for = |pred: &dyn Fn(&ZenohEvent) -> bool, secs: u64| {
            let end = Instant::now() + Duration::from_secs(secs);
            while Instant::now() < end {
                if let Ok(e) = ev_rx.recv_timeout(Duration::from_millis(200)) {
                    if pred(&e) {
                        return true;
                    }
                }
            }
            false
        };
        cmd_tx.send(ZenohCommand::Connect {
            locators: String::new(),
            listen_port: "27501".into(),
            mode: "peer".into(),
            config_json: "{}".into(),
        }).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::MonitorConnected), 60));
        cmd_tx.send(ZenohCommand::Query {
            selector: "demo/".into(), // trailing slash: not a valid key expression
            value: String::new(),
            timeout_ms: 1000,
        }).unwrap();
        assert!(wait_for(&|e| matches!(e, ZenohEvent::OperationFailed { op: FailedOp::Query, .. }), 5));
        drop(cmd_tx);
        worker.join().unwrap();
    }

    /// G1-1: a matching queryable that never answers is a timeout, reported once.
    /// It is not "reply error: Timeout", and no QueryNoResponses follows it.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn silent_queryable_reports_timeout_once() {
        use crate::worker::state::{WorkerCtx, WorkerState};
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let sess = Arc::new(zenoh::open(c).await.unwrap());
        let _silent = sess.declare_queryable("q/silent").await.unwrap(); // matches, never answers
        let mut st = WorkerState {
            publishing_session: Some(sess),
            ..Default::default()
        };
        let (tx, rx) = pipeline::event_channel(64);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        super::handle_query(&mut st, &ctx, "q/silent".into(), String::new(), 300).await;
        match rx.recv_timeout(Duration::from_secs(5)) {
            Ok(ZenohEvent::OperationFailed { op: FailedOp::Query, error }) => {
                assert_eq!(error, "no answer within 300 ms from a matching queryable")
            }
            other => panic!("expected a query timeout, got {other:?}"),
        }
        assert!(
            rx.recv_timeout(Duration::from_secs(1)).is_err(),
            "one outcome only: no QueryNoResponses after the timeout"
        );
    }
}
```

  - In `session.rs`, change the end of T12's `reconnect_restores_subscriptions` (G1-5). The comment `// Every SubscriptionCreated in the next 5 s: exactly one, for t/a/**, with a new id.` ends `with its old id.` instead, and the last assertion becomes:

```rust
        assert_eq!(
            recreated[0].0, a_id,
            "the re-declared subscription keeps its id"
        );
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- subscription_ids_are_unique overlapping_subscriptions failed_redeclare silent_queryable; cargo test -- --ignored invalid_selector_reports_query_failure reconnect_restores_subscriptions`. Expected: FAIL (compile errors first).
- [ ] **Step 3: Implement.**
  - In `subscribe.rs`:

```rust
static NEXT_SUB_ID: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

/// Process-unique subscription id (the old `sub_{ms}_{len}` could collide after an unsubscribe).
pub(crate) fn next_subscription_id() -> String {
    format!("sub_{}", NEXT_SUB_ID.fetch_add(1, std::sync::atomic::Ordering::Relaxed))
}
```

    - Use it in `handle_subscribe` as `id.unwrap_or_else(next_subscription_id)`. Step 3c adds the `id` parameter.
    - In the `declare_subscriber` error arm, add `let _ = ctx.event_sender.send(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, error: e.to_string() });`. For a kept id, Step 3c sends `SubscriptionRemoved` before it.
  - In `query.rs`:
    - In the `get(...).await` error arm, send `OperationFailed { op: FailedOp::Query, error: e.to_string() }`.
    - In the reply loop's `Err(e)` arm, set `received_replies = true`, so `QueryNoResponses` is not also sent. Let `text = crate::payload::preview(&e.payload().to_bytes(), 1024)`. (G1-1)
    - If `text == "Timeout"`, this is a timeout. Either this session raised it (`replier_id` is `None`) or a router on the path did (`replier_id` is that router), so do not decide by `replier_id`. For a timeout: if no reply arrived before it, send `OperationFailed { op: FailedOp::Query, error: format!("no answer within {timeout_ms} ms from a matching queryable") }`; if replies already arrived, only `debug!` it. Report a timeout at most once per query.
    - Any other `Err` is a queryable's error reply. Send `error: format!("a queryable answered with an error: {text}")`.
    - `timeout_ms` is a `u64`, and the `async move` task captures it.
    - The reply task then reads:

```rust
                let own_zid = sess.zid();
                tokio::spawn(async move {
                    let mut received_replies = false; // any reply, sample or error
                    let mut samples = 0usize;
                    let mut timeout_reported = false;
                    while let Ok(reply) = replies.recv_async().await {
                        received_replies = true;
                        // Local means this session answered, whatever the reply carries.
                        let is_local = reply.replier_id().is_some_and(|g| g.zid() == own_zid);
                        match reply.result() {
                            Ok(sample) => {
                                samples += 1;
                                let message = super::samples::message_from_sample(
                                    sample,
                                    MessageType::QueryReply,
                                    is_local,
                                    MessageSource::PublishingSession,
                                );
                                send_sample(&event_sender_query, &drops, message);
                            }
                            Err(e) => {
                                let text = crate::payload::preview(&e.payload().to_bytes(), 1024);
                                if text == "Timeout" {
                                    // Raised by this session or a router, never by a queryable.
                                    if samples == 0 && !timeout_reported {
                                        timeout_reported = true;
                                        let _ = event_sender_query.send(ZenohEvent::OperationFailed {
                                            op: FailedOp::Query,
                                            error: format!(
                                                "no answer within {timeout_ms} ms from a matching queryable"
                                            ),
                                        });
                                    } else {
                                        debug!("Query timed out after {} replies", samples);
                                    }
                                } else {
                                    let _ = event_sender_query.send(ZenohEvent::OperationFailed {
                                        op: FailedOp::Query,
                                        error: format!("a queryable answered with an error: {text}"),
                                    });
                                }
                            }
                        }
                    }
                    if !received_replies {
                        let _ = event_sender_query.send(ZenohEvent::QueryNoResponses {
                            selector: selector_clone,
                        });
                    }
                });
```

- [ ] **Step 3b: Decide "local" by replier, not by text (F-T16-7).** Every Zenoh Explorer's queryable attached `source:local`, so replies from another machine's Explorer were marked "From local queryable". In the reply loop, capture the session id before spawning (`let own_zid = sess.zid();`) and replace the attachment check with:

```rust
// Local means this session answered, whatever the reply carries.
let is_local = reply.replier_id().is_some_and(|g| g.zid() == own_zid);
```

  `Reply::replier_id` needs zenoh's `unstable` feature, which `Cargo.toml` enables. Compute `is_local` before `reply.result()` consumes the reply. Verify with `grep -n 'source:local' src/worker/query.rs`, which must print nothing.
- [ ] **Step 3c: Keep a subscription's id across reconnect (G1-5, K8 a and b).** Today the re-declare issues a new id. An Unsubscribe clicked during the reconnect names the old id and is lost, and a failed re-declare leaves a row that can never be removed.
  - `handle_subscribe` gains the parameter `id: Option<String>` and uses `id.unwrap_or_else(next_subscription_id)`:

```rust
/// Subscribe arm: declares the subscriber on the publishing session and
/// registers its sample task. `id` is `Some` when a reconnect re-declares a
/// kept subscription, which keeps its id; a new subscription gets a fresh one.
pub(crate) async fn handle_subscribe(
    st: &mut WorkerState,
    ctx: &WorkerCtx,
    key_expr: String,
    id: Option<String>,
) {
    if let Some(ref sess) = st.publishing_session {
        match sess.declare_subscriber(&key_expr).await {
            Ok(subscriber) => {
                let sub_id = id.unwrap_or_else(next_subscription_id);
                let (task_handle, cancel_sender) = spawn_sample_task(
                    subscriber,
                    subscription_source(&sub_id), // Step 3d
                    ctx.event_sender.clone(),
                    ctx.sample_drops.clone(),
                    true,
                );
                // ... insert into st.active_subscriptions and send
                // SubscriptionCreated { id: sub_id, key_expr } as today
            }
            Err(e) => {
                error!("Failed to create subscriber: {}", e);
                if let Some(id) = id {
                    // A kept subscription that cannot come back: remove its row.
                    let _ = ctx.event_sender.send(ZenohEvent::SubscriptionRemoved { id });
                }
                let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                    op: FailedOp::Subscribe,
                    error: e.to_string(),
                });
            }
        }
    }
}
```

  - `session.rs:118-121` becomes:

```rust
            // Declare again the subscriptions the last teardown kept; each keeps its id.
            for (id, key_expr) in std::mem::take(&mut st.resubscribe) {
                subscribe::handle_subscribe(st, ctx, key_expr, Some(id)).await;
            }
```

  - `worker/mod.rs:76` passes `None`: `} => subscribe::handle_subscribe(&mut st, &ctx, key_expr, None).await,`.
  - The UI's `SubscriptionCreated` arm (`events/mod.rs:86-101`) still merges by key and sets the same id, so the row stays. Its comment "takes the new id" goes stale; T28 step 1 rewrites that arm to merge by id (K8c).
- [ ] **Step 3d: One source per user subscription (X1, T16 regression).** Every user subscription tags its samples `MessageSource::PublishingSession` (`subscribe.rs:34`). T16's rule never drops a copy from the same source (`limits.rs:46-55`). So with `demo/**` and `demo/x` both subscribed, each sample on `demo/x` is listed twice and counted twice in the tree. The fix gives each subscription its own source, so the second subscription's copy is a cross-source duplicate. A repeat from one subscription is still the same source and still counts.
  - In `types/message.rs`, add the variant and reword the old one:

```rust
pub enum MessageSource {
    /// Query replies received by the publishing session
    PublishingSession,
    /// Message from background ** subscription via the monitor session
    MonitorSession,
    /// Echo of a message published locally by this app instance
    LocalEcho,
    /// Samples of one user subscription on the publishing session, tagged with
    /// its id. Each subscription is its own source, so a sample that two
    /// overlapping subscriptions both receive is a cross-source duplicate.
    UserSubscription(std::sync::Arc<str>),
}
```

    `Arc<str>` keeps `#[derive(Debug, Clone, PartialEq)]`. A clone per sample is a reference-count increment, not an allocation. `calculate_size` still counts `size_of::<MessageSource>()`, which grows from 1 to 24 bytes (the `Arc<str>` fat pointer leaves no niche for the tag, so a separate tag is needed); no test pins that size.
  - In `subscribe.rs`, add the constructor that `handle_subscribe` and the test share:

```rust
/// The source that tags one user subscription's samples (X1): overlapping
/// subscriptions are different sources, so dedup keeps one copy.
pub(crate) fn subscription_source(id: &str) -> MessageSource {
    MessageSource::UserSubscription(Arc::from(id))
}
```

  - Check every use first with `grep -rn 'MessageSource' src`. At the base commit it prints:
    - Constructions: `subscribe.rs:34` (changed here), `session.rs:74` `MonitorSession`, `query.rs:63` `PublishingSession` for query replies, `publish.rs:182` `LocalEcho`. Tests in `samples.rs`, `pipeline.rs:96`, `commands.rs:183`, `topic_tree.rs:1037` and `limits.rs` use unit variants.
    - Readers: only the dedup path, `ingest.rs:38` and `:51`, and `limits.rs:18`, `:46`, `:57`, which compare sources with `!=`.
    - No `match` on `MessageSource` exists, and no UI label reads it. The tree's local marker comes from `is_local`, not from the source (`ingest.rs:115`). So the new variant changes only dedup. The local echo keeps working: `LocalEcho` against a subscription copy was already a cross-source pair, and still is.
  - Query replies keep `PublishingSession` and stay exempt from dedup (`ingest.rs:29`). P5's legacy query path also builds replies with `PublishingSession`.
- [ ] **Step 3e: Drop stale markers in `message.rs` (K9, this file's share).** Remove the `#[allow(dead_code)]` lines whose items already have non-test uses at the base commit:
  - `:12` on `SampleKindView::Delete` (built at `samples.rs:18`).
  - `:34` on `ZenohMessage::source` (read at `ingest.rs:38`, `:51`).
  - `:94` on `with_sample_meta` (called at `samples.rs:47`).
  - Keep `:123` (`PublishStatus::Sending`) and `:139` (`format_local_time`). Their first non-test uses come from parts e and f, which this worktree does not have. T28's closing step removes them after the merge.
- [ ] **Step 4: Verify.**

```bash
cargo test && cargo test -- --ignored invalid_selector_reports_query_failure reconnect_restores_subscriptions && cargo clippy --all-targets -- -D warnings
grep -n 'source:local' src/worker/query.rs   # prints nothing
```

- [ ] **Step 5: Commit.**

```bash
git add src/worker/subscribe.rs src/worker/query.rs src/worker/session.rs src/worker/mod.rs src/types/message.rs
git commit -m "fix(worker): report subscribe/query failures, keep ids on reconnect, one dedup source per subscription"
```

---

#### T27 part b (was T6): Queryable with Zenoh key-expression matching

**Owns:** `src/worker/queryable.rs`

**Interfaces:**
- `pub(crate) fn matching_entries(store: &LocalKvStore, scope: &keyexpr, query: &keyexpr) -> Vec<(String, StoredValue)>`. `scope` is the queryable's own pattern (G1-2).
- `pub(crate) async fn serve_queryable(sess: Arc<Session>, key_expr: String, store: Arc<RwLock<LocalKvStore>>, events: EventTx, cancel_rx: tokio::sync::mpsc::Receiver<()>)`.
- `handle_enable(st, ctx, key_expr)` and `handle_disable(st)` keep their signatures: `worker/mod.rs` (part a) calls them.
- The declare failure sends raw `e.to_string()`; do not copy a strip helper (K7).

- [ ] **Step 1: Write the failing tests** in `queryable.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn store_with(keys: &[&str]) -> LocalKvStore {
        keys.iter()
            .map(|k| (k.to_string(), StoredValue { bytes: k.as_bytes().to_vec(), encoding: "text/plain".into() }))
            .collect()
    }

    fn matched_in(store: &LocalKvStore, scope: &str, q: &str) -> Vec<String> {
        let scope = zenoh::key_expr::KeyExpr::try_from(scope).unwrap();
        let ke = zenoh::key_expr::KeyExpr::try_from(q).unwrap();
        let mut v: Vec<String> = matching_entries(store, &scope, &ke).into_iter().map(|(k, _)| k).collect();
        v.sort();
        v
    }

    fn matched(store: &LocalKvStore, q: &str) -> Vec<String> {
        matched_in(store, "**", q)
    }

    #[test]
    fn matching_respects_chunk_boundaries() {
        let s = store_with(&["demo/a", "demo/a/b", "demonstration/x"]);
        assert_eq!(matched(&s, "demo/**"), vec!["demo/a", "demo/a/b"]);
    }

    #[test]
    fn matching_single_star_and_subchunk() {
        let s = store_with(&["demo/a", "demo/ab", "demo/a/b"]);
        assert_eq!(matched(&s, "demo/*"), vec!["demo/a", "demo/ab"]);
        assert_eq!(matched(&s, "demo/a$*"), vec!["demo/a", "demo/ab"]);
    }

    #[test]
    fn matching_excludes_verbatim_chunks() {
        let s = store_with(&["@/x", "y"]);
        assert_eq!(matched(&s, "**"), vec!["y"]);
    }

    /// G1-2: `**` sent to a `demo/**` queryable must not return `other/x`.
    #[test]
    fn matching_stays_inside_queryable_pattern() {
        let s = store_with(&["demo/a", "other/x"]);
        assert_eq!(matched_in(&s, "demo/**", "**"), vec!["demo/a"]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn queryable_replies_full_payload() {
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port
        let s = Arc::new(zenoh::open(c).await.unwrap());
        let payload: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
        let store = Arc::new(RwLock::new(LocalKvStore::new()));
        store.write().unwrap().insert(
            "q/big".into(),
            StoredValue { bytes: payload.clone(), encoding: "application/octet-stream".into() },
        );
        let (_cancel_tx, cancel_rx) = tokio::sync::mpsc::channel(1);
        let (events, _rx) = crate::worker::pipeline::event_channel(16);
        tokio::spawn(serve_queryable(s.clone(), "q/**".into(), store, events, cancel_rx));
        tokio::time::sleep(Duration::from_millis(200)).await;
        let replies = s.get("q/**").await.unwrap();
        let reply = tokio::time::timeout(Duration::from_secs(5), replies.recv_async()).await.unwrap().unwrap();
        let sample = reply.result().unwrap();
        assert_eq!(sample.payload().to_bytes().as_ref(), payload.as_slice());
        assert_eq!(sample.encoding().to_string(), "application/octet-stream");
    }
}
```

  The `listen/endpoints: []` line is G1-6 (K10): a default peer listens on `tcp/[::]:0`, and the Global Constraints allow a non-ignored session test only when it binds no port. This is the repo's in-process peer pattern (`connect.rs:509-517`).
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- matching_ queryable_replies`. Expected: FAIL.
- [ ] **Step 3: Implement.**

```rust
/// Entries this queryable may serve for `query`: keys inside the queryable's own
/// `scope` that intersect `query`, using Zenoh's own key-expression semantics
/// (chunk boundaries, `$*`, verbatim `@` chunks).
pub(crate) fn matching_entries(
    store: &LocalKvStore,
    scope: &zenoh::key_expr::keyexpr,
    query: &zenoh::key_expr::keyexpr,
) -> Vec<(String, StoredValue)> {
    store
        .iter()
        .filter(|(k, _)| {
            zenoh::key_expr::KeyExpr::try_from(k.as_str())
                .map(|ke| scope.includes(&ke) && query.intersects(&ke))
                .unwrap_or(false)
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

/// Serve `store` on `key_expr` until `cancel_rx` fires or the session closes.
pub(crate) async fn serve_queryable(
    sess: Arc<Session>,
    key_expr: String,
    store: Arc<RwLock<LocalKvStore>>,
    events: EventTx,
    mut cancel_rx: tokio::sync::mpsc::Receiver<()>,
) {
    let queryable = match sess.declare_queryable(&key_expr).await {
        Ok(q) => q,
        Err(e) => {
            let _ = events.send(ZenohEvent::OperationFailed { op: FailedOp::Queryable, error: e.to_string() });
            return;
        }
    };
    loop {
        tokio::select! {
            _ = cancel_rx.recv() => break,
            query = queryable.recv_async() => {
                let Ok(query) = query else { break };
                let matches = match store.read() {
                    Ok(s) => matching_entries(&s, queryable.key_expr(), query.key_expr()),
                    Err(_) => Vec::new(),
                };
                for (key, value) in matches {
                    let _ = query
                        .reply(key.as_str(), value.bytes)
                        .encoding(value.encoding.as_str())
                        .attachment("source:local")
                        .await;
                }
            }
        }
    }
}
```

  - The queryable's reply key is checked only against the query (zenoh 1.10.1 `api/queryable.rs:584-586`), so the scope check is ours to make. `Queryable::key_expr()` (`api/queryable.rs:918`) gives the scope.
  - Make `handle_enable` spawn `serve_queryable(sess.clone(), key_expr, ctx.local_kvstore.clone(), ctx.event_sender.clone(), cancel_rx)` and delete the old inline loop and matcher.
- [ ] **Step 3b: Drop the `source:local` attachment (F-T16-7).** In `serve_queryable`, remove `.attachment("source:local")` from the reply builder; part a (was T5) decides locality from the replier id. `grep -n 'source:local' src/worker/queryable.rs` must print nothing.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/queryable.rs
git commit -m "fix(queryable): serve stored bytes; match with keyexpr::intersects inside the queryable's pattern"
```

---

#### T27 part c (was T9): Export store budget and GC

**Owns:** `src/transfer.rs`

**Interfaces:** keep every existing `pub` item and signature (`parse_chunk_key`, `insert_payload`, `format_size`, `get_payload_for_export`, `chunk_progress`, `ChunkMeta`). Part d and part f read them. Add `MAX_PLAIN_BYTES`, `STALE_TRANSFER_AGE` and `gc_stale_transfers`.

- [ ] **Step 1: Write the failing tests.**

```rust
    #[test]
    fn plain_entries_respect_byte_budget() {
        let mut store = PayloadStoreMap::new();
        let big = MAX_PLAIN_BYTES / 2 + 1;
        insert_payload(&mut store, "a".into(), entry_with(vec![0; big], 100));
        insert_payload(&mut store, "b".into(), entry_with(vec![0; big], 200));
        let total: usize = store.values().map(|e| e.bytes.len()).sum();
        assert!(total <= MAX_PLAIN_BYTES);
        assert!(store.contains_key("b") && !store.contains_key("a"));
    }

    #[test]
    fn stale_incomplete_transfers_are_collected() {
        let mut store = PayloadStoreMap::new();
        let mut old = entry_with(vec![0; 10], 0);
        old.received_at = chrono::Utc::now() - chrono::Duration::minutes(11);
        store.insert(format!("t/__chunk/{}/2/0", CHUNK_SIZE + 10), old);
        gc_stale_transfers(&mut store, chrono::Utc::now());
        assert!(store.is_empty());
    }

    /// G1-3: a complete transfer stays exportable however old it is.
    #[test]
    fn stale_complete_transfer_is_kept() {
        let mut store = PayloadStoreMap::new();
        for i in 0..2 {
            let mut e = entry_with(vec![0; 10], 0);
            e.received_at = chrono::Utc::now() - chrono::Duration::minutes(11);
            store.insert(format!("t/__chunk/{}/2/{i}", CHUNK_SIZE + 10), e);
        }
        gc_stale_transfers(&mut store, chrono::Utc::now());
        assert_eq!(store.len(), 2, "Save File still has both chunks");
    }

    /// G1-4: an oversized value also removes the key's older value.
    #[test]
    fn oversized_plain_entry_drops_previous_value() {
        let mut store = PayloadStoreMap::new();
        insert_payload(&mut store, "k".into(), entry_with(vec![1, 2, 3], 100));
        insert_payload(&mut store, "k".into(), entry_with(vec![0; MAX_PLAIN_BYTES + 1], 200));
        assert!(!store.contains_key("k"), "Save File must not export the older value");
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- plain_entries_respect stale_incomplete stale_complete oversized_plain`. Expected: FAIL.
- [ ] **Step 3: Implement.**

```rust
/// Byte budget for non-chunk entries in the export store.
pub const MAX_PLAIN_BYTES: usize = 512 * 1024 * 1024;

/// Incomplete chunk groups with no new chunk for this long are dropped.
pub const STALE_TRANSFER_AGE: chrono::Duration = chrono::Duration::minutes(10);

/// Remove incomplete chunk groups whose newest chunk is older than STALE_TRANSFER_AGE; complete groups stay exportable.
pub fn gc_stale_transfers(store: &mut PayloadStoreMap, now: DateTime<Utc>) {
    let mut groups: HashMap<(String, usize, usize), (DateTime<Utc>, HashSet<usize>)> = HashMap::new();
    for (k, e) in store.iter() {
        if let Some((t, m)) = parse_chunk_key(k) {
            let (newest, indices) = groups
                .entry((t.to_string(), m.total_size, m.total_chunks))
                .or_insert((e.received_at, HashSet::new()));
            if e.received_at > *newest {
                *newest = e.received_at;
            }
            indices.insert(m.index);
        }
    }
    store.retain(|k, _| match parse_chunk_key(k) {
        Some((t, m)) => groups
            .get(&(t.to_string(), m.total_size, m.total_chunks))
            .is_none_or(|(at, idx)| idx.len() == m.total_chunks || now - *at < STALE_TRANSFER_AGE),
        None => true,
    });
}
```

  - G1-3: without the index set, GC also removed complete transfers. Save File then failed with "No payload stored" while the tree still showed ✓ and the 💾 button (`topic_tree.rs:115-124`, `:766-771`).
  - In the plain branch of `insert_payload`, put the size check at the **top**, before the count-cap eviction (G1-4). Otherwise an oversized value returns before `store.remove(&key)`, Save File exports the key's previous bytes, and the count cap may already have evicted an unrelated entry:

```rust
        if entry.bytes.len() > MAX_PLAIN_BYTES {
            store.remove(&key);
            info!("Not storing {} for export: {} bytes exceeds budget", key, entry.bytes.len());
            return;
        }
```

  - Then keep the existing count-cap eviction, then `store.remove(&key);`, then the budget loop, then the insert, in that order:

```rust
        store.remove(&key);
        let mut plain_bytes: usize = store
            .iter()
            .filter(|(k, _)| !k.contains("/__chunk/"))
            .map(|(_, e)| e.bytes.len())
            .sum();
        while plain_bytes + entry.bytes.len() > MAX_PLAIN_BYTES {
            let Some(oldest) = store
                .iter()
                .filter(|(k, _)| !k.contains("/__chunk/"))
                .min_by_key(|(_, e)| e.received_at)
                .map(|(k, _)| k.clone())
            else {
                break;
            };
            if let Some(e) = store.remove(&oldest) {
                plain_bytes -= e.bytes.len();
            }
        }
```

  - At the top of the chunk branch, call `gc_stale_transfers(store, Utc::now());`.
  - Optional: in `get_payload_for_export`, delete the two checks that T8's per-chunk length check made unreachable: the `actual` sum with its "Corrupt transfer: chunks total …" error, and the "Reassembled size mismatch …" error. No test names either text. Keep them if unsure; every existing transfer test must still pass.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/transfer.rs
git commit -m "fix(transfer): byte budget for export store; GC stale incomplete transfers"
```

---

#### T27 part d (was T17): Rate limit applies to display only

**Owns:** `src/events/ingest.rs`

**Interfaces:** `process_single_message` becomes `pub(crate)`. `add_message_with_limits(message, display, store)` gains `store: bool`. Part a's per-subscription source (X1) needs nothing here: ingest compares sources with `!=`.

- [ ] **Step 1: Write the failing tests** in `ingest.rs`, and make `process_single_message` `pub(crate)`:

```rust
#[cfg(test)]
mod tests {
    use crate::app::ZenohExplorer;
    use crate::types::*;

    fn msg(key: &str, source: MessageSource, ty: MessageType, local: bool) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            key.into(), "v".into(), b"v".to_vec(), "text/plain".into(), chrono::Utc::now(),
            ty, local, source,
        )
    }

    #[test]
    fn rate_limited_messages_still_update_tree() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        for _ in 0..3 {
            app.process_single_message(msg("r/x", MessageSource::MonitorSession, MessageType::Subscribe, false));
        }
        let count = app.browse_tree.read().unwrap().children["r"].children["x"].message_count;
        assert_eq!(count, 3);
        assert_eq!(app.messages.len(), 1);
        assert_eq!(app.rate_limit_drops, 2);
    }

    #[test]
    fn local_and_remote_replies_are_both_kept() {
        // F-T16-8: the old local-wins rule dropped remote replies for a key in
        // every later query. Both replies must be listed, and accounted.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.process_single_message(msg("q/a", MessageSource::PublishingSession, MessageType::QueryReply, true));
        app.process_single_message(msg("q/a", MessageSource::PublishingSession, MessageType::QueryReply, false));
        assert_eq!(app.messages.len(), 2);
        let sum: usize = app.messages.iter().map(|m| m.size_bytes).sum();
        assert_eq!(app.current_memory_bytes, sum);
    }

    #[test]
    fn query_replies_skip_tree_and_pause() {
        // F-T16-9: a reply is listed even for a paused key or over the rate
        // limit, clears the waiting alert, and leaves tree counts, the Current
        // Value and the Save store alone.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        app.paused_keys.insert("q/b".into());
        app.query_alert = Some("Query sent for 'q/**'. Waiting for responses...".into());
        for _ in 0..2 {
            app.process_single_message(msg("q/b", MessageSource::PublishingSession, MessageType::QueryReply, false));
        }
        assert_eq!(app.messages.len(), 2);
        assert_eq!(app.rate_limit_drops, 0);
        assert!(app.query_alert.is_none());
        let tree = app.browse_tree.read().unwrap();
        assert!(tree.children.get("q").and_then(|q| q.children.get("b")).is_none_or(|n| n.message_count == 0));
        assert!(app.payload_store.read().unwrap().get("q/b").is_none());
    }

    #[test]
    fn paused_topic_does_not_use_rate_budget() {
        // G3-1: paused and chunk rows are never listed, so they must not use
        // the list's rate budget or count as "not listed (rate)".
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.rate_limiter = RateLimiter::new(1);
        app.paused_keys.insert("p/x".into());
        app.process_single_message(msg("p/x", MessageSource::MonitorSession, MessageType::Subscribe, false));
        app.process_single_message(msg("r/y", MessageSource::MonitorSession, MessageType::Subscribe, false));
        assert_eq!(app.messages.len(), 1);
        assert_eq!(app.rate_limit_drops, 0);
    }

    #[test]
    fn tree_marker_counts_raw_bytes() {
        // K5: "+N bytes" counts the raw payload, not the ≤50 KiB display preview.
        let (mut app, _tx) = ZenohExplorer::test_app();
        let bytes = vec![b'a'; 100 * 1024];
        app.process_single_message(ZenohMessage::new_with_bytes(
            "big/x".into(),
            crate::payload::preview(&bytes, MAX_UI_DISPLAY_SIZE),
            bytes,
            "text/plain".into(),
            chrono::Utc::now(),
            MessageType::Subscribe,
            false,
            MessageSource::MonitorSession,
        ));
        let tree = app.browse_tree.read().unwrap();
        let shown = tree.children["big"].children["x"].last_payload.clone().unwrap();
        assert!(shown.ends_with(&format!(
            "[+{} bytes · Save File writes all of it]",
            100 * 1024 - 10 * 1024
        )));
    }

    #[test]
    fn delete_is_not_a_duplicate_of_empty_put() {
        // K6: a Delete carries no bytes, like an empty Put, so the dedup key
        // must include the sample kind.
        let (mut app, _tx) = ZenohExplorer::test_app();
        let put = ZenohMessage::new_with_bytes(
            "d/x".into(), String::new(), vec![], "text/plain".into(), chrono::Utc::now(),
            MessageType::Publish, true, MessageSource::LocalEcho,
        );
        let del = ZenohMessage::new_with_bytes(
            "d/x".into(), "[DELETE]".into(), vec![], "text/plain".into(), chrono::Utc::now(),
            MessageType::Subscribe, false, MessageSource::MonitorSession,
        )
        .with_sample_meta(SampleKindView::Delete, None);
        app.process_single_message(put);
        app.process_single_message(del);
        let tree = app.browse_tree.read().unwrap();
        let node = &tree.children["d"].children["x"];
        assert_eq!(node.message_count, 2);
        assert_eq!(node.last_kind, SampleKindView::Delete);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- rate_limited local_and_remote query_replies_skip paused_topic_does_not tree_marker_counts delete_is_not`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Delete the local-wins block entirely (F-T16-8). The review found it silently drops remote replies for a key in every later query and strands "Waiting for responses…"; its in-place replace was also the R16 accounting hole. Do **not** use the replacement below, which the first draft of this plan proposed:

```rust
        if message.message_type == MessageType::QueryReply {
            if let Some(idx) = self
                .messages
                .iter()
                .position(|m| m.key == message.key && m.message_type == MessageType::QueryReply)
            {
                match (message.is_local, self.messages[idx].is_local) {
                    (true, false) => {
                        if let Some(old) = self.messages.remove(idx) {
                            self.current_memory_bytes =
                                self.current_memory_bytes.saturating_sub(old.size_bytes);
                        }
                    }
                    (false, true) => return,
                    _ => {}
                }
            }
        }
```

  - Replace the rate-limit block and the `display` computation. Only rows that would be listed use the rate budget (G3-1):

```rust
        if let Some(h) = dedup_hash {
            self.deduper.record(h, message.source.clone());
        }
        let is_query_reply = message.message_type == MessageType::QueryReply;
        let is_chunk = crate::transfer::parse_chunk_key(&message.key).is_some();
        // Only rows that would be listed use the list's rate budget; replies are always listed.
        let display = is_query_reply
            || (!is_chunk && !self.paused_keys.contains(&message.key) && {
                let ok = self.rate_limiter.check_and_update();
                if !ok {
                    self.rate_limit_drops += 1;
                }
                ok
            });
```

  - The dedup key includes the sample kind (G3-3, K6). In the `dedup_hash` closure: `let h = Deduper::hash_message(&message.key, bytes); if message.kind == SampleKindView::Delete { !h } else { h }`. This stays inside `ingest.rs`; `limits.rs` is not needed.
  - Query replies (F-T16-9): compute `is_query_reply` first. For a reply, skip `add_message_to_browse_tree` and the `payload_store` insert, set `display = true` whatever the pause set and the rate limiter say, and clear `query_alert` if it starts with `"Query sent"` before any early return in this function. Do this with a `store: bool` parameter on `add_message_with_limits` (false for replies) that guards only the `payload_store` insert. The `payload_bytes.take()` still runs, so a listed reply never keeps its raw bytes (G3-4).
  - List markers (F-T14-3): change the list marker to `"... [truncated · Save File writes all of it]"` and the tree preview marker to `format!("\n... [+{} bytes · Save File writes all of it]", full_len - safe_end)`, where `full_len = message.payload_bytes.as_ref().map_or(message.payload.len(), Vec::len)` (G3-2, K5). `add_message_to_browse_tree` runs before `add_message_with_limits` takes the bytes, so `payload_bytes` is still there. `grep -rn 'use Export' src` must print nothing.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/events/ingest.rs
git commit -m "fix(events): rate limit thins the list only; keep every query reply out of tree counts and pause"
```

---

#### T27 part e (was T25, then T18): All Messages tells the truth; JSON cache keyed on the full payload

**Owns:** `src/ui/messages.rs`, then `src/events/json_cache.rs` and `src/types/mod.rs`

**Order:** one agent runs T25's steps first, then T18's. `ui/messages.rs` used `MAX_HASH_BYTES` for its search slice, and T18 deletes the constant; T25 removes that use first. No other file uses `MAX_HASH_BYTES`.

##### Steps from T25: All Messages tells the truth

**Findings:** F-T13-13 (All Messages side), F-T14-7, F-T20-2 (list side), F-T7-7 (list side), F-T14-6, F-T20-6 (Clear).

**Interfaces:**
- `pub(crate) fn filtered_tail<'a>(messages: &'a VecDeque<ZenohMessage>, filter: &str, max: usize) -> (Vec<&'a ZenohMessage>, usize)` returns the newest `max` matches, oldest first, and the total number of matches.
- `fn paused_note(keys: &[&str]) -> String` words the paused-topics line (G3-9).

- [ ] **Step 1: Write the failing tests** in `messages.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    fn m(key: &str) -> ZenohMessage {
        ZenohMessage::new_with_bytes(
            key.into(), "p".into(), vec![], "text/plain".into(), chrono::Utc::now(),
            MessageType::Subscribe, false, MessageSource::MonitorSession,
        )
    }

    #[test]
    fn filter_searches_whole_list_case_insensitively() {
        let mut list: VecDeque<ZenohMessage> = VecDeque::new();
        list.push_back(m("Old/Match"));
        for i in 0..600 {
            list.push_back(m(&format!("noise/{i}")));
        }
        let (shown, total) = filtered_tail(&list, "old/match", 500);
        assert_eq!((shown.len(), total), (1, 1), "a match older than the newest 500 rows is still found");
        let (shown, total) = filtered_tail(&list, "", 500);
        assert_eq!((shown.len(), total), (500, 601));
    }

    #[test]
    fn paused_note_is_singular_at_one_and_bounded() {
        assert_eq!(paused_note(&["a/x"]), "New messages on 1 paused topic are not listed: a/x");
        assert_eq!(
            paused_note(&["e", "c", "a", "d", "b"]),
            "New messages on 5 paused topics are not listed: a, b, c, and 2 more"
        );
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- filter_searches_whole_list paused_note_is`. Expected: compile error.
- [ ] **Step 3: Implement.**
  - `filtered_tail`: lower-case the filter once. A row matches when its key or the first 4 KiB of its payload (a local `const SEARCH_BYTES: usize = 4 * 1024;` replaces `MAX_HASH_BYTES`, which T18 deletes) contains the filter, lower-cased. Walk newest first, collect up to `max`, count every match, and reverse the collected rows. Match without allocating per row (G3-10). Return early with `(tail, len)` when the filter is empty; this also keeps `windows(0)`, which panics, out of the path. Otherwise compare with an ASCII case fold (`hay.as_bytes().windows(f.len()).any(|w| w.eq_ignore_ascii_case(f.as_bytes()))`), and fall back to `to_lowercase` only when the filter is not ASCII.
  - The list uses `egui::ScrollArea::vertical().id_salt("all_messages")`, so it no longer shares scroll state with Message History (F-T13-13).
  - Above the list, when `total > shown`, show `"Showing the newest {shown} of {total} matching rows"` if the filter is non-empty, and `"Showing the newest {shown} of {total} rows"` if it is empty (G3-11; T19 uses the same "newest N of M" wording).
  - The count reads `format!("In list: {} (limit {})", self.messages.len(), self.max_messages)` (F-T20-2).
  - When `paused_keys` is non-empty, add a line built by `paused_note` with a `"Resume all"` button that clears `paused_keys` (F-T7-7). `paused_note` sorts its input. For n == 1 it reads `"New messages on 1 paused topic are not listed: {key}"`. Otherwise it reads `"New messages on {n} paused topics are not listed: {first three keys, sorted, joined by ", "}"`, plus `", and {n-3} more"` when n > 3 (G3-9, K2). Rows listed before the pause, and query replies, stay listed, so the line says "New messages".
  - Row times use `format_local_time` (F-T14-6).
  - Clear also resets `messages_deduped` (F-T20-6).
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && ! grep -n 'MAX_HASH_BYTES' src/ui/messages.rs`.
- [ ] **Step 5: Commit.**

```bash
git add src/ui/messages.rs
git commit -m "fix(ui): All Messages filters the whole list and keeps its own scroll state"
```

##### Steps from T18: JSON cache keyed on the full payload

- [ ] **Step 1: Write the failing test** in `json_cache.rs`:

```rust
#[cfg(test)]
mod tests {
    use crate::app::ZenohExplorer;

    #[test]
    fn json_cache_distinguishes_shared_prefix() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let prefix = "1,".repeat(2500); // 5000 bytes, beyond the old 4 KB window
        let a = format!("[{}1]", prefix);
        let b = format!("[{}2]", prefix);
        assert_ne!(app.get_cached_json(&a).unwrap(), app.get_cached_json(&b).unwrap());
    }
}
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test json_cache_distinguishes_shared_prefix`. Expected: FAIL.
- [ ] **Step 3: Implement.**

```rust
    /// Hash of the full payload (seahash), used as the JSON cache key.
    pub(crate) fn compute_payload_hash(payload: &str) -> u64 {
        seahash::hash(payload.as_bytes())
    }
```

  - Change the eviction to `// Bounded cache: reset when it grows past 256 distinct payloads.` with `> 256`.
  - Delete `MAX_HASH_BYTES` from `types/mod.rs`.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -rn MAX_HASH_BYTES src`. Expected: grep prints nothing.
- [ ] **Step 5: Commit.**

```bash
git add src/events/json_cache.rs src/types/mod.rs
git commit -m "fix(ui): JSON pretty-print cache keyed on the full payload"
```

---

#### T27 part f (was T23): Publish and Query views say what happened

**Owns:** `src/ui/publish.rs`, `src/ui/query.rs`

**Findings:** F-T8-4, F-T15-3, F-T15-7, F-T15-8, F-T16-2, F-T16-5, F-T16-6 (caption), F-T16-11, F-T14-6 (query results).

**Interfaces:** it reads T3's `publish_status`, `PublishStatus` and `format_local_time`, T14's `validation::wildcard_note`, and `crate::transfer::format_size` (part c keeps it). T7 makes the worker send `Published` or `OperationFailed { op: Publish }`, which end the pending state; T3 only added the `Published` variant and its UI arm. It produces five pure helpers, so the words are testable without a UI harness (P1 has none):
- in `publish.rs`: `fn publish_status_line(s: &PublishStatus) -> String` and `fn publish_button_label(payload_empty: bool, pending: bool) -> &'static str`;
- in `query.rs`: `pub(crate) fn connection_notice(status: &ConnectionStatus) -> Option<&'static str>` (publish.rs reuses it), `fn queryable_summary(enabled: bool, pattern: &str, stored: usize) -> String` and `fn served_count(store: &LocalKvStore, pattern: &str) -> usize`.

- [ ] **Step 1: Write the failing tests.**
  - In `publish.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn publish_status_line_words() {
        let s = PublishStatus::Sending { key: "demo/test".into(), bytes: 12 };
        assert_eq!(publish_status_line(&s), "Publishing 12 bytes to demo/test…");
        let s = PublishStatus::Failed("demo//x: invalid key".into());
        assert_eq!(publish_status_line(&s), "Not published: demo//x: invalid key");
        let s = PublishStatus::Published { key: "k".into(), bytes: 3, at: chrono::Utc::now() };
        assert!(publish_status_line(&s).starts_with("Published 3 bytes to k · "));
    }

    #[test]
    fn publish_button_label_rules() {
        assert_eq!(publish_button_label(false, false), "Publish");
        assert_eq!(publish_button_label(true, false), "Publish empty payload");
        assert_eq!(publish_button_label(false, true), "Publishing…");
    }
}
```

  - In `query.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn connection_notice_matches_state() {
        assert_eq!(connection_notice(&ConnectionStatus::Connected), None);
        assert_eq!(connection_notice(&ConnectionStatus::ConnectingMonitor), Some("Connecting… available once connected"));
        assert_eq!(connection_notice(&ConnectionStatus::Disconnected), Some("Not connected"));
        assert_eq!(
            connection_notice(&ConnectionStatus::Error("x".into())),
            Some("Not connected: the last connection attempt failed (see Connection Settings)")
        );
    }

    #[test]
    fn queryable_summary_words() {
        assert_eq!(queryable_summary(false, "**", 0), "This app's queryable is off (Publish tab)");
        assert_eq!(queryable_summary(true, "demo/**", 3), "This app answers demo/** from 3 values it published (Publish tab)");
        assert_eq!(queryable_summary(true, "demo/**", 1), "This app answers demo/** from 1 value it published (Publish tab)");
        assert_eq!(queryable_summary(true, "demo/**", 0), "This app answers demo/** but has published nothing under it yet (Publish tab)");
        // The count is what the pattern serves (G1-2 follow-on). It stays in this
        // test: P5's `cargo test -- query_book:: ui::query` expects 12 tests.
        let store: LocalKvStore = ["demo/a", "other/x"]
            .iter()
            .map(|k| (k.to_string(), StoredValue { bytes: vec![], encoding: "text/plain".into() }))
            .collect();
        assert_eq!(served_count(&store, "demo/**"), 1);
        assert_eq!(served_count(&store, "**"), 2);
        assert_eq!(served_count(&store, "demo/"), 0, "an invalid pattern serves nothing");
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- ui::publish ui::query`. Expected: compile errors.
- [ ] **Step 3: Publish view.**
  - Replace the "⚠ Not connected. Please connect first." line with `connection_notice(&self.connection_status)` in neutral text (F-T16-11).
  - Keep T14's wildcard note under the key (`publish.rs:36-42`); do not add a second one (G3-7).
  - Button: `let pending = matches!(self.publish_status, Some(PublishStatus::Sending { .. }));`. The label is `publish_button_label(payload_is_empty, pending)`. A click while `pending` does nothing, so the button stays enabled and there is no Tab-order trap (F-T7-12). On send, set `self.publish_status = Some(PublishStatus::Sending { key, bytes })`. Under the button, show `publish_status_line` when `publish_status` is `Some` (F-T8-4, F-T15-3). `publish_status_line` writes sizes with `crate::transfer::format_size` (`12 bytes`, `2.00 MB`), not a raw `B` count (G3-8).
  - Queryable group (F-T15-7, F-T16-6, F-T15-8):
    - Replace both captions with `"Answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imports)"`.
    - Make the Key Pattern field `ui.add_enabled(!self.queryable_enabled, …)` with the hover `"Untick Enable Queryable to change the pattern"`.
    - Show "Active" only while connected. While disconnected, show `"Off: not connected"`.
- [ ] **Step 4: Query view.**
  - Replace the not-connected line with `connection_notice` (F-T16-11). Its error text names the panel by its label, "Connection Settings" (`app/layout.rs:258`) (G3-8).
  - Replace the two note lines with one: `"Asks every queryable that matches the selector. With no match the answer comes back at once; a matching queryable that stays silent is reported when the timeout expires."` (F-T16-2).
  - Add one line: `queryable_summary(self.queryable_enabled, &self.queryable_pattern, self.local_kvstore.read().map_or(0, |s| served_count(&s, &self.queryable_pattern)))` (F-T16-5). `served_count` counts only stored keys that the pattern includes (`keyexpr::includes`), and returns 0 when the pattern does not parse. This matches what part b's queryable serves (G1-2 follow-on).
  - `queryable_summary` has a singular and a zero form: `"from 1 value it published"` and `"but has published nothing under it yet"` (G3-5).
  - In Query Results, format card times with `format_local_time(&m.timestamp, &chrono::Utc::now())` (F-T14-6).
- [ ] **Step 5: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`. The manual check below is run by the user in T28's closing step (was T20, Step 4), and agents record what the user reports there. Connected: publish `demo/test` twice (the status line changes from "Publishing…" to "Published … · time" both times, and the field keeps its text); type `demo//x` (the button is disabled with T14's reason; the "Not published: …" path is pinned by T7 `failed_put_is_not_echoed_or_stored`); tick Enable Queryable (the pattern field locks); open Query (the queryable line names the pattern).
- [ ] **Step 6: Commit.**

```bash
git add src/ui/publish.rs src/ui/query.rs
git commit -m "fix(ui): Publish and Query views report outcomes, state and real queryable scope"
```

---

#### T27: items left for T28, and the Done-when for the whole task

**Left for T28** (outside T27's files, or needing T28 step 1's shared helper):
- K7, the rest (T28 step 1): `connect.rs` error text reuses `validation::strip_source_path` once T28 step 1 (was T21) makes it `pub(crate)`.
- K8 c (T28 step 1): `SubscriptionCreated` merges rows by id, not key (`events/mod.rs:86-101`), and a second Subscribe for a pending key does nothing. It relies on part a's kept ids. The same arm's "takes the new id" comment goes stale after part a.
- K8 d (T28 step 2c): spawned worker tasks still call the blocking `SyncSender::send`: the discovery loop (`session.rs:47-59`), part a's reply task and part b's `serve_queryable`. A non-blocking fix needs one shared helper in `pipeline.rs` used by parts a and b, so it cannot run inside T27's parallel parts.
- K9, the rest (T28 steps 1 and 4): stale `#[allow(dead_code)]` in `commands.rs`, `store.rs`, `payload.rs`, `app/mod.rs`, `validation.rs:53`, plus `message.rs:123` and `:139` once parts e and f are merged.
- K10, the rest (T28 step 4): `listen/endpoints: []` in the non-ignored session tests of `samples.rs` and `publish.rs`, and the `source:local` comment at `samples.rs:27-28`.

**Done when:**
- The integration round on the merged tree passes: `cargo build`, `cargo test`, `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`.
- `cargo test -- --ignored invalid_selector_reports_query_failure reconnect_restores_subscriptions` passes.
- Part a: tests `subscription_ids_are_unique`, `overlapping_subscriptions_list_one_copy`, `failed_redeclare_removes_the_row` and `silent_queryable_reports_timeout_once` pass.
- Part b: tests `matching_*` (including `matching_stays_inside_queryable_pattern`) and `queryable_replies_full_payload` pass.
- Part c: tests `plain_entries_respect_byte_budget`, `stale_incomplete_transfers_are_collected`, `stale_complete_transfer_is_kept` and `oversized_plain_entry_drops_previous_value` pass.
- Part d: tests `rate_limited_messages_still_update_tree`, `local_and_remote_replies_are_both_kept`, `query_replies_skip_tree_and_pause`, `paused_topic_does_not_use_rate_budget`, `tree_marker_counts_raw_bytes` and `delete_is_not_a_duplicate_of_empty_put` pass.
- Part e: tests `filter_searches_whole_list_case_insensitively`, `paused_note_is_singular_at_one_and_bounded` and `json_cache_distinguishes_shared_prefix` pass.
- Part f: tests `publish_status_line_words`, `publish_button_label_rules`, `connection_notice_matches_state` and `queryable_summary_words` pass.
- `grep -rn 'use Export' src`, `grep -rn MAX_HASH_BYTES src` and `grep -n 'source:local' src/worker/query.rs src/worker/queryable.rs` print nothing.
- `grep -n 'a queryable answered with an error' src/worker/query.rs` and `grep -n 'listen/endpoints' src/worker/queryable.rs` match.
- `git diff --name-only $BASE HEAD` lists only the 13 owned files.
- Six verifiers, one per part, report no open finding.

---

### Task T28: UI truth, help and integration

**Replaces:** T21, T22, T24, T26 and T20. The old sections follow as T28 steps, with the pre-flight amendments and the known issues applied. Inside the steps, older task IDs keep their meaning: T5, T6, T9, T17, T25, T18 and T23 are T27 parts a, b, c, d, e, e and f; T21, T22, T24, T26 and T20 are T28 steps 1, 2a, 2b, 3 and 4.

**Depends on:** T1, T13, T19, T27. T1 is there for step 4's audit, as it was for T20.

**Owns (union; each step lists its own):**
- `src/app/mod.rs`, `src/app/layout.rs`, `src/events/mod.rs` (step 1, then step 2a).
- `src/validation.rs`, `src/worker/connect.rs` (step 1).
- `src/ui/topic_tree.rs` (step 1: the Save alert producers and the Subscribe block only; then step 2b).
- `src/types/tree.rs` (step 2b).
- `src/worker/session.rs`, `src/worker/pipeline.rs`, `src/worker/query.rs`, `src/worker/queryable.rs` (step 2c).
- `src/ui/help.rs` (step 3).
- `src/types/message.rs`, `src/types/commands.rs`, `src/types/store.rs`, `src/types/limits.rs`, `src/payload.rs`, `src/worker/samples.rs`, `src/worker/publish.rs` (step 4, Step 0: dead-code markers, test session config and comments only).

Each of these files was last owned by a completed task or by T27, so nothing else runs on them while T28 runs.

**Interfaces (details in each step):**
- Step 1: `UiAlert` and `ui_alert: Option<UiAlert>`; `health_tick`, `PING_INTERVAL`, `WORKER_TIMEOUT`, `UI_STALL`; `IDLE_REPAINT_SECS` in `app/mod.rs`; `validation::strip_source_path` becomes `pub(crate)` and `connect.rs` uses it; the field `pending_subscribes` and `ZenohExplorer::subscribe_enabled`. Later plans must wrap `ui_alert` text in a `UiAlert` variant (see step 1 and the Baseline table).
- Step 2a: `MemLevel`, `memory_readout`, `peers_text`, `header_error_clause`; the fields `memory_alert`, `monitor_ok`, `stored_bytes_cache`; `update_memory_alert` and `header_status_text`.
- Step 2b: `SubtreeSummary`, `ZenohNode::subtree_summary`, `filter_repaint_after`; `history_empty_reason` and `count_hover` in `topic_tree.rs`.
- Step 2c: `pipeline::send_event`.
- Step 3: `HELP_SECTIONS`.

**How to run.** Start T28 on the board once, before step 1 (`scripts/bin/bearhug-work start T28 --session <session> --provider <provider>`), and complete it once, after step 4, with step 4's evidence. Steps run in order. Only step 2 runs agents in parallel.
1. **Step 1 (was T21): one agent**, on the T28 branch at the commit where T27 is complete. It works test-first, runs `cargo build`, `cargo test`, `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`, and commits its files. One verifier reviews the diff.
2. **Step 2: three agents in parallel**, each in its own git worktree off the step-1 commit:
   - 2a (was T22): `app/mod.rs`, `app/layout.rs`, `events/mod.rs`.
   - 2b (was T24): `ui/topic_tree.rs`, `types/tree.rs`.
   - 2c (new, K8d): `worker/session.rs`, `worker/pipeline.rs`, `worker/query.rs`, `worker/queryable.rs`.

   Each agent runs its tests and the four cargo commands in its worktree, and commits only its files. Only 2c runs ignored network tests, so no two worktrees bind the same test port. The file sets are disjoint, and no sub-step uses a symbol another one adds, so the merges cannot conflict. After the merge, run the four cargo commands once on the T28 branch, then one verifier per sub-step.

```bash
git worktree add -b t28-2a ../t28-2a
git worktree add -b t28-2b ../t28-2b
git worktree add -b t28-2c ../t28-2c
# after each sub-step has committed in its worktree:
git merge --no-ff t28-2a && git merge --no-ff t28-2b && git merge --no-ff t28-2c
git worktree remove ../t28-2a && git worktree remove ../t28-2b && git worktree remove ../t28-2c
git branch -d t28-2a t28-2b t28-2c
cargo build && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check
```

3. **Step 3 (was T26): one agent** on `ui/help.rs`. Tests, the four cargo commands, a commit and one verifier.
4. **Step 4 (was T20).** After step 3 has committed, an agent runs Step 0 (the cleanup) and commits. An agent then runs Steps 1 and 2: every command, the ignored network tests, the audit and the residue greps. The user runs Steps 3 and 4 (idle CPU, and the smoke run with every manual check earlier tasks left), and the agent records what the user reports. Step 5 puts it all in T28's completion evidence.

**Done when:**
- Step 1: tests `operation_failure_is_an_error_alert`, `ping_is_sent_once_per_interval`, `unanswered_ping_marks_unhealthy_after_timeout`, `ui_stall_does_not_mark_worker_unhealthy`, `no_reply_verdict_does_not_claim_absence`, `disconnect_cancels_waiting_query`, `pending_publish_ends_on_disconnect_or_worker_loss`, `subscription_rows_merge_by_id`, `double_subscribe_is_ignored_while_pending`, `strip_source_path_removes_every_suffix`, `user_error_drops_bang_before_space`, `endpoint_parse_error_is_a_user_error` and `connect_error_text_has_no_source_path` pass. `grep -rn "starts_with('✓')" src` and `grep -n 'Monitor connection' src/worker/connect.rs` print nothing.
- Step 2a: tests `memory_readout_names_scope_and_counts_stored_payloads`, `memory_level_uses_one_threshold_set`, `memory_warning_does_not_touch_query_alert`, `peer_count_is_worded_and_shown_at_zero`, `monitor_failure_shows_in_header`, `header_error_clause_keeps_first_clause` and `form_locators_trim_inputs` pass.
- Step 2b: tests `branch_summary_counts_subtree`, `local_marker_follows_latest_value`, `history_empty_reason_rules`, `count_hover_names_unit`, `filter_throttle_schedules_repaint`, `history_excludes_query_replies`, `history_names_the_scan_window_instead_of_claiming_empty` and `topic_details_show_delete_and_source_time` pass. `grep -n 'id_salt(("history"' src/ui/topic_tree.rs` and `grep -n 'filter_repaint_after' src/ui/topic_tree.rs` match. `grep -n 'Subscribe tab' src/ui/topic_tree.rs` prints nothing.
- Step 2c: test `send_event_does_not_block_the_runtime` passes. `grep -n '\.send(ZenohEvent' src/worker/query.rs src/worker/queryable.rs` prints nothing. `grep -n 'try_send(ZenohEvent::DiscoveryUpdate' src/worker/session.rs` matches.
- Step 3: tests `help_names_only_real_places` and `help_claims_match_limits` pass.
- Step 4: `grep -rn 'allow(dead_code)' src` lists only the markers Step 0 keeps. `grep -n 'listen/endpoints' src/worker/samples.rs src/worker/publish.rs` matches in both files. Every command in step 4 passes, including `cargo test --locked -- --ignored` and `cargo audit`. Its output and the user's smoke-run notes are in the evidence, including Help scrolled to its last line at 1000×600.

---

#### T28 step 1 (was T21): Alerts, worker health and query outcomes

**Owns:** `src/app/mod.rs`, `src/app/layout.rs`, `src/events/mod.rs`, `src/validation.rs` (G3-6), `src/worker/connect.rs` (the rest of K7), `src/ui/topic_tree.rs` (the Save alert producers only, plus the Subscribe block for K8c)

**Findings:** F-T7-4, F-T8-7, F-T20-8 (the false alarm, not the pulse: motion belongs to the Snow White plan), F-T16-1, F-T16-11.
Pre-flight: G3-6 (K7, the central strip), G2-3, G2-4, G2-5 and the `app/mod.rs` and `validation.rs` part of K9. Two proposed follow-ups are folded in here, because this step already owns their files or makes the helper they need: the rest of K7 (worker error text at the source, `connect.rs`) and K8c (subscription rows by id).

**Interfaces:**
- In `app/mod.rs`: `pub(crate) enum UiAlert { Success(String), Warning(String), Error(String) }` with `pub fn text(&self) -> &str` (the banner reads the string through `text()`). P1 constructs only `Success` and `Error`; mark `Warning` `#[allow(dead_code)] // no P1 producer; P3-P5 raise warnings` (G2-3). The field `ui_alert: Option<UiAlert>` (was `Option<String>`).
- New fields: `ping_sent_at: Option<Instant>`, `last_tick_at: Instant`, `worker_gone: bool` (the command channel is closed: the worker thread has exited).
- In `events/mod.rs`: `pub(crate) fn health_tick(&mut self, now: Instant)`, and the constants `PING_INTERVAL = 5 s`, `WORKER_TIMEOUT = 10 s`, `UI_STALL = 2 s`. UI_STALL must stay above the idle repaint interval (G2-4). In `app/mod.rs`, add `pub(crate) const IDLE_REPAINT_SECS: u64 = 1;` and use it for the idle tick at `app/layout.rs:565`. Define `UI_STALL: Duration = Duration::from_secs(2 * IDLE_REPAINT_SECS)`. Do not write `IDLE_REPAINT * 2`: it is not a const expression. (The pre-flight put the const in `app/layout.rs`; `layout` is a private module, so `events/mod.rs` could not name it there.)
- In `validation.rs`: `strip_source_path` becomes `pub(crate)` and removes every source-path suffix (G3-6).
- In `app/mod.rs`: the field `pending_subscribes: HashSet<String>`, the keys whose Subscribe was sent and not yet answered. In `ui/topic_tree.rs`: `pub(crate) fn subscribe_enabled(&self) -> bool` on `ZenohExplorer` (K8c).
- **Later plans:** P3, P4 and P5 snippets that assign `ui_alert` must wrap the text in `UiAlert::Success`, `UiAlert::Warning` or `UiAlert::Error`; a bare `Some(String)` does not compile after this task. No later snippet is known to still assign a bare `String`: P3 (T3 `file_jobs.rs`, T12 `apply_import`, T13 `save_topic_to_file` and its test), P4 (T9 `send_transfer`) and P5 already use the variants. A snippet found later that does not is wrapped the same way.

- [ ] **Step 1: Write the failing tests** in `events/mod.rs` tests. Add `use crate::app::UiAlert;` and `use std::time::{Duration, Instant};` to the module's imports:

```rust
    fn capture_commands(app: &mut ZenohExplorer) -> std::sync::mpsc::Receiver<ZenohCommand> {
        let (tx, rx) = std::sync::mpsc::channel();
        app.command_sender = Some(tx);
        rx
    }

    #[test]
    fn operation_failure_is_an_error_alert() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, error: "bad key".into() }).unwrap();
        app.process_events();
        assert!(matches!(app.ui_alert, Some(UiAlert::Error(ref t)) if t.contains("bad key")));
        // G3-6: one strip in this arm covers every source.
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Publish, error: "k: boom at /x/y.rs:3.".into() }).unwrap();
        app.process_events();
        assert_eq!(app.publish_status, Some(PublishStatus::Failed("k: boom".into())));
    }

    #[test]
    fn ping_is_sent_once_per_interval() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0;
        for ms in [5_100u64, 5_200, 5_300, 5_400] {
            app.last_tick_at = t0 + Duration::from_millis(ms - 50);
            app.health_tick(t0 + Duration::from_millis(ms));
        }
        assert_eq!(cmds.try_iter().filter(|c| matches!(c, ZenohCommand::Ping)).count(), 1);
    }

    #[test]
    fn unanswered_ping_marks_unhealthy_after_timeout() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let _cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        let mut t = t0;
        while t < t0 + Duration::from_secs(16) {
            t += Duration::from_millis(500);
            app.last_tick_at = t - Duration::from_millis(500);
            app.health_tick(t);
        }
        assert!(!app.worker_healthy, "10 s after the first unanswered ping");
    }

    #[test]
    fn ui_stall_does_not_mark_worker_unhealthy() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let _cmds = capture_commands(&mut app);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0;
        app.health_tick(t0 + Duration::from_secs(20)); // first frame after a 20 s modal dialog
        assert!(app.worker_healthy);
    }

    #[test]
    fn no_reply_verdict_does_not_claim_absence() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        tx.send(ZenohEvent::QueryNoResponses { selector: "x/**".into() }).unwrap();
        app.process_events();
        let a = app.query_alert.unwrap();
        assert!(a.contains("No replies") && !a.contains("No queryables available") && !a.contains("Subscribe instead"), "{a}");
    }

    #[test]
    fn disconnect_cancels_waiting_query() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.query_alert = Some("Query sent for 'x/**'. Waiting for responses...".into());
        tx.send(ZenohEvent::Disconnected).unwrap();
        tx.send(ZenohEvent::QueryNoResponses { selector: "x/**".into() }).unwrap();
        app.process_events();
        assert_eq!(app.query_alert.as_deref(), Some("Query for 'x/**' cancelled: disconnected"));
    }

    #[test]
    fn pending_publish_ends_on_disconnect_or_worker_loss() {
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.publish_status = Some(PublishStatus::Sending { key: "k".into(), bytes: 1 });
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert_eq!(app.publish_status, None, "nothing will answer a put still pending at teardown");
        // A closed command channel is a dead worker, not a busy one.
        let (cmd_tx, cmd_rx) = std::sync::mpsc::channel();
        drop(cmd_rx);
        app.command_sender = Some(cmd_tx);
        let t0 = Instant::now();
        app.last_health_check = t0;
        app.last_tick_at = t0 + Duration::from_millis(4_950);
        app.publish_status = Some(PublishStatus::Sending { key: "k".into(), bytes: 1 }); // G2-5
        app.health_tick(t0 + Duration::from_secs(5));
        assert!(app.worker_gone && !app.worker_healthy);
        assert!(!matches!(app.publish_status, Some(PublishStatus::Sending { .. })), "a dead worker ends a pending publish");
    }

    #[test]
    fn subscription_rows_merge_by_id() {
        // K8c. sub_1 twice: re-declared after reconnect (T27 part a keeps its id).
        // sub_2: a second worker subscription on the same key.
        let (mut app, tx) = ZenohExplorer::test_app();
        app.pending_subscribes.insert("demo/**".into());
        for id in ["sub_1", "sub_1", "sub_2"] {
            tx.send(ZenohEvent::SubscriptionCreated { id: id.into(), key_expr: "demo/**".into() }).unwrap();
        }
        app.process_events();
        assert!(app.pending_subscribes.is_empty());
        let ids: Vec<&str> = app.subscriptions.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(ids, ["sub_1", "sub_2"], "each worker subscription keeps a removable row");
        app.pending_subscribes.insert("bad/".into());
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, error: "bad/: invalid".into() }).unwrap();
        app.process_events();
        assert!(app.pending_subscribes.is_empty(), "a failed Subscribe re-enables the button");
    }
```

  - In `ui/topic_tree.rs` tests (K8c):

```rust
    #[test]
    fn double_subscribe_is_ignored_while_pending() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.subscribe_key = "demo/**".to_string();
        assert!(app.subscribe_enabled());
        app.pending_subscribes.insert("demo/**".to_string());
        assert!(!app.subscribe_enabled(), "a second click before SubscriptionCreated does nothing");
    }
```

  - In `validation.rs` tests (G3-6):

```rust
    #[test]
    fn strip_source_path_removes_every_suffix() {
        assert_eq!(strip_source_path("k: boom at /x/y.rs:3."), "k: boom");
        assert_eq!(
            strip_source_path("k: boom at /x/y.rs:3. - Caused by inner at /a/b.rs:9."),
            "k: boom - Caused by inner"
        );
        assert_eq!(strip_source_path("retry at least once at /x/y.rs:3."), "retry at least once");
    }
```

  - In `worker/connect.rs` tests (the rest of K7). T10's `connect_error_text_has_no_source_path` must still pass.

```rust
    #[test]
    fn user_error_drops_bang_before_space() {
        // The old cut stopped at the first " at " and trimmed '!' before the spaces.
        assert_eq!(
            user_error("peer", "", "retry at least once! \n at /x/y.rs:3."),
            "Could not connect in peer mode: retry at least once"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn endpoint_parse_error_is_a_user_error() {
        // Fails while parsing the locator, before any session opens.
        let e = connect_zenoh("not-a-locator", "7447", "client", "{}")
            .await
            .err()
            .expect("an unparsable locator must fail")
            .to_string();
        assert!(e.starts_with("Could not connect in client mode: "), "{e}");
        assert!(!e.contains(".rs:"), "{e}");
    }
```

    If zenoh's `EndPoint` parser accepts `not-a-locator`, use any text it rejects.

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- operation_failure ping_is unanswered_ping ui_stall no_reply_verdict disconnect_cancels pending_publish_ends subscription_rows double_subscribe strip_source_path user_error_drops endpoint_parse_error`. Expected: compile errors (`UiAlert`, `health_tick`, `worker_gone`, `pending_subscribes`, `subscribe_enabled`). Once they compile, `strip_source_path_removes_every_suffix` fails on its second case (the old helper cuts only at the last " at "), and the two `connect.rs` tests fail.
- [ ] **Step 3: Typed alerts (F-T7-4).**
  - Add `UiAlert` to `app/mod.rs` and change the field type.
  - `events/mod.rs`: the `OperationFailed` arm sets `Some(UiAlert::Error(msg))`. T3's test `failed_queryable_unchecks_toggle` no longer compiles (`as_deref` on `Option<UiAlert>`); change its last assertion to `assert!(matches!(&app.ui_alert, Some(UiAlert::Error(t)) if t.contains("Queryable")));`.
  - One source-path strip for every error the UI shows (G3-6, K7). Zenoh's " at <path>.rs:N." suffix reaches `ui_alert`, `query_alert`, `PublishStatus::Failed` and `ConnectionStatus::Error` from publish.rs, connect.rs, session.rs and T27 parts a and b. Make `validation::strip_source_path` `pub(crate)`. It removes every ` at <token>` whose next whitespace-delimited token contains `.rs:` (the token includes its trailing `.`), and keeps all other text:

```rust
/// Drops every zenoh ` at <path>.rs:<line>.` suffix; all other text stays.
pub(crate) fn strip_source_path(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find(" at ") {
        let after = &rest[i + 4..];
        let end = after.find(char::is_whitespace).unwrap_or(after.len());
        if after[..end].contains(".rs:") {
            out.push_str(&rest[..i]);
        } else {
            out.push_str(&rest[..i + 4 + end]);
        }
        rest = &after[end..];
    }
    out.push_str(rest);
    out
}
```

    - Apply it to `error` in the `OperationFailed` arm for every op, before `msg`, `publish_status` and `query_alert` are built. Also apply it to `err` in the `ConnectionError` arm.
    - T14's `key_expr_error` tests must still pass. T27 parts a and b must not have copied the helper; Step 5d makes `connect.rs` reuse it.
  - `ui/topic_tree.rs` `save_topic_to_file`: `Some(UiAlert::Success(format!("Saved to {}", path.display())))` and `Some(UiAlert::Error(format!("Save failed: {}", e)))`.
  - `app/layout.rs` banner: replace the `starts_with('✓')` test with a match. Success shows `"Saved…"` text as is, in the existing success colour; Warning is prefixed `"Warning: "` in `ExplorerColors::WARNING`; Error is prefixed `"Error: "` in `ExplorerColors::ERROR`. Use only existing `ExplorerColors` constants; add no colour values.
- [ ] **Step 4: Worker health (F-T8-7, F-T20-8).** Move the health block at the end of `process_events` into `health_tick(Instant::now())`:

```rust
    pub(crate) fn health_tick(&mut self, now: Instant) {
        // A long gap since the last frame is the UI thread's own stall (a modal
        // dialog, a blocking read). Do not blame the worker for it.
        if now.duration_since(self.last_tick_at) > UI_STALL {
            self.last_health_check = now;
            self.ping_sent_at = None;
        }
        self.last_tick_at = now;
        if now.duration_since(self.last_health_check) >= PING_INTERVAL && self.ping_sent_at.is_none() {
            if let Some(sender) = &self.command_sender {
                if sender.send(ZenohCommand::Ping).is_err() {
                    self.worker_gone = true;
                    self.worker_healthy = false;
                    // G2-5: nothing will answer a put still pending in a dead worker.
                    if matches!(self.publish_status, Some(PublishStatus::Sending { .. })) {
                        self.publish_status = Some(PublishStatus::Failed("the worker stopped".into()));
                    }
                }
            }
            self.ping_sent_at = Some(now);
        }
        if let Some(sent) = self.ping_sent_at {
            if now.duration_since(sent) > WORKER_TIMEOUT {
                self.worker_healthy = false;
            }
        }
    }
```

  - `process_events` ends with `self.health_tick(Instant::now());` in place of the old block. Initialise `ping_sent_at: None`, `last_tick_at: Instant::now()` and `worker_gone: false` in `new()`.
  - The `Pong` arm also sets `self.ping_sent_at = None` (it already resets `last_health_check`, so the next ping waits a full interval).
  - `app/layout.rs`: when `!worker_healthy`, the label reads `format!("Worker not answering ({} s)", secs_since_ping)` whenever `worker_gone` is true, whatever `publish_status` or the connection status is. Otherwise it reads `"Worker busy: publishing"` while `publish_status` is `Some(PublishStatus::Sending { .. })`, `"Worker busy: connecting"` while connecting, and `format!("Worker not answering ({} s)", secs_since_ping)` in every other case. Leave `animate_pulse` as it is.
  - `events/mod.rs`: the `Disconnected` and `ConnectionError` arms set `publish_status = None` when it is `Some(PublishStatus::Sending { .. })`. The worker handles commands in order, so the outcome of any put sent before the disconnect arrives before `Disconnected`; a status still `Sending` here will never be answered, and without this T23's pending lock would ignore every later Publish click.
- [ ] **Step 5: Query outcomes (F-T16-1, F-T16-11).**
  - `QueryNoResponses` arm: ignore it unless `connection_status` is `Connected`. Otherwise set `format!("No replies for '{}'. No queryable matched it, or none that matched answered.", selector)`. Error replies and timeouts already arrive as `OperationFailed` (T5).
  - `Disconnected` arm: if `query_alert` starts with `"Query sent for '"`, replace it with `format!("Query for '{}' cancelled: disconnected", selector)`, where `selector` is the quoted text. Do this unconditionally, outside T12's guard on the status reset: a `Disconnected` that arrives while a new connect is under way still ended the old worker session's query.
- [ ] **Step 5b: Subscription rows by id (K8c).**
  - Precondition: T27 part a keeps a subscription's id when it is re-declared after reconnect (pre-flight G1-5): the re-declare loop in `session.rs` passes the kept id, and `cargo test -- --ignored reconnect_restores_subscriptions` passes with part a's assertion that the re-declared id equals the original. (A grep for `Some(id)` would not do: T12's test at `session.rs:271` already matches it.) If it does not, stop and report: merging by id would then leave a stale row after every reconnect.
  - `app/mod.rs`: add `pub(crate) pending_subscribes: HashSet<String>`, empty in `new()`.
  - `ui/topic_tree.rs`, Subscribe block: move the enable rule into `pub(crate) fn subscribe_enabled(&self) -> bool` in an `impl ZenohExplorer` block: connected, no `key_expr_error`, no row with this key, and the key not in `pending_subscribes` (all on `self.subscribe_key.trim()`). The button uses it. After sending `Subscribe`, insert `self.subscribe_key.trim().to_string()` into `pending_subscribes`. A second click before `SubscriptionCreated` now does nothing.
  - `events/mod.rs`, `SubscriptionCreated` arm: merge by id, not by key.

```rust
                ZenohEvent::SubscriptionCreated { id, key_expr } => {
                    self.pending_subscribes.remove(&key_expr);
                    // A row re-declared after reconnect keeps its id (T27 part a).
                    if !self.subscriptions.iter().any(|s| s.id == id) {
                        self.subscriptions.push(Subscription {
                            id,
                            key_expr,
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                    }
                }
```

  - `OperationFailed { op: FailedOp::Subscribe, .. }`, `Disconnected` and `ConnectionError` clear `pending_subscribes`. At worst this re-enables a button a moment early, and the id merge keeps any extra row removable.
- [ ] **Step 5c: Stale markers (K9, app and validation part).** Drop `#[allow(dead_code)]` on `local_kvstore` (`app/mod.rs:67`; T27 part f reads it in the Query view), `connect_started` and `connect_target` (`:85`, `:88`; read at `layout.rs:153-154`) and `port_error` (`validation.rs:53`; used at `layout.rs:50`, `:58`). If clippy reports one of them unused, keep that marker and say why in the evidence.
- [ ] **Step 5d: Worker error text at the source (the rest of K7).** T10's evidence lists these as open.
  - `user_error` reuses the shared helper and trims spaces before `!`: `let clean = crate::validation::strip_source_path(raw).trim().trim_end_matches('!').trim().to_string();`. The old code cut at the first " at ", even inside the message.
  - Route the connect-endpoint parse error (`connect.rs:255-259`) through `user_error`: `.collect::<Result<Vec<_>, _>>().map_err(|e| user_error(mode, locators, &e.to_string()))?;`.
  - `connect_zenoh_monitor` (`connect.rs:428`): return `Err(e.to_string().into())` without the `"Monitor connection failed: "` prefix, and word the timeout `"connection timeout after 15 seconds"`. The `OperationFailed` arm adds `"Monitor failed: "`, so the banner names the monitor once.
- [ ] **Step 6: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -rn "starts_with('✓')" src`. Expected: grep prints nothing. Then `grep -n 'allow(dead_code)' src/app/mod.rs src/validation.rs` prints only the `UiAlert::Warning` line, and `grep -n 'Monitor connection' src/worker/connect.rs` prints nothing. Run `cargo test -- --ignored peer_mode_unreachable_endpoint_fails monitor_sees_third_party_samples`, because `connect.rs` changed.
- [ ] **Step 7: Commit.**

```bash
git add src/app/mod.rs src/app/layout.rs src/events/mod.rs src/ui/topic_tree.rs src/validation.rs src/worker/connect.rs
git commit -m "fix(ui): typed alerts, honest worker health, truthful query verdicts"
```

---

#### T28 step 2a (was T22): Header readouts say what they measure

**Owns:** `src/app/mod.rs`, `src/app/layout.rs`, `src/events/mod.rs`

**Findings:** F-T20-5, F-T7-1 (thresholds and words; colour stays with Snow White), F-T8-5 (memory warning slot), F-T15-5 (import in the percentage), F-T20-6, F-T20-7, F-T17-9, F-T17-3 (header cause).
Pre-flight: K1 (the header clause) and the `form_locators` part of K8 (K8e). The review groups found nothing else here.

**Interfaces (in `app/mod.rs`):**
- `pub(crate) enum MemLevel { Ok, High, Critical }` with the thresholds `HIGH_PCT = 70.0` and `CRITICAL_PCT = 90.0`. One set drives both the words and the warning.
- `pub(crate) fn memory_readout(list_bytes: usize, limit_mb: usize, stored_bytes: usize) -> (String, MemLevel)`.
- `pub(crate) fn peers_text(peers: usize, routers: usize) -> String`.
- `pub(crate) fn header_error_clause(e: &str) -> String` (K1).
- New fields: `memory_alert: Option<String>`, `monitor_ok: bool`, `stored_bytes_cache: (Instant, usize)`.
- New methods on `ZenohExplorer`: `pub(crate) fn update_memory_alert(&mut self)` and `pub(crate) fn header_status_text(&self) -> String`. The old `memory_warning_shown` field is deleted.

- [ ] **Step 1: Write the failing tests** in `app/mod.rs`:

```rust
#[cfg(test)]
mod readout_tests {
    use super::*;

    #[test]
    fn memory_readout_names_scope_and_counts_stored_payloads() {
        let (text, level) = memory_readout(1024 * 1024, 100, 3 * 1024 * 1024 * 1024);
        assert!(text.contains("History 1.0 MB / 100 MB"), "{text}");
        assert!(text.contains("Stored payloads 3.00 GB"), "{text}"); // transfer::format_size wording
        assert!(matches!(level, MemLevel::Ok));
    }

    #[test]
    fn memory_level_uses_one_threshold_set() {
        let mb = 1024 * 1024;
        assert!(matches!(memory_readout(69 * mb, 100, 0).1, MemLevel::Ok));
        let (t, l) = memory_readout(70 * mb, 100, 0);
        assert!(matches!(l, MemLevel::High) && t.contains("(high)"), "{t}");
        let (t, l) = memory_readout(95 * mb, 100, 0);
        assert!(matches!(l, MemLevel::Critical) && t.contains("(critical)"), "{t}");
    }

    #[test]
    fn memory_warning_does_not_touch_query_alert() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.current_memory_bytes = 85 * 1024 * 1024;
        app.import_memory_bytes = 500 * 1024 * 1024; // staged import: not history
        app.update_memory_alert();
        assert!(app.query_alert.is_none());
        assert!(app.memory_alert.as_deref().unwrap_or("").contains("85"));
    }

    #[test]
    fn peer_count_is_worded_and_shown_at_zero() {
        assert_eq!(peers_text(0, 0), "no peers");
        assert_eq!(peers_text(1, 0), "1 peer");
        assert_eq!(peers_text(1, 2), "2 routers · 1 peer");
    }

    #[test]
    fn monitor_failure_shows_in_header() {
        let (mut app, tx) = ZenohExplorer::test_app();
        tx.send(ZenohEvent::PublishingConnected).unwrap();
        tx.send(ZenohEvent::OperationFailed { op: FailedOp::Monitor, error: "x".into() }).unwrap();
        tx.send(ZenohEvent::MonitorConnected).unwrap();
        app.process_events();
        assert!(app.header_status_text().contains("monitor off"));
    }

    #[test]
    fn header_error_clause_keeps_first_clause() {
        // K1: the three connect texts, with an IP locator that must not be cut at its dots.
        assert_eq!(
            header_error_clause("Could not connect in client mode: Unable to connect to any of [tcp/10.0.0.5:7447]"),
            "Could not connect in client mode"
        );
        assert_eq!(
            header_error_clause("Client mode needs a router address. Enter one, or switch Mode to Peer. (No peer specified)"),
            "Client mode needs a router address"
        );
        assert_eq!(
            header_error_clause("Connection timeout in client mode: Unable to establish connection within 30 seconds"),
            "Connection timeout in client mode"
        );
        assert_eq!(
            header_error_clause("abcdefghij abcdefghij abcdefghij abcdefghij abc"),
            "abcdefghij abcdefghij abcdefghij…"
        );
    }
}
```

  - In `app/layout.rs` tests (K8e):

```rust
    #[test]
    fn form_locators_trim_inputs() {
        assert_eq!(form_locators("tcp", " localhost ", " 7447"), "tcp/localhost:7447");
        assert_eq!(form_locators("tcp", "  ", "7447"), "");
        assert_eq!(locator_preview("client", "tcp", " ", "7447"), "needs an address");
        assert_eq!(connect_port_error(" ", "abc"), None, "Port is unused without an address");
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- readout_tests form_locators_trim`. Expected: compile errors, then `form_locators_trim_inputs` fails (the form does not trim).
- [ ] **Step 3: Implement.**
  - `memory_readout`: percentage = `list_bytes / (limit_mb MiB)`, capped at 100. Text: `format!("History {:.1} MB / {} MB{}", …)`, with the suffix `" (high)"` or `" (critical)"` by level, then `" · Stored payloads {}"` via `transfer::format_size` when `stored_bytes > 0`. Imports are **not** part of either figure. They keep their own `"Staged import {size}"` label, shown only while `import_memory_bytes > 0` (F-T15-5).
  - `update_memory_alert(&mut self)`, called from the header: at `MemLevel::High` or above, set `memory_alert = Some(format!("History is {pct:.0}% of its limit: the oldest rows will leave the list (they stay in the tree)"))`; below `HIGH_PCT`, set it to `None`. Delete the old write to `query_alert` (F-T8-5). Draw `memory_alert` as a label beside the readout.
  - Stored bytes: refresh `stored_bytes_cache` at most once a second, by summing `e.bytes.len()` over `payload_store` under its read lock. Show the readout when `self.current_memory_bytes > 0 || self.stored_bytes_cache.1 > 0 || self.import_memory_bytes > 0` (not `!self.messages.is_empty()`), so a staged import with an empty history still shows its label (F-T20-5, F-T15-5).
  - The drop label: replace T3's `"dropped"` with `"trimmed from list"`, giving `"({} trimmed from list, {} not listed (rate), {} pipeline)"` (F-T20-6).
  - Peers: always show `peers_text(..)` while connected, including `"no peers"`, with `.on_hover_text("Zenoh peers and routers the publishing session is linked to")` (F-T17-9, F-T20-7).
  - Monitor: `PublishingConnected` sets `monitor_ok = true`, and `OperationFailed { op: FailedOp::Monitor, .. }` sets it to false. `header_status_text()` returns `"Connected · monitor off"` when connected and `!monitor_ok`, with the hover `"The background ** monitor could not start, so only your subscriptions fill the tree"` (F-T20-7). After T10 the flag is false only when the client-mode monitor failed to open or to declare `**`. A running monitor receives whatever reaches this app, so the hover names the failure instead of claiming the monitor sees nothing.
  - An `Error(e)` status shows `"Error: "` plus the first clause of `e` in the header (F-T17-3). The clause ends at the first `": "`, `". "` or `" ("`, each including the space, so `tcp/10.0.0.5:7447` is not cut. If the result is still over 40 chars, cut at the last space before 40 and add `"…"` (K1). Put this in the pure `fn header_error_clause(e: &str) -> String` in `app/mod.rs`:

```rust
/// The header's short form of a connection error: its first clause.
pub(crate) fn header_error_clause(e: &str) -> String {
    let cut = [": ", ". ", " ("].iter().filter_map(|s| e.find(s)).min().unwrap_or(e.len());
    let clause = e[..cut].trim_end_matches('.');
    if clause.chars().count() <= 40 {
        return clause.to_string();
    }
    let head: String = clause.chars().take(40).collect();
    match head.rfind(' ') {
        Some(i) => format!("{}…", &head[..i]),
        None => format!("{head}…"),
    }
}
```

  - Trim transport, address and port in `form_locators` and `locator_preview`, and test `address.trim().is_empty()` there and in `connect_port_error` (K8e). An input of `" 7447"` passes the port check, so without the trim Connect sends `tcp/host: 7447` and zenoh fails with a raw parse error.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings`.
- [ ] **Step 5: Commit.**

```bash
git add src/app/mod.rs src/app/layout.rs src/events/mod.rs
git commit -m "fix(ui): header readouts name their scope; memory warning gets its own field"
```

---

#### T28 step 2b (was T24): Topic details and tree rows tell the truth

**Owns:** `src/ui/topic_tree.rs`, `src/types/tree.rs`

**Findings:** F-T20-1, F-T20-2 (topic side), F-T14-5, F-T20-11, F-T13-13 (history side), F-T13-14, F-T13-4, F-T7-7 (tree side), F-T14-6 and F-T20-10 (history cards).
Pre-flight: G2-1 (K3, keep T19's scan-window wording), G2-2 (singular forms, one topic count), G3-12 (replies stay out of History), G2-6 (local source time) and K4 (repaint after the filter throttle).

**Interfaces (in `types/tree.rs`):**
- `pub struct SubtreeSummary { pub topics: usize, pub messages: usize, pub last_seen: Option<Instant> }`.
- `impl ZenohNode { pub fn subtree_summary(&self) -> SubtreeSummary }`.
- `update_data` sets `is_local` from the current message.
- `pub fn filter_repaint_after(cached: Option<(&str, u64, Instant)>, filter: &str, version: u64, now: Instant) -> Option<Duration>` (K4).

- [ ] **Step 1: Write the failing tests.**
  - In `tree.rs` tests:

```rust
    #[test]
    fn branch_summary_counts_subtree() {
        let mut root = ZenohNode::new("root".into());
        for (p, n) in [("demo/bin/blob", 3), ("demo/bin/x", 2)] {
            let leaf = root.insert_path(p);
            for _ in 0..n {
                leaf.update_data("v".into(), "text/plain".into(), false, SampleKindView::Put, None);
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
        n.update_data("mine".into(), "text/plain".into(), true, SampleKindView::Put, None);
        assert!(n.is_local);
        n.update_data("theirs".into(), "text/plain".into(), false, SampleKindView::Put, None);
        assert!(!n.is_local, "a remote value replaced ours");
    }

    #[test]
    fn filter_throttle_schedules_repaint() {
        let t0 = Instant::now();
        let ms = Duration::from_millis;
        // K4: kept only by the throttle, so repaint when the interval ends.
        assert_eq!(filter_repaint_after(Some(("a", 1, t0)), "a", 2, t0 + ms(100)), Some(ms(150)));
        // Up to date, due for a recompute, or a new filter: nothing to schedule.
        assert_eq!(filter_repaint_after(Some(("a", 2, t0)), "a", 2, t0 + ms(100)), None);
        assert_eq!(filter_repaint_after(Some(("a", 1, t0)), "a", 2, t0 + ms(300)), None);
        assert_eq!(filter_repaint_after(Some(("a", 1, t0)), "b", 2, t0 + ms(100)), None);
    }
```

    If `insert_path` has a different name or return type after P1 T2, use the tree's own insertion helper.
  - In `topic_tree.rs`, append to the existing `#[cfg(test)] mod tests` (the file already has one; a second `mod tests` is error E0428):

```rust
    #[test]
    fn history_empty_reason_rules() {
        assert_eq!(history_empty_reason(0, false), "No messages on this exact key yet");
        assert_eq!(history_empty_reason(360, false), "Not in the list: cleared, trimmed, rate-limited or received while paused (the count above keeps them)");
        assert_eq!(history_empty_reason(360, true), "Paused: new messages for this topic are not listed");
    }

    #[test]
    fn count_hover_names_unit() {
        assert_eq!(count_hover(true, 6), "6 leaf topics below");
        assert_eq!(count_hover(false, 360), "360 messages received");
        assert_eq!(count_hover(true, 1), "1 leaf topic below");
        assert_eq!(count_hover(false, 1), "1 message received");
    }

    #[test]
    fn history_excludes_query_replies() {
        // G3-12: a reply for the selected key belongs to Query Results, not History.
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.browse_tree.write().unwrap().insert_path("r/a").update_data(
            "sub-val".to_string(), "text/plain".to_string(), false, SampleKindView::Put, None,
        );
        let mk = |p: &str, t: MessageType| {
            ZenohMessage::new_with_bytes(
                "r/a".to_string(), p.to_string(), p.as_bytes().to_vec(), "text/plain".to_string(),
                chrono::Utc::now(), t, false, MessageSource::MonitorSession,
            )
        };
        app.messages.push_back(mk("sub-val", MessageType::Subscribe));
        app.messages.push_back(mk("reply-val", MessageType::QueryReply));
        app.selected_topic = Some("r/a".to_string());
        let texts = details_texts(&mut app);
        assert!(!texts.iter().any(|t| t.contains("reply-val")), "{texts:?}");
    }
```

  - Existing tests that pin old wording change with it:
    - T19's `history_names_the_scan_window_instead_of_claiming_empty`: change `t == "No messages yet"` (`topic_tree.rs:1045`) to `*t == history_empty_reason(1, false)` (G2-1). Its scan-window assertions stay as they are.
    - T15's `topic_details_show_delete_and_source_time`: compare against `format_local_time(&ts, &chrono::Utc::now())` instead of `"2026-09-25T12:00:00.123Z"` (`topic_tree.rs:1000`) (G2-6).

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- branch_summary local_marker history_empty count_hover filter_throttle history_excludes`. Expected: compile errors.
- [ ] **Step 3: Implement.**
  - `tree.rs`: add `subtree_summary` as a recursive walk over the descendants (sum `message_count`; `topics` counts descendants with `message_count > 0`; `last_seen` is the latest `last_seen` among them, `None` when none has data). In `update_data`, set `self.is_local = is_local;` in place of "local publications take precedence" (F-T20-11).
  - Details header (F-T20-1, F-T14-5, F-T20-2):
    - When the selected node has children and its own `message_count == 0`, show a branch summary instead of the leaf page: `"{topics} topics below · {messages} messages received · last message {age} ago"`, each part singular at 1 (`1 topic below · 1 message received`) (G2-2), followed by the child keys. Hide Pause and Save for it.
    - For a node with both data and children, show the leaf page plus that summary line.
    - Rename the figure to `"Received: {n} (since app start)"`. The tree is created once in `new()` and never cleared, and T12 keeps it across Disconnect and reconnect, so the count spans connects.
    - Pause: the button reads `"⏸ Pause list"` / `"▶ Resume list"`, the tooltip reads `"Stop adding this topic's messages to the lists; its value and count keep updating"`, and the state label reads `"Paused (lists only)"`.
  - Message History (F-T13-13, F-T20-2, F-T14-6, F-T20-10):
    - `egui::ScrollArea::vertical().id_salt(("history", topic.as_str()))`.
    - When the history is empty: if T19's `history_scan_note(..)` is `Some`, keep T19's heading `No messages in the scanned rows` and its note unchanged. Otherwise the heading is `history_empty_reason(message_count, paused)`, and the `Waiting for messages on this topic...` line is dropped (G2-1, K3).
    - When `message_count > shown`, add `"Showing the newest {shown} of {message_count}"` under the heading (G2-2).
    - History cards and the shown count exclude `MessageType::QueryReply` rows: they belong to Query Results. The history filter becomes `m.key == *topic && m.message_type != MessageType::QueryReply`, so `shown` and `message_count` count the same samples (G3-12).
    - Card times use `format_local_time`. When `source_timestamp` is `Some`, add `"· source {time}"`, and the column hover reads `"Received time, local"`. The details `Source time:` row (`topic_tree.rs:556-561`) also uses `format_local_time(&ts, &chrono::Utc::now())` (G2-6).
  - Tree rows (F-T13-4, F-T7-7, F-T13-14):
    - The count label gets `.on_hover_text(count_hover(is_branch, n))`: for a branch `n` is `cumulative_leaves`, worded `1 leaf topic below` / `{n} leaf topics below`; for a leaf, `1 message received` / `{n} messages received` (G2-2).
    - A branch whose own `message_count > 0` shows `"({n})"` after its name.
    - A row whose key is in `paused_keys` shows `"⏸ paused"` after its name (glyph and word, no colour).
    - The empty-tree hint reads `"💡 Try demo/** or sensor/* in Subscribe to Topics above"`.
    - The italic line above the hint changes from `"Subscribe to key expressions to see network activity"` to `"Topics appear here as this app receives data"`. After T10 the `**` monitor fills the tree without a subscription, so a subscription is no longer the only way to see activity. The new line holds while disconnected, while the monitor runs, and while it is off (T22's "monitor off").
    - Filter repaint (K4): after traffic stops, the filtered tree could stay stale until the 1 s idle tick. When the filter cache is kept only because of the throttle (same filter, different `tree_version`), call `ui.ctx().request_repaint_after(d)` with `d` from `filter_repaint_after`:

```rust
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
```

  - `plus_minus_icon` and the expander are untouched.
- [ ] **Step 4: Verify.** Run `cargo test && cargo clippy --all-targets -- -D warnings && grep -n 'id_salt(("history"' src/ui/topic_tree.rs && ! grep -n 'Subscribe tab' src/ui/topic_tree.rs && grep -n 'filter_repaint_after' src/ui/topic_tree.rs`.
- [ ] **Step 5: Commit.**

```bash
git add src/ui/topic_tree.rs src/types/tree.rs
git commit -m "fix(ui): branch summaries, truthful counts, pause scope and per-topic history scroll"
```

---

#### T28 step 2c (new): Spawned worker tasks never block on a full pipeline

**Owns:** `src/worker/session.rs`, `src/worker/pipeline.rs`, `src/worker/query.rs`, `src/worker/queryable.rs`

**Findings:** pre-flight K8d, which it proposed as the follow-up "Non-blocking worker event sends", scoped to `session.rs:49-53`, T5's reply task and T6's `serve_queryable`. It is folded in here at that scope. The files are free once T27 parts a and b land, and steps 2a and 2b do not touch them. T11's and T12's evidence name the same blocking sends.

**Scope and order.** Only the three places that run on tokio worker threads change. The command loop's sends (`handle_connect`, `handle_disconnect`, `handle_subscribe`, `handle_publish` and the others) stay blocking and keep their order. They run on the worker's `block_on` thread, where a full pipeline is back-pressure that step 1's "Worker not answering" readout reports. No event is retried later:
- A `DiscoveryUpdate` that does not fit is dropped, and the next one comes 2 s later. The task can no longer hang inside a send, where teardown's `abort` cannot reach it.
- A reply task or queryable event that does not fit waits on the blocking pool, and its task awaits it before going on. The order within each task is unchanged. Against other senders these events were never ordered, and step 1 already handles a late one: `QueryNoResponses` is ignored unless connected, and `Disconnected` cancels a waiting query.

**Interfaces:** `pub(crate) async fn send_event(tx: &EventTx, event: ZenohEvent) -> bool` in `pipeline.rs`.

- [ ] **Step 1: Write the failing test** in `pipeline.rs` tests:

```rust
    #[tokio::test(flavor = "current_thread")]
    async fn send_event_does_not_block_the_runtime() {
        use std::time::{Duration, Instant};
        let (tx, rx) = event_channel(1);
        tx.try_send(ZenohEvent::Pong).unwrap(); // the pipeline is full
        let sender = tokio::spawn({
            let tx = tx.clone();
            async move { send_event(&tx, ZenohEvent::Pong).await }
        });
        let drain = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(300));
            (rx.recv().is_ok(), rx.recv().is_ok())
        });
        // The runtime's only thread stays free while the send waits for room.
        let start = Instant::now();
        for _ in 0..5 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(start.elapsed() < Duration::from_millis(200), "the runtime thread was blocked");
        assert!(sender.await.unwrap());
        assert_eq!(drain.join().unwrap(), (true, true));
    }
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test send_event_does_not_block`. Expected: compile error (`send_event`). A version that calls `tx.send` directly compiles but fails the elapsed-time assertion.
- [ ] **Step 3: Implement.**
  - In `pipeline.rs`:

```rust
/// Send from async code without blocking a runtime thread. When the pipeline
/// is full, the wait moves to the blocking pool. False when the UI side is gone.
pub(crate) async fn send_event(tx: &EventTx, event: ZenohEvent) -> bool {
    match tx.try_send(event) {
        Ok(()) => true,
        Err(std::sync::mpsc::TrySendError::Full(event)) => {
            let tx = tx.clone();
            tokio::task::spawn_blocking(move || tx.send(event).is_ok())
                .await
                .unwrap_or(false)
        }
        Err(std::sync::mpsc::TrySendError::Disconnected(_)) => false,
    }
}
```

  - `session.rs`, discovery task (`:51-56`): use `try_send`. `Ok(())` and `Full(_)` continue; `Disconnected(_)` breaks the loop.
  - `query.rs` and `queryable.rs`: every event send becomes `send_event(&tx, event).await`. This covers the reply task's `QueryNoResponses` and `OperationFailed` (T27 part a) and `serve_queryable`'s declare failure (T27 part b). The sends in `handle_query` and `handle_enable` change too: they await the send, so their order holds, and one grep then checks both files. Samples keep `send_sample`, which already drops and counts.
- [ ] **Step 4: Verify.** Run:

```bash
cargo test && cargo test -- --ignored && cargo clippy --all-targets -- -D warnings
grep -n '\.send(ZenohEvent' src/worker/query.rs src/worker/queryable.rs  # prints nothing
grep -n 'try_send(ZenohEvent::DiscoveryUpdate' src/worker/session.rs     # matches
```

  The ignored tests cover what this step touches: T12's `reconnect_then_disconnect_leaves_no_discovery_updates` and `reconnect_restores_subscriptions`, T5's `invalid_selector_reports_query_failure`, and T10's two connect tests.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/session.rs src/worker/pipeline.rs src/worker/query.rs src/worker/queryable.rs
git commit -m "fix(worker): spawned tasks never block a runtime thread on a full pipeline"
```

---

#### T28 step 3 (was T26): Help matches behaviour

**Owns:** `src/ui/help.rs`

**Findings:** F-T18-1 to F-T18-7, F-T19-3. It is written after T12, T17, T22, T23 and T24, so it describes behaviour as P1 leaves it (T24's branch summary and T23's queryable caption included). P4 T11 and P5 T24 later edit their own sections of this file.
Pre-flight: G4-1, G4-2, G4-3 (with G3-9's wording), G4-4, G4-5 and the G1-1 follow-on. The help text also states T5 and T6 behaviour (a timeout with no replies is an error; the queryable's `**` scope). Both land in T27 parts a and b, which T28 depends on (G4-5).

**Interfaces:** `pub(crate) const HELP_SECTIONS: &[(&str, &[&str])]` holds every heading and its lines, and `show_help_tab` renders it inside a `ScrollArea`. Keeping the text in data makes it testable.

- [ ] **Step 1: Write the failing tests** in `help.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn all_text() -> String {
        HELP_SECTIONS.iter().flat_map(|(h, ls)| std::iter::once(*h).chain(ls.iter().copied())).collect::<Vec<_>>().join("\n")
    }

    #[test]
    fn help_names_only_real_places() {
        let t = all_text();
        for gone in ["Subscribe tab", "Browse tab", "Messages tab"] {
            assert!(!t.contains(gone), "Help names a place that does not exist: {gone}");
        }
        assert!(t.contains("Subscribe to Topics"));
        assert!(t.contains(". Query: "), "Help has a Query step"); // G4-4: "Query" alone also matches "Queryable"
        assert!(t.contains("Troubleshooting"));
    }

    #[test]
    fn help_claims_match_limits() {
        let t = all_text();
        // The last two are false after T10: no second port, and the ** monitor fills the tree unsubscribed.
        for false_claim in ["any size", "greater than 10MB", "items in keyspace", "Match all keys", "dropped when limits", "Listen Port + 1000", "until you do"] {
            assert!(!t.contains(false_claim), "{false_claim}");
        }
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::help`. Expected: compile error (`HELP_SECTIONS`).
- [ ] **Step 3: Rewrite the text** as `HELP_SECTIONS` in screen order (F-T18-7). Each line below is one label.
  - **"What it is"**: "Watch, publish and query data on a Zenoh network."
  - **"Getting started"**:
    - "1. Connect: Peer (the default) finds other peers on the local network by multicast (UDP 7446). Listen Port is where other peers reach this app. Use a different Listen Port for each copy on one machine. Address is optional."
    - "   Client connects to a router: enter its address (for example localhost) and port (7447)."
    - "   Tested with tcp and multicast; other transports are offered but untested."
    - "2. Once connected, a background ** monitor adds every key this app receives to the tree. Subscribe to Topics, above the tree, adds a subscription of your own, such as demo/**; you need one when the header says \"monitor off\"."
    - "3. The topic tree on the left fills as messages arrive. Select a topic for its value and history; select a branch for a summary of what is below it."
    - "4. All Messages (Topics view with no topic selected) lists recent messages this app received or published, including query replies, and holds the Memory, Message and Rate Limit fields. New messages on paused topics (except query replies) and file chunks are not listed." (G4-3)
    - "5. Publish: send text, or import a file (it is read into memory)."
    - "6. Query: ask queryables for values. Results show each reply; a query with no match ends at once."
    - "7. Queryable (Publish tab): answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imports)."
  - **"Key expressions"**: "** : every key except @ admin keys"; "demo/** : every key under demo/"; "sensor/*/temperature : one level in the middle"; "device/1/status : exactly this key"; "Keys have no empty levels (no //, and no / at the start or end); * and ** fill a whole level."
  - **"Limits"** (F-T18-5):
    - "History keeps rows up to the Memory Limit (default 100 MB) and the Message Limit; older rows leave the list but stay in the tree and its counts."
    - "Messages over the Rate Limit are not listed, but the tree and Save still see them."
    - "Duplicates: the same sample seen by two sessions, or by two overlapping subscriptions, within 250 ms is listed once." (X1)
    - "Lists show the start of each value (about 200 bytes; Query Results 500); the topic's Current Value shows up to 10 KB; Save File writes all of it." (G4-3)
  - **"Troubleshooting"** (F-T18-6):
    - "Connected but the tree stays empty: check the peer count in the header (\"no peers\" means no Zenoh peer or router is linked to this app; apps in client mode that dial this app are not counted). If the header says \"monitor off\", subscribe (step 2)."
    - "Connection error: the red message in the connection panel, above the Connect button, names the cause (the header shows its first words); in Client mode an address is required." (G4-2)
    - "A button is disabled: invalid input is named under its field. Otherwise the app is not connected (Publish and Query say so at the top; Subscribe needs a connection too), or that key is already subscribed. Save File's hover says why it is unavailable." (G4-1)
    - "A query says \"No replies\": no queryable matched, or those that matched had nothing to return. A timeout with no replies is shown as an error." (G1-1 follow-on)
    - "After reconnecting, your subscriptions are re-declared automatically."
  - Render: `egui::ScrollArea::vertical().id_salt("help").auto_shrink([false; 2]).show(ui, |ui| { … })`, with each heading as `RichText::new(h).strong()` (F-T19-3).
- [ ] **Step 4: Verify.** Run `cargo test ui::help && cargo clippy --all-targets -- -D warnings`. By hand at 1000×600, while disconnected: scroll Help to its last line (the user does this in step 4's smoke run).
- [ ] **Step 5: Commit.**

```bash
git add src/ui/help.rs
git commit -m "fix(help): describe the app as it behaves; add Query, Limits and Troubleshooting"
```

---

#### T28 step 4 (was T20): Integration verification

**Owns:** Step 0 owns `src/types/message.rs`, `src/types/commands.rs`, `src/types/store.rs`, `src/types/limits.rs`, `src/payload.rs`, `src/worker/samples.rs` and `src/worker/publish.rs`, for dead-code markers, test session config and comments only. Steps 1-5 own no source files. If a check fails in a file T28 owns, a fix agent that owns only that file fixes it in its own commit, and step 4 runs again from Step 1. If it fails in a file T28 does not own, stop and block T28 with the output; do not edit that file.

- [ ] **Step 0: Stale markers and in-process test sessions (K9 rest, K10).** One agent. This was the pre-flight's proposed cleanup follow-up; it is folded in here because it touches many files and must see the code of T27 and steps 1-3 to tell what is still dead. It runs on the T28 branch after step 3 has committed, never beside it, so step 3's cargo checks never see half-done edits and the two agents never share the git index.
  - Remove each `#[allow(dead_code)]` whose item now has a non-test use. At `2d666c1` they were: `src/types/message.rs` (`SampleKindView::Delete`, `ZenohMessage::source`, `with_sample_meta`, `PublishStatus::Sending`, `format_local_time`), `src/types/commands.rs` (`FailedOp`, `ZenohEvent::OperationFailed`, `ZenohEvent::Published`), `src/types/store.rs` (`received_at`, `ActiveSubscription::key_expr`) and `src/payload.rs` (`HEX_PREVIEW_BYTES`, `preview`). T27 part a removes some of them and may move others; find what is left with `grep -rn 'allow(dead_code)' src`. After the removals, run `cargo clippy --all-targets -- -D warnings`. If clippy reports an item unused, put its marker back with a comment that names the reader it waits for, and record it in the evidence.
  - Keep these markers: `MessageType::Query`, `Subscription::{reliability, mode}` and `ZenohCommand::Subscribe { reliability, mode }` (not implemented yet), `ExplorerColors` and `card_background_color` (`colors.rs` and `app/theme.rs` belong to the Snow White plan), and step 1's `UiAlert::Warning`.
  - K10: a default peer listens on `tcp/[::]:0`, and these tests are not `#[ignore]`. In `samples.rs` `local_session()` (`:55-60`) and in `publish.rs` `failed_put_is_not_echoed_or_stored` (`:218-223`), add `c.insert_json5("listen/endpoints", "[]").unwrap(); // in-process only: no bound port` after the scouting line. The sessions talk only to themselves, so the tests still pass.
  - Reword the comments at `samples.rs:27-28` and `:100-103`: a query reply's attachment is other metadata, never a filename. After T27 part b the queryable attaches nothing, so the comments no longer name `source:local`.
  - Reword the doc comments at `limits.rs:10` and `:44`, which say a duplicate is the same sample "observed by two sessions". After T27 part a (X1) it is the same sample from two sources: two sessions, or two overlapping subscriptions of one session. Change no code in `limits.rs`.
  - Verify: `cargo build && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check`, then `grep -rn 'allow(dead_code)' src`. Expected: only the kept markers above.
  - Commit:

```bash
git add src/types/message.rs src/types/commands.rs src/types/store.rs src/types/limits.rs src/payload.rs src/worker/samples.rs src/worker/publish.rs
git commit -m "chore: drop stale dead-code markers and comments; in-process test sessions bind no port"
```

- [ ] **Step 1: Full suite.**

```bash
cargo build --locked && cargo test --locked && cargo test --locked -- --ignored \
  && cargo clippy --all-targets --locked -- -D warnings && cargo fmt --all -- --check && cargo audit
```

Expected: all pass. Audit reports 0 vulnerabilities, with only the two ignored advisories listed.
- [ ] **Step 2: Residue greps.** Every command must print nothing:

```bash
grep -rn '"text/plain".to_string()' src/worker
grep -rn 'set_max_message_size' src
grep -rn 'MAX_HASH_BYTES\|seen_recently' src
grep -rn 'mpsc::channel()' src/app | grep -v 'command\|let (tx, rx)'
# G4-7: the rest of P1's residue
grep -rn "starts_with('✓')" src
grep -rn 'use Export' src
grep -rn 'monitor_port' src/worker
grep -n 'source:local' src/worker/query.rs src/worker/queryable.rs
grep -n 'tree.clone()' src/ui/topic_tree.rs
grep -n 'Subscribe tab' src/ui/topic_tree.rs
grep -n '100 \* 1024 \* 1024' src/worker/publish.rs
# T28 steps 1 and 2c
grep -n 'Monitor connection' src/worker/connect.rs
grep -n '\.send(ZenohEvent' src/worker/query.rs src/worker/queryable.rs
```

- [ ] **Step 3: Idle CPU.** Steps 3 and 4 are run by the user; agents record what the user reports (G4-6). Run `grep -rn 'from_millis(66)' src/app`; it prints only the unhealthy-worker pulse in layout.rs (G4-7). Measure idle CPU as in T13 Step 5 and paste the readings. Connect with the Connect button: T13's readings sent Connect from a scratch hook.
- [ ] **Step 4: Smoke run.** Connect in peer mode, subscribe `demo/**`, publish text, JSON (encoding `application/json`) and an imported binary file, query `demo/**` with the queryable enabled, then disconnect and reconnect. Record what you observe for each step in the evidence.
  - Monitor check (T10, F-T20-7): before subscribing to anything, start a second copy of the app with Listen Port 7448 and publish `demo/m` from it. The first copy's tree shows `demo/m` and its header shows no "monitor off". On a network with no other Zenoh peers its peer count reads `1 peer`, because the monitor is not counted.
  - UI-review checks (T21–T26 and the extended tasks): type `demo//x` as the publish key (inline error and Publish disabled, per T14; no leaf); publish a valid value twice (the status line shows `Published …` after each of the two publishes, and the value stays in the field); disconnect with `demo/**` subscribed, reconnect, and see it re-declared; open a branch such as `demo/bin` (subtree summary, not "No messages yet"); open Help at 1000×600 and scroll to the last line.
  - Checks earlier tasks left for this run (G4-6). Record each one:
    - T2: the smoke run above covers Step 6 (connect in peer mode, publish `demo/test`, see it in the tree).
    - T10: in peer mode, Listen Port `0`, `abc` and `70000` each show an error and disable Connect. Peer mode with Address `10.255.255.1` reports an error after about 10 s. The monitor check above covers third-party traffic.
    - T12: while connecting, the header reads `Connecting to … n s` and Disconnect is disabled. After a connect error, changing Listen Port (or Port, when an Address is set) clears it. With one subscription kept, the panel reads `1 subscription resumes when you reconnect`.
    - T13: Step 3 above. The no-mouse tree update is already recorded in `24aa80c`; do not repeat it.
    - T14: an invalid key (`demo/`) in Subscribe to Topics, the Query selector and the Queryable Key Pattern each shows an inline error and disables its control.
    - T15: `z_put`, then `z_delete demo/x` from a second terminal (zenoh examples). Topic Details shows `Last sample: DELETE`. This can pass now that T4 carries the sample kind.
    - T23 (T27 part f): ticking Enable Queryable locks Key Pattern, and Query shows the queryable line, in the singular for one stored value. The UI-review checks above cover its publish checks.
    - T26 (step 3): the Help check in the UI-review list above.
    - T28's own: in client mode with no address, the header reads `Error: Client mode needs a router address` (K1). With an Address set, a Port of ` 7447` (leading space) connects to `tcp/<address>:7447` (K8e). A fast double click on Subscribe adds one row (K8c).
- [ ] **Step 5: Evidence.** Paste the Step 1 summaries, the Step 2 greps and the Step 4 observations into T28's completion evidence. Step 0 is this step's only commit.
