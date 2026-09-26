# T1 evidence: Zenoh Explorer baseline captures (2026-09-25)

**Build under test:** commit in `baseline-commit.txt`, debug build, `cargo run` equivalent (`target/debug/zenoh-explorer`, `RUST_LOG=warn`).

**Checks:**
- `cargo-test.txt`: `cargo test` output tail plus exit code.
- `cargo-clippy.txt`: `cargo clippy -- -D warnings`, full output plus exit code (forced re-check via `touch src/main.rs`).

**How captures were taken:**
- macOS `screencapture -x -o -l <windowid>` captured the app window only, at 2× Retina.
- Window sizes are logical content sizes; the title bar adds 28 pt. They were set through the Accessibility API.
- Clicks were real `CGEvent`s. The helper is `win-helper.swift`, and `drive.sh` holds the click/capture functions. Coordinates are window points.
- Themes were switched with the app's own ☀/🌙 header toggle.

**Connected state:**
- The explorer ran in **peer mode with multicast discovery** (no address; listen port 7447) against `zpub-traffic-generator.rs`.
- `zpub-traffic-generator.rs` is a throwaway zenoh 1.x peer built in the scratchpad, outside this repo. Every 500 ms it publishes `demo/sensors/{temp1,humidity}`, `demo/robot/status` (JSON), `demo/logs/app` and `demo/bin/blob`. On its first 3 ticks it also sends chunk keys `demo/files/report/__chunk/<4·64MiB+1000>/5/<0..2>` (16-byte payloads, attachment `report.pdf`) to produce an in-progress transfer.
- `zenohd` is not installed on this machine, so no router was used.
- The explorer's automatic `**` monitor session showed **no traffic**. Topics appeared only after an explicit **Subscribe `demo/**`**.
- After subscribing, the generator was **restarted once** so its chunk keys (sent on ticks 0–2 only) arrived after the subscription. Message lists therefore mix ticks from both runs.

**Reproducing:**
- Build `zpub-traffic-generator.rs` as `src/main.rs` of a scratch crate with `zenoh = { version = "1.0", features = ["unstable"] }` and `tokio = { version = "1", features = ["full"] }` (this run resolved zenoh 1.7.2 from the repo's `Cargo.lock`).
- Compile the helper with `swiftc -O win-helper.swift -o win`.
- In `drive.sh`, set `S` to the folder holding `win`. Write the explorer's PID to `$S/explorer.pid` when launching it.
- Coordinates in `drive.sh` calls are window points and depend on layout state. The click sequence was run interactively and is described in the review doc's T1 section; it is not committed as a script.

| File | View | Theme | Size | Connection |
|---|---|---|---|---|
| [dark-1000-01-disconnected-panel.png](dark-1000-01-disconnected-panel.png) | Disconnected connection panel (after Disconnect; tree and messages retained) | dark | 1000×600 | disconnected |
| [dark-1000-02-topics-all-messages.png](dark-1000-02-topics-all-messages.png) | Topics: tree with `demo/**` subscription + All Messages list | dark | 1000×600 | connected |
| [dark-1000-03-topic-details-leaf.png](dark-1000-03-topic-details-leaf.png) | Topic details for leaf `demo/sensors/temp1` | dark | 1000×600 | connected |
| [dark-1000-04-alert-banner.png](dark-1000-04-alert-banner.png) | Alert banner after Save File, showing the full path `~/Desktop/demo_sensors_temp1.bin` (the 4-byte file was deleted afterwards) | dark | 1000×600 | connected |
| [dark-1000-05-transfer-details.png](dark-1000-05-transfer-details.png) | Active transfer: `demo/files/report` 3/5 chunks, details panel | dark | 1000×600 | connected |
| [dark-1000-06-publish.png](dark-1000-06-publish.png) | Publish tab | dark | 1000×600 | connected |
| [dark-1000-07-query.png](dark-1000-07-query.png) | Query tab | dark | 1000×600 | connected |
| [dark-1000-08-help.png](dark-1000-08-help.png) | Help tab | dark | 1000×600 | connected |
| [dark-1400-01-disconnected-panel.png](dark-1400-01-disconnected-panel.png) | Disconnected connection panel (after Disconnect; tree and messages retained) | dark | 1400×900 | disconnected |
| [dark-1400-02-topics-all-messages.png](dark-1400-02-topics-all-messages.png) | Topics: tree with `demo/**` subscription + All Messages list | dark | 1400×900 | connected |
| [dark-1400-03-topic-details-leaf.png](dark-1400-03-topic-details-leaf.png) | Topic details for leaf `demo/sensors/temp1` | dark | 1400×900 | connected |
| [dark-1400-04-alert-banner.png](dark-1400-04-alert-banner.png) | Alert banner after Save File, showing the full path `~/Desktop/demo_sensors_temp1.bin` (the 4-byte file was deleted afterwards) | dark | 1400×900 | connected |
| [dark-1400-05-transfer-details.png](dark-1400-05-transfer-details.png) | Active transfer: `demo/files/report` 3/5 chunks, details panel | dark | 1400×900 | connected |
| [dark-1400-06-publish.png](dark-1400-06-publish.png) | Publish tab | dark | 1400×900 | connected |
| [dark-1400-07-query.png](dark-1400-07-query.png) | Query tab | dark | 1400×900 | connected |
| [dark-1400-08-help.png](dark-1400-08-help.png) | Help tab | dark | 1400×900 | connected |
| [light-1000-01-disconnected-panel.png](light-1000-01-disconnected-panel.png) | Disconnected connection panel (after Disconnect; tree and messages retained) | light | 1000×600 | disconnected |
| [light-1000-02-topics-all-messages.png](light-1000-02-topics-all-messages.png) | Topics: tree with `demo/**` subscription + All Messages list | light | 1000×600 | connected |
| [light-1000-03-topic-details-leaf.png](light-1000-03-topic-details-leaf.png) | Topic details for leaf `demo/sensors/temp1` | light | 1000×600 | connected |
| [light-1000-04-alert-banner.png](light-1000-04-alert-banner.png) | Alert banner after Save File, showing the full path `~/Desktop/demo_sensors_temp1.bin` (the 4-byte file was deleted afterwards) | light | 1000×600 | connected |
| [light-1000-05-transfer-details.png](light-1000-05-transfer-details.png) | Active transfer: `demo/files/report` 3/5 chunks, details panel | light | 1000×600 | connected |
| [light-1000-06-publish.png](light-1000-06-publish.png) | Publish tab | light | 1000×600 | connected |
| [light-1000-07-query.png](light-1000-07-query.png) | Query tab | light | 1000×600 | connected |
| [light-1000-08-help.png](light-1000-08-help.png) | Help tab | light | 1000×600 | connected |
| [light-1400-01-disconnected-panel.png](light-1400-01-disconnected-panel.png) | Disconnected connection panel (after Disconnect; tree and messages retained) | light | 1400×900 | disconnected |
| [light-1400-02-topics-all-messages.png](light-1400-02-topics-all-messages.png) | Topics: tree with `demo/**` subscription + All Messages list | light | 1400×900 | connected |
| [light-1400-03-topic-details-leaf.png](light-1400-03-topic-details-leaf.png) | Topic details for leaf `demo/sensors/temp1` | light | 1400×900 | connected |
| [light-1400-04-alert-banner.png](light-1400-04-alert-banner.png) | Alert banner after Save File, showing the full path `~/Desktop/demo_sensors_temp1.bin` (the 4-byte file was deleted afterwards) | light | 1400×900 | connected |
| [light-1400-05-transfer-details.png](light-1400-05-transfer-details.png) | Active transfer: `demo/files/report` 3/5 chunks, details panel | light | 1400×900 | connected |
| [light-1400-06-publish.png](light-1400-06-publish.png) | Publish tab | light | 1400×900 | connected |
| [light-1400-07-query.png](light-1400-07-query.png) | Query tab | light | 1400×900 | connected |
| [light-1400-08-help.png](light-1400-08-help.png) | Help tab | light | 1400×900 | connected |
