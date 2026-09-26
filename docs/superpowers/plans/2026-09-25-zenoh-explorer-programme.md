# Zenoh Explorer programme: plans P1–P5 from the 2026-09-25 deep review

**Source review:** `docs/superpowers/reviews/2026-09-25-zenoh-explorer-deep-review.md`, which lists findings R1–R19 and the items deferred to later plans.

**How to read this:** Bear Hug accepts one plan at a time, so the plans run **in order**. Within each plan, tasks are grouped into **lanes** that own separate files. Once a plan's Wave 0 lands, its lanes can run at the same time. Every plan has a file-ownership matrix showing that no two tasks that can run together edit the same file.

| # | Plan | Tasks | Peak parallel | Kind of change | Depends on |
|---|---|---|---|---|---|
| P1 | [Correctness and hardening](2026-09-25-correctness-and-hardening.md) | 26 | 9 | Correctness and robustness fixes (R1–R19). Starts with a module split. | — |
| P2 | [CI and release hardening](2026-09-25-p2-ci-release-hardening.md) | 14 | 7 | Workflows (SHA pins, permissions, matrix, MSRV, signing, provenance, symbols), plus release docs | P1 (MSRV 1.88, audit job) |
| P3 | [egui 0.36 / rfd 0.17 port](2026-09-25-p3-egui-036-port.md) | 17 | 5 | UI platform upgrade. Adds kittest, virtualised tree, persistence, async file jobs and accessibility. | P1 layout, P2 CI jobs (MSRV → 1.95) |
| P4 | [Transfer protocol v2](2026-09-25-p4-transfer-protocol-v2.md) | 13 | 4 | Pull-based `@xfer` transfer: manifest, BLAKE3, Querier, spool to disk, liveliness | P1, P3 (async dialogs, kittest) |
| P5 | [Explorer features](2026-09-25-p5-explorer-features.md) | 27 | 15 | Admin space, topology, liveliness, QoS/query options, payload viewer, key-expression filter, profiles, shortcuts, logs, rates | P1–P4 |

**Peak parallel** is the widest wave: the largest number of tasks whose dependencies are all met at the same step. For P2 that is T1 together with T4–T9. A task started as soon as its own dependencies finish can overlap tasks from a later wave, so the dependency graphs allow at most 10 (P1), 8 (P2), 5 (P3), 6 (P4) and 16 (P5) tasks at once. For P1, one such set is T1, T4, T6, T7, T8, T10, T11, T14, T16 and T18.

**Shared rules across all five plans:**
- A task edits only the files in its **Owns** list.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Colour values belong to the Snow White UI plan. P3 may change how the theme is installed but keeps the current colour values.
- P1–P5 now also carry the behaviour findings of the UI/UX review (`docs/superpowers/reviews/2026-09-24-ui-ux-snow-white-review.md`). The routing of those findings to P-plans is the "P-routing" table in the review doc's T21 section. P1's committed digest (`28eadfb8…`) is superseded, so all five plans (P1–P5) need a fresh digest review before `bearhug-work accept` (P2's changes are its T10 Done-when, whose pipes are escaped so Bear Hug's parser reads its table, and its T8 Done-when, reworded as prose).
- After P1 T21, `ui_alert` is `Option<UiAlert>` (`Success`, `Warning` or `Error`, read through `UiAlert::text`). Later plans wrap banner text in a variant, such as `Some(UiAlert::Error(format!(…)))`, never `Some(String)`. Tests read it with `app.ui_alert.as_ref().map(UiAlert::text)` or `matches!`.

**Board and acceptance:**
- Each plan gets its own board row when it is accepted. BOARD.md has no P1–P5 rows yet.
- `bearhug-work accept` is refused while `2026-09-24-ui-ux-snow-white-review.md` is the active plan. Accept P1 by its digest once that plan is finished, then P2–P5 in order.

**Open questions from the plan authors (answer before accepting the relevant plan):**

- **P1 – monitor route (UI review Q7):** P1 T10 already assumes an answer. It runs the `**` monitor as a zenoh client that dials the publishing session's own listener in peer mode, or the same routers in client mode (zenoh 1.7.2 does not forward one peer's samples to another peer), and adds the ignored test `monitor_sees_third_party_samples`, proven on zenoh 1.7.2 and 1.10.1. P5 T22 does the same for a config file. Confirm that approach. Or choose an honest "monitor: off" state that tells users to subscribe, and reword P1 T26's Help and P1 T24's empty-tree hint to match (F-T20-7). The other cross-plan rulings the UI review asks for before the P-plans are accepted are in T21's "Corrections and cross-plan notes" (review doc, section T21).

- **P2 – repository URL:** is `https://github.com/zenoh-project/zenoh-explorer` the real repository? (The repo has no git remote.)
- **P2 – signing:** do you have, or want, an Apple Developer ID and an Azure Artifact Signing account? Without them, releases ship unsigned with warnings.
- **P2 – Linux debug symbols:** if no `.dwp` file is produced, extract symbols with `objcopy` or skip Linux symbols?
- **P3 – order:** should P3 land before Snow White's implementation plan, so that plan targets egui 0.36?
- **P3 – snapshot reference platform:** macOS (the proposal) or Linux only?
- **P3 – saved subscriptions:** re-subscribe automatically on connect (the proposal), or restore them as suggestions only?
- **P4 – v1 `__chunk` receive:** remove it (recommended, Option B) or keep it read-only for one release (Option A)?
- **P4 – fetch policy:** fetch only on click (recommended), or auto-fetch below a size threshold?
- **P4 – spool:** OS temp directory, 16 GiB cap and 24 h cleanup (recommended), or a per-user cache directory?
- **P5 – image preview:** re-adding `egui_extras` (image feature only) is needed for PNG/JPEG. Accept that, or defer image preview to P6?
- **P5 – "subscribers present" indicator:** it also counts this app's own subscriptions. Is that acceptable?
- **P5 – connection profiles:** store them in a separate `profiles.json` (proposed), or inside P3's `Settings`?
