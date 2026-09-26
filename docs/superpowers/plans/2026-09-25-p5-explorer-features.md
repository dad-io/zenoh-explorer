# P5 · Explorer Features Implementation Plan (concurrent lanes)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Only modify the files listed under your task's "Owns".** Another task may be running at the same time on every other file.

**Goal:** Give Zenoh Explorer the inspection features that competing Zenoh and MQTT tools have and it lacks: an admin-space browser, a live topology view, a liveliness browser, QoS and options on Publish and Query, a multi-format payload viewer, key-expression tree filtering, connection profiles, keyboard control, an in-app log view and per-topic rates.

**Architecture:**
- **Threads stay as they are.** The UI thread, the batching buffer thread and the tokio worker thread that owns the two Zenoh sessions.
- **One feature, one set of files.** Each feature gets:
  - a worker module (`src/worker/<feature>.rs`) that turns one `ZenohCommand` into `ZenohEvent`s;
  - a pure model module (`src/<feature>_model.rs` or similar), which is unit-tested without egui or zenoh;
  - a UI module (`src/ui/<feature>.rs`) that owns its state struct and its tab.
- **Wave 0 (T1) is the only task that touches shared files.** It declares every new command, event, option type, tab, app field, worker-state field and dispatch arm, and it creates each feature file as a compiling stub. After T1, the lanes own disjoint files.
- **Admin data has its own path.** `**` never matches a chunk that starts with `@`, so the monitor's `**` subscription cannot see the admin space. Admin data arrives only through explicit `@/…` queries (`ZenohCommand::AdminQuery`), and it is shown in its own tab and tree. It is never merged into the topic tree.

**Tech Stack:**
- Rust 2021, MSRV set by P3 (egui 0.36.2 needs rustc ≥ 1.95).
- zenoh 1.10.1 with the `unstable` feature, plus zenoh-ext 1.10.1.
- egui / eframe / egui_extras / egui_kittest 0.36.2, and rfd 0.17.
- tokio, tracing-subscriber 0.3.
- New crates: ciborium 0.2 and image 0.25 (png and jpeg only).

**Spec:** the P5 brief. It covers eleven features in priority order:
1. admin space
2. topology
3. liveliness
4. QoS on Publish
5. query options
6. payload viewer
7. key-expression filter
8. connection profiles
9. keyboard shortcuts
10. logs tab
11. rates

It also carries the deferred list of `docs/superpowers/reviews/2026-09-25-zenoh-explorer-deep-review.md` ("New explorer features: admin space, topology, liveliness, QoS controls, decoders").

**Programme:** P5 of five, and it runs last:
- P1 correctness: `2026-09-25-correctness-and-hardening.md`.
- P2 CI and release.
- P3 egui 0.36 port: rfd 0.17, the egui_kittest harness, tree virtualization with cached rows, eframe persistence `Settings` and accesskit labels.
- P4 transfer protocol v2: the `@xfer` namespace and `TransferRegistry`.

**Decisions:** none recorded (`docs/memex` has 0). **Kind of change:** new user-facing features. There are no protocol changes and no theme changes.

## Baseline this plan assumes

This plan is written against the tree as it will be **after P1–P4**. Line numbers quoted as "pre-P1" refer to the current `bearhug-mode-test` tree (commit `cf9fb6c`). P1–P4 will move that code, so always locate code by the **function or item name** given next to the line number.

- **Worker** (P1 T2/T3/T12):
  - `src/worker/mod.rs` holds the dispatch loop `zenoh_worker(command_receiver, event_sender: EventTx, local_kvstore, sample_drops)`.
  - `src/worker/state.rs` holds `WorkerState { publishing_session, monitor_session, active_subscriptions, monitor_subscription, queryable_task, discovery_task }` with `async fn teardown(&mut self)`, and `WorkerCtx { event_sender: EventTx, local_kvstore, sample_drops }`.
  - `src/worker/session.rs` holds `handle_connect` / `handle_disconnect`, and `src/worker/connect.rs` holds `connect_zenoh` / `connect_zenoh_monitor` / `parse_listen_port` / `monitor_endpoints`. P1 T10 removed the monitor port: the monitor runs in client mode and listens on nothing.
  - `src/worker/pipeline.rs` holds the `EventTx` alias, `event_channel`, `send_sample` and the buffer thread.
  - The remaining handlers are `subscribe.rs`, `query.rs` (`handle_query`), `publish.rs` (`handle_publish`, `publish_shape`), `queryable.rs` and `samples.rs` (`message_from_sample`).
- **Types** (P1 T2/T3):
  - `src/types/{mod,message,commands,tree,limits,store}.rs`.
  - `ZenohMessage` has `kind: SampleKindView` and `source_timestamp`, plus a manual payload-free `Debug`.
  - `ZenohCommand` has a manual `Debug`.
  - `FailedOp` and `ZenohEvent::OperationFailed { op, error }` exist.
- **Events** (P1): `src/events/mod.rs` holds `process_events` with an 8 ms budget, plus `src/events/ingest.rs` and `src/events/json_cache.rs`.
- **App** (P1 + P3):
  - `src/app/mod.rs` holds the struct, `new(ctx)` and `#[cfg(test)] test_app() -> (Self, Sender<ZenohEvent>)`.
  - `src/app/layout.rs` holds `impl eframe::App`. Under eframe 0.36 the per-frame method is `fn ui(&mut self, ui: &mut egui::Ui, frame: &mut eframe::Frame)` (verified: https://docs.rs/eframe/0.36.2/eframe/trait.App.html).
  - `src/app/theme.rs` holds the theme (Snow White territory; do not touch).
  - **Banner type (P1 T21):** `ui_alert` is `Option<UiAlert>`, not `Option<String>`. `UiAlert` is `pub(crate) enum UiAlert { Success(String), Warning(String), Error(String) }` in `src/app/mod.rs`, with `text(&self) -> &str`. The banner adds the "Warning: " and "Error: " prefixes itself, so the text carries no `✓` or `⚠` prefix. Every P5 snippet that sets the banner wraps its text in one of the three variants, and tests match with `matches!` or `is_none()`, because P1 does not promise `PartialEq` on `UiAlert`.
  - **Time display (P1 T3):** `crate::types::format_local_time(&DateTime<Utc>, &DateTime<Utc>) -> String` formats local wall-clock time and adds the date when it is not today (F-T14-6). P5 formats every displayed time with it and never calls `.format(…)` on a UTC time.
  - **Local replies (P1 T5/T6):** a reply is local when `reply.replier_id()` has this session's zid. The `source:local` attachment no longer exists (F-T16-7).
  - **Query view text (P1 T23):** the Query view keeps one timing note: "Asks every queryable that matches the selector. With no match the answer comes back at once; a matching queryable that stays silent is reported when the timeout expires." (F-T16-2). Its "not connected" notice is worded by connection state (F-T16-11).
  - **Publish button (P1 T23):** its label is `publish_button_label(payload_is_empty, pending)` ("Publish", "Publish empty payload" or "Publishing…"). While `pending` (`publish_status` is `Sending`), a click does nothing, so a second press cannot send an empty payload (F-T15-3). Every new way to submit (Enter, Ctrl/Cmd+Enter) goes through the same guard.
  - **Help (P1 T26):** `src/ui/help.rs` holds `pub(crate) const HELP_SECTIONS: &[(&str, &[&str])]`, rendered inside a `ScrollArea`. Its last section is "Troubleshooting". There is no "Performance Tips" block any more. P5 T24 inserts its two sections directly before "Troubleshooting", so that section stays last.
- **UI:** `src/ui/{mod,topic_tree,publish,query,messages,help}.rs`. `src/payload.rs` holds `preview(bytes, max_text)`, and `src/validation.rs` holds `key_expr_error` and `selector_error`.
- **P3 additions assumed** (from `2026-09-25-p3-egui-036-port.md`):
  - Dependencies: `egui_kittest = { version = "0.36", features = ["eframe", "wgpu", "snapshot"] }` under dev-dependencies; eframe with `persistence`; `serde` derive; `rust-version = "1.95"`.
  - `App::logic` → `tick()` drains events, and `App::ui` draws.
  - `src/settings.rs` holds the persisted `Settings` (mode, transport, address, port, listen port, theme, subscriptions, filter).
  - `src/dialogs.rs` holds async rfd jobs, and `src/ui/file_jobs.rs` holds `FileJobsUI` (Save).
  - `src/ui/tree_rows.rs` holds `TreeRow { full_path, depth, is_branch }`, `flatten_rows`, `row_state_id` and `rows_cache_is_stale`, with the app fields `tree_rows_cache` and `tree_expand_generation`. The tree is drawn by `show_tree_row` inside `ScrollArea::show_rows`.
  - Labelled fields (P3 T11, T14): the Subscribe Key field is `labelled_by` its "Key:" label, and so are the Publish Key and the Query Selector (with "Key:" and "Selector:"). Kittest therefore finds each field with `get_by_label("Key:")` or `get_by_label("Selector:")`, because it leaves out a label node that labels another node. P5 keeps these `labelled_by` calls wherever it edits those rows.
  - The kittest helpers live in `src/ui/tests/{mod,shell,tree,a11y,views,snapshots}.rs` (`harness_for`, `harness`, `settle`), and module READMEs live in `src/README.md` and `src/ui/README.md`.
  - P5 UI tests follow the same in-crate pattern. They test one panel at a time with `Harness::new_ui_state`, and may use `crate::ui::tests::harness_for` for whole-app checks.
- **P4 additions assumed** (from `2026-09-25-p4-transfer-protocol-v2.md`):
  - File transfer lives in `src/transfer/**` behind the `@xfer` key space and `TransferRegistry`.
  - Plain publish is capped at `PLAIN_PUBLISH_MAX`, and v1 `__chunk` sending is removed.
  - P5 never touches `src/transfer/**` or `src/ui/transfers.rs`.

T1 Step 0 checks every assumption and stops if one is false.

## Global Constraints

- After every task, run `cargo build`, `cargo test`, `cargo clippy --all-targets -- -D warnings` and `cargo fmt --all -- --check`. All must pass.
- **Test filters.** A test command with more than one name filter passes them to libtest after `--`, as in `cargo test -- ui::publish ui::attachment_editor`. cargo takes only one positional `TESTNAME`, so `cargo test a b` stops with "error: unexpected argument 'b' found" before any test runs. A single filter may stay positional (`cargo test decode::`).
- A task may create or modify only the files in its **Owns** list. If a step seems to need another file, stop and report instead of editing it.
- **Crate versions:**
  - egui, eframe, egui_extras and egui_kittest stay on `0.36.2`, and rfd stays on `0.17`.
  - zenoh is declared `"1.10"` (lock 1.10.1, `unstable`), and zenoh-ext is `"1.10"`.
  - The only new crates are `zenoh-ext`, `ciborium = "0.2"`, `image = { version = "0.25", default-features = false, features = ["png", "jpeg"] }` and `egui_extras` (image loader only), all added in T1.
  - P3 said not to re-add `egui_extras`. P5 re-adds it only for its `image` loader.
- **Zenoh tests:**
  - Tests that open Zenoh sessions use `#[tokio::test(flavor = "multi_thread", worker_threads = 2)]` with `scouting/multicast/enabled = false`.
  - Tests that bind a listen port are `#[ignore = "opens network sessions"]`.
  - Tests that need a real router are `#[ignore = "needs zenohd"]`, with setup instructions in their doc comment.
  - The task's Done-when runs these ignored tests explicitly.
- **UI tests:**
  - Every UI task adds `egui_kittest` tests in an in-crate `#[cfg(test)] mod ui_tests`, built from `ZenohExplorer::test_app()` or `test_app_with_commands()` (T1).
  - Every pure-logic function gets unit tests.
  - UI tests find widgets by their accessible label (`kittest::Queryable::get_by_label`), so every new interactive control needs a visible text label, or `widget_info` when it is painted.
- **No theme colours.** Do not edit `src/colors.rs` or `src/app/theme.rs`, and do not add new `Color32` constants: the Snow White plan owns colour. New state indicators (alive or gone, matching, recent activity, filter mode) must carry a text or glyph cue and must never rely on colour alone.
- **Do not touch** `src/transfer/**` or `src/ui/transfers.rs` (P4), `src/settings.rs`, `src/dialogs.rs`, `src/ui/file_jobs.rs`, `src/ui/tree_rows.rs` or `src/ui/tests/**` (P3), or the tree's `plus_minus_icon`.
- Every external API used here was checked against the registry sources of zenoh 1.10.1, egui 0.36.2, eframe 0.36.2, egui_extras 0.36.2 and egui_kittest 0.36.2 (plus kittest 0.4.0). The matching docs.rs page is cited at the step that uses the API.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- **Disconnect while features are running.** If the user disconnects with liveliness, connectivity, matching or a query in flight, every feature task must stop. No events may arrive for the dead session, and the next Connect must start clean. Pinned by T1 `teardown_aborts_feature_tasks`.
- **A query that gets no replies, that the user cancels, or that a disconnect cuts off.** The run must end as "no replies", "cancelled" or "cancelled: disconnected", never stay on "running". This holds even when a quick reconnect has already set the status to Connecting. Pinned by T14 `cancel_finishes_run_as_cancelled` and T16 `finished_run_without_replies_says_no_replies`, `disconnect_ends_running_runs_as_cancelled` and `disconnect_while_reconnecting_ends_running_run` (F-T16-11).
- **A half-typed or invalid key expression in the tree filter** (`demo/**/`, `a//b`, `$*x`). The filter must not panic and must not blank the tree: it falls back to substring matching with a visible warning. Pinned by T19 `invalid_ke_falls_back_to_substring`.
- **Garbage bytes in the CBOR, JSON or image views.** Truncated CBOR and random bytes labelled `image/png` must show an error line, never panic. Pinned by T17 `cbor_garbage_is_error_not_panic` and `image_kind_needs_magic_bytes`.
- **A flapping liveliness token** (appears, disappears, appears). It must stay one row that is alive, with a change count of 3, not three rows. Pinned by T10 `flapping_token_is_one_row`.

## Tasks

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Lane Scaffold · owns Cargo.toml, Cargo.lock, src/README.md, src/ui/README.md, src/main.rs, src/types/{mod,features,commands,message}.rs, src/app/{mod,layout}.rs, src/events/mod.rs, src/worker/{mod,state,session,connect,query,publish,admin,liveliness,connectivity,matching}.rs, src/ui/{mod,topic_tree,publish,query,admin,topology,liveliness,logs,payload_viewer,connection,attachment_editor}.rs, src/{attachment,encodings,admin_model,topology_model,selector_params,query_book,decode,filter,tree_nav,rates,profiles,shortcuts,logs}.rs] Shared scaffolding: crates, feature types, commands and events, four new tabs, app and worker state fields, dispatch arms, stub modules (including `QueryBook::on_disconnected`, called from the `Disconnected` arm outside P1 T12's status guard, F-T16-11), the `pending_subscribes` set that the `SubscriptionCreated`, Subscribe-failure and `Disconnected` arms clear (T19), the focus ids `PUBLISH_KEY_ID` and `QUERY_SELECTOR_ID` on the Publish Key and Query Selector fields (F-T19-5), the payload block moved into `payload_viewer.rs` and the connection panel into `connection.rs`, complete `attachment.rs` and `encodings.rs` | — | Tests `features::`, `attachment::`, `encodings::` and `teardown_aborts_feature_tasks` pass. The app starts and shows 8 tabs (Topics, Publish, Query, Admin, Topology, Liveliness, Logs, Help). `git grep -n 'todo!\|unimplemented!' src` is empty. `grep -n 'query_book.on_disconnected' src/events/mod.rs` and `grep -n 'pending_subscribes.remove' src/events/mod.rs` each match. `grep -q 'PUBLISH_KEY_ID' src/ui/publish.rs && grep -q 'QUERY_SELECTOR_ID' src/ui/query.rs` exits 0, so both fields carry their focus ids. |
| T2 | [Wave 1 · Lane ADM · owns examples/admin_dump.rs, tests/fixtures/admin-space/*] Record real admin-space replies from two local `zenohd` 1.10.1 routers into a JSONL fixture, before any admin UI work | T1 | `tests/fixtures/admin-space/zenohd-1.10.1.jsonl` exists. It has ≥ 1 reply each for node info `@/<zid>/router`, `linkstate/south:…` or `linkstate/north`, `subscriber/…`, `token/…` and `metrics`. Its `README.md` records the zenohd version and the exact commands. |
| T3 | [Wave 1 · Lane ADM · owns src/worker/admin.rs] `AdminQuery` handler: `get` on `@/…` with target All and no consolidation, replies batched into `ZenohEvent::Admin` | T1 | Loopback test `admin_query_returns_own_node_info` (session with `adminspace/enabled=true`) passes. `cargo test -- --ignored admin_query_against_zenohd` passes with a local zenohd. |
| T4 | [Wave 2 · Lane ADM · owns src/admin_model.rs] Pure admin model: key parser (`@/<zid>/<whatami>/<section>/…`), node and section tree, pretty payload rendering, all validated against the T2 fixture | T2 | `cargo test admin_model::` passes, including `every_fixture_key_parses` and `linkstate_keys_use_region_ids`. |
| T5 | [Wave 3 · Lane ADM · owns src/ui/admin.rs] Admin tab: selector with presets, Refresh, a node → section → entry tree, a pretty JSON pane with Copy | T4 | Kittest tests `admin_refresh_sends_admin_query`, `admin_tab_shows_nodes_and_pretty_json` and `admin_ignores_stale_batches` pass. `grep -n 'format("%H' src/ui/admin.rs` prints nothing (local time, F-T14-6). |
| T6 | [Wave 1 · Lane TOP · owns src/worker/connectivity.rs] Connectivity worker: own ZID snapshot plus `transport_events_listener` and `link_events_listener` (history on) → `ZenohEvent::Connectivity` | T1 | Loopback test `connectivity_snapshot_reports_own_zid` passes. `cargo test -- --ignored connectivity_reports_peer_transport` passes. |
| T7 | [Wave 3 · Lane TOP · owns src/topology_model.rs] Topology model: linkstate DOT parser, admin info `sessions` edges, own-link edges, deterministic force-directed layout | T4 | `cargo test topology_model::` passes, including `parses_fixture_linkstate`, `layout_is_deterministic_and_finite` and `connected_nodes_end_closer`. |
| T8 | [Wave 4 · Lane TOP · owns src/ui/topology.rs] Topology tab: painter graph with shape-coded node kinds and stroke-coded link sources, accessible node buttons, a details pane and live refresh | T7 | Kittest tests `topology_counts_nodes_and_links`, `clicking_node_shows_details` and `opening_tab_starts_connectivity` pass. |
| T9 | [Wave 1 · Lane LIV · owns src/worker/liveliness.rs] Liveliness worker: `liveliness().declare_subscriber(key).history(true)`, where a PUT or DELETE becomes a `LivelinessEvent` | T1 | Loopback test `liveliness_reports_token_up_and_down` passes. |
| T10 | [Wave 1 · Lane LIV · owns src/ui/liveliness.rs] Liveliness tab: a token table with alive or gone state (glyph and word), first seen, last change, change count, filter, Clear gone, and Start/Stop | T1 | Unit test `flapping_token_is_one_row` and kittest tests `start_sends_liveliness_command` and `clear_gone_removes_dead_rows` pass. `grep -n 'format("%H' src/ui/liveliness.rs` prints nothing (local time, F-T14-6). |
| T11 | [Wave 1 · Lane PUB · owns src/worker/publish.rs] Publish honours `PublishOptions`: put or delete, priority, congestion (Drop, Block, BlockFirst), express, reliability, attachment | T1 | Loopback tests `publish_applies_attachment` and `publish_delete_sends_delete_sample` pass. `cargo test -- --ignored publish_qos_crosses_the_wire` passes. |
| T12 | [Wave 1 · Lane PUB · owns src/worker/matching.rs] Matching watch: a publisher declared on the monitor session with `Locality::Remote`, `matching_status` and then `matching_listener` → `ZenohEvent::Matching` | T1 | Loopback test `matching_ignores_monitor_own_subscriber` passes. `cargo test -- --ignored matching_flips_when_subscriber_appears` passes. |
| T13 | [Wave 1 · Lane PUB · owns src/ui/publish.rs, src/ui/attachment_editor.rs] Publish form: put or delete, QoS section, encoding presets combo, key/value or text attachment editor, matching indicator, Ctrl+Enter, and Enter in the Key field publishes with focus kept in Key (F-T19-4), only when Key had focus as the frame started. Every trigger keeps P1 T23's button label and its no-send-while-pending guard (F-T15-3), and the button stays as P3 T14 left it | T1 | Kittest tests `publish_sends_options`, `delete_mode_disables_payload`, `matching_indicator_uses_words`, `enter_in_key_field_publishes`, `enter_after_leaving_key_does_not_publish` and `attachment_editor_adds_and_removes_rows` pass. |
| T14 | [Wave 1 · Lane QRY · owns src/worker/query.rs, src/worker/samples.rs] `QueryWithOptions`: target, consolidation, payload encoding, attachment, `accept_replies`, `CancellationToken`, reply metadata (encoding, attachment, replier_id), error replies, and `SampleExtras` on received samples. A reply is local only when its `replier_id` is this session's zid, never from the `source:local` text (F-T16-7) | T1 | Loopback tests `query_reply_carries_metadata`, `own_session_reply_is_local_by_replier_id`, `error_reply_is_reported_as_row`, `cancel_finishes_run_as_cancelled` and `sample_extras_capture_attachment_and_qos` pass. `grep -n 'source:local' src/worker/query.rs` prints nothing. |
| T15 | [Wave 1 · Lane QRY · owns src/selector_params.rs] Selector parameter model: parse and build `key?k=v;k2=v2` with percent-encoding of `%`, `;` and `#`, `_time` presets validated by `zenoh::query::TimeRange` | T1 | `cargo test selector_params::` passes, including `roundtrip_escapes_separators` and `time_presets_parse`. |
| T16 | [Wave 2 · Lane QRY · owns src/ui/query.rs, src/query_book.rs] Query form and results: options, parameter editor with time presets, attachment, Cancel, results grouped per query ID with per-run and global Clear, error rows. A disconnect ends running queries as "cancelled: disconnected" and marks older runs "earlier session" (`QueryBook::on_disconnected`, F-T16-11), also when a quick reconnect has already set the status to Connecting. Enter in Selector sends the query (F-T19-4), only when Selector had focus as the frame started. Times use `format_local_time` (F-T14-6), and P1 T23's timing note stays (F-T16-2) | T13, T15 | Unit tests `query_book_caps_runs_and_replies`, `finished_run_without_replies_says_no_replies`, `disconnect_ends_running_runs_as_cancelled`, `disconnect_event_ends_running_run` and `disconnect_while_reconnecting_ends_running_run`, and kittest tests `query_sends_options_and_registers_run`, `enter_in_selector_sends_query`, `enter_after_leaving_selector_does_not_query`, `cancel_button_sends_cancel` and `param_editor_rewrites_selector`, pass. `grep -n 'will timeout\|format("%H' src/ui/query.rs` prints nothing. |
| T17 | [Wave 1 · Lane PAY · owns src/decode.rs] Pure decoders: hex+ASCII lines, pretty JSON, CBOR → JSON (ciborium), PNG and JPEG sniffing, available-tab selection | T1 | `cargo test decode::` passes, including `cbor_garbage_is_error_not_panic` and `image_kind_needs_magic_bytes`. |
| T18 | [Wave 2 · Lane PAY · owns src/ui/payload_viewer.rs] Payload viewer: Text, JSON, Hex, CBOR and Image tabs, Copy buttons, "Load full payload" from the store, last-sample attachment and QoS | T17 | Kittest tests `viewer_offers_json_and_hex_tabs`, `load_full_reads_store_bytes`, `image_tab_only_for_images` and `copy_puts_text_on_clipboard` pass. |
| T19 | [Wave 1 · Lane TREE · owns src/filter.rs, src/ui/topic_tree.rs] Key-expression filter mode (`*`, `**` or `$*` → `keyexpr::intersects`), mode indicator, Enter subscribes (from a KE filter and from the Subscribe Key field, F-T19-4) once per key: never for a key already subscribed or still pending, and with no success banner on send (F-T8-3). Invalid-KE fallback | T1 | Unit tests `ke_mode_matches_by_intersection` and `invalid_ke_falls_back_to_substring`, and kittest tests `enter_in_ke_mode_subscribes`, `enter_in_subscribe_key_subscribes` and `enter_after_leaving_filter_does_not_subscribe`, pass. |
| T20 | [Wave 2 · Lane TREE · owns src/tree_nav.rs, src/ui/topic_tree.rs] Arrow-key tree navigation over P3's cached `TreeRow`s (Up, Down, Right expands, Left collapses or goes to parent) with scroll-to-selected via `vertical_scroll_offset`. Clearing the filter opens the selection's ancestors and scrolls it into view (F-T13-3) | T19 | Unit tests `down_up_walk_visible_rows`, `left_goes_to_parent_when_closed`, `scroll_offset_centres_and_clamps` and `ancestors_of_nested_path`, and kittest tests `arrow_keys_move_selection` and `clearing_filter_reveals_selection`, pass. |
| T21 | [Wave 3 · Lane TREE · owns src/rates.rs, src/types/tree.rs, src/ui/topic_tree.rs] Per-topic msg/s next to counts, the age of the last message on each leaf row ("now", "12 s ago", "4 min ago", "2 h ago"), a recent-activity marker (`↻` glyph plus bold), and "Last message <local time> (<age>) · <rate>" on the topic page (F-T20-4) | T20 | Unit tests `rate_over_one_second_window`, `rate_decays_to_zero` and `age_words`, and kittest tests `recent_leaf_shows_glyph_and_rate`, `quiet_leaf_row_shows_age` and `topic_page_shows_last_message_age`, pass. |
| T22 | [Wave 1 · Lane PROF · owns src/profiles.rs, src/worker/connect.rs, src/worker/session.rs] Profiles model: persistence, recent endpoints, endpoint and config validation. Worker: JSON5, YAML and TOML config import plus TLS root CA. In file mode the monitor is built from the file but runs as a client: it dials the file's own listen endpoints (peer file with no connect endpoint, or router file) or the file's connect endpoints (peer file with connect endpoints, or client file), and fails with "nothing to dial" when there are none. The user's own session stays exactly as the file says (F-T20-7) | T1 | `cargo test profiles::` passes. Loopback test `connect_with_toml_config_file` passes. Tests `file_mode_monitor_with_nothing_to_dial_fails`, `file_monitor_config_by_mode` and `loopback_dial_maps_unspecified_host` pass. `cargo test -- --ignored file_mode_monitor_sees_third_party_samples` passes, including its third-session `t/f` assertion. `grep -rn 'monitor_port' src/worker` prints nothing. |
| T23 | [Wave 2 · Lane PROF · owns src/ui/connection.rs] Connection panel: profile picker, Save as, Delete, endpoint list editor with `?` metadata and `#` config, protocol builder (tcp, udp, tls, quic, ws), recent endpoints, config import, TLS certificate picker | T22 | Kittest tests `choosing_profile_fills_connect`, `invalid_endpoint_disables_connect` and `save_as_adds_profile` pass. |
| T24 | [Wave 1 · Lane KEYS · owns src/shortcuts.rs, src/ui/help.rs] Global shortcuts: Ctrl/Cmd+1..8 tabs that also move focus into the view (F-T19-5), Ctrl+F filter, Ctrl+Enter act, Esc dismiss banner, Space toggles auto-scroll. Help lists them in two sections inserted into P1 T26's `HELP_SECTIONS` directly before "Troubleshooting", which stays last (the old "after Performance Tips" anchor no longer exists) | T1 | Kittest tests `cmd_digit_switches_tab` (including the focus checks), `cmd_f_focuses_filter`, `esc_dismisses_banner`, `space_toggles_autoscroll_only_without_focus` and `cmd_enter_flags_submit_on_action_tabs` pass. Unit test `help_lists_keyboard_shortcuts` and P1 T26's Help tests pass. |
| T25 | [Wave 1 · Lane LOG · owns src/logs.rs, src/main.rs] tracing `Layer` → 5 000-line ring buffer (`logs::global()`), installed next to the fmt layer | T1 | `cargo test logs::` passes, including `ring_keeps_last_5000` and `layer_captures_message_and_fields`. |
| T26 | [Wave 2 · Lane LOG · owns src/ui/logs.rs] Logs tab: level filter, text filter, follow, Clear, Copy visible, virtualised rows | T25 | Kittest tests `logs_tab_filters_by_level` and `logs_copy_visible` pass. `grep -n 'format("%H' src/ui/logs.rs` prints nothing (local time, F-T14-6). |
| T27 | [Wave 5 · Integration · owns no source files] Full verification, ignored network and zenohd tests, audit, and a manual smoke run of every feature | T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13, T14, T15, T16, T17, T18, T19, T20, T21, T22, T23, T24, T25, T26 | Every command in T27 passes, and its output is pasted into the evidence. The smoke checklist is ticked. |

## Lanes and waves

```
Wave 0  T1 scaffold ──────────────────────────────────────────────────────────────────────┐
Wave 1  ADM: T2 T3   TOP: T6   LIV: T9 T10   PUB: T11 T12 T13   QRY: T14 T15              │
        PAY: T17     TREE: T19   PROF: T22   KEYS: T24   LOG: T25                          │
Wave 2  ADM: T4(T2)            QRY: T16(T13,T15)  PAY: T18(T17)  TREE: T20(T19)            │
        PROF: T23(T22)         LOG: T26(T25)                                               │
Wave 3  ADM: T5(T4)   TOP: T7(T4)   TREE: T21(T20)                                         │
Wave 4  TOP: T8(T7)                                                                        │
Wave 5  T27 integration (all) ◄────────────────────────────────────────────────────────────┘
```

The maximum width is **15 tasks at once**, in Wave 1: T2, T3, T6, T9, T10, T11, T12, T13, T14, T15, T17, T19, T22, T24 and T25. The critical path is T1 → T2 → T4 → T7 → T8 → T27.

**File ownership matrix.** A file listed for several tasks is always reached through a dependency chain, so no two of those tasks can run at the same time.

| File | Tasks (in dependency order) |
|---|---|
| Cargo.toml, Cargo.lock, src/README.md, src/ui/README.md | T1 |
| src/main.rs | T1 → T25 |
| src/types/mod.rs, features.rs, commands.rs, message.rs | T1 |
| src/types/tree.rs | T21 (only) |
| src/app/mod.rs, src/app/layout.rs, src/events/mod.rs | T1 |
| src/worker/mod.rs, state.rs | T1 |
| src/worker/session.rs, connect.rs | T1 → T22 |
| src/worker/query.rs | T1 → T14 |
| src/worker/samples.rs | T14 (only) |
| src/worker/publish.rs | T1 → T11 |
| src/worker/admin.rs | T1 → T3 |
| src/worker/connectivity.rs | T1 → T6 |
| src/worker/liveliness.rs | T1 → T9 |
| src/worker/matching.rs | T1 → T12 |
| src/ui/mod.rs | T1 |
| src/ui/topic_tree.rs | T1 → T19 → T20 → T21 |
| src/ui/publish.rs, src/ui/attachment_editor.rs | T1 → T13 |
| src/ui/query.rs, src/query_book.rs | T1 → T16 (via T13, T15) |
| src/ui/admin.rs | T1 → T5 (via T4 → T2) |
| src/ui/topology.rs | T1 → T8 (via T7) |
| src/ui/liveliness.rs | T1 → T10 |
| src/ui/logs.rs | T1 → T26 (via T25) |
| src/ui/payload_viewer.rs | T1 → T18 (via T17) |
| src/ui/connection.rs | T1 → T23 (via T22) |
| src/ui/help.rs | T24 (only) |
| src/attachment.rs, src/encodings.rs | T1 (complete) |
| src/admin_model.rs | T1 (stub) → T4 |
| src/topology_model.rs | T1 (stub) → T7 |
| src/selector_params.rs | T1 (stub) → T15 |
| src/decode.rs | T1 (stub) → T17 |
| src/filter.rs | T1 (stub) → T19 |
| src/tree_nav.rs | T1 (stub) → T20 |
| src/rates.rs | T1 (stub) → T21 |
| src/profiles.rs | T1 (stub) → T22 |
| src/shortcuts.rs | T1 (stub) → T24 |
| src/logs.rs | T1 (stub) → T25 |
| examples/admin_dump.rs, tests/fixtures/admin-space/* | T2 |

**Cross-lane reads, not writes:**
- T7 reads `admin_model::parse_admin_key` (T4), so T7 depends on T4.
- T16 reads `ui::attachment_editor::attachment_editor` (T13), so T16 depends on T13.
- T5, T8, T10, T13, T16 and T18 run against events declared in T1. Their worker counterparts (T3, T6, T9, T11, T12, T14) can land in either order. T27 checks the pairs end to end.

**Merging:** each task commits only its owned files. Wave 1 branches from the T1 commit, and owned files are disjoint within a wave, so merges are conflict-free by construction.

## Out of scope → P6

- **Protobuf decoding.** It needs a `.proto` or `FileDescriptorSet` loader (for example `prost-reflect`) plus a UI for mapping schemas to key expressions.
- **CDR decoding.** ROS 2 / DDS payloads need the IDL or message type (for example a `.msg` registry). Without a schema, CDR bytes can only be hex-dumped, which the Hex tab already does.
- **Writing to the admin space.** `put` on `@/<zid>/router/config/**` needs router write permission and a confirmation UX.
- **Topology export** (DOT or PNG) and manual node pinning or dragging.
- **Aggregate msg/s on branch rows** and sparkline history.
- **One-shot liveliness GET** (`liveliness().get()`) and liveliness token declaration from the UI.
- **Reusing a `Querier`** (`declare_querier`) for repeated queries, and querier matching status.
- **Shared-memory transport details** (`Transport::is_shm` exists only with the `shared-memory` feature).

---

### Task T1: Shared scaffolding (Wave 0)

**Owns:**
- `Cargo.toml`, `Cargo.lock`, `src/README.md`, `src/ui/README.md` (P3 T2/T3 module READMEs), `src/main.rs`
- `src/types/mod.rs`, `src/types/features.rs` (new), `src/types/commands.rs`, `src/types/message.rs`
- `src/app/mod.rs`, `src/app/layout.rs`, `src/events/mod.rs`
- `src/worker/{mod,state,session,connect,query,publish}.rs`, plus the new `src/worker/{admin,liveliness,connectivity,matching}.rs`
- `src/ui/{mod,topic_tree,publish,query}.rs`, plus the new `src/ui/{admin,topology,liveliness,logs,payload_viewer,connection,attachment_editor}.rs`
- New: `src/{attachment,encodings,admin_model,topology_model,selector_params,query_book,decode,filter,tree_nav,rates,profiles,shortcuts,logs}.rs`

**Rule:** behaviour-preserving, apart from the four new (stub) tabs. Moved code is moved verbatim. Stubs compile and do nothing visible beyond a heading. `attachment.rs`, `encodings.rs` and the `features.rs` conversions are complete and tested, because several lanes use them.

**Interfaces (produced; every later task relies on these exact names):**
- `crate::types::{RequestId, PriorityView, CongestionView, ReliabilityView, PublishKind, AttachmentSpec, PublishOptions, QueryTargetView, ConsolidationView, QueryRequest, SampleExtras, ReplyRow, QueryEvent, AdminReply, AdminBatch, LivelinessEvent, TransportView, LinkView, ConnectivityEvent}`, all from `src/types/features.rs`, re-exported by `types/mod.rs`.
- `FailedOp` gains `Admin`, `Liveliness`, `Connectivity` and `Matching`.
- New `ZenohCommand` variants:
  - `AdminQuery { id, selector, timeout_ms }`
  - `StartLiveliness { key_expr }`, `StopLiveliness`
  - `StartConnectivity`, `StopConnectivity`
  - `WatchMatching { key }`
  - `QueryWithOptions(QueryRequest)`, `CancelQuery { id }`
- Changed `ZenohCommand` variants:
  - `Publish { …, options: PublishOptions }`
  - `Connect { …, config_file: Option<PathBuf>, tls_root_ca: Option<PathBuf> }`
- New `ZenohEvent` variants: `Admin(AdminBatch)`, `Connectivity(ConnectivityEvent)`, `Liveliness(LivelinessEvent)`, `Matching { key, matching }` and `Query(QueryEvent)`.
- `ZenohMessage.extras: Option<Box<SampleExtras>>` and `ZenohMessage::with_extras(self, Option<Box<SampleExtras>>) -> Self`.
- `DetailView::{Admin, Topology, Liveliness, Logs}`, `DetailView::TABS: [DetailView; 8]` and `DetailView::label(self) -> &'static str`.
- `WorkerState` fields:
  - `liveliness_task: Option<JoinHandle<()>>`
  - `connectivity_tasks: Vec<JoinHandle<()>>`
  - `matching_task: Option<JoinHandle<()>>`
  - `query_tokens: HashMap<RequestId, zenoh::cancellation::CancellationToken>`
  - `query_tasks: HashMap<RequestId, JoinHandle<()>>`
- Worker handler signatures (the stubs that lanes fill):
  - `admin::handle_admin_query(st: &WorkerState, ctx: &WorkerCtx, id: RequestId, selector: String, timeout_ms: u64)`
  - `liveliness::handle_start(st: &mut WorkerState, ctx: &WorkerCtx, key_expr: String)` and `liveliness::handle_stop(st: &mut WorkerState)`
  - `connectivity::handle_start(st: &mut WorkerState, ctx: &WorkerCtx)` and `connectivity::handle_stop(st: &mut WorkerState)`
  - `matching::handle_watch(st: &mut WorkerState, ctx: &WorkerCtx, key: String)`
  - `query::handle_query_with_options(st: &mut WorkerState, ctx: &WorkerCtx, req: QueryRequest)` and `query::handle_cancel(st: &mut WorkerState, ctx: &WorkerCtx, id: RequestId)`
  - All of these are `async fn` except the two `handle_stop`s.
  - `publish::handle_publish(…, filename, options: PublishOptions)`, which gains a last parameter.
  - `connect::connect_zenoh(locators, listen_port, mode, config_json, config_file: Option<&Path>, tls_root_ca: Option<&Path>)` and `connect::connect_zenoh_monitor(locators, listen_port, mode, config_file: Option<&Path>, tls_root_ca: Option<&Path>)`, which gain two last parameters. `connect_zenoh` then takes six arguments and the monitor five, each starting with P1 T10's order. P1 T10 replaced the monitor's old monitor-port parameter with the publishing session's `listen_port`, so no plan passes a monitor port.
  - `session::handle_connect(…, config_file: Option<PathBuf>, tls_root_ca: Option<PathBuf>)`.
- App fields:
  - `admin: ui::admin::AdminState`
  - `topology: ui::topology::TopologyState`
  - `liveliness: ui::liveliness::LivelinessState`
  - `logs: ui::logs::LogsState`
  - `payload_viewer: ui::payload_viewer::PayloadViewerState`
  - `publish_form: ui::publish::PublishFormState`
  - `query_form: ui::query::QueryFormState`
  - `query_book: query_book::QueryBook`
  - `connection_form: ui::connection::ConnectionFormState`
  - `tree_nav: tree_nav::TreeNavState`
  - `shortcut_submit: bool`
  - `request_seq: u64`
  - `pending_subscribes: std::collections::HashSet<String>`: key expressions whose `Subscribe` the Topics panel has sent and the worker has not yet answered. T1's event arms clear it (Step 10). T19 fills it and reads it, so a second Enter or click before `SubscriptionCreated` sends nothing.
- App methods: `next_request_id(&mut self) -> RequestId`, `send_command(&self, ZenohCommand) -> bool`, and `#[cfg(test)] test_app_with_commands() -> (Self, Sender<ZenohEvent>, Receiver<ZenohCommand>)`.
- UI hooks (stubs that lanes fill):
  - traits `AdminUI::show_admin_tab`, `TopologyUI::show_topology_tab`, `LivelinessUI::show_liveliness_tab`, `LogsUI::show_logs_tab`, `ConnectionUI::show_connection_panel`
  - `PayloadViewerUI::show_current_value(&mut self, ui, topic: &str, payload: Option<String>, encoding: Option<String>)`
  - `ZenohExplorer::handle_shortcuts(&mut self, ctx: &egui::Context)` in `shortcuts.rs`
- State hooks (stubs): `AdminState::on_batch(&mut self, AdminBatch)`, `TopologyState::{on_admin(&mut self, &AdminBatch), on_connectivity(&mut self, ConnectivityEvent)}`, `LivelinessState::on_event(&mut self, LivelinessEvent)`, `PublishFormState::on_matching(&mut self, &str, bool)`, `QueryBook::apply(&mut self, QueryEvent)`, plus `on_disconnected(&mut self)` on the Admin, Topology and Liveliness states and on `QueryBook` (T16 fills it in so running queries end as cancelled, F-T16-11).
- `crate::ui::TREE_FILTER_ID: &str = "tree_filter"`, the `egui::Id` source of the topic-tree filter `TextEdit`.
- `crate::ui::PUBLISH_KEY_ID: &str = "publish_key"` and `crate::ui::QUERY_SELECTOR_ID: &str = "query_selector"`, the `egui::Id` sources of the Publish Key and Query Selector `TextEdit`s. T24's Ctrl/Cmd+digit moves focus to them (F-T19-5), and T13 and T16 keep focus there after Enter submits (F-T19-4).
- `attachment::{encode, decode_key_values, describe}` and `encodings::{ENCODING_PRESETS, encoding_combo}`.

- [ ] **Step 0: Check the baseline.** Run:

```bash
ls src/worker/{mod,state,session,connect,pipeline,subscribe,query,publish,queryable,samples}.rs \
   src/types/{mod,message,commands,tree,limits,store}.rs src/events/{mod,ingest,json_cache}.rs \
   src/app/{mod,layout,theme}.rs src/payload.rs src/validation.rs
grep -nE '^(egui|eframe|egui_extras|rfd|serde|zenoh)\b|egui_kittest|persistence' Cargo.toml
grep -n 'fn ui(&mut self, ui: &mut egui::Ui' src/app/layout.rs
grep -n 'fn test_app' src/app/mod.rs
grep -n 'enum UiAlert\|ui_alert: Option<UiAlert>' src/app/mod.rs
grep -n 'pub fn format_local_time' src/types/message.rs
grep -n 'HELP_SECTIONS' src/ui/help.rs
```

Expected:
- every file is listed;
- `enum UiAlert` and `ui_alert: Option<UiAlert>` both match (P1 T21), `format_local_time` matches (P1 T3), and `HELP_SECTIONS` matches (P1 T26);
- `eframe = { version = "0.36…", … "persistence" … }`, `egui = "0.36…"`, `rfd = "0.17…"` and `serde` with `derive`;
- `egui_kittest = "0.36.2"` under `[dev-dependencies]`;
- one `fn ui(` match and one `fn test_app` match.

If a file or dependency is missing, stop and report which P1–P4 task did not land. Do not patch another plan's work here. The one exception: if `egui_kittest` or `serde` is absent, add it in Step 1, because both are needed here.

- [ ] **Step 1: Crates.** In `Cargo.toml` `[dependencies]`:
  - change the zenoh line to `zenoh = { version = "1.10", features = ["unstable"] }`;
  - add:

```toml
zenoh-ext = "1.10"
egui_extras = { version = "0.36", default-features = false, features = ["image"] }
image = { version = "0.25", default-features = false, features = ["png", "jpeg"] }
ciborium = "0.2"
```

  - If P4 already added `zenoh-ext`, keep P4's line.
  - P3's "do not re-add `egui_extras`" rule was scoped to the port. P5 re-adds it with `default-features = false` and only the `image` feature, because egui 0.36 has no built-in PNG or JPEG decoder.
  - If `serde` is absent, add `serde = { version = "1", features = ["derive"] }`.
  - If `egui_kittest` is absent, add `[dev-dependencies]` with `egui_kittest = "0.36.2"`.

Run `cargo build && cargo tree -i zenoh-ext --depth 0 && cargo tree -i ciborium --depth 0`. Expected: `zenoh-ext v1.10.1` and `ciborium v0.2.2`.
  - Sources: z_serialize at https://docs.rs/zenoh-ext/1.10.1/zenoh_ext/fn.z_serialize.html, install_image_loaders at https://docs.rs/egui_extras/0.36.2/egui_extras/fn.install_image_loaders.html (the `image` feature enables the loader; the `image` crate features enable the PNG and JPEG decoders, because egui_extras depends on `image` with `default-features = false`).

- [ ] **Step 2: Write the failing tests** for the complete helpers.
  - At the bottom of the new `src/types/features.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn priority_roundtrips_through_zenoh() {
        for p in PriorityView::ALL {
            let z: zenoh::qos::Priority = p.into();
            assert_eq!(PriorityView::from(z), p);
        }
    }

    #[test]
    fn congestion_and_reliability_roundtrip() {
        for c in CongestionView::ALL {
            let z: zenoh::qos::CongestionControl = c.into();
            assert_eq!(CongestionView::from(z), c);
        }
        for r in [ReliabilityView::Reliable, ReliabilityView::BestEffort] {
            let z: zenoh::qos::Reliability = r.into();
            assert_eq!(ReliabilityView::from(z), r);
        }
    }

    #[test]
    fn query_enums_map_to_zenoh() {
        use zenoh::query::{ConsolidationMode, QueryTarget};
        assert_eq!(QueryTarget::from(QueryTargetView::AllComplete), QueryTarget::AllComplete);
        assert_eq!(ConsolidationMode::from(ConsolidationView::Latest), ConsolidationMode::Latest);
        assert_eq!(ConsolidationMode::from(ConsolidationView::default()), ConsolidationMode::Auto);
    }

    #[test]
    fn publish_defaults_keep_current_behaviour() {
        let o = PublishOptions::default();
        assert_eq!(o.kind, PublishKind::Put);
        assert_eq!(o.congestion, CongestionView::Block); // explorer has always used Block
        assert_eq!(o.priority, PriorityView::Data);
        assert_eq!(o.attachment, AttachmentSpec::None);
    }
}
```

  - In the new `src/attachment.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::AttachmentSpec;

    #[test]
    fn key_values_roundtrip() {
        let spec = AttachmentSpec::KeyValue(vec![("a".into(), "1".into()), ("b".into(), "x y".into())]);
        let bytes = encode(&spec).unwrap();
        assert_eq!(
            decode_key_values(&bytes).unwrap(),
            vec![("a".to_string(), "1".to_string()), ("b".to_string(), "x y".to_string())]
        );
        assert_eq!(describe(&bytes), "a=1, b=x y");
    }

    #[test]
    fn plain_text_is_not_mistaken_for_key_values() {
        assert_eq!(decode_key_values(b"source:local"), None);
        assert_eq!(describe(b"source:local"), "source:local");
    }

    #[test]
    fn none_encodes_to_nothing_and_binary_describes_as_hex() {
        assert_eq!(encode(&AttachmentSpec::None), None);
        assert_eq!(encode(&AttachmentSpec::Text("hi".into())).unwrap(), b"hi".to_vec());
        assert!(describe(&[0xff, 0xfe]).starts_with("[binary 2 bytes]"));
    }
}
```

  - In the new `src/encodings.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn presets_are_canonical_zenoh_encodings() {
        for p in ENCODING_PRESETS {
            assert_eq!(zenoh::bytes::Encoding::from(*p).to_string(), *p, "preset {p}");
        }
    }
}
```

  - In `src/worker/state.rs` tests (create the `#[cfg(test)] mod tests` if P1 did not):

```rust
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn teardown_aborts_feature_tasks() {
        let mut st = WorkerState::default();
        let never = || tokio::spawn(async { tokio::time::sleep(std::time::Duration::from_secs(3600)).await });
        st.liveliness_task = Some(never());
        st.connectivity_tasks.push(never());
        st.matching_task = Some(never());
        st.query_tasks.insert(1, never());
        st.query_tokens.insert(1, zenoh::cancellation::CancellationToken::default());
        let probes: Vec<_> = [
            st.liveliness_task.as_ref().unwrap().abort_handle(),
            st.connectivity_tasks[0].abort_handle(),
            st.matching_task.as_ref().unwrap().abort_handle(),
            st.query_tasks[&1].abort_handle(),
        ]
        .into();
        let token = st.query_tokens[&1].clone();
        st.teardown().await;
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert!(probes.iter().all(|h| h.is_finished()));
        assert!(token.is_cancelled());
        assert!(st.query_tokens.is_empty() && st.query_tasks.is_empty() && st.connectivity_tasks.is_empty());
    }
```

- [ ] **Step 3: Run the tests to confirm they fail.** Run `cargo test -- features:: attachment:: encodings:: teardown_aborts`. Expected: compile errors for the missing modules and fields.

- [ ] **Step 4: Create `src/types/features.rs`.** Add `mod features; pub use features::*;` to `src/types/mod.rs`. The conversions follow the zenoh 1.10.1 enums:
  - `Priority`: https://docs.rs/zenoh/1.10.1/zenoh/qos/enum.Priority.html
  - `CongestionControl`: https://docs.rs/zenoh/1.10.1/zenoh/qos/enum.CongestionControl.html. `BlockFirst` exists only with `unstable`, which is on.
  - `Reliability`: https://docs.rs/zenoh/1.10.1/zenoh/qos/enum.Reliability.html
  - `QueryTarget`: https://docs.rs/zenoh/1.10.1/zenoh/query/enum.QueryTarget.html
  - `ConsolidationMode`: https://docs.rs/zenoh/1.10.1/zenoh/query/enum.ConsolidationMode.html

  None of these enums is `#[non_exhaustive]` in 1.10.1, so the matches are exhaustive.

```rust
//! Request, event and option types for the P5 explorer features.
//!
//! Every feature lane compiles against these definitions and fills in the
//! behaviour in its own module. Some fields are read only once the owning
//! lane has landed, hence the module-level `dead_code` allowance.
#![allow(dead_code)]

use chrono::{DateTime, Utc};

/// Identifier the UI assigns to one admin query or data query.
pub type RequestId = u64;

/// Publication priority, mirroring `zenoh::qos::Priority`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PriorityView {
    RealTime,
    InteractiveHigh,
    InteractiveLow,
    DataHigh,
    #[default]
    Data,
    DataLow,
    Background,
}

impl PriorityView {
    pub const ALL: [PriorityView; 7] = [
        PriorityView::RealTime,
        PriorityView::InteractiveHigh,
        PriorityView::InteractiveLow,
        PriorityView::DataHigh,
        PriorityView::Data,
        PriorityView::DataLow,
        PriorityView::Background,
    ];

    pub fn label(self) -> &'static str {
        match self {
            PriorityView::RealTime => "Real-time",
            PriorityView::InteractiveHigh => "Interactive high",
            PriorityView::InteractiveLow => "Interactive low",
            PriorityView::DataHigh => "Data high",
            PriorityView::Data => "Data (default)",
            PriorityView::DataLow => "Data low",
            PriorityView::Background => "Background",
        }
    }
}

impl From<PriorityView> for zenoh::qos::Priority {
    fn from(p: PriorityView) -> Self {
        use zenoh::qos::Priority as P;
        match p {
            PriorityView::RealTime => P::RealTime,
            PriorityView::InteractiveHigh => P::InteractiveHigh,
            PriorityView::InteractiveLow => P::InteractiveLow,
            PriorityView::DataHigh => P::DataHigh,
            PriorityView::Data => P::Data,
            PriorityView::DataLow => P::DataLow,
            PriorityView::Background => P::Background,
        }
    }
}

impl From<zenoh::qos::Priority> for PriorityView {
    fn from(p: zenoh::qos::Priority) -> Self {
        use zenoh::qos::Priority as P;
        match p {
            P::RealTime => PriorityView::RealTime,
            P::InteractiveHigh => PriorityView::InteractiveHigh,
            P::InteractiveLow => PriorityView::InteractiveLow,
            P::DataHigh => PriorityView::DataHigh,
            P::Data => PriorityView::Data,
            P::DataLow => PriorityView::DataLow,
            P::Background => PriorityView::Background,
        }
    }
}

/// Congestion control. The explorer's historical default is `Block`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CongestionView {
    Drop,
    #[default]
    Block,
    /// Unstable in zenoh 1.10: blocks for the first message only.
    BlockFirst,
}

impl CongestionView {
    pub const ALL: [CongestionView; 3] = [CongestionView::Drop, CongestionView::Block, CongestionView::BlockFirst];

    pub fn label(self) -> &'static str {
        match self {
            CongestionView::Drop => "Drop",
            CongestionView::Block => "Block",
            CongestionView::BlockFirst => "Block first (unstable)",
        }
    }
}

impl From<CongestionView> for zenoh::qos::CongestionControl {
    fn from(c: CongestionView) -> Self {
        use zenoh::qos::CongestionControl as C;
        match c {
            CongestionView::Drop => C::Drop,
            CongestionView::Block => C::Block,
            CongestionView::BlockFirst => C::BlockFirst,
        }
    }
}

impl From<zenoh::qos::CongestionControl> for CongestionView {
    fn from(c: zenoh::qos::CongestionControl) -> Self {
        use zenoh::qos::CongestionControl as C;
        match c {
            C::Drop => CongestionView::Drop,
            C::Block => CongestionView::Block,
            C::BlockFirst => CongestionView::BlockFirst,
        }
    }
}

/// Reliability marker (unstable in zenoh 1.10; it selects links, it does not retransmit).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ReliabilityView {
    #[default]
    Reliable,
    BestEffort,
}

impl From<ReliabilityView> for zenoh::qos::Reliability {
    fn from(r: ReliabilityView) -> Self {
        match r {
            ReliabilityView::Reliable => zenoh::qos::Reliability::Reliable,
            ReliabilityView::BestEffort => zenoh::qos::Reliability::BestEffort,
        }
    }
}

impl From<zenoh::qos::Reliability> for ReliabilityView {
    fn from(r: zenoh::qos::Reliability) -> Self {
        match r {
            zenoh::qos::Reliability::Reliable => ReliabilityView::Reliable,
            zenoh::qos::Reliability::BestEffort => ReliabilityView::BestEffort,
        }
    }
}

/// PUT or DELETE from the Publish form.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum PublishKind {
    #[default]
    Put,
    Delete,
}

/// What to send as a Zenoh attachment.
#[derive(Debug, Clone, PartialEq, Default)]
pub enum AttachmentSpec {
    #[default]
    None,
    /// UTF-8 text sent as raw bytes.
    Text(String),
    /// Pairs serialized with `zenoh_ext::z_serialize::<Vec<(String, String)>>`,
    /// the cross-binding convention for key/value attachments.
    KeyValue(Vec<(String, String)>),
}

/// Options chosen on the Publish form.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct PublishOptions {
    pub kind: PublishKind,
    pub priority: PriorityView,
    pub congestion: CongestionView,
    pub express: bool,
    pub reliability: ReliabilityView,
    pub attachment: AttachmentSpec,
}

/// Query target, mirroring `zenoh::query::QueryTarget`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum QueryTargetView {
    #[default]
    BestMatching,
    All,
    AllComplete,
}

impl QueryTargetView {
    pub const ALL: [QueryTargetView; 3] = [QueryTargetView::BestMatching, QueryTargetView::All, QueryTargetView::AllComplete];

    pub fn label(self) -> &'static str {
        match self {
            QueryTargetView::BestMatching => "Best matching",
            QueryTargetView::All => "All",
            QueryTargetView::AllComplete => "All complete",
        }
    }
}

impl From<QueryTargetView> for zenoh::query::QueryTarget {
    fn from(t: QueryTargetView) -> Self {
        match t {
            QueryTargetView::BestMatching => zenoh::query::QueryTarget::BestMatching,
            QueryTargetView::All => zenoh::query::QueryTarget::All,
            QueryTargetView::AllComplete => zenoh::query::QueryTarget::AllComplete,
        }
    }
}

/// Reply consolidation, mirroring `zenoh::query::ConsolidationMode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ConsolidationView {
    #[default]
    Auto,
    None,
    Monotonic,
    Latest,
}

impl ConsolidationView {
    pub const ALL: [ConsolidationView; 4] =
        [ConsolidationView::Auto, ConsolidationView::None, ConsolidationView::Monotonic, ConsolidationView::Latest];

    pub fn label(self) -> &'static str {
        match self {
            ConsolidationView::Auto => "Auto",
            ConsolidationView::None => "None",
            ConsolidationView::Monotonic => "Monotonic",
            ConsolidationView::Latest => "Latest",
        }
    }
}

impl From<ConsolidationView> for zenoh::query::ConsolidationMode {
    fn from(c: ConsolidationView) -> Self {
        use zenoh::query::ConsolidationMode as M;
        match c {
            ConsolidationView::Auto => M::Auto,
            ConsolidationView::None => M::None,
            ConsolidationView::Monotonic => M::Monotonic,
            ConsolidationView::Latest => M::Latest,
        }
    }
}

/// A query with every option the Query form exposes.
#[derive(Debug, Clone, PartialEq)]
pub struct QueryRequest {
    pub id: RequestId,
    pub selector: String,
    /// Optional query payload; `None` sends no payload.
    pub payload: Option<Vec<u8>>,
    /// Encoding of `payload`; ignored when `payload` is `None`.
    pub encoding: String,
    pub attachment: AttachmentSpec,
    pub target: QueryTargetView,
    pub consolidation: ConsolidationView,
    /// `accept_replies(ReplyKeyExpr::Any)`: accept replies on disjoint keys.
    pub accept_any_keyexpr: bool,
    pub timeout_ms: u64,
}

/// Sample metadata beyond key, payload and encoding. Boxed on `ZenohMessage`
/// and present only when something is non-default, to keep messages small.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct SampleExtras {
    pub attachment: Option<Vec<u8>>,
    pub priority: PriorityView,
    pub congestion: CongestionView,
    pub express: bool,
    pub reliability: ReliabilityView,
    /// `Reply::replier_id()` rendered as `<zid>:<eid>`.
    pub replier_id: Option<String>,
    pub query_id: Option<RequestId>,
}

/// One reply row of a query run (preview only; full bytes go to the store).
#[derive(Debug, Clone)]
pub struct ReplyRow {
    pub key: String,
    pub encoding: String,
    pub preview: String,
    pub size: usize,
    pub replier_id: Option<String>,
    pub attachment: Option<Vec<u8>>,
    pub received_at: DateTime<Utc>,
    /// True for `Err(ReplyError)`; `key` is then empty.
    pub is_error: bool,
}

/// Progress of a `QueryWithOptions` run.
#[derive(Debug, Clone)]
pub enum QueryEvent {
    Reply { id: RequestId, row: ReplyRow },
    Finished { id: RequestId, cancelled: bool },
}

/// One admin-space reply, kept raw (JSON, DOT text or metrics).
#[derive(Debug, Clone, PartialEq)]
pub struct AdminReply {
    pub key: String,
    pub encoding: String,
    pub payload: Vec<u8>,
}

/// A chunk of replies to one `AdminQuery`; `done` marks the last chunk.
#[derive(Debug, Clone)]
pub struct AdminBatch {
    pub id: RequestId,
    pub replies: Vec<AdminReply>,
    pub errors: Vec<String>,
    pub done: bool,
}

/// A liveliness token appeared (`alive`) or disappeared.
#[derive(Debug, Clone, PartialEq)]
pub struct LivelinessEvent {
    pub key: String,
    pub alive: bool,
    pub at: DateTime<Utc>,
}

/// `zenoh::session::Transport`, flattened for the UI.
#[derive(Debug, Clone, PartialEq)]
pub struct TransportView {
    pub zid: String,
    pub whatami: String,
    pub is_qos: bool,
    pub is_multicast: bool,
}

/// `zenoh::session::Link`, flattened for the UI.
#[derive(Debug, Clone, PartialEq)]
pub struct LinkView {
    pub zid: String,
    pub src: String,
    pub dst: String,
    pub mtu: u16,
    pub is_streamed: bool,
}

/// Connectivity of the explorer's own publishing session.
#[derive(Debug, Clone, PartialEq)]
pub enum ConnectivityEvent {
    /// Sent first; resets own-link state. Listener history then replays existing transports.
    Snapshot { own_zid: String },
    TransportOpened(TransportView),
    TransportClosed { zid: String },
    LinkAdded(LinkView),
    LinkRemoved { zid: String, src: String, dst: String },
}
```

- [ ] **Step 5: Extend commands, events and messages.**
  - In `src/types/commands.rs`:
    - Add `Admin, Liveliness, Connectivity, Matching` to `FailedOp`.
    - Add to `ZenohCommand`:

```rust
    /// Query the admin space (`@/…`) and stream replies as `ZenohEvent::Admin`.
    AdminQuery { id: RequestId, selector: String, timeout_ms: u64 },
    /// Subscribe to liveliness tokens (with history) on `key_expr`.
    StartLiveliness { key_expr: String },
    StopLiveliness,
    /// Stream own-session transports and links as `ZenohEvent::Connectivity`.
    StartConnectivity,
    StopConnectivity,
    /// Report whether `key` has remote subscribers; an empty key stops watching.
    WatchMatching { key: String },
    /// A query with the full option set; replies arrive as `ZenohEvent::Query`.
    QueryWithOptions(QueryRequest),
    /// Cancel a running `QueryWithOptions`.
    CancelQuery { id: RequestId },
```

    - Add `options: PublishOptions,` as the last field of `Publish`.
    - Add `config_file: Option<std::path::PathBuf>, tls_root_ca: Option<std::path::PathBuf>,` as the last fields of `Connect`.
    - Mark the legacy `Query { … }` variant `#[allow(dead_code)]` with the doc line `/// Legacy query without options; the UI sends `QueryWithOptions` after P5 T16.` so it stays warning-free once the UI stops constructing it.
    - Add to `ZenohEvent`:

```rust
    Admin(AdminBatch),
    Connectivity(ConnectivityEvent),
    Liveliness(LivelinessEvent),
    /// Matching status of the watched publish key.
    Matching { key: String, matching: bool },
    Query(QueryEvent),
```

    - In the manual `impl Debug for ZenohCommand`: change the `Publish` pattern to `ZenohCommand::Publish { key, payload, encoding, from_import, filename, .. }` and add the arms:

```rust
            ZenohCommand::AdminQuery { id, selector, .. } => f.debug_struct("AdminQuery").field("id", id).field("selector", selector).finish_non_exhaustive(),
            ZenohCommand::StartLiveliness { key_expr } => f.debug_struct("StartLiveliness").field("key_expr", key_expr).finish(),
            ZenohCommand::StopLiveliness => f.write_str("StopLiveliness"),
            ZenohCommand::StartConnectivity => f.write_str("StartConnectivity"),
            ZenohCommand::StopConnectivity => f.write_str("StopConnectivity"),
            ZenohCommand::WatchMatching { key } => f.debug_struct("WatchMatching").field("key", key).finish(),
            ZenohCommand::QueryWithOptions(req) => f
                .debug_struct("QueryWithOptions")
                .field("id", &req.id)
                .field("selector", &req.selector)
                .field("payload_len", &req.payload.as_ref().map(Vec::len))
                .finish_non_exhaustive(),
            ZenohCommand::CancelQuery { id } => f.debug_struct("CancelQuery").field("id", id).finish(),
```

    - Update P1's test `debug_output_omits_payload_bytes` so its `Publish` literal ends with `options: PublishOptions::default(),`.
  - In `src/types/message.rs`:
    - Add the field `pub extras: Option<Box<SampleExtras>>,` to `ZenohMessage` (doc: `/// Attachment, QoS and reply metadata when any is non-default.`).
    - Initialise it to `None` in `new_with_bytes`.
    - Add `+ self.extras.as_ref().map_or(0, |e| e.attachment.as_ref().map_or(0, Vec::len) + std::mem::size_of::<SampleExtras>())` to `calculate_size`.
    - Add:

```rust
    /// Attach sample extras (attachment, QoS, reply metadata).
    pub fn with_extras(mut self, extras: Option<Box<SampleExtras>>) -> Self {
        self.extras = extras;
        self
    }
```

    - Extend `DetailView` (pre-P1 `types.rs:398-405`). Derive `Debug, Clone, Copy, PartialEq, Eq`, then:

```rust
pub enum DetailView {
    TopicDetails,
    Publish,
    Query,
    Admin,
    Topology,
    Liveliness,
    Logs,
    Help,
}

impl DetailView {
    /// Tab order; Ctrl/Cmd+1..8 follow it.
    pub const TABS: [DetailView; 8] = [
        DetailView::TopicDetails,
        DetailView::Publish,
        DetailView::Query,
        DetailView::Admin,
        DetailView::Topology,
        DetailView::Liveliness,
        DetailView::Logs,
        DetailView::Help,
    ];

    /// Toolbar label (also the accessible name used by UI tests).
    pub fn label(self) -> &'static str {
        match self {
            DetailView::TopicDetails => "📊 Topics",
            DetailView::Publish => "📤 Publish",
            DetailView::Query => "🔍 Query",
            DetailView::Admin => "Admin",
            DetailView::Topology => "Topology",
            DetailView::Liveliness => "Liveliness",
            DetailView::Logs => "Logs",
            DetailView::Help => "❓ Help",
        }
    }
}
```

  - Fix every construction site of the changed variants. Run `git grep -n 'ZenohCommand::Publish {\|ZenohCommand::Connect {' src`. Append `options: PublishOptions::default(),` or `config_file: None, tls_root_ca: None,` at each site, including P1's tests (for example `reconnect_then_disconnect_leaves_no_discovery_updates`).

- [ ] **Step 6: Complete `src/attachment.rs` and `src/encodings.rs`** (above their tests):

```rust
//! Encoding and display of Zenoh attachments (publish, query and received samples).

use crate::types::AttachmentSpec;

/// Bytes to send for an attachment spec, or `None` when nothing is attached.
pub fn encode(spec: &AttachmentSpec) -> Option<Vec<u8>> {
    match spec {
        AttachmentSpec::None => None,
        AttachmentSpec::Text(s) => Some(s.as_bytes().to_vec()),
        AttachmentSpec::KeyValue(pairs) => Some(zenoh_ext::z_serialize(pairs).to_bytes().into_owned()),
    }
}

/// Key/value pairs when `bytes` is exactly the canonical `z_serialize` of a
/// `Vec<(String, String)>`. The re-encode check rejects accidental parses of text.
pub fn decode_key_values(bytes: &[u8]) -> Option<Vec<(String, String)>> {
    let zb = zenoh::bytes::ZBytes::from(bytes.to_vec());
    let pairs: Vec<(String, String)> = zenoh_ext::z_deserialize(&zb).ok()?;
    (zenoh_ext::z_serialize(&pairs).to_bytes().as_ref() == bytes).then_some(pairs)
}

/// One-line human description: `k=v, …`, text, or a hex preview.
pub fn describe(bytes: &[u8]) -> String {
    match decode_key_values(bytes) {
        Some(pairs) => pairs.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(", "),
        None => crate::payload::preview(bytes, 256),
    }
}
```

```rust
//! Encoding presets offered in the Publish and Query forms.

/// Common `zenoh::bytes::Encoding` presets, in menu order. Each string is the
/// canonical `Display` form of the matching `Encoding::…` constant (zenoh 1.10.1,
/// https://docs.rs/zenoh/1.10.1/zenoh/bytes/struct.Encoding.html).
pub const ENCODING_PRESETS: &[&str] = &[
    "zenoh/bytes",
    "zenoh/string",
    "zenoh/serialized",
    "text/plain",
    "application/json",
    "text/json",
    "application/cbor",
    "application/cdr",
    "application/protobuf",
    "application/yaml",
    "text/csv",
    "application/octet-stream",
    "image/png",
    "image/jpeg",
];

/// A "Presets" combo plus a free-text field bound to the same string.
/// Returns the text field's response.
pub fn encoding_combo(ui: &mut egui::Ui, id_salt: &str, value: &mut String) -> egui::Response {
    ui.horizontal(|ui| {
        egui::ComboBox::from_id_salt(id_salt).selected_text("Presets").show_ui(ui, |ui| {
            for p in ENCODING_PRESETS {
                ui.selectable_value(value, (*p).to_string(), *p);
            }
        });
        ui.add(
            egui::TextEdit::singleline(value)
                .desired_width(200.0)
                .hint_text("e.g. text/plain;utf-8"),
        )
    })
    .inner
}
```

- [ ] **Step 7: Worker state, stubs and dispatch.**
  - In `src/worker/state.rs`, add these fields to `WorkerState` (it stays `#[derive(Default)]`; `CancellationToken: Default`, see https://docs.rs/zenoh/1.10.1/zenoh/cancellation/struct.CancellationToken.html):

```rust
    /// Liveliness subscriber task (P5 T9).
    pub(crate) liveliness_task: Option<tokio::task::JoinHandle<()>>,
    /// Transport/link listener tasks (P5 T6).
    pub(crate) connectivity_tasks: Vec<tokio::task::JoinHandle<()>>,
    /// Matching-listener task for the watched publish key (P5 T12).
    pub(crate) matching_task: Option<tokio::task::JoinHandle<()>>,
    /// Cancellation tokens and reply tasks of running `QueryWithOptions` (P5 T14).
    pub(crate) query_tokens: std::collections::HashMap<crate::types::RequestId, zenoh::cancellation::CancellationToken>,
    pub(crate) query_tasks: std::collections::HashMap<crate::types::RequestId, tokio::task::JoinHandle<()>>,
```

  - At the top of `teardown()`, before any session is closed:

```rust
        if let Some(t) = self.liveliness_task.take() {
            t.abort();
        }
        for t in self.connectivity_tasks.drain(..) {
            t.abort();
        }
        if let Some(t) = self.matching_task.take() {
            t.abort();
        }
        for (_, token) in self.query_tokens.drain() {
            if let Err(e) = token.cancel().await {
                tracing::warn!("query cancel during teardown failed: {}", e);
            }
        }
        for (_, t) in self.query_tasks.drain() {
            t.abort();
        }
```

  - Add worker test support to `src/worker/state.rs`. Every worker loopback test in T3, T6, T9, T11, T12, T14 and T22 uses it:

```rust
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    use crate::types::ZenohEvent;
    use std::sync::{atomic::AtomicUsize, Arc, RwLock};
    use std::time::{Duration, Instant};

    /// Session with multicast scouting off and no listeners. `extra` holds
    /// `(config key, json5 value)` inserts, e.g. `("adminspace/enabled", "true")`.
    pub(crate) async fn session(extra: &[(&str, &str)]) -> zenoh::Session {
        let mut c = zenoh::Config::default();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap();
        for (k, v) in extra {
            c.insert_json5(k, v).unwrap();
        }
        zenoh::open(c).await.unwrap()
    }

    /// A worker context whose events land in the returned receiver.
    pub(crate) fn ctx() -> (WorkerCtx, std::sync::mpsc::Receiver<ZenohEvent>) {
        let (tx, rx) = crate::worker::pipeline::event_channel(10_000);
        let ctx = WorkerCtx {
            event_sender: tx,
            local_kvstore: Arc::new(RwLock::new(crate::types::LocalKvStore::new())),
            sample_drops: Arc::new(AtomicUsize::new(0)),
        };
        (ctx, rx)
    }

    /// Worker state whose publishing session is `s`.
    pub(crate) fn state_with(s: zenoh::Session) -> WorkerState {
        WorkerState { publishing_session: Some(Arc::new(s)), ..Default::default() }
    }

    /// Poll `rx` until an event matches `pred` or `secs` elapse (async-friendly).
    pub(crate) async fn wait_for(
        rx: &std::sync::mpsc::Receiver<ZenohEvent>,
        secs: u64,
        pred: impl Fn(&ZenohEvent) -> bool,
    ) -> Option<ZenohEvent> {
        let end = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < end {
            while let Ok(ev) = rx.try_recv() {
                if pred(&ev) {
                    return Some(ev);
                }
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        None
    }
}
```

    If P1–P4 gave `WorkerCtx` extra fields, initialise them in `ctx()` the same way `app/mod.rs` does.
  - Create the four worker stubs. Each starts with a `//!` line naming its lane task:
    - `src/worker/admin.rs`: `pub(crate) async fn handle_admin_query(_st: &WorkerState, _ctx: &WorkerCtx, _id: RequestId, _selector: String, _timeout_ms: u64) {}`
    - `src/worker/liveliness.rs`: `pub(crate) async fn handle_start(_st: &mut WorkerState, _ctx: &WorkerCtx, _key_expr: String) {}` and `pub(crate) fn handle_stop(st: &mut WorkerState) { if let Some(t) = st.liveliness_task.take() { t.abort(); } }`
    - `src/worker/connectivity.rs`: `pub(crate) async fn handle_start(_st: &mut WorkerState, _ctx: &WorkerCtx) {}` and `pub(crate) fn handle_stop(st: &mut WorkerState) { for t in st.connectivity_tasks.drain(..) { t.abort(); } }`
    - `src/worker/matching.rs`: `pub(crate) async fn handle_watch(_st: &mut WorkerState, _ctx: &WorkerCtx, _key: String) {}`
    - Each file uses `use super::state::{WorkerCtx, WorkerState}; use crate::types::*;` as needed.
  - In `src/worker/query.rs`, add stubs:

```rust
/// Query with options (filled by P5 T14).
pub(crate) async fn handle_query_with_options(_st: &mut WorkerState, _ctx: &WorkerCtx, _req: QueryRequest) {}

/// Cancel a running query (filled by P5 T14).
pub(crate) async fn handle_cancel(_st: &mut WorkerState, _ctx: &WorkerCtx, _id: RequestId) {}
```

  - In `src/worker/publish.rs`, give `handle_publish` a last parameter `_options: PublishOptions`. The body is unchanged.
  - In `src/worker/connect.rs`, give `connect_zenoh` and `connect_zenoh_monitor` two last parameters, `_config_file: Option<&std::path::Path>, _tls_root_ca: Option<&std::path::Path>`. `connect_zenoh` then takes `(locators, listen_port, mode, config_json, _config_file, _tls_root_ca)` and the monitor `(locators, listen_port, mode, _config_file, _tls_root_ca)`, P1 T10's parameters first. Do not add a monitor-port parameter: P1 T10 removed it. Append `None, None` to the `connect_zenoh` and `connect_zenoh_monitor` calls in P1 T10's `connect.rs` tests (`peer_mode_unreachable_endpoint_fails` and `monitor_sees_third_party_samples`), or Step 13's `cargo test` does not compile. In `src/worker/session.rs`, give `handle_connect` the last parameters `config_file: Option<std::path::PathBuf>, tls_root_ca: Option<std::path::PathBuf>` and pass `config_file.as_deref(), tls_root_ca.as_deref()` to both connect calls.
  - In `src/worker/mod.rs`:
    - Add `pub mod admin; pub mod connectivity; pub mod liveliness; pub mod matching;`.
    - In the dispatch `match`, pass `config_file, tls_root_ca` through the `Connect` arm and `options` through the `Publish` arm, then add:

```rust
                    ZenohCommand::AdminQuery { id, selector, timeout_ms } => {
                        admin::handle_admin_query(&st, &ctx, id, selector, timeout_ms).await
                    }
                    ZenohCommand::StartLiveliness { key_expr } => liveliness::handle_start(&mut st, &ctx, key_expr).await,
                    ZenohCommand::StopLiveliness => liveliness::handle_stop(&mut st),
                    ZenohCommand::StartConnectivity => connectivity::handle_start(&mut st, &ctx).await,
                    ZenohCommand::StopConnectivity => connectivity::handle_stop(&mut st),
                    ZenohCommand::WatchMatching { key } => matching::handle_watch(&mut st, &ctx, key).await,
                    ZenohCommand::QueryWithOptions(req) => query::handle_query_with_options(&mut st, &ctx, req).await,
                    ZenohCommand::CancelQuery { id } => query::handle_cancel(&mut st, &ctx, id).await,
```

- [ ] **Step 8: New pure-module stubs.** Create each file below with only a `//!` doc line, add `mod <name>;` for each to `src/main.rs`, and add the listed stub items. Every later task replaces its stub body.
  - `src/admin_model.rs`: `//! Admin-space key parsing and tree model (P5 T4).`
  - `src/topology_model.rs`: `//! Topology graph model and layout (P5 T7).`
  - `src/selector_params.rs`: `//! Selector parameter parsing and building (P5 T15).`
  - `src/decode.rs`: `//! Payload decoders for the viewer (P5 T17).`
  - `src/filter.rs`: `//! Topic-tree filter modes (P5 T19).`
  - `src/rates.rs`: `//! Per-topic message rates (P5 T21).`
  - `src/profiles.rs`: `//! Connection profiles (P5 T22).`
  - `src/logs.rs`: `//! In-app log capture (P5 T25).`
  - `src/tree_nav.rs`: its doc line, plus:

```rust
//! Keyboard navigation of the topic tree (P5 T20).

/// Keyboard-navigation state kept on the app.
#[derive(Debug, Default)]
pub struct TreeNavState {
    /// Set when the selection moved by keyboard; the row renderer scrolls to it once.
    pub scroll_to_selected: bool,
}
```

  - `src/query_book.rs`:

```rust
//! Query runs grouped by request id (P5 T16).

use crate::types::QueryEvent;

/// Results of `QueryWithOptions` runs, newest first.
#[derive(Debug, Default)]
pub struct QueryBook {}

impl QueryBook {
    /// Apply one worker event to the matching run.
    pub fn apply(&mut self, _ev: QueryEvent) {}
    /// End every running query after a disconnect (T16 fills this in).
    pub fn on_disconnected(&mut self) {}
}
```

  - `src/shortcuts.rs`:

```rust
//! Global keyboard shortcuts (P5 T24).

use crate::app::ZenohExplorer;

impl ZenohExplorer {
    /// Consume global shortcuts for this frame. Called before any panel is drawn.
    pub(crate) fn handle_shortcuts(&mut self, _ctx: &egui::Context) {}
}
```

- [ ] **Step 9: UI stubs and moves.**
  - In `src/ui/mod.rs`, add `pub mod admin; pub mod attachment_editor; pub mod connection; pub mod liveliness; pub mod logs; pub mod payload_viewer; pub mod topology;` and:

```rust
/// `egui::Id` source of the topic-tree filter field (focused by Ctrl/Cmd+F and Ctrl/Cmd+1).
pub const TREE_FILTER_ID: &str = "tree_filter";
/// `egui::Id` source of the Publish Key field (focused by Ctrl/Cmd+2).
pub const PUBLISH_KEY_ID: &str = "publish_key";
/// `egui::Id` source of the Query Selector field (focused by Ctrl/Cmd+3).
pub const QUERY_SELECTOR_ID: &str = "query_selector";
```

  - Create each tab stub with the same shape. For example, `src/ui/admin.rs`:

```rust
//! Admin-space tab (P5 T5).

use crate::app::ZenohExplorer;
use crate::types::AdminBatch;

/// State of the Admin tab.
#[derive(Debug, Default)]
pub struct AdminState {}

impl AdminState {
    /// Consume one batch of admin replies.
    pub fn on_batch(&mut self, _batch: AdminBatch) {}
    /// Reset in-flight state after a disconnect.
    pub fn on_disconnected(&mut self) {}
}

pub trait AdminUI {
    fn show_admin_tab(&mut self, ui: &mut egui::Ui);
}

impl AdminUI for ZenohExplorer {
    fn show_admin_tab(&mut self, ui: &mut egui::Ui) {
        ui.heading("Admin space");
    }
}
```

  - The other stubs follow the same pattern:
    - `src/ui/topology.rs`: `TopologyState` with `on_admin(&mut self, _batch: &AdminBatch)`, `on_connectivity(&mut self, _ev: ConnectivityEvent)` and `on_disconnected(&mut self)`; trait `TopologyUI::show_topology_tab`, heading "Topology".
    - `src/ui/liveliness.rs`: `LivelinessState` with `on_event(&mut self, _ev: LivelinessEvent)` and `on_disconnected(&mut self)`; trait `LivelinessUI::show_liveliness_tab`, heading "Liveliness".
    - `src/ui/logs.rs`: `LogsState` (no methods); trait `LogsUI::show_logs_tab`, heading "Logs".
    - `src/ui/attachment_editor.rs`: only `//! Attachment editor widget (P5 T13).`
  - Move the payload block. In `src/ui/topic_tree.rs::show_topic_details`, move the whole `if let Some(payload) = payload_opt { … }` block, including the "Encoding:" row (pre-P1 `topic_tree.rs:466-528`; leave P1 T15's DELETE and source-time rows in place), **verbatim** into `src/ui/payload_viewer.rs`:

```rust
//! Payload viewer (P5 T18). Initially the moved "Current Value" block.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::types::*;

/// State of the payload viewer.
#[derive(Debug, Default)]
pub struct PayloadViewerState {}

pub trait PayloadViewerUI {
    fn show_current_value(&mut self, ui: &mut egui::Ui, topic: &str, payload: Option<String>, encoding: Option<String>);
}

impl PayloadViewerUI for ZenohExplorer {
    fn show_current_value(&mut self, ui: &mut egui::Ui, topic: &str, payload_opt: Option<String>, encoding_opt: Option<String>) {
        // <moved block, unchanged; `topic` was `&String`, now `&str` — use `topic.to_string()` where the old code inserted `topic.clone()`>
    }
}
```

    Call it from the old position as `self.show_current_value(ui, topic, payload_opt, encoding_opt);`, which needs `use crate::ui::payload_viewer::PayloadViewerUI;`.
  - Move the connection panel. Move the Connect/Disconnect block from `src/app/layout.rs` (pre-P1 `app.rs:467-628`, from `// Compact connection panel in toolbar` to the end of the Disconnect `else` branch) **verbatim** into `src/ui/connection.rs` as `impl ConnectionUI for ZenohExplorer { fn show_connection_panel(&mut self, ui: &mut egui::Ui) { … } }`. Add `#[derive(Debug, Default)] pub struct ConnectionFormState {}` and the trait. The `ZenohCommand::Connect` literal there gains `config_file: None, tls_root_ca: None`. Call `self.show_connection_panel(ui);` at the old position.
  - In `src/ui/publish.rs`:
    - Add `#[derive(Debug, Default)] pub struct PublishFormState {}` with `pub fn on_matching(&mut self, _key: &str, _matching: bool) {}`.
    - The `ZenohCommand::Publish` literal gains `options: PublishOptions::default()`.
    - The Key field (pre-P1 `publish.rs:31`, `ui.text_edit_singleline(&mut self.publish_key)`, as P1 T14 and P3 T14 left it) becomes `ui.add(egui::TextEdit::singleline(&mut self.publish_key).id(egui::Id::new(crate::ui::PUBLISH_KEY_ID)))`. Keep any hint text or hover P1 gave it, and keep P3 T14's `.labelled_by(label.id)` on the response.
  - In `src/ui/query.rs`:
    - Add `#[derive(Debug, Default)] pub struct QueryFormState {}`.
    - The Selector field (pre-P1 `query.rs:63`, `ui.text_edit_singleline(&mut self.query_selector)`, as P1 T14 and P3 T14 left it) becomes `ui.add(egui::TextEdit::singleline(&mut self.query_selector).id(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)))`. Keep any hint text or hover P1 gave it, and keep P3 T14's `.labelled_by(label.id)` on the response.
  - In `src/ui/topic_tree.rs`:
    - The filter `TextEdit` (pre-P1 `topic_tree.rs:164`, `ui.text_edit_singleline(&mut self.tree_filter)`) becomes `ui.add(egui::TextEdit::singleline(&mut self.tree_filter).id(egui::Id::new(crate::ui::TREE_FILTER_ID)))`, keeping its `.on_hover_text`.
    - Extend `show_detail_panel` (pre-P1 `topic_tree.rs:300-307`) with the arms `DetailView::Admin => self.show_admin_tab(ui)`, `DetailView::Topology => self.show_topology_tab(ui)`, `DetailView::Liveliness => self.show_liveliness_tab(ui)` and `DetailView::Logs => self.show_logs_tab(ui)`, plus the trait imports.

- [ ] **Step 10: App fields, helpers, events and tabs.**
  - In `src/app/mod.rs`, add the fields listed in Interfaces, all initialised with `Default::default()` except `shortcut_submit: false` and `request_seq: 0`. Then add:

```rust
impl ZenohExplorer {
    /// Fresh id for an admin or data query.
    pub(crate) fn next_request_id(&mut self) -> crate::types::RequestId {
        self.request_seq += 1;
        self.request_seq
    }

    /// Send a command to the worker; false if the worker is gone.
    pub(crate) fn send_command(&self, cmd: ZenohCommand) -> bool {
        self.command_sender.as_ref().is_some_and(|s| s.send(cmd).is_ok())
    }
}

#[cfg(test)]
impl ZenohExplorer {
    /// Like `test_app`, plus a receiver that captures every command the UI sends.
    pub(crate) fn test_app_with_commands() -> (
        Self,
        std::sync::mpsc::Sender<ZenohEvent>,
        std::sync::mpsc::Receiver<ZenohCommand>,
    ) {
        let (mut app, tx) = Self::test_app();
        let (ctx, crx) = std::sync::mpsc::channel();
        app.command_sender = Some(ctx);
        (app, tx, crx)
    }
}
```

  - In `src/events/mod.rs`, add these arms to the `process_events` match:

```rust
                ZenohEvent::Admin(batch) => {
                    self.topology.on_admin(&batch);
                    self.admin.on_batch(batch);
                }
                ZenohEvent::Connectivity(ev) => self.topology.on_connectivity(ev),
                ZenohEvent::Liveliness(ev) => self.liveliness.on_event(ev),
                ZenohEvent::Matching { key, matching } => self.publish_form.on_matching(&key, matching),
                ZenohEvent::Query(ev) => self.query_book.apply(ev),
```

    In the `Disconnected` arm, add `self.admin.on_disconnected(); self.topology.on_disconnected(); self.liveliness.on_disconnected(); self.query_book.on_disconnected(); self.pending_subscribes.clear();` **outside** P1 T12's status guard:
    - P1 T12 wraps only the status and peer reset in `if !matches!(self.connection_status, ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor) { … }`. It keeps `self.queryable_enabled = false;` and P1 T21's query cancel unconditional, because the worker's teardown ran whichever way the guard goes. These five calls go with those unconditional lines, never inside the guard.
    - The reason: after a Disconnect followed quickly by Connect, the status is already `ConnectingPublishing` when this `Disconnected` event arrives. The worker's `teardown` has still aborted the query tasks and stopped the liveliness, connectivity and matching tasks, so no `Finished` event arrives for a run that was in flight. `QueryBook::on_disconnected` is what ends it (F-T16-11). Inside the guard, that run would stay "running" with a live Cancel button and never be marked "earlier session", and the Admin, Topology and Liveliness state would stay too. T16's `disconnect_while_reconnecting_ends_running_run` pins this.
    - P1 T21's `disconnect_cancels_waiting_query` covers the legacy `query_alert` path in the same arm; keep it.

    For T19's duplicate guard, also wire `pending_subscribes` into two P1 arms:
    - In P1 T12's `SubscriptionCreated { id, key_expr }` arm, add `self.pending_subscribes.remove(&key_expr);` as its first statement, before `key_expr` is moved.
    - In P1 T3's `OperationFailed` arm, clear it for a failed subscribe: add `FailedOp::Subscribe => self.pending_subscribes.clear(),` to its `match op`, before the `_ => {}` arm (or add the call to P1's `FailedOp::Subscribe` arm if one exists). The event carries no key, so every pending key is released and the user can retry.
  - In `src/app/layout.rs`:
    - Replace the four toolbar `selectable_label`s (pre-P1 `app.rs:661-696`) with:

```rust
                        for tab in DetailView::TABS {
                            if ui.selectable_label(self.detail_view == tab, tab.label()).clicked() {
                                self.detail_view = tab;
                            }
                        }
```

    - As the first statement of `fn ui`, add `self.handle_shortcuts(&ui.ctx().clone());`. P3 T4 moved event draining into `logic` → `tick()`; shortcuts belong in `ui` because they consume this pass's input before any panel is drawn.
  - In `src/main.rs`, inside the eframe creation closure, before the app is built, add `egui_extras::install_image_loaders(&cc.egui_ctx);`.

- [ ] **Step 11: Run the tests.** Run `cargo test -- features:: attachment:: encodings:: teardown_aborts`. Expected: PASS, 9 tests (4 in `types::features::tests`, 3 in `attachment::tests`, 1 in `encodings::tests` and `teardown_aborts_feature_tasks`). Then run `cargo test`. Expected: every P1–P4 test still passes.

- [ ] **Step 12: Module READMEs.** P3 T2/T3 created `src/README.md` and `src/ui/README.md` (purpose, responsibilities, interfaces, invariants, tests). Add one entry per new module, describing its **final** responsibility so the lanes do not have to edit the READMEs:
  - `src/README.md`: `admin_model`, `topology_model`, `selector_params`, `query_book`, `decode`, `filter`, `tree_nav`, `rates`, `profiles`, `shortcuts`, `logs`, `attachment`, `encodings`, and a "worker feature handlers" paragraph for `worker/{admin,connectivity,liveliness,matching}.rs`. That paragraph states the invariant that every long-lived feature task lives in `WorkerState` and is stopped by `teardown()`.
  - `src/ui/README.md`: `admin`, `topology`, `liveliness`, `logs`, `payload_viewer`, `connection` and `attachment_editor`, plus the rule that state indicators use a word or glyph and never colour alone.
  - Entry shape:

```markdown
- `admin_model.rs` — parses admin-space keys (`@/<zid>/<whatami>/<section>/…`) into a node/section tree; pure; tests `admin_model::` (fixture from zenohd 1.10.1).
```

- [ ] **Step 13: Verify.**

```bash
cargo build && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check
git grep -n 'todo!\|unimplemented!' src
cargo run   # click through all 8 tabs; Admin/Topology/Liveliness/Logs show their heading; Topics/Publish/Query behave as before
```

Expected: everything passes, and the grep prints nothing.

- [ ] **Step 14: Commit.**

```bash
git add Cargo.toml Cargo.lock src
git commit -m "feat(p5): scaffold feature types, commands, tabs, state and stub modules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T2: Record real admin-space keys from zenohd 1.10.1 (Lane ADM)

**Owns:** `examples/admin_dump.rs` (new), `tests/fixtures/admin-space/zenohd-1.10.1.jsonl` (new), `tests/fixtures/admin-space/README.md` (new)

**Why first:** zenoh 1.9 renamed the `@/<zid>/<mode>/linkstate/*` wildcard from peers and routers to **region ids** (`south:0:router`, `north`). See the 1.9.0 release notes (https://github.com/eclipse-zenoh/zenoh/releases/tag/1.9.0) and https://discourse.openrobotics.org/t/eclipse-zenoh-1-9-0-longwang-released-regions-quic-multistream-go-binding-rmw-zenoh-impact-inside/54135. The public manual (https://zenoh.io/docs/manual/abstractions/) documents only `@/<zid>/router`. The registry source of zenoh 1.10.1 (`src/net/runtime/adminspace.rs`, `AdminSpace::start`) registers these handlers:
- `@/<zid>/<whatami>`, node info as JSON;
- `metrics`;
- `linkstate/*`, a petgraph DOT `graph { … }` as `text/plain`;
- `subscriber/**`, `publisher/**`, `queryable/**`, `querier/**` and `token/**`, as JSON;
- `route/successor/**`;
- `plugins/**` and `status/plugins/**` (with plugins only);
- `config/**`.

The admin UI must be built against recorded reality, not the docs.

**Interfaces:** Produces the fixture file. Each line is one JSON object: `{"key": "@/…", "encoding": "…", "payload_utf8": "…"}`, or `"payload_hex"` for non-UTF-8 bytes. An error reply is `{"error": "…"}`. T4 and T7 `include_str!` it.

- [ ] **Step 1: Install zenohd 1.10.1.** Run `cargo install zenohd --version 1.10.1 --locked`, then `zenohd --version`. Expected: `1.10.1` in the output. Alternative: download `zenoh-1.10.1-<target>-standalone.zip` from https://github.com/eclipse-zenoh/zenoh/releases/tag/1.10.1 and use the `zenohd` binary inside.
- [ ] **Step 2: Check the CLI flags.** Run `zenohd --help`. Confirm that `--no-multicast-scouting`, `-l/--listen`, `-e/--connect`, `--adminspace-permissions` and `--rest-http-port` exist. If `--adminspace-permissions` is missing, use `--cfg='adminspace/enabled:true'` instead in Step 4.
- [ ] **Step 3: Write `examples/admin_dump.rs`:**

```rust
//! Dump every admin-space reply of a running zenohd as JSON Lines (P5 T2).
//!
//! Usage: `cargo run --example admin_dump -- tcp/127.0.0.1:7447 > tests/fixtures/admin-space/zenohd-1.10.1.jsonl`
//! The example declares a subscriber, a queryable and a liveliness token first,
//! so the fixture also contains the subscriber/queryable/token sections.

use std::time::Duration;

#[tokio::main]
async fn main() -> zenoh::Result<()> {
    let endpoint = std::env::args().nth(1).unwrap_or_else(|| "tcp/127.0.0.1:7447".to_string());
    let mut c = zenoh::Config::default();
    c.insert_json5("mode", "\"client\"")?;
    c.insert_json5("connect/endpoints", &format!("[\"{endpoint}\"]"))?;
    c.insert_json5("scouting/multicast/enabled", "false")?;
    let s = zenoh::open(c).await?;
    let _sub = s.declare_subscriber("demo/fixture/**").await?;
    let _qbl = s.declare_queryable("demo/fixture/q").await?;
    let _tok = s.liveliness().declare_token("demo/fixture/alive").await?;
    tokio::time::sleep(Duration::from_millis(500)).await;

    let replies = s
        .get("@/**")
        .target(zenoh::query::QueryTarget::All)
        .consolidation(zenoh::query::ConsolidationMode::None)
        .timeout(Duration::from_secs(5))
        .await?;
    while let Ok(reply) = replies.recv_async().await {
        let line = match reply.result() {
            Ok(sample) => {
                let bytes = sample.payload().to_bytes();
                let mut o = serde_json::json!({
                    "key": sample.key_expr().as_str(),
                    "encoding": sample.encoding().to_string(),
                });
                match std::str::from_utf8(&bytes) {
                    Ok(t) => o["payload_utf8"] = t.into(),
                    Err(_) => o["payload_hex"] = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>().into(),
                }
                o
            }
            Err(e) => serde_json::json!({ "error": String::from_utf8_lossy(&e.payload().to_bytes()) }),
        };
        println!("{line}");
    }
    s.close().await?;
    Ok(())
}
```

- [ ] **Step 4: Start two linked routers**, so that linkstate has an edge. Use two terminals:

```bash
zenohd --no-multicast-scouting -l tcp/127.0.0.1:7447 --adminspace-permissions r --rest-http-port none
zenohd --no-multicast-scouting -l tcp/127.0.0.1:7448 -e tcp/127.0.0.1:7447 --adminspace-permissions r --rest-http-port none
```

- [ ] **Step 5: Record the fixture.**

```bash
mkdir -p tests/fixtures/admin-space
cargo run --example admin_dump -- tcp/127.0.0.1:7447 > tests/fixtures/admin-space/zenohd-1.10.1.jsonl
F=tests/fixtures/admin-space/zenohd-1.10.1.jsonl
grep -cE '"key":"@/[0-9a-f]+/router"' $F        # node info: expect >= 2
grep -c '/linkstate/' $F                         # expect >= 1
grep -cE '/linkstate/(south:[0-9]+:[a-z]+|north|local)"' $F   # region ids: expect == previous count
grep -c '/subscriber/demo/fixture' $F            # expect >= 1
grep -c '/token/demo/fixture/alive' $F           # expect >= 1
grep -c '/metrics"' $F                           # expect >= 1
```

If a count is 0, re-run after waiting 2 s. Routing tables propagate asynchronously.
- [ ] **Step 6: Write `tests/fixtures/admin-space/README.md`.** Record:
  - the purpose ("recorded admin-space replies for P5 T4/T7 tests");
  - the output of `zenohd --version`;
  - the exact Step 4 and Step 5 commands;
  - the line format from Interfaces;
  - one sample line per section, copied from the file, with long payloads truncated;
  - a note that ZIDs are random per run, so tests must never hard-code them.
- [ ] **Step 7: Commit.**

```bash
git add examples/admin_dump.rs tests/fixtures/admin-space
git commit -m "test(admin): record zenohd 1.10.1 admin-space replies as a fixture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T3: Admin query worker (Lane ADM)

**Owns:** `src/worker/admin.rs`

**Interfaces:**
- Consumes (T1): `AdminBatch`, `AdminReply`, `WorkerState`, `WorkerCtx`, `test_support::{session, ctx, state_with, wait_for}`.
- Produces: `handle_admin_query(st, ctx, id, selector, timeout_ms)`. It spawns a task that sends `ZenohEvent::Admin` chunks of at most `ADMIN_BATCH = 64` replies, the last one with `done: true`. When there is no session, it sends a single `done` batch with the error `"not connected"`.
- zenoh API: `Session::get` with `.target(QueryTarget::All)`, `.consolidation(ConsolidationMode::None)` and `.timeout(..)` (https://docs.rs/zenoh/1.10.1/zenoh/session/struct.SessionGetBuilder.html); `Reply::result()` (https://docs.rs/zenoh/1.10.1/zenoh/query/struct.Reply.html).

- [ ] **Step 1: Write the failing tests** at the bottom of `src/worker/admin.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker::state::test_support::{ctx, session, state_with, wait_for};

    async fn collect(rx: &std::sync::mpsc::Receiver<ZenohEvent>, id: RequestId) -> (Vec<AdminReply>, Vec<String>) {
        let (mut replies, mut errors) = (Vec::new(), Vec::new());
        loop {
            let ev = wait_for(rx, 15, |e| matches!(e, ZenohEvent::Admin(b) if b.id == id))
                .await
                .expect("admin batch");
            let ZenohEvent::Admin(b) = ev else { unreachable!() };
            replies.extend(b.replies);
            errors.extend(b.errors);
            if b.done {
                return (replies, errors);
            }
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn admin_query_returns_own_node_info() {
        let s = session(&[("adminspace/enabled", "true")]).await;
        let zid = s.zid().to_string();
        let st = state_with(s);
        let (ctx, rx) = ctx();
        handle_admin_query(&st, &ctx, 7, "@/**".into(), 3000).await;
        let (replies, errors) = collect(&rx, 7).await;
        assert!(errors.is_empty(), "{errors:?}");
        let info = replies.iter().find(|r| r.key == format!("@/{zid}/peer")).expect("own node info");
        let json: serde_json::Value = serde_json::from_slice(&info.payload).unwrap();
        assert_eq!(json["zid"].as_str().unwrap(), zid);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn admin_query_without_session_reports_not_connected() {
        let st = WorkerState::default();
        let (ctx, rx) = ctx();
        handle_admin_query(&st, &ctx, 1, "@/**".into(), 100).await;
        let (_, errors) = collect(&rx, 1).await;
        assert_eq!(errors, vec!["not connected".to_string()]);
    }

    /// Needs a router:
    /// `zenohd --no-multicast-scouting -l tcp/127.0.0.1:7447 --adminspace-permissions r --rest-http-port none`
    /// (install: `cargo install zenohd --version 1.10.1 --locked`).
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "needs zenohd"]
    async fn admin_query_against_zenohd() {
        let s = session(&[("mode", "\"client\""), ("connect/endpoints", "[\"tcp/127.0.0.1:7447\"]")]).await;
        let st = state_with(s);
        let (ctx, rx) = ctx();
        handle_admin_query(&st, &ctx, 2, "@/*/router".into(), 3000).await;
        let (replies, _) = collect(&rx, 2).await;
        assert!(replies.iter().any(|r| r.key.ends_with("/router") && r.encoding.contains("json")));
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test worker::admin`. Expected: FAIL. The stub sends nothing, so `wait_for` returns `None` and `expect("admin batch")` panics.
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Admin-space queries (`@/…`) for the Admin and Topology tabs (P5 T3).
//!
//! `**` never matches chunks that start with `@` (verbatim chunks), so the
//! monitor's `**` subscription cannot see this data; it is fetched on demand.

use std::time::Duration;

use zenoh::query::{ConsolidationMode, QueryTarget};

use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

/// Replies per `ZenohEvent::Admin` chunk, so a router with thousands of
/// resources streams into the UI instead of arriving as one huge event.
pub(crate) const ADMIN_BATCH: usize = 64;

/// Run one admin query. The reply loop ends by itself at `timeout_ms`, or
/// when the session closes, so it is not tracked in `WorkerState`.
pub(crate) async fn handle_admin_query(
    st: &WorkerState,
    ctx: &WorkerCtx,
    id: RequestId,
    selector: String,
    timeout_ms: u64,
) {
    let tx = ctx.event_sender.clone();
    let Some(sess) = st.publishing_session.clone() else {
        let _ = tx.send(ZenohEvent::Admin(AdminBatch {
            id,
            replies: Vec::new(),
            errors: vec!["not connected".to_string()],
            done: true,
        }));
        return;
    };
    tokio::spawn(async move {
        let replies = match sess
            .get(&selector)
            .target(QueryTarget::All)
            .consolidation(ConsolidationMode::None)
            .timeout(Duration::from_millis(timeout_ms))
            .await
        {
            Ok(r) => r,
            Err(e) => {
                let _ = tx.send(ZenohEvent::Admin(AdminBatch {
                    id,
                    replies: Vec::new(),
                    errors: vec![format!("admin query failed: {e}")],
                    done: true,
                }));
                return;
            }
        };
        let mut batch = Vec::with_capacity(ADMIN_BATCH);
        let mut errors = Vec::new();
        while let Ok(reply) = replies.recv_async().await {
            match reply.result() {
                Ok(s) => batch.push(AdminReply {
                    key: s.key_expr().to_string(),
                    encoding: s.encoding().to_string(),
                    payload: s.payload().to_bytes().into_owned(),
                }),
                Err(e) => errors.push(crate::payload::preview(&e.payload().to_bytes(), 1024)),
            }
            if batch.len() >= ADMIN_BATCH {
                let chunk = std::mem::replace(&mut batch, Vec::with_capacity(ADMIN_BATCH));
                let ev = ZenohEvent::Admin(AdminBatch { id, replies: chunk, errors: std::mem::take(&mut errors), done: false });
                if tx.send(ev).is_err() {
                    return;
                }
            }
        }
        let _ = tx.send(ZenohEvent::Admin(AdminBatch { id, replies: batch, errors, done: true }));
    });
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test worker::admin`. Expected: 2 passed, 1 ignored.
  - **Fallback:** if `admin_query_returns_own_node_info` gets no reply from its own session, 1.10.1 does not route a session's `get` to its own admin space. In that case, change the test to two sessions: B with `adminspace/enabled=true` and `listen/endpoints=["tcp/127.0.0.1:27621"]`, and A connecting to it. Query `@/<B zid>/peer`, mark the test `#[ignore = "opens network sessions"]`, and state the finding in the commit body.
- [ ] **Step 5: Run the router test** (with zenohd from T2 Step 4 running): `cargo test -- --ignored admin_query_against_zenohd`. Expected: PASS.
- [ ] **Step 6: Commit.**

```bash
git add src/worker/admin.rs
git commit -m "feat(worker): admin-space query handler streaming batched replies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T4: Admin model (Lane ADM)

**Owns:** `src/admin_model.rs`

**Interfaces:**
- Consumes: `AdminReply` (T1) and the T2 fixture.
- Produces:
  - `enum AdminSection { Info, Metrics, Linkstate, Subscriber, Publisher, Queryable, Querier, Token, RouteSuccessor, Plugins, StatusPlugins, Config, Other }`, deriving `Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord`, with `label(self) -> &'static str`.
  - `struct AdminKey<'a> { zid: &'a str, whatami: &'a str, section: AdminSection, rest: String }`.
  - `fn parse_admin_key(key: &str) -> Option<AdminKey<'_>>`.
  - `fn is_region_id(s: &str) -> bool`.
  - `struct AdminNode { zid, whatami, sections: BTreeMap<AdminSection, Vec<AdminReply>> }`.
  - `struct AdminTree { nodes: BTreeMap<String, AdminNode>, unparsed: Vec<AdminReply> }`, with `insert`, `clear`, `is_empty`, `len` and `find(&str) -> Option<&AdminReply>`.
  - `fn entry_label(key: &str) -> String`.
  - `fn pretty_payload(encoding: &str, payload: &[u8]) -> String`.
  - `#[cfg(test)] pub(crate) fn fixture_replies() -> Vec<AdminReply>` (used by T7's tests).

- [ ] **Step 1: Write the failing tests** at the bottom of `src/admin_model.rs`:

```rust
#[cfg(test)]
pub(crate) const FIXTURE: &str = include_str!("../tests/fixtures/admin-space/zenohd-1.10.1.jsonl");

/// Replies recorded by P5 T2 (error lines skipped).
#[cfg(test)]
pub(crate) fn fixture_replies() -> Vec<AdminReply> {
    FIXTURE
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| {
            let v: serde_json::Value = serde_json::from_str(l).expect("fixture line is JSON");
            let key = v.get("key")?.as_str()?.to_string();
            let encoding = v["encoding"].as_str().unwrap_or_default().to_string();
            let payload = if let Some(t) = v.get("payload_utf8").and_then(|t| t.as_str()) {
                t.as_bytes().to_vec()
            } else {
                let h = v["payload_hex"].as_str().unwrap_or_default();
                (0..h.len()).step_by(2).map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap()).collect()
            };
            Some(AdminReply { key, encoding, payload })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sections() {
        let k = parse_admin_key("@/abc/router").unwrap();
        assert_eq!((k.zid, k.whatami, k.section, k.rest.as_str()), ("abc", "router", AdminSection::Info, ""));
        let k = parse_admin_key("@/abc/router/subscriber/demo/**").unwrap();
        assert_eq!((k.section, k.rest.as_str()), (AdminSection::Subscriber, "demo/**"));
        let k = parse_admin_key("@/abc/peer/route/successor/src/x/dst/y").unwrap();
        assert_eq!((k.section, k.rest.as_str()), (AdminSection::RouteSuccessor, "src/x/dst/y"));
        let k = parse_admin_key("@/abc/router/linkstate/south:0:router").unwrap();
        assert_eq!((k.section, k.rest.as_str()), (AdminSection::Linkstate, "south:0:router"));
        assert_eq!(parse_admin_key("demo/x"), None);
        assert_eq!(parse_admin_key("@/abc/robot"), None);
        assert_eq!(parse_admin_key("@//router"), None);
    }

    #[test]
    fn region_ids() {
        assert!(is_region_id("north") && is_region_id("local") && is_region_id("south:0:router"));
        assert!(!is_region_id("south:x:router") && !is_region_id("routers") && !is_region_id("south:0:robot"));
    }

    #[test]
    fn every_fixture_key_parses() {
        let replies = fixture_replies();
        assert!(!replies.is_empty());
        for r in &replies {
            let k = parse_admin_key(&r.key).unwrap_or_else(|| panic!("unparsed fixture key {}", r.key));
            assert_ne!(k.section, AdminSection::Other, "{}", r.key);
        }
    }

    #[test]
    fn linkstate_keys_use_region_ids() {
        let replies = fixture_replies();
        let ls: Vec<_> = replies
            .iter()
            .filter_map(|r| parse_admin_key(&r.key))
            .filter(|k| k.section == AdminSection::Linkstate)
            .collect();
        assert!(!ls.is_empty());
        assert!(ls.iter().all(|k| is_region_id(&k.rest)), "{:?}", ls.iter().map(|k| &k.rest).collect::<Vec<_>>());
    }

    #[test]
    fn fixture_builds_two_router_nodes_with_json_info() {
        let mut t = AdminTree::default();
        for r in fixture_replies() {
            t.insert(r);
        }
        let routers: Vec<_> = t.nodes.values().filter(|n| n.whatami == "router").collect();
        assert!(routers.len() >= 2);
        for n in routers {
            let info = &n.sections[&AdminSection::Info][0];
            assert!(pretty_payload(&info.encoding, &info.payload).contains("\"version\""));
        }
        assert!(t.unparsed.is_empty());
    }

    #[test]
    fn insert_replaces_same_key_and_find_works() {
        let mut t = AdminTree::default();
        let r = |p: &str| AdminReply { key: "@/a/router".into(), encoding: "application/json".into(), payload: p.as_bytes().to_vec() };
        t.insert(r("{\"v\":1}"));
        t.insert(r("{\"v\":2}"));
        assert_eq!(t.len(), 1);
        assert_eq!(t.find("@/a/router").unwrap().payload, b"{\"v\":2}".to_vec());
        t.insert(AdminReply { key: "odd".into(), encoding: String::new(), payload: vec![] });
        assert_eq!((t.len(), t.unparsed.len()), (2, 1));
        assert!(t.find("odd").is_some());
    }

    #[test]
    fn pretty_payload_json_gzip_and_text() {
        assert_eq!(pretty_payload("application/json", br#"{"a":1}"#), "{\n  \"a\": 1\n}");
        assert!(pretty_payload("text/plain;content-encoding=gzip", &[1, 2, 3]).starts_with("[gzip-compressed, 3 bytes]"));
        assert_eq!(pretty_payload("text/plain", b"graph {}"), "graph {}");
    }

    #[test]
    fn entry_labels() {
        assert_eq!(entry_label("@/a/router"), "(node info)");
        assert_eq!(entry_label("@/a/router/token/demo/alive"), "demo/alive");
        assert_eq!(entry_label("@/a/router/metrics"), "(metrics)");
        assert_eq!(entry_label("weird"), "weird");
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test admin_model::`. Expected: compile errors for the missing items.
- [ ] **Step 3: Implement** above the tests:

```rust
//! Admin-space key parsing and tree model (P5 T4).
//!
//! Layout served by zenoh 1.10.1 (`net/runtime/adminspace.rs`; linkstate keys
//! use region ids since 1.9):
//! - `@/<zid>/<whatami>`: node info (JSON: zid, version, locators, sessions…)
//! - `…/metrics`: OpenMetrics text, possibly gzip
//! - `…/linkstate/<region>`: petgraph DOT text; region = `north` | `local` | `south:<n>:<mode>`
//! - `…/subscriber|publisher|queryable|querier|token/<keyexpr>`: JSON
//! - `…/route/successor/…`, `…/plugins/…`, `…/status/plugins/…`, `…/config/…`

use std::collections::BTreeMap;

use crate::types::AdminReply;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum AdminSection {
    Info,
    Metrics,
    Linkstate,
    Subscriber,
    Publisher,
    Queryable,
    Querier,
    Token,
    RouteSuccessor,
    Plugins,
    StatusPlugins,
    Config,
    Other,
}

impl AdminSection {
    pub fn label(self) -> &'static str {
        match self {
            AdminSection::Info => "info",
            AdminSection::Metrics => "metrics",
            AdminSection::Linkstate => "linkstate",
            AdminSection::Subscriber => "subscribers",
            AdminSection::Publisher => "publishers",
            AdminSection::Queryable => "queryables",
            AdminSection::Querier => "queriers",
            AdminSection::Token => "tokens",
            AdminSection::RouteSuccessor => "route successors",
            AdminSection::Plugins => "plugins",
            AdminSection::StatusPlugins => "plugin status",
            AdminSection::Config => "config",
            AdminSection::Other => "other",
        }
    }
}

/// A parsed admin key. `rest` is the part after the section prefix.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdminKey<'a> {
    pub zid: &'a str,
    pub whatami: &'a str,
    pub section: AdminSection,
    pub rest: String,
}

pub fn parse_admin_key(key: &str) -> Option<AdminKey<'_>> {
    let mut parts = key.split('/');
    if parts.next()? != "@" {
        return None;
    }
    let zid = parts.next().filter(|z| !z.is_empty())?;
    let whatami = parts.next()?;
    if !matches!(whatami, "router" | "peer" | "client") {
        return None;
    }
    let rest: Vec<&str> = parts.collect();
    let (section, skip) = match rest.as_slice() {
        [] => (AdminSection::Info, 0),
        ["metrics", ..] => (AdminSection::Metrics, 1),
        ["linkstate", ..] => (AdminSection::Linkstate, 1),
        ["subscriber", ..] => (AdminSection::Subscriber, 1),
        ["publisher", ..] => (AdminSection::Publisher, 1),
        ["queryable", ..] => (AdminSection::Queryable, 1),
        ["querier", ..] => (AdminSection::Querier, 1),
        ["token", ..] => (AdminSection::Token, 1),
        ["route", "successor", ..] => (AdminSection::RouteSuccessor, 2),
        ["status", "plugins", ..] => (AdminSection::StatusPlugins, 2),
        ["plugins", ..] => (AdminSection::Plugins, 1),
        ["config", ..] => (AdminSection::Config, 1),
        _ => (AdminSection::Other, 0),
    };
    Some(AdminKey { zid, whatami, section, rest: rest[skip..].join("/") })
}

/// `north`, `local` or `south:<u16>:<router|peer|client>` (zenoh-protocol 1.10 `Region` Display).
pub fn is_region_id(s: &str) -> bool {
    match s {
        "north" | "local" => true,
        _ => {
            let p: Vec<&str> = s.split(':').collect();
            p.len() == 3 && p[0] == "south" && p[1].parse::<u16>().is_ok() && matches!(p[2], "router" | "peer" | "client")
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct AdminNode {
    pub zid: String,
    pub whatami: String,
    /// Entries per section, sorted by key.
    pub sections: BTreeMap<AdminSection, Vec<AdminReply>>,
}

/// Admin replies grouped node → section → entry.
#[derive(Debug, Clone, Default)]
pub struct AdminTree {
    pub nodes: BTreeMap<String, AdminNode>,
    pub unparsed: Vec<AdminReply>,
}

impl AdminTree {
    /// Insert or replace (same key) one reply.
    pub fn insert(&mut self, r: AdminReply) {
        let Some(k) = parse_admin_key(&r.key) else {
            self.unparsed.push(r);
            return;
        };
        let (zid, whatami, section) = (k.zid.to_string(), k.whatami.to_string(), k.section);
        let node = self.nodes.entry(zid.clone()).or_insert_with(|| AdminNode { zid, whatami, sections: BTreeMap::new() });
        let list = node.sections.entry(section).or_default();
        match list.binary_search_by(|e| e.key.cmp(&r.key)) {
            Ok(i) => list[i] = r,
            Err(i) => list.insert(i, r),
        }
    }

    pub fn clear(&mut self) {
        *self = Self::default();
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty() && self.unparsed.is_empty()
    }

    pub fn len(&self) -> usize {
        self.unparsed.len() + self.nodes.values().flat_map(|n| n.sections.values()).map(Vec::len).sum::<usize>()
    }

    pub fn find(&self, key: &str) -> Option<&AdminReply> {
        match parse_admin_key(key) {
            Some(k) => self.nodes.get(k.zid)?.sections.get(&k.section)?.iter().find(|e| e.key == key),
            None => self.unparsed.iter().find(|e| e.key == key),
        }
    }
}

/// Short label for an entry row inside its section.
pub fn entry_label(key: &str) -> String {
    match parse_admin_key(key) {
        Some(k) if k.section == AdminSection::Info => "(node info)".to_string(),
        Some(k) if k.rest.is_empty() => format!("({})", k.section.label()),
        Some(k) => k.rest,
        None => key.to_string(),
    }
}

/// JSON pretty-printed, gzip announced, anything else as a bounded preview.
pub fn pretty_payload(encoding: &str, payload: &[u8]) -> String {
    if encoding.contains("gzip") {
        return format!("[gzip-compressed, {} bytes]", payload.len());
    }
    if let Ok(v) = serde_json::from_slice::<serde_json::Value>(payload) {
        if let Ok(p) = serde_json::to_string_pretty(&v) {
            return p;
        }
    }
    crate::payload::preview(payload, 256 * 1024)
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test admin_model::`. Expected: 8 passed. If `every_fixture_key_parses` fails on a section this parser does not know, zenoh added a handler after 1.10.1's source was read. Add a match arm for it (and a `label`) rather than weakening the test.
- [ ] **Step 5: Commit.**

```bash
git add src/admin_model.rs
git commit -m "feat(admin): admin-space key parser and tree model, validated on recorded zenohd replies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T5: Admin tab (Lane ADM)

**Owns:** `src/ui/admin.rs`

**Interfaces:**
- Consumes:
  - T4: `admin_model::{AdminTree, AdminSection, entry_label, pretty_payload}`.
  - T1: `ZenohCommand::AdminQuery`, `next_request_id`, `send_command`, `test_app_with_commands`.
  - P1: `validation::selector_error`.
- Produces: `AdminState { selector, request_id, in_flight, tree, selected, errors, last_refresh }`, with `on_batch` / `on_disconnected` implemented, plus `ADMIN_PRESETS` and `ADMIN_TIMEOUT_MS`.
- egui APIs, verified in the 0.36.2 source:
  - `egui::Panel::left(id).resizable(..).default_size(..).show_inside(ui, ..)` (https://docs.rs/egui/0.36.2/egui/containers/panel/struct.Panel.html)
  - `CollapsingHeader::new(..).id_salt(..).default_open(..)`
  - `Context::copy_text` (https://docs.rs/egui/0.36.2/egui/struct.Context.html#method.copy_text)

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/admin.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    fn info(zid: &str) -> AdminReply {
        AdminReply {
            key: format!("@/{zid}/router"),
            encoding: "application/json".into(),
            payload: br#"{"zid":"aa11","version":"1.10.1"}"#.to_vec(),
        }
    }

    #[test]
    fn admin_refresh_sends_admin_query() {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_admin_tab(ui), app);
        h.run();
        h.get_by_label("Refresh").click();
        h.run();
        let cmd = cmds.try_iter().find(|c| matches!(c, ZenohCommand::AdminQuery { .. })).expect("AdminQuery sent");
        let ZenohCommand::AdminQuery { id, selector, .. } = cmd else { unreachable!() };
        assert_eq!(selector, "@/**");
        assert_eq!(h.state().admin.request_id, Some(id));
        assert!(h.state().admin.in_flight);
    }

    #[test]
    fn admin_tab_shows_nodes_and_pretty_json() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.admin.request_id = Some(1);
        app.admin.on_batch(AdminBatch { id: 1, replies: vec![info("aa11")], errors: vec![], done: true });
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_admin_tab(ui), app);
        h.run();
        h.get_by_label("aa11 (router)");
        h.get_by_label("(node info)").click();
        h.run();
        h.get_by_label_contains("\"version\": \"1.10.1\"");
        assert_eq!(h.state().admin.selected.as_deref(), Some("@/aa11/router"));
    }

    #[test]
    fn admin_ignores_stale_batches() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.admin.request_id = Some(2);
        app.admin.on_batch(AdminBatch { id: 1, replies: vec![info("aa11")], errors: vec![], done: true });
        assert!(app.admin.tree.is_empty());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::admin`. Expected: compile errors, because the `AdminState` fields do not exist yet.
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Admin tab: browse the Zenoh admin space (`@/…`), which the `**` monitor
//! subscription never sees because `**` does not match `@` chunks.

use chrono::{DateTime, Utc};
use egui::RichText;

use crate::admin_model::{self, AdminTree};
use crate::app::ZenohExplorer;
use crate::types::*;

/// Selectors offered in the Presets menu.
pub const ADMIN_PRESETS: &[(&str, &str)] = &[
    ("Everything", "@/**"),
    ("Node info", "@/*/*"),
    ("Link state", "@/*/*/linkstate/*"),
    ("Liveliness tokens", "@/*/*/token/**"),
    ("Subscribers", "@/*/*/subscriber/**"),
    ("Queryables", "@/*/*/queryable/**"),
];

/// Admin queries wait this long for replies.
pub const ADMIN_TIMEOUT_MS: u64 = 3_000;

#[derive(Debug)]
pub struct AdminState {
    pub selector: String,
    pub request_id: Option<RequestId>,
    pub in_flight: bool,
    pub tree: AdminTree,
    pub selected: Option<String>,
    pub errors: Vec<String>,
    pub last_refresh: Option<DateTime<Utc>>,
}

impl Default for AdminState {
    fn default() -> Self {
        Self {
            selector: "@/**".to_string(),
            request_id: None,
            in_flight: false,
            tree: AdminTree::default(),
            selected: None,
            errors: Vec::new(),
            last_refresh: None,
        }
    }
}

impl AdminState {
    /// Consume one batch; batches of an older request are ignored.
    pub fn on_batch(&mut self, batch: AdminBatch) {
        if self.request_id != Some(batch.id) {
            return;
        }
        for r in batch.replies {
            self.tree.insert(r);
        }
        self.errors.extend(batch.errors);
        if batch.done {
            self.in_flight = false;
            self.last_refresh = Some(Utc::now());
        }
    }

    pub fn on_disconnected(&mut self) {
        self.in_flight = false;
        self.request_id = None;
    }

    fn begin(&mut self, id: RequestId) {
        self.request_id = Some(id);
        self.in_flight = true;
        self.tree.clear();
        self.errors.clear();
        self.selected = None;
    }
}

pub trait AdminUI {
    fn show_admin_tab(&mut self, ui: &mut egui::Ui);
}

impl AdminUI for ZenohExplorer {
    fn show_admin_tab(&mut self, ui: &mut egui::Ui) {
        let connected = matches!(self.connection_status, ConnectionStatus::Connected);
        ui.horizontal(|ui| {
            ui.label("Selector:");
            ui.add(egui::TextEdit::singleline(&mut self.admin.selector).desired_width(260.0));
            egui::ComboBox::from_id_salt("admin_presets").selected_text("Presets").show_ui(ui, |ui| {
                for (name, sel) in ADMIN_PRESETS {
                    if ui.selectable_label(self.admin.selector == *sel, *name).clicked() {
                        self.admin.selector = (*sel).to_string();
                    }
                }
            });
            let selector_err = crate::validation::selector_error(&self.admin.selector);
            let enabled = connected && selector_err.is_none() && !self.admin.in_flight;
            if ui.add_enabled(enabled, egui::Button::new("Refresh")).clicked() {
                let id = self.next_request_id();
                self.admin.begin(id);
                self.send_command(ZenohCommand::AdminQuery {
                    id,
                    selector: self.admin.selector.clone(),
                    timeout_ms: ADMIN_TIMEOUT_MS,
                });
            }
            if self.admin.in_flight {
                ui.spinner();
                ui.label("Querying…");
            } else if let Some(t) = self.admin.last_refresh {
                let at = crate::types::format_local_time(&t, &chrono::Utc::now()); // local time (F-T14-6)
                ui.label(format!("{} entries · refreshed {at}", self.admin.tree.len()));
            }
            if let Some(e) = selector_err {
                ui.label(format!("⚠ {e}"));
            }
        });
        for e in &self.admin.errors {
            ui.label(format!("⚠ reply error: {e}"));
        }
        ui.separator();

        let mut clicked: Option<String> = None;
        egui::Panel::left("admin_tree_panel").resizable(true).default_size(340.0).show_inside(ui, |ui| {
            egui::ScrollArea::vertical().id_salt("admin_tree_scroll").auto_shrink([false; 2]).show(ui, |ui| {
                if self.admin.tree.is_empty() {
                    ui.label(if connected { "No admin data yet. Press Refresh." } else { "Connect, then press Refresh." });
                }
                for node in self.admin.tree.nodes.values() {
                    egui::CollapsingHeader::new(format!("{} ({})", node.zid, node.whatami))
                        .id_salt(("admin_node", &node.zid))
                        .default_open(true)
                        .show(ui, |ui| {
                            for (section, entries) in &node.sections {
                                egui::CollapsingHeader::new(format!("{} ({})", section.label(), entries.len()))
                                    .id_salt(("admin_section", &node.zid, *section))
                                    .default_open(*section == admin_model::AdminSection::Info)
                                    .show(ui, |ui| {
                                        for e in entries {
                                            let sel = self.admin.selected.as_deref() == Some(e.key.as_str());
                                            if ui.selectable_label(sel, admin_model::entry_label(&e.key)).clicked() {
                                                clicked = Some(e.key.clone());
                                            }
                                        }
                                    });
                            }
                        });
                }
                if !self.admin.tree.unparsed.is_empty() {
                    ui.collapsing(format!("other keys ({})", self.admin.tree.unparsed.len()), |ui| {
                        for e in &self.admin.tree.unparsed {
                            if ui.selectable_label(self.admin.selected.as_deref() == Some(e.key.as_str()), &e.key).clicked() {
                                clicked = Some(e.key.clone());
                            }
                        }
                    });
                }
            });
        });
        if clicked.is_some() {
            self.admin.selected = clicked;
        }

        match self.admin.selected.as_deref().and_then(|k| self.admin.tree.find(k)) {
            Some(entry) => {
                let text = admin_model::pretty_payload(&entry.encoding, &entry.payload);
                ui.horizontal(|ui| {
                    ui.label(RichText::new(&entry.key).strong());
                    ui.label(format!("· {} · {} bytes", entry.encoding, entry.payload.len()));
                    if ui.button("Copy").clicked() {
                        ui.ctx().copy_text(text.clone());
                    }
                });
                egui::ScrollArea::both().id_salt("admin_payload_scroll").auto_shrink([false; 2]).show(ui, |ui| {
                    ui.label(RichText::new(text).monospace());
                });
            }
            None => {
                ui.label("Select an entry to see its payload.");
            }
        }
    }
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test ui::admin`. Expected: 3 passed.
- [ ] **Step 5: Manual check** (zenohd from T2 running): run `cargo run`, connect in client mode to `tcp/127.0.0.1:7447`, open Admin and press Refresh. Both routers appear. Open linkstate: the DOT text is shown. Open a subscriber entry: the JSON is pretty-printed.
- [ ] **Step 6: Commit.**

```bash
git add src/ui/admin.rs
git commit -m "feat(ui): Admin tab browsing @/** with node/section tree and pretty JSON

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T6: Connectivity worker (Lane TOP)

**Owns:** `src/worker/connectivity.rs`

**Interfaces:**
- Consumes (T1): `ConnectivityEvent`, `TransportView`, `LinkView`, `FailedOp::Connectivity`, `WorkerState.connectivity_tasks`, `test_support`.
- Produces: `handle_start` and `handle_stop`. `handle_start` sends `Snapshot { own_zid }` first. It then streams `TransportOpened` / `TransportClosed` / `LinkAdded` / `LinkRemoved` from listeners declared with `history(true)`, so existing transports replay as `Put` events and no separate snapshot is racy.
- zenoh API (unstable, added in 1.8, verified in 1.10.1 `src/api/info.rs`):
  - `SessionInfo::transport_events_listener()` and `link_events_listener()`, with `.history(bool)`, `.await` and `recv_async()` (https://docs.rs/zenoh/1.10.1/zenoh/session/struct.SessionInfo.html#method.transport_events_listener)
  - `TransportEvent::{kind, transport}` and `LinkEvent::{kind, link}`
  - `Transport::{zid, whatami, is_qos, is_multicast}` and `Link::{zid, src, dst, mtu, is_streamed}`
  - `Transport::is_shm` exists only with the `shared-memory` feature, so it is not used.

- [ ] **Step 1: Write the failing tests** in `src/worker/connectivity.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker::state::test_support::{ctx, session, state_with, wait_for};

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn connectivity_snapshot_reports_own_zid() {
        let s = session(&[]).await;
        let zid = s.zid().to_string();
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_start(&mut st, &ctx).await;
        let ev = wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Connectivity(ConnectivityEvent::Snapshot { .. })))
            .await
            .expect("snapshot");
        assert_eq!(ev_own_zid(&ev), zid);
        assert_eq!(st.connectivity_tasks.len(), 2);
        handle_stop(&mut st);
        assert!(st.connectivity_tasks.is_empty());
    }

    fn ev_own_zid(ev: &ZenohEvent) -> String {
        match ev {
            ZenohEvent::Connectivity(ConnectivityEvent::Snapshot { own_zid }) => own_zid.clone(),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn connectivity_reports_peer_transport() {
        let b = session(&[("listen/endpoints", "[\"tcp/127.0.0.1:27611\"]")]).await;
        let b_zid = b.zid().to_string();
        let a = session(&[("connect/endpoints", "[\"tcp/127.0.0.1:27611\"]")]).await;
        let mut st = state_with(a);
        let (ctx, rx) = ctx();
        handle_start(&mut st, &ctx).await;
        let opened = wait_for(&rx, 10, |e| {
            matches!(e, ZenohEvent::Connectivity(ConnectivityEvent::TransportOpened(t)) if t.zid == b_zid)
        })
        .await;
        assert!(opened.is_some(), "no TransportOpened for peer");
        let link = wait_for(&rx, 10, |e| {
            matches!(e, ZenohEvent::Connectivity(ConnectivityEvent::LinkAdded(l)) if l.zid == b_zid)
        })
        .await;
        assert!(link.is_some(), "no LinkAdded for peer");
        drop(b);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test worker::connectivity`. Expected: FAIL (`expect("snapshot")` panics on the stub).
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Own-session connectivity (transports and links) for the Topology tab (P5 T6).

use zenoh::sample::SampleKind;

use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

fn transport_view(t: &zenoh::session::Transport) -> TransportView {
    TransportView {
        zid: t.zid().to_string(),
        whatami: t.whatami().to_string(),
        is_qos: t.is_qos(),
        is_multicast: t.is_multicast(),
    }
}

fn link_view(l: &zenoh::session::Link) -> LinkView {
    LinkView {
        zid: l.zid().to_string(),
        src: l.src().to_string(),
        dst: l.dst().to_string(),
        mtu: l.mtu(),
        is_streamed: l.is_streamed(),
    }
}

fn fail(ctx: &WorkerCtx, error: String) {
    let _ = ctx.event_sender.send(ZenohEvent::OperationFailed { op: FailedOp::Connectivity, error });
}

/// Start streaming own-session connectivity. Restarts cleanly if already running.
pub(crate) async fn handle_start(st: &mut WorkerState, ctx: &WorkerCtx) {
    handle_stop(st);
    let Some(sess) = st.publishing_session.clone() else {
        fail(ctx, "not connected".to_string());
        return;
    };
    let tx = ctx.event_sender.clone();
    let _ = tx.send(ZenohEvent::Connectivity(ConnectivityEvent::Snapshot { own_zid: sess.zid().to_string() }));

    let transports = match sess.info().transport_events_listener().history(true).await {
        Ok(l) => l,
        Err(e) => return fail(ctx, format!("transport listener: {e}")),
    };
    let links = match sess.info().link_events_listener().history(true).await {
        Ok(l) => l,
        Err(e) => return fail(ctx, format!("link listener: {e}")),
    };

    let t_tx = tx.clone();
    st.connectivity_tasks.push(tokio::spawn(async move {
        while let Ok(ev) = transports.recv_async().await {
            let out = match ev.kind() {
                SampleKind::Put => ConnectivityEvent::TransportOpened(transport_view(ev.transport())),
                SampleKind::Delete => ConnectivityEvent::TransportClosed { zid: ev.transport().zid().to_string() },
            };
            if t_tx.send(ZenohEvent::Connectivity(out)).is_err() {
                break;
            }
        }
    }));
    st.connectivity_tasks.push(tokio::spawn(async move {
        while let Ok(ev) = links.recv_async().await {
            let l = ev.link();
            let out = match ev.kind() {
                SampleKind::Put => ConnectivityEvent::LinkAdded(link_view(l)),
                SampleKind::Delete => ConnectivityEvent::LinkRemoved {
                    zid: l.zid().to_string(),
                    src: l.src().to_string(),
                    dst: l.dst().to_string(),
                },
            };
            if tx.send(ZenohEvent::Connectivity(out)).is_err() {
                break;
            }
        }
    }));
}

/// Stop the listeners; dropping them undeclares them.
pub(crate) fn handle_stop(st: &mut WorkerState) {
    for t in st.connectivity_tasks.drain(..) {
        t.abort();
    }
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test worker::connectivity && cargo test -- --ignored connectivity_reports_peer_transport`. Expected: 1 passed, then 1 passed.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/connectivity.rs
git commit -m "feat(worker): stream own-session transports and links via SessionInfo listeners

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T7: Topology model (Lane TOP)

**Owns:** `src/topology_model.rs`

**Interfaces:**
- Consumes:
  - T4: `admin_model::{parse_admin_key, AdminSection, fixture_replies}`.
  - T1: `AdminReply`, `ConnectivityEvent`.
- Produces:
  - `enum EdgeSource { Linkstate, Session, OwnLink }`, with `Ord`.
  - `type EdgeKey = (String, String)` and `fn edge_key(a, b) -> EdgeKey`, which orders the pair.
  - `fn parse_dot(dot: &str) -> (Vec<String>, Vec<(String, String)>)`, returning node labels and edges by label.
  - `struct AdminSnapshot { whatami: BTreeMap<String,String>, edges: BTreeMap<EdgeKey, BTreeSet<EdgeSource>> }`, with `ingest(&mut self, &AdminReply)`.
  - `struct TopoNode { zid, whatami: Option<String>, own: bool, pos: [f32; 2] }`, where `pos` is in 0..1.
  - `struct Topology { nodes, edges, own_zid, version }`, with `set_admin(AdminSnapshot)`, `apply_connectivity(&ConnectivityEvent)`, `layout(iterations)`, `node_count()`, `edge_count()` and `neighbours(&str) -> Vec<(String, BTreeSet<EdgeSource>)>`.
- **Data sources:**
  - The linkstate payload is `format!("{:?}", petgraph::dot::Dot::new(&graph))` over nodes whose `Debug` is the ZID. That makes it `graph { 0 [ label = "<zid>" ] … 0 -- 1 [ label = "<weight>" ] }` (zenoh 1.10.1 `src/net/protocol/network.rs::dot`).
  - Node info JSON carries `sessions[].peer` and `sessions[].whatami` (`adminspace.rs::local_data`).
  - The linkstate key's region (`south:<n>:<mode>`) gives the mode of the nodes in that graph.

- [ ] **Step 1: Write the failing tests** in `src/topology_model.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::TransportView;

    const DOT: &str = "graph {\n    0 [ label = \"aa\" ]\n    1 [ label = \"bb\" ]\n    0 -- 1 [ label = \"100.0\" ]\n}\n";

    fn snap(nodes: &[(&str, &str)], edges: &[(&str, &str)]) -> AdminSnapshot {
        let mut s = AdminSnapshot::default();
        for (z, w) in nodes {
            s.whatami.insert(z.to_string(), w.to_string());
        }
        for (a, b) in edges {
            s.edges.entry(edge_key(a, b)).or_default().insert(EdgeSource::Linkstate);
        }
        s
    }

    fn dist(t: &Topology, a: &str, b: &str) -> f32 {
        let (p, q) = (t.nodes[a].pos, t.nodes[b].pos);
        ((p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2)).sqrt()
    }

    #[test]
    fn parses_simple_dot() {
        let (nodes, edges) = parse_dot(DOT);
        assert_eq!(nodes, vec!["aa".to_string(), "bb".to_string()]);
        assert_eq!(edges, vec![("aa".to_string(), "bb".to_string())]);
        assert_eq!(parse_dot("graph {}"), (vec![], vec![]));
    }

    #[test]
    fn parses_fixture_linkstate() {
        let replies = crate::admin_model::fixture_replies();
        let zids: BTreeSet<String> = replies
            .iter()
            .filter_map(|r| parse_admin_key(&r.key).map(|k| k.zid.to_string()))
            .collect();
        let mut edge_total = 0;
        for r in replies.iter().filter(|r| r.key.contains("/linkstate/")) {
            let (_, edges) = parse_dot(&String::from_utf8_lossy(&r.payload));
            for (a, b) in &edges {
                assert!(zids.contains(a) && zids.contains(b), "edge {a}--{b} not among fixture zids");
            }
            edge_total += edges.len();
        }
        assert!(edge_total >= 1, "two linked routers must produce a linkstate edge");
    }

    #[test]
    fn snapshot_from_fixture_links_the_two_routers() {
        let mut s = AdminSnapshot::default();
        for r in crate::admin_model::fixture_replies() {
            s.ingest(&r);
        }
        let routers: Vec<&String> = s.whatami.iter().filter(|(_, w)| *w == "router").map(|(z, _)| z).collect();
        assert!(routers.len() >= 2);
        assert!(s.edges.contains_key(&edge_key(routers[0], routers[1])));
    }

    #[test]
    fn own_links_follow_connectivity_events() {
        let mut t = Topology::default();
        t.apply_connectivity(&ConnectivityEvent::Snapshot { own_zid: "o".into() });
        let tv = TransportView { zid: "r".into(), whatami: "router".into(), is_qos: true, is_multicast: false };
        t.apply_connectivity(&ConnectivityEvent::TransportOpened(tv));
        assert!(t.edges[&edge_key("o", "r")].contains(&EdgeSource::OwnLink));
        assert!(t.nodes["o"].own);
        assert_eq!(t.nodes["r"].whatami.as_deref(), Some("router"));
        t.apply_connectivity(&ConnectivityEvent::TransportClosed { zid: "r".into() });
        assert!(!t.edges.contains_key(&edge_key("o", "r")));
        assert!(!t.nodes.contains_key("r"));
    }

    #[test]
    fn layout_is_deterministic_and_finite() {
        let mut t = Topology::default();
        t.set_admin(snap(
            &[("a", "router"), ("b", "router"), ("c", "peer"), ("d", "client"), ("e", "peer")],
            &[("a", "b"), ("b", "c"), ("c", "a"), ("a", "d")],
        ));
        let mut u = t.clone();
        t.layout(200);
        u.layout(200);
        for (z, n) in &t.nodes {
            assert_eq!(n.pos, u.nodes[z].pos);
            assert!(n.pos.iter().all(|v| v.is_finite() && (0.05..=0.95).contains(v)), "{z}: {:?}", n.pos);
        }
    }

    #[test]
    fn connected_nodes_end_closer() {
        let mut t = Topology::default();
        t.set_admin(snap(&[("a", "router"), ("b", "router"), ("c", "peer"), ("d", "peer")], &[("a", "b")]));
        t.layout(300);
        assert!(dist(&t, "a", "b") < dist(&t, "a", "c"));
        assert!(dist(&t, "a", "b") < dist(&t, "b", "d"));
    }

    #[test]
    fn layout_handles_zero_and_one_node_and_keeps_positions() {
        let mut t = Topology::default();
        t.layout(10);
        t.set_admin(snap(&[("a", "router")], &[]));
        t.layout(10);
        assert_eq!(t.nodes["a"].pos, [0.5, 0.5]);
        t.set_admin(snap(&[("a", "router"), ("b", "peer")], &[("a", "b")]));
        assert_eq!(t.nodes["a"].pos, [0.5, 0.5], "rebuild keeps surviving positions");
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test topology_model::`. Expected: compile errors for the missing items.
- [ ] **Step 3: Implement** above the tests:

```rust
//! Topology graph from admin-space linkstate and node info plus own-session
//! connectivity, with a deterministic force-directed layout (P5 T7).

use std::collections::{BTreeMap, BTreeSet};

use crate::admin_model::{parse_admin_key, AdminSection};
use crate::types::{AdminReply, ConnectivityEvent};

/// Where an edge was learned; one edge can have several sources.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EdgeSource {
    /// Router link-state graph (`@/<zid>/<mode>/linkstate/<region>`).
    Linkstate,
    /// A node's info `sessions` list.
    Session,
    /// A transport of this app's own publishing session.
    OwnLink,
}

/// Undirected edge key: the two ZIDs, smaller first.
pub type EdgeKey = (String, String);

pub fn edge_key(a: &str, b: &str) -> EdgeKey {
    if a <= b {
        (a.to_string(), b.to_string())
    } else {
        (b.to_string(), a.to_string())
    }
}

/// Parse petgraph `Dot` output: node labels and edges (by label).
pub fn parse_dot(dot: &str) -> (Vec<String>, Vec<(String, String)>) {
    let mut labels: BTreeMap<String, String> = BTreeMap::new();
    let mut raw_edges = Vec::new();
    for line in dot.lines().map(str::trim) {
        let (head, attrs) = match line.find('[') {
            Some(i) => (line[..i].trim(), &line[i..]),
            None => (line.trim_end_matches(';').trim(), ""),
        };
        if let Some((a, b)) = head.split_once("--").or_else(|| head.split_once("->")) {
            raw_edges.push((a.trim().to_string(), b.trim().to_string()));
        } else if !attrs.is_empty() && !head.is_empty() && head.chars().all(|c| c.is_ascii_alphanumeric()) {
            labels.insert(head.to_string(), label_of(attrs).unwrap_or_else(|| head.to_string()));
        }
    }
    let name = |id: &str| labels.get(id).cloned().unwrap_or_else(|| id.to_string());
    let edges = raw_edges.iter().map(|(a, b)| (name(a), name(b))).collect();
    (labels.values().cloned().collect(), edges)
}

fn label_of(attrs: &str) -> Option<String> {
    let rest = &attrs[attrs.find("label")?..];
    let rest = &rest[rest.find('"')? + 1..];
    Some(rest[..rest.find('"')?].to_string())
}

/// Mode encoded in a region id (`south:<n>:<mode>`); `None` for `north`/`local`.
fn region_mode(region: &str) -> Option<&str> {
    region.strip_prefix("south:")?.split(':').nth(1)
}

/// The admin-derived half of the graph, rebuilt from scratch on every refresh.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AdminSnapshot {
    pub whatami: BTreeMap<String, String>,
    pub edges: BTreeMap<EdgeKey, BTreeSet<EdgeSource>>,
}

impl AdminSnapshot {
    pub fn ingest(&mut self, r: &AdminReply) {
        let Some(k) = parse_admin_key(&r.key) else { return };
        self.whatami.insert(k.zid.to_string(), k.whatami.to_string());
        match k.section {
            AdminSection::Info => {
                let Ok(v) = serde_json::from_slice::<serde_json::Value>(&r.payload) else { return };
                for s in v["sessions"].as_array().into_iter().flatten() {
                    let Some(peer) = s["peer"].as_str() else { continue };
                    if let Some(w) = s["whatami"].as_str() {
                        self.whatami.entry(peer.to_string()).or_insert_with(|| w.to_string());
                    }
                    self.edges.entry(edge_key(k.zid, peer)).or_default().insert(EdgeSource::Session);
                }
            }
            AdminSection::Linkstate => {
                let (nodes, edges) = parse_dot(&String::from_utf8_lossy(&r.payload));
                if let Some(mode) = region_mode(&k.rest) {
                    for n in nodes {
                        self.whatami.entry(n).or_insert_with(|| mode.to_string());
                    }
                }
                for (a, b) in edges {
                    if a != b {
                        self.edges.entry(edge_key(&a, &b)).or_default().insert(EdgeSource::Linkstate);
                    }
                }
            }
            _ => {}
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct TopoNode {
    pub zid: String,
    pub whatami: Option<String>,
    /// This app's own publishing session.
    pub own: bool,
    /// Layout position in the unit square; NaN until laid out.
    pub pos: [f32; 2],
}

#[derive(Debug, Clone, Default)]
pub struct Topology {
    pub nodes: BTreeMap<String, TopoNode>,
    pub edges: BTreeMap<EdgeKey, BTreeSet<EdgeSource>>,
    pub own_zid: Option<String>,
    /// Bumped on every rebuild so the UI re-runs layout only when needed.
    pub version: u64,
    admin: AdminSnapshot,
    own_links: BTreeMap<String, String>,
}

impl Topology {
    pub fn set_admin(&mut self, snap: AdminSnapshot) {
        self.admin = snap;
        self.rebuild();
    }

    pub fn apply_connectivity(&mut self, ev: &ConnectivityEvent) {
        match ev {
            ConnectivityEvent::Snapshot { own_zid } => {
                self.own_zid = (!own_zid.is_empty()).then(|| own_zid.clone());
                self.own_links.clear();
            }
            ConnectivityEvent::TransportOpened(t) => {
                self.own_links.insert(t.zid.clone(), t.whatami.clone());
            }
            ConnectivityEvent::TransportClosed { zid } => {
                self.own_links.remove(zid);
            }
            ConnectivityEvent::LinkAdded(_) | ConnectivityEvent::LinkRemoved { .. } => return,
        }
        self.rebuild();
    }

    fn rebuild(&mut self) {
        let mut want: BTreeMap<String, Option<String>> =
            self.admin.whatami.iter().map(|(z, w)| (z.clone(), Some(w.clone()))).collect();
        let mut edges = self.admin.edges.clone();
        for (a, b) in edges.keys() {
            want.entry(a.clone()).or_insert(None);
            want.entry(b.clone()).or_insert(None);
        }
        if let Some(own) = &self.own_zid {
            want.entry(own.clone()).or_insert(None);
            for (peer, w) in &self.own_links {
                let slot = want.entry(peer.clone()).or_insert(None);
                if slot.is_none() {
                    *slot = Some(w.clone());
                }
                edges.entry(edge_key(own, peer)).or_default().insert(EdgeSource::OwnLink);
            }
        }
        let old = std::mem::take(&mut self.nodes);
        for (zid, whatami) in want {
            let pos = old.get(&zid).map_or([f32::NAN, f32::NAN], |n| n.pos);
            let own = self.own_zid.as_deref() == Some(zid.as_str());
            self.nodes.insert(zid.clone(), TopoNode { zid, whatami, own, pos });
        }
        self.edges = edges;
        self.version += 1;
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    pub fn neighbours(&self, zid: &str) -> Vec<(String, BTreeSet<EdgeSource>)> {
        self.edges
            .iter()
            .filter_map(|((a, b), s)| match (a == zid, b == zid) {
                (true, _) => Some((b.clone(), s.clone())),
                (_, true) => Some((a.clone(), s.clone())),
                _ => None,
            })
            .collect()
    }

    /// Fruchterman–Reingold in the unit square. Deterministic: new nodes start
    /// on a circle in ZID order; surviving nodes keep their last position.
    pub fn layout(&mut self, iterations: usize) {
        let ids: Vec<String> = self.nodes.keys().cloned().collect();
        let n = ids.len();
        if n == 0 {
            return;
        }
        if n == 1 {
            self.nodes.get_mut(&ids[0]).expect("id from keys").pos = [0.5, 0.5];
            return;
        }
        let index: BTreeMap<&str, usize> = ids.iter().enumerate().map(|(i, z)| (z.as_str(), i)).collect();
        let mut pos: Vec<[f32; 2]> = ids
            .iter()
            .enumerate()
            .map(|(i, z)| {
                let p = self.nodes[z].pos;
                if p.iter().all(|v| v.is_finite()) {
                    p
                } else {
                    let a = i as f32 / n as f32 * std::f32::consts::TAU;
                    [0.5 + 0.35 * a.cos(), 0.5 + 0.35 * a.sin()]
                }
            })
            .collect();
        let edges: Vec<(usize, usize)> = self
            .edges
            .keys()
            .filter_map(|(a, b)| Some((*index.get(a.as_str())?, *index.get(b.as_str())?)))
            .filter(|(a, b)| a != b)
            .collect();
        let k = (1.0 / n as f32).sqrt();
        let mut temp = 0.1_f32;
        for _ in 0..iterations {
            let mut disp = vec![[0.0_f32; 2]; n];
            for i in 0..n {
                for j in (i + 1)..n {
                    let mut d = [pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]];
                    if d[0].abs() < 1e-4 && d[1].abs() < 1e-4 {
                        d = [(i as f32 - j as f32) * 1e-3, 1e-3]; // separate coincident nodes deterministically
                    }
                    let dist = (d[0] * d[0] + d[1] * d[1]).sqrt().max(0.01);
                    let f = k * k / dist;
                    let u = [d[0] / dist * f, d[1] / dist * f];
                    disp[i][0] += u[0];
                    disp[i][1] += u[1];
                    disp[j][0] -= u[0];
                    disp[j][1] -= u[1];
                }
            }
            for &(a, b) in &edges {
                let d = [pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]];
                let dist = (d[0] * d[0] + d[1] * d[1]).sqrt().max(0.01);
                let f = dist * dist / k;
                let u = [d[0] / dist * f, d[1] / dist * f];
                disp[a][0] -= u[0];
                disp[a][1] -= u[1];
                disp[b][0] += u[0];
                disp[b][1] += u[1];
            }
            for i in 0..n {
                let len = (disp[i][0] * disp[i][0] + disp[i][1] * disp[i][1]).sqrt();
                if len > 0.0 {
                    let step = len.min(temp);
                    pos[i][0] += disp[i][0] / len * step;
                    pos[i][1] += disp[i][1] / len * step;
                }
                pos[i][0] = pos[i][0].clamp(0.05, 0.95);
                pos[i][1] = pos[i][1].clamp(0.05, 0.95);
            }
            temp *= 0.95;
        }
        for (i, z) in ids.iter().enumerate() {
            self.nodes.get_mut(z).expect("id from keys").pos = pos[i];
        }
    }
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test topology_model::`. Expected: 7 passed. If `parses_fixture_linkstate` fails because a label carries quotes or escapes, print the fixture's DOT line and fix `label_of` for that form. Do not relax the assertion.
- [ ] **Step 5: Commit.**

```bash
git add src/topology_model.rs
git commit -m "feat(topology): graph model from linkstate/session info/own links with deterministic layout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T8: Topology tab (Lane TOP)

**Owns:** `src/ui/topology.rs`

**Interfaces:**
- Consumes:
  - T7: `topology_model::{Topology, AdminSnapshot, EdgeSource}`.
  - T1: `ZenohCommand::{AdminQuery, StartConnectivity}`, `next_request_id`, `send_command`.
- Produces: `TopologyState` (`on_admin`, `on_connectivity` and `on_disconnected` implemented), `TOPOLOGY_REFRESH` (5 s) and `TOPOLOGY_SELECTORS`.
- egui APIs, verified in the 0.36.2 source:
  - `Ui::allocate_painter`, `Painter::{line_segment, circle_stroke, rect_stroke(rect, radius, stroke, StrokeKind), text}` and `Shape::{dashed_line, closed_line}`
  - `Ui::interact` and `Response::widget_info(|| WidgetInfo::labeled(WidgetType::Button, true, label))` (https://docs.rs/egui/0.36.2/egui/struct.Response.html#method.widget_info)
- **Encoding rule (no colour):**
  - Node kind is shown by shape: ■ router, ● peer, ▲ client, and a double ring for this app.
  - Link source is shown by stroke: solid for link state, dashed for session, thick for this app's link.
  - All strokes use `ui.visuals().text_color()`.

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/topology.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    const DOT: &str = "graph {\n    0 [ label = \"bbbb2222\" ]\n    1 [ label = \"cccc3333\" ]\n    0 -- 1 [ label = \"100.0\" ]\n}\n";

    fn seeded() -> ZenohExplorer {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.topology.on_connectivity(ConnectivityEvent::Snapshot { own_zid: "aaaa1111".into() });
        app.topology.on_connectivity(ConnectivityEvent::TransportOpened(TransportView {
            zid: "bbbb2222".into(),
            whatami: "router".into(),
            is_qos: true,
            is_multicast: false,
        }));
        app.topology.pending.insert(1);
        app.topology.on_admin(&AdminBatch {
            id: 1,
            replies: vec![AdminReply {
                key: "@/bbbb2222/router/linkstate/south:0:router".into(),
                encoding: "text/plain".into(),
                payload: DOT.as_bytes().to_vec(),
            }],
            errors: vec![],
            done: true,
        });
        app
    }

    #[test]
    fn topology_counts_nodes_and_links() {
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_topology_tab(ui), seeded());
        h.run();
        h.get_by_label("3 nodes, 2 links");
    }

    #[test]
    fn clicking_node_shows_details() {
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_topology_tab(ui), seeded());
        h.run();
        h.get_by_label("Node cccc3333 (router)").click();
        h.run();
        h.get_by_label("ZID: cccc3333");
        h.get_by_label_contains("bbbb2222 via link state");
    }

    #[test]
    fn opening_tab_starts_connectivity() {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_topology_tab(ui), app);
        h.run();
        h.run();
        let sent: Vec<ZenohCommand> = cmds.try_iter().collect();
        assert_eq!(sent.iter().filter(|c| matches!(c, ZenohCommand::StartConnectivity)).count(), 1);
        let selectors: Vec<&str> = sent
            .iter()
            .filter_map(|c| match c {
                ZenohCommand::AdminQuery { selector, .. } => Some(selector.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(selectors, TOPOLOGY_SELECTORS.to_vec());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::topology`. Expected: compile errors (`pending`, `TOPOLOGY_SELECTORS` missing).
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Topology tab: nodes and links from admin linkstate/session info plus this
//! app's own transports, drawn with the egui painter (P5 T8).

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use egui::{Align2, FontId, Pos2, Rect, Sense, Shape, Stroke, StrokeKind, Vec2, WidgetInfo, WidgetType};

use crate::app::ZenohExplorer;
use crate::topology_model::{AdminSnapshot, EdgeSource, Topology};
use crate::types::*;

/// Automatic refresh period of the admin half of the graph.
pub const TOPOLOGY_REFRESH: Duration = Duration::from_secs(5);
/// Node info (for `sessions`) and router link state.
pub const TOPOLOGY_SELECTORS: [&str; 2] = ["@/*/*", "@/*/*/linkstate/*"];
/// Admin query timeout for topology refreshes (own constant: T8 does not depend on T5).
pub const TOPOLOGY_TIMEOUT_MS: u64 = 3_000;

#[derive(Debug)]
pub struct TopologyState {
    pub topo: Topology,
    /// Admin request ids of the refresh in flight.
    pub pending: BTreeSet<RequestId>,
    staging: AdminSnapshot,
    pub last_refresh: Option<Instant>,
    pub connectivity_started: bool,
    pub auto_refresh: bool,
    pub selected: Option<String>,
    laid_out_version: u64,
}

impl Default for TopologyState {
    fn default() -> Self {
        Self {
            topo: Topology::default(),
            pending: BTreeSet::new(),
            staging: AdminSnapshot::default(),
            last_refresh: None,
            connectivity_started: false,
            auto_refresh: true,
            selected: None,
            laid_out_version: u64::MAX,
        }
    }
}

impl TopologyState {
    /// Stage replies of our own refresh; commit when every pending query is done.
    pub fn on_admin(&mut self, batch: &AdminBatch) {
        if !self.pending.contains(&batch.id) {
            return;
        }
        for r in &batch.replies {
            self.staging.ingest(r);
        }
        if batch.done {
            self.pending.remove(&batch.id);
            if self.pending.is_empty() {
                self.topo.set_admin(std::mem::take(&mut self.staging));
            }
        }
    }

    pub fn on_connectivity(&mut self, ev: ConnectivityEvent) {
        self.topo.apply_connectivity(&ev);
    }

    pub fn on_disconnected(&mut self) {
        *self = Self::default();
    }
}

fn short(zid: &str) -> &str {
    &zid[..zid.len().min(8)]
}

fn source_words(s: &BTreeSet<EdgeSource>) -> String {
    s.iter()
        .map(|e| match e {
            EdgeSource::Linkstate => "link state",
            EdgeSource::Session => "session",
            EdgeSource::OwnLink => "this app's transport",
        })
        .collect::<Vec<_>>()
        .join(" + ")
}

pub trait TopologyUI {
    fn show_topology_tab(&mut self, ui: &mut egui::Ui);
}

impl ZenohExplorer {
    fn refresh_topology(&mut self) {
        let st_pending: Vec<RequestId> = TOPOLOGY_SELECTORS.iter().map(|_| self.next_request_id()).collect();
        self.topology.staging = AdminSnapshot::default();
        self.topology.pending = st_pending.iter().copied().collect();
        self.topology.last_refresh = Some(Instant::now());
        for (id, sel) in st_pending.into_iter().zip(TOPOLOGY_SELECTORS) {
            self.send_command(ZenohCommand::AdminQuery {
                id,
                selector: sel.to_string(),
                timeout_ms: TOPOLOGY_TIMEOUT_MS,
            });
        }
    }
}

impl TopologyUI for ZenohExplorer {
    fn show_topology_tab(&mut self, ui: &mut egui::Ui) {
        let connected = matches!(self.connection_status, ConnectionStatus::Connected);
        if connected && !self.topology.connectivity_started {
            self.topology.connectivity_started = self.send_command(ZenohCommand::StartConnectivity);
        }
        let due = self.topology.last_refresh.is_none_or(|t| self.topology.auto_refresh && t.elapsed() >= TOPOLOGY_REFRESH);
        if connected && self.topology.pending.is_empty() && due {
            self.refresh_topology();
        }
        if connected && self.topology.auto_refresh {
            ui.ctx().request_repaint_after(TOPOLOGY_REFRESH);
        }

        ui.horizontal(|ui| {
            if ui.add_enabled(connected && self.topology.pending.is_empty(), egui::Button::new("Refresh")).clicked() {
                self.refresh_topology();
            }
            ui.checkbox(&mut self.topology.auto_refresh, "Auto-refresh (5 s)");
            if !self.topology.pending.is_empty() {
                ui.spinner();
            }
            ui.label(format!("{} nodes, {} links", self.topology.topo.node_count(), self.topology.topo.edge_count()));
        });
        ui.label("Shapes: ■ router · ● peer · ▲ client · ◎ this app    Lines: solid = link state · dashed = session · thick = this app's transport");

        if self.topology.topo.version != self.topology.laid_out_version {
            self.topology.topo.layout(150);
            self.topology.laid_out_version = self.topology.topo.version;
        }

        let height = (ui.available_height() - 140.0).max(240.0);
        let (canvas, painter) = ui.allocate_painter(Vec2::new(ui.available_width(), height), Sense::hover());
        let rect = canvas.rect;
        let fg = ui.visuals().text_color();
        let to_screen = |p: [f32; 2]| Pos2::new(rect.left() + p[0] * rect.width(), rect.top() + p[1] * rect.height());

        for ((a, b), srcs) in &self.topology.topo.edges {
            let (Some(na), Some(nb)) = (self.topology.topo.nodes.get(a), self.topology.topo.nodes.get(b)) else { continue };
            let (pa, pb) = (to_screen(na.pos), to_screen(nb.pos));
            if srcs.contains(&EdgeSource::OwnLink) {
                painter.line_segment([pa, pb], Stroke::new(3.0, fg));
            } else if srcs.contains(&EdgeSource::Linkstate) {
                painter.line_segment([pa, pb], Stroke::new(1.5, fg));
            } else {
                painter.extend(Shape::dashed_line(&[pa, pb], Stroke::new(1.0, fg), 6.0, 4.0));
            }
        }

        let mut clicked = None;
        for node in self.topology.topo.nodes.values() {
            let c = to_screen(node.pos);
            let width = if self.topology.selected.as_deref() == Some(node.zid.as_str()) { 3.0 } else { 1.5 };
            let stroke = Stroke::new(width, fg);
            match node.whatami.as_deref() {
                Some("router") => {
                    painter.rect_stroke(Rect::from_center_size(c, Vec2::splat(14.0)), 0.0, stroke, StrokeKind::Middle);
                }
                Some("client") => {
                    let pts = vec![c + Vec2::new(0.0, -8.0), c + Vec2::new(7.0, 6.0), c + Vec2::new(-7.0, 6.0)];
                    painter.add(Shape::closed_line(pts, stroke));
                }
                _ => {
                    painter.circle_stroke(c, 7.0, stroke);
                }
            }
            if node.own {
                painter.circle_stroke(c, 12.0, Stroke::new(1.0, fg));
            }
            painter.text(c + Vec2::new(0.0, 14.0), Align2::CENTER_TOP, short(&node.zid), FontId::proportional(11.0), fg);

            let kind = node.whatami.clone().unwrap_or_else(|| "unknown".to_string());
            let label = format!("Node {} ({kind})", short(&node.zid));
            let resp = ui.interact(Rect::from_center_size(c, Vec2::splat(24.0)), ui.id().with(("topo_node", &node.zid)), Sense::click());
            resp.widget_info(|| WidgetInfo::labeled(WidgetType::Button, true, label.clone()));
            if resp.on_hover_text(&node.zid).clicked() {
                clicked = Some(node.zid.clone());
            }
        }
        if clicked.is_some() {
            self.topology.selected = clicked;
        }

        ui.separator();
        match self.topology.selected.as_deref().and_then(|z| self.topology.topo.nodes.get(z)) {
            Some(n) => {
                ui.label(format!("ZID: {}", n.zid));
                ui.label(format!("Kind: {}", n.whatami.as_deref().unwrap_or("unknown")));
                if n.own {
                    ui.label("This is this app's publishing session.");
                }
                for (peer, srcs) in self.topology.topo.neighbours(&n.zid) {
                    ui.label(format!("↔ {} via {}", short(&peer), source_words(&srcs)));
                }
            }
            None => {
                ui.label(if connected { "Click a node for details." } else { "Connect to see the topology." });
            }
        }
    }
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test ui::topology`. Expected: 3 passed.
  - In `clicking_node_shows_details`, the neighbour line reads `↔ bbbb2222 via link state`, which the `get_by_label_contains` assertion matches.
  - `is_none_or` needs Rust ≥ 1.82, which P3's MSRV satisfies.
- [ ] **Step 5: Manual check.** With both T2 routers running, connect in client mode to 7447 and open Topology. You should see two squares joined by a solid line, and this app's ◎ circle joined to the 7447 router by a thick line. Stop the second router: within 5 s its node and edge disappear.
- [ ] **Step 6: Commit.**

```bash
git add src/ui/topology.rs
git commit -m "feat(ui): live Topology tab with shape/stroke-coded nodes and links

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T9: Liveliness worker (Lane LIV)

**Owns:** `src/worker/liveliness.rs`

**Interfaces:**
- Consumes (T1): `LivelinessEvent`, `FailedOp::Liveliness`, `WorkerState.liveliness_task`, `test_support`.
- Produces:
  - `handle_start(st, ctx, key_expr)`, which replaces any running subscriber. A token that appears is a `PUT` → `alive: true`; a token that disappears is a `DELETE` → `alive: false`.
  - `handle_stop(st)`.
- zenoh API: `Session::liveliness().declare_subscriber(key).history(true)` (https://docs.rs/zenoh/1.10.1/zenoh/liveliness/struct.LivelinessSubscriberBuilder.html#method.history, stable in 1.10.1) and `Liveliness::declare_token` (https://docs.rs/zenoh/1.10.1/zenoh/liveliness/struct.Liveliness.html).

- [ ] **Step 1: Write the failing test** in `src/worker/liveliness.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker::state::test_support::{ctx, session, state_with, wait_for};

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn liveliness_reports_token_up_and_down() {
        let s = session(&[]).await;
        // Declared before the subscriber: must arrive through history(true).
        let token = s.liveliness().declare_token("t/live/a").await.unwrap();
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_start(&mut st, &ctx, "t/live/**".into()).await;
        let up = wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Liveliness(l) if l.key == "t/live/a" && l.alive)).await;
        assert!(up.is_some(), "token not reported alive");
        token.undeclare().await.unwrap();
        let down = wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Liveliness(l) if l.key == "t/live/a" && !l.alive)).await;
        assert!(down.is_some(), "token drop not reported");
        handle_stop(&mut st);
        assert!(st.liveliness_task.is_none());
    }
}
```

- [ ] **Step 2: Run the test to confirm it fails.** Run `cargo test worker::liveliness`. Expected: FAIL ("token not reported alive").
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Liveliness token browser backend (P5 T9).

use chrono::Utc;
use zenoh::sample::SampleKind;

use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

/// Subscribe to liveliness tokens on `key_expr`, with history so tokens that
/// already exist are reported first.
pub(crate) async fn handle_start(st: &mut WorkerState, ctx: &WorkerCtx, key_expr: String) {
    handle_stop(st);
    let fail = |error: String| {
        let _ = ctx.event_sender.send(ZenohEvent::OperationFailed { op: FailedOp::Liveliness, error });
    };
    let Some(sess) = st.publishing_session.clone() else {
        return fail("not connected".to_string());
    };
    let sub = match sess.liveliness().declare_subscriber(&key_expr).history(true).await {
        Ok(s) => s,
        Err(e) => return fail(format!("{key_expr}: {e}")),
    };
    let tx = ctx.event_sender.clone();
    st.liveliness_task = Some(tokio::spawn(async move {
        while let Ok(sample) = sub.recv_async().await {
            let ev = LivelinessEvent {
                key: sample.key_expr().to_string(),
                alive: sample.kind() == SampleKind::Put,
                at: Utc::now(),
            };
            if tx.send(ZenohEvent::Liveliness(ev)).is_err() {
                break;
            }
        }
    }));
}

/// Stop the subscriber (aborting the task drops and undeclares it).
pub(crate) fn handle_stop(st: &mut WorkerState) {
    if let Some(t) = st.liveliness_task.take() {
        t.abort();
    }
}
```

- [ ] **Step 4: Run the test.** Run `cargo test worker::liveliness`. Expected: 1 passed.
  - **Fallback:** if a session does not see its own tokens, split the test into two sessions linked by `tcp/127.0.0.1:27661` (B declares the token, A subscribes), mark it `#[ignore = "opens network sessions"]`, and record that in the commit body.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/liveliness.rs
git commit -m "feat(worker): liveliness subscriber with history reporting token up/down

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T10: Liveliness tab (Lane LIV)

**Owns:** `src/ui/liveliness.rs`

**Interfaces:**
- Consumes (T1): `LivelinessEvent`, `ZenohCommand::{StartLiveliness, StopLiveliness}`, `send_command`, `validation::key_expr_error`.
- Produces:
  - `TokenRow { alive, first_seen, last_change, changes }`.
  - `TokenTable`, with `apply(&LivelinessEvent)`, `alive_count`, `gone_count`, `clear_gone` and `iter`.
  - `LivelinessState { key_expr, running, table, filter }`, with `on_event` and `on_disconnected` implemented.

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/liveliness.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    fn ev(key: &str, alive: bool) -> LivelinessEvent {
        LivelinessEvent { key: key.into(), alive, at: chrono::Utc::now() }
    }

    #[test]
    fn flapping_token_is_one_row() {
        let mut t = TokenTable::default();
        t.apply(&ev("demo/a", true));
        t.apply(&ev("demo/a", true)); // duplicate (history + live) is not a change
        t.apply(&ev("demo/a", false));
        t.apply(&ev("demo/a", true));
        let rows: Vec<_> = t.iter().collect();
        assert_eq!(rows.len(), 1);
        assert!(rows[0].1.alive);
        assert_eq!(rows[0].1.changes, 3);
        assert_eq!((t.alive_count(), t.gone_count()), (1, 0));
    }

    #[test]
    fn start_sends_liveliness_command() {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_liveliness_tab(ui), app);
        h.run();
        h.get_by_label("Start").click();
        h.run();
        assert!(cmds.try_iter().any(|c| matches!(c, ZenohCommand::StartLiveliness { key_expr } if key_expr == "**")));
        assert!(h.state().liveliness.running);
        h.get_by_label("Stop");
    }

    #[test]
    fn clear_gone_removes_dead_rows() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.liveliness.on_event(ev("demo/a", true));
        app.liveliness.on_event(ev("demo/b", true));
        app.liveliness.on_event(ev("demo/b", false));
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_liveliness_tab(ui), app);
        h.run();
        h.get_by_label("1 alive, 1 gone");
        h.get_by_label("demo/b");
        h.get_by_label("○ gone");
        h.get_by_label("Clear gone").click();
        h.run();
        assert!(h.query_by_label("demo/b").is_none());
        h.get_by_label("demo/a");
        h.get_by_label("● alive");
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::liveliness`. Expected: compile errors (`TokenTable` missing).
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Liveliness tab: live tokens with appear/disappear history (P5 T10).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};

use crate::app::ZenohExplorer;
use crate::types::*;

#[derive(Debug, Clone, PartialEq)]
pub struct TokenRow {
    pub alive: bool,
    pub first_seen: DateTime<Utc>,
    pub last_change: DateTime<Utc>,
    /// Number of state changes, counting the first appearance.
    pub changes: u32,
}

/// One row per token key; repeated identical states are not changes.
#[derive(Debug, Default)]
pub struct TokenTable {
    rows: BTreeMap<String, TokenRow>,
}

impl TokenTable {
    pub fn apply(&mut self, ev: &LivelinessEvent) {
        match self.rows.get_mut(&ev.key) {
            Some(r) if r.alive != ev.alive => {
                r.alive = ev.alive;
                r.last_change = ev.at;
                r.changes += 1;
            }
            Some(_) => {}
            None => {
                self.rows.insert(
                    ev.key.clone(),
                    TokenRow { alive: ev.alive, first_seen: ev.at, last_change: ev.at, changes: 1 },
                );
            }
        }
    }

    pub fn alive_count(&self) -> usize {
        self.rows.values().filter(|r| r.alive).count()
    }

    pub fn gone_count(&self) -> usize {
        self.rows.len() - self.alive_count()
    }

    pub fn clear_gone(&mut self) {
        self.rows.retain(|_, r| r.alive);
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &TokenRow)> {
        self.rows.iter()
    }
}

#[derive(Debug)]
pub struct LivelinessState {
    pub key_expr: String,
    pub running: bool,
    pub table: TokenTable,
    pub filter: String,
}

impl Default for LivelinessState {
    fn default() -> Self {
        Self { key_expr: "**".to_string(), running: false, table: TokenTable::default(), filter: String::new() }
    }
}

impl LivelinessState {
    pub fn on_event(&mut self, ev: LivelinessEvent) {
        self.table.apply(&ev);
    }

    pub fn on_disconnected(&mut self) {
        self.running = false;
    }
}

pub trait LivelinessUI {
    fn show_liveliness_tab(&mut self, ui: &mut egui::Ui);
}

impl LivelinessUI for ZenohExplorer {
    fn show_liveliness_tab(&mut self, ui: &mut egui::Ui) {
        let connected = matches!(self.connection_status, ConnectionStatus::Connected);
        ui.horizontal(|ui| {
            ui.label("Key expression:");
            ui.add_enabled(
                !self.liveliness.running,
                egui::TextEdit::singleline(&mut self.liveliness.key_expr).desired_width(220.0),
            );
            let err = crate::validation::key_expr_error(&self.liveliness.key_expr);
            if self.liveliness.running {
                if ui.button("Stop").clicked() {
                    self.send_command(ZenohCommand::StopLiveliness);
                    self.liveliness.running = false;
                }
            } else if ui.add_enabled(connected && err.is_none(), egui::Button::new("Start")).clicked() {
                self.liveliness.running =
                    self.send_command(ZenohCommand::StartLiveliness { key_expr: self.liveliness.key_expr.clone() });
            }
            if let Some(e) = err {
                ui.label(format!("⚠ {e}"));
            }
        });
        ui.label("Tip: ** does not match @-chunks; use **/@xfer/* to see file-transfer offers.");
        ui.horizontal(|ui| {
            ui.label("Filter:");
            ui.text_edit_singleline(&mut self.liveliness.filter);
            ui.label(format!("{} alive, {} gone", self.liveliness.table.alive_count(), self.liveliness.table.gone_count()));
            if ui.button("Clear gone").clicked() {
                self.liveliness.table.clear_gone();
            }
        });
        ui.separator();
        if self.liveliness.table.iter().next().is_none() {
            ui.label(if self.liveliness.running { "No tokens yet." } else { "Press Start to watch liveliness tokens." });
            return;
        }
        let filter = self.liveliness.filter.to_lowercase();
        let now = chrono::Utc::now();
        egui::ScrollArea::vertical().id_salt("liveliness_scroll").auto_shrink([false; 2]).show(ui, |ui| {
            egui::Grid::new("liveliness_grid").striped(true).num_columns(5).show(ui, |ui| {
                ui.strong("State");
                ui.strong("Token");
                ui.strong("First seen");
                ui.strong("Last change");
                ui.strong("Changes");
                ui.end_row();
                for (key, row) in self.liveliness.table.iter() {
                    if !filter.is_empty() && !key.to_lowercase().contains(&filter) {
                        continue;
                    }
                    ui.label(if row.alive { "● alive" } else { "○ gone" });
                    ui.label(key);
                    // Local time (F-T14-6).
                    ui.label(crate::types::format_local_time(&row.first_seen, &now));
                    ui.label(crate::types::format_local_time(&row.last_change, &now));
                    ui.label(row.changes.to_string());
                    ui.end_row();
                }
            });
        });
    }
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test ui::liveliness`. Expected: 3 passed.
- [ ] **Step 5: Commit.**

```bash
git add src/ui/liveliness.rs
git commit -m "feat(ui): Liveliness tab with token table, alive/gone words and change counts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T11: Publish options in the worker (Lane PUB)

**Owns:** `src/worker/publish.rs`

**Interfaces:**
- Consumes (T1): `PublishOptions`, `PublishKind`, `attachment::encode` and the `From` conversions to `zenoh::qos::*`.
- Produces:
  - `handle_publish(…, options)` honours `options`.
  - `pub(crate) fn with_options<'a, 'b, T>(b: PublicationBuilder<PublisherBuilder<'a, 'b>, T>, o: &PublishOptions) -> PublicationBuilder<PublisherBuilder<'a, 'b>, T>`.
- zenoh API (verified in 1.10.1 `api/builders/publisher.rs`):
  - `Session::put` / `Session::delete` return `PublicationBuilder<PublisherBuilder, T>`.
  - `priority`, `congestion_control` and `express` come from `QoSBuilderTrait`, exposed as inherent methods by `#[zenoh_macros::internal_trait]`.
  - `reliability` is an inherent `#[unstable]` method, and `attachment` comes from `SampleBuilderTrait`.
  - Docs: https://docs.rs/zenoh/1.10.1/zenoh/pubsub/struct.PublicationBuilder.html
- **Rules:**
  - A user attachment replaces the filename attachment on single puts.
  - P4's `@xfer` file transfers have their own QoS (DataLow/Block) and get no user options.
  - A DELETE echoes a `[DELETE]` message and removes the key from the local queryable store.

- [ ] **Step 1: Write the failing tests** in the `src/worker/publish.rs` tests module (create it if absent):

```rust
#[cfg(test)]
mod p5_tests {
    use super::*;
    use crate::types::*;
    use crate::worker::state::test_support::{ctx, session, state_with};
    use std::time::Duration;

    fn opts() -> PublishOptions {
        PublishOptions {
            priority: PriorityView::DataHigh,
            congestion: CongestionView::Drop,
            express: true,
            attachment: AttachmentSpec::KeyValue(vec![("trace".into(), "42".into())]),
            ..Default::default()
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn publish_applies_attachment() {
        let s = session(&[]).await;
        let sub = s.declare_subscriber("t/opt").await.unwrap();
        let mut st = state_with(s);
        let (ctx, _rx) = ctx();
        handle_publish(&mut st, &ctx, "t/opt".into(), b"hello".to_vec(), "text/plain".into(), false, Some("f.txt".into()), opts()).await;
        let sample = tokio::time::timeout(Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        assert_eq!(sample.payload().to_bytes().as_ref(), b"hello");
        let att = sample.attachment().expect("attachment").to_bytes();
        assert_eq!(crate::attachment::decode_key_values(&att).unwrap(), vec![("trace".to_string(), "42".to_string())]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn publish_delete_sends_delete_sample() {
        let s = session(&[]).await;
        let sub = s.declare_subscriber("t/del").await.unwrap();
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        let o = PublishOptions { kind: PublishKind::Delete, ..Default::default() };
        handle_publish(&mut st, &ctx, "t/del".into(), Vec::new(), "text/plain".into(), false, None, o).await;
        let sample = tokio::time::timeout(Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        assert_eq!(sample.kind(), zenoh::sample::SampleKind::Delete);
        let echoed = rx.try_iter().any(|e| matches!(e, ZenohEvent::MessageReceived(m) if m.kind == SampleKindView::Delete && m.key == "t/del"));
        assert!(echoed, "DELETE not echoed to the UI");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn publish_qos_crosses_the_wire() {
        let b = session(&[("listen/endpoints", "[\"tcp/127.0.0.1:27631\"]")]).await;
        let sub = b.declare_subscriber("t/qos").await.unwrap();
        let a = session(&[("connect/endpoints", "[\"tcp/127.0.0.1:27631\"]")]).await;
        tokio::time::sleep(Duration::from_millis(500)).await;
        let mut st = state_with(a);
        let (ctx, _rx) = ctx();
        handle_publish(&mut st, &ctx, "t/qos".into(), b"x".to_vec(), "text/plain".into(), false, None, opts()).await;
        let sample = tokio::time::timeout(Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        assert_eq!(sample.priority(), zenoh::qos::Priority::DataHigh);
        assert_eq!(sample.congestion_control(), zenoh::qos::CongestionControl::Drop);
        assert!(sample.express());
    }
}
```

  If P1 declared `handle_publish(st: &WorkerState, …)`, pass `&st` instead of `&mut st`. If the echo is sent through `send_sample` under another event variant, match that variant instead.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test worker::publish::p5_tests`. Expected: FAIL. `publish_applies_attachment` fails because the attachment is the filename, and `publish_delete_sends_delete_sample` fails because the kind is `Put`.
- [ ] **Step 3: Add the helper** near the top of `src/worker/publish.rs`:

```rust
use zenoh::pubsub::{PublicationBuilder, PublisherBuilder};

/// Apply Publish-form QoS and the user attachment to a put or delete builder.
pub(crate) fn with_options<'a, 'b, T>(
    b: PublicationBuilder<PublisherBuilder<'a, 'b>, T>,
    o: &PublishOptions,
) -> PublicationBuilder<PublisherBuilder<'a, 'b>, T> {
    let b = b
        .priority(o.priority.into())
        .congestion_control(o.congestion.into())
        .express(o.express)
        .reliability(o.reliability.into());
    match crate::attachment::encode(&o.attachment) {
        Some(a) => b.attachment(a),
        None => b,
    }
}
```

- [ ] **Step 4: Use it in `handle_publish`.** Rename the parameter `_options` to `options`.
  - Directly after the "no session" early return, add the DELETE path:

```rust
    if options.kind == PublishKind::Delete {
        match with_options(sess.delete(&key), &options).await {
            Ok(()) => {
                if let Ok(mut store) = ctx.local_kvstore.write() {
                    store.remove(&key);
                }
                let echo = ZenohMessage::new_with_bytes(
                    key.clone(),
                    "[DELETE]".to_string(),
                    Vec::new(),
                    encoding.clone(),
                    chrono::Utc::now(),
                    MessageType::Publish,
                    true,
                    MessageSource::LocalEcho,
                )
                .with_sample_meta(SampleKindView::Delete, None);
                crate::worker::pipeline::send_sample(&ctx.event_sender, &ctx.sample_drops, echo);
            }
            Err(e) => {
                let _ = ctx.event_sender.send(ZenohEvent::OperationFailed {
                    op: FailedOp::Publish,
                    error: format!("delete {key}: {e}"),
                });
            }
        }
        return;
    }
```

  - Find the single-message put branch. After P4 T10 it is the only put path, since chunked sending was removed and plain publish is capped at `PLAIN_PUBLISH_MAX`. There, delete the `.congestion_control(zenoh::qos::CongestionControl::Block)` call, then replace the filename-attachment block with:

```rust
        let mut put = sess.put(&key, payload).encoding(encoding.as_str());
        if options.attachment == AttachmentSpec::None {
            if let Some(ref name) = filename {
                put = put.attachment(name.as_bytes().to_vec());
            }
        }
        let put = with_options(put, &options);
```

  - Keep the `payload` move or clone exactly as P1 T7 / P4 T10 left it. File transfers go through P4's `@xfer` offer path, which T11 does not touch.
- [ ] **Step 5: Run the tests.** Run `cargo test worker::publish && cargo test -- --ignored publish_qos_crosses_the_wire`. Expected: all pass, including P1's `publish_shape_*`.
- [ ] **Step 6: Commit.**

```bash
git add src/worker/publish.rs
git commit -m "feat(worker): publish honours QoS, attachment and delete options

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T12: Matching status worker (Lane PUB)

**Owns:** `src/worker/matching.rs`

**Interfaces:**
- Consumes (T1): `ZenohEvent::Matching`, `FailedOp::Matching`, `WorkerState.matching_task`.
- Produces: `handle_watch(st, ctx, key)`. An empty `key` only stops the watch.
- **Why the monitor session:** the explorer's monitor session subscribes to `**`, so a publisher on any other session always matches. Declaring the probe publisher **on the monitor session** with `allowed_destination(Locality::Remote)` excludes that local `**` subscriber. Subscriptions the user made in this app (publishing session) still count, and the UI hover text says so.
- zenoh API:
  - `Session::declare_publisher(..).allowed_destination(Locality::Remote)`.
  - `Publisher::matching_status()` and `Publisher::matching_listener()` → `recv_async()` → `MatchingStatus::matching()` (https://docs.rs/zenoh/1.10.1/zenoh/pubsub/struct.Publisher.html#method.matching_listener). The matching listener respects the publisher's destination (1.10.1 `api/publisher.rs:357`).

- [ ] **Step 1: Write the failing tests** in `src/worker/matching.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::worker::state::test_support::{ctx, session, wait_for};
    use std::sync::Arc;

    fn matching(ev: &ZenohEvent, want: bool) -> bool {
        matches!(ev, ZenohEvent::Matching { key, matching } if key == "t/m" && *matching == want)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn matching_ignores_monitor_own_subscriber() {
        let mon = session(&[]).await;
        let _own = mon.declare_subscriber("**").await.unwrap();
        let mut st = WorkerState { monitor_session: Some(Arc::new(mon)), ..Default::default() };
        let (ctx, rx) = ctx();
        handle_watch(&mut st, &ctx, "t/m".into()).await;
        assert!(wait_for(&rx, 5, |e| matching(e, false)).await.is_some());
        handle_watch(&mut st, &ctx, String::new()).await;
        assert!(st.matching_task.is_none());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn matching_flips_when_subscriber_appears() {
        let b = session(&[("listen/endpoints", "[\"tcp/127.0.0.1:27641\"]")]).await;
        let mon = session(&[("connect/endpoints", "[\"tcp/127.0.0.1:27641\"]")]).await;
        let mut st = WorkerState { monitor_session: Some(Arc::new(mon)), ..Default::default() };
        let (ctx, rx) = ctx();
        handle_watch(&mut st, &ctx, "t/m".into()).await;
        assert!(wait_for(&rx, 5, |e| matching(e, false)).await.is_some());
        let sub = b.declare_subscriber("t/**").await.unwrap();
        assert!(wait_for(&rx, 10, |e| matching(e, true)).await.is_some());
        drop(sub);
        assert!(wait_for(&rx, 10, |e| matching(e, false)).await.is_some());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test worker::matching`. Expected: FAIL (no `Matching` event).
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Matching status of the Publish key (P5 T12).

use zenoh::sample::Locality;

use super::state::{WorkerCtx, WorkerState};
use crate::types::*;

/// Watch whether `key` has subscribers outside the monitor session.
/// Replaces any previous watch; an empty key just stops.
pub(crate) async fn handle_watch(st: &mut WorkerState, ctx: &WorkerCtx, key: String) {
    if let Some(t) = st.matching_task.take() {
        t.abort();
    }
    if key.is_empty() {
        return;
    }
    let Some(sess) = st.monitor_session.clone().or_else(|| st.publishing_session.clone()) else {
        return;
    };
    let tx = ctx.event_sender.clone();
    st.matching_task = Some(tokio::spawn(async move {
        let fail = |error: String| {
            let _ = tx.send(ZenohEvent::OperationFailed { op: FailedOp::Matching, error });
        };
        let publisher = match sess.declare_publisher(key.clone()).allowed_destination(Locality::Remote).await {
            Ok(p) => p,
            Err(e) => return fail(format!("{key}: {e}")),
        };
        // Listener first, so a change between status and listener is not lost.
        let listener = match publisher.matching_listener().await {
            Ok(l) => l,
            Err(e) => return fail(format!("{key}: {e}")),
        };
        match publisher.matching_status().await {
            Ok(s) => {
                let _ = tx.send(ZenohEvent::Matching { key: key.clone(), matching: s.matching() });
            }
            Err(e) => return fail(format!("{key}: {e}")),
        }
        while let Ok(s) = listener.recv_async().await {
            if tx.send(ZenohEvent::Matching { key: key.clone(), matching: s.matching() }).is_err() {
                break;
            }
        }
    }));
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test worker::matching && cargo test -- --ignored matching_flips_when_subscriber_appears`. Expected: 1 passed, then 1 passed.
- [ ] **Step 5: Commit.**

```bash
git add src/worker/matching.rs
git commit -m "feat(worker): publisher matching status watch excluding the monitor's ** subscriber

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T13: Publish form (Lane PUB)

**Owns:** `src/ui/publish.rs`, `src/ui/attachment_editor.rs`

**Interfaces:**
- Consumes:
  - T1: `PublishOptions` and friends, `encodings::encoding_combo`, `ZenohCommand::{Publish, WatchMatching}`, `shortcut_submit`, `send_command`, `PUBLISH_KEY_ID`.
  - P1: `validation::key_expr_error`; T23's `publish_button_label`, `publish_status` and its `pending` guard.
  - P3 T14: the Key field's `labelled_by`, and the Publish button as T14 left it: `add_enabled(can_publish, …)`, or its F-T7-12 fallback (always enabled, a refused click raises `UiAlert::Warning(reason)`).
- Produces:
  - `PublishFormState { options: PublishOptions, matching: Option<bool>, watched_key: String }`, with `on_matching` implemented.
  - `pub fn attachment_editor(ui: &mut egui::Ui, id_salt: &str, spec: &mut AttachmentSpec)` in `src/ui/attachment_editor.rs` (T16 reuses it).
  - Enter in the Key field submits like the button, through the same `can_publish` and `pending` checks, and focus stays in Key whether or not it published (F-T19-4). A second Enter while the first publish is pending sends nothing (P1 T23, F-T15-3).
- egui APIs: `Ui::radio_value`, `ComboBox::from_id_salt`, `CollapsingHeader`, `Ui::add_enabled_ui`, `Ui::push_id`, `Response::{lost_focus, request_focus, labelled_by}`, `Memory::has_focus` and `InputState::key_pressed`, all present in 0.36.2.
  - A single-line `TextEdit` gives up focus on Enter, so `lost_focus() && key_pressed(Enter)` finds an Enter typed in Key.
  - That test alone is not enough. In 0.36.2, `Memory::lost_focus` stays true for one extra frame after focus moves to another widget mid-frame (`memory/mod.rs:860-873`; egui's regression test `lost_focus_fires_after_mid_frame_focus_transfer`). The app repaints only on events, so after a click from Key into the multiline Payload the next frame can be an Enter press. Key would then report `lost_focus()` and publish while the user was typing a newline in Payload.
  - So the submit test also requires that Key had focus when the frame started. Read `ui.memory(|m| m.has_focus(egui::Id::new(crate::ui::PUBLISH_KEY_ID)))` **before** the field is drawn.

- [ ] **Step 1: Write the failing tests.**
  - In `src/ui/attachment_editor.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    #[test]
    fn attachment_editor_adds_and_removes_rows() {
        let mut h = Harness::new_ui_state(
            |ui, spec: &mut AttachmentSpec| attachment_editor(ui, "t", spec),
            AttachmentSpec::None,
        );
        h.run();
        h.get_by_label("Key/value").click();
        h.run();
        h.get_by_label("+ Add row").click();
        h.run();
        assert!(matches!(h.state(), AttachmentSpec::KeyValue(r) if r.len() == 2));
        h.get_by_label("Remove row 1").click();
        h.run();
        assert!(matches!(h.state(), AttachmentSpec::KeyValue(r) if r.len() == 1));
        h.get_by_label("Text").click();
        h.run();
        assert!(matches!(h.state(), AttachmentSpec::Text(t) if t.is_empty()));
    }
}
```

  - At the bottom of `src/ui/publish.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui::accesskit::Role;
    use egui_kittest::{kittest::Queryable, Harness};

    fn connected() -> (ZenohExplorer, std::sync::mpsc::Receiver<ZenohCommand>) {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        app.publish_key = "demo/x".into();
        // Non-empty, so P1 T23's button reads "Publish", not "Publish empty payload".
        app.publish_payload = "v".into();
        (app, cmds)
    }

    fn published(cmds: &std::sync::mpsc::Receiver<ZenohCommand>) -> Option<(Vec<u8>, PublishOptions)> {
        cmds.try_iter().find_map(|c| match c {
            ZenohCommand::Publish { payload, options, .. } => Some((payload, options)),
            _ => None,
        })
    }

    #[test]
    fn publish_sends_options() {
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_publish_tab(ui), app);
        h.run();
        h.get_by_label("QoS").click();
        h.run();
        // A ComboBox exposes its selected text as the accessibility *value* (egui 0.36.2 combo_box.rs:246).
        h.get_by_value("Data (default)").click();
        h.run();
        h.get_by_label("Data high").click();
        h.run();
        h.get_by_label("Express (no batching)").click();
        h.run();
        h.get_by_label("Publish").click();
        h.run();
        let (_, o) = published(&cmds).expect("Publish sent");
        assert_eq!((o.priority, o.express, o.kind), (PriorityView::DataHigh, true, PublishKind::Put));
    }

    #[test]
    fn delete_mode_disables_payload() {
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_publish_tab(ui), app);
        h.run();
        h.get_by_label("Delete").click();
        h.run();
        h.get_by_label("Delete key").click();
        h.run();
        let (payload, o) = published(&cmds).expect("Publish sent");
        assert_eq!(o.kind, PublishKind::Delete);
        assert!(payload.is_empty());
    }

    #[test]
    fn enter_in_key_field_publishes() {
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_publish_tab(ui), app);
        h.run();
        h.get_by_label("Key:").focus();
        h.run();
        h.key_press(egui::Key::Enter);
        h.run();
        assert!(published(&cmds).is_some(), "Enter in Key publishes (F-T19-4)");
        h.run();
        assert_eq!(
            h.ctx.memory(|m| m.focused()),
            Some(egui::Id::new(crate::ui::PUBLISH_KEY_ID)),
            "focus stays in Key after Enter"
        );
        // No worker answers in the harness, so the first publish stays pending.
        h.key_press(egui::Key::Enter);
        h.run();
        assert!(published(&cmds).is_none(), "Enter while pending sends nothing (P1 T23, F-T15-3)");
    }

    #[test]
    fn enter_after_leaving_key_does_not_publish() {
        // egui 0.36.2 keeps Key's `lost_focus()` true for one more frame after focus moves
        // mid-frame, so an Enter typed in Payload on the very next frame must not publish.
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_publish_tab(ui), app);
        h.run();
        h.get_by_label("Key:").focus();
        h.run();
        h.get_by_role_and_label(Role::MultilineTextInput, "Payload:").focus();
        h.step(); // one frame: focus moves from Key to Payload while it is drawn
        h.key_press(egui::Key::Enter);
        h.step(); // the next frame is the Enter press, typed in Payload
        assert!(published(&cmds).is_none(), "Enter in Payload is a newline, not a publish (F-T19-4)");
    }

    #[test]
    fn matching_indicator_uses_words() {
        let (mut app, _cmds) = connected();
        app.publish_form.watched_key = "demo/x".into();
        app.publish_form.on_matching("demo/x", true);
        app.publish_form.on_matching("other/key", false); // not the watched key: ignored
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_publish_tab(ui), app);
        h.run();
        h.get_by_label("● subscribers present");
        h.state_mut().publish_form.on_matching("demo/x", false);
        h.run();
        h.get_by_label("○ no subscribers");
    }
}
```

  `h.step()` runs one frame per queued event, so the focus request and the Enter press land in two consecutive frames with no idle frame between them. That is the order a user produces by clicking into Payload and pressing Enter at once. P3 T14 gives Payload the accessible label "Payload:" (`publish_fields_are_labelled`).
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- ui::publish ui::attachment_editor`. Expected: compile errors (`attachment_editor` and the form fields are missing).
- [ ] **Step 3: Implement the attachment editor** (replace the stub in `src/ui/attachment_editor.rs`):

```rust
//! Attachment editor widget shared by the Publish and Query forms (P5 T13).

use crate::types::AttachmentSpec;

/// Edit an attachment as none, text, or key/value rows.
pub fn attachment_editor(ui: &mut egui::Ui, id_salt: &str, spec: &mut AttachmentSpec) {
    ui.push_id(id_salt, |ui| {
        ui.horizontal(|ui| {
            ui.label("Attachment:");
            let before = match spec {
                AttachmentSpec::None => 0,
                AttachmentSpec::Text(_) => 1,
                AttachmentSpec::KeyValue(_) => 2,
            };
            let mut mode = before;
            ui.radio_value(&mut mode, 0, "None");
            ui.radio_value(&mut mode, 1, "Text");
            ui.radio_value(&mut mode, 2, "Key/value");
            if mode != before {
                *spec = match mode {
                    1 => AttachmentSpec::Text(String::new()),
                    2 => AttachmentSpec::KeyValue(vec![(String::new(), String::new())]),
                    _ => AttachmentSpec::None,
                };
            }
        });
        match spec {
            AttachmentSpec::None => {}
            AttachmentSpec::Text(t) => {
                ui.add(egui::TextEdit::singleline(t).hint_text("attachment text"));
            }
            AttachmentSpec::KeyValue(rows) => {
                let mut remove = None;
                for (i, (k, v)) in rows.iter_mut().enumerate() {
                    ui.horizontal(|ui| {
                        ui.add(egui::TextEdit::singleline(k).desired_width(120.0).hint_text("key"));
                        ui.label("=");
                        ui.add(egui::TextEdit::singleline(v).desired_width(200.0).hint_text("value"));
                        if ui.small_button(format!("Remove row {}", i + 1)).clicked() {
                            remove = Some(i);
                        }
                    });
                }
                if let Some(i) = remove {
                    rows.remove(i);
                }
                if ui.button("+ Add row").clicked() {
                    rows.push((String::new(), String::new()));
                }
                ui.label("Sent as zenoh-ext z_serialize(Vec<(String, String)>).");
            }
        }
    });
}
```

- [ ] **Step 4: Implement the form** in `src/ui/publish.rs`.
  - Replace the `PublishFormState` stub:

```rust
/// Publish-form options and the matching indicator state.
#[derive(Debug, Default)]
pub struct PublishFormState {
    pub options: PublishOptions,
    /// Last matching status for `watched_key`; `None` while unknown.
    pub matching: Option<bool>,
    /// Key the worker is currently watching.
    pub watched_key: String,
}

impl PublishFormState {
    pub fn on_matching(&mut self, key: &str, matching: bool) {
        if key == self.watched_key {
            self.matching = Some(matching);
        }
    }
}

fn matching_words(connected: bool, m: Option<bool>) -> &'static str {
    match (connected, m) {
        (false, _) => "○ not connected",
        (true, None) => "… checking subscribers",
        (true, Some(true)) => "● subscribers present",
        (true, Some(false)) => "○ no subscribers",
    }
}
```

  - In `show_publish_tab`, make these edits by anchor. The pre-P1 line numbers are for `publish.rs`; the text itself is P1 T14's version.
    1. **Key row** (pre-P1 29-32). Directly **before** the row, read whether Key had focus when this frame started:

```rust
            // Read before Key is drawn: egui 0.36.2 keeps `lost_focus()` true one extra frame
            // after a mid-frame focus move, so `lost_focus()` alone cannot tell an Enter typed
            // in Key from one typed in the field the user just clicked into.
            let key_had_focus = ui.memory(|m| m.has_focus(egui::Id::new(crate::ui::PUBLISH_KEY_ID)));
```

       Keep the key `TextEdit` response as `key_resp`, returned from the row's `ui.horizontal` closure (`let key_resp = ui.horizontal(|ui| { … }).inner;`). The row keeps T1's `PUBLISH_KEY_ID` and P3 T14's `.labelled_by(label.id)`, which is how tests and screen readers find the field as "Key:". After the row, add:

```rust
            // Enter in Key submits like the button (F-T19-4), only if Key had focus as the frame began.
            let key_enter = key_had_focus && key_resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
            let connected = matches!(self.connection_status, ConnectionStatus::Connected);
            let key_ok = crate::validation::key_expr_error(&self.publish_key).is_none();
            if connected && key_ok && (key_enter || !key_resp.has_focus()) && self.publish_form.watched_key != self.publish_key {
                self.publish_form.watched_key = self.publish_key.clone();
                self.publish_form.matching = None;
                self.send_command(ZenohCommand::WatchMatching { key: self.publish_key.clone() });
            }
            ui.label(matching_words(connected, self.publish_form.matching)).on_hover_text(
                "Subscribers outside this app's background monitor, including this app's own Subscribe entries.",
            );
            ui.horizontal(|ui| {
                ui.label("Operation:");
                ui.radio_value(&mut self.publish_form.options.kind, PublishKind::Put, "Put");
                ui.radio_value(&mut self.publish_form.options.kind, PublishKind::Delete, "Delete");
            });
            let is_put = self.publish_form.options.kind == PublishKind::Put;
```

    2. **Payload section.** Wrap everything from the `// Payload section with file import` row through the payload `ScrollArea` (pre-P1 34-201) in `ui.add_enabled_ui(is_put, |ui| { … });`.
    3. **Encoding row** (pre-P1 203-206). Replace it with:

```rust
            ui.horizontal(|ui| {
                ui.label("Encoding:");
                crate::encodings::encoding_combo(ui, "publish_encoding", &mut self.publish_encoding);
            });
            egui::CollapsingHeader::new("QoS").id_salt("publish_qos").show(ui, |ui| {
                let o = &mut self.publish_form.options;
                ui.horizontal(|ui| {
                    ui.label("Priority:");
                    egui::ComboBox::from_id_salt("publish_priority").selected_text(o.priority.label()).show_ui(ui, |ui| {
                        for p in PriorityView::ALL {
                            ui.selectable_value(&mut o.priority, p, p.label());
                        }
                    });
                    ui.label("Congestion:");
                    egui::ComboBox::from_id_salt("publish_congestion").selected_text(o.congestion.label()).show_ui(ui, |ui| {
                        for c in CongestionView::ALL {
                            ui.selectable_value(&mut o.congestion, c, c.label());
                        }
                    });
                });
                ui.horizontal(|ui| {
                    ui.checkbox(&mut o.express, "Express (no batching)");
                    ui.label("Reliability:");
                    ui.radio_value(&mut o.reliability, ReliabilityView::Reliable, "Reliable");
                    ui.radio_value(&mut o.reliability, ReliabilityView::BestEffort, "Best effort (marker only)");
                });
            });
            crate::ui::attachment_editor::attachment_editor(ui, "publish_attachment", &mut self.publish_form.options.attachment);
```

    4. **Publish button** (pre-P1 208-254, as P1 T14, P1 T23 and P3 T14 left it). Keep the button exactly as P3 T14 left it, and OR the new triggers into its existing click branch. Keep P1's enable condition `can_publish` and P1 T23's `pending` and `payload_is_empty` locals. Change only the trigger and the label (Delete mode gets its own). If P3 T14 kept `add_enabled` (its `tab_passes_disabled_publish` passed without the fallback), the code is:

```rust
            let submit = std::mem::take(&mut self.shortcut_submit) || key_enter;
            // P1 T23's label and pending guard cover every trigger (F-T15-3).
            let label = match (is_put, pending) {
                (true, _) => publish_button_label(payload_is_empty, pending),
                (false, true) => "Publishing…",
                (false, false) => "Delete key",
            };
            let clicked = ui.add_enabled(can_publish, egui::Button::new(label)).clicked();
            if (clicked || (submit && can_publish)) && !pending && self.command_sender.is_some() {
```

       If P3 T14 took its F-T7-12 fallback instead (its commit body records the choice, and the Publish button is drawn with `ui.add(egui::Button::new(label))`), Publish is always enabled, and a click while `can_publish` is false raises `UiAlert::Warning(reason)`. Do not bring back `add_enabled(can_publish, …)`: that would undo P3's F-T7-12 fix and break its `tab_passes_disabled_publish`. Keep P3's `let clicked = ui.add(egui::Button::new(label)).clicked();` and its refusal, and route `submit` into the same branch: `if (clicked || submit) && !pending { if !can_publish { /* P3's warning, unchanged */ } else if self.command_sender.is_some() { /* step 5 */ } }`. Only reason about `can_publish` in the button line when P3 kept `add_enabled`.

    5. **Command.** Inside that branch, build the payload as P1 left it when `is_put`, and as `Vec::new()` for a delete. Send `ZenohCommand::Publish { …, options: self.publish_form.options.clone() }`, and keep P1 T23's `self.publish_status = Some(PublishStatus::Sending { … })`. **After** the branch, add `if key_enter { key_resp.request_focus(); }`. It sits outside the branch so that focus stays in Key even when nothing was sent (invalid key, pending, not connected), and the next Tab does not restart at the header.
- [ ] **Step 5: Run the tests.** Run `cargo test -- ui::publish ui::attachment_editor`. Expected: 15 passed.
  - T13's 6: in `ui::publish::ui_tests`, `publish_sends_options`, `delete_mode_disables_payload`, `enter_in_key_field_publishes`, `enter_after_leaving_key_does_not_publish` and `matching_indicator_uses_words`; in `ui::attachment_editor::ui_tests`, `attachment_editor_adds_and_removes_rows`.
  - The 9 earlier tests in `ui::publish::tests`, unchanged: P1 T23's `publish_status_line_words` and `publish_button_label_rules`; P3 T12's `apply_import_sets_payload_and_memory`, `import_button_disabled_while_importing`, `apply_import_error_keeps_draft`, `import_infers_and_restores_encoding`, `encoding_for_filename_rules` and `import_label_uses_format_size`; P4 T9's `import_cap_is_plain_publish_max`.
- [ ] **Step 6: Commit.**

```bash
git add src/ui/publish.rs src/ui/attachment_editor.rs
git commit -m "feat(ui): Publish form QoS, put/delete, encoding presets, attachments, matching indicator and Enter to publish

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T14: Query options in the worker (Lane QRY)

**Owns:** `src/worker/query.rs`, `src/worker/samples.rs`

**Interfaces:**
- Consumes (T1): `QueryRequest`, `QueryEvent`, `ReplyRow`, `SampleExtras`, `WorkerState.{query_tokens, query_tasks}`, `attachment::encode`; P1 T4: `message_from_sample`.
- Produces:
  - `handle_query_with_options(st, ctx, req)`, which sends `Query(Reply{…})` per reply (errors included, with `is_error: true`) and then `Query(Finished{cancelled})`.
  - `handle_cancel(st, ctx, id)`, which cancels the token, aborts the task and sends `Finished { cancelled: true }` itself, so it is deterministic.
  - `samples::extras_from_sample(&Sample) -> Option<Box<SampleExtras>>`. `message_from_sample` attaches it.
- zenoh API (1.10.1):
  - `SessionGetBuilder::{target, consolidation, timeout, payload, encoding, attachment, accept_replies(ReplyKeyExpr::Any)}`. `accept_replies` is stable in 1.10.1, and it sets the `_anyke` selector parameter.
  - `.cancellation_token(CancellationToken)` is unstable. It is an inherent method via `#[zenoh_macros::internal_trait]` on `CancellationTokenBuilderTrait`, so no import is needed (https://docs.rs/zenoh/1.10.1/zenoh/cancellation/struct.CancellationToken.html). Its `cancel()` returns an awaitable `ZResult<()>`.
  - `Reply::replier_id() -> Option<EntityGlobalId>` is unstable (https://docs.rs/zenoh/1.10.1/zenoh/query/struct.Reply.html#method.replier_id). `EntityGlobalId` has `zid()` and `eid()` but no `Display`, so it is rendered as `"{zid}:{eid}"`.
  - **Local replies (F-T16-7).** A reply is local when `reply.replier_id()` carries this session's zid (`Session::zid()`), the same rule P1 T5 uses on the legacy query path. It is never decided from attachment text: before P1 T6, every Zenoh Explorer's queryable attached `source:local`, so replies from another machine's Explorer were shown as local. A timeout reply has no `replier_id` and is not local.
  - `ReplyError::{payload, encoding}`.
  - `Sample::{priority, congestion_control, express, reliability (unstable), attachment}`.

- [ ] **Step 1: Write the failing tests** in `src/worker/query.rs`:

```rust
#[cfg(test)]
mod p5_tests {
    use super::*;
    use crate::types::*;
    use crate::worker::state::test_support::{ctx, session, state_with, wait_for};

    fn req(id: RequestId, selector: &str, timeout_ms: u64) -> QueryRequest {
        QueryRequest {
            id,
            selector: selector.into(),
            payload: None,
            encoding: String::new(),
            attachment: AttachmentSpec::None,
            target: QueryTargetView::All,
            consolidation: ConsolidationView::None,
            accept_any_keyexpr: false,
            timeout_ms,
        }
    }

    fn reply_row(ev: ZenohEvent) -> ReplyRow {
        match ev {
            ZenohEvent::Query(QueryEvent::Reply { row, .. }) => row,
            other => panic!("unexpected {other:?}"),
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn query_reply_carries_metadata() {
        let s = session(&[]).await;
        let zid = s.zid().to_string();
        let q = s.declare_queryable("t/q").await.unwrap();
        tokio::spawn(async move {
            while let Ok(query) = q.recv_async().await {
                let att = crate::attachment::encode(&AttachmentSpec::KeyValue(vec![("k".into(), "v".into())])).unwrap();
                query
                    .reply("t/q", r#"{"v":1}"#)
                    .encoding(zenoh::bytes::Encoding::APPLICATION_JSON)
                    .attachment(att)
                    .await
                    .unwrap();
            }
        });
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_query_with_options(&mut st, &ctx, req(1, "t/q", 2000)).await;
        let row = reply_row(wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Query(QueryEvent::Reply { id: 1, .. }))).await.expect("reply"));
        assert_eq!((row.key.as_str(), row.encoding.as_str(), row.preview.as_str()), ("t/q", "application/json", r#"{"v":1}"#));
        assert!(!row.is_error);
        assert_eq!(crate::attachment::describe(row.attachment.as_deref().unwrap()), "k=v");
        if let Some(r) = &row.replier_id {
            assert!(r.starts_with(&zid), "{r}");
        }
        let fin = wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Query(QueryEvent::Finished { id: 1, cancelled: false }))).await;
        assert!(fin.is_some());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn own_session_reply_is_local_by_replier_id() {
        // The reply carries no attachment at all: a text check would call it remote (F-T16-7).
        let s = session(&[]).await;
        let q = s.declare_queryable("t/own").await.unwrap();
        tokio::spawn(async move {
            while let Ok(query) = q.recv_async().await {
                query.reply("t/own", "v").await.unwrap();
            }
        });
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_query_with_options(&mut st, &ctx, req(4, "t/own", 2000)).await;
        let ev = wait_for(&rx, 5, |e| matches!(e, ZenohEvent::MessageReceived(m) if m.key == "t/own")).await;
        let Some(ZenohEvent::MessageReceived(m)) = ev else { panic!("reply sample not forwarded") };
        assert!(m.is_local, "a reply from this session's own queryable is local");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn error_reply_is_reported_as_row() {
        let s = session(&[]).await;
        let q = s.declare_queryable("t/err").await.unwrap();
        tokio::spawn(async move {
            while let Ok(query) = q.recv_async().await {
                query.reply_err("boom").await.unwrap();
            }
        });
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_query_with_options(&mut st, &ctx, req(2, "t/err", 2000)).await;
        let row = reply_row(wait_for(&rx, 5, |e| matches!(e, ZenohEvent::Query(QueryEvent::Reply { id: 2, .. }))).await.expect("error row"));
        assert!(row.is_error);
        assert_eq!(row.preview, "boom");
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancel_finishes_run_as_cancelled() {
        let s = session(&[]).await;
        let _silent = s.declare_queryable("t/slow").await.unwrap(); // never replies
        let mut st = state_with(s);
        let (ctx, rx) = ctx();
        handle_query_with_options(&mut st, &ctx, req(3, "t/slow", 30_000)).await;
        handle_cancel(&mut st, &ctx, 3).await;
        let fin = wait_for(&rx, 3, |e| matches!(e, ZenohEvent::Query(QueryEvent::Finished { id: 3, cancelled: true }))).await;
        assert!(fin.is_some(), "cancel did not finish the run");
        assert!(!st.query_tokens.contains_key(&3) && !st.query_tasks.contains_key(&3));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn sample_extras_capture_attachment_and_qos() {
        let s = session(&[]).await;
        let sub = s.declare_subscriber("t/x").await.unwrap();
        s.put("t/x", "p").attachment(b"hello".to_vec()).await.unwrap();
        let sample = tokio::time::timeout(std::time::Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        let x = crate::worker::samples::extras_from_sample(&sample).expect("extras present when attachment set");
        assert_eq!(x.attachment.as_deref(), Some(&b"hello"[..]));
        s.put("t/x", "plain").await.unwrap();
        let plain = tokio::time::timeout(std::time::Duration::from_secs(5), sub.recv_async()).await.unwrap().unwrap();
        assert!(crate::worker::samples::extras_from_sample(&plain).is_none(), "default QoS and no attachment: no extras");
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test worker::query::p5_tests`. Expected: compile error, because `extras_from_sample` is missing. After adding it, the stubs make the others FAIL.
- [ ] **Step 3: Implement `extras_from_sample`** in `src/worker/samples.rs`, and chain it in `message_from_sample` as `.with_extras(extras_from_sample(sample))` after `.with_sample_meta(…)`:

```rust
/// Attachment and QoS of a received sample; `None` when everything is the wire
/// default (no attachment, Data priority, Drop, not express, Reliable), so the
/// common case allocates nothing.
pub(crate) fn extras_from_sample(sample: &zenoh::sample::Sample) -> Option<Box<SampleExtras>> {
    let x = SampleExtras {
        attachment: sample.attachment().map(|a| a.to_bytes().into_owned()),
        priority: sample.priority().into(),
        congestion: sample.congestion_control().into(),
        express: sample.express(),
        reliability: sample.reliability().into(),
        replier_id: None,
        query_id: None,
    };
    let wire_default = SampleExtras { congestion: CongestionView::Drop, ..Default::default() };
    (x != wire_default).then(|| Box::new(x))
}
```

- [ ] **Step 4: Implement the handlers** in `src/worker/query.rs` (replace the two T1 stubs):

```rust
fn query_failed(ctx: &WorkerCtx, id: RequestId, error: String) {
    let _ = ctx.event_sender.send(ZenohEvent::OperationFailed { op: FailedOp::Query, error });
    let _ = ctx.event_sender.send(ZenohEvent::Query(QueryEvent::Finished { id, cancelled: false }));
}

/// Query with the full option set (P5 T14).
pub(crate) async fn handle_query_with_options(st: &mut WorkerState, ctx: &WorkerCtx, req: QueryRequest) {
    // Forget runs that already ended.
    let done: Vec<RequestId> = st.query_tasks.iter().filter(|(_, h)| h.is_finished()).map(|(k, _)| *k).collect();
    for k in done {
        st.query_tasks.remove(&k);
        st.query_tokens.remove(&k);
    }
    let id = req.id;
    let Some(sess) = st.publishing_session.clone() else {
        return query_failed(ctx, id, "not connected".to_string());
    };
    // Locality comes from the replier's zid, never from attachment text (F-T16-7).
    let own_zid = sess.zid();
    let token = zenoh::cancellation::CancellationToken::default();
    let mut get = sess
        .get(&req.selector)
        .target(req.target.into())
        .consolidation(zenoh::query::ConsolidationMode::from(req.consolidation))
        .timeout(std::time::Duration::from_millis(req.timeout_ms))
        .cancellation_token(token.clone());
    if req.accept_any_keyexpr {
        get = get.accept_replies(zenoh::query::ReplyKeyExpr::Any);
    }
    if let Some(p) = req.payload.clone() {
        get = get.payload(p).encoding(req.encoding.as_str());
    }
    if let Some(a) = crate::attachment::encode(&req.attachment) {
        get = get.attachment(a);
    }
    let replies = match get.await {
        Ok(r) => r,
        Err(e) => return query_failed(ctx, id, format!("{}: {e}", req.selector)),
    };
    st.query_tokens.insert(id, token.clone());
    let tx = ctx.event_sender.clone();
    let drops = ctx.sample_drops.clone();
    let handle = tokio::spawn(async move {
        while let Ok(reply) = replies.recv_async().await {
            // Read before `reply.result()` borrows the reply's sample.
            let is_local = reply.replier_id().is_some_and(|g| g.zid() == own_zid);
            let replier_id = reply.replier_id().map(|g| format!("{}:{}", g.zid(), g.eid()));
            let row = match reply.result() {
                Ok(sample) => {
                    let bytes = sample.payload().to_bytes();
                    let row = ReplyRow {
                        key: sample.key_expr().to_string(),
                        encoding: sample.encoding().to_string(),
                        preview: crate::payload::preview(&bytes, 4096),
                        size: bytes.len(),
                        replier_id: replier_id.clone(),
                        attachment: sample.attachment().map(|a| a.to_bytes().into_owned()),
                        received_at: chrono::Utc::now(),
                        is_error: false,
                    };
                    // Tree and payload store, exactly like the legacy query path.
                    let mut msg = crate::worker::samples::message_from_sample(
                        sample,
                        MessageType::QueryReply,
                        is_local,
                        MessageSource::PublishingSession,
                    );
                    let x = msg.extras.get_or_insert_with(|| Box::new(SampleExtras { congestion: CongestionView::Drop, ..Default::default() }));
                    x.replier_id = replier_id;
                    x.query_id = Some(id);
                    crate::worker::pipeline::send_sample(&tx, &drops, msg);
                    row
                }
                Err(err) => {
                    let bytes = err.payload().to_bytes();
                    ReplyRow {
                        key: String::new(),
                        encoding: err.encoding().to_string(),
                        preview: crate::payload::preview(&bytes, 4096),
                        size: bytes.len(),
                        replier_id,
                        attachment: None,
                        received_at: chrono::Utc::now(),
                        is_error: true,
                    }
                }
            };
            if tx.send(ZenohEvent::Query(QueryEvent::Reply { id, row })).is_err() {
                return;
            }
        }
        let _ = tx.send(ZenohEvent::Query(QueryEvent::Finished { id, cancelled: token.is_cancelled() }));
    });
    st.query_tasks.insert(id, handle);
}

/// Cancel a running query: stop it on the network, stop its task, and report
/// the run as cancelled (the UI treats a second `Finished` as a no-op).
pub(crate) async fn handle_cancel(st: &mut WorkerState, ctx: &WorkerCtx, id: RequestId) {
    if let Some(token) = st.query_tokens.remove(&id) {
        if let Err(e) = token.cancel().await {
            let _ = ctx.event_sender.send(ZenohEvent::OperationFailed { op: FailedOp::Query, error: format!("cancel {id}: {e}") });
        }
    }
    if let Some(t) = st.query_tasks.remove(&id) {
        t.abort();
    }
    let _ = ctx.event_sender.send(ZenohEvent::Query(QueryEvent::Finished { id, cancelled: true }));
}
```

- [ ] **Step 5: Run the tests.** Run `cargo test -- worker::query worker::samples`. Expected: the 5 `p5_tests` pass, along with P1's query and sample tests. Then run `grep -n 'source:local' src/worker/query.rs`. Expected: no output (F-T16-7).
  - `own_session_reply_is_local_by_replier_id` matches `ZenohMessage.key` and `.is_local` as P1 T3/T4 named them. If P1 named them differently, use P1's names.
- [ ] **Step 6: Commit.**

```bash
git add src/worker/query.rs src/worker/samples.rs
git commit -m "feat(worker): query options, cancellation, reply metadata/errors and sample extras

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T15: Selector parameters (Lane QRY)

**Owns:** `src/selector_params.rs`

**Interfaces:**
- Produces:
  - `struct ParamRow { key: String, value: String }`
  - `const TIME_PRESETS: &[(&str, &str)]`
  - `fn split_selector(&str) -> (&str, &str)`
  - `fn parse_params(&str) -> Vec<ParamRow>`
  - `fn build_selector(key: &str, rows: &[ParamRow]) -> String`
  - `fn escape(&str) -> String` and `fn unescape(&str) -> String`
  - `fn time_range_error(&str) -> Option<String>`
- **Facts:**
  - Zenoh splits parameters on `;` and then on the first `=`, and does not percent-decode (zenoh-protocol 1.10.1 `core/parameters.rs`).
  - `_time` accepts `[<start>..<end>]` with `now(<±duration>)` offsets; `zenoh::query::TimeRange` implements `FromStr` (unstable, zenoh-util 1.10.1 `time_range.rs`; https://docs.rs/zenoh/1.10.1/zenoh/query/struct.TimeRange.html).
  - The explorer percent-encodes `%`, `;` and `#` in keys and values, so that a value can contain them. A queryable sees the encoded text, and the hover help says so.

- [ ] **Step 1: Write the failing tests** in `src/selector_params.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn row(k: &str, v: &str) -> ParamRow {
        ParamRow { key: k.into(), value: v.into() }
    }

    #[test]
    fn split_and_parse() {
        assert_eq!(split_selector("a/b?x=1;y"), ("a/b", "x=1;y"));
        assert_eq!(split_selector("a/b"), ("a/b", ""));
        assert_eq!(parse_params("x=1;y;;z=a=b"), vec![row("x", "1"), row("y", ""), row("z", "a=b")]);
    }

    #[test]
    fn roundtrip_escapes_separators() {
        let rows = vec![row("q", "a;b#c%d"), row("_time", "[now(-1h)..]")];
        let sel = build_selector("demo/**", &rows);
        assert_eq!(sel, "demo/**?q=a%3Bb%23c%25d;_time=[now(-1h)..]");
        let (k, p) = split_selector(&sel);
        assert_eq!(k, "demo/**");
        assert_eq!(parse_params(p), rows);
        assert_eq!(unescape("%253B"), "%3B"); // %25 decodes last
    }

    #[test]
    fn build_skips_empty_keys_and_bare_flags() {
        assert_eq!(build_selector("k", &[row("", "x"), row("flag", "")]), "k?flag");
        assert_eq!(build_selector("k", &[]), "k");
    }

    #[test]
    fn time_presets_parse() {
        for (_, v) in TIME_PRESETS {
            assert_eq!(time_range_error(v), None, "{v}");
        }
        assert!(time_range_error("[yesterday..]").is_some());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test selector_params::`. Expected: compile errors.
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Selector parameter editing: `key?k=v;k2=v2` (P5 T15).
//!
//! Zenoh splits parameters on `;` and the first `=`, and does not
//! percent-decode. The explorer percent-encodes `%`, `;` and `#` in keys and
//! values, so a value can contain them, and decodes them when parsing.

/// One `key=value` parameter (`value` empty for a bare flag).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ParamRow {
    pub key: String,
    pub value: String,
}

/// `_time` presets (zenoh time DSL; open end means "until now").
pub const TIME_PRESETS: &[(&str, &str)] = &[
    ("Last 5 minutes", "[now(-5m)..]"),
    ("Last hour", "[now(-1h)..]"),
    ("Last day", "[now(-1d)..]"),
    ("Last hour, closed", "[now(-1h)..now()]"),
];

pub fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '%' => out.push_str("%25"),
            ';' => out.push_str("%3B"),
            '#' => out.push_str("%23"),
            _ => out.push(c),
        }
    }
    out
}

pub fn unescape(s: &str) -> String {
    s.replace("%3B", ";").replace("%3b", ";").replace("%23", "#").replace("%25", "%")
}

/// `(key expression, parameters)`; parameters are empty without `?`.
pub fn split_selector(s: &str) -> (&str, &str) {
    s.split_once('?').unwrap_or((s, ""))
}

pub fn parse_params(params: &str) -> Vec<ParamRow> {
    params
        .split(';')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            ParamRow { key: unescape(k), value: unescape(v) }
        })
        .collect()
}

pub fn build_selector(key: &str, rows: &[ParamRow]) -> String {
    let parts: Vec<String> = rows
        .iter()
        .filter(|r| !r.key.trim().is_empty())
        .map(|r| {
            if r.value.is_empty() {
                escape(&r.key)
            } else {
                format!("{}={}", escape(&r.key), escape(&r.value))
            }
        })
        .collect();
    if parts.is_empty() {
        key.to_string()
    } else {
        format!("{key}?{}", parts.join(";"))
    }
}

/// Error text when `v` is not a valid zenoh `_time` range.
pub fn time_range_error(v: &str) -> Option<String> {
    v.parse::<zenoh::query::TimeRange>().err().map(|e| e.to_string())
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test selector_params::`. Expected: 4 passed.
- [ ] **Step 5: Commit.**

```bash
git add src/selector_params.rs
git commit -m "feat(query): selector parameter model with escaping and _time presets

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T16: Query form and grouped results (Lane QRY)

**Owns:** `src/ui/query.rs`, `src/query_book.rs`

**Interfaces:**
- Consumes:
  - T1: `QueryRequest`, `QueryEvent`, `ReplyRow`, `ZenohCommand::{QueryWithOptions, CancelQuery}`, `encodings::encoding_combo`, `shortcut_submit`, `next_request_id`.
  - T13: `ui::attachment_editor::attachment_editor`.
  - T15: `selector_params::*`.
  - P1: `validation::selector_error`, `types::format_local_time` (T3), the Query timing note and the state-worded "not connected" notice (T23), and `process_events` (for the wiring test).
  - P3 T14: the Selector field's `labelled_by`, and the Query button as T14 left it: `add_enabled(can_query, …)`, or its F-T7-12 fallback (always enabled, a refused click raises `UiAlert::Warning(reason)`).
- Produces:
  - `QueryRun { id, selector, started: DateTime<Utc>, replies: Vec<ReplyRow>, errors: usize, dropped: usize, state: RunState, disconnected: bool, earlier_session: bool }`.
  - `enum RunState { Running, Done, Cancelled }`.
  - `QueryBook`, with `start`, `apply`, `on_disconnected`, `clear`, `clear_run`, `runs`, `is_running` and `latest_running`. The caps are `MAX_RUNS = 20` and `MAX_REPLIES_PER_RUN = 1000`.
- **Rules:**
  - **Disconnect (F-T16-11).** `on_disconnected` is called from T1's `Disconnected` arm, outside P1 T12's status guard, so it also runs when a quick reconnect has already set the status to `ConnectingPublishing`. It ends every running run as `Cancelled` with `disconnected = true`, and that run's summary reads "cancelled: disconnected, N replies". The worker's teardown aborts the query tasks, so without this a run would say "running" for ever. A late `Finished` for that run is ignored, as for any finished run. `on_disconnected` also sets `earlier_session = true` on **every** run in the book, and the run's title then ends in "· earlier session". Results from a closed session are no longer shown as if they came from the current one (the finding's second recommendation).
  - **Enter (F-T19-4).** Enter in the Selector field sends the query through the same `can_query` check as the Query button. Focus stays in Selector whether or not the query was sent. As in T13, Selector must have had focus when the frame started: egui 0.36.2 keeps `lost_focus()` true one extra frame after a mid-frame focus move, so without that check an Enter typed in Value or a parameter field just after leaving Selector would send the query.
  - **Times (F-T14-6).** Run and reply times use `format_local_time`, never `.format(…)` on a UTC time.
  - **Timing note (F-T16-2).** The Query view keeps P1 T23's one-line note. Do not bring back "If no queryables are running, queries will timeout".
  - `QueryFormState { target, consolidation, accept_any, payload, payload_encoding, attachment }`.

- [ ] **Step 1: Write the failing tests.**
  - In `src/query_book.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::*;

    fn row() -> ReplyRow {
        ReplyRow {
            key: "k".into(),
            encoding: "text/plain".into(),
            preview: "p".into(),
            size: 1,
            replier_id: None,
            attachment: None,
            received_at: chrono::Utc::now(),
            is_error: false,
        }
    }

    #[test]
    fn query_book_caps_runs_and_replies() {
        let mut b = QueryBook::default();
        for id in 0..(MAX_RUNS as u64 + 5) {
            b.start(id, format!("q/{id}"));
        }
        assert_eq!(b.runs().count(), MAX_RUNS);
        assert_eq!(b.runs().next().unwrap().id, MAX_RUNS as u64 + 4, "newest first");
        let id = MAX_RUNS as u64 + 4;
        for _ in 0..(MAX_REPLIES_PER_RUN + 10) {
            b.apply(QueryEvent::Reply { id, row: row() });
        }
        let run = b.runs().next().unwrap();
        assert_eq!(run.replies.len(), MAX_REPLIES_PER_RUN);
        assert_eq!(run.dropped, 10);
    }

    #[test]
    fn finished_run_without_replies_says_no_replies() {
        let mut b = QueryBook::default();
        b.start(1, "demo/**".into());
        assert!(b.is_running(1));
        b.apply(QueryEvent::Finished { id: 1, cancelled: false });
        b.apply(QueryEvent::Finished { id: 1, cancelled: true }); // late duplicate: first wins
        let run = b.runs().next().unwrap();
        assert_eq!(run.state, RunState::Done);
        assert_eq!(run.summary(), "no replies");
        b.start(2, "x".into());
        b.apply(QueryEvent::Finished { id: 2, cancelled: true });
        assert_eq!(b.runs().next().unwrap().summary(), "cancelled, 0 replies");
        b.clear_run(2);
        assert_eq!(b.runs().count(), 1);
        b.clear();
        assert_eq!(b.runs().count(), 0);
    }

    #[test]
    fn disconnect_ends_running_runs_as_cancelled() {
        let mut b = QueryBook::default();
        b.start(1, "done/**".into());
        b.apply(QueryEvent::Finished { id: 1, cancelled: false });
        b.start(2, "slow/**".into());
        b.apply(QueryEvent::Reply { id: 2, row: row() });
        b.on_disconnected();
        assert!(!b.is_running(2));
        assert_eq!(b.latest_running(), None, "no Cancel button after a disconnect");
        let slow = b.runs().find(|r| r.id == 2).unwrap();
        assert_eq!((slow.state, slow.disconnected), (RunState::Cancelled, true));
        assert_eq!(slow.summary(), "cancelled: disconnected, 1 replies");
        let done = b.runs().find(|r| r.id == 1).unwrap();
        assert_eq!((done.state, done.disconnected), (RunState::Done, false), "finished runs keep their verdict");
        assert!(slow.earlier_session && done.earlier_session, "every run before the disconnect is marked as from an earlier session");
        b.apply(QueryEvent::Finished { id: 2, cancelled: false }); // late event from the dead session
        assert_eq!(b.runs().find(|r| r.id == 2).unwrap().state, RunState::Cancelled);
        b.start(3, "after/**".into());
        assert!(!b.runs().find(|r| r.id == 3).unwrap().earlier_session, "a run of the new session is not marked");
    }
}
```

  - At the bottom of `src/ui/query.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    fn connected() -> (ZenohExplorer, std::sync::mpsc::Receiver<ZenohCommand>) {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        app.query_selector = "demo/**".into();
        (app, cmds)
    }

    #[test]
    fn query_sends_options_and_registers_run() {
        let (mut app, cmds) = connected();
        app.query_form.target = QueryTargetView::AllComplete;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_query_tab(ui), app);
        h.run();
        h.get_by_label("Accept replies on any key").click();
        h.run();
        h.get_by_label("Query").click();
        h.run();
        let req = cmds
            .try_iter()
            .find_map(|c| if let ZenohCommand::QueryWithOptions(r) = c { Some(r) } else { None })
            .expect("QueryWithOptions sent");
        assert_eq!((req.selector.as_str(), req.target, req.accept_any_keyexpr), ("demo/**", QueryTargetView::AllComplete, true));
        assert!(h.state().query_book.is_running(req.id));
        h.get_by_label_contains("running");
    }

    #[test]
    fn enter_in_selector_sends_query() {
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_query_tab(ui), app);
        h.run();
        h.get_by_label("Selector:").focus();
        h.run();
        h.key_press(egui::Key::Enter);
        h.run();
        assert!(
            cmds.try_iter().any(|c| matches!(c, ZenohCommand::QueryWithOptions(ref r) if r.selector == "demo/**")),
            "Enter in Selector sends the query (F-T19-4)"
        );
        h.run();
        assert_eq!(
            h.ctx.memory(|m| m.focused()),
            Some(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)),
            "focus stays in Selector after Enter"
        );
    }

    #[test]
    fn enter_after_leaving_selector_does_not_query() {
        // egui 0.36.2 keeps Selector's `lost_focus()` true for one more frame after focus
        // moves mid-frame, so an Enter typed in Value on the very next frame must not query.
        let (app, cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_query_tab(ui), app);
        h.run();
        h.get_by_label("Selector:").focus();
        h.run();
        h.get_by_label("Value (optional):").focus();
        h.step(); // one frame: focus moves from Selector to Value while it is drawn
        h.key_press(egui::Key::Enter);
        h.step(); // the next frame is the Enter press, typed in Value
        assert!(
            !cmds.try_iter().any(|c| matches!(c, ZenohCommand::QueryWithOptions(_))),
            "Enter in Value does not send the query (F-T19-4)"
        );
    }

    #[test]
    fn disconnect_event_ends_running_run() {
        // Goes through T1's `Disconnected` arm in `process_events`, not just the book.
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        app.query_book.start(7, "slow/**".into());
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert!(!app.query_book.is_running(7), "a disconnect ends the running query (F-T16-11)");
        assert_eq!(app.query_book.runs().next().unwrap().summary(), "cancelled: disconnected, 0 replies");
    }

    #[test]
    fn disconnect_while_reconnecting_ends_running_run() {
        // Disconnect, then Connect at once: the status is already ConnectingPublishing when the
        // old session's `Disconnected` arrives. P1 T12 then skips only its status reset; the
        // worker's teardown still aborted the query task, so the run must end here too.
        let (mut app, tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        app.query_book.start(8, "slow/**".into());
        tx.send(ZenohEvent::Disconnected).unwrap();
        app.process_events();
        assert!(
            matches!(app.connection_status, ConnectionStatus::ConnectingPublishing),
            "precondition: P1 T12's guard kept the new connect's status"
        );
        let run = app.query_book.runs().next().unwrap();
        assert_eq!(run.summary(), "cancelled: disconnected, 0 replies", "T1 calls on_disconnected outside P1 T12's guard");
        assert!(run.earlier_session);
        assert_eq!(app.query_book.latest_running(), None, "no live Cancel button for a dead run");
    }

    #[test]
    fn cancel_button_sends_cancel() {
        let (mut app, cmds) = connected();
        app.query_book.start(9, "demo/**".into());
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_query_tab(ui), app);
        h.run();
        h.get_by_label("Cancel").click();
        h.run();
        assert!(cmds.try_iter().any(|c| matches!(c, ZenohCommand::CancelQuery { id: 9 })));
    }

    #[test]
    fn param_editor_rewrites_selector() {
        let (app, _cmds) = connected();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_query_tab(ui), app);
        h.run();
        h.get_by_label("Parameters").click();
        h.run();
        h.get_by_label("Time range…").click();
        h.run();
        h.get_by_label("Last hour").click();
        h.run();
        assert_eq!(h.state().query_selector, "demo/**?_time=[now(-1h)..]");
    }
}
```

  P3 T14 labels the Value field "Value (optional):" (`query_fields_are_labelled`). `h.step()` runs one frame per queued event, as in T13's `enter_after_leaving_key_does_not_publish`.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- query_book:: ui::query`. Expected: compile errors.
- [ ] **Step 3: Implement `src/query_book.rs`** (replace the stub):

```rust
//! Query runs grouped by request id, newest first (P5 T16).

use std::collections::VecDeque;

use chrono::{DateTime, Utc};

use crate::types::{QueryEvent, ReplyRow, RequestId};

pub const MAX_RUNS: usize = 20;
pub const MAX_REPLIES_PER_RUN: usize = 1000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunState {
    Running,
    Done,
    Cancelled,
}

#[derive(Debug, Clone)]
pub struct QueryRun {
    pub id: RequestId,
    pub selector: String,
    pub started: DateTime<Utc>,
    pub replies: Vec<ReplyRow>,
    pub errors: usize,
    /// Replies beyond `MAX_REPLIES_PER_RUN`, counted but not kept.
    pub dropped: usize,
    pub state: RunState,
    /// Ended by a disconnect rather than by the worker or the Cancel button.
    pub disconnected: bool,
    /// Started before the last disconnect, so it belongs to a closed session.
    pub earlier_session: bool,
}

impl QueryRun {
    /// One-line status in words (never colour alone).
    pub fn summary(&self) -> String {
        let n = self.replies.len() + self.dropped;
        match self.state {
            RunState::Running => format!("running, {n} replies"),
            RunState::Done if n == 0 => "no replies".to_string(),
            RunState::Done => format!("done, {n} replies, {} errors", self.errors),
            RunState::Cancelled if self.disconnected => format!("cancelled: disconnected, {n} replies"),
            RunState::Cancelled => format!("cancelled, {n} replies"),
        }
    }
}

#[derive(Debug, Default)]
pub struct QueryBook {
    runs: VecDeque<QueryRun>,
}

impl QueryBook {
    pub fn start(&mut self, id: RequestId, selector: String) {
        self.runs.push_front(QueryRun {
            id,
            selector,
            started: Utc::now(),
            replies: Vec::new(),
            errors: 0,
            dropped: 0,
            state: RunState::Running,
            disconnected: false,
            earlier_session: false,
        });
        self.runs.truncate(MAX_RUNS);
    }

    pub fn apply(&mut self, ev: QueryEvent) {
        match ev {
            QueryEvent::Reply { id, row } => {
                let Some(run) = self.runs.iter_mut().find(|r| r.id == id) else { return };
                if row.is_error {
                    run.errors += 1;
                }
                if run.replies.len() < MAX_REPLIES_PER_RUN {
                    run.replies.push(row);
                } else {
                    run.dropped += 1;
                }
            }
            QueryEvent::Finished { id, cancelled } => {
                if let Some(run) = self.runs.iter_mut().find(|r| r.id == id && r.state == RunState::Running) {
                    run.state = if cancelled { RunState::Cancelled } else { RunState::Done };
                }
            }
        }
    }

    /// The session is gone: no `Finished` will arrive for running queries, so end them here,
    /// and mark every kept run as belonging to that closed session (F-T16-11).
    pub fn on_disconnected(&mut self) {
        for run in self.runs.iter_mut() {
            if run.state == RunState::Running {
                run.state = RunState::Cancelled;
                run.disconnected = true;
            }
            run.earlier_session = true;
        }
    }

    pub fn clear(&mut self) {
        self.runs.clear();
    }

    pub fn clear_run(&mut self, id: RequestId) {
        self.runs.retain(|r| r.id != id);
    }

    pub fn runs(&self) -> impl Iterator<Item = &QueryRun> {
        self.runs.iter()
    }

    pub fn is_running(&self, id: RequestId) -> bool {
        self.runs.iter().any(|r| r.id == id && r.state == RunState::Running)
    }

    pub fn latest_running(&self) -> Option<RequestId> {
        self.runs.iter().find(|r| r.state == RunState::Running).map(|r| r.id)
    }
}
```

- [ ] **Step 4: Implement the form** in `src/ui/query.rs`.
  - Replace the `QueryFormState` stub:

```rust
/// Options of the Query form beyond selector, value and timeout.
#[derive(Debug)]
pub struct QueryFormState {
    pub target: QueryTargetView,
    pub consolidation: ConsolidationView,
    pub accept_any: bool,
    pub payload_encoding: String,
    pub attachment: AttachmentSpec,
}

impl Default for QueryFormState {
    fn default() -> Self {
        Self {
            target: QueryTargetView::All,
            consolidation: ConsolidationView::None,
            accept_any: false,
            payload_encoding: "text/plain".to_string(),
            attachment: AttachmentSpec::None,
        }
    }
}
```

    The defaults `All` and `None` preserve the explorer's current behaviour (pre-P1 `zenoh_worker.rs:653-655`).
  - In `show_query_tab`:
    - Keep P1's state-worded "not connected" notice (P1 T23, F-T16-11), P1 T23's one-line timing note (F-T16-2: "Asks every queryable that matches the selector. With no match the answer comes back at once; a matching queryable that stays silent is reported when the timeout expires."), the alert group, and the selector, value and timeout rows (with P1 T14's validation). Do not restore the old "queries will timeout" lines.
    - **Selector row.** It keeps T1's `QUERY_SELECTOR_ID` and P3 T14's `.labelled_by(label.id)`, which is how tests find it as "Selector:". Directly **before** the row, add:

```rust
            // Read before Selector is drawn: egui 0.36.2 keeps `lost_focus()` true one extra
            // frame after a mid-frame focus move (see T13).
            let selector_had_focus = ui.memory(|m| m.has_focus(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)));
```

      Return the `TextEdit` response from the row's `ui.horizontal` closure as `selector_resp` (`let selector_resp = ui.horizontal(|ui| { … }).inner;`). After the row, add:

```rust
            // Enter in Selector sends the query like the button (F-T19-4), only if Selector had focus as the frame began.
            let selector_enter =
                selector_had_focus && selector_resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
```

    - After the timeout row, add the options, the parameter editor and the attachment:

```rust
            ui.horizontal(|ui| {
                ui.label("Target:");
                egui::ComboBox::from_id_salt("query_target").selected_text(self.query_form.target.label()).show_ui(ui, |ui| {
                    for t in QueryTargetView::ALL {
                        ui.selectable_value(&mut self.query_form.target, t, t.label());
                    }
                });
                ui.label("Consolidation:");
                egui::ComboBox::from_id_salt("query_consolidation")
                    .selected_text(self.query_form.consolidation.label())
                    .show_ui(ui, |ui| {
                        for c in ConsolidationView::ALL {
                            ui.selectable_value(&mut self.query_form.consolidation, c, c.label());
                        }
                    });
                ui.checkbox(&mut self.query_form.accept_any, "Accept replies on any key");
            });
            if !self.query_value.is_empty() {
                ui.horizontal(|ui| {
                    ui.label("Value encoding:");
                    crate::encodings::encoding_combo(ui, "query_encoding", &mut self.query_form.payload_encoding);
                });
            }
            egui::CollapsingHeader::new("Parameters").id_salt("query_params").show(ui, |ui| {
                let (key, params) = crate::selector_params::split_selector(&self.query_selector);
                let key = key.to_string();
                let mut rows = crate::selector_params::parse_params(params);
                let mut changed = false;
                let mut remove = None;
                for (i, r) in rows.iter_mut().enumerate() {
                    ui.horizontal(|ui| {
                        changed |= ui.add(egui::TextEdit::singleline(&mut r.key).desired_width(100.0).hint_text("name")).changed();
                        ui.label("=");
                        changed |= ui.add(egui::TextEdit::singleline(&mut r.value).desired_width(220.0).hint_text("value")).changed();
                        if r.key == "_time" {
                            if let Some(e) = crate::selector_params::time_range_error(&r.value) {
                                ui.label(format!("⚠ {e}"));
                            }
                        }
                        if ui.small_button(format!("Remove parameter {}", i + 1)).clicked() {
                            remove = Some(i);
                        }
                    });
                }
                if let Some(i) = remove {
                    rows.remove(i);
                    changed = true;
                }
                ui.horizontal(|ui| {
                    if ui.button("+ Add parameter").clicked() {
                        rows.push(crate::selector_params::ParamRow { key: "name".into(), value: String::new() });
                        changed = true;
                    }
                    ui.menu_button("Time range…", |ui| {
                        for (label, value) in crate::selector_params::TIME_PRESETS {
                            if ui.button(*label).clicked() {
                                rows.retain(|r| r.key != "_time");
                                rows.push(crate::selector_params::ParamRow { key: "_time".into(), value: (*value).into() });
                                changed = true;
                                ui.close();
                            }
                        }
                    });
                });
                ui.label("Values containing % ; # are percent-encoded; queryables see the encoded text.");
                if changed {
                    self.query_selector = crate::selector_params::build_selector(&key, &rows);
                }
            });
            crate::ui::attachment_editor::attachment_editor(ui, "query_attachment", &mut self.query_form.attachment);
```

      `ui.menu_button` and `Ui::close()` are egui 0.36's menu API (https://docs.rs/egui/0.36.2/egui/struct.Ui.html#method.menu_button). If `close()` is not found, use `ui.close_menu()`.
    - Replace the Query button block (pre-P1 73-97). Keep the button line exactly as P3 T14 left it and OR `submit` into its existing click branch. If P3 T14 kept `add_enabled`, the block is:

```rust
            let submit = std::mem::take(&mut self.shortcut_submit) || selector_enter;
            ui.horizontal(|ui| {
                let clicked = ui.add_enabled(can_query, egui::Button::new("Query")).clicked();
                if clicked || (submit && can_query) {
                    let id = self.next_request_id();
                    let payload = (!self.query_value.is_empty()).then(|| self.query_value.as_bytes().to_vec());
                    let req = QueryRequest {
                        id,
                        selector: self.query_selector.clone(),
                        payload,
                        encoding: self.query_form.payload_encoding.clone(),
                        attachment: self.query_form.attachment.clone(),
                        target: self.query_form.target,
                        consolidation: self.query_form.consolidation,
                        accept_any_keyexpr: self.query_form.accept_any,
                        timeout_ms,
                    };
                    self.query_book.start(id, req.selector.clone());
                    self.send_command(ZenohCommand::QueryWithOptions(req));
                    self.query_alert = None;
                }
                if let Some(id) = self.query_book.latest_running() {
                    if ui.button("Cancel").clicked() {
                        self.send_command(ZenohCommand::CancelQuery { id });
                    }
                }
            });
            // Outside the send branch: focus stays in Selector even when nothing was sent (F-T19-4).
            if selector_enter {
                selector_resp.request_focus();
            }
```

      Here `can_query` and `timeout_ms: u64` are P1 T14's validated selector and timeout. If P1 named them differently, use P1's names.

      If P3 T14 took its F-T7-12 fallback instead (its commit body records the choice, and Query is drawn with `ui.button("Query")`), Query is always enabled, and a click while `can_query` is false raises `UiAlert::Warning(reason)`. Do not bring back `add_enabled(can_query, …)`: that would undo P3's F-T7-12 fix. Keep P3's button line and refusal, and route `submit` into the same branch: `if clicked || submit { if !can_query { /* P3's warning, unchanged */ } else { /* the send above */ } }`. The Cancel button and the focus line after the `horizontal` stay as shown. Only reason about `can_query` in the button line when P3 kept `add_enabled`.
    - Replace the "Query Results" group (pre-P1 102-196, the filter over `self.messages`) with the grouped runs:

```rust
        ui.group(|ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("Query Results").strong());
                if ui.button("Clear all").clicked() {
                    self.query_book.clear();
                }
            });
            ui.separator();
            if self.query_book.runs().next().is_none() {
                ui.label("No query results yet. Send a query to see results here.");
                return;
            }
            let mut clear = None;
            let now = chrono::Utc::now();
            egui::ScrollArea::vertical().id_salt("query_runs").auto_shrink([false; 2]).show(ui, |ui| {
                for run in self.query_book.runs() {
                    let started = crate::types::format_local_time(&run.started, &now);
                    let session = if run.earlier_session { " · earlier session" } else { "" };
                    let title = format!("#{} {} · {} · {}{session}", run.id, run.selector, started, run.summary());
                    egui::CollapsingHeader::new(title).id_salt(("query_run", run.id)).default_open(true).show(ui, |ui| {
                        if ui.small_button(format!("Clear #{}", run.id)).clicked() {
                            clear = Some(run.id);
                        }
                        for r in &run.replies {
                            ui.group(|ui| {
                                if r.is_error {
                                    ui.label(RichText::new(format!("ERROR reply · {}", r.encoding)).strong());
                                } else {
                                    ui.label(RichText::new(&r.key).strong());
                                }
                                let received = crate::types::format_local_time(&r.received_at, &now);
                                let mut meta = format!("{} · {} bytes · received {}", r.encoding, r.size, received);
                                if let Some(id) = &r.replier_id {
                                    meta.push_str(&format!(" · from {id}"));
                                }
                                ui.label(RichText::new(meta).size(TEXT_SMALL_SIZE));
                                if let Some(a) = &r.attachment {
                                    ui.label(format!("attachment: {}", crate::attachment::describe(a)));
                                }
                                ui.label(RichText::new(&r.preview).monospace());
                            });
                        }
                        if run.dropped > 0 {
                            ui.label(format!("… {} more replies not kept", run.dropped));
                        }
                    });
                }
            });
            if let Some(id) = clear {
                self.query_book.clear_run(id);
            }
        });
```

- [ ] **Step 5: Run the tests.** Run `cargo test -- query_book:: ui::query`. Expected: 12 passed: 3 in `query_book::tests`, T16's 7 in `ui::query::ui_tests`, and P1 T23's `connection_notice_matches_state` and `queryable_summary_words` in `ui::query::tests`. Then run `grep -n 'will timeout\|format("%H' src/ui/query.rs`. Expected: no output (F-T16-2, F-T14-6).
- [ ] **Step 6: Commit.**

```bash
git add src/ui/query.rs src/query_book.rs
git commit -m "feat(ui): Query options, parameter editor with time presets, cancel, Enter to send and per-query result groups

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T17: Payload decoders (Lane PAY)

**Owns:** `src/decode.rs`

**Interfaces:**
- Produces:
  - `enum ViewerTab { Text, Json, Hex, Cbor, Image }`, with `label()`.
  - `enum ImageKind { Png, Jpeg }`, with `extension()`.
  - `HEX_BYTES_PER_LINE = 16` and `MAX_PRETTY_BYTES = 4 MiB`.
  - `fn hex_line_count(len: usize) -> usize` and `fn hex_line(bytes: &[u8], line: usize) -> String`.
  - `fn text_view(bytes: &[u8]) -> Option<&str>`.
  - `fn pretty_json(bytes: &[u8]) -> Option<String>`.
  - `fn cbor_to_json(bytes: &[u8]) -> Result<String, String>`.
  - `fn image_kind(encoding: &str, bytes: &[u8]) -> Option<ImageKind>`, which requires magic bytes.
  - `fn available_tabs(encoding: &str, bytes: &[u8]) -> Vec<ViewerTab>`.
- ciborium 0.2.2:
  - `ciborium::from_reader::<ciborium::Value, _>(&[u8])` and `ciborium::into_writer`.
  - `ciborium::Value` is `#[non_exhaustive]`, so a wildcard arm is required.
  - `i128: From<ciborium::value::Integer>` (https://docs.rs/ciborium/0.2.2/ciborium/value/enum.Value.html).
- **Protobuf and CDR are out of scope** (schema needed, see the P6 list). CBOR is decoded because it is self-describing.

- [ ] **Step 1: Write the failing tests** in `src/decode.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    const PNG_1X1: [u8; 67] = [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0A, 0x49,
        0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00,
        0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ];

    #[test]
    fn hex_line_format() {
        let b = b"Hello, world!\x00\x01";
        assert_eq!(hex_line(b, 0), "00000000  48 65 6c 6c 6f 2c 20 77  6f 72 6c 64 21 00 01     |Hello, world!..|");
        assert_eq!(hex_line(b, 1), "");
        assert_eq!((hex_line_count(0), hex_line_count(16), hex_line_count(17)), (0, 1, 2));
        assert!(hex_line(&[0u8; 32], 1).starts_with("00000010  00 00"));
    }

    #[test]
    fn json_and_text() {
        assert_eq!(pretty_json(br#"{"a":[1,2]}"#).unwrap(), "{\n  \"a\": [\n    1,\n    2\n  ]\n}");
        assert!(pretty_json(b"hello").is_none());
        assert_eq!(text_view(b"hi"), Some("hi"));
        assert_eq!(text_view(&[0xff]), None);
    }

    #[test]
    fn cbor_roundtrip_to_json() {
        use ciborium::Value;
        let v = Value::Map(vec![
            (Value::Text("a".into()), Value::Integer(1.into())),
            (Value::Text("b".into()), Value::Array(vec![Value::Bool(true), Value::Null])),
            (Value::Text("c".into()), Value::Bytes(vec![0x00, 0xff])),
            (Value::Integer(7.into()), Value::Tag(1, Box::new(Value::Integer(1_700_000_000.into())))),
        ]);
        let mut buf = Vec::new();
        ciborium::into_writer(&v, &mut buf).unwrap();
        let json: serde_json::Value = serde_json::from_str(&cbor_to_json(&buf).unwrap()).unwrap();
        assert_eq!(
            json,
            serde_json::json!({"a": 1, "b": [true, null], "c": "h'00ff'", "7": {"tag": 1, "value": 1_700_000_000}})
        );
    }

    #[test]
    fn cbor_garbage_is_error_not_panic() {
        assert!(cbor_to_json(&[]).is_err());
        assert!(cbor_to_json(&[0xa1]).is_err()); // map header, no entries
        assert!(cbor_to_json(&[0xff, 0xff, 0x00]).is_err());
    }

    #[test]
    fn image_kind_needs_magic_bytes() {
        assert_eq!(image_kind("image/png", b"not a png"), None);
        assert_eq!(image_kind("", &PNG_1X1), Some(ImageKind::Png));
        assert_eq!(image_kind("image/jpeg", &[0xff, 0xd8, 0xff, 0xe0, 0, 0]), Some(ImageKind::Jpeg));
    }

    #[test]
    fn available_tabs_by_content() {
        use ViewerTab::*;
        assert_eq!(available_tabs("application/json", br#"{"a":1}"#), vec![Text, Json, Hex]);
        assert_eq!(available_tabs("text/plain", b"hello"), vec![Text, Hex]);
        let mut cbor = Vec::new();
        ciborium::into_writer(&ciborium::Value::Bytes(vec![0xff; 4]), &mut cbor).unwrap();
        assert_eq!(available_tabs("application/cbor", &cbor), vec![Cbor, Hex]);
        assert_eq!(available_tabs("image/png", &PNG_1X1), vec![Image, Hex]);
        assert_eq!(available_tabs("", b""), vec![Text, Hex]);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test decode::`. Expected: compile errors.
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Payload decoders for the viewer: hex dump, JSON, CBOR, image sniffing (P5 T17).

pub const HEX_BYTES_PER_LINE: usize = 16;
/// Larger JSON is shown as text only, to keep frames fast.
pub const MAX_PRETTY_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ViewerTab {
    Text,
    Json,
    Hex,
    Cbor,
    Image,
}

impl ViewerTab {
    pub fn label(self) -> &'static str {
        match self {
            ViewerTab::Text => "Text",
            ViewerTab::Json => "JSON",
            ViewerTab::Hex => "Hex",
            ViewerTab::Cbor => "CBOR",
            ViewerTab::Image => "Image",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImageKind {
    Png,
    Jpeg,
}

impl ImageKind {
    pub fn extension(self) -> &'static str {
        match self {
            ImageKind::Png => "png",
            ImageKind::Jpeg => "jpg",
        }
    }
}

pub fn hex_line_count(len: usize) -> usize {
    len.div_ceil(HEX_BYTES_PER_LINE)
}

/// `offset  xx xx … xx  xx … xx  |ascii|`, 16 bytes per line; empty past the end.
pub fn hex_line(bytes: &[u8], line: usize) -> String {
    let start = line * HEX_BYTES_PER_LINE;
    if start >= bytes.len() {
        return String::new();
    }
    let chunk = &bytes[start..(start + HEX_BYTES_PER_LINE).min(bytes.len())];
    let mut hex = String::with_capacity(48);
    for (i, b) in chunk.iter().enumerate() {
        if i > 0 {
            hex.push(' ');
        }
        if i == 8 {
            hex.push(' ');
        }
        hex.push_str(&format!("{b:02x}"));
    }
    let ascii: String = chunk.iter().map(|&b| if (0x20..=0x7e).contains(&b) { b as char } else { '.' }).collect();
    format!("{start:08x}  {hex:<48}  |{ascii}|")
}

pub fn text_view(bytes: &[u8]) -> Option<&str> {
    std::str::from_utf8(bytes).ok()
}

pub fn pretty_json(bytes: &[u8]) -> Option<String> {
    if bytes.len() > MAX_PRETTY_BYTES {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(bytes).ok()?;
    serde_json::to_string_pretty(&v).ok()
}

fn cbor_value_to_json(v: ciborium::Value) -> serde_json::Value {
    use ciborium::Value as C;
    use serde_json::Value as J;
    match v {
        C::Integer(i) => {
            let n: i128 = i.into();
            if let Ok(x) = i64::try_from(n) {
                J::from(x)
            } else if let Ok(x) = u64::try_from(n) {
                J::from(x)
            } else {
                J::String(n.to_string())
            }
        }
        C::Bytes(b) => J::String(format!("h'{}'", b.iter().map(|x| format!("{x:02x}")).collect::<String>())),
        C::Float(f) => serde_json::Number::from_f64(f).map_or(J::Null, J::Number),
        C::Text(s) => J::String(s),
        C::Bool(b) => J::Bool(b),
        C::Null => J::Null,
        C::Tag(tag, inner) => serde_json::json!({ "tag": tag, "value": cbor_value_to_json(*inner) }),
        C::Array(a) => J::Array(a.into_iter().map(cbor_value_to_json).collect()),
        C::Map(m) => J::Object(
            m.into_iter()
                .map(|(k, v)| {
                    let key = match k {
                        C::Text(s) => s,
                        other => match cbor_value_to_json(other) {
                            J::String(s) => s,
                            j => j.to_string(),
                        },
                    };
                    (key, cbor_value_to_json(v))
                })
                .collect(),
        ),
        _ => J::Null, // ciborium::Value is #[non_exhaustive]
    }
}

/// Decode one CBOR item into pretty JSON (byte strings as `h'…'`, tags as `{tag, value}`).
pub fn cbor_to_json(bytes: &[u8]) -> Result<String, String> {
    let v: ciborium::Value = ciborium::from_reader(bytes).map_err(|e| format!("not CBOR: {e:?}"))?;
    serde_json::to_string_pretty(&cbor_value_to_json(v)).map_err(|e| e.to_string())
}

/// PNG or JPEG, decided by magic bytes (the encoding alone is not trusted).
pub fn image_kind(_encoding: &str, bytes: &[u8]) -> Option<ImageKind> {
    if bytes.starts_with(&[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]) {
        Some(ImageKind::Png)
    } else if bytes.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some(ImageKind::Jpeg)
    } else {
        None
    }
}

/// Tabs that make sense for this payload, most specific first; Hex is always last.
pub fn available_tabs(encoding: &str, bytes: &[u8]) -> Vec<ViewerTab> {
    let mut tabs = Vec::new();
    let is_text = text_view(bytes).is_some();
    if is_text {
        tabs.push(ViewerTab::Text);
    }
    if is_text && !bytes.is_empty() && pretty_json(bytes).is_some() {
        tabs.push(ViewerTab::Json);
    }
    let cbor_declared = encoding.to_ascii_lowercase().contains("cbor");
    let image = image_kind(encoding, bytes).is_some();
    // Undeclared binary is offered as CBOR only if it parses and is not an
    // image (PNG's 0x89 header byte is a valid CBOR array head).
    if !bytes.is_empty() && (cbor_declared || (!is_text && !image && cbor_to_json(bytes).is_ok())) {
        tabs.push(ViewerTab::Cbor);
    }
    if image {
        tabs.push(ViewerTab::Image);
    }
    tabs.push(ViewerTab::Hex);
    tabs
}
```

- [ ] **Step 4: Run the tests.** Run `cargo test decode::`. Expected: 6 passed.
  - In `available_tabs_by_content`, the CBOR sample is a byte string of four `0xff` bytes behind a CBOR header. That is not valid UTF-8, so Text is excluded.
  - If `serde_json`'s pretty output differs in whitespace from the `json_and_text` literal, compare parsed values instead of strings.
- [ ] **Step 5: Commit.**

```bash
git add src/decode.rs
git commit -m "feat(viewer): hex/JSON/CBOR decoders and PNG/JPEG sniffing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T18: Payload viewer (Lane PAY)

**Owns:** `src/ui/payload_viewer.rs`

**Interfaces:**
- Consumes:
  - T17: `decode::*`.
  - T1: `SampleExtras`, `attachment::describe`, the `show_current_value` hook, `payload_store`.
- Produces:
  - `PayloadViewerState { tab, loaded, image_seq, last_image_uri }`.
  - `LoadedPayload { topic, bytes: Arc<[u8]>, total_len }`.
  - `MAX_VIEW_BYTES = 16 MiB`.
  - `show_current_value`, which replaces the moved block.
- egui APIs, verified in 0.36.2:
  - `ScrollArea::show_rows` for the hex view.
  - `egui::Image::from_bytes(uri, egui::load::Bytes)`, where `From<Arc<[u8]>>` is implemented, plus `.max_height`, and `Context::forget_image` (https://docs.rs/egui/0.36.2/egui/widgets/struct.Image.html#method.from_bytes). This needs the image loaders that T1 installs; the egui_extras `image` loader decodes with the `image` crate's png and jpeg features.
  - `Context::copy_text`.
- **Store access:** "Load full payload" reads the same plain entry that Save exports: `self.payload_store.read()` → `store.get(topic).map(|e| &e.bytes)` (`PayloadEntry.bytes`, P1).
  - P3 moved Save into `FileJobsUI` (`src/ui/file_jobs.rs`).
  - P4 moved file transfers to the `@xfer` registry, so they are not in `payload_store`, and the viewer does not show them.
  - If `PayloadEntry` changed shape, read bytes the way `FileJobsUI` does.

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/payload_viewer.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui_kittest::{kittest::Queryable, Harness};

    const PNG_MAGIC: [u8; 12] = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0x0D];

    fn harness(app: ZenohExplorer, topic: &'static str, preview: &'static str, enc: &'static str) -> Harness<'static, ZenohExplorer> {
        Harness::new_ui_state(
            move |ui, app: &mut ZenohExplorer| app.show_current_value(ui, topic, Some(preview.to_string()), Some(enc.to_string())),
            app,
        )
    }

    #[test]
    fn viewer_offers_json_and_hex_tabs() {
        let (app, _tx) = ZenohExplorer::test_app();
        let mut h = harness(app, "demo/j", r#"{"a":1}"#, "application/json");
        h.run();
        h.get_by_label("Text");
        h.get_by_label("JSON").click();
        h.run();
        h.get_by_label_contains("\"a\": 1");
        h.get_by_label("Hex").click();
        h.run();
        h.get_by_label_contains("00000000  7b 22 61 22");
        assert!(h.query_by_label("Image").is_none());
    }

    #[test]
    fn load_full_reads_store_bytes() {
        let (app, _tx) = ZenohExplorer::test_app();
        app.payload_store.write().unwrap().insert(
            "demo/big".into(),
            PayloadEntry { bytes: b"FULL-PAYLOAD-BYTES".to_vec(), received_at: chrono::Utc::now(), filename: None },
        );
        let mut h = harness(app, "demo/big", "FULL...", "text/plain");
        h.run();
        h.get_by_label_contains("preview");
        h.get_by_label("Load full payload").click();
        h.run();
        let loaded = h.state().payload_viewer.loaded.as_ref().expect("loaded");
        assert_eq!(&*loaded.bytes, b"FULL-PAYLOAD-BYTES");
        h.get_by_label_contains("18 bytes (full)");
    }

    #[test]
    fn image_tab_only_for_images() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.payload_viewer.loaded = Some(LoadedPayload {
            topic: "demo/png".into(),
            bytes: std::sync::Arc::from(&PNG_MAGIC[..]),
            total_len: PNG_MAGIC.len(),
        });
        let mut h = harness(app, "demo/png", "[binary 12 bytes]", "image/png");
        h.run();
        h.get_by_label("Image");
        assert!(h.query_by_label("Text").is_none(), "PNG magic is not UTF-8");
    }

    #[test]
    fn copy_puts_text_on_clipboard() {
        let (app, _tx) = ZenohExplorer::test_app();
        let mut h = harness(app, "demo/j", r#"{"a":1}"#, "application/json");
        h.run();
        h.get_by_label("JSON").click();
        h.run();
        h.get_by_label("Copy").click();
        h.step();
        let copied = h.output().platform_output.commands.iter().any(|c| {
            matches!(c, egui::OutputCommand::CopyText(t) if t.contains("\"a\": 1"))
        });
        assert!(copied, "Copy did not emit CopyText with the pretty JSON");
    }
}
```

  The `PayloadEntry` literal uses P1's fields. If P4 added fields, add them with the constructor P4 provides. If `copy_puts_text_on_clipboard` sees no command after one `step()`, the click is applied a frame later: replace `h.step()` with two `h.step()` calls. Do not use `run()`, whose last frame no longer carries the command.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::payload_viewer`. Expected: compile errors (`LoadedPayload` missing).
- [ ] **Step 3: Implement** (replace T1's moved block):

```rust
//! Payload viewer: Text / JSON / Hex / CBOR / Image tabs over the topic's
//! latest payload, with copy and "load full payload" (P5 T18).

use std::sync::Arc;

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::decode::{self, ViewerTab};
use crate::types::*;

/// Largest payload loaded into the viewer.
pub const MAX_VIEW_BYTES: usize = 16 * 1024 * 1024;
/// Largest text rendered as one label.
const MAX_TEXT_RENDER: usize = 256 * 1024;

#[derive(Debug, Clone)]
pub struct LoadedPayload {
    pub topic: String,
    pub bytes: Arc<[u8]>,
    /// Size in the store; larger than `bytes.len()` when capped.
    pub total_len: usize,
}

#[derive(Debug, Default)]
pub struct PayloadViewerState {
    pub tab: Option<ViewerTab>,
    pub loaded: Option<LoadedPayload>,
    image_seq: u64,
    last_image_uri: Option<String>,
}

pub trait PayloadViewerUI {
    fn show_current_value(&mut self, ui: &mut egui::Ui, topic: &str, payload: Option<String>, encoding: Option<String>);
}

fn tab_text(tab: ViewerTab, bytes: &[u8]) -> String {
    match tab {
        ViewerTab::Text => decode::text_view(bytes).unwrap_or_default().to_string(),
        ViewerTab::Json => decode::pretty_json(bytes).unwrap_or_default(),
        ViewerTab::Cbor => decode::cbor_to_json(bytes).unwrap_or_else(|e| e),
        ViewerTab::Hex => (0..decode::hex_line_count(bytes.len().min(MAX_TEXT_RENDER)))
            .map(|i| decode::hex_line(bytes, i))
            .collect::<Vec<_>>()
            .join("\n"),
        ViewerTab::Image => String::new(),
    }
}

impl PayloadViewerUI for ZenohExplorer {
    fn show_current_value(&mut self, ui: &mut egui::Ui, topic: &str, payload: Option<String>, encoding: Option<String>) {
        if self.payload_viewer.loaded.as_ref().is_some_and(|l| l.topic != topic) {
            self.payload_viewer.loaded = None;
            self.payload_viewer.tab = None;
        }
        let Some(preview) = payload else { return };
        let encoding = encoding.unwrap_or_default();
        let (bytes, full): (Arc<[u8]>, bool) = match &self.payload_viewer.loaded {
            Some(l) => (l.bytes.clone(), true),
            None => (Arc::from(preview.as_bytes()), false),
        };
        let stored_len = self.payload_store.read().ok().and_then(|s| s.get(topic).map(|e| e.bytes.len()));

        ui.separator();
        ui.horizontal(|ui| {
            ui.label(RichText::new("Current Value").strong());
            ui.label(format!("· {}", if encoding.is_empty() { "(no encoding)" } else { &encoding }));
            match &self.payload_viewer.loaded {
                Some(l) if l.total_len > l.bytes.len() => {
                    ui.label(format!("· first {} of {} bytes (full, capped)", l.bytes.len(), l.total_len));
                }
                Some(l) => {
                    ui.label(format!("· {} bytes (full)", l.bytes.len()));
                }
                None => {
                    ui.label(format!("· {} bytes shown (preview)", bytes.len()));
                    if ui.add_enabled(stored_len.is_some(), egui::Button::new("Load full payload")).clicked() {
                        let store = self.payload_store.read().ok();
                        if let Some(e) = store.as_ref().and_then(|s| s.get(topic)) {
                            let n = e.bytes.len().min(MAX_VIEW_BYTES);
                            self.payload_viewer.loaded =
                                Some(LoadedPayload { topic: topic.to_string(), bytes: Arc::from(&e.bytes[..n]), total_len: e.bytes.len() });
                        }
                    }
                }
            }
        });

        let tabs = decode::available_tabs(&encoding, &bytes);
        let tab = self.payload_viewer.tab.filter(|t| tabs.contains(t)).unwrap_or(tabs[0]);
        ui.horizontal(|ui| {
            for t in &tabs {
                if ui.selectable_label(*t == tab, t.label()).clicked() {
                    self.payload_viewer.tab = Some(*t);
                }
            }
            if tab != ViewerTab::Image && ui.button("Copy").clicked() {
                ui.ctx().copy_text(tab_text(tab, &bytes));
            }
        });
        if !full && preview.contains("... [+") {
            ui.label("Showing the stored preview; use Load full payload for the exact bytes.");
        }

        match tab {
            ViewerTab::Hex => {
                let row_h = ui.text_style_height(&egui::TextStyle::Monospace);
                egui::ScrollArea::vertical().id_salt(("viewer_hex", topic)).max_height(400.0).show_rows(
                    ui,
                    row_h,
                    decode::hex_line_count(bytes.len()),
                    |ui, range| {
                        for i in range {
                            ui.label(RichText::new(decode::hex_line(&bytes, i)).monospace());
                        }
                    },
                );
            }
            ViewerTab::Image => match decode::image_kind(&encoding, &bytes) {
                Some(kind) => {
                    let uri = format!("bytes://payload/{}.{}", self.payload_viewer.image_seq, kind.extension());
                    if self.payload_viewer.last_image_uri.as_deref() != Some(uri.as_str()) {
                        if let Some(old) = self.payload_viewer.last_image_uri.replace(uri.clone()) {
                            ui.ctx().forget_image(&old);
                        }
                    }
                    ui.add(egui::Image::from_bytes(uri, egui::load::Bytes::from(bytes.clone())).max_height(400.0));
                    ui.label(format!("{} image, {} bytes", kind.extension().to_uppercase(), bytes.len()));
                }
                None => {
                    ui.label("⚠ not a PNG/JPEG image");
                }
            },
            other => {
                let mut text = tab_text(other, &bytes);
                if text.len() > MAX_TEXT_RENDER {
                    let cut = safe_truncate_index(&text, MAX_TEXT_RENDER);
                    text.truncate(cut);
                    text.push_str("\n… (truncated for display; Copy copies the same text)");
                }
                egui::ScrollArea::vertical().id_salt(("viewer_text", topic)).max_height(400.0).show(ui, |ui| {
                    ui.label(RichText::new(text).monospace());
                });
            }
        }

        let last = self.messages.iter().rev().take(2_000).find(|m| m.key == topic).and_then(|m| m.extras.clone());
        if let Some(x) = last {
            ui.separator();
            if let Some(a) = &x.attachment {
                ui.label(format!("Attachment: {}", crate::attachment::describe(a)));
            }
            ui.label(format!(
                "QoS: priority {}, congestion {}, express {}, reliability {:?}",
                x.priority.label(),
                x.congestion.label(),
                if x.express { "yes" } else { "no" },
                x.reliability
            ));
        }
    }
}
```

  Bump `self.payload_viewer.image_seq += 1;` wherever `loaded` is replaced: in the Load button branch and in the topic-change reset at the top. That way each loaded payload gets a fresh image URI.
- [ ] **Step 4: Run the tests.** Run `cargo test ui::payload_viewer`. Expected: 4 passed.
- [ ] **Step 5: Manual check.** Publish a PNG file from the Publish tab ("Import File", encoding `image/png`) to `demo/img`. Select `demo/img` and press Load full payload: the Image tab renders the picture. Publish `{"x":[1,2]}` with `application/json`: the JSON tab pretty-prints it, and Copy pastes the same text.
- [ ] **Step 6: Commit.**

```bash
git add src/ui/payload_viewer.rs
git commit -m "feat(ui): payload viewer with Text/JSON/Hex/CBOR/Image tabs, copy and full load

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T19: Key-expression filter mode (Lane TREE)

**Owns:** `src/filter.rs`, `src/ui/topic_tree.rs`

**Interfaces:**
- Consumes: `types::compute_visible_paths` (P1), `ZenohCommand::Subscribe` (P1 fields), `crate::ui::TREE_FILTER_ID` and `pending_subscribes` with its event wiring (T1), `ZenohEvent::{SubscriptionCreated, SubscriptionRemoved}` and `process_events` (P1, for the tests), P3 T10's `filter_text_key`, `TreeRowsKey` and `tree_expand_generation`, and the Subscribe button as P3 T11 left it (`add_enabled(can_subscribe, …)`, or its F-T7-12 fallback that is always enabled and refuses a click with `UiAlert::Warning(reason)`).
- Produces:
  - `enum FilterMode { Empty, Substring(String), KeyExpr(String), InvalidKeyExpr { error: String, fallback: String } }`.
  - `fn mode_of(filter: &str) -> FilterMode`.
  - `FilterMode::{indicator, hover, visible_paths(&ZenohNode) -> Option<HashSet<String>>}`.
  - `fn ke_visible_paths(root: &ZenohNode, ke: &keyexpr) -> HashSet<String>`.
- zenoh API:
  - `zenoh::key_expr::keyexpr::new(&str) -> ZResult<&keyexpr>` validates canonical form.
  - `keyexpr::intersects(&self, &keyexpr) -> bool` (https://docs.rs/zenoh/1.10.1/zenoh/key_expr/struct.keyexpr.html#method.intersects).
- **Rule:**
  - Key-expression mode is chosen when the filter contains `*`, which also covers `**` and `$*`.
  - In KE mode, a node is visible if its full key intersects the expression, or if it has a visible descendant. A matching branch does **not** reveal non-matching children. This differs from text mode, and it is the key-expression meaning.
  - An invalid KE falls back to a substring match on the text with `*`, `$` and outer `/` removed, and shows a ⚠ indicator.
  - **Enter submits (F-T19-4).** Enter in the Subscribe Key field (pre-P1 `topic_tree.rs:182`) subscribes, like the Subscribe button, and focus stays in Key. Enter in the filter subscribes only in KE mode (above).
  - **One subscription per key.** Both Enter triggers skip a key that is already in `self.subscriptions` (P1 T14's rule for the button) or in T1's `pending_subscribes` (sent, not yet answered). Every send from this panel, by Enter or by the button, inserts its key into `pending_subscribes`. T1's event arms remove it on `SubscriptionCreated` and clear the set on a Subscribe failure or `Disconnected`. Without this guard, a second Enter on `demo/*` declares a second worker subscriber. P1 T12's `SubscriptionCreated` arm then gives the existing row the new id instead of adding a row, and the first subscriber is orphaned: it keeps delivering samples, ✖ removes only the new id, and P1 T12's resubscribe list re-declares it on reconnect. A second Enter sent before `SubscriptionCreated` arrives would pass P1's check alone, which is why the pending set is needed.
  - **No success banner on send (F-T8-3, F-T8-4).** Sending is not an outcome. In KE mode the filter row shows the real state in words: `Enter subscribes`, then `subscribing…` while the key is pending, then `subscribed` once it is in `self.subscriptions`. A failure arrives as P1's `OperationFailed` banner. The Subscribe Key path shows its outcome in the Active list, as the button always has.
  - **Enter needs focus at frame start.** In egui 0.36.2, `lost_focus()` stays true one extra frame after a mid-frame focus move (`memory/mod.rs:860-873`; see T13). So each Enter test also requires that its field had focus before it was drawn. Otherwise Enter typed in the Subscribe Key field right after leaving a KE filter would subscribe to the filter's expression.

- [ ] **Step 0: Locate the code.** Run `grep -n 'compute_visible_paths\|TREE_FILTER_ID\|filter_cache_is_stale\|filter_text_key\|subscribe_key' src/ui/topic_tree.rs`. This finds the filter row (T1 gave it the id), the cache refresh (P1 T19 with P3 T10's `filter_lower`) and the Subscribe Key row. All edits below happen at those three spots.
- [ ] **Step 1: Write the failing tests.**
  - In `src/filter.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn tree() -> ZenohNode {
        let mut root = ZenohNode::new("root".into());
        for k in ["demo/a/temp", "demo/b/temp", "demo/b/hum", "other/temp"] {
            root.insert_path(k);
        }
        root
    }

    #[test]
    fn mode_detection() {
        assert_eq!(mode_of("  "), FilterMode::Empty);
        assert_eq!(mode_of("Temp"), FilterMode::Substring("temp".into()));
        assert_eq!(mode_of("demo/*"), FilterMode::KeyExpr("demo/*".into()));
        assert_eq!(mode_of("a$*b"), FilterMode::KeyExpr("a$*b".into()));
        assert!(matches!(mode_of("demo/**/"), FilterMode::InvalidKeyExpr { .. }));
    }

    #[test]
    fn ke_mode_matches_by_intersection() {
        let v = mode_of("demo/*/temp").visible_paths(&tree()).unwrap();
        for p in ["demo", "demo/a", "demo/b", "demo/a/temp", "demo/b/temp"] {
            assert!(v.contains(p), "{p}");
        }
        assert!(!v.contains("demo/b/hum") && !v.contains("other/temp") && !v.contains("other"));
        let all_demo = mode_of("demo/**").visible_paths(&tree()).unwrap();
        assert!(all_demo.contains("demo/b/hum") && !all_demo.contains("other"));
    }

    #[test]
    fn invalid_ke_falls_back_to_substring() {
        let m = mode_of("demo/**/");
        let FilterMode::InvalidKeyExpr { fallback, .. } = &m else { panic!("{m:?}") };
        assert_eq!(fallback, "demo");
        let v = m.visible_paths(&tree()).unwrap();
        assert!(v.contains("demo/a/temp") && !v.is_empty());
        assert!(m.indicator().starts_with('⚠'));
    }
}
```

  - In `src/ui/topic_tree.rs` (add to, or create, `mod ui_tests`):

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use egui::accesskit::Role;
    use egui_kittest::{kittest::Queryable, Harness};

    /// How many `Subscribe` commands for `key` were sent since the last call.
    fn subscribes(cmds: &std::sync::mpsc::Receiver<ZenohCommand>, key: &str) -> usize {
        cmds.try_iter()
            .filter(|c| matches!(c, ZenohCommand::Subscribe { key_expr, .. } if key_expr == key))
            .count()
    }

    #[test]
    fn enter_in_ke_mode_subscribes() {
        let (mut app, tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        let input = h.get_by_role(Role::TextInput);
        input.focus();
        input.type_text("demo/*");
        h.run();
        h.get_by_label("KE key expr");
        h.key_press(egui::Key::Enter);
        h.run();
        assert_eq!(subscribes(&cmds, "demo/*"), 1, "Enter in KE mode subscribes (F-T19-4)");
        // Neutral words until the worker answers; no success banner on send (F-T8-3).
        h.get_by_label("subscribing…");
        assert!(h.state().ui_alert.is_none());
        h.key_press(egui::Key::Enter); // focus stayed in the filter
        h.run();
        assert_eq!(subscribes(&cmds, "demo/*"), 0, "a second Enter before SubscriptionCreated sends nothing");
        tx.send(ZenohEvent::SubscriptionCreated { id: "s1".into(), key_expr: "demo/*".into() }).unwrap();
        h.state_mut().process_events();
        h.run();
        h.get_by_label("subscribed");
        h.key_press(egui::Key::Enter);
        h.run();
        assert_eq!(subscribes(&cmds, "demo/*"), 0, "already subscribed: Enter sends nothing, as the button does");
        tx.send(ZenohEvent::SubscriptionRemoved { id: "s1".into() }).unwrap();
        h.state_mut().process_events();
        h.key_press(egui::Key::Enter);
        h.run();
        assert_eq!(subscribes(&cmds, "demo/*"), 1, "once the row is removed, Enter subscribes again");
    }

    #[test]
    fn enter_in_subscribe_key_subscribes() {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        app.subscribe_key = "demo/**".into();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.get_by_label("Subscribe to Topics").click();
        h.run();
        h.get_by_label("Key:").focus();
        h.run();
        h.key_press(egui::Key::Enter);
        h.run();
        assert_eq!(subscribes(&cmds, "demo/**"), 1, "Enter in Subscribe Key subscribes (F-T19-4)");
        h.run();
        assert!(h.get_by_label("Key:").is_focused(), "focus stays in Key after Enter");
        h.key_press(egui::Key::Enter);
        h.run();
        assert_eq!(subscribes(&cmds, "demo/**"), 0, "a second Enter before SubscriptionCreated sends nothing");
    }

    #[test]
    fn enter_after_leaving_filter_does_not_subscribe() {
        // egui 0.36.2 keeps the filter's `lost_focus()` true for one more frame after focus
        // moves mid-frame, so Enter typed in Key on the very next frame must not subscribe
        // to the filter's expression.
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Connected;
        app.tree_filter = "demo/*".into(); // KE mode; subscribe_key stays empty
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.get_by_label("Subscribe to Topics").click();
        h.run();
        h.get_by_role_and_label(Role::TextInput, "Filter topics").focus();
        h.run();
        h.get_by_label("Key:").focus();
        h.step(); // one frame: focus moves from the filter to Key while it is drawn
        h.key_press(egui::Key::Enter);
        h.step(); // the next frame is the Enter press, typed in the empty Key field
        assert!(
            !cmds.try_iter().any(|c| matches!(c, ZenohCommand::Subscribe { .. })),
            "Enter in Key must not subscribe to the filter's demo/*"
        );
    }
}
```

  The "Subscribe to Topics" section is collapsed by default, so in `enter_in_ke_mode_subscribes` the filter is the only text input. `enter_in_subscribe_key_subscribes` opens the section first and finds the Key field by the label P3 T11 gave it (`labelled_by`). `enter_after_leaving_filter_does_not_subscribe` finds the filter by P3 T11's accessible name "Filter topics". Its Key field is empty, so Enter there sends nothing through either P3 variant of the button, and any `Subscribe` would have come from the filter. `h.step()` runs one frame per queued event, as in T13's `enter_after_leaving_key_does_not_publish`.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- filter:: ui::topic_tree`. Expected: compile errors.
- [ ] **Step 3: Implement `src/filter.rs`** (replace the stub):

```rust
//! Topic-tree filter modes (P5 T19): case-insensitive substring, or a key
//! expression (`*`, `**`, `$*`) matched with `keyexpr::intersects`.

use std::collections::HashSet;

use zenoh::key_expr::keyexpr;

use crate::types::ZenohNode;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FilterMode {
    Empty,
    /// Lowercased needle.
    Substring(String),
    /// A valid key expression.
    KeyExpr(String),
    /// Looked like a key expression but is invalid; substring-match `fallback` instead.
    InvalidKeyExpr { error: String, fallback: String },
}

pub fn mode_of(filter: &str) -> FilterMode {
    let f = filter.trim();
    if f.is_empty() {
        return FilterMode::Empty;
    }
    if !f.contains('*') {
        return FilterMode::Substring(f.to_lowercase());
    }
    match keyexpr::new(f) {
        Ok(_) => FilterMode::KeyExpr(f.to_string()),
        Err(e) => FilterMode::InvalidKeyExpr {
            error: e.to_string(),
            fallback: f.replace(['*', '$'], "").trim_matches('/').to_lowercase(),
        },
    }
}

impl FilterMode {
    /// Short mode indicator (text, never colour).
    pub fn indicator(&self) -> &'static str {
        match self {
            FilterMode::Empty => "",
            FilterMode::Substring(_) => "Aa text",
            FilterMode::KeyExpr(_) => "KE key expr",
            FilterMode::InvalidKeyExpr { .. } => "⚠ invalid KE, text match",
        }
    }

    pub fn hover(&self) -> String {
        match self {
            FilterMode::Empty | FilterMode::Substring(_) => {
                "Text mode: case-insensitive substring. Type * or ** for key-expression mode.".to_string()
            }
            FilterMode::KeyExpr(_) => "Key-expression mode (keyexpr::intersects). Press Enter to subscribe.".to_string(),
            FilterMode::InvalidKeyExpr { error, .. } => format!("Invalid key expression: {error}"),
        }
    }

    /// Visible node paths, or `None` when not filtering.
    pub fn visible_paths(&self, root: &ZenohNode) -> Option<HashSet<String>> {
        match self {
            FilterMode::Empty => None,
            FilterMode::Substring(s) | FilterMode::InvalidKeyExpr { fallback: s, .. } => {
                Some(crate::types::compute_visible_paths(root, s))
            }
            FilterMode::KeyExpr(ke) => keyexpr::new(ke).ok().map(|k| ke_visible_paths(root, k)),
        }
    }
}

/// Nodes whose full key intersects `ke`, plus their ancestors.
pub fn ke_visible_paths(root: &ZenohNode, ke: &keyexpr) -> HashSet<String> {
    fn walk(node: &ZenohNode, path: &str, ke: &keyexpr, out: &mut HashSet<String>) -> bool {
        let mut visible = keyexpr::new(path).is_ok_and(|k| ke.intersects(k));
        for (key, child) in &node.children {
            if walk(child, &format!("{path}/{key}"), ke, out) {
                visible = true;
            }
        }
        if visible {
            out.insert(path.to_string());
        }
        visible
    }
    let mut out = HashSet::new();
    for (key, child) in &root.children {
        walk(child, key, ke, &mut out);
    }
    out
}
```

- [ ] **Step 4: Wire it into `src/ui/topic_tree.rs`.**
  - **Filter row** (the `TextEdit` with `TREE_FILTER_ID`). Directly **before** the `TextEdit`, add:

```rust
                // Read before the filter is drawn: egui 0.36.2 keeps `lost_focus()` true one
                // extra frame after a mid-frame focus move (see the Rule).
                let filter_had_focus = ui.memory(|m| m.has_focus(egui::Id::new(crate::ui::TREE_FILTER_ID)));
```

    Keep the `TextEdit`'s response as `filter_resp`, and after it add:

```rust
                let mode = crate::filter::mode_of(&self.tree_filter);
                ui.label(mode.indicator()).on_hover_text(mode.hover());
                if let crate::filter::FilterMode::KeyExpr(ke) = &mode {
                    // The same duplicate rule as the Subscribe button (P1 T14), plus keys sent but not yet answered.
                    let subscribed = self.subscriptions.iter().any(|s| s.key_expr == *ke);
                    let pending = self.pending_subscribes.contains(ke);
                    // Real state in words, replaced by the outcome; no banner on send (F-T8-3, F-T8-4).
                    ui.label(if subscribed { "subscribed" } else if pending { "subscribing…" } else { "Enter subscribes" });
                    let enter = filter_had_focus && filter_resp.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    if enter && !subscribed && !pending && matches!(self.connection_status, ConnectionStatus::Connected) {
                        self.send_command(ZenohCommand::Subscribe {
                            key_expr: ke.clone(),
                            reliability: self.subscribe_reliability.clone(),
                            mode: self.subscribe_mode.clone(),
                        });
                        self.pending_subscribes.insert(ke.clone());
                    }
                    if enter {
                        filter_resp.request_focus(); // keep typing in the filter (F-T19-4)
                    }
                }
```

    Here `filter_resp` is the response of the filter `TextEdit` `ui.add(…)`. If P1 changed `Subscribe`'s fields, use exactly the fields the "Subscribe" button sends. Nothing here sets `ui_alert`.
  - **Subscribe Key row** (pre-P1 `topic_tree.rs:180-183`, inside `ui.collapsing("Subscribe to Topics", …)`). P3 T11 already turned it into `let key_label = ui.label("Key:"); ui.text_edit_singleline(&mut self.subscribe_key).labelled_by(key_label.id);`. Keep that. Directly **before** the row, add `let focused_before = ui.memory(|m| m.focused());`. The field has no fixed id, so this compares ids after drawing instead of asking `has_focus` first. Return the `TextEdit` response from the row's `ui.horizontal` closure as `key_resp` (`let key_resp = ui.horizontal(|ui| { … }).inner;`). Then keep the button exactly as P3 T11 left it and OR the Enter trigger into its existing click branch, with P1's enable condition and `Subscribe` fields exactly as the button uses them. If P3 T11 kept `add_enabled`, the code is:

```rust
                    // Enter in Key subscribes like the button (F-T19-4), only if Key had focus as the frame began.
                    let key_enter = focused_before == Some(key_resp.id)
                        && key_resp.lost_focus()
                        && ui.input(|i| i.key_pressed(egui::Key::Enter));
                    // Sent but not yet answered: a second Enter or click before SubscriptionCreated sends nothing.
                    let pending = self.pending_subscribes.contains(self.subscribe_key.trim());
                    let clicked = ui.add_enabled(can_subscribe, egui::Button::new("Subscribe")).clicked();
                    if (clicked || (key_enter && can_subscribe)) && !pending {
                        // P1's existing send of ZenohCommand::Subscribe { … }, unchanged, then:
                        // self.pending_subscribes.insert(<the key_expr it just sent>);
                    }
                    if key_enter {
                        key_resp.request_focus(); // also when nothing was sent, so Tab does not restart at the header
                    }
```

    `can_subscribe` is P1's existing enable expression for the button (connected, a valid key, not already subscribed: `self.subscriptions.iter().any(|s| s.key_expr == self.subscribe_key.trim())`), bound to a name so both triggers share it.

    If P3 T11 took its F-T7-12 fallback instead (its commit body records the choice, and Subscribe is drawn with `ui.button("Subscribe")`), Subscribe is always enabled, and a click with a block reason raises `UiAlert::Warning(reason)`. Do not bring back `add_enabled(can_subscribe, …)`: that would undo P3's F-T7-12 fix and break its `tab_passes_disabled_subscribe`. Keep P3's button and refusal, and route Enter into the same branch: `if (ui.button("Subscribe").clicked() || key_enter) && !pending { if let Some(reason) = subscribe_block_reason { /* P3's warning, unchanged */ } else { /* send as today, then insert into pending_subscribes */ } }`. Only reason about `can_subscribe` in the button line when P3 kept `add_enabled`.
  - **Cache refresh.** Work against P3 T10's code, not the pre-P1 line. After P3 there is a single `let filter_lower = crate::ui::tree_rows::filter_text_key(&self.tree_filter);` (trimmed and lower-cased). It feeds P1 T19's filter cache, `TreeRowsKey.filter` and the `row_state_id` open-state ids. Keep that line, and keep it feeding `TreeRowsKey` and `row_state_id`: `show_tree_row` also calls `filter_text_key`, so the flatten step and the rows must agree on those ids, or collapsing a branch under a mixed-case filter stops hiding its children.
    - Only P1 T19's filter-cache block changes. It moves to a case-kept key, because KE mode is case-sensitive. Its visible set comes from `filter::mode_of`, and a text change bumps P3's `tree_expand_generation`. That block already runs before P3's rows block, which reads its visible set, so the bump reaches this frame's `TreeRowsKey`:

```rust
            // Case kept: `Demo/*` and `demo/*` are different key expressions. The name differs from
            // P3's `filter_lower` and from T20's `filter_key`, which are the lower-cased row key.
            let filter_exact = self.tree_filter.trim().to_string();
            if !filter_exact.is_empty() {
                let now = Instant::now();
                let cached = self.tree_filter_cache.as_ref().map(|(q, v, at, _)| (q.as_str(), *v, *at));
                if filter_cache_is_stale(cached, &filter_exact, self.tree_version, now) {
                    let text_changed = cached.is_none_or(|(q, _, _)| q != filter_exact);
                    let visible = crate::filter::mode_of(&filter_exact).visible_paths(tree).unwrap_or_default();
                    self.tree_filter_cache = Some((filter_exact.clone(), self.tree_version, now, visible));
                    if text_changed {
                        // P3's row cache is keyed by the lower-cased text, so a case-only edit changes this
                        // visible set without changing TreeRowsKey.filter. Force the row rebuild.
                        self.tree_expand_generation = self.tree_expand_generation.wrapping_add(1);
                    }
                }
            } else {
                self.tree_filter_cache = None;
            }
```

    - This replaces P1 T19's staleness check and `compute_visible_paths(tree, &filter_lower)` call, and whatever emptiness test P1 or P3 put around them. Keep P1 T19's `filter_cache_is_stale` throttle and the four-tuple shape. `src/ui/tree_rows.rs` is not touched.
- [ ] **Step 5: Run the tests.** Run `cargo test -- filter:: ui::topic_tree`. Expected: 6 passed (3 in `filter::tests`, 3 in `ui::topic_tree::ui_tests`), plus P1's existing tests in `ui::topic_tree::tests`.
- [ ] **Step 6: Commit.**

```bash
git add src/filter.rs src/ui/topic_tree.rs
git commit -m "feat(tree): key-expression filter mode with intersects, indicator, and Enter to subscribe from the filter and Key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T20: Keyboard tree navigation (Lane TREE)

**Owns:** `src/tree_nav.rs`, `src/ui/topic_tree.rs`

**Interfaces:**
- Consumes:
  - T1: `TreeNavState`.
  - P3 T9/T10: `crate::ui::tree_rows::{TreeRow { full_path, depth, is_branch }, flatten_rows, row_state_id(full_path: &str, filter: &str), filter_text_key}` (the filter text is `""` when not filtering), the app fields `tree_rows_cache: Option<TreeRowsCache>` and `tree_expand_generation: u64`, and `show_rows` rendering with `row_height = ui.spacing().interact_size.y`.
  - P1 T19: the filter cache four-tuple, whose visible set is element `.3`.
- Produces:
  - `enum NavKey { Up, Down, Left, Right }`.
  - `struct NavOutcome { select, open, close }`.
  - `fn navigate(rows: &[TreeRow], current: Option<&str>, key: NavKey, is_open: &dyn Fn(&str) -> bool) -> NavOutcome`.
  - `fn scroll_offset_for(index: usize, row_pitch: f32, viewport: f32) -> f32`.
  - `fn ancestors(path: &str) -> Vec<String>` (`"a/b/c"` → `["a", "a/b"]`).
  - `TreeNavState` gains `was_filtering: bool`, the filter state seen on the previous frame.
  - It reuses P3's flattening rather than duplicating it.
- **Rule (F-T13-3):** when the filter goes from non-empty to empty and a topic is selected, every ancestor of the selection is opened in the unfiltered tree and the selection is scrolled into view on that frame. Clearing the filter no longer buries the selected row under a collapsed branch.
- egui APIs, verified in 0.36.2:
  - `InputState::consume_key` and `Context::egui_wants_keyboard_input`.
  - `CollapsingState::{load_with_default_open, set_open, store, is_open}`.
  - `ScrollArea::vertical_scroll_offset`. Rows are virtualised, so an off-screen row is never drawn and `scroll_to_me` cannot reach it.
  - `Memory::move_focus(FocusDirection::None)`. egui 0.36's `Memory::begin_pass` turns plain arrow keys into focus movement when a widget has focus (`memory/mod.rs:591`), so the tree cancels that movement after handling an arrow.

- [ ] **Step 0: Locate the code.** Run `grep -n 'tree_rows_cache\|tree_expand_generation\|row_state_id\|show_rows' src/ui/topic_tree.rs`. Expected: P3 T10's cache refresh and `ScrollArea::show_rows` block. Steps 4a and 4b edit those two places. If P3 renamed any of these items, use P3's names.
- [ ] **Step 1: Write the failing tests.**
  - In `src/tree_nav.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::ZenohNode;
    use crate::ui::tree_rows::flatten_rows;

    fn rows(open: &dyn Fn(&str) -> bool) -> Vec<TreeRow> {
        let mut root = ZenohNode::new("root".into());
        for k in ["a/x", "a/y", "b"] {
            root.insert_path(k);
        }
        flatten_rows(&root, None, open)
    }

    #[test]
    fn down_up_walk_visible_rows() {
        let open = |_: &str| true;
        let r = rows(&open);
        assert_eq!(navigate(&r, None, NavKey::Down, &open).select.as_deref(), Some("a"));
        assert_eq!(navigate(&r, Some("a"), NavKey::Down, &open).select.as_deref(), Some("a/x"));
        assert_eq!(navigate(&r, Some("a/x"), NavKey::Up, &open).select.as_deref(), Some("a"));
        assert_eq!(navigate(&r, Some("b"), NavKey::Down, &open), NavOutcome::default());
        assert_eq!(navigate(&r, Some("gone/away"), NavKey::Up, &open).select.as_deref(), Some("a"));
    }

    #[test]
    fn right_expands_then_enters() {
        let closed = |_: &str| false;
        assert_eq!(navigate(&rows(&closed), Some("a"), NavKey::Right, &closed).open.as_deref(), Some("a"));
        let open = |_: &str| true;
        assert_eq!(navigate(&rows(&open), Some("a"), NavKey::Right, &open).select.as_deref(), Some("a/x"));
        assert_eq!(navigate(&rows(&open), Some("b"), NavKey::Right, &open), NavOutcome::default());
    }

    #[test]
    fn left_goes_to_parent_when_closed() {
        let open = |_: &str| true;
        let r = rows(&open);
        assert_eq!(navigate(&r, Some("a"), NavKey::Left, &open).close.as_deref(), Some("a"));
        assert_eq!(navigate(&r, Some("a/y"), NavKey::Left, &open).select.as_deref(), Some("a"));
        assert_eq!(navigate(&r, Some("b"), NavKey::Left, &open), NavOutcome::default());
    }

    #[test]
    fn scroll_offset_centres_and_clamps() {
        assert_eq!(scroll_offset_for(0, 20.0, 400.0), 0.0);
        assert_eq!(scroll_offset_for(100, 20.0, 400.0), 100.0 * 20.0 - 200.0 + 10.0);
    }

    #[test]
    fn ancestors_of_nested_path() {
        assert_eq!(ancestors("a/b/c"), vec!["a".to_string(), "a/b".to_string()]);
        assert!(ancestors("a").is_empty());
    }
}
```

  - In `src/ui/topic_tree.rs` `mod ui_tests`:

```rust
    #[test]
    fn arrow_keys_move_selection() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        {
            let mut t = app.browse_tree.write().unwrap();
            t.insert_path("k/one");
            t.insert_path("k/two");
        }
        app.tree_version += 1;
        app.selected_topic = Some("k".into());
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.key_press(egui::Key::ArrowRight); // expand k
        h.run();
        h.key_press(egui::Key::ArrowDown);
        h.run();
        assert_eq!(h.state().selected_topic.as_deref(), Some("k/one"));
        h.key_press(egui::Key::ArrowDown);
        h.run();
        assert_eq!(h.state().selected_topic.as_deref(), Some("k/two"));
        h.key_press(egui::Key::ArrowLeft);
        h.run();
        assert_eq!(h.state().selected_topic.as_deref(), Some("k"));
    }

    #[test]
    fn clearing_filter_reveals_selection() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        {
            let mut t = app.browse_tree.write().unwrap();
            t.insert_path("k/deep/leaf");
            t.insert_path("other/x");
        }
        app.tree_version += 1;
        app.tree_filter = "leaf".into();
        app.selected_topic = Some("k/deep/leaf".into());
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.state_mut().tree_filter.clear();
        h.run();
        h.run();
        let open = |p: &str| {
            egui::collapsing_header::CollapsingState::load(&h.ctx, row_state_id(p, "")).is_some_and(|s| s.is_open())
        };
        assert!(open("k") && open("k/deep"), "every ancestor of the selection is open after the filter clears (F-T13-3)");
        assert!(!open("other"), "unrelated branches stay closed");
        assert_eq!(h.state().selected_topic.as_deref(), Some("k/deep/leaf"));
        // Rows are virtualised, so a row that is drawn is a row the user can see.
        h.get_by_label_contains("leaf");
    }
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- tree_nav:: ui::topic_tree`. Expected: compile errors.
- [ ] **Step 3: Implement `src/tree_nav.rs`** (keep T1's `TreeNavState` at the top):

```rust
pub use crate::ui::tree_rows::TreeRow;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NavKey {
    Up,
    Down,
    Left,
    Right,
}

/// What a key press should do; all `None` means nothing.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct NavOutcome {
    pub select: Option<String>,
    pub open: Option<String>,
    pub close: Option<String>,
}

/// Explorer-style navigation over P3's flattened rows (display order).
/// With no current row (or a row that vanished), any key selects the first row.
pub fn navigate(rows: &[TreeRow], current: Option<&str>, key: NavKey, is_open: &dyn Fn(&str) -> bool) -> NavOutcome {
    let select = |r: Option<&TreeRow>| NavOutcome { select: r.map(|r| r.full_path.clone()), ..Default::default() };
    let Some(i) = current.and_then(|c| rows.iter().position(|r| r.full_path == c)) else {
        return select(rows.first());
    };
    let row = &rows[i];
    match key {
        NavKey::Down => select(rows.get(i + 1)),
        NavKey::Up => select(i.checked_sub(1).and_then(|j| rows.get(j))),
        NavKey::Right if row.is_branch && !is_open(&row.full_path) => {
            NavOutcome { open: Some(row.full_path.clone()), ..Default::default() }
        }
        NavKey::Right if row.is_branch => select(rows.get(i + 1).filter(|n| n.depth == row.depth + 1)),
        NavKey::Right => NavOutcome::default(),
        NavKey::Left if row.is_branch && is_open(&row.full_path) => {
            NavOutcome { close: Some(row.full_path.clone()), ..Default::default() }
        }
        NavKey::Left => NavOutcome {
            select: row.full_path.rsplit_once('/').map(|(parent, _)| parent.to_string()),
            ..Default::default()
        },
    }
}

/// Scroll offset that puts row `index` (rows `row_pitch` apart) mid-viewport.
pub fn scroll_offset_for(index: usize, row_pitch: f32, viewport: f32) -> f32 {
    (index as f32 * row_pitch - viewport / 2.0 + row_pitch / 2.0).max(0.0)
}

/// Every proper ancestor path of `path`, outermost first.
pub fn ancestors(path: &str) -> Vec<String> {
    path.match_indices('/').map(|(i, _)| path[..i].to_string()).collect()
}
```

  Add the field to T1's `TreeNavState`:

```rust
    /// Whether the tree filter was non-empty on the previous frame (F-T13-3).
    pub was_filtering: bool,
```

- [ ] **Step 4: Wire it into `src/ui/topic_tree.rs::show_tree_panel`.**
  - **4a.** Directly before P3 T10's `if rows_cache_is_stale(…)` block, handle the keys. Opening or closing bumps `tree_expand_generation`, so the row cache rebuilds in the same frame:

```rust
            let ctx = ui.ctx().clone();
            let nav_key = if ctx.egui_wants_keyboard_input() {
                None
            } else {
                ctx.input_mut(|i| {
                    use egui::{Key, Modifiers};
                    [(Key::ArrowUp, NavKey::Up), (Key::ArrowDown, NavKey::Down), (Key::ArrowLeft, NavKey::Left), (Key::ArrowRight, NavKey::Right)]
                        .into_iter()
                        .find_map(|(k, n)| i.consume_key(Modifiers::NONE, k).then_some(n))
                })
            };
            if let Some(key) = nav_key {
                ctx.memory_mut(|m| m.move_focus(egui::FocusDirection::None));
                let filtering = self.tree_filter_cache.is_some();
                // Same open-state key as P3 T10's flatten step.
                let filter_key = crate::ui::tree_rows::filter_text_key(&self.tree_filter);
                let state_filter = if filtering { filter_key.as_str() } else { "" };
                let is_open = |p: &str| {
                    egui::collapsing_header::CollapsingState::load_with_default_open(&ctx, row_state_id(p, state_filter), filtering).is_open()
                };
                let visible = self.tree_filter_cache.as_ref().map(|c| &c.3);
                let rows = flatten_rows(tree, visible, &is_open);
                let out = crate::tree_nav::navigate(&rows, self.selected_topic.as_deref(), key, &is_open);
                for (path, open) in [(out.open, true), (out.close, false)] {
                    if let Some(p) = path {
                        let mut s = egui::collapsing_header::CollapsingState::load_with_default_open(&ctx, row_state_id(&p, state_filter), filtering);
                        s.set_open(open);
                        s.store(&ctx);
                        self.tree_expand_generation = self.tree_expand_generation.wrapping_add(1);
                    }
                }
                if let Some(p) = out.select {
                    self.selected_topic = Some(p);
                    self.detail_view = DetailView::TopicDetails;
                    self.tree_nav.scroll_to_selected = true;
                }
            }
```

  - **4a′. Reveal the selection when the filter clears (F-T13-3).** Directly after 4a, still before `rows_cache_is_stale(…)`:

```rust
            let filtering_now = !self.tree_filter.trim().is_empty();
            if self.tree_nav.was_filtering && !filtering_now {
                if let Some(sel) = self.selected_topic.clone() {
                    for p in crate::tree_nav::ancestors(&sel) {
                        let mut s = egui::collapsing_header::CollapsingState::load_with_default_open(&ctx, row_state_id(&p, ""), false);
                        s.set_open(true);
                        s.store(&ctx);
                    }
                    self.tree_expand_generation = self.tree_expand_generation.wrapping_add(1);
                    self.tree_nav.scroll_to_selected = true;
                }
            }
            self.tree_nav.was_filtering = filtering_now;
```

    `row_state_id(p, "")` is the unfiltered state id (P3 T9 keys collapse state by filter text), so this opens the branches the user sees once the filter is gone and leaves their filtered state alone. Bumping `tree_expand_generation` rebuilds the row cache in the same frame, so 4b finds the selected row and scrolls to it.

    Add `use crate::tree_nav::NavKey;`.
  - **4b.** In P3 T10's `egui::ScrollArea::vertical().auto_shrink([false; 2]).show_rows(ui, row_height, rows.len(), …)`, bind the builder first and apply the offset once:

```rust
            let mut scroll = egui::ScrollArea::vertical().auto_shrink([false; 2]);
            if self.tree_nav.scroll_to_selected {
                if let Some(idx) = rows.iter().position(|r| Some(&r.full_path) == self.selected_topic.as_ref()) {
                    let pitch = row_height + ui.spacing().item_spacing.y;
                    scroll = scroll.vertical_scroll_offset(crate::tree_nav::scroll_offset_for(idx, pitch, ui.available_height()));
                }
                self.tree_nav.scroll_to_selected = false;
            }
            scroll.show_rows(ui, row_height, rows.len(), |ui, range| {
                // P3 T10's row loop, unchanged
            });
```

- [ ] **Step 5: Run the tests.** Run `cargo test -- tree_nav:: ui::topic_tree ui::tests::tree`. Expected: all pass. That is T20's 5 unit tests in `tree_nav::tests` and its 2 kittests (`arrow_keys_move_selection`, `clearing_filter_reveals_selection`), T19's 3 kittests and P1's tests, both in `ui::topic_tree`, and P3's 9 tests in `ui::tests::tree`.
  - If `arrow_keys_move_selection` moves two rows per press, egui's focus traversal was not cancelled. Check that `move_focus(FocusDirection::None)` runs before any row is drawn in that pass.
- [ ] **Step 6: Commit.**

```bash
git add src/tree_nav.rs src/ui/topic_tree.rs
git commit -m "feat(tree): arrow-key navigation, scroll-to-selected, and reveal the selection when the filter clears

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T21: Per-topic rates, last-message ages and recent activity (Lane TREE)

**Owns:** `src/rates.rs`, `src/types/tree.rs`, `src/ui/topic_tree.rs`

**Interfaces:**
- Produces:
  - `struct RateMeter`, with `new(now: Instant)`, `record(now)`, `rate(now) -> f32` and `#[cfg(test)] fixed(now, rate)`.
  - `RECENT_WINDOW = 1.5 s`.
  - `fn is_recent(last_seen: Instant, now: Instant) -> bool`.
  - `fn format_rate(r: f32) -> String`.
  - `fn format_age(age: Duration) -> String`: "now" under 2 s, then "12 s ago", "4 min ago", "2 h ago".
  - The field `ZenohNode.rate: RateMeter`, updated in `update_data`.
- **Rule:**
  - The rate is messages per second over the last completed 1-second window.
  - After 2 s without messages, it reads 0.
  - A leaf updated within 1.5 s shows `↻` and bold text, a glyph and weight cue rather than colour.
  - **Row age (F-T20-4, first recommendation).** Every leaf with messages shows the age of its last message after the rate, in words from `format_age`: "now", "12 s ago", "4 min ago", "2 h ago". A topic quiet for 10 s and one quiet for an hour no longer look the same, even though the rate reads 0 after 2 s and `↻` lasts 1.5 s. The age is text only. The finding's "dim older rows" part is not placed here: tone is colour, which the Snow White plan owns. The row asks for a repaint when its words next change: every second under a minute, then at the next whole minute or hour.
  - While any row is recent or has a rate, the panel asks for a repaint in 500 ms, because P1 repaints on events only.
  - **Topic page recency (F-T20-4).** A selected leaf's page shows "Last message 13:21:07.412 (4 s ago) · 2.0/s received". The clock time comes from P1 T3's `format_local_time`, and the age and rate come from `last_seen` and `rate`. "No messages in the last 2 s" is decided from the age, not from a zero rate. A single message after a long gap has rate 0 but age "now", and must not be called silent. A topic whose publisher stopped an hour ago now reads "(1 h ago) · no messages in the last 2 s" instead of looking live. The page asks for a repaint in 1 s so the age keeps counting.

- [ ] **Step 1: Write the failing tests.**
  - In `src/rates.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn ms(t0: Instant, ms: u64) -> Instant {
        t0 + Duration::from_millis(ms)
    }

    #[test]
    fn rate_over_one_second_window() {
        let t0 = Instant::now();
        let mut m = RateMeter::new(t0);
        for i in 0..10 {
            m.record(ms(t0, i * 100));
        }
        assert!((m.rate(ms(t0, 1000)) - 10.0).abs() < 0.01);
        m.record(ms(t0, 1000)); // rolls the window
        assert!((m.rate(ms(t0, 1500)) - 10.0).abs() < 0.01);
    }

    #[test]
    fn rate_decays_to_zero() {
        let t0 = Instant::now();
        let mut m = RateMeter::new(t0);
        m.record(t0);
        assert_eq!(m.rate(ms(t0, 2100)), 0.0);
        m.record(ms(t0, 10_000)); // first message after a long gap: no stale rate
        assert_eq!(m.rate(ms(t0, 10_100)), 0.0);
    }

    #[test]
    fn formatting_and_recency() {
        assert_eq!(format_rate(0.0), "");
        assert_eq!(format_rate(0.5), "0.5/s");
        assert_eq!(format_rate(12.3), "12/s");
        let t0 = Instant::now();
        assert!(is_recent(t0, ms(t0, 1000)));
        assert!(!is_recent(t0, ms(t0, 1600)));
    }

    #[test]
    fn age_words() {
        assert_eq!(format_age(Duration::from_millis(1500)), "now");
        assert_eq!(format_age(Duration::from_secs(12)), "12 s ago");
        assert_eq!(format_age(Duration::from_secs(4 * 60 + 5)), "4 min ago");
        assert_eq!(format_age(Duration::from_secs(2 * 3600 + 10)), "2 h ago");
    }
}
```

  - In `src/ui/topic_tree.rs` `mod ui_tests`:

```rust
    #[test]
    fn recent_leaf_shows_glyph_and_rate() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        {
            let mut t = app.browse_tree.write().unwrap();
            let leaf = t.insert_path("solo"); // top-level leaf: rendered without expanding anything
            leaf.last_seen = std::time::Instant::now();
            leaf.message_count = 3;
            leaf.rate = crate::rates::RateMeter::fixed(std::time::Instant::now(), 12.0);
        }
        app.tree_version += 1;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.get_by_label_contains("↻");
        h.get_by_label_contains("solo");
        h.get_by_label("12/s");
        h.get_by_label("now"); // row age (F-T20-4)
    }

    #[test]
    fn quiet_leaf_row_shows_age() {
        // F-T20-4: a topic quiet for minutes must not look like one that just went quiet.
        let (mut app, _tx) = ZenohExplorer::test_app();
        {
            let mut t = app.browse_tree.write().unwrap();
            let leaf = t.insert_path("stale");
            leaf.message_count = 1;
            leaf.last_seen = std::time::Instant::now() - std::time::Duration::from_secs(5 * 60);
        }
        app.tree_version += 1;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_tree_panel(ui), app);
        h.run();
        h.get_by_label("5 min ago");
        assert!(h.query_by_label_contains("↻").is_none(), "a quiet leaf has no activity glyph");
    }

    #[test]
    fn topic_page_shows_last_message_age() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        {
            let mut t = app.browse_tree.write().unwrap();
            let leaf = t.insert_path("quiet");
            leaf.message_count = 1;
            leaf.last_seen = std::time::Instant::now() - std::time::Duration::from_secs(5 * 60);
        }
        app.tree_version += 1;
        app.selected_topic = Some("quiet".into());
        app.detail_view = DetailView::TopicDetails;
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_detail_panel(ui), app);
        h.run();
        h.get_by_label_contains("Last message");
        h.get_by_label_contains("5 min ago");
        h.get_by_label_contains("no messages in the last 2 s");
        // A first message after the gap: rate still 0, but the topic is not silent.
        h.state_mut().browse_tree.write().unwrap().insert_path("quiet").last_seen = std::time::Instant::now();
        h.run();
        h.get_by_label_contains("(now)");
        assert!(h.query_by_label_contains("no messages in the last 2 s").is_none());
    }
```

  `show_detail_panel` is the function that draws the selected topic's page (pre-P1 `topic_tree.rs:300-307`). If P1 or P3 renamed it, use the current name.
  `quiet_leaf_row_shows_age` goes back 5 minutes, not hours, so `Instant - Duration` cannot underflow on a machine that booted recently.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- rates:: ui::topic_tree`. Expected: compile errors.
- [ ] **Step 3: Implement `src/rates.rs`** (replace the stub):

```rust
//! Per-topic message rates and recent-activity detection (P5 T21).

use std::time::{Duration, Instant};

/// A leaf updated within this window is marked as recently active.
pub const RECENT_WINDOW: Duration = Duration::from_millis(1500);
const WINDOW: Duration = Duration::from_secs(1);
const STALE: Duration = Duration::from_secs(2);

/// Messages per second over the last completed 1 s window.
#[derive(Debug, Clone)]
pub struct RateMeter {
    window_start: Instant,
    count: u32,
    last_rate: f32,
}

impl RateMeter {
    pub fn new(now: Instant) -> Self {
        Self { window_start: now, count: 0, last_rate: 0.0 }
    }

    #[cfg(test)]
    pub fn fixed(now: Instant, rate: f32) -> Self {
        Self { window_start: now, count: 0, last_rate: rate }
    }

    pub fn record(&mut self, now: Instant) {
        let e = now.saturating_duration_since(self.window_start);
        if e >= WINDOW {
            self.last_rate = if e >= STALE { 0.0 } else { self.count as f32 / e.as_secs_f32() };
            self.window_start = now;
            self.count = 0;
        }
        self.count += 1;
    }

    pub fn rate(&self, now: Instant) -> f32 {
        let e = now.saturating_duration_since(self.window_start);
        if e >= STALE {
            0.0
        } else if e >= WINDOW {
            self.count as f32 / e.as_secs_f32()
        } else {
            self.last_rate
        }
    }
}

pub fn is_recent(last_seen: Instant, now: Instant) -> bool {
    now.saturating_duration_since(last_seen) < RECENT_WINDOW
}

/// Age of the last message in words: "now" under 2 s, then seconds, minutes, hours.
pub fn format_age(age: Duration) -> String {
    let s = age.as_secs();
    if age < Duration::from_secs(2) {
        "now".to_string()
    } else if s < 60 {
        format!("{s} s ago")
    } else if s < 3600 {
        format!("{} min ago", s / 60)
    } else {
        format!("{} h ago", s / 3600)
    }
}

/// `""` for zero, one decimal below 10/s, whole numbers above.
pub fn format_rate(r: f32) -> String {
    if r <= 0.0 {
        String::new()
    } else if r < 10.0 {
        format!("{r:.1}/s")
    } else {
        format!("{r:.0}/s")
    }
}
```

- [ ] **Step 4: Add the node field** in `src/types/tree.rs`:
  - add `/// Message rate of this node (P5 T21). pub rate: crate::rates::RateMeter,` to `ZenohNode`;
  - initialise it in `ZenohNode::new` as `rate: crate::rates::RateMeter::new(Instant::now())`;
  - in `update_data`, after `self.last_seen = Instant::now();`, add `self.rate.record(self.last_seen);`.
- [ ] **Step 5: Render it** in `src/ui/topic_tree.rs`, in the leaf part of `show_tree_row` (P3 T10).
  - At the top of `show_tree_row`, add `let now = std::time::Instant::now();`.
  - Replace the leaf `selectable_label` text with:

```rust
                let recent = crate::rates::is_recent(node.last_seen, now);
                let text = format!("{}{} {}", if recent { "↻ " } else { "" }, icon, node.key);
                let label = if recent { RichText::new(text).strong() } else { RichText::new(text) };
                let response = ui.selectable_label(is_selected, label);
```

  - Just before `leader_line_with_count(…)` for leaves, add:

```rust
                let rate = node.rate.rate(now);
                let rate_text = crate::rates::format_rate(rate);
                if !rate_text.is_empty() {
                    ui.label(RichText::new(rate_text).size(TEXT_SMALL_SIZE));
                }
                // Age of the last message (F-T20-4): a topic quiet for an hour must not look like one quiet for 10 s.
                if node.message_count > 0 {
                    let age = now.saturating_duration_since(node.last_seen);
                    ui.label(RichText::new(crate::rates::format_age(age)).size(TEXT_SMALL_SIZE));
                    // Repaint when the words next change: each second under a minute, then at the next minute or hour.
                    let s = age.as_secs();
                    let next = if s < 60 { 1 } else if s < 3600 { 60 - s % 60 } else { 3600 - s % 3600 };
                    ui.ctx().request_repaint_after(std::time::Duration::from_secs(next));
                }
                if recent || rate > 0.0 {
                    ui.ctx().request_repaint_after(std::time::Duration::from_millis(500));
                }
```

  Only rows that are drawn ask for a repaint (the tree is virtualised), and egui keeps the earliest request of a pass, so a long tree costs one timer.

  Keep T20's `scroll_to_me` block after the new `let response`.
- [ ] **Step 5b: Show recency on the topic page (F-T20-4).** In `show_topic_details` (the selected-leaf page), find the tuple that P1 extracts from the node under the tree read lock (pre-P1 `topic_tree.rs:396-412`, which P1 T15 and T24 extend). With `let now = std::time::Instant::now();` before it, also copy out `node.last_seen` and `node.rate.rate(now)`. A node with no messages of its own (`message_count == 0`) gets no line, and P1 T24's branch summary is unchanged. Put the line immediately **before** P1 T24's received-count row, `"Received: {n} (since app start)"`; find it with `grep -n 'Received:' src/ui/topic_tree.rs`. P1 T24 renamed this figure (from `Messages:`), and P1's "Renamed UI strings" table says later plans must not look up or paste back older wording. That row comes after the extraction, below the heading and the Save and Pause row:

```rust
            let age = now.saturating_duration_since(last_seen);
            let wall_now = chrono::Utc::now();
            let wall = wall_now - chrono::Duration::from_std(age).unwrap_or(chrono::Duration::zero());
            let clock = crate::types::format_local_time(&wall, &wall_now);
            // Silence is judged by age; a first message after a gap has rate 0 but is not silent.
            let rate_words = match crate::rates::format_rate(rate) {
                r if !r.is_empty() => format!(" · {r} received"),
                _ if age >= std::time::Duration::from_secs(2) => " · no messages in the last 2 s".to_string(),
                _ => String::new(),
            };
            ui.label(format!("Last message {clock} ({}){rate_words}", crate::rates::format_age(age))).on_hover_text(
                "When this app received the latest message, in local time. The rate counts messages added to this topic \
                 over the last second. With 'List each sample once' on, a sample received by both of this app's \
                 sessions counts once.",
            );
            ui.ctx().request_repaint_after(std::time::Duration::from_secs(1));
```

  The time is the **receive** time, like P1 T24's history cards (F-T20-10), and the hover says so. The rate follows the tree count. After P1 T17 the rate limit no longer thins it. After P1 T16, "List each sample once" (P3 T14's name for the setting) merges only the same sample seen by both of this app's sessions within 250 ms, and a publisher's repeated values are always counted (F-T20-3). The hover says exactly that, and nothing more.
- [ ] **Step 6: Run the tests.** Run `cargo test -- rates:: ui::topic_tree types::`. Expected: every test passes, including `age_words`, `quiet_leaf_row_shows_age`, `topic_page_shows_last_message_age` and the P1 tree tests.
- [ ] **Step 7: Commit.**

```bash
git add src/rates.rs src/types/tree.rs src/ui/topic_tree.rs
git commit -m "feat(tree): per-topic msg/s, last-message age on rows and the topic page, and a recent-activity glyph

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task T22: Connection profiles model and config import (Lane PROF)

**Owns:** `src/profiles.rs`, `src/worker/connect.rs`, `src/worker/session.rs`

**Interfaces:**
- Consumes (T1): the `config_file` and `tls_root_ca` parameters already threaded through `handle_connect`, `connect_zenoh` and `connect_zenoh_monitor`.
- Produces:
  - `Profile { name, mode, endpoints: Vec<String>, listen_port, config_file: Option<PathBuf>, tls_root_ca: Option<PathBuf> }`, with serde.
  - `ProfileStore { profiles, recent_endpoints: VecDeque<String>, last_used }`, with `upsert`, `remove`, `get`, `remember_endpoint`, `load(&Path)`, `save(&Path)` and `default_path()`.
  - `MAX_RECENT = 10`, `SUPPORTED_PROTOCOLS` and `APP_ID`.
  - `fn endpoint_error(&str) -> Option<String>`.
  - `fn compose_endpoint(proto, addr, port, metadata, config) -> String`.
  - `fn config_file_error(&Path) -> Option<String>`.
- **Worker rule:**
  - With `config_file = Some(path)`, `connect_zenoh` starts from `zenoh::Config::from_file(path)` and applies **none** of the explorer's mode, scouting, listen, endpoint or tuning overrides: the file is authoritative.
  - **The file-mode monitor is a client of the traffic it watches (F-T20-7).** The monitor session's config is derived from the same file (`file_monitor_config`). It keeps every other setting of the file (transport, TLS, access control, tuning) and overrides these keys, on the monitor only:
    - `mode = "client"`. zenoh's default peer routing forwards samples from one peer to its clients, but not to its other peers. A peer-mode monitor that dials the user's session therefore misses every sample a third peer sends through that session: the "Connected" but empty `**` tree of F-T20-7. Reproduced against zenoh 1.7.2 with a publishing peer S listening on TCP, a monitor dialling S (listen `[]`, multicast and gossip off) and a third peer connecting to S. A peer monitor received S's own samples but not the third peer's, also when the third peer listened. A client monitor received the third peer's samples.
    - `connect/endpoints` = the dial list below, written as one plain list so it applies in client mode whatever mode-dependent shape the file used.
    - `listen/endpoints = []`, `scouting/multicast/enabled = false` and `scouting/gossip/enabled = false`. This avoids port clashes and self-discovery.
    - `id = null`, so the monitor gets a fresh zenoh id. A file that fixes `id` would give both sessions the same id, and the user's session then refuses the monitor's connection ("Unable to connect", checked on zenoh 1.7.2 and 1.10.1).
    - `connect/timeout_ms = 0` and `connect/exit_on_failure = true`, which are zenoh's client defaults: one attempt per endpoint, then `Err`. A file with a plain `connect: { timeout_ms: -1 }` would otherwise make `zenoh::open` retry an unreachable dial for ever. In a check on zenoh 1.7.2 it blocked for over 4 minutes, and an enclosing `tokio::time::timeout` could not interrupt it.
  - **Overriding the monitor's mode does not change the user's own session.** `connect_zenoh` opens the user's session from the file with none of these overrides, so it runs in exactly the mode, with exactly the endpoints and id, that the file says. Only the internal monitor session, which the user never sees, becomes a client.
  - **What the monitor dials.** `file_monitor_dial` reads the list from the file before any override. A file without `mode` counts as peer mode, because that is zenoh's default. A mode-dependent endpoint list (a `{ router, peer, client }` object) is read at the file's own mode.
    - Peer file (or no `mode`) with an empty `connect/endpoints`: the file's own `listen/endpoints`.
    - Peer file (or no `mode`) with connect endpoints: those connect endpoints.
    - Client file: its connect endpoints.
    - Router file: its own `listen/endpoints`, like a peer file without connect endpoints. zenoh's router default is `tcp/[::]:7447`, so a router file without a `listen` section is dialled at `tcp/[::1]:7447`, then `tcp/127.0.0.1:7447`.
    - Own listen endpoints go through `loopback_dial`. `0.0.0.0` becomes `127.0.0.1`. `[::]` becomes `[::1]`, then `127.0.0.1`, IPv6 first, as in P1 T10's `monitor_endpoints`: a `[::]` socket is IPv6-only on Windows, because zenoh-link-tcp does not set `IPV6_V6ONLY` and the OS default applies. The client-mode monitor keeps the first endpoint that connects, so `127.0.0.1` is the fallback for a host without IPv6 loopback. An endpoint on port 0 is skipped, because zenoh picks that port at open time. `tcp/[::]:0` is zenoh's peer default when the file has no `listen` section. Connect endpoints are dialled unchanged.
  - **Nothing to dial.** With an empty dial list, `connect_zenoh_monitor` returns `Err` rather than opening a blind monitor. The text contains "nothing to dial" and names what to add to the file. P1 T10's monitor-failure arm in `handle_connect` then sends `OperationFailed { op: FailedOp::Monitor, error: e.to_string() }`. This includes a client file that finds its router by multicast scouting only; the fix is to add the router's endpoint to the file's `connect/endpoints`.
  - `tls_root_ca` sets `transport/link/tls/root_ca_certificate` on both sessions.
- **Storage:** profiles live in `profiles.json` inside `eframe::storage_dir(APP_ID)`, the same directory P3's persisted `Settings` use. P3's `Settings` keeps the last-used connection fields; profiles are the named sets.
- zenoh and eframe APIs:
  - `Config::from_file` supports `.json`, `.json5`, `.yaml`/`.yml`, and `.toml` behind `unstable`, with a "TOML format is unstable" warning (zenoh-config 1.10.1 `lib.rs:1457-1500`; https://docs.rs/zenoh/1.10.1/zenoh/struct.Config.html#method.from_file).
  - `EndPoint: FromStr` accepts `proto/address?metadata#config` (zenoh-protocol 1.10.1 `core/endpoint.rs`; separators `?`, `#`, `;`, `=`).
  - The TLS key is `transport/link/tls/root_ca_certificate` (zenoh-config 1.10.1 `lib.rs:781`).
  - `eframe::storage_dir(app_id)` needs the `persistence` feature (https://docs.rs/eframe/0.36.2/eframe/fn.storage_dir.html).

- [ ] **Step 1: Write the failing tests.**
  - In `src/profiles.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("zx-p5-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn profile(name: &str) -> Profile {
        Profile {
            name: name.into(),
            mode: "client".into(),
            endpoints: vec!["tcp/127.0.0.1:7447".into()],
            listen_port: "7447".into(),
            config_file: None,
            tls_root_ca: None,
        }
    }

    #[test]
    fn upsert_remove_and_recent_cap() {
        let mut s = ProfileStore::default();
        s.upsert(profile("b"));
        s.upsert(profile("a"));
        s.upsert(Profile { mode: "peer".into(), ..profile("a") });
        assert_eq!(s.profiles.iter().map(|p| p.name.as_str()).collect::<Vec<_>>(), ["a", "b"]);
        assert_eq!(s.get("a").unwrap().mode, "peer");
        s.last_used = Some("a".into());
        s.remove("a");
        assert!(s.get("a").is_none() && s.last_used.is_none());
        for i in 0..15 {
            s.remember_endpoint(&format!("tcp/h{i}:7447"));
        }
        s.remember_endpoint("tcp/h3:7447");
        assert_eq!(s.recent_endpoints.len(), MAX_RECENT);
        assert_eq!(s.recent_endpoints[0], "tcp/h3:7447");
    }

    #[test]
    fn save_load_roundtrip_and_corrupt_file() {
        let p = tmp("store").join("profiles.json");
        let mut s = ProfileStore::default();
        s.upsert(profile("lab"));
        s.save(&p).unwrap();
        assert_eq!(ProfileStore::load(&p), s);
        std::fs::write(&p, "{ not json").unwrap();
        assert_eq!(ProfileStore::load(&p), ProfileStore::default());
        assert_eq!(ProfileStore::load(&p.with_file_name("missing.json")), ProfileStore::default());
    }

    #[test]
    fn endpoint_validation_and_compose() {
        assert_eq!(endpoint_error("tcp/127.0.0.1:7447"), None);
        let ep = compose_endpoint("tls", "host.example", "7447", "iface=en0", "root_ca_certificate=/c.pem");
        assert_eq!(ep, "tls/host.example:7447?iface=en0#root_ca_certificate=/c.pem");
        assert_eq!(endpoint_error(&ep), None);
        assert!(endpoint_error("").is_some());
        assert!(endpoint_error("pigeon/x:1").unwrap().contains("unsupported protocol"));
        assert!(endpoint_error("localhost:7447").is_some());
    }

    #[test]
    fn config_import_formats() {
        let d = tmp("cfg");
        let j = d.join("c.json5");
        std::fs::write(&j, r#"{ mode: "peer", scouting: { multicast: { enabled: false } } }"#).unwrap();
        assert_eq!(config_file_error(&j), None);
        let t = d.join("c.toml");
        std::fs::write(&t, "mode = \"peer\"\n").unwrap();
        assert_eq!(config_file_error(&t), None, "TOML needs zenoh's unstable feature");
        let bad = d.join("c.ini");
        std::fs::write(&bad, "mode=peer").unwrap();
        assert!(config_file_error(&bad).is_some());
    }
}
```

  - In `src/worker/connect.rs` tests:

```rust
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn connect_with_toml_config_file() {
        let d = std::env::temp_dir().join(format!("zx-p5-{}-connect", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("c.toml");
        std::fs::write(&p, "mode = \"peer\"\n[scouting.multicast]\nenabled = false\n[listen]\nendpoints = []\n").unwrap();
        // UI says "client" with no router: it only opens if the file (peer) wins.
        let s = connect_zenoh("", "7447", "client", "{}", Some(&p), None).await.expect("session from TOML");
        // Monitor: (locators, listen_port, mode, config_file, tls_root_ca). With a file it ignores the
        // form's fields. This peer file has no connect endpoint and `listen = []`, so the monitor has
        // nothing to dial. The "nothing to dial" text also proves the TOML was parsed: a parse
        // failure reads "config file …" instead.
        let e = connect_zenoh_monitor("", "7447", "client", Some(&p), None)
            .await
            .expect_err("a peer file with no endpoint must not open a monitor");
        assert!(e.to_string().contains("nothing to dial"), "{e}");
        s.close().await.unwrap();
    }

    // F-T20-7 in file mode. zenoh's peer default listen endpoint is `tcp/[::]:0`, so this file
    // gives the monitor nothing to dial: it must fail rather than open a blind monitor.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn file_mode_monitor_with_nothing_to_dial_fails() {
        let d = std::env::temp_dir().join(format!("zx-p5-{}-bare", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("peer.json5");
        std::fs::write(&p, r#"{ mode: "peer", scouting: { multicast: { enabled: false } } }"#).unwrap();
        let e = connect_zenoh_monitor("", "", "peer", Some(&p), None)
            .await
            .expect_err("a peer file with nothing to dial must not open a monitor");
        assert!(e.to_string().contains("nothing to dial"), "{e}");
    }

    /// `from` puts `key` every 250 ms until a `**` subscriber on `monitor` receives it; false after 5 s.
    async fn monitor_receives(monitor: &zenoh::Session, from: &zenoh::Session, key: &str) -> bool {
        let sub = monitor.declare_subscriber("**").await.unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            loop {
                from.put(key, "x").await.unwrap();
                let next = tokio::time::timeout(std::time::Duration::from_millis(250), sub.recv_async()).await;
                if matches!(next, Ok(Ok(s)) if s.key_expr().as_str() == key) {
                    return;
                }
            }
        })
        .await
        .is_ok()
    }

    // P1 T10's `monitor_sees_third_party_samples`, for a config file. Port 27651 must be free.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "opens network sessions"]
    async fn file_mode_monitor_sees_third_party_samples() {
        let d = std::env::temp_dir().join(format!("zx-p5-{}-dial", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("peer.json5");
        // Peer mode, no connect endpoint, and an unspecified listen host the monitor must map to 127.0.0.1.
        std::fs::write(
            &p,
            r#"{ mode: "peer", scouting: { multicast: { enabled: false } }, listen: { endpoints: ["tcp/0.0.0.0:27651"] } }"#,
        )
        .unwrap();
        let s = connect_zenoh("", "", "peer", "{}", Some(&p), None).await.expect("session from file");
        let m = connect_zenoh_monitor("", "", "peer", Some(&p), None).await.expect("monitor from file");
        // Third session: peer mode, multicast scouting off, one connect endpoint (as in P1 T10's test).
        let mut c = zenoh::Config::default();
        c.insert_json5("mode", r#""peer""#).unwrap();
        c.insert_json5("scouting/multicast/enabled", "false").unwrap();
        c.insert_json5("listen/endpoints", "[]").unwrap();
        c.insert_json5("connect/endpoints", r#"["tcp/127.0.0.1:27651"]"#).unwrap();
        let third = zenoh::open(c).await.unwrap();
        // The publishing session's own put proves the dial. The third session's put is F-T20-7:
        // it reaches the monitor only because the monitor is a client of the publishing session.
        assert!(monitor_receives(&m, &s, "t/s").await, "monitor did not receive the publishing session's t/s");
        assert!(monitor_receives(&m, &third, "t/f").await, "monitor did not receive the third session's t/f");
        // The user's session stays a peer, as the file says: the third session is its peer. The
        // monitor joined it as a client, so it is not in the peer list.
        let peers: Vec<_> = s.info().peers_zid().await.collect();
        assert!(peers.contains(&third.zid()), "the user's session is not in peer mode");
        assert!(!peers.contains(&m.zid()), "the monitor joined as a peer, not as a client");
        third.close().await.unwrap();
        m.close().await.unwrap();
        s.close().await.unwrap();
    }
```

  The calls use P1 T10's order plus T1's two trailing parameters: `connect_zenoh(locators, listen_port, mode, config_json, config_file, tls_root_ca)` takes six arguments and `connect_zenoh_monitor(locators, listen_port, mode, config_file, tls_root_ca)` five. P1 T10 replaced the monitor's old monitor-port parameter with `listen_port`, so there is no monitor port. Two tests pass an empty `listen_port` with a file, which shows that neither call parses it in file mode.
- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test -- profiles:: connect_with_toml`. Expected: compile errors in `profiles`. `connect_with_toml_config_file` fails because the "client" mode without a router cannot open. The two `file_mode_monitor_*` tests are first run in Step 4, once the crate compiles and before the monitor fix.
- [ ] **Step 3: Implement `src/profiles.rs`** (replace the stub):

```rust
//! Connection profiles, recent endpoints and config-file validation (P5 T22).

use std::collections::VecDeque;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const MAX_RECENT: usize = 10;
/// Must match the `app_name` passed to `eframe::run_native` in `main.rs`.
pub const APP_ID: &str = "Zenoh Explorer";
pub const SUPPORTED_PROTOCOLS: &[&str] = &["tcp", "udp", "tls", "quic", "ws", "unixsock-stream"];

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Profile {
    pub name: String,
    /// "peer" or "client" (ignored when `config_file` is set).
    pub mode: String,
    pub endpoints: Vec<String>,
    pub listen_port: String,
    #[serde(default)]
    pub config_file: Option<PathBuf>,
    #[serde(default)]
    pub tls_root_ca: Option<PathBuf>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ProfileStore {
    pub profiles: Vec<Profile>,
    #[serde(default)]
    pub recent_endpoints: VecDeque<String>,
    #[serde(default)]
    pub last_used: Option<String>,
}

impl ProfileStore {
    /// Insert or replace by name; profiles stay sorted by name.
    pub fn upsert(&mut self, p: Profile) {
        match self.profiles.iter_mut().find(|x| x.name == p.name) {
            Some(x) => *x = p,
            None => {
                self.profiles.push(p);
                self.profiles.sort_by(|a, b| a.name.cmp(&b.name));
            }
        }
    }

    pub fn remove(&mut self, name: &str) {
        self.profiles.retain(|p| p.name != name);
        if self.last_used.as_deref() == Some(name) {
            self.last_used = None;
        }
    }

    pub fn get(&self, name: &str) -> Option<&Profile> {
        self.profiles.iter().find(|p| p.name == name)
    }

    /// Most recent first, de-duplicated, at most `MAX_RECENT`.
    pub fn remember_endpoint(&mut self, ep: &str) {
        let ep = ep.trim();
        if ep.is_empty() {
            return;
        }
        self.recent_endpoints.retain(|e| e != ep);
        self.recent_endpoints.push_front(ep.to_string());
        self.recent_endpoints.truncate(MAX_RECENT);
    }

    /// Missing or corrupt files yield an empty store (corruption is logged).
    pub fn load(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(s) => serde_json::from_str(&s).unwrap_or_else(|e| {
                tracing::warn!("ignoring unreadable profiles file {}: {e}", path.display());
                Self::default()
            }),
            Err(_) => Self::default(),
        }
    }

    /// Atomic write (temp file + rename).
    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self).map_err(std::io::Error::other)?)?;
        std::fs::rename(tmp, path)
    }

    pub fn default_path() -> Option<PathBuf> {
        eframe::storage_dir(APP_ID).map(|d| d.join("profiles.json"))
    }
}

/// `None` when `s` is a Zenoh endpoint with a supported protocol.
pub fn endpoint_error(s: &str) -> Option<String> {
    let s = s.trim();
    if s.is_empty() {
        return Some("empty endpoint".to_string());
    }
    let proto = s.split_once('/').map_or("", |(p, _)| p);
    if !SUPPORTED_PROTOCOLS.contains(&proto) {
        return Some(format!("unsupported protocol '{proto}' (use {})", SUPPORTED_PROTOCOLS.join(", ")));
    }
    s.parse::<zenoh::config::EndPoint>().err().map(|e| e.to_string())
}

/// `proto/addr:port[?metadata][#config]`; metadata and config are `k=v;k=v`.
pub fn compose_endpoint(proto: &str, addr: &str, port: &str, metadata: &str, config: &str) -> String {
    let mut s = format!("{proto}/{addr}:{port}");
    if !metadata.trim().is_empty() {
        s.push('?');
        s.push_str(metadata.trim());
    }
    if !config.trim().is_empty() {
        s.push('#');
        s.push_str(config.trim());
    }
    s
}

/// `None` when zenoh can load the file (json/json5/yaml/toml).
pub fn config_file_error(path: &Path) -> Option<String> {
    zenoh::Config::from_file(path).err().map(|e| e.to_string())
}
```

- [ ] **Step 4: Implement the worker side.**
  - In `src/worker/connect.rs::connect_zenoh`, rename `_config_file` and `_tls_root_ca` to `config_file` and `tls_root_ca`. Replace `let mut config = zenoh::config::Config::default();` and **everything up to the `zenoh::open` call** with:

```rust
    let mut config = match config_file {
        Some(p) => zenoh::Config::from_file(p).map_err(|e| format!("config file {}: {e}", p.display()))?,
        None => {
            let mut config = zenoh::config::Config::default();
            // <P1's existing setters, unchanged: tuning, mode, scouting, listen, connect endpoints, config_json>
            config
        }
    };
    if let Some(ca) = tls_root_ca {
        config.insert_json5("transport/link/tls/root_ca_certificate", &serde_json::to_string(&ca.display().to_string())?)?;
    }
```

    P1's setters move inside the `None` arm verbatim. They already return errors with `?` after P1 T10.
  - In `connect_zenoh_monitor`, apply the same match. The `Some(p)` arm is:

```rust
        Some(p) => {
            let mut c = zenoh::Config::from_file(p).map_err(|e| format!("config file {}: {e}", p.display()))?;
            c.insert_json5("listen/endpoints", "[]")?;
            c.insert_json5("scouting/multicast/enabled", "false")?;
            c.insert_json5("scouting/gossip/enabled", "false")?;
            c
        }
```

    Add the same `tls_root_ca` block after it.
  - **The file-mode monitor runs as a client of the traffic it watches (F-T20-7; see the Worker rule).** Follow P1 T10's order: first the test, then the fix.
    - Test first. With the arm above in place, run `cargo test file_mode_monitor_with_nothing_to_dial_fails`, then `cargo test -- --ignored file_mode_monitor_sees_third_party_samples`. Expected: both FAIL. The monitor for the bare file opens instead of failing, and `t/s` is not received, because the monitor has no endpoint and nothing reaches it. `connect_with_toml_config_file` fails at this point for the same reason: its monitor opens instead of returning "nothing to dial". Dialling the listener alone would not be enough: a peer-mode monitor then receives `t/s` but still not the third session's `t/f` (the Worker rule's zenoh 1.7.2 reproduction).
    - Add these two tests to the `connect.rs` tests, then run `cargo test -- loopback_dial file_monitor_config`. Expected: a compile error, because `loopback_dial` and `file_monitor_config` do not exist yet.

```rust
    #[test]
    fn loopback_dial_maps_unspecified_host() {
        assert_eq!(loopback_dial("tcp/0.0.0.0:7447"), ["tcp/127.0.0.1:7447"]);
        // `[::]` is IPv6-only on Windows: IPv6 loopback first, then IPv4, as P1 T10's `monitor_endpoints`.
        assert_eq!(
            loopback_dial("tcp/[::]:7447#iface=en0"),
            ["tcp/[::1]:7447#iface=en0", "tcp/127.0.0.1:7447#iface=en0"]
        );
        assert_eq!(loopback_dial("udp/192.168.1.5:7447"), ["udp/192.168.1.5:7447"]);
        assert!(loopback_dial("tcp/[::]:0").is_empty());
    }

    // F-T20-7: what the file-mode monitor becomes for each file mode. Builds configs; opens nothing.
    #[test]
    fn file_monitor_config_by_mode() {
        let d = std::env::temp_dir().join(format!("zx-p5-{}-monitor-cfg", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let monitor = |name: &str, body: &str| {
            let p = d.join(name);
            std::fs::write(&p, body).unwrap();
            file_monitor_config(&p)
        };
        let get = |c: &zenoh::Config, key: &str| c.get_json(key).unwrap();
        // Peer file, no connect endpoint: its own listener on loopback. Fixed id and endless timeout are overridden.
        let c = monitor(
            "peer.json5",
            r#"{ id: "aabbccdd", mode: "peer", connect: { timeout_ms: -1 }, listen: { endpoints: ["tcp/0.0.0.0:7447"] } }"#,
        )
        .unwrap();
        assert_eq!(get(&c, "mode"), r#""client""#);
        assert_eq!(get(&c, "connect/endpoints"), r#"["tcp/127.0.0.1:7447"]"#);
        assert_eq!(get(&c, "listen/endpoints"), "[]");
        assert_eq!(get(&c, "id"), "null");
        assert_eq!(get(&c, "connect/timeout_ms"), "0");
        assert_eq!(get(&c, "connect/exit_on_failure"), "true");
        assert_eq!(get(&c, "scouting/multicast/enabled"), "false");
        assert_eq!(get(&c, "scouting/gossip/enabled"), "false");
        // No `mode` (so peer) with a connect endpoint: that endpoint, not the listener.
        let c = monitor(
            "nomode.json5",
            r#"{ connect: { endpoints: ["tcp/10.0.0.2:7447"] }, listen: { endpoints: ["tcp/0.0.0.0:7447"] } }"#,
        )
        .unwrap();
        assert_eq!(get(&c, "connect/endpoints"), r#"["tcp/10.0.0.2:7447"]"#);
        // Client file with a mode-dependent list: its client entry.
        let c = monitor(
            "client.json5",
            r#"{ mode: "client", connect: { endpoints: { client: ["tcp/10.0.0.3:7447"], peer: [] } } }"#,
        )
        .unwrap();
        assert_eq!(get(&c, "connect/endpoints"), r#"["tcp/10.0.0.3:7447"]"#);
        // Router file without `listen`: zenoh's router default `tcp/[::]:7447`, on both loopbacks, IPv6 first.
        let c = monitor("router.json5", r#"{ mode: "router" }"#).unwrap();
        assert_eq!(get(&c, "connect/endpoints"), r#"["tcp/[::1]:7447","tcp/127.0.0.1:7447"]"#);
        // Client file with no connect endpoint: nothing to dial.
        let e = monitor("client-bare.json5", r#"{ mode: "client" }"#).unwrap_err();
        assert!(e.contains("nothing to dial"), "{e}");
    }
```

    - Fix. Add these three private helpers to `connect.rs`. `file_monitor_dial` reads the file through `Config::get_json`, because zenoh does not export the `ModeDependent` trait. A mode-dependent endpoint list comes back either as one list or as a `{ router, peer, client }` object.

```rust
/// A listen endpoint as a dialer on this machine reaches it. `0.0.0.0` becomes `127.0.0.1`.
/// `[::]` becomes `[::1]`, then `127.0.0.1`: IPv6 first, as in P1 T10's `monitor_endpoints`,
/// because a `[::]` socket is IPv6-only on Windows. The client-mode monitor keeps the first
/// endpoint that connects. Empty for port 0, which zenoh picks at open.
fn loopback_dial(listen: &str) -> Vec<String> {
    let Some((proto, rest)) = listen.split_once('/') else {
        return Vec::new();
    };
    let (addr, tail) = rest.split_at(rest.find(['?', '#']).unwrap_or(rest.len()));
    let Some((host, port)) = addr.rsplit_once(':') else {
        return vec![listen.to_string()]; // no host:port, e.g. unixsock-stream
    };
    if port == "0" {
        return Vec::new();
    }
    let hosts = match host {
        "0.0.0.0" => vec!["127.0.0.1"],
        "[::]" => vec!["[::1]", "127.0.0.1"],
        _ => vec![host],
    };
    hosts.iter().map(|h| format!("{proto}/{h}:{port}{tail}")).collect()
}

/// What the file-mode monitor dials (F-T20-7), read before `file_monitor_config` overrides
/// anything. A file without `mode` is peer mode, zenoh's default. A peer file with no connect
/// endpoint, and a router file, give their own listen endpoints through `loopback_dial`. A peer
/// file with connect endpoints, and a client file, give those connect endpoints unchanged.
fn file_monitor_dial(c: &zenoh::Config) -> Result<Vec<String>, String> {
    let json = |key: &str| -> Result<serde_json::Value, String> {
        let s = c.get_json(key).map_err(|e| format!("config file {key}: {e}"))?;
        serde_json::from_str(&s).map_err(|e| format!("config file {key}: {e}"))
    };
    let mode = json("mode")?.as_str().unwrap_or("peer").to_string();
    // A mode-dependent list is one list or a `{ router, peer, client }` object: read the file's mode.
    let endpoints = |key: &str| -> Result<Vec<String>, String> {
        let v = json(key)?;
        let list = if v.is_object() { v[mode.as_str()].clone() } else { v };
        Ok(list.as_array().into_iter().flatten().filter_map(|e| e.as_str().map(String::from)).collect())
    };
    let connect = endpoints("connect/endpoints")?;
    let dial: Vec<String> = match mode.as_str() {
        "client" => connect,
        "peer" if !connect.is_empty() => connect,
        _ => endpoints("listen/endpoints")?.iter().flat_map(|e| loopback_dial(e)).collect(),
    };
    if dial.is_empty() {
        let need = match mode.as_str() {
            "client" => "a connect endpoint",
            "router" => "a listen endpoint on a fixed port",
            _ => "a connect endpoint or a listen endpoint on a fixed port",
        };
        return Err(format!("the monitor has nothing to dial: add {need} to this {mode}-mode config file"));
    }
    Ok(dial)
}

/// The monitor's config in file mode (F-T20-7): the file itself, with only the monitor's role
/// overridden. It runs as a client, because zenoh's peer routing forwards a peer's samples to
/// its clients but not to its other peers. The user's session is untouched: `connect_zenoh`
/// opens it from the same file with none of these overrides, in exactly the mode the file says.
fn file_monitor_config(p: &std::path::Path) -> Result<zenoh::Config, String> {
    let mut c = zenoh::Config::from_file(p).map_err(|e| format!("config file {}: {e}", p.display()))?;
    // Read the file's own endpoints before they are replaced below.
    let dial = serde_json::to_string(&file_monitor_dial(&c)?).map_err(|e| e.to_string())?;
    let mut set = |key: &str, value: &str| {
        c.insert_json5(key, value).map_err(|e| format!("monitor config {key}: {e}"))
    };
    set("mode", r#""client""#)?;
    // One plain list, so it applies in client mode whatever mode-dependent shape the file used.
    set("connect/endpoints", &dial)?;
    set("listen/endpoints", "[]")?;
    set("scouting/multicast/enabled", "false")?;
    set("scouting/gossip/enabled", "false")?;
    // A fresh zenoh id: a file that fixes `id` gives both sessions the same one, and the user's
    // session then refuses the monitor's connection.
    set("id", "null")?;
    // zenoh's client defaults: one attempt per endpoint, then `Err`. An inherited plain
    // `timeout_ms: -1` makes `zenoh::open` retry an unreachable dial for ever.
    set("connect/timeout_ms", "0")?;
    set("connect/exit_on_failure", "true")?;
    Ok(c)
}
```

    In `connect_zenoh_monitor`, the `Some(p)` arm becomes:

```rust
        Some(p) => file_monitor_config(p)?,
```

    The `tls_root_ca` block after the match stays as it is. `connect_zenoh`'s `Some(p)` arm does not change: the user's session is opened from the file with no overrides, in the file's own mode. The monitor call keeps its five arguments in T1's order, `(locators, listen_port, mode, config_file, tls_root_ca)`. In file mode it ignores its `listen_port` and `mode` arguments, as it ignores the form's other fields. `session.rs` needs no change for this: the `Err` reaches P1 T10's monitor-failure arm, which sends `OperationFailed { op: FailedOp::Monitor, error: e.to_string() }` before `MonitorConnected`. P1 T22's header then reads "Connected · monitor off".
    - Rerun the five tests (`loopback_dial_maps_unspecified_host`, `file_monitor_config_by_mode`, `file_mode_monitor_with_nothing_to_dial_fails`, `connect_with_toml_config_file`, then the ignored `file_mode_monitor_sees_third_party_samples`). Expected: PASS, including the third session's `t/f` and the peer-list assertions. If `t/s` arrives but `t/f` does not, stop and report. Do not switch the monitor back to peer mode and do not enable gossip.
  - `src/worker/session.rs` needs no change in this task. After P1 T10, `handle_connect` passes the form's `listen_port` to both connect calls and computes no monitor port. Only the `None` arms read `listen_port` (`parse_listen_port` in `connect_zenoh`, `monitor_endpoints` in `connect_zenoh_monitor`), so an empty listen port in file mode is not an error. Do not add a monitor-port variable or parameter anywhere.
- [ ] **Step 5: Run the tests.** Run `cargo test -- profiles:: worker::connect worker::session && cargo test -- --ignored file_mode_monitor_sees_third_party_samples monitor_sees_third_party_samples; grep -rn 'monitor_port' src/worker`. Expected: all of these pass: the 4 profile tests, `connect_with_toml_config_file`, `file_mode_monitor_with_nothing_to_dial_fails`, `loopback_dial_maps_unspecified_host`, `file_monitor_config_by_mode`, P1 T10's `listen_port_rejects_zero_and_garbage`, `monitor_endpoints_follow_publishing_mode` and `connect_error_text_has_no_source_path`, and then the two ignored monitor tests (this task's, including its third-session `t/f` assertion, and P1 T10's `monitor_sees_third_party_samples` for the `None` arm). The grep prints nothing. `connect_with_toml_config_file` needs the zenoh 1.10.x lock from P1 T1: zenoh-config 1.7.2's `from_file` rejects `.toml` ("Unsupported file type"), so on a 1.7.2 lock it fails before it reaches the monitor. If it fails that way, check `Cargo.lock` before changing any code.
- [ ] **Step 6: Commit.**

```bash
git add src/profiles.rs src/worker/connect.rs src/worker/session.rs
git commit -m "feat(connect): profiles store, endpoint validation, JSON5/YAML/TOML config import and TLS root CA

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T23: Connection panel with profiles (Lane PROF)

**Owns:** `src/ui/connection.rs`

**Interfaces:**
- Consumes:
  - T22: `profiles::*`.
  - T1: the moved panel and `ZenohCommand::Connect { …, config_file, tls_root_ca }`.
  - App fields: `connection_mode`, `listen_port`, and `connect_transport` / `connect_address` / `connect_port`, which become the endpoint builder's fields and so stay in use and stay persisted by P3's `Settings`.
- Produces: `ConnectionFormState { store, store_path, loaded, selected, new_name, endpoints, builder_meta, builder_config, config_file, config_error, tls_root_ca }`, with `ensure_loaded` and `persist`.
- rfd 0.17: `rfd::FileDialog::new().add_filter(name, &[ext]).pick_file() -> Option<PathBuf>` (https://docs.rs/rfd/0.17.2/rfd/struct.FileDialog.html). Picking only returns a path and reads nothing, so the blocking dialog is acceptable here. P3's `dialogs.rs` exists for chunked file I/O.

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/connection.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use crate::profiles::{Profile, ProfileStore};
    use egui_kittest::{kittest::Queryable, Harness};

    fn app_with(store: ProfileStore, endpoints: Vec<String>) -> (ZenohExplorer, std::sync::mpsc::Receiver<ZenohCommand>) {
        let (mut app, _tx, cmds) = ZenohExplorer::test_app_with_commands();
        app.connection_status = ConnectionStatus::Disconnected;
        app.connection_form.loaded = true; // never touch the real profiles file in tests
        app.connection_form.store_path = None;
        app.connection_form.store = store;
        app.connection_form.endpoints = endpoints;
        (app, cmds)
    }

    fn connect_cmd(cmds: &std::sync::mpsc::Receiver<ZenohCommand>) -> Option<(String, String)> {
        cmds.try_iter().find_map(|c| match c {
            ZenohCommand::Connect { locators, mode, .. } => Some((locators, mode)),
            _ => None,
        })
    }

    #[test]
    fn choosing_profile_fills_connect() {
        let mut store = ProfileStore::default();
        store.upsert(Profile {
            name: "lab".into(),
            mode: "client".into(),
            endpoints: vec!["tcp/10.0.0.1:7447".into()],
            listen_port: "7447".into(),
            config_file: None,
            tls_root_ca: None,
        });
        let (app, cmds) = app_with(store, vec![]);
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_connection_panel(ui), app);
        h.run();
        h.get_by_value("(no profile)").click();
        h.run();
        h.get_by_label("lab").click();
        h.run();
        h.get_by_label("Connect").click();
        h.run();
        assert_eq!(connect_cmd(&cmds), Some(("tcp/10.0.0.1:7447".to_string(), "client".to_string())));
        assert_eq!(h.state().connection_form.store.recent_endpoints.front().map(String::as_str), Some("tcp/10.0.0.1:7447"));
    }

    #[test]
    fn invalid_endpoint_disables_connect() {
        let (app, cmds) = app_with(ProfileStore::default(), vec!["pigeon/x:1".into()]);
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_connection_panel(ui), app);
        h.run();
        h.get_by_label_contains("unsupported protocol");
        h.get_by_label("Connect").click();
        h.run();
        assert_eq!(connect_cmd(&cmds), None);
    }

    #[test]
    fn save_as_adds_profile() {
        let (mut app, _cmds) = app_with(ProfileStore::default(), vec!["tcp/1.2.3.4:7447".into()]);
        app.connection_form.new_name = "home".into();
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_connection_panel(ui), app);
        h.run();
        h.get_by_label("Save as").click();
        h.run();
        let p = h.state().connection_form.store.get("home").expect("saved").clone();
        assert_eq!(p.endpoints, vec!["tcp/1.2.3.4:7447".to_string()]);
        assert_eq!(h.state().connection_form.selected.as_deref(), Some("home"));
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::connection`. Expected: compile errors (the form fields are missing).
- [ ] **Step 3: Implement.**
  - Replace T1's `ConnectionFormState` stub:

```rust
use std::path::PathBuf;

use crate::profiles::{self, Profile, ProfileStore};

#[derive(Debug, Default)]
pub struct ConnectionFormState {
    pub store: ProfileStore,
    /// `None` in tests; otherwise `ProfileStore::default_path()`.
    pub store_path: Option<PathBuf>,
    pub loaded: bool,
    pub selected: Option<String>,
    pub new_name: String,
    pub endpoints: Vec<String>,
    pub builder_meta: String,
    pub builder_config: String,
    pub config_file: Option<PathBuf>,
    pub config_error: Option<String>,
    pub tls_root_ca: Option<PathBuf>,
}

impl ConnectionFormState {
    /// Load the profiles file once, on first display.
    pub fn ensure_loaded(&mut self) {
        if self.loaded {
            return;
        }
        self.loaded = true;
        self.store_path = ProfileStore::default_path();
        if let Some(p) = &self.store_path {
            self.store = ProfileStore::load(p);
        }
    }

    pub fn persist(&self) {
        if let Some(p) = &self.store_path {
            if let Err(e) = self.store.save(p) {
                tracing::warn!("could not save profiles to {}: {e}", p.display());
            }
        }
    }
}
```

  - In `show_connection_panel`, keep T1's moved structure: the `if Disconnected | Error { group } else { Disconnect }` branch and the Disconnect branch stay unchanged. Replace the **body of the settings group** with the code below. It takes the place of the old Transport/Address/Port row and the locator preview. Keep the Mode combo, Listen Port and the explanatory labels from the moved code at the marked spot.

```rust
                self.connection_form.ensure_loaded();
                ui.label("Connection Settings");
                ui.horizontal(|ui| {
                    ui.label("Profile:");
                    let shown = self.connection_form.selected.clone().unwrap_or_else(|| "(no profile)".to_string());
                    let mut chosen: Option<Profile> = None;
                    egui::ComboBox::from_id_salt("profile_pick").selected_text(shown).show_ui(ui, |ui| {
                        for p in &self.connection_form.store.profiles {
                            if ui.selectable_label(self.connection_form.selected.as_deref() == Some(p.name.as_str()), &p.name).clicked() {
                                chosen = Some(p.clone());
                            }
                        }
                    });
                    if let Some(p) = chosen {
                        self.connection_mode = p.mode.clone();
                        self.listen_port = p.listen_port.clone();
                        self.connection_form.endpoints = p.endpoints.clone();
                        self.connection_form.config_file = p.config_file.clone();
                        self.connection_form.tls_root_ca = p.tls_root_ca.clone();
                        self.connection_form.selected = Some(p.name);
                    }
                    ui.add(egui::TextEdit::singleline(&mut self.connection_form.new_name).desired_width(100.0).hint_text("profile name"));
                    if ui.add_enabled(!self.connection_form.new_name.trim().is_empty(), egui::Button::new("Save as")).clicked() {
                        let name = self.connection_form.new_name.trim().to_string();
                        self.connection_form.store.upsert(Profile {
                            name: name.clone(),
                            mode: self.connection_mode.clone(),
                            endpoints: self.connection_form.endpoints.clone(),
                            listen_port: self.listen_port.clone(),
                            config_file: self.connection_form.config_file.clone(),
                            tls_root_ca: self.connection_form.tls_root_ca.clone(),
                        });
                        self.connection_form.selected = Some(name);
                        self.connection_form.persist();
                    }
                    if let Some(sel) = self.connection_form.selected.clone() {
                        if ui.button("Delete profile").clicked() {
                            self.connection_form.store.remove(&sel);
                            self.connection_form.selected = None;
                            self.connection_form.persist();
                        }
                    }
                });

                let from_file = self.connection_form.config_file.is_some();
                ui.add_enabled_ui(!from_file, |ui| {
                    ui.label("Endpoints (empty = multicast discovery):");
                    let mut remove = None;
                    for (i, ep) in self.connection_form.endpoints.iter_mut().enumerate() {
                        ui.horizontal(|ui| {
                            ui.add(egui::TextEdit::singleline(ep).desired_width(320.0));
                            match profiles::endpoint_error(ep) {
                                Some(e) => ui.label(format!("⚠ {e}")),
                                None => ui.label("✓ valid"),
                            };
                            if ui.small_button(format!("Remove endpoint {}", i + 1)).clicked() {
                                remove = Some(i);
                            }
                        });
                    }
                    if let Some(i) = remove {
                        self.connection_form.endpoints.remove(i);
                    }
                    ui.horizontal(|ui| {
                        egui::ComboBox::from_id_salt("endpoint_proto").selected_text(&self.connect_transport).show_ui(ui, |ui| {
                            for p in profiles::SUPPORTED_PROTOCOLS {
                                ui.selectable_value(&mut self.connect_transport, (*p).to_string(), *p);
                            }
                        });
                        ui.add(egui::TextEdit::singleline(&mut self.connect_address).desired_width(120.0).hint_text("address"));
                        ui.add(egui::TextEdit::singleline(&mut self.connect_port).desired_width(50.0).hint_text("port"));
                        ui.label("?");
                        ui.add(egui::TextEdit::singleline(&mut self.connection_form.builder_meta).desired_width(110.0).hint_text("iface=en0"));
                        ui.label("#");
                        ui.add(egui::TextEdit::singleline(&mut self.connection_form.builder_config).desired_width(170.0).hint_text("root_ca_certificate=/ca.pem"));
                        if ui.add_enabled(!self.connect_address.trim().is_empty(), egui::Button::new("Add endpoint")).clicked() {
                            let ep = profiles::compose_endpoint(
                                &self.connect_transport,
                                self.connect_address.trim(),
                                self.connect_port.trim(),
                                &self.connection_form.builder_meta,
                                &self.connection_form.builder_config,
                            );
                            self.connection_form.endpoints.push(ep);
                        }
                        let recent: Vec<String> = self.connection_form.store.recent_endpoints.iter().cloned().collect();
                        ui.add_enabled_ui(!recent.is_empty(), |ui| {
                            ui.menu_button("Recent…", |ui| {
                                for ep in recent {
                                    if ui.button(&ep).clicked() {
                                        self.connection_form.endpoints.push(ep);
                                        ui.close();
                                    }
                                }
                            });
                        });
                    });
                    // <T1-moved Mode combo, Listen Port row and the peer/client help labels go here, unchanged>
                });

                ui.horizontal(|ui| {
                    ui.label("Config file:");
                    match &self.connection_form.config_file {
                        Some(p) => ui.label(p.display().to_string()),
                        None => ui.label("(none)"),
                    };
                    if ui.button("Import config…").clicked() {
                        if let Some(p) = rfd::FileDialog::new()
                            .add_filter("Zenoh config", &["json5", "json", "yaml", "yml", "toml"])
                            .pick_file()
                        {
                            self.connection_form.config_error = profiles::config_file_error(&p);
                            if self.connection_form.config_error.is_none() {
                                self.connection_form.config_file = Some(p);
                            }
                        }
                    }
                    if from_file && ui.button("Clear config file").clicked() {
                        self.connection_form.config_file = None;
                    }
                });
                if let Some(e) = &self.connection_form.config_error {
                    ui.label(format!("⚠ config not loaded: {e}"));
                }
                if from_file {
                    ui.label("Mode, endpoints and listen port come from the config file.");
                }
                ui.horizontal(|ui| {
                    ui.label("TLS root CA:");
                    match &self.connection_form.tls_root_ca {
                        Some(p) => ui.label(p.display().to_string()),
                        None => ui.label("(none)"),
                    };
                    if ui.button("Choose certificate…").clicked() {
                        if let Some(p) = rfd::FileDialog::new().add_filter("Certificate", &["pem", "crt", "cer"]).pick_file() {
                            self.connection_form.tls_root_ca = Some(p);
                        }
                    }
                    if self.connection_form.tls_root_ca.is_some() && ui.button("Clear certificate").clicked() {
                        self.connection_form.tls_root_ca = None;
                    }
                });
```

  - Replace the moved Connect button block. Keep P1 T12's status handling (the Error state when the send fails) and use `send_command` in its place:

```rust
                if let ConnectionStatus::Error(ref err) = self.connection_status {
                    ui.label(format!("⚠ Error: {err}"));
                }
                let endpoints_ok = from_file || self.connection_form.endpoints.iter().all(|e| profiles::endpoint_error(e).is_none());
                if ui.add_enabled(endpoints_ok, egui::Button::new("Connect")).clicked() {
                    let locators = if from_file { String::new() } else { self.connection_form.endpoints.join(",") };
                    let sent = self.send_command(ZenohCommand::Connect {
                        locators,
                        listen_port: self.listen_port.clone(),
                        mode: self.connection_mode.clone(),
                        config_json: self.config_json.clone(),
                        config_file: self.connection_form.config_file.clone(),
                        tls_root_ca: self.connection_form.tls_root_ca.clone(),
                    });
                    self.connection_status = if sent {
                        ConnectionStatus::ConnectingPublishing
                    } else {
                        ConnectionStatus::Error("worker not running".to_string())
                    };
                    for ep in self.connection_form.endpoints.clone() {
                        self.connection_form.store.remember_endpoint(&ep);
                    }
                    self.connection_form.store.last_used = self.connection_form.selected.clone();
                    self.connection_form.persist();
                }
```

  - **Migration.** When `ensure_loaded` runs, if `endpoints` is empty and `connect_address` is non-empty, push `compose_endpoint(&self.connect_transport, &self.connect_address, &self.connect_port, "", "")`, so a P3-restored address still connects. Do this right after `self.connection_form.ensure_loaded();`, guarded by a local `first = !self.connection_form.loaded` captured before the call.
- [ ] **Step 4: Run the tests.** Run `cargo test ui::connection`. Expected: 3 passed.
- [ ] **Step 5: Manual check.**
  - Save a profile "local-router" with `tcp/127.0.0.1:7447` in client mode, quit, relaunch, pick it, and connect to zenohd.
  - Import a `.toml` with `mode = "client"` and `connect.endpoints = ["tcp/127.0.0.1:7447"]`, then connect.
  - Pick a non-config file (`.txt`): an error is shown and nothing is set.
- [ ] **Step 6: Commit.**

```bash
git add src/ui/connection.rs
git commit -m "feat(ui): connection profiles, endpoint editor with ?/# metadata, recent endpoints, config import and TLS CA picker

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T24: Global keyboard shortcuts (Lane KEYS)

**Owns:** `src/shortcuts.rs`, `src/ui/help.rs`

**Interfaces:**
- Consumes (T1): `DetailView::TABS`, `shortcut_submit`, `TREE_FILTER_ID`, `PUBLISH_KEY_ID`, `QUERY_SELECTOR_ID`, and the `handle_shortcuts` call at the top of `App::ui`. P1 T21: `UiAlert`. P1 T26: `HELP_SECTIONS`.
- Produces `handle_shortcuts` with these bindings:
  - Ctrl/Cmd+1..8 select a tab in `DetailView::TABS` order **and move focus into that view** (F-T19-5), through `fn focus_target(view: DetailView) -> Option<egui::Id>`: Topics → the tree filter, Publish → the Key field, Query → the Selector field. The other tabs (Admin, Topology, Liveliness, Logs, Help) have no focus target in P5; for them the shortcut releases focus, so it does not stay on a tree row or a field of the view just left.
  - Ctrl/Cmd+F shows Topics and focuses the filter.
  - Ctrl/Cmd+Enter sets `shortcut_submit`, only on Publish or Query. T13 and T16 consume it.
  - Esc dismisses `ui_alert` when nothing has focus.
  - Space toggles `auto_scroll` when nothing has focus.
- egui APIs (0.36.2):
  - `InputState::{consume_shortcut, consume_key}`, `KeyboardShortcut::new(Modifiers::COMMAND, Key::…)` (https://docs.rs/egui/0.36.2/egui/struct.InputState.html#method.consume_shortcut).
  - `Memory::{focused, request_focus}`. `Modifiers::COMMAND` is Cmd on macOS and Ctrl elsewhere.
- **Focus targets need the widget on screen.** egui drops focus from a widget that was not drawn in the pass, so `request_focus` works because `handle_shortcuts` runs at the top of `ui` and the view is drawn later in the same pass.
- **Focus rule:** egui clears focus on Esc at `begin_pass`. So Esc pressed in a text field both unfocuses it and, if a banner is shown, dismisses the banner. That matches the "Esc dismisses" expectation.

- [ ] **Step 1: Write the failing tests** in `src/shortcuts.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use crate::app::ZenohExplorer;
    use crate::types::*;
    use egui::{Key, Modifiers};
    use egui_kittest::Harness;

    fn harness(app: ZenohExplorer) -> Harness<'static, ZenohExplorer> {
        Harness::new_ui_state(
            |ui, app: &mut ZenohExplorer| {
                let ctx = ui.ctx().clone();
                app.handle_shortcuts(&ctx);
                ui.add(egui::TextEdit::singleline(&mut app.tree_filter).id(egui::Id::new(crate::ui::TREE_FILTER_ID)));
                // Stand-ins for the Publish Key and Query Selector fields, drawn with T1's ids.
                ui.add(egui::TextEdit::singleline(&mut app.publish_key).id(egui::Id::new(crate::ui::PUBLISH_KEY_ID)));
                ui.add(egui::TextEdit::singleline(&mut app.query_selector).id(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)));
            },
            app,
        )
    }

    fn focused(h: &Harness<'static, ZenohExplorer>) -> Option<egui::Id> {
        h.ctx.memory(|m| m.focused())
    }

    #[test]
    fn cmd_digit_switches_tab() {
        let (app, _tx) = ZenohExplorer::test_app();
        let mut h = harness(app);
        h.key_press_modifiers(Modifiers::COMMAND, Key::Num2);
        h.run();
        assert_eq!(h.state().detail_view, DetailView::Publish);
        assert_eq!(focused(&h), Some(egui::Id::new(crate::ui::PUBLISH_KEY_ID)), "Cmd+2 moves focus into Publish (F-T19-5)");
        h.key_press_modifiers(Modifiers::COMMAND, Key::Num3);
        h.run();
        assert_eq!(h.state().detail_view, DetailView::Query);
        assert_eq!(focused(&h), Some(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)));
        h.key_press_modifiers(Modifiers::COMMAND, Key::Num1);
        h.run();
        assert_eq!(focused(&h), Some(egui::Id::new(crate::ui::TREE_FILTER_ID)));
        h.key_press_modifiers(Modifiers::COMMAND, Key::Num8);
        h.run();
        assert_eq!(h.state().detail_view, DetailView::Help);
        assert_eq!(focused(&h), None, "a view without a focus target releases focus");
    }

    #[test]
    fn cmd_f_focuses_filter() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.detail_view = DetailView::Help;
        let mut h = harness(app);
        h.key_press_modifiers(Modifiers::COMMAND, Key::F);
        h.run();
        assert_eq!(h.state().detail_view, DetailView::TopicDetails);
        assert_eq!(h.ctx.memory(|m| m.focused()), Some(egui::Id::new(crate::ui::TREE_FILTER_ID)));
    }

    #[test]
    fn esc_dismisses_banner() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.ui_alert = Some(crate::app::UiAlert::Error("Save failed".into()));
        let mut h = harness(app);
        h.key_press(Key::Escape);
        h.run();
        assert!(h.state().ui_alert.is_none());
    }

    #[test]
    fn space_toggles_autoscroll_only_without_focus() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.auto_scroll = true;
        let mut h = harness(app);
        h.key_press(Key::Space);
        h.run();
        assert!(!h.state().auto_scroll);
        h.key_press_modifiers(Modifiers::COMMAND, Key::F); // focus the text field
        h.run();
        h.event(egui::Event::Text(" ".into()));
        h.key_press(Key::Space);
        h.run();
        assert!(!h.state().auto_scroll, "Space typed into a field must not toggle");
    }

    #[test]
    fn cmd_enter_flags_submit_on_action_tabs() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.detail_view = DetailView::Publish;
        let mut h = harness(app);
        h.key_press_modifiers(Modifiers::COMMAND, Key::Enter);
        h.run();
        assert!(h.state().shortcut_submit);
        h.state_mut().shortcut_submit = false;
        h.state_mut().detail_view = DetailView::TopicDetails;
        h.key_press_modifiers(Modifiers::COMMAND, Key::Enter);
        h.run();
        assert!(!h.state().shortcut_submit);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test shortcuts::`. Expected: FAIL (the stub does nothing).
- [ ] **Step 3: Implement** (replace the stub body):

```rust
//! Global keyboard shortcuts (P5 T24).

use egui::{Key, KeyboardShortcut, Modifiers};

use crate::app::ZenohExplorer;
use crate::types::DetailView;

const TAB_KEYS: [Key; 8] = [Key::Num1, Key::Num2, Key::Num3, Key::Num4, Key::Num5, Key::Num6, Key::Num7, Key::Num8];

/// The field a tab shortcut focuses, so the next keystroke lands in the view (F-T19-5).
pub(crate) fn focus_target(view: DetailView) -> Option<egui::Id> {
    match view {
        DetailView::TopicDetails => Some(egui::Id::new(crate::ui::TREE_FILTER_ID)),
        DetailView::Publish => Some(egui::Id::new(crate::ui::PUBLISH_KEY_ID)),
        DetailView::Query => Some(egui::Id::new(crate::ui::QUERY_SELECTOR_ID)),
        _ => None,
    }
}

impl ZenohExplorer {
    /// Consume global shortcuts for this frame. Called before any panel is drawn.
    pub(crate) fn handle_shortcuts(&mut self, ctx: &egui::Context) {
        let cmd = |k: Key| KeyboardShortcut::new(Modifiers::COMMAND, k);
        for (i, key) in TAB_KEYS.iter().enumerate() {
            if ctx.input_mut(|inp| inp.consume_shortcut(&cmd(*key))) {
                let tab = DetailView::TABS[i];
                self.detail_view = tab;
                ctx.memory_mut(|m| match focus_target(tab) {
                    Some(id) => m.request_focus(id),
                    None => {
                        if let Some(id) = m.focused() {
                            m.surrender_focus(id);
                        }
                    }
                });
            }
        }
        if ctx.input_mut(|i| i.consume_shortcut(&cmd(Key::F))) {
            self.detail_view = DetailView::TopicDetails;
            ctx.memory_mut(|m| m.request_focus(egui::Id::new(crate::ui::TREE_FILTER_ID)));
        }
        if ctx.input_mut(|i| i.consume_shortcut(&cmd(Key::Enter)))
            && matches!(self.detail_view, DetailView::Publish | DetailView::Query)
        {
            self.shortcut_submit = true;
        }
        let nothing_focused = ctx.memory(|m| m.focused().is_none());
        if nothing_focused && self.ui_alert.is_some() && ctx.input_mut(|i| i.consume_key(Modifiers::NONE, Key::Escape)) {
            self.ui_alert = None;
        }
        if nothing_focused && ctx.input_mut(|i| i.consume_key(Modifiers::NONE, Key::Space)) {
            self.auto_scroll = !self.auto_scroll;
        }
    }
}
```

- [ ] **Step 4: Update Help.** P1 T26 turned Help into data: `HELP_SECTIONS: &[(&str, &[&str])]`, in screen order, with "Troubleshooting" as the last section. There is no "Performance Tips" block to anchor on any more. Find the anchor with `grep -n '"Troubleshooting"' src/ui/help.rs`, insert two sections **directly before** that entry, and change nothing else in the file. Troubleshooting stays last, which matches the review's recommended order (…, Views, …, Keyboard, Troubleshooting; F-T18-8). P4 T11's "File transfers" entry sits after "Key expressions", so it is not affected.
  - First the failing test, added to P1 T26's `mod tests` in `help.rs`:

```rust
    #[test]
    fn help_lists_keyboard_shortcuts() {
        let (_, lines) = HELP_SECTIONS.iter().find(|(h, _)| *h == "Keyboard shortcuts").expect("Keyboard shortcuts section");
        let t = lines.join("\n");
        for must in ["Ctrl/Cmd+1", "moves focus", "Ctrl/Cmd+F", "Ctrl/Cmd+Enter", "Enter in", "Esc", "Arrow keys"] {
            assert!(t.contains(must), "Help does not mention {must}");
        }
        let pos = |name: &str| HELP_SECTIONS.iter().position(|(h, _)| *h == name);
        assert_eq!(pos("Troubleshooting"), Some(HELP_SECTIONS.len() - 1), "Troubleshooting stays last");
        assert_eq!(pos("Keyboard shortcuts").map(|i| i + 1), pos("Troubleshooting"));
        assert_eq!(pos("Inspection tabs").map(|i| i + 2), pos("Troubleshooting"));
    }
```

  - Then the two entries, inserted before `("Troubleshooting", …)`:

```rust
    (
        "Inspection tabs",
        &[
            "Admin: the router admin space (@/…), which ** subscriptions never show.",
            "Topology: routers, peers and this app, from link state and live transports.",
            "Liveliness: tokens that appear and disappear.",
            "Logs: this app's own log, filterable by level.",
        ],
    ),
    (
        "Keyboard shortcuts",
        &[
            "Ctrl/Cmd+1…8 open Topics, Publish, Query, Admin, Topology, Liveliness, Logs, Help. On Topics, Publish and Query this also moves focus into the view: the topic filter, the Key field, the Selector field.",
            "Enter in the Key field of Subscribe to Topics, in Publish's Key field or in Query's Selector field sends it, like the button next to it; the field keeps focus.",
            "Ctrl/Cmd+F: focus the topic filter (type * or ** for key-expression mode; Enter then subscribes).",
            "Ctrl/Cmd+Enter: Publish or send the Query on those tabs.",
            "Esc: dismiss the banner. Space: pause or resume auto-scroll (when no field is focused).",
            "Arrow keys in the topic tree: Up/Down move, Right expands or enters, Left collapses or goes to the parent.",
        ],
    ),
```

  P1 T26's `help_names_only_real_places` and `help_claims_match_limits` must still pass: the new lines name only real places and make no size claims.

- [ ] **Step 5: Run the tests.** Run `cargo test -- shortcuts:: ui::help`. Expected: 9 passed. That is the 5 tests in `shortcuts::ui_tests`, and 4 in `ui::help::tests`: `help_lists_keyboard_shortcuts`, P1 T26's `help_names_only_real_places` and `help_claims_match_limits`, and P4 T11's `help_points_large_files_to_transfers`.
  - If `space_toggles_autoscroll_only_without_focus` fails on its second half, the focus request takes effect a frame later. Add one more `h.run()` after the Cmd+F press.
- [ ] **Step 6: Commit.**

```bash
git add src/shortcuts.rs src/ui/help.rs
git commit -m "feat(ui): global keyboard shortcuts that move focus into the view, and Help sections for them

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T25: In-app log capture (Lane LOG)

**Owns:** `src/logs.rs`, `src/main.rs`

**Interfaces:**
- Produces:
  - `LOG_CAPACITY = 5_000`.
  - `LogLine { at, level: tracing::Level, target, message }`.
  - `LogBuffer` (a cheap clone holding `Arc<Mutex<VecDeque<LogLine>>>`), with `push`, `len`, `is_empty`, `clear` and `filtered(max_level, needle) -> Vec<LogLine>`.
  - `RingLayer::new(LogBuffer)`, which implements `tracing_subscriber::Layer`.
  - `fn global() -> &'static LogBuffer`.
  - `fn init_tracing()`.
- **Why:** the release build on Windows has `windows_subsystem = "windows"` (pre-P1 `main.rs:2`), so it has no console and logs were invisible. The ring buffer feeds the Logs tab (T26).
- APIs:
  - `tracing_subscriber::registry().with(EnvFilter).with(fmt::layer()).with(RingLayer)` and `Layer::on_event` (tracing-subscriber 0.3.23; https://docs.rs/tracing-subscriber/0.3/tracing_subscriber/layer/trait.Layer.html).
  - `tracing::field::Visit::record_debug` / `record_str`.
  - `Level` ordering: more verbose is greater, so `ERROR < WARN < … < TRACE` (tracing-core 0.1.36 `metadata.rs:106-107`).

- [ ] **Step 1: Write the failing tests** in `src/logs.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use tracing_subscriber::layer::SubscriberExt;

    #[test]
    fn layer_captures_message_and_fields() {
        let buf = LogBuffer::default();
        let sub = tracing_subscriber::registry().with(RingLayer::new(buf.clone()));
        tracing::subscriber::with_default(sub, || {
            tracing::warn!(peer = 7, "hello {}", "world");
            tracing::debug!(target: "zenoh::net", "dbg");
        });
        let all = buf.filtered(Level::TRACE, "");
        assert_eq!(all.len(), 2);
        assert_eq!((all[0].level, all[0].message.as_str()), (Level::WARN, "hello world peer=7"));
        assert_eq!(all[1].target, "zenoh::net");
        assert_eq!(buf.filtered(Level::INFO, "").len(), 1, "INFO shows WARN but not DEBUG");
        assert_eq!(buf.filtered(Level::TRACE, "ZENOH").len(), 1, "needle matches target, case-insensitive");
    }

    #[test]
    fn ring_keeps_last_5000() {
        let buf = LogBuffer::default();
        for i in 0..(LOG_CAPACITY + 3) {
            buf.push(LogLine { at: chrono::Utc::now(), level: Level::INFO, target: "t".into(), message: i.to_string() });
        }
        assert_eq!(buf.len(), LOG_CAPACITY);
        assert_eq!(buf.filtered(Level::TRACE, "")[0].message, "3");
        buf.clear();
        assert!(buf.is_empty());
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test logs::`. Expected: compile errors.
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! In-app log capture: a tracing `Layer` feeding a bounded ring buffer that
//! the Logs tab reads (P5 T25). Needed because release builds on Windows have
//! no console.

use std::collections::VecDeque;
use std::fmt::Write as _;
use std::sync::{Arc, Mutex, OnceLock};

use chrono::{DateTime, Utc};
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::{Context, Layer};

pub const LOG_CAPACITY: usize = 5_000;

#[derive(Debug, Clone, PartialEq)]
pub struct LogLine {
    pub at: DateTime<Utc>,
    pub level: Level,
    pub target: String,
    pub message: String,
}

/// Shared, bounded log store (cloning shares the same buffer).
#[derive(Debug, Clone, Default)]
pub struct LogBuffer {
    inner: Arc<Mutex<VecDeque<LogLine>>>,
}

impl LogBuffer {
    pub fn push(&self, line: LogLine) {
        if let Ok(mut q) = self.inner.lock() {
            if q.len() == LOG_CAPACITY {
                q.pop_front();
            }
            q.push_back(line);
        }
    }

    pub fn len(&self) -> usize {
        self.inner.lock().map_or(0, |q| q.len())
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn clear(&self) {
        if let Ok(mut q) = self.inner.lock() {
            q.clear();
        }
    }

    /// Lines at `max_level` or more severe whose message or target contains
    /// `needle` (case-insensitive), oldest first.
    pub fn filtered(&self, max_level: Level, needle: &str) -> Vec<LogLine> {
        let n = needle.to_lowercase();
        self.inner.lock().map_or_else(
            |_| Vec::new(),
            |q| {
                q.iter()
                    .filter(|l| l.level <= max_level)
                    .filter(|l| n.is_empty() || l.message.to_lowercase().contains(&n) || l.target.to_lowercase().contains(&n))
                    .cloned()
                    .collect()
            },
        )
    }
}

#[derive(Default)]
struct MessageVisitor {
    message: String,
    fields: String,
}

impl Visit for MessageVisitor {
    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.message.push_str(value);
        } else {
            let _ = write!(self.fields, " {}={}", field.name(), value);
        }
    }

    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.message, "{value:?}");
        } else {
            let _ = write!(self.fields, " {}={:?}", field.name(), value);
        }
    }
}

/// `tracing` layer that copies every enabled event into a `LogBuffer`.
pub struct RingLayer {
    buf: LogBuffer,
}

impl RingLayer {
    pub fn new(buf: LogBuffer) -> Self {
        Self { buf }
    }
}

impl<S: Subscriber> Layer<S> for RingLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let mut v = MessageVisitor::default();
        event.record(&mut v);
        let meta = event.metadata();
        self.buf.push(LogLine {
            at: Utc::now(),
            level: *meta.level(),
            target: meta.target().to_string(),
            message: format!("{}{}", v.message, v.fields),
        });
    }
}

/// The process-wide buffer the Logs tab shows.
pub fn global() -> &'static LogBuffer {
    static GLOBAL: OnceLock<LogBuffer> = OnceLock::new();
    GLOBAL.get_or_init(LogBuffer::default)
}

/// Console output plus the in-app ring, both behind the same filter
/// (`RUST_LOG`, default `info,zenoh=warn` as set by P1).
pub fn init_tracing() {
    use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info,zenoh=warn"));
    tracing_subscriber::registry()
        .with(filter)
        .with(tracing_subscriber::fmt::layer())
        .with(RingLayer::new(global().clone()))
        .init();
}
```

- [ ] **Step 4: Use it in `src/main.rs`.** Replace P1's `tracing_subscriber::fmt().with_env_filter(…).init();` block with `logs::init_tracing();`.
- [ ] **Step 5: Run the tests.** Run `cargo test logs:: && cargo run`. Expected: 2 passed, and the console still prints info lines on start.
- [ ] **Step 6: Commit.**

```bash
git add src/logs.rs src/main.rs
git commit -m "feat(logs): tracing layer into a 5000-line in-app ring buffer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T26: Logs tab (Lane LOG)

**Owns:** `src/ui/logs.rs`

**Interfaces:**
- Consumes: T25's `logs::{global, LogBuffer, LogLine}`.
- Produces `LogsState { buffer, max_level, filter, follow }`. `buffer` defaults to `logs::global().clone()`; tests swap in a private buffer.
- egui APIs: `ScrollArea::stick_to_bottom` plus `show_rows`, `Context::copy_text`, and `ComboBox` (0.36.2).

- [ ] **Step 1: Write the failing tests** at the bottom of `src/ui/logs.rs`:

```rust
#[cfg(test)]
mod ui_tests {
    use super::*;
    use crate::logs::{LogBuffer, LogLine};
    use egui_kittest::{kittest::Queryable, Harness};

    fn app_with_lines() -> ZenohExplorer {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let buf = LogBuffer::default();
        for (level, msg) in [(tracing::Level::ERROR, "boom happened"), (tracing::Level::DEBUG, "noise here")] {
            buf.push(LogLine { at: chrono::Utc::now(), level, target: "zenoh_explorer".into(), message: msg.into() });
        }
        app.logs.buffer = buf;
        app
    }

    #[test]
    fn logs_tab_filters_by_level() {
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_logs_tab(ui), app_with_lines());
        h.run();
        h.get_by_label_contains("boom happened");
        assert!(h.query_by_label_contains("noise here").is_none());
        h.get_by_label("1 of 2 lines");
        h.get_by_value("Info").click();
        h.run();
        h.get_by_label("Debug").click();
        h.run();
        h.get_by_label_contains("noise here");
        h.get_by_label("2 of 2 lines");
    }

    #[test]
    fn logs_copy_visible() {
        let mut h = Harness::new_ui_state(|ui, app: &mut ZenohExplorer| app.show_logs_tab(ui), app_with_lines());
        h.run();
        h.get_by_label("Copy visible").click();
        h.step();
        let copied = h.output().platform_output.commands.iter().any(|c| {
            matches!(c, egui::OutputCommand::CopyText(t) if t.contains("boom happened") && !t.contains("noise"))
        });
        assert!(copied);
    }
}
```

- [ ] **Step 2: Run the tests to confirm they fail.** Run `cargo test ui::logs`. Expected: compile errors (the `LogsState` fields are missing).
- [ ] **Step 3: Implement** (replace the stub):

```rust
//! Logs tab: this app's own tracing output, filterable (P5 T26).

use egui::RichText;
use tracing::Level;

use crate::app::ZenohExplorer;
use crate::logs::{LogBuffer, LogLine};

const LEVELS: [(Level, &str); 5] =
    [(Level::ERROR, "Error"), (Level::WARN, "Warn"), (Level::INFO, "Info"), (Level::DEBUG, "Debug"), (Level::TRACE, "Trace")];

#[derive(Debug)]
pub struct LogsState {
    pub buffer: LogBuffer,
    pub max_level: Level,
    pub filter: String,
    pub follow: bool,
}

impl Default for LogsState {
    fn default() -> Self {
        Self { buffer: crate::logs::global().clone(), max_level: Level::INFO, filter: String::new(), follow: true }
    }
}

fn line_text(l: &LogLine) -> String {
    // Local time, like every other list (F-T14-6).
    let at = crate::types::format_local_time(&l.at, &chrono::Utc::now());
    format!("{at} {:<5} {}: {}", l.level.as_str(), l.target, l.message)
}

pub trait LogsUI {
    fn show_logs_tab(&mut self, ui: &mut egui::Ui);
}

impl LogsUI for ZenohExplorer {
    fn show_logs_tab(&mut self, ui: &mut egui::Ui) {
        let lines = self.logs.buffer.filtered(self.logs.max_level, &self.logs.filter);
        ui.horizontal_wrapped(|ui| {
            ui.label("Level:");
            let current = LEVELS.iter().find(|(l, _)| *l == self.logs.max_level).map_or("Info", |(_, n)| n);
            egui::ComboBox::from_id_salt("log_level").selected_text(current).show_ui(ui, |ui| {
                for (level, name) in LEVELS {
                    ui.selectable_value(&mut self.logs.max_level, level, name);
                }
            });
            ui.label("Filter:");
            ui.text_edit_singleline(&mut self.logs.filter);
            ui.checkbox(&mut self.logs.follow, "Follow");
            if ui.button("Clear").clicked() {
                self.logs.buffer.clear();
            }
            if ui.button("Copy visible").clicked() {
                ui.ctx().copy_text(lines.iter().map(line_text).collect::<Vec<_>>().join("\n"));
            }
            ui.label(format!("{} of {} lines", lines.len(), self.logs.buffer.len()));
        });
        ui.separator();
        let row_h = ui.text_style_height(&egui::TextStyle::Monospace);
        egui::ScrollArea::vertical()
            .id_salt("logs_scroll")
            .auto_shrink([false; 2])
            .stick_to_bottom(self.logs.follow)
            .show_rows(ui, row_h, lines.len(), |ui, range| {
                for l in &lines[range] {
                    ui.label(RichText::new(line_text(l)).monospace());
                }
            });
        if self.logs.follow {
            ui.ctx().request_repaint_after(std::time::Duration::from_millis(500));
        }
    }
}
```

  The 500 ms repaint while following keeps new lines visible, because P1 repaints on worker events only and log lines are not worker events. It applies only while the Logs tab is open.
- [ ] **Step 4: Run the tests.** Run `cargo test ui::logs`. Expected: 2 passed. If `logs_copy_visible` sees no command after one `step()`, use two `step()` calls (see T18).
- [ ] **Step 5: Commit.**

```bash
git add src/ui/logs.rs
git commit -m "feat(ui): Logs tab with level/text filter, follow, clear and copy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task T27: Integration verification

**Owns:** no source files. If a check fails, open a fix task on the owning lane instead of editing here.

- [ ] **Step 1: Full suite.**

```bash
cargo build && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --all -- --check
```

Expected: every command passes. The test count equals the P1–P4 total plus P5's new tests; list them with `cargo test 2>&1 | grep 'test result'`.
- [ ] **Step 2: Network loopback tests.**

```bash
cargo test -- --ignored connectivity_reports_peer_transport publish_qos_crosses_the_wire matching_flips_when_subscriber_appears file_mode_monitor_sees_third_party_samples
```

Expected: 4 passed. Ports 27611, 27631, 27641 and 27651 must be free.
- [ ] **Step 3: Router tests.** Start zenohd as in T2 Step 4 (7447 only is enough), then run:

```bash
cargo test -- --ignored admin_query_against_zenohd
```

Expected: 1 passed.
- [ ] **Step 4: Hygiene.**

```bash
cargo audit
scripts/bin/bearhug-arch status
git grep -n 'todo!\|unimplemented!' src
git grep -nE 'Color32::(from_rgb|from_rgba|[A-Z_]+\b)' src/ui/{admin,topology,liveliness,logs,payload_viewer,connection,attachment_editor}.rs src/{filter,tree_nav,rates,shortcuts}.rs
```

Expected:
- `cargo audit` shows only the ignores recorded in `.cargo/audit.toml`.
- `bearhug-arch` reports documentation `covered`. Check that the README "Source layout" lines from T1 still describe each module accurately.
- Both greps print nothing, so the new UI adds no colour of its own.
- [ ] **Step 5: End-to-end smoke** (two routers from T2 plus the app in client mode on 7447). Tick each item in the evidence:
  1. **Admin:** Refresh lists two routers; linkstate shows DOT; a token entry appears after a peer declares one.
  2. **Topology:** three nodes (two squares and ◎) joined by solid and thick lines. Killing router 7448 removes it within 5 s.
  3. **Liveliness:** Start on `**`, then `z_liveliness -k demo/alive` appears as "● alive". Stopping it shows "○ gone", with changes = 2.
  4. **Publish:** with a subscriber on `demo/**` from another client, the indicator says "● subscribers present". Priority DataHigh plus a key/value attachment arrive, as seen in the other client or in this app's viewer "QoS:" line. Delete mode produces "Last sample: DELETE".
  5. **Query:** target All, `_time` preset "Last hour" and Accept replies on any key send one run. A storage or queryable replies. Cancel on a slow query marks the run "cancelled". Disconnect during a slow query marks it "cancelled: disconnected" and the Cancel button goes away. After reconnecting, runs from before the disconnect read "· earlier session". Enter in Selector sends a run. Run and reply times are local wall-clock time. A reply from this app's own queryable is local; one from a second Explorer on another machine is not. Clear #n removes it.
  6. **Viewer:** JSON, Hex, CBOR (publish `a1 61 61 01` as `application/cbor`) and Image (a PNG) tabs; Copy works; Load full payload shows "(full)".
  7. **Tree:** `demo/*/temp` shows the KE indicator and filters by intersection, and Enter subscribes. Enter in Subscribe Key subscribes. Arrow keys walk and expand. Select a leaf found by the filter, clear the filter, and the leaf is still visible and selected. Rates show `n/s` and `↻` for busy topics. A topic whose publisher stopped shows "Last message … (N min ago) · no messages in the last 2 s" on its page.
  8. **Profiles:** a saved profile survives a restart. A TOML import connects. `pigeon/x:1` disables Connect.
  9. **Shortcuts:** Cmd/Ctrl+1..8, F, Enter, Esc and Space behave as the Help tab lists them. Help shows "Keyboard shortcuts" just above "Troubleshooting", which is still the last section. Cmd/Ctrl+2 then typing puts the text in the Publish Key field, and Enter there publishes. A second Enter while "Publishing…" is shown sends nothing.
  10. **Logs:** Error/Info/Debug filter; Copy visible; a release build on Windows shows log lines in the Logs tab. Log, Admin "refreshed" and Liveliness times are local wall-clock time.
- [ ] **Step 6: Record.** Paste the outputs of Steps 1–4 and the ticked checklist into the Bear Hug evidence for T27. No commit is needed, because there are no file changes.
