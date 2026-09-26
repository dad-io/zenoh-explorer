# Zenoh Explorer: code review of correctness, robustness and currency (2026-09-25)

**Scope:** every file under `src/`, plus `Cargo.toml`, `.github/workflows/*.yml` and `README.md`. Each file was read in full by its own reviewer agent. The main findings were then re-checked against the code by the coordinating session.

**Baseline:**
- `cargo build`, `cargo test` (30 passed), `cargo clippy --all-targets -- -D warnings` and `cargo fmt --check` are all clean on macOS with rustc 1.94.0.
- `cargo audit` reports **12 vulnerabilities**.

**Versions (checked 2026-09-25 via crates.io):**

| Crate | Locked | Latest | Notes |
|---|---|---|---|
| zenoh | 1.7.2 | 1.10.1 | MSRV 1.75 for both. |
| egui / eframe | 0.29.1 | 0.36.2 | MSRV 1.95; upgrading is a real port. |
| rfd | 0.14.1 | 0.17.2 | |

Tracing: `fmt::init()` with `env-filter` defaults to the ERROR level. `info!` is therefore silent unless `RUST_LOG` is set, which was verified in the tracing-subscriber 0.3.23 source.

## Findings this plan fixes (IDs referenced by the plan)

| ID | Sev | Location | Finding |
|---|---|---|---|
| R1 | High | `Cargo.lock` | 12 RUSTSEC advisories: rustls, rustls-webpki, quinn-proto (remote memory exhaustion), lz4_flex, crossbeam-epoch, webbrowser, quick-xml (build-time only), and rsa (no fix). A full `cargo update` leaves 2 (rsa, lz4_flex); zenoh 1.10.1 compiles and passes the tests. |
| R2 | Med | `Cargo.toml` | `egui_extras`, `serde` and `anyhow` are unused. `tokio = full` pulls in more than needed. There is no `rust-version`, although the lock needs ≥ 1.88. `zenoh = "1.0"` sits below the tested version. eframe lacks `wayland`/`x11`/`accesskit`. |
| R3 | High | `zenoh_worker.rs:222-235, 380-396, 691-718` | Each sample is fully copied twice: `to_vec` plus a full `try_to_string`. The binary-preview code is duplicated four times. |
| R4 | High | `zenoh_worker.rs:246, 407, 724, 247, 408, 725` | Encoding is hard-coded to `"text/plain"` and the time is always `Utc::now()`. `sample.kind()` is never read, so a DELETE looks like an empty PUT. |
| R5 | High | `zenoh_worker.rs:462-503, 812-859` | The queryable store saves the 256-byte display preview, not the payload, so queriers get truncated or hex text. Its hand-written wildcard matcher is wrong: `demo/**` matches `demonstration/x`. |
| R6 | High | `zenoh_worker.rs:932-940, 1158-1165` | `max_message_size` is raised to 100 GB on both sessions, so any peer can make the explorer reassemble huge messages in memory. |
| R7 | Med | `zenoh_worker.rs:187, 1051-1057, 1199-1206` | `listen_port + 1000` overflows `u16`. `listen_endpoint.parse().unwrap()` and config `unwrap()`s panic the worker on bad input. |
| R8 | High | `transfer.rs:160-219, 225-229` | Export allocates `total_size` as claimed by a remote peer before checking the real chunk lengths. The transmitted filename is used without sanitising. |
| R9 | Med | `zenoh_worker.rs:106`, `types.rs:269`, `events.rs:79` | Derived `Debug` prints whole payloads, which is multi-GB for large publishes when `RUST_LOG=info`/`debug`. Hot paths log at `info!`. |
| R10 | Med | `zenoh_worker.rs:447, 555-606, 734-757, 871-873, 273, 289-297` | Failures in subscribe, publish, query, queryable and the monitor are only logged. A bad selector leaves "Waiting for responses…" on screen forever, and error replies are reported as "no queryables". |
| R11 | Med | `ui/publish.rs`, `ui/query.rs`, `ui/topic_tree.rs` | Key expressions and selectors are not validated before sending. The typed payload is cleared even when the publish fails. A bad timeout silently becomes 10 s. |
| R12 | High | `app.rs:91-93`, `zenoh_worker.rs:12-70` | All channels are unbounded. The buffer thread wakes every 1 ms when idle. `process_events` drains the whole backlog in one frame. |
| R13 | High | `zenoh_worker.rs:108-182, 313-345` | Each Connect starts a discovery thread plus runtime that is never stopped. A second Connect leaks the old sessions. The queryable task survives Disconnect. The UI shows "Connecting…" forever if the send fails. |
| R14 | High | `app.rs:716` | The app repaints every 66 ms forever, even when idle. |
| R15 | High | `events.rs:186-210`, `app.rs:160` | Dedup hashes key + payload over 60 s, so repeated identical values from one publisher are dropped. |
| R16 | High | `events.rs:204-207` | The rate limiter drops messages before the tree and store update, although the code comment says nothing is lost. The local-wins query-reply path skips memory accounting. |
| R17 | Med | `events.rs:14-25, 63-66` | The JSON cache key hashes only the first 4 KB, so two payloads with the same 4 KB prefix show the wrong pretty-print. The cache is cleared wholesale. |
| R18 | High | `ui/topic_tree.rs:228-232, 534-541`, `app.rs:147` | The whole tree is deep-cloned every frame. Topic history scans up to 1 M messages per frame. The filter is recomputed on every message. The default `max_messages` is 1,000,000, but the UI clamps it to 50,000. |
| R19 | Med | `transfer.rs:69-105`, `events.rs:283` | The export store has no byte budget, and incomplete transfers are never garbage-collected. |

## Deferred to later plans (not in this plan)

- Upgrades to egui 0.36 and rfd 0.17.
- File-transfer protocol redesign: manifest, transfer ID, BLAKE3, Querier pull.
- Tree virtualization (`show_rows`), key-expression filter mode, accessibility, and theme contrast (overlaps the Snow White UI review plan).
- New explorer features: admin space, topology, liveliness, QoS controls, decoders.
- CI and release hardening: SHA pins, permissions, signing, the `release.yml:115` injection, the test matrix.
- README corrections.
