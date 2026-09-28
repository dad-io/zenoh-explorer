# Snow White UI Implementation Plan (SW)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. T1 runs two parallel agents and then one ("How T1 runs"); T2 runs ten parallel agents in two batches in git worktrees ("How T2 runs"); T3 runs one agent ("How T3 runs"). Steps use checkbox (`- [ ]`) syntax for tracking.

This plan turns the five candidate plans of the Snow White UI/UX review (CP-A1 keyboard focus and hit targets, CP-A2 words and glyphs, CP-A3 landmark stability, CP-B visual theme, CP-C causal motion) into three board tasks. The review sketched 66 rows; they are regrouped by **file ownership**, because nearly every row edits one of four files (`src/app/layout.rs`, `src/ui/topic_tree.rs`, `src/app/theme.rs`, `src/colors.rs`) and rows split by review task could never run in parallel. **T1** builds the shared foundation (theme core, motion core, then a structural split of the big files plus a headless probe). **T2** applies every visible change in ten parallel parts, one per file group. **T3** removes the old API, runs the integrated checks and measures motion in the real app. Every review row survives as a part, stage or step whose heading says "(was CP-X Tn)".

**Map from review rows to this plan.** "Dissolved" means the row's work is done inside the parts that own its lines; its check moves to T3.

| Review row | New place | File(s) |
|---|---|---|
| CP-A1 T1 focus ring, quiet hover, accent bar | helpers: T1 part b (b4); tabs: T2 part b; tree rows: T2 part d | `src/style/focus.rs`, `src/style/keys.rs`, `src/app/layout.rs`, `src/ui/topic_tree.rs` |
| CP-A1 T2 expander and Subscribe-header ring | T2 part d | `src/ui/topic_tree.rs` |
| CP-A1 T3 glyph-only controls at 24×24 | dissolved: T2 parts a, b, d, e (each control where it is reworded); check in T3 stage 2 | `src/app/header.rs`, `src/app/layout.rs`, `src/ui/topic_tree.rs`, `src/ui/topic_details.rs` |
| CP-A1 T4 full-row click, 24 pt pitch, leaf placeholder | T2 part d | `src/ui/topic_tree.rs` |
| CP-A1 T5 limits row scrolls into view | dropped: replaced by CP-A3 T6's popover; its check joins T2 part i | `src/ui/limits.rs` |
| CP-A1 T6 timeout and ports as DragValues | helper `field_number`: T1 part a; ports: T2 part c; timeout: T2 part g | `src/validation.rs`, `src/ui/connection.rs`, `src/ui/query.rs` |
| CP-A1 T7 filter opens only ancestors of direct matches | T2 part d | `src/types/tree.rs`, `src/ui/topic_tree.rs` |
| CP-A1 T8 keyboard-only integration | T3 stage 2 | `src/app/probe.rs` |
| CP-A2 T1 header status word and painted mark | T2 part a | `src/app/header.rs` |
| CP-A2 T2 worker word without the pulse | T2 part a | `src/app/header.rs`, `src/app/theme.rs` |
| CP-A2 T3 Light/Dark selector, "View" tabs, glyph collisions | selector: T2 part a; tab words: T1 part a (`DetailView::label`) and T2 part b; ⏵/⏷: T2 parts e, f; 🔍: T2 part d | several |
| CP-A2 T4 a word for each `✖` | T2 parts b (Dismiss), d (Clear filter, Unsubscribe) | `src/app/layout.rs`, `src/ui/topic_tree.rs` |
| CP-A2 T5 filter hint, "n of m topics", match emphasis | T2 part d | `src/ui/topic_tree.rs`, `src/types/tree.rs` |
| CP-A2 T6 leaf kinds, neutral branch glyph, painted local marker | T2 parts d, g | `src/ui/topic_tree.rs`, `src/ui/query.rs` |
| CP-A2 T7 Publish preview caption | T2 part f | `src/ui/publish.rs` |
| CP-A2 T8 Help as the reference layer | consts and `help_link`: T1 part a; sections and drift test: T2 part h; links: T2 parts c, d, e, f, g, i | `src/ui/help.rs` and call sites |
| CP-A2 T9 visible reasons beside disabled keys | T2 parts d (Subscribe), e (Save File) | `src/ui/topic_tree.rs`, `src/ui/topic_details.rs` |
| CP-A2 T10 integration | T3 stage 2 | `src/app/probe.rs` |
| CP-A3 T1 status strip with expiry | T2 part b | `src/app/layout.rs` |
| CP-A3 T2 fixed header slots | T2 part a | `src/app/header.rs` |
| CP-A3 T3 one connection key, Disconnect row deleted | `start_connect`, `ports_ok`: T1 part a; key: T2 part a; row deletion: T2 part b | `src/app/header.rs`, `src/app/layout.rs` |
| CP-A3 T4 connection settings in a fixed place (Connection view) | variant: T1 part a; tab and auto-select: T2 part b; form body: T2 part c | `src/types/message.rs`, `src/app/layout.rs`, `src/ui/connection.rs` |
| CP-A3 T5 tab row inside the detail panel | T2 part b | `src/app/layout.rs` |
| CP-A3 T6 limits in a popover beside the memory readout | body moved: T1 part a; popover: T2 part a; contents: T2 part i | `src/ui/limits.rs`, `src/app/header.rs`, `src/ui/messages.rs` |
| CP-A3 T7 Back row moved into the detail heading | T2 parts d (remove), e (heading) | `src/ui/topic_tree.rs`, `src/ui/topic_details.rs` |
| CP-A3 T8 tree click from Publish or Query | T2 part d | `src/ui/topic_tree.rs` |
| CP-A3 T9 Query slots | T2 part g | `src/ui/query.rs` |
| CP-A3 T10 Publish slots | T2 part f | `src/ui/publish.rs` |
| CP-A3 T11 one-line message rows, one order | renderer: T1 part a; All Messages: T2 part i; History: T2 part e | `src/ui/message_row.rs`, `src/ui/messages.rs`, `src/ui/topic_details.rs` |
| CP-A3 T12 editable Encoding combo | **not in this plan**: needs P3 T12's helpers (Open question 4) | — |
| CP-A3 T13 landmark integration | T3 stage 2 | `src/app/probe.rs` |
| CP-B T1 Palette | T1 part b (b1) | `src/colors.rs` |
| CP-B T2 two Visuals, `set_theme`, 1 pt strokes | T1 part b (b2) | `src/style/visuals.rs`, `src/style/mod.rs`, `src/app/theme.rs` |
| CP-B T3 contrast values | values: T1 part b (b1); call sites: every T2 part | `src/colors.rs` and call sites |
| CP-B T4 focus token | T1 part b (b2) | `src/style/visuals.rs` |
| CP-B T5 badges as quiet legends | helper: T1 part b (b5); call sites: T2 parts e, i | `src/style/badge.rs` |
| CP-B T6 key roles, disabled outline, key heights | T1 part b (b5); call sites: every T2 part | `src/style/keys.rs` |
| CP-B T7 fonts | T1 part b (b3) | `src/style/fonts.rs` |
| CP-B T8 text styles, weight, italics, mono locator, content style | table: T1 part b (b3); call sites: every T2 part; constants removed: T3 stage 1 | `src/style/text.rs`, `src/types/mod.rs` |
| CP-B T9 Snow White token values | T1 part b (b1) | `src/colors.rs` |
| CP-B T10 dark-mode ruling (ivory default; dark opt-in in neutral greys, user decision) | values: T1 part b (b1 `DARK`); default: T1 part a (`dark_mode: false`); selector: T2 part a | `src/colors.rs`, `src/app/mod.rs`, `src/app/header.rs` |
| CP-B T11 type at the Snow White floor | T1 part b (b3); clipping check: T3 stage 2 | `src/style/text.rs` |
| CP-B T12 mono Legend style | style: T1 part b (b3); call sites: T2 parts a, c, d, e, g, i | several |
| CP-B T13 caller-painted keys, latched tab bank | painter: T1 part b (b5); tab bank: T2 part b; source keys: T2 parts a, d, e, f, g | several |
| CP-B T14 content display, JSON colours, hex grid | helpers: T1 part b (b6); call sites: T2 parts e, f, g | `src/style/content.rs`, `src/style/glass.rs` |
| CP-B T15 statusGlass header module | frame: T1 part b (b6); use: T2 part a | `src/style/glass.rs`, `src/app/header.rs` |
| CP-B T16 fixed landmark skeleton | T2 part b | `src/app/layout.rs` |
| CP-B T17 integration | T3 stage 2 | `src/app/probe.rs` |
| CP-C T1 action ledger | T1 part c (c3); wiring: T2 part j | `src/motion/ledger.rs`, `src/events/mod.rs` |
| CP-C T2 commit binding by kind | T2 part j | `src/events/mod.rs` |
| CP-C T3 logical source keys | keys: T1 part c (c1); registration: T2 parts a, b, d, e, f, g, i | `src/motion/keys.rs` and call sites |
| CP-C T4 owning components for label-only results | frames: T1 part b (b6); registration: T2 parts a, d, f | several |
| CP-C T5 gutters | T2 part b | `src/app/layout.rs` |
| CP-C T6 pure sampler | T1 part c (c2) | `src/motion/sampler.rs` |
| CP-C T7 bevel painter, 4 pt fitted profile, cache | T1 part c (c4) | `src/motion/bevel.rs` |
| CP-C T8 connection painter | path and mesh: T1 part c (c5); slots: T2 part b | `src/motion/link.rs`, `src/app/layout.rs` |
| CP-C T9 repaint policy | T1 part c (c6) | `src/motion/repaint.rs` |
| CP-C T10 real-app measurement | T3 stage 3 | none (log output) |
| CP-C T11 supersede and cleanup | T1 part c (c3, c6) | `src/motion/mod.rs` |
| CP-C T12 pending on async sources | T1 part c (c3); binding: T2 part j | `src/motion/ledger.rs` |
| CP-C T13 wire the ten actions | action table: T1 part c (c1); call sites: T2 parts a, b, d, e, f, g, i, j | several |
| CP-C T14 count and value change response | T1 part c (c6); tree rows: T2 part d; transfer completion: **not in this plan** (P4 T9) | `src/motion/mod.rs`, `src/ui/topic_tree.rs` |
| CP-C T15 reduced motion (fully static, user decision) | T1 part c (c2, c6); toggle: T2 part a (header, beside the theme selector) | `src/motion/sampler.rs`, `src/motion/mod.rs`, `src/app/header.rs` |
| CP-C T16 key painting yields to motion | parameter: T1 part b (b5); callers: T2 parts | `src/style/keys.rs` |
| CP-C T17 protected expander check | T3 stage 2 | — |
| CP-C T18 motion integration | T3 stage 2 | `src/app/probe.rs` |

**Goal:** Make Zenoh Explorer keyboard-usable and legible (visible focus, 24 pt targets, words instead of lone glyphs, WCAG AA text and 3:1 boundaries in both themes), keep every landmark still when state changes, and give it the Snow White look (ivory chassis, olive keys, rust selection, glass status and content displays) with the causal motion response, on the current egui 0.29 tree, with an automatic check for every visual change.

**Architecture:**
- **Theme core in one place.** `src/colors.rs` holds a `Palette` with semantic fields and two instances, `LIGHT` (ivory, the default) and `DARK` (the opt-in dark variant in lighter neutral greys, user decision). A new `src/style/` module turns the palette into two complete `Visuals` installed once with `set_visuals_of`, the text-style table, the font stack, the focus ring, caller-painted keys, latched labels, badges and glass frames. Call sites ask `crate::style::p(ui)` for the palette; no call site builds a `Color32`.
- **Motion core in one place.** A new `src/motion/` module holds the causal-motion engine ported from the T11 spike (`267dd50`): a pure sampler, the ring-strip bevel mesh with a cache, the gutter link, the action ledger keyed by kind, the repaint policy and reduced motion (fully static when on), with theme-dependent shadow and link inks so the effect also works on the grey dark base. Its public API is fixed in T1; T2 parts only call it.
- **Structure first, then parallel parts.** T1 splits `layout.rs` into `header.rs`, `connection.rs` and `limits.rs`, splits the detail view out of `topic_tree.rs` into `topic_details.rs`, extracts `frame_ui(ctx)` from `eframe::App::update` so tests can render the whole window, and adds a headless probe (`src/app/probe.rs`) that reads painted text, rects, colours, strokes and AccessKit bounds. After that each T2 part owns a disjoint file group.
- **Checks are automatic.** Colour: WCAG contrast computed by unit tests on the palette. Layout, focus, size and motion: headless frames through the probe (`egui::Context::run`, no window, no GPU). Text and glyphs: greps and a `has_glyphs` test. Only the real-app frame timing in T3 stage 3 needs a display.

**Tech Stack:** Rust 1.88 (MSRV, `Cargo.toml:5`), egui/eframe 0.29.1 (glow, `accesskit` feature on, which re-exports `egui::accesskit` 0.16.3), epaint 0.29.1 default fonts (Ubuntu-Light, Hack, NotoEmoji, emoji-icon-font), `cargo test`, `cargo clippy`, `cargo fmt`, `grep`, git worktrees.

**Spec:** `docs/superpowers/reviews/2026-09-24-ui-ux-snow-white-review.md`, section "T21 — Synthesis": the ranked findings table (lines 5157–5336), the candidate plans CP-A1, CP-A2, CP-A3, CP-B and CP-C (lines 5389–5578), and the open questions (lines 5676–5722). Token values come from the same document's "Token map" (lines 197–247) and "Token proposal" / "Contrast: proposed (Snow White)" tables (lines 1108–1279); motion values from T9 (lines 2170–2240) and T11 (spike `267dd50`, `examples/causal_motion_spike.rs`, never merged); the per-action map from T12 (lines 2878–2960).

**Depends on plan:** P1 `docs/superpowers/plans/2026-09-25-correctness-and-hardening.md` (complete) and P2 `docs/superpowers/plans/2026-09-25-p2-ci-release-hardening.md` (complete, HEAD `3ce8c01` on `bearhug-mode-test`). The line numbers below were read on `3ce8c01`. P1's end state this plan builds on: `ui_alert: Option<UiAlert>` with `UiAlert::{Success, Warning, Error}(String)` (`src/app/mod.rs:20-35`); `header_status_text`, `memory_readout`, `peers_text` (`src/app/mod.rs:74-139, 364-376`); `PublishStatus` and `publish_status_line` (`src/types/message.rs:117-130`, `src/ui/publish.rs:14-30`); `HELP_SECTIONS` (`src/ui/help.rs:10-58`); `pending_subscribes`; `MessageSource::UserSubscription`. **P3 runs after this plan** and keeps its colour values and structure (P3 plan line on `theme.rs`: "If Snow White has already edited theme.rs, T4 keeps Snow White's values"); what P3 must absorb is listed under "P3, P4 and P5 absorption notes".

**Decisions:** no memex decisions. Recorded user decisions: keys 10 % shorter than the mockup brief (Hero 44→40, Standard 40→36, Compact 36→32, Mini 32→29 pt), never under the 24 pt target floor (F-T4-10); `ui_alert` stays `Option<UiAlert>` and every new assignment wraps its text in a variant. From the review of this plan (relayed by the coordinator on 2026-09-26): full adoption of CP-A1, CP-A2, CP-A3, CP-B and CP-C; the dark palette stays a user-selectable option, lightened to neutral greys, with ivory the default; reduced motion is off completely (static end states); its toggle sits in the header beside the theme selector and is not persisted until P3 Settings; Snow White fixed-width header slots win at 720 pt, with long readouts truncated and the full text on hover; the structural split of T1 part a is confirmed. "Defaults this plan assumes" marks each of these "decided by the user". **Kind of change:** UI only (look, words, layout, focus, motion). No worker, protocol or dependency change; no new crate; no font download.

## Defaults this plan assumes

Rows marked "decided by the user" are settled. The other rows are defaults the plan is written for, so that every step has concrete code; each of them is repeated under "Open questions for the user" with what changes if the answer differs.

| Question | Default in this plan | Where it is applied |
|---|---|---|
| Q1 adopt Snow White | decided by the user: full adoption, all of CP-A1, CP-A2, CP-A3, CP-B and CP-C | whole plan |
| Q2 dark mode | decided by the user: ivory `LIGHT` is the default at launch; the dark palette stays a user-selectable "🌙 Dark" value of a worded two-value selector, lightened to neutral greys (`DARK`: chassis #3c3c3c, panel #484848, field #333333; neither near-black graphite nor warm-tinted); every text and control pair passes the WCAG tests in both palettes; the motion's shadow and link inks have a neutral dark set for the grey base; not persisted (P3 T5 persists) | T1 part b (b1, b2), T1 part c (c1, c4, c5, c6), T1 part a (`dark_mode: false`), T2 part a, T3 stage 1 |
| Q3 protected expander | `plus_minus_icon` and `animation_time = 0.001` unchanged | Global Constraints, T2 part d, T3 stage 2 |
| Q4 reduced-motion setting | decided by the user: a "Reduce motion" toggle in the header, directly beside the ☀ Light / 🌙 Dark selector (not in the limits popover); off at every launch and not persisted until P3 Settings (absorption note 3); no OS query | T2 part a |
| Q5 reduced-motion behaviour | decided by the user: off completely. With reduced motion on, every motion effect is static: the end state appears at once, with no relay, no fade, no pulse and no timed relay removal (no wake-up near 1155 ms) | T1 part c (c2, c6), T3 stage 2 |
| Q9 connection settings | (a) a "🔌 Connection" detail view; selected at launch while disconnected and on a connection error | T2 parts b, c |
| Q10 tree click from Publish or Query | jump to Topics as today, plus an immediate repaint so the selection shows in the same frame | T2 part d |
| Q12 link length | (a) 16 pt gutters between tree and detail and under the in-panel tab row | T2 part b |
| alert expiry | Success after 6 s, Warning after 10 s, Error until Dismiss | T2 part b |
| header width | decided by the user: Snow White fixed-width slots win. Two constant header rows whose slots fit a 720 pt window; long P1 readouts are truncated to their slot with the full text in a hover tooltip, and three labels are shortened; nothing wraps into another row, clips or overflows at 720 pt or 1000 pt. The drop counters and the memory warning stay in the header, sharing one fixed notice slot (T2 part a says why) | T2 part a; absorption note 4 (P3 T7) |
| structural split | decided by the user: T1 part a splits `layout.rs` into `header.rs`, `src/ui/connection.rs` and `src/ui/limits.rs`, moves the detail view into `topic_details.rs`, extracts `frame_ui`, adds the probe and pre-declares the new fields | T1 part a; absorption notes 1, 2, 8, 9 and 12 (P3 T4 and P5 T1 rebase) |
| key heights | decided by the user: Hero 40, Standard 36, Compact 32, Mini 29 pt, never under the 24 pt floor | T1 part b (b5) |
| `✓` glyph | replaced by `✔` (U+2714, NotoEmoji) instead of bundling a font | T2 parts d, e |
| `.strong()` weight | labels use a `Label` text style (14 pt, secondary ink) instead of a bold face (no font download) | T1 part b, T2 parts |

## Not in this plan (routed elsewhere)

These findings or parts of findings are P-routed by the review's P-routing table or depend on a later plan. No step here implements them:
- The three limits as `DragValue`s, the wrapped Messages toolbar and the "List each sample once" rename (F-T4-9, F-T4-11: P3 T14). The popover keeps P1's text fields and the word "Dedup"; P3 T14 converts them in `src/ui/limits.rs`.
- Sticky collapse per filter text (F-T13-2's other half: P3 T9), focus scrolling for tree rows and toggles (F-T7-14: P3 T11), accessible names through `widget_info` (P3 T11), the disabled-key Tab wrap and drag-to-scroll Tab stops (F-T7-12, F-T7-13: P3 T11, T14), kittest (P3 T3), snapshot tests (P3 T16), theme persistence (P3 T5), the off-thread import and encoding inference (P3 T12).
- The editable Encoding combo (CP-A3 T12): needs P3 T12's `encoding_for_filename`, `encoding_set_by_import`, `end_import`, none of which exist on `3ce8c01`.
- Query run ids, per-run headers and Cancel (P5 T16); the query commit binds to the events that exist today (T2 part j). Arrow-key tree navigation (P5 T20), per-row rates (P5 T21), Esc to dismiss (P5 T24), connection profiles (P5 T23).
- The transfer-completion response of CP-C T14 (needs P4 T9's transfer panel).
- The worker split and Cancel while connecting (Q14; not placed anywhere).

## Global Constraints

- **Scope of files.** Only `src/**` changes (plus `assets/` stays untouched: no font file is added). `Cargo.toml` and `Cargo.lock` do not change. No `unsafe`.
- **Protected expander.** `fn plus_minus_icon` (`src/ui/topic_tree.rs:75-100`) is not edited, and `style.animation_time` stays `0.001`. Checked in T2 part d and T3 stage 2 by comparing the function's text, extracted with the brace-matching script `$SWRUN/fn_body.py` ("Local tooling and disk"), at the base and at HEAD, so moving the function does not matter; and by the unit test `expander_untouched`.
- **Alerts.** Every new alert is `self.ui_alert = Some(UiAlert::Success(..))`, `UiAlert::Warning(..)` or `UiAlert::Error(..)`. The strip renders P1's words ("Warning: ", "Error: ") unchanged.
- **P1 wording stays.** `header_status_text`, `memory_readout`, `peers_text`, `publish_status_line`, `connection_notice` (except its "Connection Settings" place name), the query verdicts in `src/events/mod.rs` and the history texts are placed, not reworded, except where a step quotes the new text.
- **No colour literals outside the palette.** After T3, the colour-literal grep of T3 stage 1 Step 4 prints nothing: outside `src/colors.rs` only `Color32::TRANSPARENT`, `Color32::PLACEHOLDER` and the alpha-compositing helper in `src/motion/bevel.rs` remain. During T2, new code uses `crate::style::p(ui)` (or `crate::colors::palette(dark)`) and never adds `ExplorerColors::` uses.
- **Key heights (user decision).** `KeyTier::{Hero = 40, Standard = 36, Compact = 32, Mini = 29}` pt; every interactive control is at least 24×24 pt (`crate::style::focus::MIN_TARGET`). Source keys of the motion effect (Connect, Subscribe, Publish, Import File, Query, Save File) are `Standard` or `Hero` (≥ 34 pt, F-T11-4); tabs are `Compact` and use the 4 pt fitted bevel profile.
- **Frozen interfaces in T2.** A T2 part may call, but not change the signature of, anything T1 created in a file it does not own: `crate::style::*`, `crate::colors::*`, `crate::motion::Motion` and its public methods, `crate::app::probe::*`, `ConnectionUI::start_connect`, `ZenohExplorer::ports_ok`, `LimitsUI::show_limits_controls`, `ZenohExplorer::help_link`, `crate::ui::message_row::*`, `DetailView::{ALL, label}`, `validation::field_number`, and the pre-declared fields in `src/app/mod.rs`. If a part needs a change there, it stops and reports; the coordinator adds it in T3 stage 1 or re-plans.
- **Temporary allowances are tagged.** Items added in T1 before their first caller carry `#[allow(dead_code)] // SW-T2: <who uses it>`; the motion and style modules start with `#![allow(dead_code)] // SW-T2`. T3 stage 1 removes every `SW-T2` marker, and `grep -rn 'SW-T2' src` must print nothing at the end.
- **The plan's code has not been compiled.** It was written against the egui/epaint 0.29.1 and accesskit 0.16.3 sources in `~/.cargo/registry` (disk rules forbade a build while planning). When the compiler or clippy disagrees, the implementer makes the smallest compile or clippy fix consistent with the step's intent (the nearest 0.29.1 equivalent; behaviour and test meaning unchanged) and reports it as a deviation: in the commit message and in the part's evidence for the verifier.
- **Every commit** ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Commands run from the worktree or checkout named in "How Tn runs"; never from another agent's worktree.

## Local tooling and disk

- Present on this Mac: `cargo`, `rustc` (stable and 1.95; MSRV 1.88 is checked by P2's CI, not here), `grep`, `git`, `python3`. Nothing needs installing.
- **Disk is the binding limit.** On 2026-09-25 the data volume had about 11 GiB free and `target/debug` was 5.7 GiB. Each part's `CARGO_TARGET_DIR` is an APFS clone (`cp -Rc`) of `target/debug`, which shares blocks until rebuilt; a part's own rebuild of this crate and its test binary writes roughly 0.5–1 GiB. Therefore: at most **four** parts build at the same time; before a part's first build it runs `df -g /System/Volumes/Data | awk 'NR==2{print $4}'` and waits if the free space is under 3 GiB; a part's target directory is deleted as soon as its checks pass and its branch is committed. No part runs `cargo build --release` (T3 stage 3 is the only release build).
- The five `#[ignore = "opens network sessions"]` tests bind fixed ports, so two runs of them must never overlap. No T1 or T2 part runs `--ignored`. In T3, stage 3 runs one ignored test by name (`bevel_cost_dev_profile`, no network) and stage 4 runs `cargo test --locked -- --ignored` (every ignored test, the five network tests included). The one T3 agent runs its stages in order and stage 4 starts after stage 3 has finished, so both are fine sequentially and the port-binding run happens once.
- **Expander guard script.** Written once by the coordinator when T1 starts ("How T1 runs" step 1) and used by T2 part d and T3 stage 2. It prints `fn <name>`'s full text, from `fn` through the matching closing brace, skipping braces inside string and char literals and `//` comments, and exits non-zero if the function is missing or unbalanced; a function that moves keeps the same text:

  ```bash
  cat > "$SWRUN/fn_body.py" <<'EOF'
  import re, sys
  name, src = sys.argv[1], sys.stdin.read()
  m = re.search(r'\bfn\s+' + re.escape(name) + r'\b', src)
  if not m:
      sys.exit(f"fn {name} not found")
  i, depth = m.end(), 0
  while i < len(src):
      c = src[i]
      if src.startswith('//', i):
          j = src.find('\n', i)
          i = len(src) if j < 0 else j
          continue
      if c == '"':
          i += 1
          while i < len(src) and src[i] != '"':
              i += 2 if src[i] == '\\' else 1
      elif c == "'" and (cm := re.match(r"'(?:\\.|[^\\'])'", src[i:i + 5])):
          i += len(cm.group(0))
          continue
      elif c == '{':
          depth += 1
      elif c == '}':
          depth -= 1
          if depth == 0:
              print(src[m.start():i + 1])
              sys.exit(0)
      i += 1
  sys.exit(f"fn {name}: unbalanced braces")
  EOF
  ```

  Comparison (`<rev>` is the base being guarded against):

  ```bash
  git show "<rev>":src/ui/topic_tree.rs | python3 "$SWRUN/fn_body.py" plus_minus_icon > "$SWRUN/pmi-base.txt" &&
  python3 "$SWRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$SWRUN/pmi-head.txt" &&
  cmp "$SWRUN/pmi-base.txt" "$SWRUN/pmi-head.txt" && echo expander-unchanged
  ```

  Both extractions must succeed and the texts must be byte-identical; anything else (including a missing function) prints no `expander-unchanged`.

## Review Focus

- **Keyboard user on a light theme:** every Tab stop on a tab, tree row, expander, "Subscribe to Topics" header, key or text field must show a ring at ≥ 3:1 against its background, and the ring must never look like the selected latch. Pinned by T1 part b (focus token, `latched_label`), T2 parts b and d, and T3 stage 2's Tab pass.
- **Landmarks under state change:** connecting, an alert, a selected topic, a pending query or publish and a long connection error must not move the title, tabs, tree, Query key, Publish key or strip by a single point. Pinned by the probe's landmark table (T3 stage 2) and each part's own probe test.
- **Colour-only state:** no state may be told apart by hue alone (connection status, local marker, message type, memory level, disabled keys). Pinned by T2 part a's `status_mark`, part d's painted marker plus hover word, T1 part b's badge legends and greyscale key-role test.
- **Motion honesty:** a failed put, a failed connect or a keyless subscribe failure must never produce a reveal; a stale commit after a newer action must be ignored. Pinned by T2 part j's binding tests and T1 part c's ledger tests.
- **Idle cost after motion:** once a response settles the app must return to P1's idle tick (1 s), with no 66 ms or per-frame repaint left behind. Pinned by T1 part c's repaint tests, T2 part a's pulse removal and T3 stage 3's idle check.
- **Header at 720 pt:** the fixed slots never wrap into another row, clip or overflow at 720 pt or 1000 pt, and every truncated readout keeps its full text on hover. Pinned by T2 part a's `slot_budget_fits_720`, `header_fits_at_720_and_1000` and `header_slots_do_not_move` (which also runs at 720 pt).
- **Reduced motion is static:** with the header toggle on, no surface animates, no relay appears and no timed wake-up is scheduled; the end state shows at once. Pinned by T1 part c's `reduced_is_fully_static` and `reduced_motion_is_static`, T2 part a's `reduce_motion_toggle_in_header` and T3's `reduced_motion_is_static_in_app`.
- **Merge safety:** a T2 part that edits a file it does not own, or changes a frozen T1 signature, would break the other nine parts. Pinned by the ownership check (`git diff --name-only`) and the conflict rule in "How T2 runs".

---

## Tasks

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Merged · stage 1 parts b and c in parallel, then stage 2 part a · owns `src/colors.rs`, `src/app/theme.rs`, `src/style/` (b); `src/motion/` (c); `src/main.rs` (Step 0); `src/app/layout.rs`, `src/app/header.rs`, `src/app/mod.rs`, `src/app/probe.rs`, `src/ui/mod.rs`, `src/ui/connection.rs`, `src/ui/limits.rs`, `src/ui/message_row.rs`, `src/ui/topic_details.rs`, `src/ui/topic_tree.rs`, `src/ui/messages.rs`, `src/ui/help.rs`, `src/types/message.rs`, `src/types/mod.rs` (tags only), `src/validation.rs` (a)] Foundation. Step 0 (coordinator): empty `style` and `motion` modules declared in `main.rs`. b (was CP-B T1, T2, T3 values, T4, T6 helpers, T7, T8 table, T9, T11 values, T13 painter, T14 and T15 frames; CP-A1 T1 helpers): Palette LIGHT and DARK (neutral greys, user decision) with WCAG tests, visuals installed once per theme, fonts with Hack in Proportional, text styles, focus ring, latched label, caller-painted keys with the four tiers, badges, glass frames, JSON colours and hex grid. c (was CP-C T3 keys, T6, T7, T8 path, T9, T11, T12, T13 table, T14, T15): motion engine from the T11 spike with ledger, sampler, cached bevel with 8 pt and 4 pt profiles, link path, light and neutral-grey dark inks, repaint policy and fully static reduced motion, all pure and unit-tested. a (structure, no visible change except the ivory default): `frame_ui(ctx)`, header and connection and limits and detail view split into their own files, `DetailView::Connection` with `ALL` and `label`, Help constants and `help_link`, `message_row` renderer, `field_number`, pre-declared fields, and the headless probe | — | Step 0 commit builds. Part b: `cargo test --locked style::` and `cargo test --locked colors::` pass, including `palette_text_pairs_meet_aa` and `palette_boundaries_meet_3_to_1` for both palettes, `visuals_are_complete`, `installed_visuals_follow_theme`, `ui_glyphs_render`, `key_roles_differ_in_greyscale`, `key_tiers_follow_user_heights` and `json_tokens_meet_aa`; `test -d src/style && ! grep -rn 'Color32::from_' src/style` exits 0 (the directory must exist: a grep of a missing directory also prints nothing). Part c: `cargo test --locked motion::` passes, including `seat_reproduces_t9_hex`, `motion_inks_by_theme`, `side_schedule_matches_t9`, `pending_rises_to_072`, `reduced_is_fully_static`, `relay_expires_after_its_response`, `reduced_motion_is_static`, `ledger_*` (pending to committed, pending to failed, stale commit ignored, timeout), `bevel_stays_in_band` for both profiles, `mesh_cache_reuses` and `repaint_delay_adds_predicted_dt`. Part a: all tests that existed at the base still pass under their new paths, plus `probe_renders_whole_window`, `probe_reads_accesskit_bounds`, `detail_view_labels_cover_all`, `field_number_parses_or_falls_back` and `message_row_text_order`. For every part, `git diff --name-only` against its base lists only its owned files. On the merged tree `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings` and `cargo test --locked` pass, and one read-only verifier per part reports no open finding |
| T2 | [Wave 1 · Merged · parts a–j in parallel, two batches of at most four builds · owns `src/app/header.rs`, `src/app/theme.rs` (a); `src/app/layout.rs` (b); `src/ui/connection.rs` (c); `src/ui/topic_tree.rs`, `src/types/tree.rs` (d); `src/ui/topic_details.rs` (e); `src/ui/publish.rs` (f); `src/ui/query.rs` (g); `src/ui/help.rs` (h); `src/ui/messages.rs`, `src/ui/limits.rs`, `src/ui/message_row.rs` (i); `src/events/mod.rs`, `src/motion/` (j)] Every visible change. a (was CP-A2 T1, T2, T3 selector; CP-A3 T2, T3 key, T6 popover; CP-B T15; CP-C sources in the header, T15 toggle): statusGlass header with fixed slots that fit 720 pt without wrapping (truncated readouts, full text on hover), painted status mark, static worker word, worded Light and Dark selector with the Reduce motion toggle beside it (not persisted), one connection key, one notice slot for the memory warning and drop counters, limits popover. b (was CP-A3 T1, T4 place, T5; CP-A2 T3 tab words, T4 Dismiss; CP-A1 T1 tabs; CP-B T13 tab bank, T16; CP-C T5, T8 slots): status strip with expiry, Connection view tab, tabs inside the detail panel as a latched bank with rings, three-band skeleton with 16 pt gutters, motion frame hooks. c (was CP-A1 T6 ports; CP-A3 T4 form): Connection view form with port DragValues and no Connect button. d (was CP-A1 T1 rows, T2, T4, T7; CP-A2 T3, T4, T5, T6, T9 Subscribe; CP-A3 T7, T8; CP-C tree sources): tree rows at 24 pt with full-row click, rings, accent bar, leaf placeholder, leaf kinds, painted marker, filter words and counts, ancestors-only auto-open. e (was CP-A2 T9 Save, CP-A3 T7 heading, T11 history; CP-B T14): detail view with Back in the heading, content display, inline Save result. f (was CP-A2 T7; CP-A3 T10): Publish slots and preview caption. g (was CP-A1 T6 timeout; CP-A2 T6 marker; CP-A3 T9): Query slots and timeout DragValue. h (was CP-A2 T8): Help reference layer with drift test. i (was CP-A3 T6 contents, T11; CP-A1 T5 check): one-line message rows and the popover's limit fields. j (was CP-C T1 and T2 wiring): commit binding in `process_events` | T1 | Each part's tests named in its section pass in its own worktree, and the part's ownership check lists only its owned files. Part a: `header_slots_do_not_move` (at 1400, 1000 and 720 pt), `slot_budget_fits_720`, `header_fits_at_720_and_1000`, `status_mark_rules`, `health_word_is_static`, `connection_key_rules`, `notice_text_rules`, `connecting_key_sends_nothing`, `reduce_motion_toggle_in_header`; `grep -rn 'animate_pulse' src` and `grep -rn 'from_millis(66)' src` print nothing. Part b: `strip_keeps_workspace_still`, `alert_expiry_rules`, `tabs_sit_over_detail_panel`, `tab_focus_shows_ring`, `connection_view_auto_selects`, `gutters_show_chassis`; `grep -rn 'alert_banner' src` prints nothing. Part c: `ports_cannot_commit_out_of_range`, `form_moves_with_view_only`. Part d: `tree_row_pitch_is_24`, `leaf_sits_right_of_parent`, `expander_rect_unchanged` (regression guard), `full_row_click_selects`, `focused_row_shows_ring_not_latch`, `selected_row_has_accent_bar`, `expander_focus_shows_ring`, `filter_default_open_rules`, `filter_counts_and_hint`, `leaf_kind_rules`, `subscribe_blocked_reason_rules`; `grep -n 'small_button' src/ui/topic_tree.rs` prints nothing. Part e: `back_is_in_heading`, `save_reason_is_visible`, `history_rows_are_one_line`, `current_value_uses_content_display`. Part f: `publish_key_does_not_move`, `preview_caption_words`. Part g: `query_key_does_not_move`, `timeout_cannot_commit_out_of_range`, `pending_line_words`. Part h: `help_names_only_real_views` and `troubleshooting_is_last` (each a regression guard plus one assertion that is red before part h), `help_links_resolve`, `hints_share_help_text`, `help_target_scrolls_into_view`. Part i: `messages_rows_are_one_line`, `types_mixed_rules`, `limits_controls_visible_at_1000x600`. Part j: `failed_put_never_reveals`, `connection_error_ends_pending`, `monitor_failure_then_connected_reveals`, `keyless_subscribe_failure_with_two_pending` (regression guard) with `keyless_subscribe_failure_with_one_pending_ends_it`, `empty_query_reveals_results`. All ten `--no-ff` merges are conflict-free, and on the merged tree `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings` and `cargo test --locked` pass; one read-only verifier per part reports no open finding |
| T3 | [Wave 2 · Merged · stages 1 → 2 → 3 → 4, one agent · owns every file under `src/` for clean-up and fix-ups] Clean-up and integration. 1 (was CP-B T17's removals): remove `ExplorerColors`, the old colour methods, the tertiary getter, `animate_fade_in`, the size constants and every SW-T2 allowance; move the motion colours and both ink sets into `src/colors.rs` and check the dark inks on the grey base. 2 (was CP-A1 T8, CP-A2 T10, CP-A3 T13, CP-B T17, CP-C T17, T18): the integrated probe suite at 1400×900 and 1000×600 in both themes, covering landmarks, the Tab pass, 24 pt targets, contrast of installed visuals, glyph coverage, 150 % zoom, the expander guard, the motion schedule and static reduced motion. 3 (was CP-C T10): release-build motion timing and idle check in the real app. 4: verifiers, board evidence and the manual capture checklist | T2 | Stage 1: `grep -rn 'ExplorerColors' src`, `grep -rn 'SW-T2' src`, `grep -rn 'text_tertiary_color' src`, `grep -rn 'animate_fade_in' src` and `grep -rn 'HEADING_LARGE_SIZE' src` each print nothing, the colour-literal grep in stage 1 Step 4 prints nothing (copy that grep from the step, not from this cell), and `cargo test --locked colors::` passes including `motion_inks_suit_each_base`. Stage 2: `cargo test --locked probe::integration` passes with every test named in stage 2 (landmarks_are_stable, keyboard_pass_rings_every_stop, targets_are_at_least_24, installed_contrast_meets_wcag, no_tofu_in_ui_strings, no_clipping_at_1000x600_and_150_percent, expander_untouched (a regression guard), motion_reveal_matches_t9, reduced_motion_is_static_in_app); the text of `fn plus_minus_icon` extracted by the brace-matching `$SWRUN/fn_body.py` is byte-identical at the T1 base and at HEAD (stage 2 Step 2 prints `expander-unchanged`). Stage 3: the timing test output and the ZE_MOTION_LOG summary (median and p95 CPU per animating frame, update intervals, idle frames per second after settle) are in the commit message, or recorded as unmeasured with the reason when no display is available. Stage 4 (after stage 3 has finished, so the port-binding ignored tests run once and alone): `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` and `cargo test --locked -- --ignored` pass on the final tree; one read-only verifier per stage reports no open finding |


## How the work is split

```
T1  Step 0 (coordinator: empty style + motion modules in main.rs)
      │
      ├── stage 1 ─┬── part b  theme core   (colors.rs, app/theme.rs, style/*)       was CP-B T1–T4, T6–T9, T11, T13–T15 helpers
      │            └── part c  motion core  (motion/*)                               was CP-C T3, T6–T9, T11–T15 engine
      │            ownership check → --no-ff merge b, c → build check
      │
      └── stage 2 ──── part a  structure    (layout.rs split, header.rs, connection.rs,
                                             limits.rs, topic_details.rs, probe.rs, …) consumes b and c
            ownership check → --no-ff merge a → one integration run → 3 read-only verifiers
      │
T2  parts in parallel, batch 1: d tree · e details · b layout · a header
                       batch 2: c connection · f publish · g query · h help · i lists · j motion wiring
      │  (each consumes only T1 symbols; no part consumes another T2 part's symbols)
      ownership check → --no-ff merges a, b, c, d, e, f, g, h, i, j → one integration run → 10 read-only verifiers
      │
T3  stage 1 clean-up → stage 2 integrated probe suite → stage 3 real-app motion timing → stage 4 verification
```

**Why this shape.** A part that needs a symbol another part introduces cannot run beside it. Part a in T1 needs `crate::motion::Motion` (for the app field) and `crate::style::install` (for the probe), so it runs after b and c. Everything a T2 part calls on another file group was created in T1 and is frozen (Global Constraints), so the ten T2 parts are independent. T3 needs every T2 part merged: it deletes the old API that T2 stopped using and measures the whole window. The maximum width is two agents in T1 and ten agents in T2 (four building at a time, disk rule).

**How T1 runs.**

1. **Start and base.** Start T1 on the board once (`scripts/bin/bearhug-work start T1 --session <session> --provider claude`). Then, in the main checkout:

   ```bash
   SWRUN=${TMPDIR:-/tmp}/sw/../swrun
   mkdir -p "$SWRUN"
   # write "$SWRUN/fn_body.py" from "Local tooling and disk" (the expander guard script)
   git status --porcelain            # must print nothing
   df -g /System/Volumes/Data | awk 'NR==2{print $4}'   # free GiB; stop if under 6
   ```

2. **Step 0 (coordinator, main checkout).** Create the two empty modules so parts b and c each own a whole directory and never touch `main.rs`:

   ```bash
   mkdir -p src/style src/motion
   printf '//! Snow White style: palette-driven visuals, fonts, text styles and painted controls.\n' > src/style/mod.rs
   printf '//! Causal motion: action ledger, bevel and link painting, repaint policy.\n' > src/motion/mod.rs
   ```

   In `src/main.rs`, change the module list (lines 10-18) to:

   ```rust
   mod app;
   mod colors;
   mod events;
   mod motion;
   mod payload;
   mod style;
   mod transfer;
   mod types;
   mod ui;
   mod validation;
   mod worker;
   ```

   ```bash
   cargo check --locked 2>&1 | tail -n 2      # Expected: "Finished" and no warning
   git add src/main.rs src/style/mod.rs src/motion/mod.rs
   git commit -m "chore(sw): empty style and motion modules

   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   BASE1=$(git rev-parse HEAD)   # record in the evidence
   ```

3. **Stage 1 worktrees (parts b and c).** Both compile the crate with the stable toolchain, so each target directory is an APFS clone of the main checkout's `target/debug`:

   ```bash
   for p in b c; do
     git worktree add -b "sw-t1-$p" "$SWRUN/wt-$p" "$BASE1"
     mkdir -p "$SWRUN/tgt-t1-$p" && cp -Rc target/debug "$SWRUN/tgt-t1-$p/debug"
   done
   ```

   One agent per part, in `"$SWRUN/wt-<part>"` with `export CARGO_TARGET_DIR="$SWRUN/tgt-t1-<part>"`. A part edits only its owned files (task table), runs the checks in its section, and commits on its branch after each sub-step (b1…b6, c1…c6).

4. **Stage 1 ownership check and merge**, in the main checkout:

   ```bash
   git diff --name-only "$BASE1" sw-t1-b    # only src/colors.rs, src/app/theme.rs, src/style/*
   git diff --name-only "$BASE1" sw-t1-c    # only src/motion/*
   for p in b c; do
     git merge --no-ff "sw-t1-$p" -m "merge(sw-t1): part $p

   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   done
   export CARGO_TARGET_DIR="$SWRUN/tgt-t1-b"
   cargo test --locked -- style:: colors:: motion:: 2>&1 | tail -n 3   # all pass
   BASE2=$(git rev-parse HEAD)
   rm -rf "$SWRUN/tgt-t1-c"
   ```

   Anything outside a part's owned files, or a conflict: stop and report.

5. **Stage 2 (part a).** `git worktree add -b sw-t1-a "$SWRUN/wt-a" "$BASE2"`; the target directory is `mv "$SWRUN/tgt-t1-b" "$SWRUN/tgt-t1-a"` (it already holds the merged stage-1 build). One agent runs a1…a7 in order and commits after each. Ownership check: `git diff --name-only "$BASE2" sw-t1-a` lists only part a's files from the task table. Merge with `--no-ff` as above.

6. **One integration run** on the merged tree, in the main checkout:

   ```bash
   export CARGO_TARGET_DIR="$SWRUN/tgt-int"
   [ -d "$CARGO_TARGET_DIR" ] || { mkdir -p "$CARGO_TARGET_DIR" && cp -Rc "$SWRUN/tgt-t1-a/debug" "$CARGO_TARGET_DIR/debug"; }
   cargo fmt --all -- --check
   cargo clippy --all-targets --locked -- -D warnings
   cargo test --locked 2>&1 | grep -E '^test result' # every line "ok", 0 failed
   test -d src/style && ! grep -rn 'Color32::from_' src/style   # exits 0: src/style exists and has no colour literals
   rm -rf "$SWRUN/tgt-t1-a"
   ```

7. **Verify and close.** Three read-only verifiers (b, c, a) each read `git diff <part base> sw-t1-<part>` against that part's section and report findings; they edit nothing. A fix-up is made by the coordinator in the owning file in a separate commit naming the part. Complete T1 on the board with the evidence (bases, per-part test output, merge commits, integration output, verifier verdicts). Remove the worktrees and branches (`git worktree remove "$SWRUN/wt-$p"`, `git branch -d "sw-t1-$p"`). Keep `$SWRUN/tgt-int`.

**How T2 runs.**

1. **Start and base.** Start T2 on the board. `BASE=$(git rev-parse HEAD)` (the T1 end commit plus any board-only commit); `git status --porcelain` prints nothing.
2. **Worktrees.** One worktree and branch per part, all from `$BASE`: `for p in a b c d e f g h i j; do git worktree add -b "sw-t2-$p" "$SWRUN/wt-$p" "$BASE"; done`. Target directories are created per batch, not all at once:

   ```bash
   seed() { mkdir -p "$SWRUN/tgt-t2-$1" && cp -Rc "$SWRUN/tgt-int/debug" "$SWRUN/tgt-t2-$1/debug"; }
   for p in d e b a; do seed "$p"; done     # batch 1
   ```

3. **Batch 1 (parts d, e, b, a), then batch 2 (parts c, f, g, h, i, j).** One agent per part, in `"$SWRUN/wt-<part>"` with `export CARGO_TARGET_DIR="$SWRUN/tgt-t2-<part>"`. Before its first build each agent checks free space (`df -g /System/Volumes/Data | awk 'NR==2{print $4}'` ≥ 3) and waits otherwise. When a batch-1 part has committed and passed its checks, the coordinator deletes its target directory and seeds the next batch-2 part, so at most four part directories exist at once. Batch 2 parts h and j build smallest and may start as soon as one batch-1 part finishes. A part edits only its owned files, runs only `cargo test --locked <its filters>` plus `cargo clippy --all-targets --locked -- -D warnings`, and commits after each step. No part runs `--ignored` tests or `--release`.
4. **Ownership check**, in the main checkout, for each part: `git diff --name-only "$BASE" "sw-t2-$p"`. The output must be a subset of the part's owned files (task table). Anything else: stop and report.
5. **Merge**, in the main checkout on the task branch, in the order a, b, c, d, e, f, g, h, i, j, each with `git merge --no-ff "sw-t2-$p" -m "merge(sw-t2): part $p …"` and the Co-Authored-By line. Owned files are disjoint, so every merge is conflict-free; a conflict means a part edited a file it does not own: stop and report.
6. **One integration run** on the merged tree with `CARGO_TARGET_DIR="$SWRUN/tgt-int"`: `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` (every `test result` line ok). The part tests all run together here for the first time, because each part's tests use T1 symbols only; a failure that appears only after the merge is fixed by the coordinator in the owning file, in a commit naming the part.
7. **Verify and close.** Ten read-only verifiers, one per part, each read `git diff "$BASE" "sw-t2-$p"` against that part's section and report findings without editing. Complete T2 with the evidence; remove worktrees, branches and every `tgt-t2-*` directory. Keep `$SWRUN/tgt-int`.

**How T3 runs.** One agent, in the main checkout on the task branch, after T2 is complete, with `CARGO_TARGET_DIR="$SWRUN/tgt-int"`. Stages 1, 2, 3 and 4 run in order, each ending with its own commit. Stage 2's tests are added to `src/app/probe.rs`; a failure found there is fixed in the file that caused it, in a separate commit naming the T2 part that owns it. Stage 3 is the only release build and runs only the ignored timing test by name; stage 4 runs every ignored test (the five port-binding network tests included) once, after stage 3 has finished. Stage 3 needs a visible window for the motion timing, so if the screen is locked or no display is available, the agent records "unmeasured: <reason>" instead of a number and does not claim the timing done-when. Complete T3 with each stage's output as evidence.

**File-ownership matrix.**

| File | T1 owner | T2 owner | T3 |
|---|---|---|---|
| `src/main.rs` | Step 0 | none (frozen) | clean-up only |
| `src/colors.rs` | part b | none (frozen) | stage 1 (removes `ExplorerColors`, adds the motion colours and ink sets) |
| `src/app/theme.rs` | part b | part a | stage 1 |
| `src/style/*` (new) | part b | none (frozen) | stage 1 (allowance removal) |
| `src/motion/*` (new) | part c | part j (internals only) | stage 1 |
| `src/app/layout.rs` | part a | part b | fix-ups |
| `src/app/header.rs` (new) | part a | part a | fix-ups |
| `src/app/mod.rs` | part a | none (frozen) | stage 1 |
| `src/app/probe.rs` (new, `cfg(test)`) | part a | none (frozen; parts write their probe tests in their own files) | stage 2 |
| `src/ui/mod.rs` | part a | none (frozen) | — |
| `src/ui/connection.rs` (new) | part a | part c | fix-ups |
| `src/ui/limits.rs` (new) | part a | part i | fix-ups |
| `src/ui/message_row.rs` (new) | part a | part i | fix-ups |
| `src/ui/topic_details.rs` (new) | part a | part e | fix-ups |
| `src/ui/topic_tree.rs` | part a (split only) | part d | fix-ups |
| `src/types/tree.rs` | — | part d | — |
| `src/ui/messages.rs` | part a (moves limits out) | part i | fix-ups |
| `src/ui/help.rs` | part a (consts, `help_link`) | part h | fix-ups |
| `src/ui/publish.rs` | — | part f | fix-ups |
| `src/ui/query.rs` | — | part g | fix-ups |
| `src/events/mod.rs` | — | part j | fix-ups |
| `src/types/message.rs` | part a | none (frozen) | stage 1 (removes `color()` methods) |
| `src/types/mod.rs` | part a (SW-T2 tags on the size constants only) | none | stage 1 (removes size constants) |
| `src/validation.rs` | part a | none (frozen) | — |

**Automatic check for every visual change.** Each row names the test or grep that fails if the change regresses. Tests marked (probe) render headless frames through `src/app/probe.rs`.

| Visual change | Automatic check | Where |
|---|---|---|
| Palette text and boundary contrast, both themes | `palette_text_pairs_meet_aa`, `palette_boundaries_meet_3_to_1` (WCAG 2.x formula) | T1 b1 |
| Installed visuals match the palette under either OS appearance | `visuals_are_complete`, `installed_visuals_follow_theme`; T3 `installed_contrast_meets_wcag` | T1 b2, T3 |
| Focus ring colour ≥ 3:1 | `focus_token_meets_3_to_1`; (probe) `tab_focus_shows_ring`, `focused_row_shows_ring_not_latch`, `expander_focus_shows_ring`, T3 `keyboard_pass_rings_every_stop` | T1 b2, T2 b, d, T3 |
| Tofu glyphs gone | `ui_glyphs_render` (`has_glyphs` after one pass); T3 `no_tofu_in_ui_strings` greps every string literal | T1 b3, T3 |
| Type sizes, one role per size | `text_styles_follow_floor`; T3 stage 1 grep for `TEXT_SMALL_SIZE` and `HEADING_LARGE_SIZE` | T1 b3, T3 |
| Key heights and 24 pt targets | `key_tiers_follow_user_heights`; (probe) T3 `targets_are_at_least_24` via AccessKit bounds | T1 b5, T3 |
| Three key roles in greyscale | `key_roles_differ_in_greyscale` | T1 b5 |
| Badges not red, not a key fill, legible | `badge_inks_meet_aa`, (probe) `history_rows_are_one_line`, `messages_rows_are_one_line` | T1 b5, T2 e, i |
| JSON token colours | `json_tokens_meet_aa`, `json_job_colours_tokens` | T1 b6 |
| Header slots, strip, tabs, Query key, Publish key, tree rows do not move | (probe) part tests plus T3 `landmarks_are_stable` | T2 a, b, d, f, g; T3 |
| Status not by hue alone | `status_mark_rules`, (probe) `header_has_no_tofu_dot` | T2 a |
| Motion colours, schedule, band confinement | `seat_reproduces_t9_hex`, `side_schedule_matches_t9`, `bevel_stays_in_band`; T3 `motion_reveal_matches_t9` | T1 c, T3 |
| Motion inks on the grey dark base | `motion_inks_by_theme`, `mesh_cache_reuses` (one mesh per theme); T3 `motion_inks_suit_each_base` | T1 c, T3 |
| Reduced motion fully static | `reduced_is_fully_static`, `reduced_motion_is_static`; (probe) `reduce_motion_toggle_in_header`, T3 `reduced_motion_is_static_in_app` | T1 c, T2 a, T3 |
| Header fixed slots at 720 pt and 1000 pt: no wrap, clip or overflow | `slot_budget_fits_720`, (probe) `header_fits_at_720_and_1000`, `header_slots_do_not_move` | T2 a |
| Motion never lies | part j binding tests | T2 j |
| Expander untouched | brace-matched text compare (`$SWRUN/fn_body.py`) and `expander_untouched` | T2 d, T3 |

## P3, P4 and P5 absorption notes

P3 (`docs/superpowers/plans/2026-09-25-p3-egui-036-port.md`) is not accepted yet and runs after this plan. These are the amendments P3 needs so that it keeps Snow White's work instead of reverting it. This plan does not edit the P3 file (one kind of change per plan); Open question 14 asks whether to amend P3 now.

1. **P3 T1 (mechanical 0.36 port).** `Painter::rect_stroke` gains a `StrokeKind` argument (egui 0.36.2 `painter.rs:406-414`): the call sites are `src/style/focus.rs`, `src/style/keys.rs`, `src/style/badge.rs` and `src/app/layout.rs` (strip). `Rounding` becomes `CornerRadius` in `src/style/*`. The catcher's `Sense { click: true, drag: false, focusable: false }` literal in `src/ui/topic_tree.rs` becomes `Sense::CLICK`. `ZenohExplorer::frame_ui(ctx)` is ported into 0.36's `App::ui`; `update` already only calls it. `src/app/probe.rs` reads `accesskit` nodes by `name()` and `bounds()`, which keep their names in the accesskit version 0.36 uses. P3 T1's owns-list gains `src/style/*`, `src/motion/*`, `src/app/header.rs`, `src/app/probe.rs`, `src/ui/connection.rs`, `src/ui/limits.rs`, `src/ui/message_row.rs`, `src/ui/topic_details.rs`.
2. **P3 T4 (theme installation).** Theme installation already happens once through `crate::style::install` with `set_visuals_of` and `style_mut_of` (both exist in 0.36.2, `context.rs:2267, 2237`) and `apply_theme` only calls `ctx.set_theme`. P3 T4 ports that, keeps every value, keeps the name `install`, and its `src/ui/tests/shell.rs` asserts name `LIGHT.panel`, `LIGHT.text` and `DARK.panel` (the neutral-grey dark palette, #484848) instead of `ExplorerColors::CARD_BACKGROUND`, `TEXT_PRIMARY` and `DARK_CARD_BACKGROUND` (P3 plan around lines 1195–1215). `override_text_color == Some(palette.text)` still holds. **Rebase (structural split, user decision):** P3 T4 owns `layout.rs`, `theme.rs` and `mod.rs`, but after T1 part a the header code it edits lives in `src/app/header.rs`, the connection form in `src/ui/connection.rs` and the limits in `src/ui/limits.rs`; P3 T4 is rebased onto those files and its owns-list gains `src/app/header.rs` and `src/style/*`.
3. **P3 T5 (settings).** `dark_mode` now defaults to `false` (ivory); "🌙 Dark" selects the neutral-grey `DARK` palette, and P3 T5 persists `dark_mode` as it plans. The header's "Reduce motion" toggle (T2 part a) is not persisted and starts off at each launch (user decision, parked until P3 Settings). **Required P3 addition:** P3 T5 adds a `reduce_motion: bool` setting (default `false`), applies it at startup with `self.motion.set_reduced(value, Instant::now())`, and saves it when the header toggle changes; the toggle stays in the header.
4. **P3 T7 (minimum window 720×480): required amendment.** The header already fits 720 pt with Snow White's fixed-width slots (T2 part a, user decision): long readouts are truncated inside their slot with the full text on hover, and nothing wraps. P3 T7's `header_fits_at_720` and its wrapping status group must be replaced: the test checks the fixed slots instead, i.e. at 720 pt every header control and readout lies inside the window, no two overlap, no text runs past the window edge, and the header band's bottom (the statusGlass rect that `show_header` records under `HEADER_GLASS_ID`,; not the tree heading, which the "Active:" list moves down) is the same in every state, so there is no second, wrapped row. Only texts and controls inside that band are checked. T2 part a's `header_fits_at_720_and_1000` is that test; P3 T7 keeps it, adds no wrapping code, and only sets the window minimum.
5. **P3 T9/T10 (virtualized tree).** `row_height` for `ScrollArea::show_rows` must be `crate::style::focus::TREE_ROW_HEIGHT` (24) with `item_spacing.y = 0` on the tree `Ui`, not `interact_size.y` (P3 plan around lines 2246–2258). `show_tree_row` must carry the row body of T2 part d verbatim: the fixed-height row, the catcher, the leaf placeholder, the ring, the accent bar, `LeafKind`, the painted local marker, the `LayoutJob` label with match emphasis and the motion registration. Both `default_open = filtering` sites (P3 plan around lines 2222–2228 and 2308–2320) become `filtering && filter_default_open(path, filter_lower)`.
6. **P3 T11 (accessibility).** Its replacement snippets for the search row and the Unsubscribe button (P3 plan around lines 2468–2522) must not bring back `ui.label("🔍")`, `ui.button("✖")` or `ui.small_button("✖")`; it adds `widget_info` to the worded buttons ("Clear filter", "Unsubscribe") that T2 part d leaves. `keep_focus_in_view`'s edge can use `TREE_ROW_HEIGHT`.
7. **P3 T12 (import).** `src/ui/publish.rs` keeps `preview_caption`, the fixed status slot and the inline Import message line (T2 part f); P3's `import_label_uses_format_size` test points at `preview_caption`. A failed import sets the inline line and, per P3, also a `UiAlert::Error` (Open question 5).
8. **P3 T13 (save jobs).** `save_topic_to_file` now lives in `src/ui/topic_details.rs` (T1 part a). A running save adds the reason "Saving…" through the same visible-reason label (T2 part e).
9. **P3 T14 (limits and query labels).** The three limits and the dedup switch are in `src/ui/limits.rs`, shown in the header popover; P3 T14 converts them there (its owns-list gains `src/ui/limits.rs`; `messages_toolbar_fits_at_720` and `limits_are_drag_values` retarget). The query timeout is already a `DragValue` (`Role::SpinButton`), so P3 T14's text at plan lines 3141 and 3291 must put `labelled_by` on the `DragValue` response. P3 plan line 57 ("CP-A1 T5 keeps the limits-row part") now reads "CP-A3 T6".
10. **P3 T3 and T16.** kittest can replace `src/app/probe.rs`; the probe uses no eframe `Frame` and may also stay. Snapshots are refreshed with `UPDATE_SNAPSHOTS=1` after this plan.
11. **P4 T8/T9.** Transfer rows use `LeafKind::Transfer`; the transfer-completion motion response (CP-C T14's remainder) calls `Motion::value_changed` on the transfer row when its status reaches `Verified`.
12. **P5.** T1 extends the existing `src/ui/connection.rs`; T23 builds the profile panel inside the Connection view and calls `start_connect`, never a new Connect button; T24's Cmd+digit order follows `DetailView::ALL` (five views) and Esc targets the strip's "Dismiss"; T16's run header takes the place of the Results header text from T2 part g and binds the motion reveal to `Reply { id }` / `Finished { id }`; T20 keyboard selection calls the same select path as a click so the ring and bar follow.

---

## Task T1: Foundation

Owns, per part: see the task table. Step 0, the worktrees, merges and integration are under "How T1 runs". Stage 1 runs parts b and c in parallel; stage 2 runs part a on the merged stage-1 tree.

### Part b (was CP-B T1, T2, T3 values, T4, T6 helpers, T7, T8 table, T9, T11 values, T13 painter, T14 and T15 frames; CP-A1 T1 helpers): theme core

**Files:**
- Modify: `src/colors.rs` (add `Palette`, `LIGHT`, `DARK`, `palette`, `contrast_ratio`, `relative_luminance`, `with_alpha`; `ExplorerColors` stays until T3 stage 1)
- Modify: `src/app/theme.rs` (`apply_theme` becomes install-once plus `set_theme`; the getters read the palette)
- Modify: `src/style/mod.rs` (Step 0 stub)
- Create: `src/style/visuals.rs`, `src/style/text.rs`, `src/style/fonts.rs`, `src/style/focus.rs`, `src/style/keys.rs`, `src/style/badge.rs`, `src/style/glass.rs`, `src/style/content.rs`

**Interfaces:**
- Consumes: `crate::types::{MessageType, ConnectionStatus}` (unchanged), egui 0.29.1 `Context::{set_visuals_of, style_mut_of, set_theme, set_fonts}`.
- Produces (frozen for T2; signatures exact):
  - `crate::colors::{Palette, LIGHT, DARK}`, `pub fn palette(dark: bool) -> &'static Palette`, `pub fn contrast_ratio(a: Color32, b: Color32) -> f32`, `pub fn relative_luminance(c: Color32) -> f32`, `pub fn with_alpha(c: Color32, alpha: f32) -> Color32`
  - `crate::style::{p, install}`: `pub fn p(ui: &egui::Ui) -> &'static Palette`, `pub fn install(ctx: &egui::Context)`
  - `crate::style::visuals::visuals_for(p: &Palette, dark: bool) -> egui::Visuals`
  - `crate::style::text::{BODY, BUTTON, SMALL, MONO, HEADING, LABEL, LEGEND, PARAGRAPH_LINE_HEIGHT, legend, label_style, text_styles, label, legend_text, content, small, heading, paragraph}`
  - `crate::style::fonts::{font_definitions, UI_GLYPHS}`
  - `crate::style::focus::{MIN_TARGET, TREE_ROW_HEIGHT, RING_WIDTH, RING_OUTSET, track_modality, keyboard_nav, keyboard_focused, focus_ring_color, ring_rect, paint_focus_ring}`
  - `crate::style::keys::{KEY_RADIUS, KeyTier, KeyRole, KeyFace, KeyResponse, key_face, key, latched_label, latched_text_color}`
  - `crate::style::badge::{badge_ink, badge, status_ink}`
  - `crate::style::glass::{Faced, FACE_RADIUS, face_frame, status_glass_frame, content_glass_frame, face, status_glass, content_glass}`
  - `crate::style::content::{json_job, hex_ascii_rows}`
  - `ZenohExplorer::apply_theme(&self, ctx: &egui::Context)` keeps its signature (called once per frame by `frame_ui`/`update`).

- [ ] **Step b1-1: Write the failing palette tests.** Append to `src/colors.rs`:

```rust
#[cfg(test)]
mod palette_tests {
    use super::*;

    fn text_pairs(p: &Palette) -> Vec<(&'static str, Color32, Color32)> {
        vec![
            ("text/panel", p.text, p.panel),
            ("text/chassis", p.text, p.chassis),
            ("text/field", p.text, p.field),
            ("text_secondary/panel", p.text_secondary, p.panel),
            ("text_secondary/chassis", p.text_secondary, p.chassis),
            ("text_secondary/field", p.text_secondary, p.field),
            ("text/key", p.text, p.key),
            ("text/key_hover", p.text, p.key_hover),
            ("text/key_pressed", p.text, p.key_pressed),
            ("key_text/key_primary", p.key_text, p.key_primary),
            ("key_text/key_primary_hover", p.key_text, p.key_primary_hover),
            ("key_text/key_primary_pressed", p.key_text, p.key_primary_pressed),
            ("key_text/action", p.key_text, p.action),
            ("selected_text/selected", p.selected_text, p.selected),
            ("text/selection_tint", p.text, p.selection_tint),
            ("ok/panel", p.ok, p.panel),
            ("ok/chassis", p.ok, p.chassis),
            ("ok/field", p.ok, p.field),
            ("warn/panel", p.warn, p.panel),
            ("warn/chassis", p.warn, p.chassis),
            ("err/panel", p.err, p.panel),
            ("err/chassis", p.err, p.chassis),
            ("err/field", p.err, p.field),
            ("status_text/status_glass", p.status_text, p.status_glass),
            ("status_warn/status_glass", p.status_warn, p.status_glass),
            ("status_err/status_glass", p.status_err, p.status_glass),
            ("content_text/content_glass", p.content_text, p.content_glass),
            ("content_secondary/content_glass", p.content_secondary, p.content_glass),
            ("json_key/content_glass", p.json_key, p.content_glass),
            ("json_string/content_glass", p.json_string, p.content_glass),
            ("json_number/content_glass", p.json_number, p.content_glass),
            ("json_literal/content_glass", p.json_literal, p.content_glass),
            ("badge_sub/panel", p.badge_sub, p.panel),
            ("badge_put/panel", p.badge_put, p.panel),
            ("badge_get/panel", p.badge_get, p.panel),
            ("badge_reply/panel", p.badge_reply, p.panel),
            ("badge_sub/chassis", p.badge_sub, p.chassis),
            ("badge_put/chassis", p.badge_put, p.chassis),
            ("badge_get/chassis", p.badge_get, p.chassis),
            ("badge_reply/chassis", p.badge_reply, p.chassis),
        ]
    }

    fn boundary_pairs(p: &Palette) -> Vec<(&'static str, Color32, Color32)> {
        vec![
            ("focus/panel", p.focus, p.panel),
            ("focus/field", p.focus, p.field),
            ("focus/chassis", p.focus, p.chassis),
            ("rim/panel", p.rim, p.panel),
            ("rim/field", p.rim, p.field),
            ("rim/chassis", p.rim, p.chassis),
            ("selected_bar/panel", p.selected_bar, p.panel),
            ("selected_bar/field", p.selected_bar, p.field),
            ("selected_rim/panel", p.selected_rim, p.panel),
            ("key_primary_rim/panel", p.key_primary_rim, p.panel),
            ("leader/panel", p.leader, p.panel),
        ]
    }

    #[test]
    fn contrast_ratio_matches_review_numbers() {
        let r = contrast_ratio(Color32::from_rgb(0xff, 0xf4, 0xdf), Color32::from_rgb(0x49, 0x5e, 0x4e));
        assert!((r - 6.43).abs() < 0.01, "{r}");
        let r = contrast_ratio(Color32::from_rgb(0x1c, 0x1c, 0x1e), Color32::from_rgb(0x00, 0x7a, 0xff));
        assert!((r - 4.24).abs() < 0.01, "{r}");
    }

    #[test]
    fn palette_text_pairs_meet_aa() {
        for (name, p) in [("LIGHT", &LIGHT), ("DARK", &DARK)] {
            for (pair, fg, bg) in text_pairs(p) {
                let r = contrast_ratio(fg, bg);
                assert!(r >= 4.5, "{name} {pair}: {r:.2}:1");
            }
        }
    }

    #[test]
    fn palette_boundaries_meet_3_to_1() {
        for (name, p) in [("LIGHT", &LIGHT), ("DARK", &DARK)] {
            for (pair, fg, bg) in boundary_pairs(p) {
                let r = contrast_ratio(fg, bg);
                assert!(r >= 3.0, "{name} {pair}: {r:.2}:1");
            }
        }
    }

    #[test]
    fn reply_is_not_error_red_and_selection_is_opaque() {
        for p in [&LIGHT, &DARK] {
            assert_ne!(p.badge_reply, p.err);
            assert_eq!(p.selected.a(), 255);
            assert_eq!(p.selection_tint.a(), 255);
        }
    }
}
```

Run: `cargo test --locked colors::palette_tests 2>&1 | tail -n 5`
Expected: compile error `cannot find type Palette` (red).

- [ ] **Step b1-2: Add the palette.** Insert into `src/colors.rs` after the `ExplorerColors` impl (before the test module). `LIGHT` values are the review's "Token proposal" and "Contrast: proposed" tables (rows marked [derived] there, and the ivory selection tint derived for this plan). `DARK` follows the user's decision on dark mode: the review's near-black graphite surfaces are replaced by lighter **neutral greys** (every surface, rim, text and leader value has R = G = B), while the olive keys, rust action and latch, glass displays and JSON colours keep the review's values. Its greys, focus, status inks, badge inks and selection tint are derived for this plan. Every value was checked with the WCAG formula before writing; the lowest `DARK` ratios are:

| `DARK` pair | Ratio | Needed |
|---|---|---|
| text #f5f5f5 / panel #484848 (chassis #3c3c3c, field #333333) | 8.39 (10.12, 11.59) | 4.5 |
| text_secondary #d2d2d2 / panel | 6.05 | 4.5 |
| key_text #fff4df / key_primary_hover #64735b | 4.65 | 4.5 |
| err #f7b0ae / panel | 5.13 | 4.5 |
| badge_put #f0b894 / panel (badge_get 5.51, badge_reply 5.54, badge_sub 5.73) | 5.22 | 4.5 |
| rim and leader #ababab / panel | 3.98 | 3.0 |
| focus #e8a878 / panel (field 6.20, chassis 5.41) | 4.49 | 3.0 |
| selected_rim and selected_bar #e0e0e0 / panel | 6.93 | 3.0 |
| key_primary_rim #ececec / panel | 7.74 | 3.0 |
| greyscale key roles: neutral/primary, neutral/disabled, primary/disabled | 1.94, 2.78, 5.41 | 1.5 |

The rust latch fill #9c5539 is 1.64:1 on the grey panel, which is why the latch carries the #e0e0e0 `selected_rim` (the review's own rule for dark mode); `selected_text` on it is 5.11:1.

```rust
/// Semantic colours of one theme. Every colour the UI paints comes from a
/// `Palette`; `LIGHT` (Snow White ivory) is the default, `DARK` (neutral greys) the opt-in.
#[allow(dead_code)] // SW-T2: `ok`, `selected_bar`, `leader` are first read in T2 parts b and d; `content_secondary` has no reader yet (T3 stage 1 Step 3 deletes it and its test row if clippy still reports it)
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Palette {
    /// Window background and gutters (`panel_fill`).
    pub chassis: Color32,
    /// Module faces.
    pub panel: Color32,
    /// Text-field recess (`extreme_bg_color`).
    pub field: Color32,
    /// Decorative separators and disabled-key outlines.
    pub seam: Color32,
    /// Input and neutral-key rims (>= 3:1 on panel, field and chassis).
    pub rim: Color32,
    pub text: Color32,
    /// Secondary and former tertiary text (AA on panel, chassis and field).
    pub text_secondary: Color32,
    pub key: Color32,
    pub key_hover: Color32,
    pub key_pressed: Color32,
    pub key_primary: Color32,
    pub key_primary_hover: Color32,
    pub key_primary_pressed: Color32,
    pub key_primary_rim: Color32,
    pub key_text: Color32,
    /// The one action key (Save File).
    pub action: Color32,
    /// Latched tab and tree-row fill, its text and its rim.
    pub selected: Color32,
    pub selected_text: Color32,
    pub selected_rim: Color32,
    /// The 3 pt bar on the selected tree row and the progress fill.
    pub selected_bar: Color32,
    /// `selection.bg_fill`: text selection inside fields.
    pub selection_tint: Color32,
    /// Focus ring, focused field frame, `widgets.active.bg_stroke`.
    pub focus: Color32,
    pub ok: Color32,
    pub warn: Color32,
    pub err: Color32,
    pub status_glass: Color32,
    pub status_text: Color32,
    pub status_warn: Color32,
    pub status_err: Color32,
    pub content_glass: Color32,
    pub content_text: Color32,
    pub content_secondary: Color32,
    pub json_key: Color32,
    pub json_string: Color32,
    pub json_number: Color32,
    pub json_literal: Color32,
    /// Message-type legend inks (text and outline, no fill).
    pub badge_sub: Color32,
    pub badge_put: Color32,
    pub badge_get: Color32,
    pub badge_reply: Color32,
    /// Tree leader lines (>= 3:1).
    pub leader: Color32,
}

const fn hex(rgb: u32) -> Color32 {
    Color32::from_rgb((rgb >> 16) as u8, (rgb >> 8) as u8, rgb as u8)
}

/// Snow White ivory.
pub const LIGHT: Palette = Palette {
    chassis: hex(0xe9e6dc),
    panel: hex(0xf3f0e7),
    field: hex(0xfbf9f3),
    seam: hex(0xbbbdb1),
    rim: hex(0x5a6353),
    text: hex(0x333a35),
    text_secondary: hex(0x5a6353),
    key: hex(0xfbf9f3),
    key_hover: hex(0xeeeadf),
    key_pressed: hex(0xe3dfd2),
    key_primary: hex(0x495e4e),
    key_primary_hover: hex(0x64735b),
    key_primary_pressed: hex(0x4e5c46),
    key_primary_rim: hex(0x495e4e),
    key_text: hex(0xfff4df),
    action: hex(0x94532f),
    selected: hex(0x9c5539),
    selected_text: hex(0xfff4df),
    selected_rim: hex(0x9c5539),
    selected_bar: hex(0x9c5539),
    selection_tint: hex(0xecd3c3),
    focus: hex(0xae5339),
    ok: hex(0x3f6a3c),
    warn: hex(0x855412),
    err: hex(0x9e3b2b),
    status_glass: hex(0x172b24),
    status_text: hex(0xcee2b4),
    status_warn: hex(0xffd07f),
    status_err: hex(0xee9b99),
    content_glass: hex(0x090f38),
    content_text: hex(0xfff1da),
    content_secondary: hex(0xbfc2e9),
    json_key: hex(0x92d48d),
    json_string: hex(0xffd07f),
    json_number: hex(0xee9b99),
    json_literal: hex(0xbfc2e9),
    badge_sub: hex(0x495e4e),
    badge_put: hex(0x94532f),
    badge_get: hex(0x445dcc),
    badge_reply: hex(0x7b53ad),
    leader: hex(0x5a6353),
};

/// Neutral grey, the opt-in dark variant (user decision): lighter than the
/// review's near-black graphite, and no warm tint in its surfaces.
pub const DARK: Palette = Palette {
    chassis: hex(0x3c3c3c),
    panel: hex(0x484848),
    field: hex(0x333333),
    seam: hex(0x5f5f5f),
    rim: hex(0xababab),
    text: hex(0xf5f5f5),
    text_secondary: hex(0xd2d2d2),
    key: hex(0x484848),
    key_hover: hex(0x535353),
    key_pressed: hex(0x3d3d3d),
    key_primary: hex(0x495e4e),
    key_primary_hover: hex(0x64735b),
    key_primary_pressed: hex(0x4e5c46),
    key_primary_rim: hex(0xececec),
    key_text: hex(0xfff4df),
    action: hex(0x94532f),
    selected: hex(0x9c5539),
    selected_text: hex(0xfff4df),
    selected_rim: hex(0xe0e0e0),
    selected_bar: hex(0xe0e0e0),
    selection_tint: hex(0x66473a),
    focus: hex(0xe8a878),
    ok: hex(0x9edc99),
    warn: hex(0xffd07f),
    err: hex(0xf7b0ae),
    status_glass: hex(0x172b24),
    status_text: hex(0xcee2b4),
    status_warn: hex(0xffd07f),
    status_err: hex(0xee9b99),
    content_glass: hex(0x090f38),
    content_text: hex(0xfff1da),
    content_secondary: hex(0xbfc2e9),
    json_key: hex(0x92d48d),
    json_string: hex(0xffd07f),
    json_number: hex(0xee9b99),
    json_literal: hex(0xbfc2e9),
    badge_sub: hex(0x9edc99),
    badge_put: hex(0xf0b894),
    badge_get: hex(0xbcc6ff),
    badge_reply: hex(0xd9bff5),
    leader: hex(0xababab),
};

/// The palette of the current theme.
pub fn palette(dark: bool) -> &'static Palette {
    if dark {
        &DARK
    } else {
        &LIGHT
    }
}

/// WCAG 2.x relative luminance of an opaque colour. Test-only: every caller
/// (palette, visuals, keys, badge, content and T3 contrast tests) is a test.
#[cfg(test)]
pub fn relative_luminance(c: Color32) -> f32 {
    fn channel(v: u8) -> f32 {
        let c = v as f32 / 255.0;
        if c <= 0.04045 {
            c / 12.92
        } else {
            ((c + 0.055) / 1.055).powf(2.4)
        }
    }
    0.2126 * channel(c.r()) + 0.7152 * channel(c.g()) + 0.0722 * channel(c.b())
}

/// WCAG 2.x contrast ratio, 1.0 to 21.0. Test-only, like `relative_luminance`.
#[cfg(test)]
pub fn contrast_ratio(a: Color32, b: Color32) -> f32 {
    let (la, lb) = (relative_luminance(a), relative_luminance(b));
    (la.max(lb) + 0.05) / (la.min(lb) + 0.05)
}

/// CSS-style alpha: premultiplied in gamma space.
#[allow(dead_code)] // SW-T2: first caller is motion::spec::layer in T3 stage 1 Step 1
pub fn with_alpha(c: Color32, alpha: f32) -> Color32 {
    Color32::from_rgb(c.r(), c.g(), c.b()).gamma_multiply(alpha.clamp(0.0, 1.0))
}
```

Run: `cargo test --locked colors:: 2>&1 | grep -E '^test |test result'`
Expected: `contrast_ratio_matches_review_numbers`, `palette_text_pairs_meet_aa`, `palette_boundaries_meet_3_to_1`, `reply_is_not_error_red_and_selection_is_opaque` ok.

Commit: `git add src/colors.rs && git commit -m "feat(sw-b1): Snow White palette with WCAG tests (was CP-B T1, T3, T9)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`

- [ ] **Step b2-1: Failing visuals tests.** Create `src/style/visuals.rs` with the test module first:

```rust
//! Two complete `Visuals`, one per theme, built from a `Palette` (was CP-B T2, T4).

use egui::{Color32, Stroke, Visuals};

use crate::colors::Palette;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::colors::{DARK, LIGHT};

    #[test]
    fn visuals_are_complete() {
        for (p, dark) in [(&LIGHT, false), (&DARK, true)] {
            let v = visuals_for(p, dark);
            assert_eq!(v.dark_mode, dark);
            assert_eq!(v.override_text_color, Some(p.text));
            assert_eq!(v.panel_fill, p.chassis);
            assert_eq!(v.window_fill, p.panel);
            assert_eq!(v.extreme_bg_color, p.field);
            assert_eq!(v.faint_bg_color, p.chassis);
            assert_eq!(v.code_bg_color, p.content_glass);
            assert_eq!(v.selection.bg_fill, p.selection_tint);
            assert_eq!(v.selection.stroke, Stroke::new(1.5, p.focus));
            assert_eq!(v.text_cursor.stroke.color, p.text);
            assert_eq!(v.widgets.inactive.bg_stroke, Stroke::new(1.0, p.rim));
            assert_eq!(v.widgets.hovered.bg_stroke, Stroke::new(1.0, p.rim));
            assert_eq!(v.widgets.active.bg_stroke, Stroke::new(1.5, p.focus));
            assert_eq!(v.widgets.inactive.weak_bg_fill, p.key);
            assert_eq!(v.widgets.hovered.weak_bg_fill, p.key_hover);
            assert_eq!(v.widgets.active.weak_bg_fill, p.key_pressed);
            assert_eq!(v.widgets.noninteractive.weak_bg_fill, Color32::TRANSPARENT);
            assert_eq!(v.widgets.noninteractive.bg_stroke, Stroke::new(1.0, p.seam));
            for w in [&v.widgets.noninteractive, &v.widgets.inactive, &v.widgets.hovered, &v.widgets.active, &v.widgets.open] {
                assert_eq!(w.fg_stroke.color, p.text);
            }
        }
    }

    #[test]
    fn focus_token_meets_3_to_1() {
        for (p, dark) in [(&LIGHT, false), (&DARK, true)] {
            let v = visuals_for(p, dark);
            for bg in [p.panel, p.field, p.chassis] {
                assert!(crate::colors::contrast_ratio(v.widgets.active.bg_stroke.color, bg) >= 3.0);
                assert!(crate::colors::contrast_ratio(v.selection.stroke.color, bg) >= 3.0);
            }
        }
    }
}
```

Run: `cargo test --locked style::visuals 2>&1 | tail -n 3`
Expected: compile error `cannot find function visuals_for` (red). (Add `pub mod visuals;` to `src/style/mod.rs` first, see b2-3.)

- [ ] **Step b2-2: Implement `visuals_for`.** Insert above the test module:

```rust
/// The complete visuals of one theme. Every palette-backed field is set, so
/// the OS appearance cannot leak an egui default into either theme.
pub fn visuals_for(p: &Palette, dark: bool) -> Visuals {
    let mut v = if dark { Visuals::dark() } else { Visuals::light() };
    v.override_text_color = Some(p.text);
    v.panel_fill = p.chassis;
    v.window_fill = p.panel;
    v.faint_bg_color = p.chassis;
    v.extreme_bg_color = p.field;
    v.code_bg_color = p.content_glass;
    v.hyperlink_color = p.focus;
    v.warn_fg_color = p.warn;
    v.error_fg_color = p.err;
    v.window_stroke = Stroke::new(1.0, p.seam);
    // The focused text-field frame (F-T5-11); latched labels paint their own text colour.
    v.selection.bg_fill = p.selection_tint;
    v.selection.stroke = Stroke::new(1.5, p.focus);
    v.text_cursor.stroke = Stroke::new(2.0, p.text);

    let w = &mut v.widgets;
    w.noninteractive.bg_fill = p.panel;
    // Disabled keys fade to this fill: outline only (F-T7-3's shape).
    w.noninteractive.weak_bg_fill = Color32::TRANSPARENT;
    w.noninteractive.bg_stroke = Stroke::new(1.0, p.seam);
    w.noninteractive.fg_stroke = Stroke::new(1.0, p.text);

    w.inactive.bg_fill = p.field;
    w.inactive.weak_bg_fill = p.key;
    w.inactive.bg_stroke = Stroke::new(1.0, p.rim);
    w.inactive.fg_stroke = Stroke::new(1.0, p.text);

    w.hovered.bg_fill = p.field;
    w.hovered.weak_bg_fill = p.key_hover;
    w.hovered.bg_stroke = Stroke::new(1.0, p.rim);
    w.hovered.fg_stroke = Stroke::new(1.5, p.text);

    // `active` is both keyboard focus and press in 0.29 (style.rs:1072-1083).
    w.active.bg_fill = p.field;
    w.active.weak_bg_fill = p.key_pressed;
    w.active.bg_stroke = Stroke::new(1.5, p.focus);
    w.active.fg_stroke = Stroke::new(2.0, p.text);

    w.open.bg_fill = p.field;
    w.open.weak_bg_fill = p.key_hover;
    w.open.bg_stroke = Stroke::new(1.0, p.rim);
    w.open.fg_stroke = Stroke::new(1.0, p.text);
    v
}
```

- [ ] **Step b2-3: `style/mod.rs` with `install` and `p`.** Replace the Step 0 stub with:

```rust
//! Snow White style: palette-driven visuals, fonts, text styles and painted controls.
#![allow(dead_code)] // SW-T2: most helpers get their callers in T2

pub mod badge;
pub mod content;
pub mod focus;
pub mod fonts;
pub mod glass;
pub mod keys;
pub mod text;
pub mod visuals;

pub use crate::colors::{palette, Palette};

/// The palette of the theme `ui` is drawn in.
pub fn p(ui: &egui::Ui) -> &'static Palette {
    palette(ui.visuals().dark_mode)
}

/// Installs fonts, both themes' visuals, the text styles and the animation
/// time once per `Context`. Later calls return at once.
pub fn install(ctx: &egui::Context) {
    let id = egui::Id::new("sw_style_installed");
    if ctx.data(|d| d.get_temp::<bool>(id)).unwrap_or(false) {
        return;
    }
    ctx.set_fonts(fonts::font_definitions());
    ctx.set_visuals_of(egui::Theme::Light, visuals::visuals_for(&crate::colors::LIGHT, false));
    ctx.set_visuals_of(egui::Theme::Dark, visuals::visuals_for(&crate::colors::DARK, true));
    for theme in [egui::Theme::Light, egui::Theme::Dark] {
        ctx.style_mut_of(theme, |s| {
            s.animation_time = 0.001; // protected: the expander's motion (Q3)
            s.text_styles = text::text_styles();
        });
    }
    ctx.data_mut(|d| d.insert_temp(id, true));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_visuals_follow_theme() {
        let ctx = egui::Context::default();
        install(&ctx);
        ctx.set_theme(egui::Theme::Dark);
        let _ = ctx.run(egui::RawInput::default(), |_| {});
        assert_eq!(ctx.style().visuals, visuals::visuals_for(&crate::colors::DARK, true));
        ctx.set_theme(egui::Theme::Light);
        let _ = ctx.run(egui::RawInput::default(), |_| {});
        assert_eq!(ctx.style().visuals, visuals::visuals_for(&crate::colors::LIGHT, false));
        assert_eq!(ctx.style().animation_time, 0.001);
    }
}
```

Create the seven other files with only their `//!` line for now (`badge.rs`, `content.rs`, `focus.rs`, `fonts.rs`, `glass.rs`, `keys.rs`, `text.rs`), so the module compiles; b3–b6 fill them. Temporarily comment out `ctx.set_fonts(...)` and `s.text_styles = ...` until b3 lands, and restore them in b3.

- [ ] **Step b2-4: `apply_theme` and getters.** In `src/app/theme.rs` replace `apply_theme` (lines 18-87) with:

```rust
    /// Installs the Snow White style once and selects the theme for this frame.
    /// The per-frame `style_mut` of P1 is gone (F-T5-4).
    pub(crate) fn apply_theme(&self, ctx: &egui::Context) {
        crate::style::install(ctx);
        crate::style::focus::track_modality(ctx);
        let theme = if self.dark_mode {
            egui::Theme::Dark
        } else {
            egui::Theme::Light
        };
        if ctx.theme() != theme {
            ctx.set_theme(theme);
        }
    }
```

and change the getters to read the palette (the old constants stay in `colors.rs` until T3):

```rust
    #[allow(dead_code)] // SW-T2: T2 part b removes the last caller; T3 stage 1 deletes it
    pub(crate) fn background_color(&self) -> Color32 {
        crate::colors::palette(self.dark_mode).chassis
    }
    pub(crate) fn card_background_color(&self) -> Color32 {
        crate::colors::palette(self.dark_mode).panel
    }
    #[allow(dead_code)] // SW-T2: T2 removes the last callers; T3 stage 1 deletes it if unused
    pub(crate) fn text_color(&self) -> Color32 {
        crate::colors::palette(self.dark_mode).text
    }
    #[allow(dead_code)] // SW-T2: T2 removes the last callers; T3 stage 1 deletes it if unused
    pub(crate) fn text_secondary_color(&self) -> Color32 {
        crate::colors::palette(self.dark_mode).text_secondary
    }
    /// Merged into secondary (F-T5-8); removed in T3 stage 1.
    #[allow(dead_code)] // SW-T2: T2 removes the last callers; T3 stage 1 deletes it
    pub(crate) fn text_tertiary_color(&self) -> Color32 {
        crate::colors::palette(self.dark_mode).text_secondary
    }
```

Keep `#[allow(dead_code)]` on `card_background_color`, and keep `animate_fade_in` and `animate_pulse` unchanged (T2 part a deletes the pulse, T3 the fade), except that `animate_fade_in` gets `#[allow(dead_code)] // SW-T2: T2 part d removes its last callers; T3 stage 1 deletes it`. The getters above and `animate_fade_in` carry these tags because T2 parts do not own `theme.rs` (only part a does) and each part's own `-D warnings` check would otherwise fail when it removes a last caller; this is also why `MessageType::color`, `ConnectionStatus::color` and the `types/mod.rs` size constants are tagged in part a (Step a6). Remove the now unused `use crate::colors::ExplorerColors;` line.

Run: `cargo test --locked style:: 2>&1 | grep -E '^test |test result'; grep -rn 'style_mut(' src`
Expected: `visuals_are_complete`, `focus_token_meets_3_to_1`, `installed_visuals_follow_theme` ok; the grep prints nothing.

Commit b2 (`feat(sw-b2): complete visuals per theme, installed once (was CP-B T2, T4)`).

- [ ] **Step b3-1: Fonts and text styles, failing test first.** Write `src/style/fonts.rs`:

```rust
//! Font stack (was CP-B T7): Hack appended to Proportional so `●`, `→`, `▼`
//! render; `✓` is replaced by `✔` at its call sites (NotoEmoji has U+2714).

use egui::{FontDefinitions, FontFamily};

/// Every non-ASCII glyph the UI uses after T2. `no_tofu_in_ui_strings` (T3)
/// scans the sources for glyphs missing from this list.
pub const UI_GLYPHS: &str = "☀🌙📊📤🔍🔌❓⏵⏷▶⏸⬅✔●■▪📁🛠🔣📝📥💾⬇⏳📦→…·";

pub fn font_definitions() -> FontDefinitions {
    let mut fonts = FontDefinitions::default();
    fonts
        .families
        .get_mut(&FontFamily::Proportional)
        .expect("egui ships a Proportional family")
        .push("Hack".to_owned());
    fonts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ui_glyphs_render() {
        let ctx = egui::Context::default();
        crate::style::install(&ctx);
        let _ = ctx.run(egui::RawInput::default(), |_| {}); // fonts exist after one pass
        for c in UI_GLYPHS.chars() {
            let ok = ctx.fonts(|f| f.has_glyph(&egui::FontId::proportional(14.0), c));
            assert!(ok, "no glyph for {c:?} (U+{:04X})", c as u32);
        }
    }
}
```

Write `src/style/text.rs`:

```rust
//! Text styles defined once (was CP-B T8, T11, T12): sizes at the Snow White
//! floor, a `Label` style instead of `.strong()`, a mono `Legend` style.

use std::collections::BTreeMap;

use egui::{FontId, RichText, TextStyle};

use crate::colors::Palette;

pub const BODY: f32 = 16.0;
pub const BUTTON: f32 = 14.0;
pub const SMALL: f32 = 12.0;
pub const MONO: f32 = 13.0;
pub const HEADING: f32 = 22.0;
pub const LABEL: f32 = 14.0;
pub const LEGEND: f32 = 13.0;
/// Paragraph line height: 1.5 × body (type.bodyLineHeight).
pub const PARAGRAPH_LINE_HEIGHT: f32 = 24.0;

/// Key expressions, ids, locators, sizes, timestamps and badges.
pub fn legend() -> TextStyle {
    TextStyle::Name("Legend".into())
}

/// Field and section labels: distinct from their values by size and ink (F-T6-4).
pub fn label_style() -> TextStyle {
    TextStyle::Name("Label".into())
}

pub fn text_styles() -> BTreeMap<TextStyle, FontId> {
    [
        (TextStyle::Heading, FontId::proportional(HEADING)),
        (TextStyle::Body, FontId::proportional(BODY)),
        (TextStyle::Button, FontId::proportional(BUTTON)),
        (TextStyle::Small, FontId::proportional(SMALL)),
        (TextStyle::Monospace, FontId::monospace(MONO)),
        (legend(), FontId::monospace(LEGEND)),
        (label_style(), FontId::proportional(LABEL)),
    ]
    .into()
}

/// A field or section label.
pub fn label(s: impl Into<String>, p: &Palette) -> RichText {
    RichText::new(s).text_style(label_style()).color(p.text_secondary)
}

/// A key expression, locator, id, size or time.
pub fn legend_text(s: impl Into<String>) -> RichText {
    RichText::new(s).text_style(legend())
}

/// Every payload body (F-T6-7).
pub fn content(s: impl Into<String>) -> RichText {
    RichText::new(s).text_style(TextStyle::Monospace)
}

/// Metadata and hints: small, secondary, upright (F-T6-6 drops italics).
pub fn small(s: impl Into<String>, p: &Palette) -> RichText {
    RichText::new(s).text_style(TextStyle::Small).color(p.text_secondary)
}

pub fn heading(s: impl Into<String>) -> RichText {
    RichText::new(s).text_style(TextStyle::Heading)
}

/// Body text with paragraph line height.
pub fn paragraph(s: impl Into<String>) -> RichText {
    RichText::new(s).line_height(Some(PARAGRAPH_LINE_HEIGHT))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_styles_follow_floor() {
        let s = text_styles();
        assert_eq!(s[&TextStyle::Body].size, 16.0);
        assert_eq!(s[&TextStyle::Button].size, 14.0);
        assert_eq!(s[&TextStyle::Small].size, 12.0);
        assert_eq!(s[&TextStyle::Monospace].size, 13.0);
        assert!(s[&TextStyle::Heading].size >= 22.0);
        assert!(s[&TextStyle::Small].size < s[&TextStyle::Body].size, "F-T6-2");
        assert_eq!(s[&legend()].family, egui::FontFamily::Monospace);
        assert_eq!(s[&label_style()].family, egui::FontFamily::Proportional);
    }
}
```

Restore the two lines commented out in b2-3. Run: `cargo test --locked style:: 2>&1 | grep -E 'ui_glyphs_render|text_styles_follow_floor|test result'`
Expected: both ok. Commit b3 (`feat(sw-b3): fonts with Hack in Proportional and one text-style table (was CP-B T7, T8, T11, T12)`).

- [ ] **Step b4: Focus helpers (was CP-A1 T1 helper).** Write `src/style/focus.rs`:

```rust
//! Focus-only ring, keyboard-modality tracking and the target-size floor.

use egui::{vec2, Color32, Context, Event, Id, Key, Rect, Response, Stroke, Ui, Vec2, Visuals};

/// WCAG 2.5.8 minimum target (F-T4-10); no control is smaller.
pub const MIN_TARGET: Vec2 = vec2(24.0, 24.0);
/// Tree row pitch and height (F-T13-9): 24 pt with `item_spacing.y = 0`.
pub const TREE_ROW_HEIGHT: f32 = 24.0;
pub const RING_WIDTH: f32 = 2.0;
pub const RING_OUTSET: f32 = 2.0;

const NAV_ID: &str = "sw_keyboard_nav";

/// Remembers whether the last navigation input was the keyboard (Tab or an
/// arrow) or the pointer. Called once per frame by `apply_theme`.
pub fn track_modality(ctx: &Context) {
    let (keyboard, pointer) = ctx.input(|i| {
        let keyboard = i.events.iter().any(|e| {
            matches!(
                e,
                Event::Key {
                    key: Key::Tab | Key::ArrowUp | Key::ArrowDown | Key::ArrowLeft | Key::ArrowRight,
                    pressed: true,
                    ..
                }
            )
        });
        (keyboard, i.pointer.any_pressed())
    });
    if keyboard || pointer {
        ctx.data_mut(|d| d.insert_temp(Id::new(NAV_ID), keyboard && !pointer));
    }
}

pub fn keyboard_nav(ctx: &Context) -> bool {
    ctx.data(|d| d.get_temp::<bool>(Id::new(NAV_ID))).unwrap_or(false)
}

/// Focus reached by the keyboard, not a mouse press (egui-0.29.1 response.rs:282, 500).
pub fn keyboard_focused(r: &Response) -> bool {
    r.has_focus() && !r.is_pointer_button_down_on() && keyboard_nav(&r.ctx)
}

/// The ring reads `widgets.active.bg_stroke`, which `visuals_for` sets to the
/// `focus` token, so the ring recolours with the theme.
pub fn focus_ring_color(v: &Visuals) -> Color32 {
    v.widgets.active.bg_stroke.color
}

pub fn ring_rect(target: Rect, outset: f32) -> Rect {
    target.expand(outset)
}

/// A ring around `target`; a negative `outset` insets it (tree rows).
pub fn paint_focus_ring(ui: &Ui, target: Rect, outset: f32) {
    let color = focus_ring_color(ui.visuals());
    ui.painter()
        .rect_stroke(ring_rect(target, outset), 4.0, Stroke::new(RING_WIDTH, color));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_rect_outsets_and_insets() {
        let r = Rect::from_min_max(egui::pos2(10.0, 10.0), egui::pos2(30.0, 34.0));
        assert_eq!(ring_rect(r, 2.0), Rect::from_min_max(egui::pos2(8.0, 8.0), egui::pos2(32.0, 36.0)));
        assert_eq!(ring_rect(r, -1.0).height(), 22.0);
    }

    #[test]
    fn keyboard_focus_needs_keyboard_nav() {
        let ctx = Context::default();
        let id = Id::new("probe_button");
        let run = |events: Vec<Event>| {
            let mut focused = false;
            let _ = ctx.run(egui::RawInput { events, ..Default::default() }, |ctx| {
                track_modality(ctx);
                egui::CentralPanel::default().show(ctx, |ui| {
                    let r = ui.push_id(id, |ui| ui.button("x")).inner;
                    r.request_focus();
                    focused = keyboard_focused(&r);
                });
            });
            focused
        };
        assert!(!run(vec![]), "focus without keyboard navigation shows no ring");
        let tab = Event::Key {
            key: Key::Tab,
            physical_key: None,
            pressed: true,
            repeat: false,
            modifiers: egui::Modifiers::NONE,
        };
        let _ = run(vec![tab]);
        assert!(run(vec![]), "after Tab the focused button shows the ring");
    }
}
```

Run: `cargo test --locked style::focus 2>&1 | tail -n 3` — Expected: 2 passed. Commit b4 (`feat(sw-b4): focus-only ring helpers (was CP-A1 T1 helper)`).

- [ ] **Step b5-1: Keys, latched labels and badges — failing tests.** Write `src/style/keys.rs` with this test module first:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::colors::{contrast_ratio, DARK, LIGHT};

    #[test]
    fn key_tiers_follow_user_heights() {
        // User decision: 10 % under the mockup brief (44, 40, 36, 32), never under 24.
        assert_eq!(KeyTier::Hero.height(), 40.0);
        assert_eq!(KeyTier::Standard.height(), 36.0);
        assert_eq!(KeyTier::Compact.height(), 32.0);
        assert_eq!(KeyTier::Mini.height(), 29.0);
        for t in [KeyTier::Hero, KeyTier::Standard, KeyTier::Compact, KeyTier::Mini] {
            assert!(t.height() >= crate::style::focus::MIN_TARGET.y);
        }
        assert!(KeyTier::Standard.height() >= 34.0, "source keys carry the 8 pt band (F-T11-4)");
    }

    /// Greyscale: each pair of roles differs by >= 1.5:1 in fill or in rim.
    #[test]
    fn key_roles_differ_in_greyscale() {
        for p in [&LIGHT, &DARK] {
            let faces = [
                key_face(p, KeyRole::Neutral, true, false, false),
                key_face(p, KeyRole::Primary, true, false, false),
                key_face(p, KeyRole::Neutral, false, false, false),
            ];
            let fill = |f: &KeyFace| if f.fill == egui::Color32::TRANSPARENT { p.panel } else { f.fill };
            for (i, a) in faces.iter().enumerate() {
                for (j, b) in faces.iter().enumerate().skip(i + 1) {
                    let d = contrast_ratio(fill(a), fill(b)).max(contrast_ratio(a.rim.color, b.rim.color));
                    assert!(d >= 1.5, "roles {i} and {j}: {d:.2}");
                }
            }
        }
    }

    #[test]
    fn latched_is_distinct_from_focus() {
        for p in [&LIGHT, &DARK] {
            assert_ne!(p.selected, p.focus);
            assert!(contrast_ratio(latched_text_color(p, true), p.selected) >= 4.5);
        }
    }
}
```

and `src/style/badge.rs` with:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::colors::{DARK, LIGHT};

    #[test]
    fn badge_inks_meet_aa() {
        for p in [&LIGHT, &DARK] {
            for kind in [MessageType::Subscribe, MessageType::Publish, MessageType::Query, MessageType::QueryReply] {
                let ink = badge_ink(&kind, p);
                assert!(crate::colors::contrast_ratio(ink, p.panel) >= 4.5, "{kind:?}");
            }
            assert_ne!(badge_ink(&MessageType::QueryReply, p), p.err, "REPLY is not error red");
        }
    }
}
```

Run: `cargo test --locked style::keys style::badge 2>&1 | tail -n 3` — Expected: compile errors (red).

- [ ] **Step b5-2: Implement keys (was CP-B T6, T13; CP-C T16 hook).** Insert above the test module of `src/style/keys.rs`:

```rust
//! Caller-painted keys in four height tiers, latched labels for tabs and tree rows.

use egui::epaint::RectShape;
use egui::{vec2, Button, Color32, Painter, Response, RichText, Shape, Stroke, TextStyle, Ui, WidgetText};

use crate::colors::Palette;
use crate::style::focus::{self, MIN_TARGET, RING_OUTSET};

pub const KEY_RADIUS: f32 = 6.0;

/// Key heights, 10 % under the mockup brief (user decision).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyTier {
    /// 40 pt: the header connection key.
    Hero,
    /// 36 pt: source keys (Subscribe, Publish, Import File, Query, Save File).
    Standard,
    /// 32 pt: tabs, the theme selector, secondary keys.
    Compact,
    /// 29 pt: row and dismiss keys.
    Mini,
}

impl KeyTier {
    pub const fn height(self) -> f32 {
        match self {
            KeyTier::Hero => 40.0,
            KeyTier::Standard => 36.0,
            KeyTier::Compact => 32.0,
            KeyTier::Mini => 29.0,
        }
    }
}

/// Neutral by default; the olive accent only on a module's primary action;
/// rust only on Save File (F-T4-6).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyRole {
    Neutral,
    Primary,
    Action,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct KeyFace {
    pub fill: Color32,
    pub rim: Stroke,
    pub label: Color32,
}

/// The face of a key in one state. Disabled keys are outline only (F-T7-3).
pub fn key_face(p: &Palette, role: KeyRole, enabled: bool, hovered: bool, pressed: bool) -> KeyFace {
    if !enabled {
        return KeyFace {
            fill: Color32::TRANSPARENT,
            rim: Stroke::new(1.0, p.seam),
            label: p.text_secondary,
        };
    }
    let pick = |rest, hover, press| {
        if pressed {
            press
        } else if hovered {
            hover
        } else {
            rest
        }
    };
    match role {
        KeyRole::Neutral => KeyFace {
            fill: pick(p.key, p.key_hover, p.key_pressed),
            rim: Stroke::new(1.0, p.rim),
            label: p.text,
        },
        KeyRole::Primary => KeyFace {
            fill: pick(p.key_primary, p.key_primary_hover, p.key_primary_pressed),
            rim: Stroke::new(1.0, p.key_primary_rim),
            label: p.key_text,
        },
        KeyRole::Action => KeyFace {
            fill: p.action,
            rim: Stroke::new(if hovered || pressed { 1.5 } else { 1.0 }, p.rim),
            label: p.key_text,
        },
    }
}

/// A painted key and the slot the motion bevel paints into.
pub struct KeyResponse {
    pub response: Response,
    pub painter: Painter,
    pub bevel_slot: egui::layers::ShapeIdx,
}

/// A frameless `Button` whose face and bevel slots are reserved before
/// `ui.add` (F-T4-12, T10 (a) Z-order). `motion_owned` skips the key's own
/// rest shadow while the motion effect paints its whole stack (CP-C T16).
pub fn key(
    ui: &mut Ui,
    text: impl Into<String>,
    tier: KeyTier,
    role: KeyRole,
    enabled: bool,
    motion_owned: bool,
) -> KeyResponse {
    let p = crate::style::p(ui);
    let face_slot = ui.painter().add(Shape::Noop);
    let bevel_slot = ui.painter().add(Shape::Noop);
    let label = key_face(p, role, enabled, false, false).label;
    let button = Button::new(RichText::new(text).text_style(TextStyle::Button).color(label))
        .frame(false)
        .min_size(vec2(MIN_TARGET.x, tier.height()));
    let response = ui.add_enabled(enabled, button);
    let face = key_face(p, role, enabled, response.hovered(), response.is_pointer_button_down_on());
    let rect = response.rect;
    ui.painter()
        .set(face_slot, RectShape::new(rect, KEY_RADIUS, face.fill, face.rim));
    if enabled && !motion_owned {
        // keyElevation: the outer base line under a resting key.
        ui.painter().hline(
            rect.x_range().shrink(KEY_RADIUS),
            rect.bottom() + 1.0,
            Stroke::new(2.0, p.seam),
        );
    }
    if focus::keyboard_focused(&response) {
        focus::paint_focus_ring(ui, rect, RING_OUTSET);
    }
    KeyResponse {
        response,
        painter: ui.painter().clone(),
        bevel_slot,
    }
}

/// Text colour for a latched label's `WidgetText` (callers colour their text).
pub fn latched_text_color(p: &Palette, selected: bool) -> Color32 {
    if selected {
        p.selected_text
    } else {
        p.text
    }
}

/// A tab or tree-row label: the latch is a rust fill with a rim; hover and
/// keyboard focus get only the quiet chassis tint (F-T7-9), and `ring` adds
/// the focus ring outside the label. Tree rows pass `ring = false` and ring
/// the whole row instead.
pub fn latched_label(ui: &mut Ui, selected: bool, text: impl Into<WidgetText>, ring: bool) -> Response {
    let p = crate::style::p(ui);
    let bg = ui.painter().add(Shape::Noop);
    let response = ui.add(Button::new(text).frame(false).min_size(MIN_TARGET));
    let rect = response.rect;
    let quiet = response.hovered() || focus::keyboard_focused(&response);
    let (fill, stroke) = if selected {
        (p.selected, Stroke::new(1.0, p.selected_rim))
    } else if quiet {
        (ui.visuals().faint_bg_color, Stroke::NONE)
    } else {
        (Color32::TRANSPARENT, Stroke::NONE)
    };
    ui.painter().set(bg, RectShape::new(rect, 4.0, fill, stroke));
    if ring && focus::keyboard_focused(&response) {
        focus::paint_focus_ring(ui, rect, RING_OUTSET);
    }
    response
}
```

- [ ] **Step b5-3: Implement badges and status ink (was CP-B T5).** Insert above the test module of `src/style/badge.rs`:

```rust
//! Message-type legends and header status inks.

use egui::{vec2, Color32, Response, Stroke, Ui};

use crate::colors::Palette;
use crate::types::{ConnectionStatus, MessageType};

/// The legend ink of a message type: the category colour, never error red.
pub fn badge_ink(kind: &MessageType, p: &Palette) -> Color32 {
    match kind {
        MessageType::Subscribe => p.badge_sub,
        MessageType::Publish => p.badge_put,
        MessageType::Query => p.badge_get,
        MessageType::QueryReply => p.badge_reply,
    }
}

/// A quiet mono legend with a 1 pt outline and no fill (no key look).
pub fn badge(ui: &mut Ui, kind: &MessageType) -> Response {
    let ink = badge_ink(kind, crate::style::p(ui));
    let response = ui.label(crate::style::text::legend_text(kind.label()).color(ink));
    ui.painter()
        .rect_stroke(response.rect.expand2(vec2(3.0, 1.0)), 3.0, Stroke::new(1.0, ink));
    response
}

/// Header status ink on the statusGlass readout. Disconnected shares the
/// neutral readout ink; the word and the painted mark tell the states apart.
pub fn status_ink(status: &ConnectionStatus, monitor_ok: bool, p: &Palette) -> Color32 {
    match status {
        ConnectionStatus::Connected if monitor_ok => p.status_text,
        ConnectionStatus::Connected => p.status_warn,
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => p.status_warn,
        ConnectionStatus::Disconnected => p.status_text,
        ConnectionStatus::Error(_) => p.status_err,
    }
}
```

Run: `cargo test --locked style:: 2>&1 | grep -E 'key_|latched|badge|test result'`
Expected: `key_tiers_follow_user_heights`, `key_roles_differ_in_greyscale`, `latched_is_distinct_from_focus`, `badge_inks_meet_aa` ok. Commit b5 (`feat(sw-b5): caller-painted keys, latched labels, legends (was CP-B T5, T6, T13)`).

- [ ] **Step b6-1: Glass frames and content helpers — failing tests.** `src/style/content.rs` test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::colors::{DARK, LIGHT};

    fn colour_of(job: &LayoutJob, token: &str) -> Color32 {
        job.sections
            .iter()
            .find(|s| &job.text[s.byte_range.clone()] == token)
            .unwrap_or_else(|| panic!("no section {token:?} in {:?}", job.text))
            .format
            .color
    }

    #[test]
    fn json_job_colours_tokens() {
        let p = &LIGHT;
        let job = json_job("{\"a\": 1, \"b\": \"x\", \"c\": true}", p, egui::FontId::monospace(13.0));
        assert_eq!(colour_of(&job, "\"a\""), p.json_key);
        assert_eq!(colour_of(&job, "1"), p.json_number);
        assert_eq!(colour_of(&job, "\"x\""), p.json_string);
        assert_eq!(colour_of(&job, "true"), p.json_literal);
        assert_eq!(job.text, "{\"a\": 1, \"b\": \"x\", \"c\": true}");
    }

    #[test]
    fn json_tokens_meet_aa() {
        for p in [&LIGHT, &DARK] {
            for c in [p.json_key, p.json_string, p.json_number, p.json_literal, p.content_text] {
                assert!(crate::colors::contrast_ratio(c, p.content_glass) >= 4.5);
            }
        }
    }

    #[test]
    fn hex_rows_have_offset_hex_and_ascii() {
        let rows = hex_ascii_rows(b"Hello, world!\x00\x01\x02\x03", 256);
        assert_eq!(rows.len(), 2);
        assert!(rows[0].starts_with("00000000  48 65 6c 6c 6f"), "{}", rows[0]);
        assert!(rows[0].ends_with("|Hello, world!...|"), "{}", rows[0]);
        assert!(rows[1].starts_with("00000010  03"), "{}", rows[1]);
        assert_eq!(hex_ascii_rows(&[0u8; 1000], 64).len(), 4, "capped at max bytes");
    }
}
```

`src/style/glass.rs` test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_glass_sets_content_ink_and_reserves_a_slot() {
        let ctx = egui::Context::default();
        crate::style::install(&ctx);
        let mut ink = None;
        let _ = ctx.run(egui::RawInput::default(), |ctx| {
            egui::CentralPanel::default().show(ctx, |ui| {
                let f = content_glass(ui, |ui| ui.visuals().override_text_color);
                ink = f.inner;
                assert!(f.rect.width() > 0.0);
            });
        });
        assert_eq!(ink, Some(crate::colors::LIGHT.content_text));
    }
}
```

- [ ] **Step b6-2: Implement (was CP-B T14, T15; CP-C T4 owning frames).** `src/style/glass.rs`, above its tests:

```rust
//! Module faces and glass displays, each reserving a motion bevel slot above
//! its fill and below its content.

use egui::{Frame, Margin, Painter, Rect, Shape, Stroke, Ui};

use crate::colors::Palette;

pub const FACE_RADIUS: f32 = 8.0;

/// What a framed module returns: its content's value, its rect, and the
/// painter and slot the motion bevel paints into.
pub struct Faced<R> {
    pub inner: R,
    pub rect: Rect,
    pub painter: Painter,
    pub slot: egui::layers::ShapeIdx,
}

/// A module face on the chassis (replaces `ui.group`).
pub fn face_frame(p: &Palette) -> Frame {
    Frame::none()
        .fill(p.panel)
        .stroke(Stroke::new(1.0, p.seam))
        .rounding(FACE_RADIUS)
        .inner_margin(Margin::same(12.0))
}

/// The header readout module (statusGlass, note 9).
pub fn status_glass_frame(p: &Palette) -> Frame {
    Frame::none()
        .fill(p.status_glass)
        .rounding(6.0)
        .inner_margin(Margin::symmetric(10.0, 4.0))
}

/// Payload displays (contentGlass with a bezel).
pub fn content_glass_frame(p: &Palette) -> Frame {
    Frame::none()
        .fill(p.content_glass)
        .stroke(Stroke::new(2.0, p.seam))
        .rounding(6.0)
        .inner_margin(Margin::same(10.0))
}

fn framed<R>(ui: &mut Ui, frame: Frame, ink: Option<egui::Color32>, add: impl FnOnce(&mut Ui) -> R) -> Faced<R> {
    let shown = frame.show(ui, |ui| {
        if let Some(ink) = ink {
            ui.visuals_mut().override_text_color = Some(ink);
        }
        let slot = ui.painter().add(Shape::Noop);
        let inner = add(ui);
        (inner, slot, ui.painter().clone())
    });
    let (inner, slot, painter) = shown.inner;
    Faced {
        inner,
        rect: shown.response.rect,
        painter,
        slot,
    }
}

pub fn face<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> Faced<R> {
    let p = crate::style::p(ui);
    framed(ui, face_frame(p), None, add)
}

pub fn status_glass<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> Faced<R> {
    let p = crate::style::p(ui);
    framed(ui, status_glass_frame(p), Some(p.status_text), add)
}

pub fn content_glass<R>(ui: &mut Ui, add: impl FnOnce(&mut Ui) -> R) -> Faced<R> {
    let p = crate::style::p(ui);
    framed(ui, content_glass_frame(p), Some(p.content_text), add)
}
```

`src/style/content.rs`, above its tests:

```rust
//! Payload display helpers: JSON token colours and a hex + ASCII grid.

use egui::text::{LayoutJob, TextFormat};
use egui::{Color32, FontId};

use crate::colors::Palette;

/// Colours a pretty-printed JSON text by token: keys, strings, numbers,
/// literals; everything else in `content_text`.
pub fn json_job(text: &str, p: &Palette, font: FontId) -> LayoutJob {
    let mut job = LayoutJob::default();
    let push = |job: &mut LayoutJob, s: &str, color: Color32| {
        job.append(
            s,
            0.0,
            TextFormat {
                font_id: font.clone(),
                color,
                ..Default::default()
            },
        )
    };
    let bytes = text.as_bytes();
    let len = bytes.len();
    let mut i = 0;
    while i < len {
        let c = bytes[i];
        if c == b'"' {
            let start = i;
            i += 1;
            while i < len {
                match bytes[i] {
                    b'\\' => i += 2,
                    b'"' => {
                        i += 1;
                        break;
                    }
                    _ => i += 1,
                }
            }
            let end = i.min(len);
            let is_key = text[end..].trim_start().starts_with(':');
            push(&mut job, &text[start..end], if is_key { p.json_key } else { p.json_string });
            i = end;
        } else if c == b'-' || c.is_ascii_digit() {
            let start = i;
            while i < len && matches!(bytes[i], b'0'..=b'9' | b'-' | b'+' | b'.' | b'e' | b'E') {
                i += 1;
            }
            push(&mut job, &text[start..i], p.json_number);
        } else if let Some(lit) = ["true", "false", "null"].iter().find(|l| text[i..].starts_with(**l)) {
            push(&mut job, lit, p.json_literal);
            i += lit.len();
        } else {
            let ch_len = text[i..].chars().next().map_or(1, char::len_utf8);
            push(&mut job, &text[i..i + ch_len], p.content_text);
            i += ch_len;
        }
    }
    job
}

/// Rows of `offset  16 hex bytes  |ascii|` over at most `max` bytes.
pub fn hex_ascii_rows(bytes: &[u8], max: usize) -> Vec<String> {
    bytes[..bytes.len().min(max)]
        .chunks(16)
        .enumerate()
        .map(|(n, row)| {
            let hex: Vec<String> = row.iter().map(|b| format!("{b:02x}")).collect();
            let ascii: String = row
                .iter()
                .map(|&b| if b.is_ascii_graphic() || b == b' ' { b as char } else { '.' })
                .collect();
            format!("{:08x}  {:<47}  |{ascii}|", n * 16, hex.join(" "))
        })
        .collect()
}
```

Run: `cargo test --locked style:: 2>&1 | grep -E '^test |test result'; test -d src/style && ! grep -rn 'Color32::from_' src/style && echo no-literals`
Expected: every `style::` test ok (visuals, glyphs, text, focus, keys, badge, glass, content); the last command prints `no-literals`. Commit b6 (`feat(sw-b6): glass frames, JSON colours, hex grid (was CP-B T14, T15)`).

- [ ] **Step b7: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE1" HEAD` lists only `src/colors.rs`, `src/app/theme.rs` and `src/style/*`.

### Part c (was CP-C T3 keys, T6, T7, T8 path and mesh, T9, T11, T12, T13 action table, T14 engine, T15 engine): motion core

The engine is the T11 spike (`git show 267dd50:examples/causal_motion_spike.rs`) moved into modules, with `Instant` instead of `i.time` (so `process_events`, which has no `Context`, can commit), the action table of T12 instead of the spike's single source, a cached mesh and the 4 pt fitted profile. Everything is pure or takes a `Painter`; nothing here reads app state. Colours stay in `src/motion/spec.rs` until T3 stage 1 moves them into `src/colors.rs`. Two user decisions shape this part: reduced motion is off completely (every effect static, the end state at once, no relay), and the dark palette is neutral grey, so the shadow, recess and highlight inks come in a light set (T9's values) and a neutral dark set chosen by the painter's theme; the response colours (seat, orange, spectrum) are the same in both themes.

**Files:**
- Modify: `src/motion/mod.rs` (Step 0 stub)
- Create: `src/motion/spec.rs`, `src/motion/keys.rs`, `src/motion/sampler.rs`, `src/motion/ledger.rs`, `src/motion/bevel.rs`, `src/motion/link.rs`, `src/motion/repaint.rs`

**Interfaces:**
- Consumes: `crate::types::DetailView` (existing, derives `Clone, PartialEq, Debug`), egui 0.29.1 `Painter::set`, `Mesh`, `Color32::{lerp_to_gamma, gamma_multiply}`.
- Produces (frozen for T2; T2 part j may change bodies, never signatures):
  - `crate::motion::SurfaceKey` with `tab(&DetailView)`, `tree(&str)`, `tree_panel()`, `detail_body()`, `header_status()`, `conn_toggle()`, `sub_submit()`, `active(&str)`, `filter_tree()`, `filter_list()`, `messages_list()`, `save(&str)`, `save_inline(&str)`, `pub_submit()`, `pub_status()`, `pub_import()`, `pub_import_row()`, `query_submit()`, `query_results()`, and `id(&self) -> egui::Id`
  - `crate::motion::{ActionKind, Commit, Failure}` (enums below)
  - `crate::motion::Motion` with `new()`, `reduced()`, `set_reduced(bool, Instant)`, `begin(ActionKind, Instant)`, `commit(Commit, Instant)`, `fail(Failure, usize, Instant)`, `owns(&SurfaceKey) -> bool`, `is_pending() -> bool`, `begin_frame(Instant)`, `surface(SurfaceKey, Rect, &Painter, ShapeIdx, f32)`, `link_slot(&Painter, ShapeIdx, Rect)`, `value_changed(SurfaceKey, Instant)`, `end_frame(&egui::Context, Instant)`
  - `crate::motion::spec::{seat, layer, hold_through, MotionInks, INKS_LIGHT, INKS_DARK, inks}` and the timing constants; `crate::motion::sampler::{Role, Surface, Sample, side_rank}`; `crate::motion::bevel::{Profile, ring_mesh, MeshCache}` (`ring_mesh(rect, radius, &Sample, Profile, &MotionInks)`, `MeshCache::get(rect, radius, &Sample, Profile, &MotionInks)`); `crate::motion::link::{facing_path, max_link_len, link_mesh, Link}` (`link_mesh(a, b, pattern, opacity, &MotionInks)`); `crate::motion::repaint::{STEP_SECS, repaint_delay, wake_after}`

- [ ] **Step c1: Spec and keys.** Write `src/motion/spec.rs`:

```rust
//! Snow White causal-motion constants (review T9, T10) and colour mixing.

use egui::Color32;
use std::time::Duration;

/// `responseEdgeBase`, the seat colour every edge colour is mixed with.
pub const BASE: Color32 = Color32::from_rgb(0xd6, 0xd1, 0xc2);
/// `responseOrange`.
pub const ORANGE: Color32 = Color32::from_rgb(0xba, 0x77, 0x54);
/// `headerSpectrum`.
pub const SPECTRUM: [Color32; 4] = [
    Color32::from_rgb(0x8e, 0xaa, 0x6f),
    Color32::from_rgb(0xd8, 0xba, 0x6b),
    Color32::from_rgb(0xc4, 0x87, 0x4a),
    Color32::from_rgb(0xa7, 0x56, 0x3e),
];

/// The theme-dependent inks of the effect: the contact shadow (layer 5),
/// the link's recess cross-gradient and the white lip and catchlight
/// (layers 6 and 7). They follow the base they sit on; the response colours
/// above are the same in both themes.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotionInks {
    pub contact: Color32,
    pub link_dark: Color32,
    pub link_light: Color32,
    pub lip: Color32,
}

/// T9's values, on ivory.
pub const INKS_LIGHT: MotionInks = MotionInks {
    contact: Color32::from_rgb(0x4e, 0x3c, 0x32),
    link_dark: Color32::from_rgb(0x4c, 0x3b, 0x2f),
    link_light: Color32::from_rgb(0xff, 0xfb, 0xee),
    lip: Color32::WHITE,
};

/// Neutral greys for the grey dark palette (user decision): the shadow and
/// recess stay darker than its #3c3c3c chassis and #484848 panel, the
/// highlight lighter, and none is warm-tinted.
pub const INKS_DARK: MotionInks = MotionInks {
    contact: Color32::from_rgb(0x14, 0x14, 0x14),
    link_dark: Color32::from_rgb(0x1a, 0x1a, 0x1a),
    link_light: Color32::from_rgb(0xf2, 0xf2, 0xf2),
    lip: Color32::from_rgb(0xdc, 0xdc, 0xdc),
};

/// The ink set of the theme being painted (`visuals.dark_mode`).
pub fn inks(dark: bool) -> &'static MotionInks {
    if dark {
        &INKS_DARK
    } else {
        &INKS_LIGHT
    }
}

pub const SIDE_STAGGER_MS: f32 = 28.0;
pub const FIRST_STOP_MS: f32 = 105.0;
pub const FULL_COVERAGE_MS: f32 = 492.0; // 0.60 × 820
pub const PEAK_HOLD_MS: f32 = 250.0;
pub const RELEASE_TO_END_MS: f32 = 328.0;
pub const RIM_DELAY_NAV_MS: f32 = 55.0;
pub const RIM_DELAY_LOCAL_MS: f32 = 35.0;
pub const RELAY_DELAY_MS: f32 = 15.0;
pub const RESULT_COVERAGE_MS: f32 = 200.0;
pub const PENDING_DEPTH: f32 = 0.72;
pub const PEAK_DEPTH: f32 = 1.15;
pub const STOP_DEPTH: [f32; 5] = [1.0, 1.12, 1.0, 0.88, PEAK_DEPTH];
pub const PENDING_TIMEOUT: Duration = Duration::from_secs(15);
pub const RELAY_CLEANUP_MS: f32 = 30.0;
pub const LINK_FADE_IN_MS: f32 = 140.0;
/// The connection keeps these for every action (T9: 797/1125 hard-coded).
pub const LINK_RELEASE_MS: f32 = 797.0;
pub const LINK_END_MS: f32 = 1125.0;
pub const LINK_MAX_LEN: f32 = 520.0;
pub const LINK_MAX_FRACTION: f32 = 0.65;
/// Count and value changes (CP-C T14): a short edge response.
pub const VALUE_PULSE_MS: f32 = 328.0;
/// Filter keystrokes within this window share one response (T12 row 4).
pub const FILTER_COALESCE_MS: f32 = 250.0;

/// `color-mix(in srgb, c 50%, #d6d1c2)`: gamma-space lerp (T10 (f)).
pub fn seat(c: Color32) -> Color32 {
    c.lerp_to_gamma(BASE, 0.5)
}

/// CSS alpha: premultiplied in gamma space.
pub fn layer(c: Color32, alpha: f32) -> Color32 {
    Color32::from_rgb(c.r(), c.g(), c.b()).gamma_multiply(alpha.clamp(0.0, 1.0))
}

/// `holdThrough`: rim delay + full coverage + peak hold (797 navigation, 777 local).
pub fn hold_through(rim_delay_ms: f32) -> f32 {
    rim_delay_ms + FULL_COVERAGE_MS + PEAK_HOLD_MS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(c: Color32) -> String {
        format!("#{:02x}{:02x}{:02x}", c.r(), c.g(), c.b())
    }

    /// T9's six values (T11 logged them exactly).
    #[test]
    fn seat_reproduces_t9_hex() {
        assert_eq!(hex(seat(ORANGE)), "#c8a48b"); // 1. muted orange, start
        assert_eq!(hex(seat(SPECTRUM[0])), "#b2be99");
        assert_eq!(hex(seat(SPECTRUM[1])), "#d7c697");
        assert_eq!(hex(seat(SPECTRUM[2])), "#cdac86");
        assert_eq!(hex(seat(SPECTRUM[3])), "#bf9480");
        assert_eq!(hex(seat(ORANGE)), "#c8a48b"); // 6. muted orange, settle
        assert_eq!(hold_through(RIM_DELAY_NAV_MS), 797.0);
        assert_eq!(hold_through(RIM_DELAY_LOCAL_MS), 777.0);
    }

    /// Ivory keeps T9's inks; the grey dark base gets neutral greys, with the
    /// shadow and recess below its chassis and the highlight above its panel.
    /// (T3 stage 1's `motion_inks_suit_each_base` checks them against `DARK`.)
    #[test]
    fn motion_inks_by_theme() {
        assert_eq!(inks(false), &INKS_LIGHT);
        assert_eq!(inks(true), &INKS_DARK);
        assert_eq!(hex(INKS_LIGHT.contact), "#4e3c32", "T9's contact shadow");
        for c in [INKS_DARK.contact, INKS_DARK.link_dark, INKS_DARK.link_light, INKS_DARK.lip] {
            assert!(c.r() == c.g() && c.g() == c.b(), "neutral grey: {}", hex(c));
        }
        assert!(INKS_DARK.contact.r() < 0x3c && INKS_DARK.link_dark.r() < 0x3c, "darker than the grey chassis");
        assert!(INKS_DARK.link_light.r() > 0x48 && INKS_DARK.lip.r() > 0x48, "lighter than the grey panel");
    }
}
```

Write `src/motion/keys.rs`:

```rust
//! Logical surface keys (F-T12-6): stable across frames and insertions,
//! unlike auto ids.

use crate::types::DetailView;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct SurfaceKey(pub String);

impl SurfaceKey {
    fn k(s: impl Into<String>) -> Self {
        SurfaceKey(s.into())
    }
    pub fn tab(view: &DetailView) -> Self {
        Self::k(format!("tab.{view:?}"))
    }
    pub fn tree(path: &str) -> Self {
        Self::k(format!("tree.{path}"))
    }
    pub fn tree_panel() -> Self {
        Self::k("tree.panel")
    }
    pub fn detail_body() -> Self {
        Self::k("detail.body")
    }
    pub fn header_status() -> Self {
        Self::k("header.status")
    }
    pub fn conn_toggle() -> Self {
        Self::k("conn.toggle")
    }
    pub fn sub_submit() -> Self {
        Self::k("sub.submit")
    }
    pub fn active(key_expr: &str) -> Self {
        Self::k(format!("active.{key_expr}"))
    }
    pub fn filter_tree() -> Self {
        Self::k("filter.tree")
    }
    pub fn filter_list() -> Self {
        Self::k("filter.list")
    }
    pub fn messages_list() -> Self {
        Self::k("messages.list")
    }
    pub fn save(topic: &str) -> Self {
        Self::k(format!("save.{topic}"))
    }
    pub fn save_inline(topic: &str) -> Self {
        Self::k(format!("save.inline.{topic}"))
    }
    pub fn pub_submit() -> Self {
        Self::k("pub.submit")
    }
    pub fn pub_status() -> Self {
        Self::k("pub.status")
    }
    pub fn pub_import() -> Self {
        Self::k("pub.import")
    }
    pub fn pub_import_row() -> Self {
        Self::k("pub.import_row")
    }
    pub fn query_submit() -> Self {
        Self::k("query.submit")
    }
    pub fn query_results() -> Self {
        Self::k("query.results")
    }
    pub fn id(&self) -> egui::Id {
        egui::Id::new(("motion", self.0.as_str()))
    }
}
```

Replace `src/motion/mod.rs` with the module list only for now (the `Motion` struct comes in c6):

```rust
//! Causal motion: action ledger, bevel and link painting, repaint policy.
#![allow(dead_code)] // SW-T2: callers arrive in T2

pub mod bevel;
pub mod keys;
pub mod ledger;
pub mod link;
pub mod repaint;
pub mod sampler;
pub mod spec;

pub use keys::SurfaceKey;
pub use ledger::{ActionKind, Commit, Failure};
```

Create `bevel.rs`, `ledger.rs`, `link.rs`, `repaint.rs`, `sampler.rs` with only a `//!` line. Run: `cargo test --locked motion::spec 2>&1 | tail -n 3` — Expected: `seat_reproduces_t9_hex` and `motion_inks_by_theme` ok. Commit c1 (`feat(sw-c1): motion spec constants and logical keys (was CP-C T3)`).

- [ ] **Step c2-1: Sampler tests first (was CP-C T6).** `src/motion/sampler.rs` test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::motion::spec::*;
    use egui::{pos2, Rect};
    use std::time::{Duration, Instant};

    fn at(t0: Instant, ms: u64) -> Instant {
        t0 + Duration::from_millis(ms)
    }

    #[test]
    fn side_rank_orders_by_distance_to_source() {
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(100.0, 50.0));
        // source to the left: left first, then top and bottom (tie keeps side order), right last
        assert_eq!(side_rank(r, pos2(-50.0, 25.0)), [1, 3, 2, 0]);
    }

    #[test]
    fn side_schedule_matches_t9() {
        let t0 = Instant::now();
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(100.0, 50.0));
        let src = pos2(-50.0, 25.0);
        let s = Surface::reveal(Role::Source, t0, RIM_DELAY_NAV_MS);
        let left = 3;
        let top = 0;
        assert_eq!(s.sample(t0, r, src, false).depth[left], 0.0);
        let x = s.sample(at(t0, 105), r, src, false);
        assert!((x.depth[left] - 1.0).abs() < 1e-3, "{:?}", x.depth);
        assert_eq!(x.edge[left], seat(SPECTRUM[0]));
        assert!(x.depth[top] < 1.0, "top starts 28 ms later: {:?}", x.depth);
        let x = s.sample(at(t0, 797), r, src, false);
        assert!((x.depth[left] - PEAK_DEPTH).abs() < 1e-3);
        assert_eq!(x.edge[left], seat(SPECTRUM[0]), "the peak holds s[k] for rank k");
        assert_eq!(x.edge[top], seat(SPECTRUM[1]));
        let x = s.sample(at(t0, 1125), r, src, false);
        assert_eq!(x.depth, [1.0; 4]);
        assert_eq!(x.edge, [seat(ORANGE); 4]);
        assert!(!x.animating);
    }

    #[test]
    fn pending_rises_to_072() {
        let t0 = Instant::now();
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(100.0, 36.0));
        let s = Surface::pending(t0);
        let x = s.sample(at(t0, 105 + 3 * 28 + 1), r, pos2(50.0, 18.0), false);
        assert_eq!(x.depth, [PENDING_DEPTH; 4]);
        assert_eq!(x.edge, [seat(ORANGE); 4]);
    }

    /// User decision: reduced motion is off completely. Every role samples its
    /// end state from the first frame on and never animates; the facade adds
    /// no relay (`reduced_motion_is_static`).
    #[test]
    fn reduced_is_fully_static() {
        let t0 = Instant::now();
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(300.0, 400.0));
        for role in [Role::Source, Role::Result] {
            let s = Surface::reveal(role, t0, RIM_DELAY_NAV_MS);
            for ms in [0, 20, 400, 1200] {
                let x = s.sample(at(t0, ms), r, pos2(0.0, 0.0), true);
                assert_eq!((x.depth, x.edge, x.animating), ([1.0; 4], [seat(ORANGE); 4], false), "{role:?} at {ms} ms");
            }
            assert!(s.latched && !s.expired(at(t0, 60_000)), "the end state stays");
        }
        let p = Surface::pending(t0).sample(t0, r, pos2(0.0, 0.0), true);
        assert_eq!((p.depth, p.animating), ([PENDING_DEPTH; 4], false), "pending shows at once, no rise");
        let v = Surface::pulse(t0).sample(t0, r, r.center(), true);
        assert_eq!((v.depth, v.animating), ([0.0; 4], false), "no value pulse");
    }

    /// Full motion only: a relay leaves about 1155 ms after the commit (T9).
    #[test]
    fn relay_expires_after_its_response() {
        let t0 = Instant::now();
        let relay = Surface::reveal(Role::Relay, t0, RIM_DELAY_NAV_MS);
        assert!(!relay.latched);
        assert!(!relay.expired(at(t0, 1154)));
        assert!(relay.expired(at(t0, 1156)), "relay removed at about 1155 ms (T9)");
    }

    #[test]
    fn value_pulse_fades_out() {
        let t0 = Instant::now();
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(200.0, 24.0));
        let s = Surface::pulse(t0);
        assert_eq!(s.sample(t0, r, r.center(), false).depth, [1.0; 4]);
        assert!(s.expired(at(t0, 400)));
    }
}
```

- [ ] **Step c2-2: Implement the sampler.** Above the tests:

```rust
//! Pure per-side sampler (T10 (k)–(m), (u); spike `Surface::sample`).

use egui::{Color32, Pos2, Rect};
use std::time::Instant;

use crate::motion::spec::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Source,
    Relay,
    Result,
    /// A count or value change on its own row (CP-C T14).
    Pulse,
}

/// One animated surface. `start` includes the role's delay.
#[derive(Clone, Debug)]
pub struct Surface {
    pub role: Role,
    pub start: Instant,
    pub coverage_ms: f32,
    pub release_ms: f32,
    pub duration_ms: f32,
    pub pending: bool,
    pub latched: bool,
    /// Reduced motion was switched mid-response: at its end state from now on.
    pub settled: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sample {
    /// Top, right, bottom, left.
    pub depth: [f32; 4],
    pub edge: [Color32; 4],
    pub animating: bool,
}

impl Surface {
    /// A participant of a committed action. `rim_delay_ms` is 55 for
    /// navigation and 35 for local actions (T9).
    pub fn reveal(role: Role, commit: Instant, rim_delay_ms: f32) -> Self {
        let delay = match role {
            Role::Source | Role::Pulse => 0.0,
            Role::Relay => RELAY_DELAY_MS,
            Role::Result => rim_delay_ms,
        };
        let hold = hold_through(rim_delay_ms);
        let coverage = if role == Role::Result { RESULT_COVERAGE_MS } else { FULL_COVERAGE_MS };
        let release = hold - delay;
        Surface {
            role,
            start: commit + std::time::Duration::from_secs_f32(delay / 1000.0),
            coverage_ms: coverage,
            release_ms: release,
            duration_ms: release + RELEASE_TO_END_MS,
            pending: false,
            latched: role != Role::Relay,
            settled: false,
        }
    }

    pub fn pending(now: Instant) -> Self {
        Surface {
            role: Role::Source,
            start: now,
            coverage_ms: 0.0,
            release_ms: 0.0,
            duration_ms: 0.0,
            pending: true,
            latched: true,
            settled: false,
        }
    }

    pub fn pulse(now: Instant) -> Self {
        Surface {
            role: Role::Pulse,
            start: now,
            coverage_ms: 0.0,
            release_ms: 0.0,
            duration_ms: VALUE_PULSE_MS,
            pending: false,
            latched: false,
            settled: false,
        }
    }

    pub fn local_ms(&self, now: Instant) -> f32 {
        if now >= self.start {
            now.duration_since(self.start).as_secs_f32() * 1000.0
        } else {
            -(self.start.duration_since(now).as_secs_f32() * 1000.0)
        }
    }

    pub fn expired(&self, now: Instant) -> bool {
        let cleanup = if self.role == Role::Pulse { 0.0 } else { RELAY_CLEANUP_MS };
        !self.latched && !self.pending && self.local_ms(now) >= self.duration_ms + cleanup
    }

    /// Milliseconds until the next discrete change (relay removal), if any.
    pub fn next_discrete_ms(&self, now: Instant) -> Option<f32> {
        if self.latched || self.pending {
            return None;
        }
        let at = self.duration_ms + RELAY_CLEANUP_MS - self.local_ms(now);
        (at > 0.0).then_some(at)
    }

    pub fn sample(&self, now: Instant, rect: Rect, source_centre: Pos2, reduced: bool) -> Sample {
        let t = self.local_ms(now);
        let orange = seat(ORANGE);
        let rank = side_rank(rect, source_centre);
        if self.role == Role::Pulse {
            let d = if reduced { 0.0 } else { (1.0 - t / VALUE_PULSE_MS).clamp(0.0, 1.0) };
            return Sample { depth: [d; 4], edge: [orange; 4], animating: !reduced && t < VALUE_PULSE_MS };
        }
        if self.pending {
            let mut depth = [0.0; 4];
            let mut animating = false;
            for (d, &k) in depth.iter_mut().zip(rank.iter()) {
                let sk = k as f32 * SIDE_STAGGER_MS;
                *d = if reduced || self.settled {
                    PENDING_DEPTH
                } else if t <= sk {
                    animating = true;
                    0.0
                } else if t < sk + FIRST_STOP_MS {
                    animating = true;
                    PENDING_DEPTH * (t - sk) / FIRST_STOP_MS
                } else {
                    PENDING_DEPTH
                };
            }
            return Sample { depth, edge: [orange; 4], animating };
        }
        if reduced || self.settled || t >= self.duration_ms {
            return Sample { depth: [1.0; 4], edge: [orange; 4], animating: false };
        }
        let mut depth = [0.0; 4];
        let mut edge = [orange; 4];
        for (side, &k) in rank.iter().enumerate() {
            let sk = k as f32 * SIDE_STAGGER_MS;
            let mut kf: Vec<(f32, f32, Color32)> = Vec::with_capacity(8);
            kf.push((sk, 0.0, orange));
            let span = (self.coverage_ms - sk - FIRST_STOP_MS).max(0.0);
            for (i, d) in STOP_DEPTH.iter().enumerate() {
                let at = sk + FIRST_STOP_MS + span * i as f32 / 4.0;
                kf.push((at, *d, seat(SPECTRUM[(i + k) % 4])));
            }
            kf.push((self.release_ms, PEAK_DEPTH, seat(SPECTRUM[k % 4])));
            kf.push((self.duration_ms, 1.0, orange));
            let (d, c) = interp(&kf, t);
            depth[side] = d;
            edge[side] = c;
        }
        Sample { depth, edge, animating: true }
    }
}

/// Linear keyframes; before the first key: depth 0, orange (`fill: backwards`).
fn interp(kf: &[(f32, f32, Color32)], t: f32) -> (f32, Color32) {
    if t <= kf[0].0 {
        return (kf[0].1, kf[0].2);
    }
    for w in kf.windows(2) {
        let (a, b) = (w[0], w[1]);
        if t <= b.0 {
            let u = if b.0 > a.0 { (t - a.0) / (b.0 - a.0) } else { 1.0 };
            return (a.1 + (b.1 - a.1) * u, a.2.lerp_to_gamma(b.2, u));
        }
    }
    let last = kf[kf.len() - 1];
    (last.1, last.2)
}

/// Rank (0 starts first) of [top, right, bottom, left] by the distance from
/// each side's midpoint to the source centre; ties keep side order.
pub fn side_rank(rect: Rect, src: Pos2) -> [usize; 4] {
    let mids = [rect.center_top(), rect.right_center(), rect.center_bottom(), rect.left_center()];
    let mut idx = [0usize, 1, 2, 3];
    idx.sort_by(|&a, &b| mids[a].distance(src).total_cmp(&mids[b].distance(src)));
    let mut rank = [0; 4];
    for (k, &side) in idx.iter().enumerate() {
        rank[side] = k;
    }
    rank
}
```

Run: `cargo test --locked motion::sampler 2>&1 | tail -n 3` — Expected: 6 passed. (In `side_rank_orders_by_distance_to_source` the expected array is `[top, right, bottom, left] = [1, 3, 2, 0]`.) Commit c2.

- [ ] **Step c3-1: Ledger tests first (was CP-C T1, T12, T13 action table).** `src/motion/ledger.rs` test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn ledger_pending_to_committed() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        let id = l.begin(ActionKind::Publish("demo/a".into()), t0);
        assert!(l.is_pending());
        assert_eq!(l.commit(&Commit::Published("demo/a".into()), t0 + Duration::from_millis(40)), Some(id));
        assert!(!l.is_pending());
    }

    #[test]
    fn ledger_pending_to_failed() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        l.begin(ActionKind::Connect, t0);
        assert!(l.fail(&Failure::Connect, 0, t0));
        assert!(!l.is_pending());
        assert_eq!(l.commit(&Commit::MonitorConnected, t0), None, "no reveal after a failure");
    }

    #[test]
    fn ledger_stale_commit_ignored() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        l.begin(ActionKind::Publish("demo/a".into()), t0);
        l.begin(ActionKind::Tab(crate::types::DetailView::Help), t0);
        assert_eq!(l.commit(&Commit::Published("demo/a".into()), t0), None);
        let mut l = Ledger::default();
        l.begin(ActionKind::Subscribe("a/**".into()), t0);
        assert_eq!(l.commit(&Commit::SubscriptionCreated("b/**".into()), t0), None, "other key");
    }

    #[test]
    fn ledger_pending_times_out() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        l.begin(ActionKind::Query, t0);
        assert!(!l.tick(t0 + Duration::from_secs(14)));
        assert!(l.tick(t0 + Duration::from_millis(15_001)));
        assert!(!l.is_pending());
    }

    #[test]
    fn ledger_keyless_subscribe_failure_needs_single_pending() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        l.begin(ActionKind::Subscribe("a/**".into()), t0);
        assert!(!l.fail(&Failure::Subscribe, 2, t0), "two pending subscribes: the failure has no key");
        assert!(l.fail(&Failure::Subscribe, 1, t0));
    }

    #[test]
    fn ledger_filter_keystrokes_coalesce() {
        let t0 = Instant::now();
        let mut l = Ledger::default();
        let a = l.begin(ActionKind::FilterTree, t0);
        let b = l.begin(ActionKind::FilterTree, t0 + Duration::from_millis(100));
        assert_eq!(a, b);
        let c = l.begin(ActionKind::FilterTree, t0 + Duration::from_millis(400));
        assert_ne!(a, c);
    }

    #[test]
    fn plans_follow_t12_table() {
        use crate::motion::spec::{RIM_DELAY_LOCAL_MS, RIM_DELAY_NAV_MS};
        let p = ActionKind::TreeSelect("demo/x".into()).plan();
        assert_eq!(p.relay, Some(SurfaceKey::tree_panel()));
        assert_eq!(p.rim_delay_ms, RIM_DELAY_NAV_MS);
        assert!(!p.pending && p.link);
        let p = ActionKind::Save("demo/x".into()).plan();
        assert_eq!(p.results, vec![SurfaceKey::save_inline("demo/x")]);
        assert_eq!(p.rim_delay_ms, RIM_DELAY_LOCAL_MS);
        assert!(!p.link, "Save's result is inline (F-T12-3)");
        assert!(ActionKind::Connect.plan().pending && ActionKind::Connect.plan().link);
        assert!(!ActionKind::Disconnect.plan().pending, "committed at the click");
        assert!(ActionKind::Query.plan().link);
        assert!(!ActionKind::Import.plan().link, "Import never links to the header (F-T12-5)");
    }
}
```

- [ ] **Step c3-2: Implement the ledger.** Above the tests:

```rust
//! Action ledger keyed by kind (F-T8-11, note 2): one live action; a new
//! click supersedes it; commits and failures bind by kind and key.

use std::time::{Duration, Instant};

use crate::motion::keys::SurfaceKey;
use crate::motion::spec::*;
use crate::types::DetailView;

/// The ten actions of T12's table.
#[derive(Clone, Debug, PartialEq)]
pub enum ActionKind {
    Tab(DetailView),
    TreeSelect(String),
    Subscribe(String),
    FilterTree,
    FilterList,
    Connect,
    Disconnect,
    Save(String),
    Publish(String),
    Import,
    Query,
}

/// What an action touches, when, and whether it waits for a commit.
#[derive(Clone, Debug, PartialEq)]
pub struct ActionPlan {
    pub source: SurfaceKey,
    pub relay: Option<SurfaceKey>,
    pub results: Vec<SurfaceKey>,
    pub rim_delay_ms: f32,
    pub pending: bool,
    pub link: bool,
}

impl ActionKind {
    pub fn plan(&self) -> ActionPlan {
        let local = |source, result, pending, link| ActionPlan {
            source,
            relay: None,
            results: vec![result],
            rim_delay_ms: RIM_DELAY_LOCAL_MS,
            pending,
            link,
        };
        match self {
            ActionKind::Tab(v) => ActionPlan {
                source: SurfaceKey::tab(v),
                relay: None,
                results: vec![SurfaceKey::detail_body()],
                rim_delay_ms: RIM_DELAY_NAV_MS,
                pending: false,
                link: true,
            },
            ActionKind::TreeSelect(path) => ActionPlan {
                source: SurfaceKey::tree(path),
                relay: Some(SurfaceKey::tree_panel()),
                results: vec![SurfaceKey::detail_body()],
                rim_delay_ms: RIM_DELAY_NAV_MS,
                pending: false,
                link: true,
            },
            ActionKind::Subscribe(k) => local(SurfaceKey::sub_submit(), SurfaceKey::active(k), true, false),
            ActionKind::FilterTree => local(SurfaceKey::filter_tree(), SurfaceKey::tree_panel(), false, false),
            ActionKind::FilterList => local(SurfaceKey::filter_list(), SurfaceKey::messages_list(), false, false),
            ActionKind::Connect => local(SurfaceKey::conn_toggle(), SurfaceKey::header_status(), true, true),
            ActionKind::Disconnect => local(SurfaceKey::conn_toggle(), SurfaceKey::header_status(), false, true),
            ActionKind::Save(t) => local(SurfaceKey::save(t), SurfaceKey::save_inline(t), false, false),
            ActionKind::Publish(_) => local(SurfaceKey::pub_submit(), SurfaceKey::pub_status(), true, false),
            ActionKind::Import => local(SurfaceKey::pub_import(), SurfaceKey::pub_import_row(), false, false),
            ActionKind::Query => local(SurfaceKey::query_submit(), SurfaceKey::query_results(), true, true),
        }
    }

    fn is_filter(&self) -> bool {
        matches!(self, ActionKind::FilterTree | ActionKind::FilterList)
    }
}

/// A worker event that commits a pending action (CP-C T2's table).
#[derive(Clone, Debug, PartialEq)]
pub enum Commit {
    MonitorConnected,
    SubscriptionCreated(String),
    Published(String),
    /// The first reply of the pending query, or its "no replies" verdict.
    QueryAnswered,
}

/// A worker event that ends a pending action without a reveal.
#[derive(Clone, Debug, PartialEq)]
pub enum Failure {
    Connect,
    /// `OperationFailed { op: Subscribe }` carries no key.
    Subscribe,
    Publish,
    Query,
    /// The worker session ended (`Disconnected`): every pending action except
    /// Connect ends (a Disconnected seen while connecting belongs to an
    /// earlier Disconnect, `src/events/mod.rs:75-83`).
    SessionEnded,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Phase {
    Pending { since: Instant },
    Revealed { at: Instant },
    Ended,
}

#[derive(Clone, Debug)]
pub struct Action {
    pub id: u64,
    pub kind: ActionKind,
    pub phase: Phase,
}

#[derive(Default, Debug)]
pub struct Ledger {
    next_id: u64,
    pub current: Option<Action>,
}

impl Ledger {
    /// Starts an action and returns its id. Synchronous actions are revealed
    /// at once; a filter keystroke inside the coalescing window reuses the
    /// live action.
    pub fn begin(&mut self, kind: ActionKind, now: Instant) -> u64 {
        if let Some(a) = &self.current {
            if a.kind == kind && kind.is_filter() {
                if let Phase::Revealed { at } = a.phase {
                    if now.duration_since(at) < Duration::from_secs_f32(FILTER_COALESCE_MS / 1000.0) {
                        return a.id;
                    }
                }
            }
        }
        self.next_id += 1;
        let phase = if kind.plan().pending {
            Phase::Pending { since: now }
        } else {
            Phase::Revealed { at: now }
        };
        self.current = Some(Action { id: self.next_id, kind, phase });
        self.next_id
    }

    pub fn is_pending(&self) -> bool {
        matches!(self.current, Some(Action { phase: Phase::Pending { .. }, .. }))
    }

    /// Reveals the pending action this event commits; a stale or unrelated
    /// commit returns None.
    pub fn commit(&mut self, c: &Commit, now: Instant) -> Option<u64> {
        let a = self.current.as_mut()?;
        if !matches!(a.phase, Phase::Pending { .. }) {
            return None;
        }
        let matches = match (&a.kind, c) {
            (ActionKind::Connect, Commit::MonitorConnected) => true,
            (ActionKind::Subscribe(k), Commit::SubscriptionCreated(key)) => k == key,
            (ActionKind::Publish(k), Commit::Published(key)) => k == key,
            (ActionKind::Query, Commit::QueryAnswered) => true,
            _ => false,
        };
        if !matches {
            return None;
        }
        a.phase = Phase::Revealed { at: now };
        Some(a.id)
    }

    /// Ends the pending action this failure belongs to; true when it did.
    /// `pending_subscribes` is the number of Subscribe keys still waiting,
    /// read before the event handler clears them.
    pub fn fail(&mut self, f: &Failure, pending_subscribes: usize, _now: Instant) -> bool {
        let Some(a) = self.current.as_mut() else {
            return false;
        };
        if !matches!(a.phase, Phase::Pending { .. }) {
            return false;
        }
        let ends = match (&a.kind, f) {
            (ActionKind::Connect, Failure::Connect) => true,
            (ActionKind::Subscribe(_), Failure::Subscribe) => pending_subscribes == 1,
            (ActionKind::Publish(_), Failure::Publish) => true,
            (ActionKind::Query, Failure::Query) => true,
            (ActionKind::Connect, Failure::SessionEnded) => false,
            (_, Failure::SessionEnded) => true,
            _ => false,
        };
        if ends {
            a.phase = Phase::Ended;
        }
        ends
    }

    /// Ends a pending action after `PENDING_TIMEOUT`; true when it did.
    pub fn tick(&mut self, now: Instant) -> bool {
        if let Some(a) = self.current.as_mut() {
            if let Phase::Pending { since } = a.phase {
                if now.duration_since(since) > PENDING_TIMEOUT {
                    a.phase = Phase::Ended;
                    return true;
                }
            }
        }
        false
    }

    /// Time left before the pending action times out.
    pub fn pending_remaining(&self, now: Instant) -> Option<Duration> {
        match &self.current {
            Some(Action { phase: Phase::Pending { since }, .. }) => {
                Some(PENDING_TIMEOUT.saturating_sub(now.duration_since(*since)))
            }
            _ => None,
        }
    }
}
```

Run: `cargo test --locked motion::ledger 2>&1 | tail -n 3` — Expected: 7 passed. Commit c3.

- [ ] **Step c4-1: Bevel tests first (was CP-C T7).** `src/motion/bevel.rs` test module:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::motion::sampler::Sample;
    use crate::motion::spec::{seat, INKS_DARK, INKS_LIGHT, ORANGE};
    use egui::{pos2, Rect};

    fn peak() -> Sample {
        Sample { depth: [1.15; 4], edge: [seat(ORANGE); 4], animating: true }
    }

    /// Every vertex lies in the rounded band: `band` from the straight edges,
    /// plus the corner arc's sag `radius·(1 − 1/√2)` at the corners.
    fn assert_in_band(rect: Rect, mesh: &egui::Mesh, band: f32, radius: f32) {
        let slack = radius * (1.0 - std::f32::consts::FRAC_1_SQRT_2);
        assert!(!mesh.vertices.is_empty());
        for v in &mesh.vertices {
            let p = v.pos;
            assert!(rect.expand(0.01).contains(p), "{p:?} outside {rect:?}");
            let d = (p.x - rect.left()).min(rect.right() - p.x).min(p.y - rect.top()).min(rect.bottom() - p.y);
            assert!(d <= band + slack + 0.01, "{p:?} is {d} inside the edge (band {band})");
        }
    }

    #[test]
    fn bevel_stays_in_band() {
        let panel = Rect::from_min_max(pos2(0.0, 0.0), pos2(300.0, 200.0));
        assert_eq!(Profile::for_rect(panel), Profile::Full);
        // a 32 pt tab: under 34 pt, the 4 pt fitted profile (F-T11-4)
        let tab = Rect::from_min_max(pos2(0.0, 0.0), pos2(96.0, 32.0));
        assert_eq!(Profile::for_rect(tab), Profile::Fitted);
        for inks in [&INKS_LIGHT, &INKS_DARK] {
            assert_in_band(panel, &ring_mesh(panel, 8.0, &peak(), Profile::Full, inks), 8.0, 8.0);
            assert_in_band(tab, &ring_mesh(tab, 6.0, &peak(), Profile::Fitted, inks), 4.0, 6.0);
        }
    }

    #[test]
    fn shade_table_matches_direct_compute() {
        for (layer, &(_, offset, blur, spread, alpha, _)) in LAYERS.iter().enumerate() {
            for d_half in [0usize, 3, 8, 16] {
                let d = d_half as f32 * 0.5;
                let direct = alpha * phi((offset * 1.0 + spread - d) / (blur / 2.0));
                assert!((shade(layer, 1.0, d, Profile::Full) - direct).abs() < 0.02, "layer {layer} d {d}");
            }
        }
    }

    #[test]
    fn mesh_cache_reuses() {
        let r = Rect::from_min_max(pos2(0.0, 0.0), pos2(200.0, 100.0));
        let mut cache = MeshCache::default();
        let a = cache.get(r, 8.0, &peak(), Profile::Full, &INKS_LIGHT);
        let b = cache.get(r, 8.0, &peak(), Profile::Full, &INKS_LIGHT);
        assert_eq!(a, b);
        assert_eq!(cache.len(), 1, "a held or resting frame reuses the mesh");
        let c = cache.get(r, 8.0, &peak(), Profile::Full, &INKS_DARK);
        assert_ne!(a, c, "the grey base gets its own inks");
        assert_eq!(cache.len(), 2, "one entry per theme");
    }
}
```

- [ ] **Step c4-2: Implement the bevel.** Above the tests (the ring strip and layer stack are the spike's `ring_mesh`/`stack_color`, lines 280–375 of `267dd50:examples/causal_motion_spike.rs`, with the band and layer geometry scaled by the profile):

```rust
//! Inset bevel as a ring-strip mesh (T10 (a)); 8 pt band for panels and
//! keys >= 34 pt, a 4 pt fitted profile for smaller sources; a precomputed
//! shade table and a mesh cache (F-T11-1).

use std::collections::HashMap;
use std::sync::OnceLock;

use egui::{pos2, Color32, Mesh, Pos2, Rect};

use crate::motion::sampler::Sample;
use crate::motion::spec::{layer, MotionInks};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Profile {
    /// 8 pt band, the specimen's geometry.
    Full,
    /// 4 pt band, every layer offset, blur and spread halved.
    Fitted,
}

impl Profile {
    pub fn for_rect(rect: Rect) -> Self {
        if rect.width() >= 34.0 && rect.height() >= 34.0 {
            Profile::Full
        } else {
            Profile::Fitted
        }
    }
    pub fn band(self) -> f32 {
        match self {
            Profile::Full => 8.0,
            Profile::Fitted => 4.0,
        }
    }
    fn scale(self) -> f32 {
        match self {
            Profile::Full => 1.0,
            Profile::Fitted => 0.5,
        }
    }
}

/// (side, offset, blur, spread, alpha, is_contact): the specimen's five inset layers.
pub(crate) const LAYERS: [(usize, f32, f32, f32, f32, bool); 5] = [
    (0, 4.0, 4.0, -2.0, 0.65, false),
    (3, 4.0, 4.0, -2.0, 0.65, false),
    (2, 3.0, 3.0, -2.0, 0.55, false),
    (1, 3.0, 3.0, -2.0, 0.55, false),
    (0, 5.0, 5.0, -4.0, 0.40, true),
];

#[allow(clippy::excessive_precision)] // Abramowitz-Stegun 7.1.26
fn erf(x: f32) -> f32 {
    let t = 1.0 / (1.0 + 0.3275911 * x.abs());
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592)
            * t
            * (-x * x).exp();
    if x >= 0.0 {
        y
    } else {
        -y
    }
}

pub(crate) fn phi(z: f32) -> f32 {
    0.5 * (1.0 + erf(z / std::f32::consts::SQRT_2))
}

const DEPTH_STEPS: usize = 64;
const DEPTH_MAX: f32 = 1.2;
const INSETS: usize = 17; // 0..=8 pt in 0.5 pt steps

/// alpha[profile][layer][depth step][inset]
fn table() -> &'static [[[[f32; INSETS]; DEPTH_STEPS + 1]; 5]; 2] {
    static TABLE: OnceLock<Box<[[[[f32; INSETS]; DEPTH_STEPS + 1]; 5]; 2]>> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = Box::new([[[[0.0; INSETS]; DEPTH_STEPS + 1]; 5]; 2]);
        for (pi, profile) in [Profile::Full, Profile::Fitted].into_iter().enumerate() {
            let s = profile.scale();
            for (li, &(_, offset, blur, spread, alpha, _)) in LAYERS.iter().enumerate() {
                for di in 0..=DEPTH_STEPS {
                    let depth = di as f32 / DEPTH_STEPS as f32 * DEPTH_MAX;
                    for ii in 0..INSETS {
                        let d = ii as f32 * 0.5 * s;
                        t[pi][li][di][ii] =
                            alpha * phi(((offset * depth + spread) * s - d) / (blur * s / 2.0));
                    }
                }
            }
        }
        t
    })
}

/// Layer alpha at distance `d` inside the edge: the table on grid points,
/// direct computation elsewhere.
pub(crate) fn shade(layer_idx: usize, depth: f32, d: f32, profile: Profile) -> f32 {
    let s = profile.scale();
    let step = d / (0.5 * s);
    let di = (depth / DEPTH_MAX * DEPTH_STEPS as f32).round();
    if (step - step.round()).abs() < 1e-3 && (step as usize) < INSETS && (0.0..=DEPTH_STEPS as f32).contains(&di) {
        let pi = if profile == Profile::Full { 0 } else { 1 };
        return table()[pi][layer_idx][di as usize][step.round() as usize];
    }
    let (_, offset, blur, spread, alpha, _) = LAYERS[layer_idx];
    alpha * phi(((offset * depth + spread) * s - d) / (blur * s / 2.0))
}

fn over(top: Color32, under: Color32) -> Color32 {
    let a = top.a() as f32 / 255.0;
    let f = |t: u8, u: u8| ((t as f32) + (u as f32) * (1.0 - a)).round().min(255.0) as u8;
    Color32::from_rgba_premultiplied(f(top.r(), under.r()), f(top.g(), under.g()), f(top.b(), under.b()), f(top.a(), under.a()))
}

fn stack_color(rect: Rect, p: Pos2, s: &Sample, profile: Profile, inks: &MotionInks) -> Color32 {
    let d = [p.y - rect.top(), rect.right() - p.x, rect.bottom() - p.y, p.x - rect.left()];
    let mut out = Color32::TRANSPARENT;
    if d[2] < 1.0 {
        out = over(layer(inks.lip, 0x77 as f32 / 255.0), out); // layer 6: inner lower lip
    }
    for (li, &(side, _, _, _, _, contact)) in LAYERS.iter().enumerate().rev() {
        let a = shade(li, s.depth[side], d[side], profile);
        let col = if contact { inks.contact } else { s.edge[side] };
        out = over(layer(col, a), out);
    }
    out
}

/// The bevel mesh: concentric rings from the rim inward over the band, in
/// the theme's inks (`crate::motion::spec::inks`).
pub fn ring_mesh(rect: Rect, radius: f32, s: &Sample, profile: Profile, inks: &MotionInks) -> Mesh {
    const PER_CORNER: usize = 6;
    let band = profile.band();
    let steps: Vec<f32> = (0..=(band * 2.0) as usize).map(|i| i as f32 * 0.5).collect();
    let ring = |inset: f32| -> Vec<Pos2> {
        let r = rect.shrink(inset);
        let rad = (radius - inset).max(0.0);
        let centers = [
            (pos2(r.right() - rad, r.bottom() - rad), 0.0f32),
            (pos2(r.left() + rad, r.bottom() - rad), 1.0),
            (pos2(r.left() + rad, r.top() + rad), 2.0),
            (pos2(r.right() - rad, r.top() + rad), 3.0),
        ];
        let bx = |from: f32, to: f32| {
            let d = (to - from).signum() * band.min((to - from).abs() / 2.0);
            [from + d, to - d]
        };
        let mut pts = Vec::with_capacity(4 * PER_CORNER + 8);
        for (k, (c, q)) in centers.into_iter().enumerate() {
            for i in 0..PER_CORNER {
                let t = (q + i as f32 / (PER_CORNER - 1) as f32) * std::f32::consts::FRAC_PI_2;
                pts.push(pos2(c.x + rad * t.cos(), c.y + rad * t.sin()));
            }
            match k {
                0 => bx(r.right(), r.left()).iter().for_each(|&x| pts.push(pos2(x, r.bottom()))),
                1 => bx(r.bottom(), r.top()).iter().for_each(|&y| pts.push(pos2(r.left(), y))),
                2 => bx(r.left(), r.right()).iter().for_each(|&x| pts.push(pos2(x, r.top()))),
                _ => bx(r.top(), r.bottom()).iter().for_each(|&y| pts.push(pos2(r.right(), y))),
            }
        }
        pts
    };
    let n = 4 * PER_CORNER + 8;
    let mut mesh = Mesh::default();
    for &st in &steps {
        for p in ring(st) {
            mesh.colored_vertex(p, stack_color(rect, p, s, profile, inks));
        }
    }
    for j in 0..steps.len() - 1 {
        let (a, b) = ((j * n) as u32, ((j + 1) * n) as u32);
        for i in 0..n as u32 {
            let i2 = (i + 1) % n as u32;
            mesh.add_triangle(a + i, a + i2, b + i);
            mesh.add_triangle(a + i2, b + i2, b + i);
        }
    }
    mesh
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
struct CacheKey {
    rect: [i32; 4],
    radius: i32,
    depth: [u8; 4],
    edge: [[u8; 4]; 4],
    profile: Profile,
    /// The theme's inks: light and dark meshes never share an entry.
    inks: [[u8; 4]; 4],
}

/// Meshes by quantised rect, radius, depths and edge colours, so held and
/// resting frames reuse the mesh instead of rebuilding it.
#[derive(Default)]
pub struct MeshCache {
    map: HashMap<CacheKey, Mesh>,
}

impl MeshCache {
    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn get(&mut self, rect: Rect, radius: f32, s: &Sample, profile: Profile, inks: &MotionInks) -> Mesh {
        let q = |v: f32| (v * 2.0).round() as i32;
        let key = CacheKey {
            rect: [q(rect.left()), q(rect.top()), q(rect.right()), q(rect.bottom())],
            radius: q(radius),
            depth: s.depth.map(|d| (d / DEPTH_MAX * DEPTH_STEPS as f32).round().clamp(0.0, 255.0) as u8),
            edge: s.edge.map(|c| c.to_array()),
            profile,
            inks: [inks.contact, inks.link_dark, inks.link_light, inks.lip].map(|c| c.to_array()),
        };
        if self.map.len() > 512 {
            self.map.clear();
        }
        self.map
            .entry(key)
            .or_insert_with(|| ring_mesh(rect, radius, s, profile, inks))
            .clone()
    }
}
```

Run: `cargo test --locked motion::bevel 2>&1 | tail -n 3` — Expected: 3 passed. Commit c4.

- [ ] **Step c5: Link path and mesh (was CP-C T8 path).** `src/motion/link.rs`:

```rust
//! The gutter connection (T10 (b), (o), (p)): straight facing paths only,
//! capped at min(520, 0.65·w), otherwise omitted.

use egui::{pos2, Mesh, Pos2, Rect};
use std::time::Instant;

use crate::motion::spec::*;

pub fn max_link_len(window_width: f32) -> f32 {
    LINK_MAX_LEN.min(LINK_MAX_FRACTION * window_width)
}

/// A straight path between facing rims: horizontal when the rects overlap
/// by >= 4 pt vertically, vertical when they overlap horizontally.
pub fn facing_path(src: Rect, dst: Rect, max_len: f32) -> Option<[Pos2; 2]> {
    let (y0, y1) = (src.top().max(dst.top()), src.bottom().min(dst.bottom()));
    if y1 - y0 >= 4.0 {
        let y = (y0 + y1) / 2.0;
        let path = if dst.left() > src.right() {
            Some([pos2(src.right(), y), pos2(dst.left(), y)])
        } else if src.left() > dst.right() {
            Some([pos2(src.left(), y), pos2(dst.right(), y)])
        } else {
            None
        };
        if let Some(p) = path {
            return (p[0].distance(p[1]) <= max_len).then_some(p);
        }
    }
    let (x0, x1) = (src.left().max(dst.left()), src.right().min(dst.right()));
    if x1 - x0 >= 4.0 {
        let x = (x0 + x1) / 2.0;
        let path = if dst.top() > src.bottom() {
            Some([pos2(x, src.bottom()), pos2(x, dst.top())])
        } else if src.top() > dst.bottom() {
            Some([pos2(x, src.top()), pos2(x, dst.bottom())])
        } else {
            None
        };
        if let Some(p) = path {
            return (p[0].distance(p[1]) <= max_len).then_some(p);
        }
    }
    None
}

/// The link's animation state.
#[derive(Clone, Debug)]
pub struct Link {
    pub start: Instant,
    pub frozen: bool,
}

impl Link {
    /// (pattern 0..1, opacity 0..1, animating)
    pub fn sample(&self, now: Instant, reduced: bool) -> (f32, f32, bool) {
        if reduced || self.frozen {
            return (0.0, 1.0, false);
        }
        let t = now.saturating_duration_since(self.start).as_secs_f32() * 1000.0;
        let opacity = (t / LINK_FADE_IN_MS).clamp(0.0, 1.0);
        let pattern = if t < LINK_FADE_IN_MS {
            t / LINK_FADE_IN_MS
        } else if t < LINK_RELEASE_MS {
            1.0
        } else if t < LINK_END_MS {
            1.0 - (t - LINK_RELEASE_MS) / (LINK_END_MS - LINK_RELEASE_MS)
        } else {
            0.0
        };
        (pattern, opacity, t < LINK_END_MS)
    }
}

/// A 4 pt track from `a` to `b` (horizontal or vertical): 7 pt stripes of
/// the spectrum at 78 %, and the recess cross-gradient in the theme's inks.
pub fn link_mesh(a: Pos2, b: Pos2, pattern: f32, opacity: f32, inks: &MotionInks) -> Mesh {
    let mut m = Mesh::default();
    let horizontal = (a.y - b.y).abs() < 0.5;
    let (lo, hi) = if horizontal { (a.x.min(b.x), a.x.max(b.x)) } else { (a.y.min(b.y), a.y.max(b.y)) };
    let across = if horizontal { a.y } else { a.x };
    let rect_of = |s: f32, e: f32, c0: f32, c1: f32| {
        if horizontal {
            Rect::from_min_max(pos2(s, c0), pos2(e, c1))
        } else {
            Rect::from_min_max(pos2(c0, s), pos2(c1, e))
        }
    };
    let orange = seat(ORANGE);
    let (mut s, mut i) = (lo, 0);
    while s < hi {
        let e = (s + 7.0).min(hi);
        let c = orange.lerp_to_gamma(seat(SPECTRUM[i % 4]), pattern);
        m.add_colored_rect(rect_of(s, e, across - 2.0, across + 2.0), layer(c, 0.78 * opacity));
        s = e;
        i += 1;
    }
    m.add_colored_rect(rect_of(lo, hi, across - 2.0, across - 1.2), layer(inks.link_dark, 0.18 * opacity));
    m.add_colored_rect(rect_of(lo, hi, across + 1.1, across + 2.0), layer(inks.link_light, 0.44 * opacity));
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn facing_paths() {
        let a = Rect::from_min_max(pos2(0.0, 100.0), pos2(400.0, 124.0));
        let b = Rect::from_min_max(pos2(416.0, 50.0), pos2(1000.0, 600.0));
        assert_eq!(facing_path(a, b, 520.0), Some([pos2(400.0, 112.0), pos2(416.0, 112.0)]));
        let q = Rect::from_min_max(pos2(420.0, 300.0), pos2(520.0, 336.0));
        let r = Rect::from_min_max(pos2(416.0, 352.0), pos2(1000.0, 600.0));
        assert_eq!(facing_path(q, r, 520.0), Some([pos2(470.0, 336.0), pos2(470.0, 352.0)]));
        let far = Rect::from_min_max(pos2(2000.0, 100.0), pos2(2100.0, 124.0));
        assert_eq!(facing_path(a, far, max_link_len(1400.0)), None, "longer than the cap");
        let diagonal = Rect::from_min_max(pos2(600.0, 400.0), pos2(700.0, 500.0));
        assert_eq!(facing_path(a, diagonal, 520.0), None, "no facing geometry");
    }

    #[test]
    fn link_pattern_holds_then_settles() {
        let t0 = Instant::now();
        let l = Link { start: t0, frozen: false };
        let ms = |v: u64| t0 + std::time::Duration::from_millis(v);
        assert_eq!(l.sample(ms(500), false).0, 1.0);
        assert_eq!(l.sample(ms(1200), false), (0.0, 1.0, false));
        assert_eq!(l.sample(ms(10), true), (0.0, 1.0, false), "reduced: static");
    }
}
```

Run: `cargo test --locked motion::link 2>&1 | tail -n 3` — Expected: 2 passed. Commit c5.

- [ ] **Step c6-1: Repaint policy and the `Motion` facade, tests first (was CP-C T9, T11, T14, T15).** `src/motion/repaint.rs`:

```rust
//! Repaint policy (F-T11-2, F-T11-5): egui subtracts `predicted_dt` from
//! every delay (egui-0.29.1 context.rs:187-190), so every request here adds it.

use std::io::Write as _;
use std::time::{Duration, Instant};

/// One motion step: 60 Hz, the cap chosen for the port.
pub const STEP_SECS: f32 = 1.0 / 60.0;

pub fn repaint_delay(step: f32, predicted_dt: f32) -> Duration {
    Duration::from_secs_f32(step + predicted_dt)
}

pub fn wake_after(remaining_ms: f32, predicted_dt: f32) -> Duration {
    Duration::from_secs_f32(remaining_ms.max(0.0) / 1000.0 + predicted_dt)
}

/// Optional per-frame log for T3 stage 3: `ZE_MOTION_LOG=<file>`.
pub struct IntervalLog {
    out: std::io::BufWriter<std::fs::File>,
    last: Option<Instant>,
}

impl IntervalLog {
    pub fn from_env() -> Option<Self> {
        let path = std::env::var_os("ZE_MOTION_LOG")?;
        let file = std::fs::File::create(path).ok()?;
        Some(Self { out: std::io::BufWriter::new(file), last: None })
    }

    pub fn frame(&mut self, now: Instant, animating: bool, effect_us: f64) {
        let interval = self.last.map_or(0.0, |l| now.duration_since(l).as_secs_f64() * 1000.0);
        self.last = Some(now);
        let _ = writeln!(self.out, "interval_ms={interval:.2} animating={animating} effect_us={effect_us:.0}");
        let _ = self.out.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repaint_delay_adds_predicted_dt() {
        let dt = 1.0 / 60.0;
        let d = repaint_delay(STEP_SECS, dt);
        assert!((d.as_secs_f32() - (STEP_SECS + dt)).abs() < 1e-6);
        assert!((wake_after(1155.0, dt).as_secs_f32() - (1.155 + dt)).abs() < 1e-5);
        assert_eq!(wake_after(-5.0, 0.0), Duration::ZERO);
    }
}
```

Test module for `src/motion/mod.rs` (append after the facade in c6-2):

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use egui::{Context, Rect, Shape};
    use std::time::Duration;

    /// One headless frame with a face registered as `key`; returns the output.
    fn frame(m: &mut Motion, ctx: &Context, now: Instant, keys: &[SurfaceKey]) -> egui::FullOutput {
        ctx.run(egui::RawInput::default(), |ctx| {
            m.begin_frame(now);
            egui::CentralPanel::default().show(ctx, |ui| {
                let link_slot = ui.painter().add(Shape::Noop);
                m.link_slot(ui.painter(), link_slot, ui.max_rect());
                for (i, key) in keys.iter().enumerate() {
                    let rect = Rect::from_min_size(egui::pos2(20.0, 20.0 + i as f32 * 120.0), egui::vec2(200.0, 100.0));
                    let slot = ui.painter().add(Shape::Noop);
                    m.surface(key.clone(), rect, ui.painter(), slot, 8.0);
                }
            });
            m.end_frame(ctx, now);
        })
    }

    fn meshes(out: &egui::FullOutput) -> usize {
        out.shapes.iter().filter(|c| matches!(c.shape, Shape::Mesh(_))).count()
    }

    fn repaint_delay_of(out: &egui::FullOutput) -> Duration {
        out.viewport_output[&egui::ViewportId::ROOT].repaint_delay
    }

    #[test]
    fn reveal_paints_into_slots_then_settles() {
        let ctx = Context::default();
        let mut m = Motion::new();
        let t0 = Instant::now();
        let keys = [SurfaceKey::query_submit(), SurfaceKey::query_results()];
        m.begin(ActionKind::Query, t0);
        let out = frame(&mut m, &ctx, t0, &keys);
        assert_eq!(meshes(&out), 1, "pending: only the source");
        assert!(repaint_delay_of(&out) < Duration::from_millis(20), "animating at 60 Hz");
        m.commit(Commit::QueryAnswered, t0 + Duration::from_millis(300));
        let out = frame(&mut m, &ctx, t0 + Duration::from_millis(600), &keys);
        assert!(meshes(&out) >= 2, "source, result and the link");
        let _ = frame(&mut m, &ctx, t0 + Duration::from_millis(3000), &keys);
        let out = frame(&mut m, &ctx, t0 + Duration::from_millis(3016), &keys);
        assert_eq!(meshes(&out), 3, "source and result rest latched; the static link stays (T9)");
        assert_eq!(repaint_delay_of(&out), Duration::MAX, "idle after the settle");
    }

    #[test]
    fn failed_pending_never_reveals() {
        let ctx = Context::default();
        let mut m = Motion::new();
        let t0 = Instant::now();
        m.begin(ActionKind::Publish("k".into()), t0);
        m.fail(Failure::Publish, 0, t0);
        m.commit(Commit::Published("k".into()), t0);
        let out = frame(&mut m, &ctx, t0 + Duration::from_millis(400), &[SurfaceKey::pub_submit(), SurfaceKey::pub_status()]);
        assert_eq!(meshes(&out), 0);
        assert!(!m.owns(&SurfaceKey::pub_submit()));
    }

    fn mesh_vertices(out: &egui::FullOutput) -> Vec<egui::epaint::Vertex> {
        out.shapes
            .iter()
            .filter_map(|c| match &c.shape {
                Shape::Mesh(m) => Some(m.vertices.clone()),
                _ => None,
            })
            .flatten()
            .collect()
    }

    /// User decision: reduced motion is off completely. The end state appears
    /// at once: no relay, no animation frame, no timed wake-up (no relay
    /// removal near 1155 ms), no pulse, and nothing changes later.
    #[test]
    fn reduced_motion_is_static() {
        let ctx = Context::default();
        let mut m = Motion::new();
        let t0 = Instant::now();
        m.set_reduced(true, t0);
        m.begin(ActionKind::TreeSelect("demo/x".into()), t0);
        assert!(!m.owns(&SurfaceKey::tree_panel()), "no relay under reduced motion");
        assert!(m.owns(&SurfaceKey::tree("demo/x")) && m.owns(&SurfaceKey::detail_body()), "source and result at once");
        let keys = [SurfaceKey::tree("demo/x"), SurfaceKey::tree_panel(), SurfaceKey::detail_body()];
        let _ = frame(&mut m, &ctx, t0, &keys);
        let first = frame(&mut m, &ctx, t0 + Duration::from_millis(16), &keys);
        assert_eq!(repaint_delay_of(&first), Duration::MAX, "no animation frame and no timed wake-up");
        let later = frame(&mut m, &ctx, t0 + Duration::from_millis(2000), &keys);
        assert!(meshes(&first) >= 2, "the end state is painted");
        assert_eq!(mesh_vertices(&first), mesh_vertices(&later), "nothing fades, moves or leaves");
        m.value_changed(SurfaceKey::tree("demo/y"), t0);
        assert_eq!(m.live_pulses(), 0, "no value pulse");
        // a pending action shows its pending face at once; only the 15 s timeout wakes
        let t1 = t0 + Duration::from_millis(3000);
        m.begin(ActionKind::Query, t1);
        let keys = [SurfaceKey::query_submit(), SurfaceKey::query_results()];
        let _ = frame(&mut m, &ctx, t1, &keys);
        let out = frame(&mut m, &ctx, t1 + Duration::from_millis(16), &keys);
        assert!(repaint_delay_of(&out) > Duration::from_secs(10), "no 60 Hz rise: {:?}", repaint_delay_of(&out));
    }

    #[test]
    fn value_change_pulses_once_per_row() {
        let mut m = Motion::new();
        let t0 = Instant::now();
        m.value_changed(SurfaceKey::tree("a"), t0);
        m.value_changed(SurfaceKey::tree("a"), t0 + Duration::from_millis(100));
        assert_eq!(m.live_pulses(), 1, "coalesced under high rates");
    }
}
```

- [ ] **Step c6-2: Implement the facade.** Append to `src/motion/mod.rs` (after the `pub use` lines, before the tests):

```rust
use std::collections::HashMap;
use std::time::Instant;

use egui::layers::ShapeIdx;
use egui::{Painter, Rect, Shape, Stroke};

use sampler::{Role, Surface};

struct Registered {
    rect: Rect,
    painter: Painter,
    slot: ShapeIdx,
    radius: f32,
}

struct LinkSlot {
    painter: Painter,
    slot: ShapeIdx,
    clip: Rect,
}

/// The app's motion state: one live action (supersede), its surfaces, the
/// link, reduced motion, this frame's registrations and the mesh cache.
pub struct Motion {
    ledger: ledger::Ledger,
    surfaces: HashMap<SurfaceKey, Surface>,
    pulses: HashMap<SurfaceKey, Surface>,
    link: Option<link::Link>,
    plan: Option<ledger::ActionPlan>,
    reduced: bool,
    frame: HashMap<SurfaceKey, Registered>,
    link_slots: Vec<LinkSlot>,
    last_rects: HashMap<SurfaceKey, Rect>,
    cache: bevel::MeshCache,
    log: Option<repaint::IntervalLog>,
}

impl Default for Motion {
    fn default() -> Self {
        Self::new()
    }
}

impl Motion {
    pub fn new() -> Self {
        Motion {
            ledger: ledger::Ledger::default(),
            surfaces: HashMap::new(),
            pulses: HashMap::new(),
            link: None,
            plan: None,
            reduced: false,
            frame: HashMap::new(),
            link_slots: Vec::new(),
            last_rects: HashMap::new(),
            cache: bevel::MeshCache::default(),
            log: repaint::IntervalLog::from_env(),
        }
    }

    pub fn reduced(&self) -> bool {
        self.reduced
    }

    /// Switching either way mid-response settles latched surfaces at their end
    /// state, drops relays and pulses and freezes the link (T9 `settleMotion`),
    /// all at once. While `reduced` is on, nothing animates (user decision).
    pub fn set_reduced(&mut self, on: bool, _now: Instant) {
        if on == self.reduced {
            return;
        }
        self.reduced = on;
        self.surfaces.retain(|_, s| s.latched || s.pending);
        for s in self.surfaces.values_mut() {
            s.settled = true;
        }
        self.pulses.clear();
        if let Some(l) = &mut self.link {
            l.frozen = true;
        }
    }

    /// A click on a source. Supersedes the live action (CP-C T11).
    pub fn begin(&mut self, kind: ActionKind, now: Instant) {
        let before = self.ledger.current.as_ref().map(|a| a.id);
        let id = self.ledger.begin(kind.clone(), now);
        if before == Some(id) {
            return; // a coalesced filter keystroke
        }
        let plan = kind.plan();
        self.surfaces.clear();
        self.link = None;
        if plan.pending {
            self.surfaces.insert(plan.source.clone(), Surface::pending(now));
            self.plan = Some(plan);
        } else {
            self.reveal(plan, now);
        }
    }

    fn reveal(&mut self, plan: ledger::ActionPlan, now: Instant) {
        let rim = plan.rim_delay_ms;
        self.surfaces.insert(plan.source.clone(), Surface::reveal(Role::Source, now, rim));
        if let Some(relay) = &plan.relay {
            // Reduced motion is off completely (user decision): no relay, so no
            // transient surface and no timed removal.
            if !self.reduced {
                self.surfaces.insert(relay.clone(), Surface::reveal(Role::Relay, now, rim));
            }
        }
        for r in &plan.results {
            self.surfaces.insert(r.clone(), Surface::reveal(Role::Result, now, rim));
        }
        if plan.link {
            self.link = Some(link::Link { start: now, frozen: false });
        }
        self.plan = Some(plan);
    }

    pub fn commit(&mut self, c: Commit, now: Instant) {
        if self.ledger.commit(&c, now).is_some() {
            if let Some(plan) = self.plan.clone() {
                self.reveal(plan, now);
            }
        }
    }

    pub fn fail(&mut self, f: Failure, pending_subscribes: usize, now: Instant) {
        if self.ledger.fail(&f, pending_subscribes, now) {
            self.surfaces.clear();
            self.link = None;
        }
    }

    /// True while the motion effect paints this surface's whole shadow stack.
    pub fn owns(&self, key: &SurfaceKey) -> bool {
        self.surfaces.contains_key(key)
    }

    pub fn is_pending(&self) -> bool {
        self.ledger.is_pending()
    }

    pub fn begin_frame(&mut self, now: Instant) {
        if self.ledger.tick(now) {
            self.surfaces.clear(); // the 15 s timeout ends pending without a reveal
        }
        self.frame.clear();
        self.link_slots.clear();
    }

    /// Registers a surface laid out this frame, with the slot reserved above
    /// its fill and below its content.
    pub fn surface(&mut self, key: SurfaceKey, rect: Rect, painter: &Painter, slot: ShapeIdx, radius: f32) {
        self.frame.insert(key, Registered { rect, painter: painter.clone(), slot, radius });
    }

    /// A slot under module faces where a link may be painted; the innermost
    /// slot whose clip holds both ends is used.
    pub fn link_slot(&mut self, painter: &Painter, slot: ShapeIdx, clip: Rect) {
        self.link_slots.push(LinkSlot { painter: painter.clone(), slot, clip });
    }

    /// A count or value change on a row: one short edge response, coalesced.
    pub fn value_changed(&mut self, key: SurfaceKey, now: Instant) {
        if self.reduced || self.surfaces.contains_key(&key) {
            return;
        }
        let live = self.pulses.get(&key).is_some_and(|p| !p.expired(now));
        if !live {
            self.pulses.insert(key, Surface::pulse(now));
        }
    }

    #[cfg(test)]
    fn live_pulses(&self) -> usize {
        self.pulses.len()
    }

    /// Paints every live surface into its slot, the link into the innermost
    /// fitting slot, and asks for the next repaint.
    pub fn end_frame(&mut self, ctx: &egui::Context, now: Instant) {
        let started = Instant::now();
        let seen = &self.frame;
        self.surfaces.retain(|k, s| seen.contains_key(k) && !s.expired(now));
        self.pulses.retain(|k, s| seen.contains_key(k) && !s.expired(now));
        let source = self.plan.as_ref().map(|p| p.source.clone());
        let src_rect = source
            .as_ref()
            .and_then(|k| self.frame.get(k).map(|r| r.rect).or_else(|| self.last_rects.get(k).copied()));
        let mut animating = false;
        let mut next_ms: Option<f32> = None;
        let inks = spec::inks(ctx.style().visuals.dark_mode);
        for (key, reg) in &self.frame {
            let Some(s) = self.surfaces.get(key).or_else(|| self.pulses.get(key)) else {
                continue;
            };
            let origin = src_rect.map_or(reg.rect.left_top(), |r| r.center());
            let smp = s.sample(now, reg.rect, origin, self.reduced);
            animating |= smp.animating;
            if let Some(ms) = s.next_discrete_ms(now) {
                next_ms = Some(next_ms.map_or(ms, |m: f32| m.min(ms)));
            }
            let profile = bevel::Profile::for_rect(reg.rect);
            let mesh = self.cache.get(reg.rect, reg.radius, &smp, profile, inks);
            reg.painter.set(reg.slot, Shape::mesh(mesh));
            // layer 7: the outer lower catchlight
            reg.painter.hline(
                reg.rect.x_range().shrink(reg.radius),
                reg.rect.bottom() + 0.5,
                Stroke::new(1.0, spec::layer(inks.lip, 0x70 as f32 / 255.0)),
            );
        }
        if let (Some(l), Some(src), Some(plan)) = (&self.link, src_rect, &self.plan) {
            let dst = plan.results.first().and_then(|k| self.frame.get(k)).map(|r| r.rect);
            let max = link::max_link_len(ctx.screen_rect().width());
            if let Some(path) = dst.and_then(|d| link::facing_path(src, d, max)) {
                let fits = |s: &&LinkSlot| s.clip.contains(path[0]) && s.clip.contains(path[1]);
                let slot = self
                    .link_slots
                    .iter()
                    .filter(fits)
                    .min_by(|a, b| a.clip.area().total_cmp(&b.clip.area()));
                if let Some(slot) = slot {
                    let (pattern, opacity, anim) = l.sample(now, self.reduced);
                    animating |= anim;
                    slot.painter
                        .set(slot.slot, Shape::mesh(link::link_mesh(path[0], path[1], pattern, opacity, inks)));
                }
            }
        }
        self.last_rects = self.frame.iter().map(|(k, r)| (k.clone(), r.rect)).collect();
        let dt = ctx.input(|i| i.predicted_dt);
        if animating {
            ctx.request_repaint_after(repaint::repaint_delay(repaint::STEP_SECS, dt));
        } else if let Some(ms) = next_ms {
            ctx.request_repaint_after(repaint::wake_after(ms, dt));
        }
        if let Some(left) = self.ledger.pending_remaining(now) {
            ctx.request_repaint_after(repaint::wake_after(left.as_secs_f32() * 1000.0, dt));
        }
        if let Some(log) = &mut self.log {
            if animating {
                log.frame(now, animating, started.elapsed().as_secs_f64() * 1e6);
            }
        }
    }
}
```

Run: `cargo test --locked motion:: 2>&1 | grep -E '^test |test result'`
Expected: every `motion::` test ok (spec 2, sampler 6, ledger 7, bevel 3, link 2, repaint 1, facade 4). If egui itself asks for a repaint on an early frame, the idle assertions still hold because each test renders one extra frame before asserting. Commit c6 (`feat(sw-c6): motion facade, repaint policy, reduced motion (was CP-C T9, T11, T14, T15)`).

- [ ] **Step c7: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE1" HEAD` lists only `src/motion/*`.

### Part a (stage 2; structure for CP-A3 T3–T6, T11, CP-A2 T8, CP-A1 T6 and the probe method of CP-A3 T13): split the big files, add the probe

Part a changes no behaviour except what b and c already changed (the ivory default is set here: `dark_mode: false`, CP-B T10). Moves are verbatim: when a step says "move lines X–Y", the code is cut and pasted unchanged except for the named edits. Line numbers are `3ce8c01`'s.

**Files:**
- Modify: `src/app/layout.rs`, `src/app/mod.rs`, `src/ui/mod.rs`, `src/ui/topic_tree.rs`, `src/ui/messages.rs`, `src/ui/help.rs`, `src/types/message.rs`, `src/types/mod.rs` (SW-T2 tags only), `src/validation.rs`
- Create: `src/app/header.rs`, `src/app/probe.rs` (`#[cfg(test)]`), `src/ui/connection.rs`, `src/ui/limits.rs`, `src/ui/topic_details.rs`, `src/ui/message_row.rs`

**Interfaces:**
- Consumes: `crate::style::{install, p, text, badge}` (part b), `crate::motion::Motion` (part c).
- Produces (frozen for T2):
  - `ZenohExplorer::frame_ui(&mut self, ctx: &egui::Context)`; `ZenohExplorer::show_header(&mut self, ui: &mut egui::Ui)` in `src/app/header.rs`
  - `crate::ui::connection::{ConnectionUI, connect_hint, locator_preview, form_locators, connect_port_error, listen_port_error}` with `ConnectionUI::{show_connection_form(&mut self, ui: &mut egui::Ui), start_connect(&mut self, ctx: &egui::Context)}` and `ZenohExplorer::ports_ok(&self) -> bool`
  - `crate::ui::limits::LimitsUI::show_limits_controls(&mut self, ui: &mut egui::Ui)`
  - `crate::ui::topic_details::DetailsUI::{show_detail_panel, show_topic_details, save_topic_to_file}` (same signatures as the old `TopicTreeUI` methods); `crate::ui::topic_tree::counted` becomes `pub(crate)`
  - `crate::ui::message_row::{RowParts, row_parts, row_text, types_mixed, message_row}`
  - `DetailView::Connection`, `DetailView::ALL: [DetailView; 5]`, `DetailView::label(self) -> &'static str`; `DetailView` derives `Clone, Copy, Debug, PartialEq, Eq, Hash`
  - `crate::ui::help::{section, CONNECT_HINT_PEER, CONNECT_HINT_CLIENT, QUERYABLE_CAPTION, KEY_RULE}` and `ZenohExplorer::help_link(&mut self, ui: &mut egui::Ui, section: &'static str) -> egui::Response`
  - `crate::validation::field_number<T: FromStr>(s: &str, fallback: T) -> T`
  - fields on `ZenohExplorer`: `ui_alert_since: Option<(UiAlert, Instant)>`, `query_sent_at: Option<(Instant, u64)>`, `help_target: Option<&'static str>`, `limits_open: bool`, `tree_filter_counts: Option<(usize, usize)>`, `save_result: Option<(String, UiAlert)>`, `import_message: Option<String>`, `connection_view_shown: bool`, `motion: crate::motion::Motion`
  - `crate::app::probe::{Probe, ProbeFrame, Painted, Node, WIDE, NARROW, tab_event, click_events}` (`cfg(test)`)

- [ ] **Step a1: `frame_ui`.** In `src/app/layout.rs`, reduce `eframe::App::update` (lines 70-591) to:

```rust
impl eframe::App for ZenohExplorer {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        // First frame debug message and ensure window is visible
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            info!("First UI update frame - window should be visible now");
            ctx.send_viewport_cmd(egui::ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(egui::ViewportCommand::Focus);
        });
        self.frame_ui(ctx);
    }
}

impl ZenohExplorer {
    /// One frame of the whole window. Tests call this directly: eframe 0.29's
    /// `Frame` cannot be constructed outside eframe (epi.rs:586).
    pub(crate) fn frame_ui(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        self.motion.begin_frame(now);
        // (lines 79-586 of the old update(), moved verbatim, with a2 and a3's edits)
        self.motion.end_frame(ctx, now);
        // Worker events wake the UI through the buffer thread; this slow tick
        // only keeps time-based readouts (health, elapsed times) current
        ctx.request_repaint_after(std::time::Duration::from_secs(IDLE_REPAINT_SECS));
    }
}
```

Run: `cargo check --locked 2>&1 | tail -n 2` — Expected: `Finished` (the `motion` field is added in a6; do a6's field edit together with this step if the checker stops here).

- [ ] **Step a2: `src/app/header.rs`.** Create:

```rust
//! The header row: title, theme control, worker health, connection status,
//! peers, memory and drop counters. Moved from layout.rs:104-268; T2 part a
//! turns it into fixed slots.

use eframe::egui;
use egui::RichText;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use crate::app::{memory_readout, peers_text, MemLevel, ZenohExplorer};
use crate::colors::ExplorerColors;
use crate::transfer;
use crate::types::*;

impl ZenohExplorer {
    /// Renders the header row.
    pub(crate) fn show_header(&mut self, ui: &mut egui::Ui) {
        // the body of layout.rs:104-268, i.e. the whole `ui.horizontal(|ui| { … });` block, moved verbatim
    }
}
```

Paste the `ui.horizontal(|ui| { … });` statement of `layout.rs:104-268` as the body. In `layout.rs`, replace it with `self.show_header(ui);`. Declare the module in `src/app/mod.rs` next to `mod layout;`: `mod header;`. Remove imports from `layout.rs` that are no longer used there (`memory_readout`, `peers_text`, `MemLevel`, `transfer`, `Ordering`, `Duration`); keep `Instant`.

- [ ] **Step a3: `src/ui/connection.rs`.** Create it with the connection form, the Connect body and the port checks moved out of `layout.rs`:

```rust
//! The connection form (moved from layout.rs:16-65 and 277-479). T2 part c
//! turns it into the Connection view; the header's key calls `start_connect`.

use eframe::egui;
use egui::RichText;
use std::time::Instant;
use tracing::{error, info};

use crate::app::ZenohExplorer;
use crate::colors::ExplorerColors;
use crate::types::*;
use crate::validation;

/// Where egui keeps the `(mode, locators, listen_port)` of the last Connect.
pub(crate) const LAST_ATTEMPT_ID: &str = "last_connect_attempt";

/// The connection form's guidance for `mode`, true of the form as shown.
pub(crate) fn connect_hint(mode: &str, _address: &str) -> &'static str {
    if mode == "client" {
        crate::ui::help::CONNECT_HINT_CLIENT
    } else {
        crate::ui::help::CONNECT_HINT_PEER
    }
}

// locator_preview, form_locators, connect_port_error, listen_port_error:
// moved verbatim from layout.rs:28-65, each made `pub(crate)`.

pub trait ConnectionUI {
    /// The form's fields, hint, error and the resume note.
    fn show_connection_form(&mut self, ui: &mut egui::Ui);
    /// Sends Connect with the form's values (the old Connect body).
    fn start_connect(&mut self, ctx: &egui::Context);
}

impl ZenohExplorer {
    /// Both port fields are valid for the chosen mode.
    pub(crate) fn ports_ok(&self) -> bool {
        connect_port_error(&self.connect_address, &self.connect_port).is_none()
            && listen_port_error(&self.connection_mode, &self.listen_port).is_none()
    }
}

impl ConnectionUI for ZenohExplorer {
    fn show_connection_form(&mut self, ui: &mut egui::Ui) {
        // layout.rs:278-422 verbatim (from `ui.label("Connection Settings");`
        // through the resume note; not 423-428, whose `let ports_ok` the
        // `self.ports_ok()` below replaces), then:
        if ui
            .add_enabled(self.ports_ok(), egui::Button::new("Connect"))
            .clicked()
        {
            self.start_connect(ui.ctx());
        }
    }

    fn start_connect(&mut self, ctx: &egui::Context) {
        let attempt_id = egui::Id::new(LAST_ATTEMPT_ID);
        // layout.rs:433-478 verbatim, with `ui.ctx().data_mut` → `ctx.data_mut`
    }
}
```

Move the two tests `connection_hints_match_the_form` and `form_locators_trim_inputs` (layout.rs:594-634) into a `#[cfg(test)] mod tests` at the end of `connection.rs`. In `layout.rs` the form's `ui.group(|ui| { … })` body becomes `ui.group(|ui| self.show_connection_form(ui));`, the `LAST_ATTEMPT_ID` constant and lines 16-65 are deleted, and `use crate::ui::connection::ConnectionUI;` is added. With the port checks and the Connect body gone, `layout.rs` no longer uses `error!` or `validation`: change `use tracing::{error, info};` to `use tracing::info;` and delete `use crate::validation;`. Add `pub mod connection;` to `src/ui/mod.rs`. In `src/ui/help.rs` add, above `HELP_SECTIONS` (a7 adds the rest):

```rust
pub(crate) const CONNECT_HINT_PEER: &str = "Peer mode: finds peers by multicast. Listen Port is where other peers reach this app. Use a different Listen Port for each copy on one machine. Address is optional.";
pub(crate) const CONNECT_HINT_CLIENT: &str = "Client mode: enter the router's address (for example localhost) and its port (7447).";
```

Run: `cargo test --locked connection 2>&1 | grep -E 'connection_hints_match_the_form|form_locators_trim_inputs'` — Expected: both ok.

- [ ] **Step a4: `src/ui/limits.rs`.** Create:

```rust
//! Session limits and the dedup switch (moved from messages.rs:118-153).
//! T2 part a shows them in the header's limits popover; T2 part i lays the
//! fields out vertically and adds a Limits help link (the counters, the
//! memory warning and the reduced-motion toggle live in the header, T2 part
//! a). P3 T14 converts the fields to DragValues here.

use egui::RichText;

use crate::app::ZenohExplorer;
use crate::types::*;

pub trait LimitsUI {
    fn show_limits_controls(&mut self, ui: &mut egui::Ui);
}

impl LimitsUI for ZenohExplorer {
    fn show_limits_controls(&mut self, ui: &mut egui::Ui) {
        // messages.rs:119-153 verbatim: the `ui.horizontal(|ui| { … });` holding
        // Memory Limit, Message Limit, Rate Limit, Dedup and "(n deduped)"
    }
}
```

In `messages.rs` replace lines 118-153 with `self.show_limits_controls(ui);` and add `use crate::ui::limits::LimitsUI;`. Add `pub mod limits;` to `src/ui/mod.rs`.

- [ ] **Step a5: `src/ui/topic_details.rs`.** Create it with a `DetailsUI` trait and move from `src/ui/topic_tree.rs`: `show_detail_panel` (lines 326-334), `show_topic_details` (336-716), `save_topic_to_file` (942-965), `history_scan_note` (1009-1016), `history_empty_reason` (1036-1045), `format_age` (1047-1056), `branch_summary_text` (1058-1072), and the tests `details_texts`, `topic_details_show_delete_and_source_time`, `history_names_the_scan_window_instead_of_claiming_empty`, `history_empty_reason_rules`, `history_excludes_query_replies` (1107-1311). In the moved `details_texts`, add `crate::style::install(&ctx);` right after `let ctx = egui::Context::default();` and before `ctx.run(..)`: part e renders this view with the named `Label` and `Legend` text styles (`text::label`, `legend_text` via `message_row`), and `TextStyle::resolve` panics on a name the context has not registered, so without the install the four moved P1 tests would panic after part e.

```rust
//! The detail panel: topic details, history and the Save flow (moved from
//! topic_tree.rs). T2 part e reworks it.

use egui::RichText;
use std::time::Instant;

use crate::app::{UiAlert, ZenohExplorer};
use crate::colors::ExplorerColors;
use crate::transfer;
use crate::types::*;
use crate::ui::connection::ConnectionUI;
use crate::ui::help::HelpUI;
use crate::ui::messages::MessagesUI;
use crate::ui::publish::PublishUI;
use crate::ui::query::QueryUI;
use crate::ui::topic_tree::{counted, TopicTreeUI};

pub trait DetailsUI {
    fn show_detail_panel(&mut self, ui: &mut egui::Ui);
    fn show_topic_details(&mut self, ui: &mut egui::Ui);
    fn save_topic_to_file(&mut self, topic: &str);
}

impl DetailsUI for ZenohExplorer {
    /// Renders the right detail panel based on current view mode
    fn show_detail_panel(&mut self, ui: &mut egui::Ui) {
        match self.detail_view {
            DetailView::TopicDetails => self.show_topic_details(ui),
            DetailView::Publish => self.show_publish_tab(ui),
            DetailView::Query => self.show_query_tab(ui),
            DetailView::Connection => self.show_connection_form(ui),
            DetailView::Help => self.show_help_tab(ui),
        }
    }
    // show_topic_details and save_topic_to_file: moved verbatim
}
```

In `topic_tree.rs`: remove the three methods from `TopicTreeUI` (declaration and impl), make `fn counted` `pub(crate)`, add `use crate::ui::topic_details::DetailsUI;` (the row 💾 calls `save_topic_to_file`), and drop imports that became unused. In `layout.rs` add `use crate::ui::topic_details::DetailsUI;`. Add `pub mod topic_details;` to `src/ui/mod.rs`.

Run: `cargo test --locked topic_ 2>&1 | grep -E '^test |test result'` — Expected: the four moved detail tests pass under `ui::topic_details::tests`, and `double_subscribe_is_ignored_while_pending`, `leaf_icons_bucket_correctly`, `count_hover_names_unit` pass under `ui::topic_tree::tests`.

- [ ] **Step a6: `DetailView`, fields, `field_number`.** In `src/types/message.rs` replace the `DetailView` enum (lines 205-212) with:

```rust
/// View modes for the right panel detail area
#[derive(PartialEq, Eq, Hash, Debug, Clone, Copy)]
pub enum DetailView {
    TopicDetails,
    Publish,
    Query,
    /// The connection settings (Q9 (a)); T2 part b gives it a tab.
    Connection,
    Help,
}

impl DetailView {
    /// Every view, in tab order.
    #[allow(dead_code)] // SW-T2: first non-test reader is the tab bank of T2 part b (it also keeps `Connection` constructed)
    pub const ALL: [DetailView; 5] = [
        DetailView::TopicDetails,
        DetailView::Publish,
        DetailView::Query,
        DetailView::Connection,
        DetailView::Help,
    ];

    /// The tab word: one covered glyph plus a word (F-T4-4).
    pub fn label(self) -> &'static str {
        match self {
            DetailView::TopicDetails => "📊 Topics",
            DetailView::Publish => "📤 Publish",
            DetailView::Query => "🔍 Query",
            DetailView::Connection => "🔌 Connection",
            DetailView::Help => "❓ Help",
        }
    }
}
```

and add to its test module:

```rust
    #[test]
    fn detail_view_labels_cover_all() {
        let labels: std::collections::HashSet<_> = DetailView::ALL.iter().map(|v| v.label()).collect();
        assert_eq!(labels.len(), DetailView::ALL.len());
        assert!(DetailView::ALL.iter().all(|v| v.label().split_once(' ').is_some()), "glyph plus word");
    }
```

In `layout.rs`, the four tab labels `"📊 Topics"`, `"📤 Publish"`, `"🔍 Query"`, `"❓ Help"` become `DetailView::TopicDetails.label()`, `DetailView::Publish.label()`, `DetailView::Query.label()`, `DetailView::Help.label()` (same words; the Connection tab comes in T2 part b).

In `src/app/mod.rs` add to `ZenohExplorer` (after `pending_subscribes`) and initialise in `new()`:

```rust
    /// The alert the status strip shows and when it appeared.
    #[allow(dead_code)] // SW-T2: read by T2 part b (strip expiry)
    pub(crate) ui_alert_since: Option<(UiAlert, Instant)>,
    /// When the pending query was sent, and its timeout in ms.
    #[allow(dead_code)] // SW-T2: read by T2 part g (pending line)
    pub(crate) query_sent_at: Option<(Instant, u64)>,
    /// The Help heading to scroll to once.
    #[allow(dead_code)] // SW-T2: read by T2 part h
    pub(crate) help_target: Option<&'static str>,
    /// The header's limits popover is open.
    #[allow(dead_code)] // SW-T2: read by T2 part a
    pub(crate) limits_open: bool,
    /// (leaf topics matching the filter, all leaf topics).
    #[allow(dead_code)] // SW-T2: read by T2 part d
    pub(crate) tree_filter_counts: Option<(usize, usize)>,
    /// The inline result of the last Save, by topic.
    #[allow(dead_code)] // SW-T2: read by T2 part e
    pub(crate) save_result: Option<(String, UiAlert)>,
    /// The Publish view's message line under Import.
    #[allow(dead_code)] // SW-T2: read by T2 part f
    pub(crate) import_message: Option<String>,
    /// The Connection view was shown once at launch.
    #[allow(dead_code)] // SW-T2: read by T2 part b
    pub(crate) connection_view_shown: bool,
    /// Causal motion state (crate::motion).
    pub(crate) motion: crate::motion::Motion,
```

```rust
            ui_alert_since: None,
            query_sent_at: None,
            help_target: None,
            limits_open: false,
            tree_filter_counts: None,
            save_result: None,
            import_message: None,
            connection_view_shown: false,
            motion: crate::motion::Motion::new(),
```

and change `dark_mode: true,` to `dark_mode: false, // ivory by default (CP-B T10, Q2 decided by the user)`. Reduced motion needs no field: `Motion::new()` starts with it off at every launch (Q4, not persisted until P3 T5).

Pre-tag the legacy items whose last callers T2 parts remove from files they do not own, so each part's `-D warnings` check and the T2 integration clippy stay clean until T3 stage 1 Step 2 deletes them: in `src/types/message.rs` put `#[allow(dead_code)] // SW-T2: removed in T3 stage 1` on `MessageType::color` (lines 157-163) and `ConnectionStatus::color` (229-237); in `src/types/mod.rs` put the same line above each of `HEADING_LARGE_SIZE`, `HEADING_MEDIUM_SIZE`, `TEXT_SMALL_SIZE`, `TOPIC_PREVIEW_TEXT_SIZE` and `SUBSCRIPTION_TEXT_SIZE` (lines 20-24; no other edit to that file). Part b tags the `theme.rs` getters and `animate_fade_in` (Step b2-4).

In `src/validation.rs` add, with tests in its existing test module:

```rust
/// The number a text field holds, or `fallback` when it does not parse.
#[allow(dead_code)] // SW-T2: used by T2 parts c and g
pub fn field_number<T: std::str::FromStr>(s: &str, fallback: T) -> T {
    s.trim().parse().unwrap_or(fallback)
}
```

```rust
    #[test]
    fn field_number_parses_or_falls_back() {
        assert_eq!(field_number(" 7447", 1u16), 7447);
        assert_eq!(field_number("abc", 7447u16), 7447);
        assert_eq!(field_number("", 10_000u64), 10_000);
        assert_eq!(field_number("99999", 7447u16), 7447, "u16 overflow falls back");
    }
```

- [ ] **Step a7: Help constants, `help_link`, message rows.** In `src/ui/help.rs` add (the Help text itself is unchanged until T2 part h):

```rust
/// Help headings, the targets of `help_link`.
#[allow(dead_code)] // SW-T2: read by the help_link calls of T2 parts c–i and the tests of part h
pub(crate) mod section {
    pub const WHAT_IT_IS: &str = "What it is";
    pub const GETTING_STARTED: &str = "Getting started";
    pub const KEY_EXPRESSIONS: &str = "Key expressions";
    pub const LIMITS: &str = "Limits";
    /// Added by T2 part h.
    pub const READING_THE_TREE: &str = "Reading the tree";
    pub const TROUBLESHOOTING: &str = "Troubleshooting";
}

#[allow(dead_code)] // SW-T2: T2 part f replaces publish.rs's local copy with it
pub(crate) const QUERYABLE_CAPTION: &str = "Answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imports)";
pub(crate) const KEY_RULE: &str = "Keys have no empty levels (no //, and no / at the start or end); * and ** fill a whole level.";

impl ZenohExplorer {
    /// A small underlined "More in Help" link that opens `section`.
    #[allow(dead_code)] // SW-T2: called by T2 parts c, d, e, f, g, i
    pub(crate) fn help_link(&mut self, ui: &mut egui::Ui, section: &'static str) -> egui::Response {
        let p = crate::style::p(ui);
        let response = ui.link(crate::style::text::small("More in Help", p).underline());
        // A link is a Tab stop whose own focus cue is only an underline in its
        // TextShape, so paint the rect ring the Tab-pass test looks for.
        if crate::style::focus::keyboard_focused(&response) {
            crate::style::focus::paint_focus_ring(ui, response.rect, crate::style::focus::RING_OUTSET);
        }
        if response.clicked() {
            self.detail_view = DetailView::Help;
            self.help_target = Some(section);
        }
        response
    }
}
```

and replace the key-rule line in `HELP_SECTIONS` ("Keys have no empty levels …") with `KEY_RULE`. Create `src/ui/message_row.rs`:

```rust
//! One-line message rows shared by All Messages and History (was CP-A3 T11):
//! time · key · payload, a type legend only when types mix.

use chrono::{DateTime, Utc};
use egui::Label;

use crate::types::*;

/// Payload bytes shown per row.
pub(crate) const ROW_PAYLOAD_BYTES: usize = 200;

#[derive(Debug, PartialEq)]
pub(crate) struct RowParts {
    pub time: String,
    pub source_time: Option<String>,
    pub key: Option<String>,
    pub payload: String,
}

pub(crate) fn row_parts(msg: &ZenohMessage, now: &DateTime<Utc>, show_key: bool) -> RowParts {
    let payload = if msg.payload.len() > ROW_PAYLOAD_BYTES {
        format!("{}...", &msg.payload[..safe_truncate_index(&msg.payload, ROW_PAYLOAD_BYTES)])
    } else {
        msg.payload.clone()
    };
    RowParts {
        time: format_local_time(&msg.timestamp, now),
        source_time: msg.source_timestamp.map(|t| format!("source {}", format_local_time(&t, now))),
        key: show_key.then(|| msg.key.clone()),
        payload: payload.replace(['\n', '\r'], " "),
    }
}

/// The row as one string, in display order (tests and hover text).
pub(crate) fn row_text(parts: &RowParts) -> String {
    let mut out = vec![parts.time.clone()];
    out.extend(parts.source_time.clone());
    out.extend(parts.key.clone());
    if !parts.payload.is_empty() {
        out.push(parts.payload.clone());
    }
    out.join(" · ")
}

/// True when the listed rows hold more than one message type.
pub(crate) fn types_mixed<'a>(mut it: impl Iterator<Item = &'a ZenohMessage>) -> bool {
    let Some(first) = it.next() else {
        return false;
    };
    it.any(|m| m.message_type != first.message_type)
}

/// One full-width line: time (legend, secondary) · key (legend) · payload
/// (content style, truncated to one line) and, when types mix, the legend.
pub(crate) fn message_row(ui: &mut egui::Ui, msg: &ZenohMessage, now: &DateTime<Utc>, show_key: bool, show_type: bool) {
    let p = crate::style::p(ui);
    let parts = row_parts(msg, now, show_key);
    ui.horizontal(|ui| {
        if show_type {
            crate::style::badge::badge(ui, &msg.message_type);
        }
        ui.label(crate::style::text::legend_text(&parts.time).color(p.text_secondary))
            .on_hover_text("Received time, local");
        if let Some(src) = &parts.source_time {
            ui.label(crate::style::text::legend_text(src).color(p.text_secondary));
        }
        if let Some(key) = &parts.key {
            ui.label(crate::style::text::legend_text(key));
        }
        if !parts.payload.is_empty() {
            ui.add(Label::new(crate::style::text::content(&parts.payload)).truncate());
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(key: &str, payload: &str, t: MessageType) -> ZenohMessage {
        ZenohMessage::new_with_bytes(key.into(), payload.into(), vec![], "text/plain".into(), chrono::Utc::now(), t, false, MessageSource::MonitorSession)
    }

    #[test]
    fn message_row_text_order() {
        let msg = m("demo/x", "21.5\nnext", MessageType::Subscribe);
        let parts = row_parts(&msg, &chrono::Utc::now(), true);
        let text = row_text(&parts);
        assert!(text.ends_with(" · demo/x · 21.5 next"), "{text}");
        assert_eq!(text.split(" · ").next().unwrap().len(), "12:00:00.000".len());
        assert_eq!(row_parts(&msg, &chrono::Utc::now(), false).key, None);
    }

    #[test]
    fn types_mixed_rules() {
        let (a, b) = (m("k", "", MessageType::Subscribe), m("k", "", MessageType::Subscribe));
        assert!(!types_mixed([&a, &b].into_iter()));
        let c = m("k", "", MessageType::Publish);
        assert!(types_mixed([&a, &c].into_iter()));
        assert!(!types_mixed(std::iter::empty()));
    }
}
```

Add `pub mod message_row;` to `src/ui/mod.rs` and put `#![allow(dead_code)] // SW-T2: used by T2 parts e and i` as the first inner attribute of `message_row.rs`.

Run: `cargo test --locked -- message_row help validation detail_view 2>&1 | grep -E '^test |test result'` — Expected: `message_row_text_order`, `types_mixed_rules`, `field_number_parses_or_falls_back`, `detail_view_labels_cover_all`, `help_names_only_real_places`, `help_claims_match_limits` ok.

- [ ] **Step a8: The headless probe.** Create `src/app/probe.rs` and declare it in `src/app/mod.rs` as `#[cfg(test)] pub(crate) mod probe;`:

```rust
//! Headless frames of the whole window for layout, focus, size and colour
//! tests (`egui::Context::run`, no window, no GPU). Reads painted text with
//! its rect and colour, rect strokes and fills, meshes, and AccessKit nodes.
#![allow(dead_code)] // SW-T2: most helpers (strokes, fills, segments, circles, mesh_count, repaint_delay, panel, tab, click, focused, text_containing, click_events, tab_event) get their callers in T2 part tests and T3 stage 2

use eframe::egui;
use egui::epaint::{ColorMode, RectShape, TextShape};
use egui::{accesskit, pos2, vec2, Color32, Context, Event, Key, Modifiers, PointerButton, Pos2, RawInput, Rect, Shape, Stroke, Vec2};
use std::time::Duration;

use crate::app::ZenohExplorer;

pub(crate) const WIDE: Vec2 = vec2(1400.0, 900.0);
pub(crate) const NARROW: Vec2 = vec2(1000.0, 600.0);

#[derive(Debug, Clone)]
pub(crate) struct Painted {
    pub text: String,
    pub rect: Rect,
    pub color: Color32,
}

#[derive(Debug, Clone)]
pub(crate) struct Node {
    pub name: String,
    pub role: accesskit::Role,
    pub rect: Rect,
}

pub(crate) struct ProbeFrame {
    pub output: egui::FullOutput,
}

fn text_color(t: &TextShape) -> Color32 {
    t.override_text_color.unwrap_or_else(|| {
        let c = t.galley.job.sections.first().map_or(t.fallback_color, |s| s.format.color);
        if c == Color32::PLACEHOLDER {
            t.fallback_color
        } else {
            c
        }
    })
}

fn walk<'a>(shape: &'a Shape, out: &mut Vec<&'a Shape>) {
    match shape {
        Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
        s => out.push(s),
    }
}

impl ProbeFrame {
    fn shapes(&self) -> Vec<&Shape> {
        let mut out = Vec::new();
        for c in &self.output.shapes {
            walk(&c.shape, &mut out);
        }
        out
    }

    pub fn texts(&self) -> Vec<Painted> {
        self.shapes()
            .into_iter()
            .filter_map(|s| match s {
                Shape::Text(t) => Some(Painted {
                    text: t.galley.text().to_string(),
                    rect: Rect::from_min_size(t.pos, t.galley.size()),
                    color: text_color(t),
                }),
                _ => None,
            })
            .collect()
    }

    pub fn text(&self, exact: &str) -> Option<Painted> {
        self.texts().into_iter().find(|t| t.text == exact)
    }

    pub fn text_containing(&self, needle: &str) -> Option<Painted> {
        self.texts().into_iter().find(|t| t.text.contains(needle))
    }

    /// Rect outlines with a visible stroke.
    pub fn strokes(&self) -> Vec<(Rect, Stroke)> {
        self.shapes()
            .into_iter()
            .filter_map(|s| match s {
                Shape::Rect(RectShape { rect, stroke, .. }) if stroke.width > 0.0 => Some((*rect, *stroke)),
                _ => None,
            })
            .collect()
    }

    /// Filled rects and their fill.
    pub fn fills(&self) -> Vec<(Rect, Color32)> {
        self.shapes()
            .into_iter()
            .filter_map(|s| match s {
                Shape::Rect(RectShape { rect, fill, .. }) if *fill != Color32::TRANSPARENT => Some((*rect, *fill)),
                _ => None,
            })
            .collect()
    }

    /// Line segments with a solid colour.
    pub fn segments(&self) -> Vec<([Pos2; 2], f32, Color32)> {
        self.shapes()
            .into_iter()
            .filter_map(|s| match s {
                Shape::LineSegment { points, stroke } => match stroke.color {
                    ColorMode::Solid(c) => Some((*points, stroke.width, c)),
                    _ => None,
                },
                _ => None,
            })
            .collect()
    }

    /// Filled circles (painted marks).
    pub fn circles(&self) -> Vec<(Pos2, f32, Color32, Stroke)> {
        self.shapes()
            .into_iter()
            .filter_map(|s| match s {
                Shape::Circle(c) => Some((c.center, c.radius, c.fill, c.stroke)),
                _ => None,
            })
            .collect()
    }

    pub fn mesh_count(&self) -> usize {
        self.shapes().into_iter().filter(|s| matches!(s, Shape::Mesh(_))).count()
    }

    /// AccessKit nodes with a name and bounds (points; pixels_per_point is 1).
    pub fn nodes(&self) -> Vec<Node> {
        let Some(update) = &self.output.platform_output.accesskit_update else {
            return Vec::new();
        };
        update
            .nodes
            .iter()
            .filter_map(|(_, n)| {
                let name = n.name()?.to_string();
                let b = n.bounds()?;
                Some(Node {
                    name,
                    role: n.role(),
                    rect: Rect::from_min_max(pos2(b.x0 as f32, b.y0 as f32), pos2(b.x1 as f32, b.y1 as f32)),
                })
            })
            .collect()
    }

    pub fn node(&self, name: &str) -> Option<Node> {
        self.nodes().into_iter().find(|n| n.name == name)
    }

    pub fn repaint_delay(&self) -> Duration {
        self.output.viewport_output[&egui::ViewportId::ROOT].repaint_delay
    }
}

pub(crate) fn tab_event() -> Event {
    Event::Key {
        key: Key::Tab,
        physical_key: None,
        pressed: true,
        repeat: false,
        modifiers: Modifiers::NONE,
    }
}

/// Press and release events at `at` (two frames: press, then release).
pub(crate) fn click_events(at: Pos2) -> (Vec<Event>, Vec<Event>) {
    let press = vec![
        Event::PointerMoved(at),
        Event::PointerButton { pos: at, button: PointerButton::Primary, pressed: true, modifiers: Modifiers::NONE },
    ];
    let release = vec![Event::PointerButton { pos: at, button: PointerButton::Primary, pressed: false, modifiers: Modifiers::NONE }];
    (press, release)
}

/// A headless window of `size` points.
pub(crate) struct Probe {
    pub ctx: Context,
    /// The window size in physical pixels.
    pub size: Vec2,
    /// UI zoom (1.5 = 150 %); the window in points is `size / zoom`.
    zoom: f32,
    time: f64,
}

impl Probe {
    pub fn new(size: Vec2) -> Self {
        let ctx = Context::default();
        ctx.enable_accesskit();
        Probe { ctx, size, zoom: 1.0, time: 0.0 }
    }

    /// Render at `zoom` from the next frame on: the same pixel window holds
    /// `size / zoom` points (egui rescales `screen_rect` only on the pass where
    /// the zoom changes, context.rs:463-474, so the probe must send it itself).
    pub fn set_zoom(&mut self, zoom: f32) {
        self.zoom = zoom;
        self.ctx.set_zoom_factor(zoom);
    }

    fn input(&mut self, events: Vec<Event>) -> RawInput {
        self.time += 1.0 / 60.0;
        RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, self.size / self.zoom)),
            time: Some(self.time),
            events,
            ..Default::default()
        }
    }

    /// One frame of the whole window.
    pub fn frame(&mut self, app: &mut ZenohExplorer, events: Vec<Event>) -> ProbeFrame {
        let input = self.input(events);
        ProbeFrame { output: self.ctx.run(input, |ctx| app.frame_ui(ctx)) }
    }

    /// Two frames: layout settles (egui sizes some widgets on the first pass).
    pub fn settle(&mut self, app: &mut ZenohExplorer) -> ProbeFrame {
        let _ = self.frame(app, vec![]);
        self.frame(app, vec![])
    }

    /// One frame of a single panel function inside a CentralPanel.
    pub fn panel(&mut self, app: &mut ZenohExplorer, events: Vec<Event>, mut f: impl FnMut(&mut ZenohExplorer, &mut egui::Ui)) -> ProbeFrame {
        let input = self.input(events);
        ProbeFrame {
            output: self.ctx.run(input, |ctx| {
                app.apply_theme(ctx);
                egui::CentralPanel::default().show(ctx, |ui| f(app, ui));
            }),
        }
    }

    /// Tab, then one frame for the focus to land; returns the second frame.
    pub fn tab(&mut self, app: &mut ZenohExplorer) -> ProbeFrame {
        let _ = self.frame(app, vec![tab_event()]);
        self.frame(app, vec![])
    }

    /// A primary click at `at`, then a settled frame.
    pub fn click(&mut self, app: &mut ZenohExplorer, at: Pos2) -> ProbeFrame {
        let (press, release) = click_events(at);
        let _ = self.frame(app, press);
        let _ = self.frame(app, release);
        self.frame(app, vec![])
    }

    pub fn focused(&self) -> Option<egui::Id> {
        self.ctx.memory(|m| m.focused())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_renders_whole_window() {
        for size in [WIDE, NARROW] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            let mut probe = Probe::new(size);
            let f = probe.settle(&mut app);
            let title = f.text("Zenoh Explorer").expect("title painted");
            assert!(Rect::from_min_size(Pos2::ZERO, size).contains_rect(title.rect));
            assert_eq!(title.color, crate::colors::LIGHT.text, "ivory is the default theme");
        }
    }

    #[test]
    fn probe_reads_accesskit_bounds() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let mut probe = Probe::new(WIDE);
        let f = probe.settle(&mut app);
        let connect = f.node("Connect").expect("the Connect button is an AccessKit node");
        assert_eq!(connect.role, accesskit::Role::Button);
        assert!(connect.rect.width() > 0.0 && connect.rect.height() > 0.0);
    }
}
```

Run: `cargo test --locked probe 2>&1 | grep -E '^test |test result'` — Expected: `probe_renders_whole_window` and `probe_reads_accesskit_bounds` ok. If egui 0.29 names the Connect node differently (for example via `label` instead of `name`), use the getter that holds the widget's text and note it in the commit message; T2 tests depend on `node(name)` finding buttons by their text.

- [ ] **Step a9: Part check.** `cargo fmt --all -- --check`; `cargo clippy --all-targets --locked -- -D warnings` exits 0; `cargo test --locked 2>&1 | grep -E '^test result'` shows every line ok (every test of the base plus the new ones); `git diff --name-only "$BASE2" HEAD` lists only part a's files. Commit after each of a1…a8 (`refactor(sw-a<n>): …`).

---

## Task T2: Every visible change

Owns, per part: see the task table. Worktrees, batches, merges and integration are under "How T2 runs". Every part: tests first (red), then code, then its checks, one commit per step; each commit message names the part and the review rows ("(was CP-A2 T1)"). Common rules for all parts:
- Colours come from `let p = crate::style::p(ui);` (or `crate::colors::palette(self.dark_mode)` outside a `Ui`). No new `ExplorerColors::` use; replace the ones in your files.
- Sizes come from text styles (`crate::style::text::*`): `TEXT_SMALL_SIZE` → `small(..)` or `TextStyle::Small`; `HEADING_MEDIUM_SIZE` empty-state headings → body text; `.italics()` is dropped (F-T6-6); section labels use `text::label(..)` (F-T6-4); key expressions, locators, ids, sizes and times use `text::legend_text(..)` (F-T6-8); payload bodies use `text::content(..)` (F-T6-7).
- `ui.group(..)` modules become `crate::style::glass::face(ui, ..)`; controls become `crate::style::keys::key(..)` with the tier and role named in the step.
- A motion source registers every frame with `self.motion.surface(key, rect, &painter, slot, radius)` and starts its action with `self.motion.begin(kind, Instant::now())` on click; a receiver registers its face.

### Part a (was CP-A2 T1, T2, T3 selector; CP-A3 T2, T3 key, T6 popover; CP-B T3 header inks, T15; CP-A1 T3 theme control; CP-C conn.toggle, header.status and T15 toggle): the header

**Files:**
- Modify: `src/app/header.rs`, `src/app/theme.rs` (delete `animate_pulse`)

**Interfaces:**
- Consumes: `ConnectionUI::start_connect`, `ZenohExplorer::ports_ok`, `LimitsUI::show_limits_controls`, `crate::style::{keys, glass, badge, text, focus}`, `crate::motion::{Motion, SurfaceKey, ActionKind}` including `Motion::{reduced, set_reduced}`, fields `limits_open`, `memory_alert`.
- Produces: `ZenohExplorer::show_header` (signature unchanged); private helpers `status_mark`, `health_text`, `notice_text`, `connection_key`, `pauses_text`, `KeyAction`, `StatusMark`, the `SLOT_*` constants and `HEADER_GLASS_ID` (egui temp-data key under which `show_header` records its statusGlass rect for the probe tests) in `header.rs`.

The header becomes two constant rows of **fixed-width slots that fit a 720 pt window** (user decision: Snow White's fixed slots win; nothing wraps into another row). At 720 pt the header panel's 8 pt margins leave 704 pt. Row 1 on the chassis: title on the left (the 22 pt heading, about 155 pt); on the right, from the edge inward, the theme selector (slot 100 pt: "☀ Light" and "🌙 Dark" measure about 41 and 39 pt at 14 pt with egui 0.29.1's default fonts, a frameless `latched_label` adds no padding, plus 8 pt spacing and the 2 pt ring outset), the "Reduce motion" toggle (120 pt, user decision: directly beside the ☀ Light / 🌙 Dark selector, off at every launch, not persisted until P3 Settings) and the worker slot (170 pt, empty while healthy): 160 + 170 + 120 + 100 + 3 × 8 = 574 pt. Row 2 is one statusGlass module across the width (10 pt frame margins leave 684 pt) with five fixed slots from the left: connection key (170 pt), status (150 pt), peers (70 pt), memory (140 pt) and notices (100 pt): 630 + 4 × 8 = 662 pt. At 1000 pt and wider the slots keep their widths and the rest of each row stays empty, so no landmark moves with the window width either. Every readout is truncated inside its slot (`Label::truncate`, an ellipsis) with its full text in a hover tooltip; P1's words are placed, not reworded, except three shortened labels: the connection key says "Disconnect (pauses 3)" (hover: "Disconnect (pauses 3 subscriptions)"), and the notice slot shows the memory warning up to its colon and the drop counters as "n rows dropped" (hover: P1's full sentences). **The drop counters and the memory warning stay in the header** (they do not move to the popover, which now holds only the limits); they share the one notice slot because six separate slots would leave each readout under 70 pt at 720 pt, and both say that rows are being limited or lost. The memory slot no longer appends " · rows dropped", since the notice slot says it in words.

- [ ] **Step a-1: Failing tests.** Append to `src/app/header.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::probe::{Probe, NARROW, WIDE};

    /// P3 T7's minimum window (720×480): the fixed slots must fit it.
    const AT_720: egui::Vec2 = egui::vec2(720.0, 480.0);

    #[test]
    fn status_mark_rules() {
        use ConnectionStatus::*;
        assert_eq!(status_mark(&Disconnected, true), StatusMark::Outline);
        assert_eq!(status_mark(&ConnectingPublishing, true), StatusMark::Spinner);
        assert_eq!(status_mark(&ConnectingMonitor, true), StatusMark::Spinner);
        assert_eq!(status_mark(&Connected, true), StatusMark::Filled);
        assert_eq!(status_mark(&Connected, false), StatusMark::Ring, "monitor off");
        assert_eq!(status_mark(&Error("x".into()), true), StatusMark::Filled);
    }

    #[test]
    fn connection_key_rules() {
        use ConnectionStatus::*;
        assert_eq!(connection_key(&Disconnected, 0, true), ("Connect".to_string(), KeyAction::Connect, true));
        assert_eq!(connection_key(&Error("x".into()), 2, false), ("Connect".to_string(), KeyAction::Connect, false));
        assert_eq!(connection_key(&ConnectingMonitor, 3, true), ("Connecting…".to_string(), KeyAction::Wait, true));
        assert_eq!(connection_key(&Connected, 0, true).0, "Disconnect");
        assert_eq!(connection_key(&Connected, 1, true).0, "Disconnect (pauses 1)");
        assert_eq!(connection_key(&Connected, 3, true), ("Disconnect (pauses 3)".to_string(), KeyAction::Disconnect, true));
        // the full words are the key's hover (header width decision: shortened label)
        assert_eq!(pauses_text(0), None);
        assert_eq!(pauses_text(1).as_deref(), Some("Disconnect (pauses 1 subscription)"));
        assert_eq!(pauses_text(3).as_deref(), Some("Disconnect (pauses 3 subscriptions)"));
        for n in [0, 1, 12, 999] {
            assert!(connection_key(&Connected, n, true).0.chars().count() <= 23, "fits the 170 pt key slot");
        }
    }

    #[test]
    fn health_word_is_static() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        assert_eq!(app.health_text(), None);
        app.worker_healthy = false;
        app.worker_gone = true;
        assert!(app.health_text().unwrap().starts_with("Worker not answering ("));
        app.worker_gone = false;
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        assert_eq!(app.health_text().as_deref(), Some("Worker busy: connecting"));
        // two frames half a second apart paint the same ink
        let mut probe = Probe::new(WIDE);
        let a = probe.settle(&mut app).text("Worker busy: connecting").unwrap().color;
        std::thread::sleep(std::time::Duration::from_millis(500));
        let b = probe.frame(&mut app, vec![]).text("Worker busy: connecting").unwrap().color;
        assert_eq!(a, b, "no pulse (F-T20-8)");
    }

    /// The memory warning and the drop counters share the notice slot:
    /// shortened in the slot, P1's full sentences in the hover.
    #[test]
    fn notice_text_rules() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        assert_eq!(app.notice_text(), None);
        app.rate_limit_drops = 12;
        let (short, full) = app.notice_text().unwrap();
        assert_eq!(short, "12 rows dropped");
        assert_eq!(full, "(0 trimmed from list, 12 not listed (rate), 0 pipeline)", "P1's words in the hover");
        app.current_memory_bytes = 85 * 1024 * 1024;
        app.update_memory_alert();
        let (short, full) = app.notice_text().unwrap();
        assert_eq!(short, "History is 85% of its limit · 12 rows dropped");
        assert!(full.starts_with("History is 85% of its limit: the oldest rows will leave the list"), "{full}");
        assert!(full.ends_with("0 pipeline)"), "{full}");
    }

    /// The slot widths add up to less than a 720 pt window: 8 pt panel
    /// margins, 10 pt glass margins, 8 pt item spacing, a 22 pt title.
    #[test]
    fn slot_budget_fits_720() {
        let gap = 8.0;
        let row1 = 160.0 + SLOT_WORKER + SLOT_MOTION + SLOT_THEME + 3.0 * gap;
        assert!(row1 <= 720.0 - 2.0 * 8.0, "row 1 needs {row1} pt");
        let row2 = SLOT_KEY + SLOT_STATUS + SLOT_PEERS + SLOT_MEMORY + SLOT_NOTICE + 4.0 * gap;
        assert!(row2 <= 720.0 - 2.0 * 8.0 - 2.0 * 10.0, "row 2 needs {row2} pt");
    }

    /// Slot rects stay put across status, peers, memory, notice and worker
    /// changes, at 1400, 1000 and 720 pt.
    #[test]
    fn header_slots_do_not_move() {
        for size in [WIDE, NARROW, AT_720] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            let mut probe = Probe::new(size);
            let mut seen: Vec<(String, egui::Rect)> = Vec::new();
            for apply in &long_states() {
                apply(&mut app);
                let f = probe.settle(&mut app);
                // the header key is the topmost Connect/Disconnect node (the Connection view's form has none after part c)
                let key = f
                    .nodes()
                    .into_iter()
                    .filter(|n| n.name.starts_with("Connect") || n.name.starts_with("Disconnect"))
                    .min_by(|a, b| a.rect.min.y.total_cmp(&b.rect.min.y))
                    .expect("connection key");
                let title = f.text("Zenoh Explorer").expect("title").rect;
                let light = f.text("☀ Light").expect("theme selector").rect;
                let motion = f.node("Reduce motion").expect("reduced-motion toggle").rect;
                // a truncated galley keeps its full text (epaint `Galley::text`), so the full words find it
                let status = f.text_containing(&app.header_status_text()).expect("status word").rect;
                for (name, r) in [("key", key.rect), ("title", title), ("light", light), ("motion", motion), ("status", status)] {
                    match seen.iter().find(|(n, _)| n == name) {
                        Some((_, first)) => assert!((first.min - r.min).length() < 0.5, "{name} moved at {size:?}: {first:?} → {r:?}"),
                        None => seen.push((name.to_string(), r)),
                    }
                    assert!(egui::Rect::from_min_size(egui::Pos2::ZERO, size).contains_rect(r), "{name} clipped at {size:?}");
                }
            }
        }
    }

    /// The longest header states P1 can produce.
    fn long_states() -> Vec<Box<dyn Fn(&mut ZenohExplorer)>> {
        vec![
            Box::new(|a| a.connection_status = ConnectionStatus::Disconnected),
            Box::new(|a| {
                a.connection_status = ConnectionStatus::ConnectingPublishing;
                a.connect_target = "tcp/10.0.0.5:7447,tcp/[fe80::1]:7448,udp/192.168.100.200:7449".into();
                a.connect_started = Some(Instant::now());
            }),
            Box::new(|a| {
                a.connection_status = ConnectionStatus::Connected;
                a.monitor_ok = false;
                a.discovered_peers = 12;
                a.discovered_routers = 3;
                for i in 0..12 {
                    a.subscriptions.push(Subscription { id: i.to_string(), key_expr: format!("demo/{i}/**"), reliability: String::new(), mode: String::new() });
                }
            }),
            Box::new(|a| {
                a.current_memory_bytes = 97 * 1024 * 1024;
                a.import_memory_bytes = 300 * 1024 * 1024;
                a.update_memory_alert();
            }),
            Box::new(|a| {
                a.rate_limit_drops = 123_456;
                a.messages_dropped = 7_890;
            }),
            Box::new(|a| {
                a.worker_healthy = false;
                a.worker_gone = true;
            }),
            Box::new(|a| a.connection_status = ConnectionStatus::Error("Could not connect in client mode: Unable to connect to any of [tcp/10.0.0.5:7447]".into())),
        ]
    }

    /// User decision (header width): at 720 pt and at 1000 pt, in the longest
    /// states, nothing in the header clips at the window edge, overflows its
    /// slot into a neighbour, or wraps into another row. The header band is
    /// measured on its own: from the window top to the bottom of the statusGlass
    /// that `show_header` records (`HEADER_GLASS_ID`), so the "Active:" list,
    /// the tree filter row, the detail tabs and (in this part's worktree) P1's
    /// top connection form, all drawn below it, are neither checked nor able
    /// to move the reference. The band's bottom must be the same in every state.
    #[test]
    fn header_fits_at_720_and_1000() {
        for size in [AT_720, NARROW] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            let mut probe = Probe::new(size);
            let screen = egui::Rect::from_min_size(egui::Pos2::ZERO, size);
            let mut header_bottom: Option<f32> = None;
            for apply in &long_states() {
                apply(&mut app);
                let f = probe.settle(&mut app);
                let glass = probe
                    .ctx
                    .data(|d| d.get_temp::<egui::Rect>(egui::Id::new(HEADER_GLASS_ID)))
                    .expect("show_header records its statusGlass rect");
                assert!(screen.contains_rect(glass), "the statusGlass fits the window at {size:?}: {glass:?}");
                // the band's bottom: the glass's outer edge (its 10 × 4 pt margins are inside `glass`)
                let below = glass.max.y;
                match header_bottom {
                    Some(b) => assert!((b - below).abs() < 0.5, "the header changed height at {size:?}: {b} → {below}"),
                    None => header_bottom = Some(below),
                }
                let texts: Vec<_> = f.texts().into_iter().filter(|t| t.rect.max.y <= below).collect();
                assert!(!texts.is_empty());
                for t in &texts {
                    assert!(screen.contains_rect(t.rect), "{:?} runs past the window at {size:?}: {:?}", t.text, t.rect);
                }
                for (i, a) in texts.iter().enumerate() {
                    for b in texts.iter().skip(i + 1) {
                        assert!(!a.rect.shrink(0.5).intersects(b.rect.shrink(0.5)), "{:?} overlaps {:?} at {size:?}", a.text, b.text);
                    }
                }
                let controls: Vec<_> = f
                    .nodes()
                    .into_iter()
                    .filter(|n| n.rect.max.y <= below)
                    .filter(|n| ["Connect", "Disconnect", "☀ Light", "🌙 Dark", "Reduce motion"].iter().any(|w| n.name.starts_with(w)))
                    .collect();
                assert!(controls.len() >= 4, "key, both theme values and the motion toggle at {size:?}");
                for n in &controls {
                    assert!(screen.contains_rect(n.rect), "{} clipped at {size:?}", n.name);
                }
                // P1's full words are painted, truncated to the slot (the galley keeps the full
                // text; its painted size is the elided one); the hover carries the full words
                let status = f.text(&app.header_status_text()).expect("status readout");
                assert!(status.rect.width() <= SLOT_STATUS, "truncated to its slot: {:?}", status.rect);
                if app.notice_text().is_some() {
                    let notice = texts.iter().rfind(|t| t.text.contains("rows dropped") || t.text.starts_with("History is"));
                    assert!(notice.is_some_and(|t| t.rect.width() <= SLOT_NOTICE), "notice truncated to its slot at {size:?}");
                }
            }
        }
    }

    #[test]
    fn header_has_no_tofu_dot() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let f = Probe::new(WIDE).settle(&mut app);
        assert!(!f.texts().iter().any(|t| t.text.starts_with('●')), "the status mark is painted (F-T7-2)");
        assert!(f.text("Disconnected").is_some());
        assert!(!f.circles().is_empty(), "a painted outline mark");
    }

    #[test]
    fn connecting_key_sends_nothing() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        let started = Instant::now();
        app.connect_started = Some(started);
        let mut probe = Probe::new(WIDE);
        let key = probe.settle(&mut app).node("Connecting…").expect("key").rect;
        let _ = probe.click(&mut app, key.center());
        assert_eq!(app.connect_started, Some(started), "a click while connecting is a no-op");
        assert!(matches!(app.connection_status, ConnectionStatus::ConnectingPublishing));
    }

    /// User decision (Q4): the toggle sits in the header directly beside the
    /// theme selector, is a 24 pt target, starts off (not persisted) and
    /// switches `Motion` both ways.
    #[test]
    fn reduce_motion_toggle_in_header() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        assert!(!app.motion.reduced(), "off at launch");
        let mut probe = Probe::new(WIDE);
        let f = probe.settle(&mut app);
        let toggle = f.node("Reduce motion").expect("header toggle").rect;
        let light = f.node("☀ Light").expect("theme selector").rect;
        assert!((toggle.center().y - light.center().y).abs() < 1.0, "same row as the theme selector");
        assert!(toggle.max.x <= light.min.x && light.min.x - toggle.max.x < 48.0, "directly left of it: {toggle:?} {light:?}");
        assert!(toggle.width() >= 24.0 && toggle.height() >= 24.0);
        let _ = probe.click(&mut app, toggle.center());
        assert!(app.motion.reduced());
        let toggle = probe.settle(&mut app).node("Reduce motion").unwrap().rect;
        let _ = probe.click(&mut app, toggle.center());
        assert!(!app.motion.reduced());
    }
}
```

Run: `cargo test --locked app::header 2>&1 | tail -n 3` — Expected: compile errors for `status_mark`, `connection_key`, `pauses_text`, `health_text`, `notice_text` and the `SLOT_*` constants (red).

- [ ] **Step a-2: Pure helpers (was CP-A2 T1, T2; CP-A3 T3).** In `src/app/header.rs` above `impl ZenohExplorer`:

```rust
/// The painted mark beside the status word (F-T7-2): a shape per state,
/// so the state never rests on hue alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum StatusMark {
    Outline,
    Filled,
    /// Connected with the monitor off: a ring around a dot.
    Ring,
    Spinner,
}

pub(crate) fn status_mark(s: &ConnectionStatus, monitor_ok: bool) -> StatusMark {
    match s {
        ConnectionStatus::Disconnected => StatusMark::Outline,
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => StatusMark::Spinner,
        ConnectionStatus::Connected if !monitor_ok => StatusMark::Ring,
        ConnectionStatus::Connected | ConnectionStatus::Error(_) => StatusMark::Filled,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum KeyAction {
    Connect,
    Disconnect,
    /// Connecting: enabled so Tab does not stop, but a click does nothing.
    Wait,
}

/// The one stateful connection key (note 7): label, action and enabled.
/// The label is short enough for the 170 pt slot; `pauses_text` is its hover.
pub(crate) fn connection_key(s: &ConnectionStatus, subscriptions: usize, ports_ok: bool) -> (String, KeyAction, bool) {
    match s {
        ConnectionStatus::Disconnected | ConnectionStatus::Error(_) => ("Connect".to_string(), KeyAction::Connect, ports_ok),
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
            ("Connecting…".to_string(), KeyAction::Wait, true)
        }
        ConnectionStatus::Connected => {
            let label = match subscriptions {
                0 => "Disconnect".to_string(),
                n => format!("Disconnect (pauses {n})"),
            };
            (label, KeyAction::Disconnect, true)
        }
    }
}

/// The Disconnect key's hover: the pause count in full words.
pub(crate) fn pauses_text(subscriptions: usize) -> Option<String> {
    match subscriptions {
        0 => None,
        1 => Some("Disconnect (pauses 1 subscription)".to_string()),
        n => Some(format!("Disconnect (pauses {n} subscriptions)")),
    }
}

impl ZenohExplorer {
    /// P1 T21's worker words, static (the pulse is gone, F-T20-8).
    pub(crate) fn health_text(&self) -> Option<String> {
        if self.worker_healthy {
            return None;
        }
        let not_answering = format!("Worker not answering ({} s)", self.ping_sent_at.map_or(0, |t| t.elapsed().as_secs()));
        Some(if self.worker_gone {
            not_answering
        } else if matches!(self.publish_status, Some(PublishStatus::Sending { .. })) {
            "Worker busy: publishing".to_string()
        } else if matches!(self.connection_status, ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor) {
            "Worker busy: connecting".to_string()
        } else {
            not_answering
        })
    }

    /// The notice slot: (shortened text, P1's full sentences for the hover).
    /// The memory warning comes first, up to its colon; the drop counters
    /// become "n rows dropped".
    pub(crate) fn notice_text(&self) -> Option<(String, String)> {
        let mut short = Vec::new();
        let mut full = Vec::new();
        if let Some(alert) = &self.memory_alert {
            short.push(alert.split(':').next().unwrap_or(alert).to_string());
            full.push(alert.clone());
        }
        let sample_drops = self.sample_drops.load(Ordering::Relaxed);
        let drops = self.messages_dropped + self.rate_limit_drops + sample_drops;
        if drops > 0 {
            short.push(format!("{drops} rows dropped"));
            full.push(format!(
                "({} trimmed from list, {} not listed (rate), {} pipeline)",
                self.messages_dropped, self.rate_limit_drops, sample_drops
            ));
        }
        (!short.is_empty()).then(|| (short.join(" · "), full.join("\n")))
    }
}
```

- [ ] **Step a-3: The two rows (was CP-A3 T2, T3, T6; CP-A2 T1, T3; CP-B T15; CP-C T15 toggle).** Replace the body of `show_header` with:

```rust
    pub(crate) fn show_header(&mut self, ui: &mut egui::Ui) {
        let p = crate::style::p(ui);
        let now = Instant::now();
        // Row 1: title | … | worker slot | Reduce motion | theme selector
        ui.allocate_ui_with_layout(
            egui::vec2(ui.available_width(), ROW1_H),
            egui::Layout::left_to_right(egui::Align::Center),
            |ui| {
                ui.label(crate::style::text::heading("Zenoh Explorer").color(p.text));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    slot(ui, SLOT_THEME, |ui| {
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            let dark = self.dark_mode;
                            let t = |s: &str, sel: bool| RichText::new(s).color(latched_text_color(p, sel));
                            if latched_label(ui, dark, t("🌙 Dark", dark), true).clicked() {
                                self.dark_mode = true;
                            }
                            if latched_label(ui, !dark, t("☀ Light", !dark), true).clicked() {
                                self.dark_mode = false;
                            }
                        });
                    });
                    // Reduced motion (user decision): beside the theme selector, a
                    // latched toggle (fill and rim when on), not persisted.
                    slot(ui, SLOT_MOTION, |ui| {
                        ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                            let on = self.motion.reduced();
                            let text = RichText::new("Reduce motion").color(latched_text_color(p, on));
                            let r = latched_label(ui, on, text, true).on_hover_text(if on {
                                "Motion effects are off: results appear at once. Click to turn them on."
                            } else {
                                "Turn motion effects off: results appear at once."
                            });
                            if r.clicked() {
                                self.motion.set_reduced(!on, now);
                            }
                        });
                    });
                    slot(ui, SLOT_WORKER, |ui| {
                        if let Some(text) = self.health_text() {
                            ui.add(egui::Label::new(RichText::new(&text).color(p.err)).truncate())
                                .on_hover_text(text);
                        }
                    });
                });
            },
        );
        // Row 2: one statusGlass module with fixed slots
        let glass = crate::style::glass::status_glass(ui, |ui| {
            let link_slot = ui.painter().add(egui::Shape::Noop);
            ui.set_min_width(ui.available_width());
            ui.horizontal(|ui| {
                // connection key
                slot(ui, SLOT_KEY, |ui| {
                    let (label, action, enabled) =
                        connection_key(&self.connection_status, self.subscriptions.len(), self.ports_ok());
                    let owned = self.motion.owns(&SurfaceKey::conn_toggle());
                    let role = if action == KeyAction::Connect { KeyRole::Primary } else { KeyRole::Neutral };
                    let k = key(ui, label, KeyTier::Hero, role, enabled, owned);
                    self.motion.surface(SurfaceKey::conn_toggle(), k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
                    if action == KeyAction::Disconnect {
                        if let Some(words) = pauses_text(self.subscriptions.len()) {
                            let _ = k.response.clone().on_hover_text(words);
                        }
                    }
                    if k.response.clicked() {
                        match action {
                            KeyAction::Connect => {
                                self.motion.begin(ActionKind::Connect, now);
                                self.start_connect(ui.ctx());
                            }
                            KeyAction::Disconnect => {
                                self.motion.begin(ActionKind::Disconnect, now);
                                self.connection_status = ConnectionStatus::Disconnected;
                                if let Some(sender) = &self.command_sender {
                                    let _ = sender.send(ZenohCommand::Disconnect);
                                }
                            }
                            KeyAction::Wait => {}
                        }
                    }
                });
                // status: painted mark + P1's words, truncated to the slot
                let status_slot = ui.painter().add(egui::Shape::Noop);
                let status = slot(ui, SLOT_STATUS, |ui| {
                    let ink = crate::style::badge::status_ink(&self.connection_status, self.monitor_ok, p);
                    paint_mark(ui, status_mark(&self.connection_status, self.monitor_ok), ink);
                    let text = self.header_status_text();
                    let r = ui.add(egui::Label::new(RichText::new(&text).color(ink)).truncate()).on_hover_text(&text);
                    if matches!(self.connection_status, ConnectionStatus::Connected) && !self.monitor_ok {
                        r.on_hover_text("The background ** monitor could not start, so only your subscriptions fill the tree");
                    }
                });
                self.motion.surface(SurfaceKey::header_status(), status, ui.painter(), status_slot, 6.0);
                // peers (shown while connected, slot always reserved)
                slot(ui, SLOT_PEERS, |ui| {
                    if matches!(self.connection_status, ConnectionStatus::Connected) {
                        let text = peers_text(self.discovered_peers, self.discovered_routers);
                        ui.add(egui::Label::new(crate::style::text::legend_text(&text).color(p.status_text)).truncate())
                            .on_hover_text(format!("{text}: Zenoh peers and routers the publishing session is linked to"));
                    }
                });
                // memory readout; a click opens the limits popover (CP-A3 T6)
                slot(ui, SLOT_MEMORY, |ui| self.memory_slot(ui, p));
                // notices: the memory warning and the drop counters (header width decision)
                slot(ui, SLOT_NOTICE, |ui| {
                    if let Some((short, full)) = self.notice_text() {
                        ui.add(egui::Label::new(crate::style::text::small(short, p).color(p.status_warn)).truncate())
                            .on_hover_text(full);
                    }
                });
            });
            (link_slot, ui.painter().clone())
        });
        let (link_slot, painter) = glass.inner;
        self.motion.link_slot(&painter, link_slot, glass.rect);
        // The header band ends at the statusGlass: recorded for the probe tests
        // (egui temp data, so no new `ZenohExplorer` field outside this part's files).
        ui.ctx().data_mut(|d| d.insert_temp(egui::Id::new(HEADER_GLASS_ID), glass.rect));
    }

    fn memory_slot(&mut self, ui: &mut egui::Ui, p: &crate::colors::Palette) {
        if self.stored_bytes_cache.0.elapsed() >= Duration::from_secs(1) {
            let stored = self
                .payload_store
                .read()
                .map(|m| m.values().map(|e| e.bytes.len()).sum())
                .unwrap_or(self.stored_bytes_cache.1);
            self.stored_bytes_cache = (Instant::now(), stored);
        }
        self.update_memory_alert();
        let (mut text, level) = memory_readout(self.current_memory_bytes, self.max_memory_mb, self.stored_bytes_cache.1);
        if self.import_memory_bytes > 0 {
            text.push_str(&format!(" · Staged import {}", transfer::format_size(self.import_memory_bytes)));
        }
        let ink = match level {
            MemLevel::Critical => p.status_err,
            MemLevel::High => p.status_warn,
            MemLevel::Ok => p.status_text,
        };
        let r = ui
            .add(egui::Label::new(crate::style::text::legend_text(&text).color(ink)).truncate().sense(egui::Sense::click()))
            .on_hover_text(format!("{text}\nClick for the session limits"));
        // Sense::click() is focusable (egui sense.rs:65-71; Label keeps it), so the
        // readout is a Tab stop and needs the ring like every other stop (Review Focus).
        if crate::style::focus::keyboard_focused(&r) {
            crate::style::focus::paint_focus_ring(ui, r.rect, crate::style::focus::RING_OUTSET);
        }
        let popup = egui::Id::new("limits_popover");
        if r.clicked() {
            ui.memory_mut(|m| m.toggle_popup(popup));
        }
        egui::popup_below_widget(ui, popup, &r, egui::PopupCloseBehavior::CloseOnClickOutside, |ui| {
            ui.set_min_width(420.0);
            self.show_limits_controls(ui);
        });
        self.limits_open = ui.memory(|m| m.is_popup_open(popup));
    }
```

and, after the `impl`, the layout helpers:

```rust
const ROW1_H: f32 = 40.0;
/// Temp-data key of the header's statusGlass rect (read by the probe tests).
pub(crate) const HEADER_GLASS_ID: &str = "header_status_glass";
// Row 1 (chassis), from the right edge inward.
const SLOT_THEME: f32 = 100.0;
const SLOT_MOTION: f32 = 120.0;
const SLOT_WORKER: f32 = 170.0;
// Row 2 (statusGlass), from the left: 630 pt plus 4 gaps fits the 684 pt inside the glass at 720 pt.
const SLOT_KEY: f32 = 170.0;
const SLOT_STATUS: f32 = 150.0;
const SLOT_PEERS: f32 = 70.0;
const SLOT_MEMORY: f32 = 140.0;
const SLOT_NOTICE: f32 = 100.0;
const ROW2_H: f32 = 40.0;

/// A fixed-width slot, allocated whether or not it has content (F-T3-4).
/// Content wider than the slot is truncated by its `Label`, never wrapped.
fn slot(ui: &mut egui::Ui, width: f32, add: impl FnOnce(&mut egui::Ui)) -> egui::Rect {
    ui.allocate_ui_with_layout(egui::vec2(width, ROW2_H), egui::Layout::left_to_right(egui::Align::Center), |ui| {
        ui.set_min_size(egui::vec2(width, ROW2_H));
        ui.set_max_width(width);
        add(ui);
    })
    .response
    .rect
}

/// The status mark, painted (F-T4-3, F-T6-1): outline, filled, ring or spinner.
fn paint_mark(ui: &mut egui::Ui, mark: StatusMark, ink: egui::Color32) {
    if mark == StatusMark::Spinner {
        ui.add(egui::Spinner::new().size(12.0).color(ink));
        return;
    }
    let (rect, _) = ui.allocate_exact_size(egui::vec2(12.0, 12.0), egui::Sense::hover());
    let c = rect.center();
    let painter = ui.painter();
    match mark {
        StatusMark::Outline => {
            painter.circle_stroke(c, 5.0, egui::Stroke::new(1.5, ink));
        }
        StatusMark::Filled => {
            painter.circle_filled(c, 5.0, ink);
        }
        StatusMark::Ring => {
            painter.circle_stroke(c, 5.0, egui::Stroke::new(1.5, ink));
            painter.circle_filled(c, 2.5, ink);
        }
        StatusMark::Spinner => {}
    }
}
```

Update the imports of `header.rs` to: `use eframe::egui; use egui::RichText; use std::sync::atomic::Ordering; use std::time::{Duration, Instant}; use crate::app::{memory_readout, peers_text, MemLevel, ZenohExplorer}; use crate::motion::{ActionKind, SurfaceKey}; use crate::style::keys::{key, latched_label, latched_text_color, KeyRole, KeyTier, KEY_RADIUS}; use crate::transfer; use crate::types::*; use crate::ui::connection::ConnectionUI; use crate::ui::limits::LimitsUI;` (drop `ExplorerColors`).

- [ ] **Step a-4: Delete the pulse (was CP-A2 T2).** Delete `animate_pulse` from `src/app/theme.rs` (lines 127-131 of `3ce8c01`). The old header code with `from_millis(66)` is gone with step a-3.

Run: `cargo test --locked app::header 2>&1 | grep -E '^test |test result'; grep -rn 'animate_pulse' src; grep -rn 'from_millis(66)' src`
Expected: `status_mark_rules`, `connection_key_rules`, `health_word_is_static`, `notice_text_rules`, `slot_budget_fits_720`, `header_slots_do_not_move`, `header_fits_at_720_and_1000`, `header_has_no_tofu_dot`, `connecting_key_sends_nothing`, `reduce_motion_toggle_in_header` ok; both greps print nothing. If `header_fits_at_720_and_1000` fails because a text is wider than its slot, the fix is a narrower readout (`truncate`, or a shorter label with the full words on hover), never a wider slot or a second row; if a slot budget really cannot hold its control at 720 pt, stop and report.

- [ ] **Step a-5: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/app/header.rs` and `src/app/theme.rs`.

### Part b (was CP-A3 T1, T4 place, T5; CP-A2 T3 tab words, T4 Dismiss; CP-A1 T1 tabs; CP-B T13 tab bank, T16; CP-C T5, T8 slots, tab and tree-panel surfaces): the window skeleton

**Files:**
- Modify: `src/app/layout.rs`

**Interfaces:**
- Consumes: `show_header`, `show_tree_panel`, `show_detail_panel`, `crate::style::{glass, keys, focus, text}`, `crate::motion`, fields `ui_alert_since`, `connection_view_shown`, `DetailView::{ALL, label}`.
- Produces: `ZenohExplorer::frame_ui` (signature unchanged); `pub(crate) fn show_view_tabs(&mut self, ui: &mut egui::Ui)`; `pub(crate) fn alert_expired(alert: &UiAlert, age: Duration) -> bool`; constants `GUTTER`, `STRIP_H`.

Three constant bands (CP-B T16): the header band (part a's two rows), the workspace (tree face | 16 pt chassis gutter | detail column), and a fixed-height status strip at the bottom (CP-A3 T1). The detail column holds the tab bank, a 16 pt chassis strip (CP-C T5, F-T12-2), and the detail face. The Disconnect row and the top connection form are gone: the form is the Connection view, selected at launch while disconnected and on each new connection error (Q9 (a)).

- [ ] **Step b-1: Failing tests.** Append to `src/app/layout.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::probe::{Probe, NARROW, WIDE};

    #[test]
    fn alert_expiry_rules() {
        let s = |n| Duration::from_secs(n);
        assert!(!alert_expired(&UiAlert::Success("x".into()), s(5)));
        assert!(alert_expired(&UiAlert::Success("x".into()), s(6)));
        assert!(!alert_expired(&UiAlert::Warning("x".into()), s(9)));
        assert!(alert_expired(&UiAlert::Warning("x".into()), s(10)));
        assert!(!alert_expired(&UiAlert::Error("x".into()), s(3600)), "errors stay until Dismiss");
    }

    fn landmarks(f: &crate::app::probe::ProbeFrame) -> Vec<egui::Rect> {
        let mut out: Vec<egui::Rect> = DetailView::ALL
            .iter()
            .map(|v| f.node(v.label()).unwrap_or_else(|| panic!("tab {}", v.label())).rect)
            .collect();
        out.push(f.text("Topics").expect("tree heading").rect);
        out
    }

    #[test]
    fn strip_keeps_workspace_still() {
        for size in [WIDE, NARROW] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            app.detail_view = DetailView::TopicDetails;
            app.connection_view_shown = true;
            let mut probe = Probe::new(size);
            let before = landmarks(&probe.settle(&mut app));
            app.ui_alert = Some(UiAlert::Error("x".into()));
            let f = probe.settle(&mut app);
            assert_eq!(before, landmarks(&f), "an alert moves nothing at {size:?}");
            let strip = f.text("Error: x").expect("strip text");
            let tree_heading = f.text("Topics").unwrap().rect;
            assert!(strip.rect.min.y > tree_heading.max.y, "the strip sits under the workspace");
            let dismiss = f.node("Dismiss").expect("Dismiss key");
            let _ = probe.click(&mut app, dismiss.rect.center());
            assert!(app.ui_alert.is_none());
        }
    }

    #[test]
    fn tabs_sit_over_detail_panel() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let f = Probe::new(WIDE).settle(&mut app);
        let tree_right = f.text("Topics").unwrap().rect.max.x;
        for v in DetailView::ALL {
            let r = f.node(v.label()).unwrap().rect;
            assert!(r.min.x > tree_right, "{} is over the detail column", v.label());
            assert!(r.height() >= crate::style::keys::KeyTier::Compact.height() - 0.5);
        }
        assert!(f.text("View:").is_some(), "the row is labelled View (F-T4-4)");
        assert!(f.text("Quick Actions:").is_none());
    }

    #[test]
    fn tab_focus_shows_ring() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let mut probe = Probe::new(WIDE);
        let publish = probe.settle(&mut app).node(DetailView::Publish.label()).unwrap().rect;
        let ring = crate::colors::LIGHT.focus;
        for _ in 0..60 {
            let f = probe.tab(&mut app);
            let Some(id) = probe.focused() else { continue };
            let Some(r) = probe.ctx.read_response(id) else { continue };
            if (r.rect.center() - publish.center()).length() < 1.0 {
                assert!(
                    f.strokes().iter().any(|(rect, s)| s.color == ring && rect.contains_rect(r.rect)),
                    "a focus ring around the focused tab"
                );
                assert!(!f.fills().iter().any(|(rect, c)| *c == crate::colors::LIGHT.selected && rect.contains_rect(r.rect)),
                    "focus is not drawn as the latch (F-T7-9)");
                return;
            }
        }
        panic!("Tab never reached the Publish tab");
    }

    #[test]
    fn connection_view_auto_selects() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let mut probe = Probe::new(WIDE);
        let _ = probe.settle(&mut app);
        assert_eq!(app.detail_view, DetailView::Connection, "launch while disconnected");
        app.detail_view = DetailView::Publish;
        let _ = probe.settle(&mut app);
        assert_eq!(app.detail_view, DetailView::Publish, "only once");
        app.connection_status = ConnectionStatus::Error("refused".into());
        let _ = probe.settle(&mut app);
        assert_eq!(app.detail_view, DetailView::Connection, "a new error shows the form");
    }

    #[test]
    fn gutters_show_chassis() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let f = Probe::new(WIDE).settle(&mut app);
        let p = &crate::colors::LIGHT;
        let mut faces: Vec<egui::Rect> = f.fills().into_iter().filter(|(_, c)| *c == p.panel).map(|(r, _)| r).collect();
        faces.sort_by(|a, b| b.area().total_cmp(&a.area()));
        let (a, b) = (faces[0], faces[1]);
        let (left, right) = if a.min.x < b.min.x { (a, b) } else { (b, a) };
        assert!(right.min.x - left.max.x >= GUTTER - 0.5, "tree and detail faces are {} pt apart", right.min.x - left.max.x);
        let tab_bottom = f.node(DetailView::Publish.label()).unwrap().rect.max.y;
        assert!(right.min.y - tab_bottom >= GUTTER - 0.5, "a chassis strip under the tab row");
    }
}
```

Run: `cargo test --locked app::layout 2>&1 | tail -n 3` — Expected: compile error for `alert_expired`/`GUTTER` (red).

- [ ] **Step b-2: Rewrite `frame_ui`.** Replace the body of `frame_ui` (kept from T1 a1) with:

```rust
    pub(crate) fn frame_ui(&mut self, ctx: &egui::Context) {
        let now = Instant::now();
        self.motion.begin_frame(now);
        self.process_events();
        if self.events_pending {
            ctx.request_repaint();
        }
        if matches!(self.connection_status, ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor) {
            ctx.request_repaint_after(Duration::from_millis(100));
        }
        self.apply_theme(ctx);
        self.auto_select_connection_view(ctx);
        self.track_alert(now);
        let p = crate::colors::palette(self.dark_mode);

        egui::CentralPanel::default()
            .frame(egui::Frame::none().fill(p.chassis).inner_margin(Margin::same(8.0)))
            .show(ctx, |ui| {
                // The gutter link slot, reserved before any face paints (CP-C T8).
                let gutter = ui.painter().add(egui::Shape::Noop);
                self.motion.link_slot(ui.painter(), gutter, ui.max_rect());

                egui::TopBottomPanel::top("header_band")
                    .frame(egui::Frame::none().inner_margin(Margin { bottom: GUTTER / 2.0, ..Default::default() }))
                    .show_separator_line(false)
                    .show_inside(ui, |ui| self.show_header(ui));

                egui::TopBottomPanel::bottom("status_strip")
                    .exact_height(STRIP_H)
                    .frame(egui::Frame::none().inner_margin(Margin::symmetric(4.0, 4.0)))
                    .show_separator_line(false)
                    .show_inside(ui, |ui| self.show_status_strip(ui, now));

                let tree = egui::SidePanel::left("tree_panel")
                    .default_width(400.0)
                    .min_width(250.0)
                    .resizable(true)
                    .show_separator_line(false)
                    .frame(crate::style::glass::face_frame(p).outer_margin(Margin { right: GUTTER, ..Default::default() }))
                    .show_inside(ui, |ui| {
                        let slot = ui.painter().add(egui::Shape::Noop);
                        self.show_tree_panel(ui);
                        (slot, ui.painter().clone())
                    });
                let (slot, painter) = tree.inner;
                self.motion.surface(SurfaceKey::tree_panel(), tree.response.rect, &painter, slot, crate::style::glass::FACE_RADIUS);

                egui::CentralPanel::default().frame(egui::Frame::none()).show_inside(ui, |ui| {
                    egui::TopBottomPanel::top("detail_tabs")
                        .frame(egui::Frame::none().inner_margin(Margin { bottom: GUTTER, ..Default::default() }))
                        .show_separator_line(false)
                        .show_inside(ui, |ui| self.show_view_tabs(ui));
                    let body = crate::style::glass::face(ui, |ui| {
                        ui.set_min_size(ui.available_size());
                        self.show_detail_panel(ui);
                    });
                    self.motion.surface(SurfaceKey::detail_body(), body.rect, &body.painter, body.slot, crate::style::glass::FACE_RADIUS);
                });
            });

        self.motion.end_frame(ctx, now);
        ctx.request_repaint_after(Duration::from_secs(IDLE_REPAINT_SECS));
    }
```

- [ ] **Step b-3: Tabs, strip, auto-select (was CP-A3 T1, T4, T5; CP-A2 T3, T4; CP-A1 T1; CP-B T13).** Add to the same `impl ZenohExplorer`:

```rust
    /// The latched, equal-width tab bank (CP-B T13) with focus rings (CP-A1 T1).
    pub(crate) fn show_view_tabs(&mut self, ui: &mut egui::Ui) {
        let p = crate::style::p(ui);
        ui.horizontal(|ui| {
            ui.label(crate::style::text::label("View:", p));
            for view in DetailView::ALL {
                let selected = self.detail_view == view;
                let cell = egui::vec2(TAB_W, crate::style::keys::KeyTier::Compact.height());
                let inner = ui.allocate_ui_with_layout(cell, egui::Layout::centered_and_justified(egui::Direction::LeftToRight), |ui| {
                    let slot = ui.painter().add(egui::Shape::Noop);
                    let text = RichText::new(view.label())
                        .text_style(egui::TextStyle::Button)
                        .color(crate::style::keys::latched_text_color(p, selected));
                    let r = crate::style::keys::latched_label(ui, selected, text, true);
                    (r, slot, ui.painter().clone())
                });
                let (r, slot, painter) = inner.inner;
                self.motion.surface(SurfaceKey::tab(&view), r.rect, &painter, slot, 4.0);
                if r.clicked() && !selected {
                    self.detail_view = view;
                    self.motion.begin(ActionKind::Tab(view), Instant::now());
                    ui.ctx().request_repaint();
                }
            }
        });
    }

    /// The fixed-height status strip for P1's UiAlert, with expiry (F-T3-2).
    fn show_status_strip(&mut self, ui: &mut egui::Ui, now: Instant) {
        let Some(alert) = self.ui_alert.clone() else { return };
        let age = self.ui_alert_since.as_ref().map_or(Duration::ZERO, |(_, at)| now.duration_since(*at));
        if alert_expired(&alert, age) {
            self.ui_alert = None;
            return;
        }
        let p = crate::style::p(ui);
        let (text, ink) = match &alert {
            UiAlert::Success(t) => (t.clone(), p.ok),
            UiAlert::Warning(t) => (format!("Warning: {t}"), p.warn),
            UiAlert::Error(t) => (format!("Error: {t}"), p.err),
        };
        ui.horizontal(|ui| {
            let k = crate::style::keys::key(ui, "Dismiss", crate::style::keys::KeyTier::Mini, crate::style::keys::KeyRole::Neutral, true, false);
            if k.response.clicked() {
                self.ui_alert = None;
            }
            ui.add(egui::Label::new(RichText::new(&text).color(ink)).truncate()).on_hover_text(&text);
        });
    }

    /// Remembers when the current alert appeared; producers only assign `ui_alert`.
    fn track_alert(&mut self, now: Instant) {
        let current = self.ui_alert_since.as_ref().map(|(a, _)| a);
        if self.ui_alert.as_ref() != current {
            self.ui_alert_since = self.ui_alert.clone().map(|a| (a, now));
        }
    }

    /// Q9 (a): the Connection view at launch while disconnected, and on each new error.
    fn auto_select_connection_view(&mut self, ctx: &egui::Context) {
        let disconnected = matches!(self.connection_status, ConnectionStatus::Disconnected | ConnectionStatus::Error(_));
        if !self.connection_view_shown {
            self.connection_view_shown = true;
            if disconnected {
                self.detail_view = DetailView::Connection;
            }
        }
        let id = egui::Id::new("sw_was_error");
        let is_error = matches!(self.connection_status, ConnectionStatus::Error(_));
        let was_error = ctx.data(|d| d.get_temp::<bool>(id)).unwrap_or(false);
        if is_error && !was_error {
            self.detail_view = DetailView::Connection;
        }
        ctx.data_mut(|d| d.insert_temp(id, is_error));
    }
```

and at module level:

```rust
/// The chassis gutter between faces (Q12 (a)).
pub(crate) const GUTTER: f32 = 16.0;
/// The status strip's constant height: a Mini key plus margins.
pub(crate) const STRIP_H: f32 = 38.0;
/// Equal tab width in the bank.
const TAB_W: f32 = 132.0;

/// Success leaves after 6 s, a warning after 10 s; an error stays until Dismiss.
pub(crate) fn alert_expired(alert: &UiAlert, age: Duration) -> bool {
    match alert {
        UiAlert::Success(_) => age >= Duration::from_secs(6),
        UiAlert::Warning(_) => age >= Duration::from_secs(10),
        UiAlert::Error(_) => false,
    }
}
```

Imports of `layout.rs`: `use eframe::egui; use egui::{Margin, RichText}; use std::time::{Duration, Instant}; use tracing::info; use crate::app::{UiAlert, ZenohExplorer, IDLE_REPAINT_SECS}; use crate::motion::{ActionKind, SurfaceKey}; use crate::types::*; use crate::ui::topic_details::DetailsUI; use crate::ui::topic_tree::TopicTreeUI;`. `grep` must no longer find `alert_banner`, `"toolbar"`, `Quick Actions` or `Button::new("Disconnect")` in `src/app/layout.rs`.

Run: `cargo test --locked app::layout 2>&1 | grep -E '^test |test result'; grep -rn 'alert_banner' src`
Expected: `alert_expiry_rules`, `strip_keeps_workspace_still`, `tabs_sit_over_detail_panel`, `tab_focus_shows_ring`, `connection_view_auto_selects`, `gutters_show_chassis` ok; the grep prints nothing. The header and panels here are still T1's versions in this worktree; the tests use only texts T1 already paints ("Topics", tab labels, "Zenoh Explorer") plus this part's own strip and tabs.

- [ ] **Step b-4: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/app/layout.rs`.

### Part c (was CP-A1 T6 ports; CP-A3 T4 form; CP-B T3/T8/T12 call sites; F-T6-5 locator; CP-A2 T8 link): the Connection view

**Files:**
- Modify: `src/ui/connection.rs`

**Interfaces:**
- Consumes: `validation::{field_number, port_error}`, `crate::style::{glass, text}`, `ZenohExplorer::help_link`, `crate::ui::help::section`.
- Produces: `ConnectionUI::{show_connection_form, start_connect}` (signatures unchanged); `pub(crate) fn port_field(ui: &mut egui::Ui, value: &mut String, range: std::ops::RangeInclusive<u16>, fallback: u16) -> egui::Response`.

- [ ] **Step c-1: Failing tests.** Append to the test module of `src/ui/connection.rs`:

```rust
    use crate::app::probe::{tab_event, Probe, NARROW};
    use egui::{Event, Key, Modifiers};

    fn key(k: Key) -> Event {
        Event::Key { key: k, physical_key: None, pressed: true, repeat: false, modifiers: Modifiers::NONE }
    }

    /// Types `text` into the only focusable field and presses Enter.
    fn type_into(value: &mut String, range: std::ops::RangeInclusive<u16>, text: &str) {
        let (mut app, _tx) = crate::app::ZenohExplorer::test_app();
        let mut probe = Probe::new(NARROW);
        let mut field = value.clone();
        let mut run = |probe: &mut Probe, events: Vec<Event>, field: &mut String| {
            let _ = probe.panel(&mut app, events, |_, ui| {
                port_field(ui, field, range.clone(), 7447);
            });
        };
        run(&mut probe, vec![], &mut field);
        run(&mut probe, vec![tab_event()], &mut field);
        run(&mut probe, vec![key(Key::Backspace); 8], &mut field);
        run(&mut probe, vec![Event::Text(text.into()), key(Key::Enter)], &mut field);
        run(&mut probe, vec![], &mut field);
        *value = field;
    }

    #[test]
    fn ports_cannot_commit_out_of_range() {
        // exact values: a range check alone would pass if keystrokes never reached
        // the DragValue, because the untouched 7447 is inside every range
        for (typed, range, want) in [("99999", 1..=65535u16, 65535u32), ("0", 1..=65535, 1), ("abc", 1..=65535, 7447), ("80", 1024..=65535, 1024)] {
            let mut v = "7447".to_string();
            type_into(&mut v, range.clone(), typed);
            let n: u32 = crate::validation::field_number(&v, 0u32);
            assert_eq!(n, want, "{typed:?} committed as {v:?}");
            assert!(range.contains(&(n as u16)));
        }
    }

    #[test]
    fn form_moves_with_view_only() {
        let (mut app, _tx) = crate::app::ZenohExplorer::test_app();
        let mut probe = Probe::new(NARROW);
        let f = probe.panel(&mut app, vec![], |app, ui| app.show_connection_form(ui));
        assert!(f.text("Connect").is_none() && f.node("Connect").is_none(), "no Connect button in the form (the header key connects)");
        assert!(f.text_containing("dials:").is_some(), "the locator preview names what Connect dials");
        assert!(f.texts().iter().all(|t| !t.text.starts_with('→')), "no tofu arrow (F-T6-1)");
    }
```

Run: `cargo test --locked ui::connection 2>&1 | tail -n 3` — Expected: compile error for `port_field` (red).

- [ ] **Step c-2: `port_field` (was CP-A1 T6).**

```rust
/// A port as a DragValue over the String field P3 T5 persists and the worker
/// parses: typing is committed on Enter or focus loss, clamped to `range`;
/// text that does not parse leaves the value unchanged (F-T4-9).
pub(crate) fn port_field(
    ui: &mut egui::Ui,
    value: &mut String,
    range: std::ops::RangeInclusive<u16>,
    fallback: u16,
) -> egui::Response {
    let mut v: u16 = validation::field_number(value, fallback);
    let r = ui.add(
        egui::DragValue::new(&mut v)
            .range(range)
            .speed(0.0)
            .update_while_editing(false),
    );
    if r.changed() {
        *value = v.to_string();
    }
    r
}
```

- [ ] **Step c-3: The form body (was CP-A3 T4).** Rewrite `show_connection_form` as one face, with the Port and Listen Port rows using `port_field`, the preview in the Legend style with the word "dials:" instead of `→` (F-T6-1, F-T6-5), no italics, palette inks, the error line under the fields with a help link, and no Connect button:

```rust
    fn show_connection_form(&mut self, ui: &mut egui::Ui) {
        let p = crate::style::p(ui);
        crate::style::glass::face(ui, |ui| {
            ui.label(crate::style::text::label("Connection settings", p));
            ui.horizontal(|ui| {
                ui.label(crate::style::text::label("Transport:", p));
                // the Transport ComboBox of layout.rs:281-310, unchanged
                ui.label(crate::style::text::label("Address:", p));
                ui.add(egui::TextEdit::singleline(&mut self.connect_address).desired_width(160.0));
                ui.label(crate::style::text::label("Port:", p));
                port_field(ui, &mut self.connect_port, 1..=65535, 7447);
            });
            if let Some(err) = connect_port_error(&self.connect_address, &self.connect_port) {
                ui.label(RichText::new(err).color(p.err));
            }
            let preview = locator_preview(&self.connection_mode, &self.connect_transport, &self.connect_address, &self.connect_port);
            ui.label(crate::style::text::legend_text(format!("dials: {preview}")).color(p.text_secondary));
            ui.horizontal(|ui| {
                ui.label(crate::style::text::label("Mode:", p));
                // the Mode ComboBox of layout.rs:345-358, unchanged
            });
            if self.connection_mode == "peer" {
                ui.horizontal(|ui| {
                    ui.label(crate::style::text::label("Listen Port:", p));
                    port_field(ui, &mut self.listen_port, 1024..=65535, 7447);
                });
                if let Some(err) = listen_port_error(&self.connection_mode, &self.listen_port) {
                    ui.label(RichText::new(err).color(p.err));
                }
            }
            ui.label(crate::style::text::small(connect_hint(&self.connection_mode, &self.connect_address), p));
            // the stale-error reset of layout.rs:385-404, unchanged (uses LAST_ATTEMPT_ID)
            if let ConnectionStatus::Error(ref err) = self.connection_status {
                let err = err.clone();
                ui.label(RichText::new(format!("Error: {err}")).color(p.err));
                self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);
            }
            // the "N subscriptions resume when you reconnect" note of layout.rs:410-422, with small()
            ui.label(crate::style::text::small("Connect and Disconnect are in the header.", p));
        });
    }
```

The two ComboBoxes, the stale-error reset and the resume note are the T1 code, moved up unchanged except `TEXT_SMALL_SIZE`/`text_secondary_color` → `small(.., p)`. `ExplorerColors` is no longer imported.

Run: `cargo test --locked ui::connection 2>&1 | grep -E '^test |test result'`
Expected: `ports_cannot_commit_out_of_range`, `form_moves_with_view_only`, `connection_hints_match_the_form`, `form_locators_trim_inputs` ok. If `form_locators_trim_inputs`' `locator_preview` assertions still pass unchanged (they test the function, not the label), nothing else changes.

- [ ] **Step c-4: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/connection.rs`.

### Part d (was CP-A1 T1 rows, T2, T3 row controls, T4, T7; CP-A2 T3 🔍, T4, T5, T6, T9 Subscribe; CP-A3 T7 removal, T8; CP-B T3 leaders and inks, T6 roles; CP-C tree, sub.submit, filter.tree, active rows, value changes): the tree panel

**Files:**
- Modify: `src/ui/topic_tree.rs`, `src/types/tree.rs`

**Interfaces:**
- Consumes: `crate::style::{keys, focus, text}`, `crate::motion::{SurfaceKey, ActionKind}`, `DetailsUI::save_topic_to_file`, `help_link`, field `tree_filter_counts`.
- Produces: in `src/types/tree.rs`: `pub fn filter_default_open(full_path: &str, filter_lower: &str) -> bool`, `pub fn count_filter_matches(root: &ZenohNode, filter_lower: &str) -> (usize, usize)`, `pub fn match_range(key: &str, filter_lower: &str) -> Option<std::ops::Range<usize>>`. In `src/ui/topic_tree.rs`: `pub(crate) enum LeafKind { Admin, Json, Text, Binary, Transfer }` with `of`, `glyph`, `word`; `pub(crate) const BRANCH_GLYPH: &str = "📁"`; `ZenohExplorer::subscribe_blocked_reason(&self) -> Option<&'static str>`; `ZenohExplorer::select_tree_path(&mut self, ctx: &egui::Context, path: &str)`. `leaf_icon` is replaced by `LeafKind` (P4 T8/T9 use `LeafKind::Transfer`). `plus_minus_icon` is unchanged.

- [ ] **Step d-1: Tree functions, tests first (was CP-A1 T7, CP-A2 T5).** Append to the tests of `src/types/tree.rs`:

```rust
    fn sample_tree() -> ZenohNode {
        let mut root = ZenohNode::new("root".into());
        root.insert_path("demo/Sensors/Temp1");
        root.insert_path("demo/Sensors/sub/x");
        root.insert_path("demo/other/y");
        root
    }

    #[test]
    fn filter_default_open_rules() {
        // a visible path that does not match itself is visible only for a descendant: open it
        assert!(filter_default_open("demo", "temp"));
        assert!(filter_default_open("demo/Sensors", "temp"));
        // a matching branch keeps its subtree closed (the siblings' subtrees stay closed)
        assert!(filter_default_open("demo", "sensors"));
        assert!(!filter_default_open("demo/Sensors", "sensors"));
        assert!(!filter_default_open("demo", "demo"));
        assert!(!filter_default_open("demo/SENSORS", "sensors"), "case-insensitive");
        let v = compute_visible_paths(&sample_tree(), "sensors");
        assert!(v.contains("demo/Sensors/sub"), "still visible, but under a closed branch");
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
```

Implement after `compute_visible_paths`:

```rust
/// While filtering, a branch opens by default only when its own path does not
/// match: it is visible only because a descendant matches, so it is an
/// ancestor of a direct match. A matching branch stays closed (F-T13-2).
pub fn filter_default_open(full_path: &str, filter_lower: &str) -> bool {
    !full_path.to_lowercase().contains(filter_lower)
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
```

Run: `cargo test --locked types::tree 2>&1 | grep -E 'filter_|match_range|test result'` — Expected: the three new tests and the existing tree tests ok. Commit d-1.

- [ ] **Step d-2: Row and panel tests (red).** Replace `leaf_icons_bucket_correctly` and add to `src/ui/topic_tree.rs` tests:

```rust
    use crate::app::probe::{tab_event, Probe, WIDE};
    use crate::style::focus::TREE_ROW_HEIGHT;

    #[test]
    fn leaf_kind_rules() {
        assert_eq!(LeafKind::of("@/session/x", None, None, false), LeafKind::Admin);
        assert_eq!(LeafKind::of("d/t", Some("application/json"), None, false), LeafKind::Json);
        assert_eq!(LeafKind::of("d/t", Some("text/plain"), Some("hello"), false), LeafKind::Text);
        assert_eq!(LeafKind::of("d/t", Some("application/octet-stream"), None, false), LeafKind::Binary);
        assert_eq!(LeafKind::of("d/t", None, Some("[binary 1024 bytes] ff 00"), false), LeafKind::Binary);
        assert_eq!(LeafKind::of("d/t", None, None, true), LeafKind::Transfer);
        let kinds = [LeafKind::Admin, LeafKind::Json, LeafKind::Text, LeafKind::Binary, LeafKind::Transfer];
        let glyphs: std::collections::HashSet<_> = kinds.iter().map(|k| k.glyph()).collect();
        assert_eq!(glyphs.len(), 5);
        assert!(!glyphs.contains("💾"), "💾 means Save only (F-T4-7)");
        assert!(!glyphs.contains(BRANCH_GLYPH));
        assert!(kinds.iter().all(|k| !k.word().is_empty()));
    }

    #[test]
    fn subscribe_blocked_reason_rules() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.subscribe_key = "demo/**".into();
        assert_eq!(app.subscribe_blocked_reason(), Some("Connect first"));
        app.connection_status = ConnectionStatus::Connected;
        assert_eq!(app.subscribe_blocked_reason(), None);
        assert!(app.subscribe_enabled());
        app.pending_subscribes.insert("demo/**".into());
        assert_eq!(app.subscribe_blocked_reason(), Some("Subscribing…"));
        app.pending_subscribes.clear();
        app.subscriptions.push(Subscription { id: "1".into(), key_expr: "demo/**".into(), reliability: String::new(), mode: String::new() });
        assert_eq!(app.subscribe_blocked_reason(), Some("Already subscribed to this key"));
        app.subscribe_key = "demo//x".into();
        assert_eq!(app.subscribe_blocked_reason(), None, "the inline key error explains it");
    }

    fn tree_app(paths: &[&str], open: &[&str]) -> (ZenohExplorer, Probe) {
        let (mut app, _tx) = ZenohExplorer::test_app();
        for p in paths {
            app.browse_tree.write().unwrap().insert_path(p);
        }
        let probe = Probe::new(WIDE);
        for b in open {
            let mut s = egui::collapsing_header::CollapsingState::load_with_default_open(&probe.ctx, egui::Id::new(("treenode", *b)), true);
            s.set_open(true);
            s.store(&probe.ctx);
        }
        (app, probe)
    }

    fn row(probe: &Probe, path: &str) -> egui::Rect {
        probe.ctx.read_response(egui::Id::new(("treerow", path))).expect(path).rect
    }

    fn panel(probe: &mut Probe, app: &mut ZenohExplorer, events: Vec<egui::Event>) -> crate::app::probe::ProbeFrame {
        probe.panel(app, events, |app, ui| app.show_tree_panel(ui))
    }

    #[test]
    fn tree_row_pitch_is_24() {
        let (mut app, mut probe) = tree_app(&["demo/a", "demo/b"], &["demo"]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let (a, b) = (row(&probe, "demo/a"), row(&probe, "demo/b"));
        assert_eq!(a.height(), TREE_ROW_HEIGHT);
        assert_eq!(b.top() - a.top(), TREE_ROW_HEIGHT, "contiguous 24 pt targets (F-T13-9)");
    }

    #[test]
    fn leaf_sits_right_of_parent() {
        let (mut app, mut probe) = tree_app(&["demo/files/report", "demo/x"], &["demo", "demo/files"]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let f = panel(&mut probe, &mut app, vec![]);
        let files = f.text_containing("files").expect("branch row").rect;
        let report = f.text_containing("report").expect("leaf row").rect;
        assert!(report.left() > files.left(), "a leaf sits right of its parent (F-T13-10)");
    }

    /// Regression guard, not a red/green test: it passes before and after this part
    /// and fails only if the row work moves or resizes egui's expander.
    #[test]
    fn expander_rect_unchanged() {
        let (mut app, mut probe) = tree_app(&["demo/a"], &[]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let spacing = probe.ctx.style().spacing.clone();
        let toggle = probe.ctx.read_response(egui::Id::new(("treenode", "demo"))).unwrap().rect;
        assert_eq!(toggle.size(), egui::vec2(spacing.indent, spacing.icon_width));
    }

    #[test]
    fn full_row_click_selects() {
        let (mut app, mut probe) = tree_app(&["demo/a", "demo/b"], &["demo"]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let r = row(&probe, "demo/a");
        let (press, release) = crate::app::probe::click_events(egui::pos2(r.right() - 30.0, r.center().y));
        let _ = panel(&mut probe, &mut app, press);
        let _ = panel(&mut probe, &mut app, release);
        assert_eq!(app.selected_topic.as_deref(), Some("demo/a"), "a click on the leader area selects");
        let toggle = probe.ctx.read_response(egui::Id::new(("treenode", "demo"))).unwrap().rect;
        app.selected_topic = None;
        let (press, release) = crate::app::probe::click_events(toggle.center());
        let _ = panel(&mut probe, &mut app, press);
        let _ = panel(&mut probe, &mut app, release);
        assert_eq!(app.selected_topic, None, "the expander toggles and does not select");
    }

    #[test]
    fn focused_row_shows_ring_not_latch() {
        let (mut app, mut probe) = tree_app(&["demo/a", "demo/b"], &["demo"]);
        let _ = panel(&mut probe, &mut app, vec![]);
        let target = row(&probe, "demo/b");
        let focus = crate::colors::LIGHT.focus;
        for _ in 0..40 {
            let _ = panel(&mut probe, &mut app, vec![tab_event()]);
            let f = panel(&mut probe, &mut app, vec![]);
            let Some(id) = probe.focused() else { continue };
            let Some(r) = probe.ctx.read_response(id) else { continue };
            if target.contains_rect(r.rect) && id != egui::Id::new(("treenode", "demo")) {
                assert!(f.strokes().iter().any(|(rect, s)| s.color == focus && (rect.height() - (target.height() - 2.0)).abs() < 0.6),
                    "an inset ring on the whole row");
                assert!(!f.fills().iter().any(|(rect, c)| *c == crate::colors::LIGHT.selected && target.contains_rect(*rect)),
                    "an unselected focused row shows no latch fill (F-T7-9)");
                return;
            }
        }
        panic!("Tab never reached demo/b");
    }

    #[test]
    fn selected_row_has_accent_bar() {
        let (mut app, mut probe) = tree_app(&["demo/a", "demo/b"], &["demo"]);
        app.selected_topic = Some("demo/a".into());
        let _ = panel(&mut probe, &mut app, vec![]);
        let f = panel(&mut probe, &mut app, vec![]);
        let r = row(&probe, "demo/a"); // the catcher: for a leaf it starts at the expander column (row.left() + 12 · depth)
        let indent = probe.ctx.style().spacing.indent; // so "right of the expander column" is catcher.left + one indent
        assert!(f.fills().iter().any(|(rect, c)| *c == crate::colors::LIGHT.selected_bar
            && (rect.width() - 3.0).abs() < 0.1
            && rect.left() >= r.left() + indent
            && r.contains_rect(*rect)), "a 3 pt bar right of the expander column (F-T13-11)");
    }

    #[test]
    fn expander_focus_shows_ring() {
        let (mut app, mut probe) = tree_app(&["demo/a"], &[]);
        let _ = panel(&mut probe, &mut app, vec![tab_event()]); // keyboard modality
        probe.ctx.memory_mut(|m| m.request_focus(egui::Id::new(("treenode", "demo"))));
        let f = panel(&mut probe, &mut app, vec![]);
        let toggle = probe.ctx.read_response(egui::Id::new(("treenode", "demo"))).unwrap().rect;
        assert!(f.strokes().iter().any(|(rect, s)| s.color == crate::colors::LIGHT.focus && rect.contains_rect(toggle)),
            "a ring outside the expander, which itself is unchanged (F-T7-10)");
    }

    #[test]
    fn filter_counts_and_hint() {
        let (mut app, mut probe) = tree_app(&["a/x", "a/y", "b/x"], &[]);
        let f = panel(&mut probe, &mut app, vec![]);
        assert!(f.text("Filter topics").is_some(), "hint text");
        assert!(f.text("Clear filter").is_none(), "shown only while filtering");
        assert!(f.text("🔍").is_none(), "🔍 belongs to the Query tab (F-T4-4)");
        assert!(f.texts().iter().all(|t| !t.text.contains("Back to All Messages")), "Back moved to the detail heading (F-T13-12)");
        app.tree_filter = "x".into();
        let _ = panel(&mut probe, &mut app, vec![]);
        let f = panel(&mut probe, &mut app, vec![]);
        assert!(f.text("2 of 3 topics").is_some());
        assert!(f.text("Clear filter").is_some());
    }
```

Run: `cargo test --locked ui::topic_tree 2>&1 | tail -n 3` — Expected: compile errors (red).

- [ ] **Step d-3: Leaf kinds, reasons, selection (was CP-A2 T6, T9; CP-A3 T8).** Replace `leaf_icon` (lines 985-1007) with:

```rust
/// One neutral glyph for every branch, at any depth (F-T13-6).
pub(crate) const BRANCH_GLYPH: &str = "📁";

/// What a leaf holds, as a glyph and a hover word (F-T13-6, F-T4-7).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LeafKind {
    Admin,
    Json,
    Text,
    Binary,
    Transfer,
}

impl LeafKind {
    /// Prefers a transfer, then the admin space, then the declared encoding,
    /// then the preview heuristic (binary previews start with "[binary").
    pub(crate) fn of(full_path: &str, encoding: Option<&str>, last_payload: Option<&str>, transfer: bool) -> Self {
        if transfer {
            return LeafKind::Transfer;
        }
        if full_path.starts_with('@') {
            return LeafKind::Admin;
        }
        if let Some(enc) = encoding {
            let e = enc.to_ascii_lowercase();
            if e.contains("json") {
                return LeafKind::Json;
            }
            if e.starts_with("text/") {
                return LeafKind::Text;
            }
            if e.contains("octet-stream") {
                return LeafKind::Binary;
            }
        }
        match last_payload {
            Some(p) if p.starts_with("[binary") => LeafKind::Binary,
            Some(_) => LeafKind::Text,
            None => LeafKind::Binary,
        }
    }

    pub(crate) fn glyph(self) -> &'static str {
        match self {
            LeafKind::Admin => "🛠",
            LeafKind::Json => "🔣",
            LeafKind::Text => "📝",
            LeafKind::Binary => "■",
            LeafKind::Transfer => "📥",
        }
    }

    pub(crate) fn word(self) -> &'static str {
        match self {
            LeafKind::Admin => "Zenoh admin key",
            LeafKind::Json => "JSON value",
            LeafKind::Text => "Text value",
            LeafKind::Binary => "Binary value",
            LeafKind::Transfer => "File transfer",
        }
    }
}
```

Add to the `impl ZenohExplorer` block holding `subscribe_enabled`:

```rust
    /// Why Subscribe is disabled, when P1's inline key error does not say it (F-T7-3).
    pub(crate) fn subscribe_blocked_reason(&self) -> Option<&'static str> {
        let key = self.subscribe_key.trim();
        if crate::validation::key_expr_error(&self.subscribe_key).is_some() {
            None
        } else if !matches!(self.connection_status, ConnectionStatus::Connected) {
            Some("Connect first")
        } else if self.subscriptions.iter().any(|s| s.key_expr == key) {
            Some("Already subscribed to this key")
        } else if self.pending_subscribes.contains(key) {
            Some("Subscribing…")
        } else {
            None
        }
    }

    /// A tree row click: Q10 default keeps today's jump to Topics, and repaints
    /// at once so the latch shows without waiting for input (F-T8-9).
    pub(crate) fn select_tree_path(&mut self, ctx: &egui::Context, path: &str) {
        self.selected_topic = Some(path.to_string());
        self.detail_view = DetailView::TopicDetails;
        self.motion.begin(ActionKind::TreeSelect(path.to_string()), Instant::now());
        ctx.request_repaint();
    }
```

- [ ] **Step d-4: The panel top (was CP-A2 T3, T4, T5, T9; CP-A3 T7; CP-A1 T2 header ring; CP-B T6).** Replace `show_tree_panel`'s opening `ui.vertical(|ui| {`, filter row, Back button, Subscribe group, separator and "Topics" label (lines 163-228, from `ui.vertical(|ui| {` through `ui.label(RichText::new("Topics").strong());`) with the block below, which re-opens the same `ui.vertical` closure and ends with the new separator and "Topics" label (so exactly one "Topics" label remains and the braces stay balanced):

```rust
        let p = crate::style::p(ui);
        ui.vertical(|ui| {
            // Filter: hint, "n of m topics", a worded clear shown only while filtering
            ui.horizontal(|ui| {
                let field = ui
                    .add(egui::TextEdit::singleline(&mut self.tree_filter).hint_text("Filter topics").desired_width(180.0))
                    .on_hover_text("Filter topics");
                let slot = ui.painter().add(egui::Shape::Noop);
                self.motion.surface(SurfaceKey::filter_tree(), field.rect, ui.painter(), slot, 4.0);
                if field.changed() {
                    self.motion.begin(ActionKind::FilterTree, Instant::now());
                }
                if !self.tree_filter.is_empty() {
                    if let Some((n, m)) = self.tree_filter_counts {
                        ui.label(crate::style::text::small(format!("{n} of {m} topics"), p));
                    }
                    let k = key(ui, "Clear filter", KeyTier::Mini, KeyRole::Neutral, true, false);
                    if k.response.clicked() {
                        self.tree_filter.clear();
                    }
                }
            });
            ui.separator();
            let sub = ui.collapsing("Subscribe to Topics", |ui| {
                ui.horizontal(|ui| {
                    ui.label(crate::style::text::label("Key:", p));
                    ui.text_edit_singleline(&mut self.subscribe_key);
                });
                if let Some(err) = crate::validation::key_expr_error(&self.subscribe_key) {
                    ui.label(RichText::new(err).color(p.err));
                    self.help_link(ui, crate::ui::help::section::KEY_EXPRESSIONS);
                }
                ui.horizontal(|ui| {
                    let owned = self.motion.owns(&SurfaceKey::sub_submit());
                    let k = key(ui, "Subscribe", KeyTier::Standard, KeyRole::Primary, self.subscribe_enabled(), owned);
                    self.motion.surface(SurfaceKey::sub_submit(), k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
                    if k.response.clicked() {
                        if let Some(sender) = &self.command_sender {
                            let _ = sender.send(ZenohCommand::Subscribe {
                                key_expr: self.subscribe_key.clone(),
                                reliability: self.subscribe_reliability.clone(),
                                mode: self.subscribe_mode.clone(),
                            });
                        }
                        let key_expr = self.subscribe_key.trim().to_string();
                        self.pending_subscribes.insert(key_expr.clone());
                        self.motion.begin(ActionKind::Subscribe(key_expr), Instant::now());
                    }
                    // The reason sits beside the key, not only in a hover (F-T7-3)
                    if let Some(reason) = self.subscribe_blocked_reason() {
                        ui.label(crate::style::text::small(reason, p));
                    }
                });
                if !self.subscriptions.is_empty() {
                    ui.label(crate::style::text::label("Active:", p));
                    let rows: Vec<(String, String)> = self.subscriptions.iter().map(|s| (s.id.clone(), s.key_expr.clone())).collect();
                    for (id, key_expr) in rows {
                        let r = ui.horizontal(|ui| {
                            let slot = ui.painter().add(egui::Shape::Noop);
                            ui.label(crate::style::text::legend_text(&key_expr));
                            let k = key(ui, "Unsubscribe", KeyTier::Mini, KeyRole::Neutral, true, false);
                            if k.response.on_hover_text(format!("Unsubscribe {key_expr}")).clicked() {
                                if let Some(sender) = &self.command_sender {
                                    let _ = sender.send(ZenohCommand::Unsubscribe { subscription_id: id.clone() });
                                }
                            }
                            (slot, ui.painter().clone())
                        });
                        let (slot, painter) = r.inner;
                        self.motion.surface(SurfaceKey::active(&key_expr), r.response.rect, &painter, slot, 4.0);
                    }
                }
            });
            if crate::style::focus::keyboard_focused(&sub.header_response) {
                crate::style::focus::paint_focus_ring(ui, sub.header_response.rect, crate::style::focus::RING_OUTSET);
            }
            ui.separator();
            ui.label(crate::style::text::label("Topics", p));
```

Keep the tree read guard and the filter cache block (lines 230-274); when the cache is recomputed, also set `self.tree_filter_counts = Some(count_filter_matches(tree, &filter_lower));`, and set it to `None` where the cache is cleared. In the `ScrollArea` closure (line 278) add `ui.spacing_mut().item_spacing.y = 0.0;` as the first statement. The empty state becomes body text with a help link (no italics, no `HEADING_MEDIUM_SIZE`):

```rust
                    if tree.children.is_empty() {
                        ui.vertical_centered(|ui| {
                            ui.add_space(32.0);
                            ui.label(RichText::new("No topics yet").color(p.text_secondary));
                            ui.label(crate::style::text::small("Topics appear here as this app receives data", p));
                            ui.label(crate::style::text::small("Try demo/** or sensor/* in Subscribe to Topics above", p));
                            self.help_link(ui, crate::ui::help::section::GETTING_STARTED);
                        });
                    }
```

(the "No topics match the filter" line keeps its words, in `small`). Imports: `use crate::style::keys::{key, latched_label, KeyRole, KeyTier, KEY_RADIUS}; use crate::motion::{ActionKind, SurfaceKey}; use crate::ui::topic_details::DetailsUI;` and drop `ExplorerColors`.

- [ ] **Step d-5: The row (was CP-A1 T1, T3, T4, T7; CP-A2 T5, T6; CP-B T3 leader; CP-C tree rows and value changes).** Change `leader_line_with_count` to take the leader colour and draw at full alpha, and give leaves a dotted line (F-T13-5):

```rust
fn leader_line_with_count(ui: &mut egui::Ui, line: Option<bool>, count: usize, count_color: egui::Color32, leader: egui::Color32) -> Option<egui::Response> {
    if count == 0 {
        return None;
    }
    let count_text = count.to_string();
    let font = egui::TextStyle::Small.resolve(ui.style());
    let galley = ui.painter().layout_no_wrap(count_text.clone(), font, count_color);
    let line_w = (ui.available_width() - galley.size().x - 16.0).max(0.0);
    let (rect, _) = ui.allocate_exact_size(egui::vec2(line_w, ui.spacing().interact_size.y), egui::Sense::hover());
    let y = rect.center().y;
    let (a, b) = (egui::pos2(rect.left() + 4.0, y), egui::pos2(rect.right() - 4.0, y));
    if rect.width() > 12.0 {
        let stroke = egui::Stroke::new(1.0, leader);
        match line {
            Some(true) => {
                ui.painter().line_segment([a, b], stroke);
            }
            Some(false) => ui.painter().extend(egui::Shape::dashed_line(&[a, b], stroke, 3.0, 3.0)),
            None => ui.painter().extend(egui::Shape::dotted_line(&[a, b], leader, 4.0, 0.75)),
        }
    }
    Some(ui.label(egui::RichText::new(count_text).text_style(egui::TextStyle::Small).color(count_color)))
}
```

Replace `show_tree_node` (lines 735-940) with:

```rust
    fn show_tree_node(&mut self, ui: &mut egui::Ui, node: &ZenohNode, parent_path: String, depth: usize) {
        let full_path = if parent_path.is_empty() { node.key.clone() } else { format!("{}/{}", parent_path, node.key) };
        if let Some((_, _, _, visible)) = &self.tree_filter_cache {
            if !visible.contains(&full_path) {
                return;
            }
        }
        let p = crate::style::p(ui);
        let filter_lower = self.tree_filter.to_lowercase();
        let filtering = self.tree_filter_cache.is_some();
        let indent = 12.0 * depth as f32;
        let is_selected = self.selected_topic.as_ref() == Some(&full_path);
        let is_branch = !node.children.is_empty();
        let mut state = is_branch.then(|| {
            let id = if filtering {
                egui::Id::new(("treenode_filtered", &full_path))
            } else {
                egui::Id::new(("treenode", &full_path))
            };
            egui::collapsing_header::CollapsingState::load_with_default_open(
                ui.ctx(),
                id,
                filtering && filter_default_open(&full_path, &filter_lower),
            )
        });
        let expanded = state.as_ref().is_some_and(|s| s.is_open());
        let transfer_snapshot: Option<TransferState> = node.transfer.clone();
        let mut select = false;

        ui.allocate_ui_with_layout(
            egui::vec2(ui.available_width(), TREE_ROW_HEIGHT),
            egui::Layout::left_to_right(egui::Align::Center),
            |ui| {
                let row = ui.max_rect();
                let bar_slot = ui.painter().add(egui::Shape::Noop);
                let bevel_slot = ui.painter().add(egui::Shape::Noop);
                let expander_x = row.left() + indent;
                let label_x = expander_x + ui.spacing().indent; // right of the expander column
                ui.add_space(indent);
                let catch_x = if let Some(state) = state.as_mut() {
                    let toggle = state.show_toggle_button(ui, plus_minus_icon);
                    if focus::keyboard_focused(&toggle) {
                        focus::paint_focus_ring(ui, toggle.rect, focus::RING_OUTSET);
                    }
                    toggle.rect.right()
                } else {
                    // leaf placeholder: a leaf sits right of its parent (F-T13-10)
                    ui.add_space(ui.spacing().indent + ui.spacing().item_spacing.x);
                    expander_x
                };
                // The full-row catcher is registered before the label and 💾,
                // so those stay on top in their own rects (egui hit_test.rs:68, 324).
                let catch = egui::Rect::from_min_max(egui::pos2(catch_x, row.top()), row.max);
                let catcher = ui.interact(catch, egui::Id::new(("treerow", &full_path)), egui::Sense { click: true, drag: false, focusable: false });

                let kind = (!is_branch).then(|| {
                    LeafKind::of(&full_path, node.last_encoding.as_deref(), node.last_payload.as_deref(), node.transfer.is_some())
                });
                let glyph = kind.map_or(BRANCH_GLYPH, LeafKind::glyph);
                let own = match_range(&node.key, &filter_lower);
                let context_only = filtering && !full_path.to_lowercase().contains(&filter_lower);
                let job = row_label(ui, glyph, &node.key, own, context_only, is_selected, p);
                let mut label = latched_label(ui, is_selected, job, false);
                if let Some(kind) = kind {
                    label = label.on_hover_text(kind.word());
                }
                if node.is_local {
                    let (r, resp) = ui.allocate_exact_size(egui::vec2(8.0, 8.0), egui::Sense::hover());
                    ui.painter().circle_filled(r.center(), 4.0, p.ok);
                    resp.on_hover_text("Published from this app");
                }
                if is_branch && node.message_count > 0 {
                    ui.label(crate::style::text::small(format!("({})", node.message_count), p));
                }
                if self.paused_keys.contains(&full_path) {
                    ui.label(crate::style::text::small("⏸ paused", p));
                }
                if let Some(t) = &transfer_snapshot {
                    render_transfer_progress(ui, t, p);
                }
                if !is_branch && node.transfer.is_none() {
                    if let Some(payload) = &node.last_payload {
                        let preview = if payload.len() > 30 {
                            format!("{}...", &payload[..safe_truncate_index(payload, 30)])
                        } else {
                            payload.clone()
                        };
                        ui.label(crate::style::text::content(preview).color(p.text_secondary));
                    }
                    let exportable = node.transfer.as_ref().is_some_and(|t| t.is_complete())
                        || self.payload_store.read().is_ok_and(|s| s.contains_key(&full_path));
                    if exportable {
                        let k = key(ui, "💾", KeyTier::Mini, KeyRole::Neutral, true, false);
                        if k.response.on_hover_text("Save file").clicked() {
                            self.save_topic_to_file(&full_path);
                        }
                    }
                }
                let (count, line) = if is_branch { (node.cumulative_leaves, Some(expanded)) } else { (node.message_count, None) };
                if let Some(r) = leader_line_with_count(ui, line, count, p.text_secondary, p.leader) {
                    r.on_hover_text(count_hover(is_branch, count));
                }
                if catcher.clicked() || label.clicked() {
                    select = true;
                }
                if is_selected {
                    let bar = egui::Rect::from_min_size(egui::pos2(label_x + 2.0, row.top() + 3.0), egui::vec2(3.0, row.height() - 6.0));
                    ui.painter().set(bar_slot, egui::Shape::rect_filled(bar, 1.0, p.selected_bar));
                }
                if focus::keyboard_focused(&label) {
                    focus::paint_focus_ring(ui, row, -1.0);
                }
                self.motion.surface(SurfaceKey::tree(&full_path), row, ui.painter(), bevel_slot, 4.0);
                // A count change gives the row one short, coalesced response (CP-C T14)
                let seen_id = egui::Id::new(("tree_count", &full_path));
                let before = ui.ctx().data(|d| d.get_temp::<usize>(seen_id));
                if before.is_some_and(|b| b != node.message_count) {
                    self.motion.value_changed(SurfaceKey::tree(&full_path), Instant::now());
                }
                ui.ctx().data_mut(|d| d.insert_temp(seen_id, node.message_count));
            },
        );
        if select {
            self.select_tree_path(ui.ctx(), &full_path);
        }
        if let Some(state) = state {
            state.show_body_unindented(ui, |ui| {
                for child in node.children.values() {
                    self.show_tree_node(ui, child, full_path.clone(), depth + 1);
                }
            });
        }
    }
```

and add the label builder and the progress restyle (the bar gets an explicit fill that meets 3:1, `✓` becomes `✔`):

```rust
/// The row label as a layout job: glyph, key, the matched substring
/// underlined (not recoloured), and context-only rows in secondary ink (F-T13-1).
fn row_label(ui: &egui::Ui, glyph: &str, key: &str, own: Option<std::ops::Range<usize>>, context_only: bool, selected: bool, p: &crate::colors::Palette) -> egui::text::LayoutJob {
    let font = egui::TextStyle::Button.resolve(ui.style());
    let base = if selected {
        p.selected_text
    } else if context_only {
        p.text_secondary
    } else {
        p.text
    };
    let fmt = |underline: bool| egui::TextFormat {
        font_id: font.clone(),
        color: base,
        underline: if underline { egui::Stroke::new(1.5, base) } else { egui::Stroke::NONE },
        ..Default::default()
    };
    let mut job = egui::text::LayoutJob::default();
    job.append(&format!("{glyph} "), 0.0, fmt(false));
    match own {
        Some(r) => {
            job.append(&key[..r.start], 0.0, fmt(false));
            job.append(&key[r.clone()], 0.0, fmt(true));
            job.append(&key[r.end..], 0.0, fmt(false));
        }
        None => job.append(key, 0.0, fmt(false)),
    }
    job
}

fn render_transfer_progress(ui: &mut egui::Ui, t: &TransferState, p: &crate::colors::Palette) {
    let frac = t.received.len() as f32 / t.total_chunks.max(1) as f32;
    ui.add(
        egui::ProgressBar::new(frac)
            .desired_width(120.0)
            .fill(p.selected_bar)
            .text(format!("{}/{}", t.received.len(), t.total_chunks)),
    );
    let text = if t.is_complete() {
        format!("✔ {}", transfer::format_size(t.total_size))
    } else {
        format!(
            "⬇ {} of {}",
            transfer::format_size(t.received.len().saturating_mul(crate::transfer::CHUNK_SIZE).min(t.total_size)),
            transfer::format_size(t.total_size)
        )
    };
    ui.label(crate::style::text::small(text, p));
}
```

Imports add `use crate::style::focus::{self, TREE_ROW_HEIGHT};`, `use std::time::Instant;` (already present) and `crate::types::*` covers `filter_default_open`, `match_range`, `count_filter_matches`. `animate_fade_in` is no longer called here (T3 deletes it).

Run: `cargo test --locked ui::topic_tree 2>&1 | grep -E '^test |test result'; grep -n 'small_button' src/ui/topic_tree.rs; grep -n '"●"' src/ui/topic_tree.rs`, then the expander guard of "Local tooling and disk" with `<rev>` = `"$BASE"`:

```bash
git show "$BASE":src/ui/topic_tree.rs | python3 "$SWRUN/fn_body.py" plus_minus_icon > "$SWRUN/pmi-base-d.txt" &&
python3 "$SWRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$SWRUN/pmi-head-d.txt" &&
cmp "$SWRUN/pmi-base-d.txt" "$SWRUN/pmi-head-d.txt" && echo expander-unchanged
```

Expected: `leaf_kind_rules`, `subscribe_blocked_reason_rules`, `tree_row_pitch_is_24`, `leaf_sits_right_of_parent`, `expander_rect_unchanged`, `full_row_click_selects`, `focused_row_shows_ring_not_latch`, `selected_row_has_accent_bar`, `expander_focus_shows_ring`, `filter_counts_and_hint`, `double_subscribe_is_ignored_while_pending`, `count_hover_names_unit` ok; the two greps print nothing; the guard prints `expander-unchanged` (it compares the brace-matched text of the function, so a move within the file does not matter).

- [ ] **Step d-6: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/topic_tree.rs` and `src/types/tree.rs`.

### Part e (was CP-A2 T3 ⏵/⏷, T9 Save reason; CP-A3 T7 heading, T11 History; CP-B T3, T5, T6, T8, T14; CP-A1 T3 history Save; CP-C save source and inline receiver): the detail view

**Files:**
- Modify: `src/ui/topic_details.rs`

**Interfaces:**
- Consumes: `crate::ui::message_row::{message_row, types_mixed}`, `crate::style::{glass, content, keys, text}`, `crate::motion`, field `save_result`, `help_link`.
- Produces: `DetailsUI` (signatures unchanged); `save_topic_to_file` also sets `self.save_result` and starts the Save motion on success.

- [ ] **Step e-1: Failing tests.** Append to the tests of `src/ui/topic_details.rs` (the existing `details_texts` helper stays; add a probe variant):

```rust
    use crate::app::probe::{Probe, WIDE};

    fn details(app: &mut ZenohExplorer) -> crate::app::probe::ProbeFrame {
        let mut probe = Probe::new(WIDE);
        let _ = probe.panel(app, vec![], |app, ui| app.show_topic_details(ui));
        probe.panel(app, vec![], |app, ui| app.show_topic_details(ui))
    }

    fn put(app: &ZenohExplorer, path: &str, payload: &str, enc: &str) {
        app.browse_tree.write().unwrap().insert_path(path).update_data(payload.into(), enc.into(), false, SampleKindView::Put, None);
    }

    #[test]
    fn back_is_in_heading() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        put(&app, "demo/x", "1", "text/plain");
        app.selected_topic = Some("demo/x".into());
        let f = details(&mut app);
        let back = f.text("⬅ All Messages").expect("Back key in the heading row");
        let heading = f.text("demo/x").expect("heading");
        assert!((back.rect.center().y - heading.rect.center().y).abs() < 12.0, "same row");
        let node = f.node("⬅ All Messages").unwrap();
        let mut probe = Probe::new(WIDE);
        let _ = probe.panel(&mut app, vec![], |app, ui| app.show_topic_details(ui));
        let (press, release) = crate::app::probe::click_events(node.rect.center());
        let _ = probe.panel(&mut app, press, |app, ui| app.show_topic_details(ui));
        let _ = probe.panel(&mut app, release, |app, ui| app.show_topic_details(ui));
        assert_eq!(app.selected_topic, None);
    }

    #[test]
    fn save_reason_is_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        put(&app, "demo/x", "1", "text/plain");
        app.selected_topic = Some("demo/x".into());
        let f = details(&mut app);
        assert!(f.text("No payload stored yet").is_some(), "the reason is painted text, not only a hover (F-T7-3)");
    }

    #[test]
    fn history_rows_are_one_line() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        put(&app, "demo/x", "b", "text/plain");
        for payload in ["first-val", "second-val"] {
            app.messages.push_back(ZenohMessage::new_with_bytes("demo/x".into(), payload.into(), payload.as_bytes().to_vec(), "text/plain".into(), chrono::Utc::now(), MessageType::Subscribe, false, MessageSource::MonitorSession));
        }
        app.selected_topic = Some("demo/x".into());
        let f = details(&mut app);
        let first = f.text("first-val").expect("row").rect;
        let second = f.text("second-val").expect("row").rect;
        assert!(second.top() > first.top(), "newest at the bottom (one order in both lists)");
        let times: Vec<_> = f.texts().into_iter().filter(|t| t.text.len() == "12:00:00.000".len() && t.text.contains(':')).collect();
        assert!(times.iter().any(|t| (t.rect.center().y - first.center().y).abs() < 4.0), "time and payload on one line");
        assert!(!f.texts().iter().any(|t| t.text == "SUB"), "no type legend when types do not mix");
    }

    #[test]
    fn current_value_uses_content_display() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        put(&app, "demo/j", "{\"a\": 1}", "application/json");
        app.selected_topic = Some("demo/j".into());
        let f = details(&mut app);
        assert!(f.fills().iter().any(|(_, c)| *c == crate::colors::LIGHT.content_glass), "a contentGlass display");
        assert!(f.text_containing("\"a\"").is_some());
    }
```

Run: `cargo test --locked ui::topic_details 2>&1 | tail -n 3` — Expected: failures (red).

- [ ] **Step e-2: Heading, action row, inline Save result (was CP-A3 T7, CP-A2 T9, CP-B T6, CP-C Save).** In `show_topic_details`, replace `ui.heading(topic);` with:

```rust
            let p = crate::style::p(ui);
            ui.horizontal(|ui| {
                let k = key(ui, "⬅ All Messages", KeyTier::Compact, KeyRole::Neutral, true, false);
                if k.response.clicked() {
                    self.selected_topic = None;
                }
                // a key expression: mono, at heading size (F-T6-8)
                ui.label(crate::style::text::heading(topic.as_str()).family(egui::FontFamily::Monospace));
            });
```

Replace the action row (lines 377-459 of `3ce8c01`, now in this file) with the same computation of `(saveable, size, reason)`, then:

```rust
                let label = match size {
                    Some(s) => format!("💾 Save File ({})", transfer::format_size(s)),
                    None => "💾 Save File".to_string(),
                };
                let skey = SurfaceKey::save(topic);
                let k = key(ui, label, KeyTier::Standard, KeyRole::Action, saveable, self.motion.owns(&skey));
                self.motion.surface(skey, k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
                let response = if saveable {
                    k.response.on_hover_text("Save full payload to file (original size)")
                } else {
                    k.response.on_disabled_hover_text(reason.clone())
                };
                if !saveable {
                    ui.label(crate::style::text::small(reason.as_str(), p));
                }
                if response.clicked() {
                    let topic_owned = topic.clone();
                    self.save_topic_to_file(&topic_owned);
                }
                // The inline Save result, the effect's receiver (F-T12-3)
                let inline_slot = ui.painter().add(egui::Shape::Noop);
                let inline = ui.allocate_ui(egui::vec2(260.0, KeyTier::Standard.height()), |ui| {
                    if let Some((t, alert)) = &self.save_result {
                        if t == topic {
                            let ink = if matches!(alert, UiAlert::Error(_)) { p.err } else { p.ok };
                            ui.add(egui::Label::new(RichText::new(alert.text()).color(ink)).truncate())
                                .on_hover_text(alert.text());
                        }
                    }
                });
                self.motion.surface(SurfaceKey::save_inline(topic), inline.response.rect, ui.painter(), inline_slot, 4.0);
                let is_paused = self.paused_keys.contains(topic);
                let pk = key(ui, if is_paused { "▶ Resume list" } else { "⏸ Pause list" }, KeyTier::Compact, KeyRole::Neutral, true, false);
                if pk.response
                    .on_hover_text("Stop adding this topic's messages to the lists; its value and count keep updating")
                    .clicked()
                {
                    if is_paused {
                        self.paused_keys.remove(topic);
                    } else {
                        self.paused_keys.insert(topic.clone());
                    }
                }
                if is_paused {
                    ui.label(RichText::new("Paused (lists only)").color(p.warn));
                }
```

In `save_topic_to_file`, set both the strip record and the inline result, and start the Save motion only on success (F-T10-1):

```rust
                    Ok(Some(path)) => {
                        let alert = UiAlert::Success(format!("Saved to {}", path.display()));
                        self.save_result = Some((topic.to_string(), alert.clone()));
                        self.ui_alert = Some(alert);
                        self.motion.begin(ActionKind::Save(topic.to_string()), Instant::now());
                    }
                    Ok(None) => {} // user cancelled
                    Err(e) => {
                        let alert = UiAlert::Error(format!("Save failed: {}", e));
                        self.save_result = Some((topic.to_string(), alert.clone()));
                        self.ui_alert = Some(alert);
                    }
```

(and the same two assignments for the outer `Err(e)` arm).

- [ ] **Step e-3: Current Value, chunks, metadata, History (was CP-B T14, T8; CP-A2 T3; CP-A3 T11).**
  - Chunk section: labels use `text::label`; `"✓ All chunks received — ready to save"` becomes `"✔ All chunks received — ready to save"` in `p.ok`; its `small_button("💾 Save")` becomes `key(ui, "💾 Save", KeyTier::Mini, KeyRole::Neutral, true, false)`; the waiting line uses `p.warn`.
  - Expand/Collapse labels: `"⏷ Collapse"` and `format!("⏵ Expand (+{} bytes)", hidden_bytes)` in a `KeyTier::Compact` neutral key.
  - Current Value: `ui.label(crate::style::text::label("Current value", p));` then

```rust
                crate::style::glass::content_glass(ui, |ui| {
                    egui::ScrollArea::vertical()
                        .id_salt(format!("payload_{}", topic))
                        .max_height(400.0)
                        .show(ui, |ui| {
                            let stored = self.payload_store.read().ok().and_then(|s| s.get(topic.as_str()).map(|e| e.bytes.clone()));
                            if let Some(pretty) = self.get_cached_json(&display_payload) {
                                let font = egui::TextStyle::Monospace.resolve(ui.style());
                                ui.label(crate::style::content::json_job(&pretty, p, font));
                            } else if let (true, Some(bytes)) = (display_payload.starts_with("[binary"), stored) {
                                for row in crate::style::content::hex_ascii_rows(&bytes, 4096) {
                                    ui.label(crate::style::text::content(row));
                                }
                            } else {
                                ui.label(crate::style::text::content(&display_payload));
                            }
                        });
                });
```

  - Encoding and source time: `text::label("Encoding:", p)` / `text::label("Source time:", p)` beside `text::legend_text(value)`; "Received: n (since app start)" keeps its words as a label/value pair; "Last sample: DELETE" keeps its words in body text. (The existing tests look up "Source time:" and "Last sample: DELETE" by exact text: keep both strings.)
  - History (CP-A3 T11): the scan keeps P1's code (newest first, 50 cards, scan note); render newest at the bottom with the shared row:

```rust
                        let show_type = crate::ui::message_row::types_mixed(topic_messages.iter().copied());
                        let wall_now = chrono::Utc::now();
                        for message in topic_messages.iter().rev() {
                            crate::ui::message_row::message_row(ui, message, &wall_now, false, show_type);
                        }
```

    and give the History `ScrollArea` `.stick_to_bottom(true)`. The empty-state headings use body text in `p.text_secondary` (no `HEADING_MEDIUM_SIZE`, no italics).
  - The branch summary's child list uses `latched_label(ui, false, child, true)` instead of `selectable_label(false, child)` (ring scope, Open question 7).

Imports: `use crate::motion::{ActionKind, SurfaceKey}; use crate::style::keys::{key, latched_label, KeyRole, KeyTier, KEY_RADIUS};` and drop `ExplorerColors`.

Run: `cargo test --locked ui::topic_details 2>&1 | grep -E '^test |test result'`
Expected: `back_is_in_heading`, `save_reason_is_visible`, `history_rows_are_one_line`, `current_value_uses_content_display` and the four moved P1 tests (`topic_details_show_delete_and_source_time`, `history_names_the_scan_window_instead_of_claiming_empty`, `history_empty_reason_rules`, `history_excludes_query_replies`) ok.

- [ ] **Step e-4: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/topic_details.rs`.

### Part f (was CP-A2 T3 ⏵/⏷, T7; CP-A3 T10; CP-B T6, T8, T14; CP-C pub.submit, pub.import, pub.status, pub.import_row): Publish

**Files:**
- Modify: `src/ui/publish.rs`

**Interfaces:**
- Consumes: `crate::ui::help::{QUERYABLE_CAPTION, section}`, `help_link`, `connection_notice` (query.rs, unchanged signature), `crate::style::{glass, keys, text}`, `crate::motion`, field `import_message`.
- Produces: `pub(crate) fn preview_caption(filename: &str, shown: usize, total: usize) -> String`; `PublishUI::show_publish_tab` (unchanged signature). The local `QUERYABLE_CAPTION` constant is deleted in favour of `crate::ui::help::QUERYABLE_CAPTION`.

The Publish face gets fixed-height lines so the Publish key never moves (F-T15-10): under Key one message line (error, wildcard note or empty); under the Payload row one Import message line (a read error goes here and the payload is left unchanged); one caption line (the preview caption after an import, empty otherwise); the preview area at a fixed 120 pt; Encoding; the Publish key; one status line under it (P1's `publish_status_line`, or the not-connected notice when there is no status).

- [ ] **Step f-1: Failing tests.** Append to the tests of `src/ui/publish.rs`:

```rust
    use crate::app::probe::{Probe, WIDE};

    #[test]
    fn preview_caption_words() {
        assert_eq!(preview_caption("f.bin", 256, 1024), "Preview of f.bin (read-only, 256 bytes of 1024 bytes shown)"); // transfer::format_size wording
        assert!(preview_caption("a.txt", 12, 12).contains("12 bytes of 12 bytes"));
    }

    fn publish_key(f: &crate::app::probe::ProbeFrame) -> egui::Rect {
        f.nodes()
            .into_iter()
            .find(|n| n.role == egui::accesskit::Role::Button && n.name.starts_with("Publish"))
            .expect("the Publish key")
            .rect
    }

    #[test]
    fn publish_key_does_not_move() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let mut probe = Probe::new(WIDE);
        let render = |app: &mut ZenohExplorer, probe: &mut Probe| {
            let _ = probe.panel(app, vec![], |app, ui| app.show_publish_tab(ui));
            publish_key(&probe.panel(app, vec![], |app, ui| app.show_publish_tab(ui)))
        };
        let first = render(&mut app, &mut probe);
        let states: Vec<Box<dyn Fn(&mut ZenohExplorer)>> = vec![
            Box::new(|a| a.connection_status = ConnectionStatus::Connected),
            Box::new(|a| a.publish_status = Some(PublishStatus::Sending { key: "demo/test".into(), bytes: 12 })),
            Box::new(|a| a.publish_status = Some(PublishStatus::Published { key: "demo/test".into(), bytes: 12, at: chrono::Utc::now() })),
            Box::new(|a| a.publish_status = Some(PublishStatus::Failed("demo//x: invalid key".into()))),
            Box::new(|a| a.publish_key = "demo//x".into()),
            Box::new(|a| {
                a.publish_key = "demo/test".into();
                a.publish_payload_bytes = Some(vec![0u8; 1024]);
                a.publish_payload_filename = Some("f.bin".into());
                a.publish_payload = "00 00 00".into();
            }),
            Box::new(|a| a.import_message = Some("Could not read f.bin: permission denied".into())),
            Box::new(|a| a.connection_status = ConnectionStatus::Disconnected),
        ];
        for apply in &states {
            apply(&mut app);
            let r = render(&mut app, &mut probe);
            assert!((r.min - first.min).length() < 0.5, "the Publish key moved: {first:?} → {r:?}");
        }
    }

    #[test]
    fn imported_preview_is_read_only_label() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.publish_payload_bytes = Some(vec![0u8; 1024]);
        app.publish_payload_filename = Some("f.bin".into());
        app.publish_payload = "00 00 00".into();
        let mut probe = Probe::new(WIDE);
        let f = probe.panel(&mut app, vec![], |app, ui| app.show_publish_tab(ui));
        assert!(f.text_containing("Preview of f.bin (read-only,").is_some());
        assert!(!f.nodes().iter().any(|n| n.role == egui::accesskit::Role::MultilineTextInput), "no editable frame (F-T7-8)");
    }
```

Run: `cargo test --locked ui::publish 2>&1 | tail -n 3` — Expected: compile error for `preview_caption` (red).

- [ ] **Step f-2: Implement (was CP-A2 T7, CP-A3 T10).** Add:

```rust
/// The imported preview's caption (F-T7-8): what is shown, of how much.
pub(crate) fn preview_caption(filename: &str, shown: usize, total: usize) -> String {
    format!(
        "Preview of {filename} (read-only, {} of {} shown)",
        crate::transfer::format_size(shown),
        crate::transfer::format_size(total)
    )
}

/// Height of a status line holding only Small text (and a Small "More in Help" link).
fn small_line_h(ui: &egui::Ui) -> f32 {
    ui.text_style_height(&egui::TextStyle::Small) + 4.0
}

/// A line of constant height `h`, so the rows below it never move. Everything
/// added must fit in `h`: Small text fits `small_line_h`; a line that can hold a
/// `KeyTier::Mini` key needs `small_line_h(ui).max(KeyTier::Mini.height())`.
fn fixed_line(ui: &mut egui::Ui, h: f32, add: impl FnOnce(&mut egui::Ui)) -> egui::Rect {
    ui.allocate_ui_with_layout(egui::vec2(ui.available_width(), h), egui::Layout::left_to_right(egui::Align::Center), |ui| {
        ui.set_min_height(h);
        add(ui);
    })
    .response
    .rect
}
```

Restructure `show_publish_tab` inside `crate::style::glass::face(ui, |ui| { … })` in this order (the import reading, preview regeneration, text-edit/clear rules and the send block keep P1's code; only placement, widgets and colours change):

```rust
        let p = crate::style::p(ui);
        crate::style::glass::face(ui, |ui| {
            ui.label(crate::style::text::label("Publish data", p));
            ui.horizontal(|ui| {
                ui.label(crate::style::text::label("Key:", p));
                ui.text_edit_singleline(&mut self.publish_key);
            });
            let key_err = crate::validation::key_expr_error(&self.publish_key);
            fixed_line(ui, small_line_h(ui), |ui| {
                if let Some(err) = &key_err {
                    ui.label(crate::style::text::small(err, p).color(p.err));
                    self.help_link(ui, crate::ui::help::section::KEY_EXPRESSIONS);
                } else if let Some(note) = crate::validation::wildcard_note(&self.publish_key) {
                    ui.label(crate::style::text::small(note, p));
                }
            });
            ui.horizontal(|ui| {
                ui.label(crate::style::text::label("Payload:", p));
                let import_key = SurfaceKey::pub_import();
                let k = key(ui, "Import File", KeyTier::Standard, KeyRole::Neutral, true, self.motion.owns(&import_key));
                self.motion.surface(import_key, k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
                if k.response.clicked() {
                    if let Some(path) = rfd::FileDialog::new().pick_file() {
                        match std::fs::read(&path) {
                            Ok(bytes) => {
                                // P1's Ok arm, unchanged (filename, collapsed preview, bytes, encoding)
                                self.import_message = None;
                                self.motion.begin(ActionKind::Import, Instant::now());
                            }
                            Err(e) => {
                                // the payload is left as it was; the reason goes under Import
                                self.import_message = Some(format!("Could not read {}: {e}", path.display()));
                            }
                        }
                    }
                }
                if self.publish_payload_bytes.is_some() {
                    let c = key(ui, "Clear import", KeyTier::Compact, KeyRole::Neutral, true, false);
                    if c.response.clicked() {
                        // P1's clear body, unchanged
                    }
                }
            });
            fixed_line(ui, small_line_h(ui), |ui| {
                if let Some(msg) = &self.import_message {
                    ui.label(crate::style::text::small(msg, p).color(p.err));
                }
            });
            // caption line: always allocated; the row the import motion lands on
            let row_slot = ui.painter().add(egui::Shape::Noop);
            // tall enough for the Mini Expand/Collapse key, so a >256-byte import does not grow it
            let caption = fixed_line(ui, small_line_h(ui).max(KeyTier::Mini.height()), |ui| {
                if let (Some(name), Some(bytes)) = (&self.publish_payload_filename, &self.publish_payload_bytes) {
                    let shown = if self.publish_payload_expanded { bytes.len().min(4 * 1024) } else { bytes.len().min(256) };
                    ui.label(crate::style::text::small(preview_caption(name, shown, bytes.len()), p));
                    if bytes.len() > 256 {
                        let label = if self.publish_payload_expanded { "⏷ Collapse" } else { "⏵ Expand" };
                        if key(ui, label, KeyTier::Mini, KeyRole::Neutral, true, false).response.clicked() {
                            self.publish_payload_expanded = !self.publish_payload_expanded;
                            // P1's regeneration of the preview string, unchanged
                        }
                    }
                }
            });
            self.motion.surface(SurfaceKey::pub_import_row(), caption, ui.painter(), row_slot, 4.0);
            // preview: read-only label in a content display after an import; the editable field otherwise
            ui.allocate_ui(egui::vec2(ui.available_width(), PREVIEW_H), |ui| {
                ui.set_min_height(PREVIEW_H);
                if self.publish_payload_bytes.is_some() {
                    crate::style::glass::content_glass(ui, |ui| {
                        egui::ScrollArea::vertical().max_height(PREVIEW_H - 24.0).show(ui, |ui| {
                            ui.add(egui::Label::new(crate::style::text::content(&self.publish_payload)).wrap());
                        });
                    });
                } else {
                    egui::ScrollArea::vertical().max_height(PREVIEW_H).show(ui, |ui| {
                        ui.add(egui::TextEdit::multiline(&mut self.publish_payload).desired_width(f32::INFINITY).font(egui::TextStyle::Monospace));
                    });
                }
            });
            ui.horizontal(|ui| {
                ui.label(crate::style::text::label("Encoding:", p));
                ui.text_edit_singleline(&mut self.publish_encoding);
            });
            let pending = matches!(self.publish_status, Some(PublishStatus::Sending { .. }));
            let payload_is_empty = self.publish_payload_bytes.as_ref().map_or(self.publish_payload.is_empty(), |b| b.is_empty());
            let enabled = matches!(self.connection_status, ConnectionStatus::Connected) && key_err.is_none();
            let submit = SurfaceKey::pub_submit();
            let k = key(ui, publish_button_label(payload_is_empty, pending), KeyTier::Standard, KeyRole::Primary, enabled, self.motion.owns(&submit));
            self.motion.surface(submit, k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
            if k.response.clicked() && !pending {
                // P1's send block, unchanged, plus at the Ok arm:
                // self.motion.begin(ActionKind::Publish(self.publish_key.clone()), Instant::now());
            }
            // the status line: the Publish result, or why Publish is unavailable
            let status_slot = ui.painter().add(egui::Shape::Noop);
            let status = fixed_line(ui, small_line_h(ui), |ui| match &self.publish_status {
                Some(status) => {
                    ui.label(crate::style::text::small(publish_status_line(status), p));
                }
                None => {
                    if let Some(notice) = connection_notice(&self.connection_status) {
                        ui.label(crate::style::text::small(notice, p));
                        self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);
                    }
                }
            });
            self.motion.surface(SurfaceKey::pub_status(), status, ui.painter(), status_slot, 4.0);
        });
```

with `const PREVIEW_H: f32 = 120.0;`. The top-of-view connection notice and separator (lines 52-56) are deleted (the notice now lives in the status slot). The Queryable section becomes a second `face` with `crate::ui::help::QUERYABLE_CAPTION` in `small`, its error in `p.err`, its "Active" state word in `p.ok`, "Off: not connected"/"Inactive" in `small`. Imports: `use std::time::Instant; use crate::motion::{ActionKind, SurfaceKey}; use crate::style::keys::{key, KeyRole, KeyTier, KEY_RADIUS};`; drop `ExplorerColors` and the local `QUERYABLE_CAPTION`.

Run: `cargo test --locked ui::publish 2>&1 | grep -E '^test |test result'`
Expected: `preview_caption_words`, `publish_key_does_not_move`, `imported_preview_is_read_only_label`, `publish_status_line_words`, `publish_button_label_rules` ok.

- [ ] **Step f-3: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/publish.rs`.

### Part g (was CP-A1 T6 timeout; CP-A2 T6 query marker; CP-A3 T9; CP-B T3, T8, T14; CP-C query.submit, query.results): Query

**Files:**
- Modify: `src/ui/query.rs`

**Interfaces:**
- Consumes: `validation::{field_number, timeout_error, selector_error}`, `crate::style::{glass, content, keys, text}`, `crate::motion`, field `query_sent_at`, `help_link`.
- Produces: `pub(crate) fn pending_line(selector: &str, elapsed_s: u64, timeout_ms: u64) -> String`, `pub(crate) fn results_header(alert: Option<&str>, replies: usize) -> String`, `pub(crate) fn connection_notice` (signature unchanged; its Error text names the Connection view), `QueryUI::show_query_tab` (unchanged).

- [ ] **Step g-1: Failing tests.** In the tests of `src/ui/query.rs`, change the expected Error notice in `connection_notice_matches_state` to `"Not connected: the last connection attempt failed (see the Connection view)"`, and add:

```rust
    use crate::app::probe::{tab_event, Probe, WIDE};
    use egui::{Event, Key, Modifiers};

    #[test]
    fn pending_line_words() {
        assert_eq!(pending_line("demo/**", 3, 10_000), "Querying demo/** … 3 s (timeout 10 s)");
        assert_eq!(pending_line("a", 0, 500), "Querying a … 0 s (timeout 0.5 s)");
    }

    #[test]
    fn results_header_words() {
        assert_eq!(results_header(None, 0), "Query Results");
        assert_eq!(results_header(None, 4), "Query Results · 4 replies");
        assert_eq!(results_header(Some("Query sent for 'x'. Waiting for responses..."), 1), "Query Results · 1 reply");
        assert_eq!(results_header(Some("No replies for 'x'. No queryable matched it, or none that matched answered."), 0),
            "Query Results · No replies for 'x'. No queryable matched it, or none that matched answered.");
    }

    fn query_key(f: &crate::app::probe::ProbeFrame) -> egui::Rect {
        f.node("Query").expect("the Query key").rect
    }

    #[test]
    fn query_key_does_not_move() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.connection_status = ConnectionStatus::Connected;
        let mut probe = Probe::new(WIDE);
        let render = |app: &mut ZenohExplorer, probe: &mut Probe| {
            let _ = probe.panel(app, vec![], |app, ui| app.show_query_tab(ui));
            query_key(&probe.panel(app, vec![], |app, ui| app.show_query_tab(ui)))
        };
        let idle = render(&mut app, &mut probe);
        app.query_alert = Some("Query sent for 'demo/**'. Waiting for responses...".into());
        app.query_sent_at = Some((std::time::Instant::now(), 10_000));
        let pending = render(&mut app, &mut probe);
        app.query_alert = Some("No replies for 'demo/**'. No queryable matched it, or none that matched answered.".into());
        let verdict = render(&mut app, &mut probe);
        app.query_alert = None;
        let dismissed = render(&mut app, &mut probe);
        for r in [pending, verdict, dismissed] {
            assert!((r.min - idle.min).length() < 0.5, "Query key moved: {idle:?} → {r:?} (F-T12-7)");
        }
    }

    #[test]
    fn timeout_cannot_commit_out_of_range() {
        for (typed, want) in [("abc", 10_000u64), ("0", 100), ("9999999", 600_000)] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            let mut probe = Probe::new(WIDE);
            let key = |k: Key| Event::Key { key: k, physical_key: None, pressed: true, repeat: false, modifiers: Modifiers::NONE };
            let mut timeout = app.query_timeout.clone();
            let run = |probe: &mut Probe, app: &mut ZenohExplorer, events: Vec<Event>, t: &mut String| {
                let _ = probe.panel(app, events, |_, ui| {
                    timeout_field(ui, t);
                });
            };
            run(&mut probe, &mut app, vec![], &mut timeout);
            run(&mut probe, &mut app, vec![tab_event()], &mut timeout);
            run(&mut probe, &mut app, vec![key(Key::Backspace); 10], &mut timeout);
            run(&mut probe, &mut app, vec![Event::Text(typed.into()), key(Key::Enter)], &mut timeout);
            run(&mut probe, &mut app, vec![], &mut timeout);
            assert_eq!(crate::validation::field_number(&timeout, 0u64), want, "{typed:?}");
            assert!(crate::validation::timeout_error(&timeout).is_none());
        }
    }

    #[test]
    fn local_reply_marker_is_painted() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.messages.push_back(ZenohMessage::new_with_bytes("demo/a".into(), "1".into(), vec![], "text/plain".into(), chrono::Utc::now(), MessageType::QueryReply, true, MessageSource::LocalEcho));
        let f = Probe::new(WIDE).panel(&mut app, vec![], |app, ui| app.show_query_tab(ui));
        assert!(!f.texts().iter().any(|t| t.text == "●"), "no tofu dot (F-T13-6)");
        assert!(f.circles().iter().any(|(_, _, fill, _)| *fill == crate::colors::LIGHT.ok));
    }
```

Run: `cargo test --locked ui::query 2>&1 | tail -n 3` — Expected: compile errors (red).

- [ ] **Step g-2: Implement (was CP-A3 T9, CP-A1 T6, CP-A2 T6).**

```rust
/// The fixed pending line under the Query key (note 12).
pub(crate) fn pending_line(selector: &str, elapsed_s: u64, timeout_ms: u64) -> String {
    let timeout = if timeout_ms % 1000 == 0 {
        format!("{} s", timeout_ms / 1000)
    } else {
        format!("{} s", timeout_ms as f64 / 1000.0)
    };
    format!("Querying {selector} … {elapsed_s} s (timeout {timeout})")
}

/// The Results header: the reply count, or the verdict (P1's words), which
/// P5 T16's run header replaces later.
pub(crate) fn results_header(alert: Option<&str>, replies: usize) -> String {
    match alert {
        Some(a) if !a.starts_with("Query sent") => format!("Query Results · {a}"),
        _ => match replies {
            0 => "Query Results".to_string(),
            1 => "Query Results · 1 reply".to_string(),
            n => format!("Query Results · {n} replies"),
        },
    }
}

/// The timeout as a DragValue over P1's String field (100–600 000 ms).
pub(crate) fn timeout_field(ui: &mut egui::Ui, value: &mut String) -> egui::Response {
    let mut v: u64 = crate::validation::field_number(value, 10_000);
    let r = ui.add(
        egui::DragValue::new(&mut v)
            .range(100..=600_000)
            .suffix(" ms")
            .speed(0.0)
            .update_while_editing(false),
    );
    if r.changed() {
        *value = v.to_string();
    }
    r
}
```

`show_query_tab` becomes: the explanation and queryable summary in `small` (unchanged words); the Query Alert group (lines 45-61) is deleted; a Query face with Selector (+ error in `p.err` and a Key expressions `help_link`), Value, the "Timeout:" label with `timeout_field` (+ P1's error label kept), the Query key, and one fixed pending line under it; then a Results face whose header holds the count or verdict and a "Dismiss" key when a verdict is shown:

```rust
            let submit = SurfaceKey::query_submit();
            let enabled = matches!(self.connection_status, ConnectionStatus::Connected) && selector_err.is_none() && timeout_err.is_none();
            let k = key(ui, "Query", KeyTier::Standard, KeyRole::Primary, enabled, self.motion.owns(&submit));
            self.motion.surface(submit, k.response.rect, &k.painter, k.bevel_slot, KEY_RADIUS);
            if k.response.clicked() {
                if let Some(sender) = &self.command_sender {
                    let timeout = self.query_timeout.trim().parse().expect("validated");
                    let _ = sender.send(ZenohCommand::Query {
                        selector: self.query_selector.clone(),
                        value: self.query_value.clone(),
                        timeout_ms: timeout,
                    });
                    self.query_alert = Some(format!("Query sent for '{}'. Waiting for responses...", self.query_selector));
                    self.query_sent_at = Some((std::time::Instant::now(), timeout));
                    self.motion.begin(ActionKind::Query, std::time::Instant::now());
                }
            }
            let h = ui.text_style_height(&egui::TextStyle::Small) + 4.0;
            ui.allocate_ui(egui::vec2(ui.available_width(), h), |ui| {
                ui.set_min_height(h);
                if let (Some(a), Some((at, ms))) = (&self.query_alert, self.query_sent_at) {
                    if a.starts_with("Query sent") {
                        ui.label(crate::style::text::small(pending_line(&self.query_selector, at.elapsed().as_secs(), ms), p));
                    }
                }
            });
```

The Results face (`let results = crate::style::glass::face(ui, |ui| { … });` then `self.motion.surface(SurfaceKey::query_results(), results.rect, &results.painter, results.slot, crate::style::glass::FACE_RADIUS);`) shows `results_header(self.query_alert.as_deref(), query_replies.len())` in a `label`, and when a verdict is present a `KeyTier::Mini` "Dismiss" key that sets `self.query_alert = None`; the verdict "No replies…" gets a Troubleshooting `help_link`. Each reply card is a `content_glass` with the painted local marker (`ui.allocate_exact_size(vec2(8.0, 8.0), Sense::hover())` + `circle_filled(.., 4.0, p.ok)` + hover "From local queryable"), the time and key in `legend_text`, and the payload through `json_job` when it parses (as in part e) or `content`. The empty state is body text in `p.text_secondary` plus `small` (no italics, no `HEADING_MEDIUM_SIZE`). `connection_notice`'s Error text becomes `"Not connected: the last connection attempt failed (see the Connection view)"`. Imports: `use crate::motion::{ActionKind, SurfaceKey}; use crate::style::keys::{key, KeyRole, KeyTier, KEY_RADIUS};`; drop `ExplorerColors`.

Run: `cargo test --locked ui::query 2>&1 | grep -E '^test |test result'`
Expected: `pending_line_words`, `results_header_words`, `query_key_does_not_move`, `timeout_cannot_commit_out_of_range`, `local_reply_marker_is_painted`, `connection_notice_matches_state`, `queryable_summary_words` ok. (P5's note in `queryable_summary_words` about its expected test count is P5's to update; this part adds tests to `ui::query`.)

- [ ] **Step g-3: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/query.rs`.

### Part h (was CP-A2 T8): Help as the reference layer

**Files:**
- Modify: `src/ui/help.rs`

**Interfaces:**
- Consumes: `DetailView::{ALL, label}`, `crate::ui::connection::connect_hint`, field `help_target`.
- Produces: `HELP_SECTIONS` with a "Reading the tree" section before "Troubleshooting"; `show_help_tab` scrolls `help_target` into view once.

- [ ] **Step h-1: Failing tests.** Add to the tests of `src/ui/help.rs`:

```rust
    #[test]
    fn help_names_only_real_views() {
        let words: Vec<&str> = DetailView::ALL.iter().map(|v| v.label().split_once(' ').unwrap().1).collect();
        let text = all_text();
        let tokens: Vec<&str> = text.split_whitespace().collect();
        for w in tokens.windows(2) {
            let next = w[1].trim_matches(|c: char| !c.is_alphanumeric());
            if next == "view" || next == "tab" {
                let place = w[0].trim_matches(|c: char| !c.is_alphanumeric());
                assert!(words.contains(&place), "Help names \"{place} {next}\", which the app does not have");
            }
        }
        // The loop alone is a regression guard (P1's Help already names only "Topics view"
        // and "Publish tab"); this line is what makes the test red before h-2.
        assert!(text.contains("Connection view"), "Help sends the reader to the Connection view");
    }

    #[test]
    fn help_links_resolve() {
        let headings: Vec<&str> = HELP_SECTIONS.iter().map(|(h, _)| *h).collect();
        for s in [section::WHAT_IT_IS, section::GETTING_STARTED, section::KEY_EXPRESSIONS, section::LIMITS, section::READING_THE_TREE, section::TROUBLESHOOTING] {
            assert!(headings.contains(&s), "{s} is not a Help heading");
        }
    }

    #[test]
    fn troubleshooting_is_last() {
        // regression guard: P1's Help already ends with Troubleshooting
        assert_eq!(HELP_SECTIONS.last().unwrap().0, section::TROUBLESHOOTING);
        // red before h-2: the new section sits directly before it
        assert_eq!(HELP_SECTIONS[HELP_SECTIONS.len() - 2].0, section::READING_THE_TREE);
    }

    #[test]
    fn hints_share_help_text() {
        use crate::ui::connection::connect_hint;
        assert_eq!(connect_hint("peer", ""), CONNECT_HINT_PEER);
        assert_eq!(connect_hint("client", ""), CONNECT_HINT_CLIENT);
        let t = all_text();
        for shared in [CONNECT_HINT_PEER, CONNECT_HINT_CLIENT, QUERYABLE_CAPTION, KEY_RULE] {
            assert!(t.contains(shared), "Help and the in-place hint drift: {shared}");
        }
    }

    #[test]
    fn help_target_scrolls_into_view() {
        use crate::app::probe::Probe;
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.help_target = Some(section::TROUBLESHOOTING);
        let mut probe = Probe::new(egui::vec2(1000.0, 300.0));
        let _ = probe.panel(&mut app, vec![], |app, ui| app.show_help_tab(ui));
        let f = probe.panel(&mut app, vec![], |app, ui| app.show_help_tab(ui));
        let h = f.text(section::TROUBLESHOOTING).expect("heading");
        assert!(h.rect.min.y >= 0.0 && h.rect.max.y <= 300.0, "{:?}", h.rect);
        assert_eq!(app.help_target, None, "scrolled once");
    }
```

Run: `cargo test --locked ui::help 2>&1 | tail -n 3` — Expected: failures (red).

- [ ] **Step h-2: Implement.** Rewrite `HELP_SECTIONS` with the shared constants and the new section; keep every line P1's `help_claims_match_limits` checks against:

```rust
pub(crate) const HELP_SECTIONS: &[(&str, &[&str])] = &[
    (section::WHAT_IT_IS, &["Watch, publish and query data on a Zenoh network."]),
    (
        section::GETTING_STARTED,
        &[
            "1. Connect: fill in the Connection view, then press Connect in the header. Peer (the default) finds other peers on the local network by multicast (UDP 7446).",
            CONNECT_HINT_PEER,
            CONNECT_HINT_CLIENT,
            "   Tested with tcp and multicast; other transports are offered but untested.",
            "2. Once connected, a background ** monitor adds every key this app receives to the tree. Subscribe to Topics, above the tree, adds a subscription of your own, such as demo/**; you need one when the header says \"monitor off\".",
            "3. The topic tree on the left fills as messages arrive. Select a topic for its value and history; select a branch for a summary of what is below it.",
            "4. All Messages (Topics view with no topic selected) lists recent messages this app received or published, including query replies. New messages on paused topics (except query replies) and file chunks are not listed. The Memory, Message and Rate Limit fields open from the header's History readout.",
            "5. Publish: send text, or import a file (it is read into memory).",
            "6. Query: ask queryables for values. Results show each reply; a query with no match ends at once.",
            "7. Queryable (Publish view):",
            QUERYABLE_CAPTION,
        ],
    ),
    (
        section::KEY_EXPRESSIONS,
        &[
            "** : every key except @ admin keys",
            "demo/** : every key under demo/",
            "sensor/*/temperature : one level in the middle",
            "device/1/status : exactly this key",
            KEY_RULE,
        ],
    ),
    (
        section::LIMITS,
        &[
            "History keeps rows up to the Memory Limit (default 100 MB) and the Message Limit; older rows leave the list but stay in the tree and its counts.",
            "Messages over the Rate Limit are not listed, but the tree and Save still see them.",
            "Duplicates: the same sample seen by two sessions, or by two overlapping subscriptions, within 250 ms is listed once.",
            "Lists show the start of each value (about 200 bytes; Query Results 500); the topic's Current Value shows up to 10 KB; Save File writes all of it.",
        ],
    ),
    (
        section::READING_THE_TREE,
        &[
            "A branch's number counts the leaf topics below it; a leaf's number counts the messages it received; (n) beside a branch is its own count.",
            "The small filled dot after a name means the value was published from this app.",
            "Leaf kinds: 🛠 Zenoh admin key, 🔣 JSON value, 📝 text value, ■ binary value, 📥 file transfer (k/n chunks received).",
            "⏸ paused: new messages for that topic are not added to the lists.",
            "While filtering, the matched part of a name is underlined and branches shown only for a match below them are in lighter text.",
        ],
    ),
    (
        section::TROUBLESHOOTING,
        &[
            "Connected but the tree stays empty: check the peer count in the header (\"no peers\" means no Zenoh peer or router is linked to this app; apps in client mode that dial this app are not counted). If the header says \"monitor off\", subscribe (step 2).",
            "Connection error: the Connection view names the cause under its fields (the header shows its first words); in Client mode an address is required.",
            "A button is disabled: its reason is written beside it; invalid input is named under its field.",
            "A query says \"No replies\": no queryable matched, or those that matched had nothing to return. A timeout with no replies is shown as an error.",
            "After reconnecting, your subscriptions are re-declared automatically.",
        ],
    ),
];
```

In `show_help_tab`, the heading uses `text::heading`, section names `text::label`, lines `text::paragraph`, and each heading scrolls when it is the target:

```rust
                    let r = ui.label(crate::style::text::label(*heading, p).text_style(egui::TextStyle::Body));
                    if self.help_target == Some(*heading) {
                        r.scroll_to_me(Some(egui::Align::TOP));
                        self.help_target = None;
                    }
                    for line in *lines {
                        ui.label(crate::style::text::paragraph(*line));
                    }
```

Run: `cargo test --locked ui::help 2>&1 | grep -E '^test |test result'`
Expected: `help_names_only_real_places`, `help_claims_match_limits`, `help_names_only_real_views`, `help_links_resolve`, `troubleshooting_is_last`, `hints_share_help_text`, `help_target_scrolls_into_view` ok.

- [ ] **Step h-3: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/help.rs`.

### Part i (was CP-A3 T6 contents, T11 All Messages; CP-A1 T5's check; CP-B T5, T8; CP-C filter.list, messages.list): lists and the limits popover

**Files:**
- Modify: `src/ui/messages.rs`, `src/ui/limits.rs`, `src/ui/message_row.rs`

**Interfaces:**
- Consumes: `crate::motion::Motion::{begin, surface}`, `help_link`, `crate::style::text`.
- Produces: `LimitsUI::show_limits_controls` (unchanged signature) lays P1's limit fields out vertically and adds a Limits link. It shows neither the drop counters and memory warning (they stay in the header's notice slot) nor the reduced-motion toggle (in the header beside the theme selector); both are T2 part a's, in `src/app/header.rs`. `message_row` signatures unchanged.

- [ ] **Step i-1: Failing tests.** In `src/ui/messages.rs` tests:

```rust
    use crate::app::probe::{Probe, NARROW, WIDE};

    #[test]
    fn messages_rows_are_one_line() {
        let (mut app, _tx) = crate::app::ZenohExplorer::test_app();
        for (k, pl) in [("a/1", "p-one"), ("a/2", "p-two"), ("a/3", "p-three")] {
            let mut msg = m(k);
            msg.payload = pl.into();
            app.messages.push_back(msg);
        }
        let f = Probe::new(WIDE).panel(&mut app, vec![], |app, ui| app.show_messages_tab(ui));
        for (k, pl) in [("a/1", "p-one"), ("a/2", "p-two"), ("a/3", "p-three")] {
            let key = f.text(k).expect("key").rect;
            let payload = f.text(pl).expect("payload").rect;
            assert!((key.center().y - payload.center().y).abs() < 4.0, "{k}: key and payload on one line (F-T14-8)");
        }
        assert!(f.text("p-three").unwrap().rect.top() > f.text("p-one").unwrap().rect.top(), "newest at the bottom");
        assert!(f.text("Memory Limit (MB):").is_none(), "the limits moved to the header popover (F-T3-5)");
    }
```

In `src/ui/limits.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::probe::{Probe, NARROW};

    #[test]
    fn limits_controls_visible_at_1000x600() {
        for zoom in [1.0, 1.5] {
            let (mut app, _tx) = ZenohExplorer::test_app();
            app.rate_limit_drops = 3;
            let mut probe = Probe::new(NARROW);
            probe.set_zoom(zoom); // 1000×600 px at 150 % is about 667×400 points
            let _ = probe.panel(&mut app, vec![], |app, ui| app.show_limits_controls(ui));
            let f = probe.panel(&mut app, vec![], |app, ui| app.show_limits_controls(ui));
            let screen = probe.ctx.screen_rect();
            assert!((screen.width() - NARROW.x / zoom).abs() < 1.0, "the probe really renders at zoom {zoom}: {screen:?}");
            for label in ["Memory Limit (MB):", "Message Limit:", "Rate Limit (msg/s):", "Dedup"] {
                let t = f.text(label).unwrap_or_else(|| panic!("{label} at zoom {zoom}"));
                assert!(screen.contains_rect(t.rect), "{label} clipped at zoom {zoom}: {:?}", t.rect);
            }
            assert!(f.text_containing("not listed (rate)").is_none(), "the drop counters stay in the header's notice slot (T2 part a)");
            assert!(f.text("Reduce motion").is_none(), "the reduced-motion toggle is in the header (T2 part a)");
        }
    }
}
```

Run: `cargo test --locked -- ui::messages ui::limits 2>&1 | tail -n 3` — Expected: failures (red).

- [ ] **Step i-2: Implement.** `show_limits_controls` becomes a vertical layout (so it fits the popover at 150 %): each of the three P1 fields on its own `ui.horizontal` row (label plus field, P1's parsing and clamps unchanged, the field given `.desired_width(120.0)`), then the Dedup row with its "(n deduped)" count, then

```rust
        self.help_link(ui, crate::ui::help::section::LIMITS);
```

(the drop counters and the memory warning stay in the header's notice slot and the reduced-motion toggle sits beside the theme selector, all in T2 part a's `header.rs`; this popover holds only the limits). In `show_messages_tab`: the filter field starts `ActionKind::FilterList` on change and registers `SurfaceKey::filter_list()`; `self.show_limits_controls(ui)` is removed; the paused note and "Showing the newest …" line use `small`; the list is rendered with the shared row, newest at the bottom (P1's `filtered_tail` already returns oldest first), with the list rect registered as `SurfaceKey::messages_list()`:

```rust
        let show_type = crate::ui::message_row::types_mixed(shown.iter().copied());
        let slot = ui.painter().add(egui::Shape::Noop);
        let list = egui::ScrollArea::vertical()
            .id_salt("all_messages")
            .auto_shrink([false; 2])
            .stick_to_bottom(self.auto_scroll)
            .show(ui, |ui| {
                for message in shown {
                    crate::ui::message_row::message_row(ui, message, &now, true, show_type);
                }
            });
        self.motion.surface(SurfaceKey::messages_list(), list.inner_rect, ui.painter(), slot, 4.0);
```

In `message_row.rs`, remove the `#![allow(dead_code)] // SW-T2` line only if clippy stays clean (T3 removes it otherwise). The `Clear` key is a `KeyTier::Compact` neutral key; "Resume all" likewise.

Run: `cargo test --locked -- ui::messages ui::limits ui::message_row 2>&1 | grep -E '^test |test result'`
Expected: `messages_rows_are_one_line`, `limits_controls_visible_at_1000x600`, `filter_searches_whole_list_case_insensitively`, `paused_note_is_singular_at_one_and_bounded`, `message_row_text_order`, `types_mixed_rules` ok.

- [ ] **Step i-3: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/ui/messages.rs`, `src/ui/limits.rs`, `src/ui/message_row.rs`.

### Part j (was CP-C T1 and T2 wiring; CP-C T12 binding): commits by kind

**Files:**
- Modify: `src/events/mod.rs`; `src/motion/*` only for internal fixes (no public signature change)

**Interfaces:**
- Consumes: `crate::motion::{Commit, Failure}`, `Motion::{commit, fail, owns, is_pending}`.
- Produces: `process_events` calls the motion binding; nothing new is public.

The binding follows CP-C T2 on the events that exist on `3ce8c01` (P5 T16's run ids later replace the Query rule): Connect reveals on `MonitorConnected` (P1 T10 still sends it after `OperationFailed { op: Monitor }`); `ConnectionError` ends Connect without a reveal; Subscribe reveals on `SubscriptionCreated { key_expr }` for its key; a keyless `OperationFailed { op: Subscribe }` ends it only when it is the only pending key; Publish reveals on `Published { key }` for its key and ends on `OperationFailed { op: Publish }`; Query reveals on the first `QueryReply` message or on `QueryNoResponses` (an empty verdict is a result, F-T10-1) and ends on `OperationFailed { op: Query }`; `Disconnected` ends every pending action except Connect. `LocalEcho` never commits anything.

- [ ] **Step j-1: Failing tests.** Add to the tests of `src/events/mod.rs`:

```rust
    mod motion_binding {
        use crate::app::ZenohExplorer;
        use crate::motion::{ActionKind, SurfaceKey};
        use crate::types::*;
        use std::time::Instant;

        fn reply(key: &str) -> ZenohMessage {
            ZenohMessage::new_with_bytes(key.into(), "1".into(), vec![], "text/plain".into(), chrono::Utc::now(), MessageType::QueryReply, false, MessageSource::PublishingSession)
        }

        #[test]
        fn failed_put_never_reveals() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.connection_status = ConnectionStatus::Connected;
            app.motion.begin(ActionKind::Publish("k".into()), Instant::now());
            tx.send(ZenohEvent::OperationFailed { op: FailedOp::Publish, error: "x".into() }).unwrap();
            tx.send(ZenohEvent::Published { key: "k".into(), bytes: 1 }).unwrap();
            app.process_events();
            assert!(!app.motion.owns(&SurfaceKey::pub_status()) && !app.motion.is_pending());
        }

        #[test]
        fn stale_publish_after_tab_is_ignored() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.motion.begin(ActionKind::Publish("k".into()), Instant::now());
            app.motion.begin(ActionKind::Tab(DetailView::Help), Instant::now());
            tx.send(ZenohEvent::Published { key: "k".into(), bytes: 1 }).unwrap();
            app.process_events();
            assert!(!app.motion.owns(&SurfaceKey::pub_status()));
        }

        #[test]
        fn connection_error_ends_pending() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.motion.begin(ActionKind::Connect, Instant::now());
            tx.send(ZenohEvent::ConnectionError("refused".into())).unwrap();
            app.process_events();
            assert!(!app.motion.is_pending());
            assert!(!app.motion.owns(&SurfaceKey::header_status()));
        }

        #[test]
        fn monitor_failure_then_connected_reveals() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.motion.begin(ActionKind::Connect, Instant::now());
            tx.send(ZenohEvent::PublishingConnected).unwrap();
            tx.send(ZenohEvent::OperationFailed { op: FailedOp::Monitor, error: "x".into() }).unwrap();
            tx.send(ZenohEvent::MonitorConnected).unwrap();
            app.process_events();
            assert!(app.motion.owns(&SurfaceKey::header_status()), "the reveal lands on \"Connected · monitor off\"");
        }

        #[test]
        fn keyless_subscribe_failure_with_two_pending() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.pending_subscribes.insert("a/**".into());
            app.pending_subscribes.insert("b/**".into());
            app.motion.begin(ActionKind::Subscribe("a/**".into()), Instant::now());
            tx.send(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, error: "x".into() }).unwrap();
            app.process_events();
            assert!(app.motion.is_pending(), "a keyless failure with two keys pending ends neither");
        }

        /// The red half of the rule above: with no binding at all the action simply
        /// stays pending, so the two-pending test alone passes before this part.
        #[test]
        fn keyless_subscribe_failure_with_one_pending_ends_it() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.pending_subscribes.insert("a/**".into());
            app.motion.begin(ActionKind::Subscribe("a/**".into()), Instant::now());
            tx.send(ZenohEvent::OperationFailed { op: FailedOp::Subscribe, error: "x".into() }).unwrap();
            app.process_events();
            assert!(!app.motion.is_pending(), "the only pending key is the one that failed");
        }

        #[test]
        fn empty_query_reveals_results() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.connection_status = ConnectionStatus::Connected;
            app.motion.begin(ActionKind::Query, Instant::now());
            tx.send(ZenohEvent::QueryNoResponses { selector: "x/**".into() }).unwrap();
            app.process_events();
            assert!(app.motion.owns(&SurfaceKey::query_results()));
        }

        #[test]
        fn first_reply_reveals_results() {
            let (mut app, tx) = ZenohExplorer::test_app();
            app.connection_status = ConnectionStatus::Connected;
            app.motion.begin(ActionKind::Query, Instant::now());
            tx.send(ZenohEvent::MessageBatch(vec![reply("x/a")])).unwrap();
            app.process_events();
            assert!(app.motion.owns(&SurfaceKey::query_results()));
        }
    }
```

Run: `cargo test --locked events::tests::motion_binding 2>&1 | tail -n 3` — Expected: failures (red). (If the existing test module of `src/events/mod.rs` has another name, nest `motion_binding` inside it and adjust the filter.)

- [ ] **Step j-2: Implement.** In `process_events`, add `let now = Instant::now();` before the `for event in events` loop, and in the arms:

```rust
                ZenohEvent::MonitorConnected => {
                    info!("GUI received MonitorConnected event - fully connected");
                    self.connection_status = ConnectionStatus::Connected;
                    self.motion.commit(crate::motion::Commit::MonitorConnected, now);
                }
                ZenohEvent::Disconnected => {
                    self.motion.fail(crate::motion::Failure::SessionEnded, self.pending_subscribes.len(), now);
                    // P1's body unchanged
                }
                ZenohEvent::ConnectionError(err) => {
                    self.motion.fail(crate::motion::Failure::Connect, self.pending_subscribes.len(), now);
                    // P1's body unchanged
                }
                ZenohEvent::MessageReceived(message) => {
                    if message.message_type == MessageType::QueryReply {
                        self.motion.commit(crate::motion::Commit::QueryAnswered, now);
                    }
                    self.process_single_message(message);
                }
                ZenohEvent::MessageBatch(messages) => {
                    if messages.iter().any(|m| m.message_type == MessageType::QueryReply) {
                        self.motion.commit(crate::motion::Commit::QueryAnswered, now);
                    }
                    for message in messages {
                        self.process_single_message(message);
                    }
                }
                ZenohEvent::SubscriptionCreated { id, key_expr } => {
                    self.motion.commit(crate::motion::Commit::SubscriptionCreated(key_expr.clone()), now);
                    // P1's body unchanged
                }
                ZenohEvent::QueryNoResponses { selector } => {
                    if matches!(self.connection_status, ConnectionStatus::Connected) {
                        self.motion.commit(crate::motion::Commit::QueryAnswered, now);
                        // P1's query_alert assignment unchanged
                    }
                }
                ZenohEvent::OperationFailed { op, error } => {
                    let pending_subscribes = self.pending_subscribes.len(); // before P1 clears it
                    match op {
                        FailedOp::Query => self.motion.fail(crate::motion::Failure::Query, pending_subscribes, now),
                        FailedOp::Publish => self.motion.fail(crate::motion::Failure::Publish, pending_subscribes, now),
                        FailedOp::Subscribe => self.motion.fail(crate::motion::Failure::Subscribe, pending_subscribes, now),
                        FailedOp::Queryable | FailedOp::Monitor => {}
                    }
                    // P1's body unchanged
                }
                ZenohEvent::Published { key, bytes } => {
                    self.motion.commit(crate::motion::Commit::Published(key.clone()), now);
                    // P1's body unchanged
                }
```

Run: `cargo test --locked events:: 2>&1 | grep -E '^test |test result'`
Expected: the eight `motion_binding` tests and every existing `events::` test ok.

- [ ] **Step j-3: Part check.** `cargo clippy --all-targets --locked -- -D warnings` exits 0; `git diff --name-only "$BASE" HEAD` lists only `src/events/mod.rs` (and `src/motion/*` if a fix was needed, with no `pub fn` signature changed: `git diff "$BASE" HEAD -- src/motion | grep -E '^[-+].*pub fn'` prints nothing).

---

## Task T3: Clean-up and integration

One agent, in the main checkout, `export SWRUN=… CARGO_TARGET_DIR="$SWRUN/tgt-int"` (see "How T3 runs"). `BASE_T1` below is `$BASE1` from T1 (record it from T1's evidence).

### Stage 1 (was CP-B T17's removals, CP-B T1's contract phase, CP-C T14's helper deletion): remove the old API

**Files:** any under `src/` (clean-up only; no behaviour change).

- [ ] **Step 1: Motion colours and inks into the palette.** In `src/colors.rs` add:

```rust
/// Snow White response colours (review T9): the same in both themes.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MotionColors {
    pub base: Color32,
    pub orange: Color32,
    pub spectrum: [Color32; 4],
}

pub const MOTION: MotionColors = MotionColors {
    base: hex(0xd6d1c2),
    orange: hex(0xba7754),
    spectrum: [hex(0x8eaa6f), hex(0xd8ba6b), hex(0xc4874a), hex(0xa7563e)],
};

/// The motion's contact shadow, link recess and lip/catchlight inks, one set
/// per theme (moved from motion/spec.rs; the dark set suits the grey base).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MotionInks {
    pub contact: Color32,
    pub link_dark: Color32,
    pub link_light: Color32,
    pub lip: Color32,
}

/// T9's values, on ivory.
pub const MOTION_INKS_LIGHT: MotionInks = MotionInks {
    contact: hex(0x4e3c32),
    link_dark: hex(0x4c3b2f),
    link_light: hex(0xfffbee),
    lip: hex(0xffffff),
};

/// Neutral greys for the `DARK` palette (user decision).
pub const MOTION_INKS_DARK: MotionInks = MotionInks {
    contact: hex(0x141414),
    link_dark: hex(0x1a1a1a),
    link_light: hex(0xf2f2f2),
    lip: hex(0xdcdcdc),
};
```

and to its `palette_tests` module:

```rust
    /// User decision: the motion effect works on the grey dark base. The
    /// seated response edges stand out from the dark faces by >= 3:1, the
    /// contact shadow and link recess sit darker than every dark face by
    /// >= 1.5:1 and the link highlight lighter by >= 3:1; ivory keeps T9's inks.
    #[test]
    fn motion_inks_suit_each_base() {
        let seat = |c: Color32| c.lerp_to_gamma(MOTION.base, 0.5);
        for bg in [DARK.panel, DARK.chassis, DARK.key] {
            for c in std::iter::once(MOTION.orange).chain(MOTION.spectrum) {
                assert!(contrast_ratio(seat(c), bg) >= 3.0, "edge {c:?} on {bg:?}");
            }
            for (name, ink) in [("contact", MOTION_INKS_DARK.contact), ("link_dark", MOTION_INKS_DARK.link_dark)] {
                assert!(relative_luminance(ink) < relative_luminance(bg), "{name} is a shadow on {bg:?}");
                assert!(contrast_ratio(ink, bg) >= 1.5, "{name} reads on {bg:?}");
            }
            assert!(contrast_ratio(MOTION_INKS_DARK.link_light, bg) >= 3.0, "link highlight on {bg:?}");
        }
        assert_eq!(MOTION_INKS_LIGHT.contact, hex(0x4e3c32), "T9's contact shadow on ivory");
    }
```

(Computed with the WCAG formula before writing: the lowest edge ratio on the grey faces is 3.37:1, contact 1.67:1 and link recess 1.58:1 on the #3c3c3c chassis, link highlight 8.17:1 on the #484848 panel.)

In `src/motion/spec.rs` replace the values of `BASE`, `ORANGE` and `SPECTRUM` with `crate::colors::MOTION.base`, `.orange` and `.spectrum`; delete its `MotionInks` struct and write `pub use crate::colors::MotionInks;`, `pub const INKS_LIGHT: MotionInks = crate::colors::MOTION_INKS_LIGHT;` and `pub const INKS_DARK: MotionInks = crate::colors::MOTION_INKS_DARK;` (so `inks`, `bevel.rs`, `link.rs` and `mod.rs` keep their code); make `layer` return `crate::colors::with_alpha(c, alpha)`. Run `cargo test --locked motion:: colors::` — Expected: all ok (`seat_reproduces_t9_hex`, `motion_inks_by_theme` and `motion_inks_suit_each_base` pass).

- [ ] **Step 2: Remove the old colour API.** Delete `ExplorerColors` from `src/colors.rs`; `MessageType::color` and `ConnectionStatus::color` and the `use crate::colors::ExplorerColors; use egui::Color32;` lines from `src/types/message.rs`; `card_background_color`, `text_tertiary_color` and `animate_fade_in` from `src/app/theme.rs` (callers of `text_tertiary_color` use `text_secondary_color`); `background_color` too if nothing calls it. Delete the size constants `HEADING_LARGE_SIZE`, `HEADING_MEDIUM_SIZE`, `TEXT_SMALL_SIZE`, `TOPIC_PREVIEW_TEXT_SIZE`, `SUBSCRIPTION_TEXT_SIZE` from `src/types/mod.rs`, replacing any remaining use with the text styles (`heading`, `small`, `content`).

- [ ] **Step 3: Remove the SW-T2 allowances.** Delete every line or attribute tagged `SW-T2` (`grep -rn 'SW-T2' src`): the `#![allow(dead_code)]` of `src/style/mod.rs`, `src/motion/mod.rs`, `src/ui/message_row.rs`, `src/app/probe.rs`, and the item allowances in `src/app/mod.rs`, `src/ui/help.rs`, `src/validation.rs`, `src/colors.rs` (`Palette`, `with_alpha`), `src/app/theme.rs` (getters), `src/types/message.rs` (`DetailView::ALL`); the tags on items Step 2 deleted are already gone. Run `cargo clippy --all-targets --locked -- -D warnings`; delete each item it reports as never used (and its test, if the item existed only for T2 and no T2 part used it), and record every deletion in the commit message.

- [ ] **Step 4: Gates.**

```bash
grep -rn 'ExplorerColors' src; grep -rn 'SW-T2' src; grep -rn 'text_tertiary_color' src
grep -rn 'animate_fade_in' src; grep -rn 'animate_pulse' src; grep -rn 'HEADING_LARGE_SIZE' src
grep -rn 'TEXT_SMALL_SIZE' src; grep -rn '\.italics()' src; grep -rn 'style_mut(' src
grep -rnE 'Color32::(from_|[A-Z])' src --include='*.rs' | grep -v '^src/colors.rs:' | grep -vE 'Color32::(TRANSPARENT|PLACEHOLDER)' | grep -v '^src/motion/bevel.rs:.*from_rgba_premultiplied'
grep -rn '"✖"' src; grep -rn '✓' src/ui src/app
```

Expected: every command prints nothing. (`from_rgba_premultiplied` in `motion/bevel.rs::over` is the alpha-compositing helper the review allows.)

- [ ] **Step 5:** `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` pass. Commit (`refactor(sw-t3-1): remove the pre-Snow-White colour and size API (was CP-B T17)`).

### Stage 2 (was CP-A1 T8, CP-A2 T10, CP-A3 T13, CP-B T17, CP-C T17, T18): the integrated probe suite

**Files:** `src/app/probe.rs` (a `#[cfg(test)] mod integration` at its end). Fix-ups go to the file that caused them, in separate commits naming the T2 part.

- [ ] **Step 1: Write the suite.** Append to `src/app/probe.rs`:

```rust
#[cfg(test)]
mod integration {
    use super::*;
    use crate::app::UiAlert;
    use crate::types::*;

    fn app() -> ZenohExplorer {
        let (mut app, tx) = ZenohExplorer::test_app();
        std::mem::drop(tx);
        for p in ["demo/sensors/temp1", "demo/sensors/temp2", "demo/x"] {
            app.browse_tree.write().unwrap().insert_path(p);
        }
        app.connection_view_shown = true;
        app.detail_view = DetailView::TopicDetails;
        app
    }

    type State = (&'static str, fn(&mut ZenohExplorer));

    fn states() -> Vec<State> {
        vec![
            ("disconnected", |a| a.connection_status = ConnectionStatus::Disconnected),
            ("connecting", |a| {
                a.connection_status = ConnectionStatus::ConnectingPublishing;
                a.connect_started = Some(std::time::Instant::now());
                a.connect_target = "tcp/10.0.0.5:7447".into();
            }),
            ("connected", |a| {
                a.connection_status = ConnectionStatus::Connected;
                a.discovered_peers = 3;
            }),
            ("error", |a| a.connection_status = ConnectionStatus::Error("Could not connect in client mode: ".to_string() + &"x".repeat(200))),
            ("alert", |a| a.ui_alert = Some(UiAlert::Error("Save failed: disk full".into()))),
            ("selected", |a| a.selected_topic = Some("demo/x".into())),
            ("worker", |a| a.worker_healthy = false),
            ("memory", |a| a.current_memory_bytes = 85 * 1024 * 1024),
        ]
    }

    fn landmarks(f: &ProbeFrame) -> Vec<(String, Rect)> {
        let mut out = vec![
            ("title".to_string(), f.text("Zenoh Explorer").expect("title").rect),
            ("light".to_string(), f.text("☀ Light").expect("theme selector").rect),
            ("tree heading".to_string(), f.text("Topics").expect("tree heading").rect),
        ];
        for v in DetailView::ALL {
            out.push((v.label().to_string(), f.node(v.label()).expect("tab").rect));
        }
        let key = f
            .nodes()
            .into_iter()
            .filter(|n| n.name.starts_with("Connect") || n.name.starts_with("Disconnect"))
            .min_by(|a, b| a.rect.min.y.total_cmp(&b.rect.min.y))
            .expect("connection key");
        out.push(("connection key".to_string(), key.rect));
        out
    }

    /// CP-A3 T13: zero landmark shifts at both sizes, both themes.
    #[test]
    fn landmarks_are_stable() {
        for size in [WIDE, NARROW] {
            for dark in [false, true] {
                let mut a = app();
                a.dark_mode = dark;
                let mut probe = Probe::new(size);
                let base = landmarks(&probe.settle(&mut a));
                for (name, apply) in states() {
                    apply(&mut a);
                    let now = landmarks(&probe.settle(&mut a));
                    for ((n, r0), (_, r1)) in base.iter().zip(&now) {
                        assert!((r0.min - r1.min).length() < 0.5, "{n} moved in state {name} at {size:?} dark={dark}: {r0:?} → {r1:?}");
                    }
                }
                let f = probe.settle(&mut a);
                let faces: f32 = f.fills().iter().filter(|(_, c)| *c == crate::colors::palette(dark).panel).map(|(r, _)| r.area()).sum();
                eprintln!("work area {size:?} dark={dark}: {:.1} %", faces / (size.x * size.y) * 100.0);
            }
        }
        // the Query and Publish keys across their own states are pinned by T2 parts g and f
    }

    /// CP-A1 T8: every Tab stop on a tab, tree row, expander or key shows the ring.
    #[test]
    fn keyboard_pass_rings_every_stop() {
        for dark in [false, true] {
            let mut a = app();
            a.dark_mode = dark;
            a.connection_status = ConnectionStatus::Connected;
            let mut probe = Probe::new(WIDE);
            let _ = probe.settle(&mut a);
            let ring = crate::colors::palette(dark).focus;
            let mut stops = 0;
            let mut seen = Vec::new();
            for _ in 0..120 {
                let f = probe.tab(&mut a);
                let Some(id) = probe.focused() else { continue };
                if seen.contains(&id) {
                    break; // wrapped around
                }
                seen.push(id);
                let Some(r) = probe.ctx.read_response(id) else { continue };
                if r.sense.focusable && !r.rect.is_negative() {
                    let is_text_input = f.nodes().iter().any(|n| n.rect == r.rect && matches!(n.role, accesskit::Role::TextInput | accesskit::Role::MultilineTextInput | accesskit::Role::SpinButton));
                    let has_ring = f.strokes().iter().any(|(rect, s)| s.color == ring && rect.intersects(r.rect));
                    assert!(has_ring || is_text_input, "stop {stops} ({:?}) has no ring (dark={dark})", r.rect);
                    stops += 1;
                }
            }
            assert!(stops >= 10, "the pass reached {stops} stops");
        }
    }

    /// CP-A1 T3: every control this plan paints is at least 24×24 pt (AccessKit bounds).
    #[test]
    fn targets_are_at_least_24() {
        let mut a = app();
        a.connection_status = ConnectionStatus::Connected;
        a.subscriptions.push(Subscription { id: "1".into(), key_expr: "demo/**".into(), reliability: String::new(), mode: String::new() });
        a.payload_store.write().unwrap().insert(
            "demo/x".into(),
            PayloadEntry { bytes: b"abc".to_vec(), received_at: chrono::Utc::now(), filename: None },
        );
        a.tree_filter = "x".into();
        a.ui_alert = Some(UiAlert::Success("ok".into()));
        let mut probe = Probe::new(WIDE);
        let header = probe.settle(&mut a).text("Subscribe to Topics").expect("header").rect;
        let f = probe.click(&mut a, header.center()); // open the Subscribe group
        let nodes = f.nodes();
        let mut listed: Vec<String> = ["Clear filter", "Unsubscribe", "Dismiss", "☀ Light", "🌙 Dark", "Reduce motion", "💾", "Subscribe"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        listed.extend(DetailView::ALL.iter().map(|v| v.label().to_string()));
        for name in &listed {
            let n = nodes.iter().find(|n| &n.name == name).unwrap_or_else(|| panic!("{name} is on screen"));
            assert!(n.rect.width() >= 24.0 && n.rect.height() >= 24.0, "{name} is {:?} (F-T4-10)", n.rect.size());
        }
        let key = nodes.iter().find(|n| n.name.starts_with("Disconnect")).expect("connection key");
        assert!(key.rect.height() >= 24.0);
    }

    /// CP-B T17: installed visuals meet WCAG in both themes.
    #[test]
    fn installed_contrast_meets_wcag() {
        use crate::colors::contrast_ratio;
        for (theme, dark) in [(egui::Theme::Light, false), (egui::Theme::Dark, true)] {
            let ctx = Context::default();
            crate::style::install(&ctx);
            ctx.set_theme(theme);
            let _ = ctx.run(RawInput::default(), |_| {});
            let v = ctx.style().visuals.clone();
            let text = v.override_text_color.unwrap();
            for bg in [v.panel_fill, v.window_fill, v.extreme_bg_color] {
                assert!(contrast_ratio(text, bg) >= 4.5, "text dark={dark}");
                assert!(contrast_ratio(v.selection.stroke.color, bg) >= 3.0, "focus frame dark={dark}");
                assert!(contrast_ratio(v.widgets.inactive.bg_stroke.color, bg) >= 3.0, "input rim dark={dark}");
            }
            let p = crate::colors::palette(dark);
            let mut a = app();
            a.dark_mode = dark;
            let f = Probe::new(WIDE).settle(&mut a);
            for t in f.texts().iter().filter(|t| t.text == "Zenoh Explorer" || t.text == "Topics") {
                assert!(contrast_ratio(t.color, p.chassis).max(contrast_ratio(t.color, p.panel)) >= 4.5, "{}", t.text);
            }
        }
    }

    /// CP-B T7 / CP-A2 T10: every non-ASCII character in a UI string literal has a glyph.
    #[test]
    fn no_tofu_in_ui_strings() {
        let ctx = Context::default();
        crate::style::install(&ctx);
        let _ = ctx.run(RawInput::default(), |_| {});
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut stack = vec![root];
        while let Some(dir) = stack.pop() {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                let src = std::fs::read_to_string(&path).unwrap();
                for (n, line) in src.lines().enumerate() {
                    if line.trim_start().starts_with("//") {
                        continue;
                    }
                    for (i, part) in line.split('"').enumerate() {
                        if i % 2 == 1 {
                            for c in part.chars().filter(|c| !c.is_ascii()) {
                                let ok = ctx.fonts(|f| f.has_glyph(&egui::FontId::proportional(14.0), c));
                                assert!(ok, "{}:{}: {c:?} renders as a box", path.display(), n + 1);
                            }
                        }
                    }
                }
            }
        }
    }

    /// CP-B T11 / CP-A3 T6: nothing clips at 1000×600; the limits fit at 150 % (part i).
    #[test]
    fn no_clipping_at_1000x600_and_150_percent() {
        for dark in [false, true] {
            let mut a = app();
            a.dark_mode = dark;
            let mut probe = Probe::new(NARROW);
            let f = probe.settle(&mut a);
            let screen = Rect::from_min_size(Pos2::ZERO, NARROW);
            for (name, r) in landmarks(&f) {
                assert!(screen.contains_rect(r), "{name} clipped at 1000×600");
            }
        }
        // 150 %: crate::ui::limits::tests::limits_controls_visible_at_1000x600 covers the popover.
    }

    /// CP-C T17: the protected expander and animation time. Regression guard: it
    /// passes on the T2 tree too and exists to catch a later change.
    #[test]
    fn expander_untouched() {
        let ctx = Context::default();
        crate::style::install(&ctx);
        for theme in [egui::Theme::Light, egui::Theme::Dark] {
            ctx.set_theme(theme);
            let _ = ctx.run(RawInput::default(), |_| {});
            assert_eq!(ctx.style().animation_time, 0.001);
        }
    }

    /// CP-C T18: a tab switch reveals, settles to the latched rest, and goes idle.
    #[test]
    fn motion_reveal_matches_t9() {
        let mut a = app();
        let mut probe = Probe::new(WIDE);
        let f = probe.settle(&mut a);
        let publish = f.node(DetailView::Publish.label()).unwrap().rect;
        let f = probe.click(&mut a, publish.center());
        assert_eq!(a.detail_view, DetailView::Publish);
        assert!(f.mesh_count() >= 2, "source, result (and the link) animate");
        assert!(f.repaint_delay() < std::time::Duration::from_millis(30), "60 Hz while animating");
        std::thread::sleep(std::time::Duration::from_millis(1300));
        let _ = probe.frame(&mut a, vec![]);
        let f = probe.frame(&mut a, vec![]);
        assert!(f.mesh_count() >= 2, "latched rest state stays painted");
        assert!(f.repaint_delay() >= std::time::Duration::from_millis(900), "back to the idle tick: {:?}", f.repaint_delay());
    }

    /// User decision (CP-C T15): with the header's "Reduce motion" on, a tab
    /// switch paints its end state at once and never schedules a motion frame.
    #[test]
    fn reduced_motion_is_static_in_app() {
        let mut a = app();
        let mut probe = Probe::new(WIDE);
        let toggle = probe.settle(&mut a).node("Reduce motion").expect("header toggle").rect;
        let _ = probe.click(&mut a, toggle.center());
        assert!(a.motion.reduced());
        let publish = probe.settle(&mut a).node(DetailView::Publish.label()).unwrap().rect;
        let _ = probe.click(&mut a, publish.center());
        assert_eq!(a.detail_view, DetailView::Publish);
        let f = probe.frame(&mut a, vec![]); // one frame for egui's own post-click repaint
        assert!(f.mesh_count() >= 2, "source and result show their end state at once");
        assert!(f.repaint_delay() >= std::time::Duration::from_millis(900), "no motion frame, no timed wake-up: {:?}", f.repaint_delay());
        std::thread::sleep(std::time::Duration::from_millis(1300));
        let later = probe.frame(&mut a, vec![]);
        assert_eq!(f.mesh_count(), later.mesh_count(), "no relay appears or leaves");
    }
}
```

- [ ] **Step 2: Run.**

```bash
cargo test --locked probe::integration -- --test-threads=1 2>&1 | grep -E '^test |test result|work area'
# expander guard: brace-matched text of the function, robust to the function moving
git show "$BASE_T1":src/ui/topic_tree.rs | python3 "$SWRUN/fn_body.py" plus_minus_icon > "$SWRUN/pmi-base.txt" &&
python3 "$SWRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$SWRUN/pmi-head.txt" &&
cmp "$SWRUN/pmi-base.txt" "$SWRUN/pmi-head.txt" && echo expander-unchanged
```

Expected: `landmarks_are_stable`, `keyboard_pass_rings_every_stop`, `targets_are_at_least_24`, `installed_contrast_meets_wcag`, `no_tofu_in_ui_strings`, `no_clipping_at_1000x600_and_150_percent`, `expander_untouched`, `motion_reveal_matches_t9`, `reduced_motion_is_static_in_app` ok; four "work area" lines; `expander-unchanged` (the function's text is compared, not line numbers, so moving it is fine; both extractions must succeed). A failing test is fixed in the file that causes it (separate commit naming the T2 part), never by weakening the assertion; if a fix would change a frozen T1 interface, stop and report.

- [ ] **Step 3:** Commit (`test(sw-t3-2): integrated probe suite (was CP-A1 T8, CP-A2 T10, CP-A3 T13, CP-B T17, CP-C T17, T18)`).

### Stage 3 (was CP-C T10): motion cost in the real app

**Files:** `src/motion/bevel.rs` (an ignored timing test), `src/motion/repaint.rs` and `src/app/layout.rs` (the `ZE_MOTION_DEMO` hook), no other change.

- [ ] **Step 1: Dev-profile effect cost.** Add to `src/motion/bevel.rs` tests:

```rust
    /// CP-C T7's budget: ≤ 0.5 ms median for four surfaces (dev profile) and
    /// ≤ 0.1 ms for resting surfaces from the cache.
    #[test]
    #[ignore = "timing"]
    fn bevel_cost_dev_profile() {
        let rects = [
            Rect::from_min_max(pos2(0.0, 0.0), pos2(400.0, 700.0)),
            Rect::from_min_max(pos2(416.0, 60.0), pos2(1390.0, 850.0)),
            Rect::from_min_max(pos2(420.0, 10.0), pos2(552.0, 42.0)),
            Rect::from_min_max(pos2(20.0, 100.0), pos2(380.0, 124.0)),
        ];
        let mut animating = Vec::new();
        let mut resting = Vec::new();
        let mut cache = MeshCache::default();
        for i in 0..200 {
            let s = Sample { depth: [0.5 + i as f32 * 0.003; 4], edge: [crate::motion::spec::seat(crate::motion::spec::ORANGE); 4], animating: true };
            let t = std::time::Instant::now();
            for r in rects {
                let _ = ring_mesh(r, 8.0, &s, Profile::for_rect(r), &crate::motion::spec::INKS_LIGHT);
            }
            animating.push(t.elapsed().as_secs_f64() * 1000.0);
            let rest = peak();
            let t = std::time::Instant::now();
            for r in rects {
                let _ = cache.get(r, 8.0, &rest, Profile::for_rect(r), &crate::motion::spec::INKS_LIGHT);
            }
            resting.push(t.elapsed().as_secs_f64() * 1000.0);
        }
        animating.sort_by(f64::total_cmp);
        resting.sort_by(f64::total_cmp);
        eprintln!("animating median {:.3} ms p95 {:.3} ms; resting median {:.3} ms", animating[100], animating[190], resting[100]);
        assert!(animating[100] <= 0.5 && resting[100] <= 0.1);
    }
```

Run: `cargo test --locked bevel_cost_dev_profile -- --ignored --nocapture 2>&1 | grep -E 'median|test result'`
Expected: one "animating median … resting median …" line and `ok`. If the median is over budget, record the numbers, apply CP-C T9's fallback (60 Hz is already the cap; reduce `PER_CORNER` from 6 to 4 in `ring_mesh`), and re-run; record both runs.

- [ ] **Step 2: Real-app log.** Add an opt-in demo driver: in `frame_ui`, when `std::env::var_os("ZE_MOTION_DEMO").is_some()`, every 2 s switch `detail_view` to the next `DetailView::ALL` entry and call `self.motion.begin(ActionKind::Tab(view), now)`; in `IntervalLog::frame`, log every frame (not only animating ones) when `ZE_MOTION_DEMO` is set. Then, with the screen unlocked and the window visible:

```bash
cargo build --release --locked
ZE_MOTION_DEMO=1 ZE_MOTION_LOG="$SWRUN/motion-release.log" "$CARGO_TARGET_DIR/release/zenoh-explorer" & PID=$!; sleep 20; kill $PID
ZE_MOTION_DEMO=1 ZE_MOTION_LOG="$SWRUN/motion-dev.log" "$CARGO_TARGET_DIR/debug/zenoh-explorer" & PID=$!; sleep 20; kill $PID
python3 - "$SWRUN/motion-release.log" "$SWRUN/motion-dev.log" <<'EOF'
import re, statistics, sys
for path in sys.argv[1:]:
    rows = [dict(kv.split('=') for kv in l.split()) for l in open(path) if l.startswith('interval_ms=')]
    anim = [r for r in rows if r['animating'] == 'true']
    eff = sorted(float(r['effect_us']) / 1000 for r in anim)
    iv = sorted(float(r['interval_ms']) for r in anim[1:])
    idle = [r for r in rows if r['animating'] == 'false']
    print(path, 'effect median %.3f ms p95 %.3f ms' % (statistics.median(eff), eff[int(len(eff) * .95)]),
          'interval median %.1f ms, under 5 ms: %.0f %%' % (statistics.median(iv), 100 * sum(v < 5 for v in iv) / len(iv)),
          'idle frames: %d' % len(idle))
EOF
```

Expected: two summary lines. The idle frames between responses must come at about 1 per second (P1 T13's idle tick): check that no idle interval under 900 ms appears except right after an action. If the screen is locked or no display is available, write "unmeasured: <reason>" in the commit message instead and do not claim this done-when. Remove the `ZE_MOTION_DEMO` hook again before committing only if the user asks; otherwise keep it documented in the commit message as a diagnostic.

- [ ] **Step 3:** Commit (`perf(sw-t3-3): motion cost measured in the real app (was CP-C T10)`) with both summaries (or the reason) in the message.

### Stage 4: final verification and close

- [ ] **Step 1:** On the final tree:

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked 2>&1 | grep -E '^test result'
cargo test --locked -- --ignored 2>&1 | grep -E '^test result'      # the five network tests and the timing test; stage 3 is finished, so nothing else binds their ports
```

Expected: every command exits 0; every `test result` line is ok.

- [ ] **Step 2: Verifiers.** One read-only verifier per stage (1, 2, 3) reads its diff against this section and reports; none edits.
- [ ] **Step 3: Manual capture checklist** (optional, for the user; not a done-when): V1–V4 in ivory and in the neutral-grey dark palette at 1400×900 and 1000×600, and the header at 720×480 in both (fixed slots, truncated readouts with their hover text, no second row); a Tab pass; one tab switch with "Reduce motion" on (the end state appears at once); a Connect, Subscribe, Publish, Query and Save each, to compare with the T9/T11 strips. Recorded as "not run" unless the user runs it.
- [ ] **Step 4:** Complete T3 on the board with each stage's output as evidence.

---

## Self-review notes

- **Coverage of the review rows.** All 66 rows of CP-A1 (8), CP-A2 (10), CP-A3 (13), CP-B (17) and CP-C (18) appear in the map at the top. Dropped: CP-A1 T5 (replaced by CP-A3 T6, its check moved to T2 part i). Dissolved: CP-A1 T3 (each control is sized where it is reworded; checked in T3 stage 2). Not in this plan: CP-A3 T12 (needs P3 T12) and CP-C T14's transfer-completion half (needs P4 T9). Integration rows (CP-A1 T8, CP-A2 T10, CP-A3 T13, CP-B T17, CP-C T17, T18) are T3 stage 2 tests; CP-C T10 is T3 stage 3.
- **P-routed findings are not duplicated.** The limits stay text fields with the word "Dedup" (P3 T14); no `widget_info` is added (P3 T11); no kittest (P3 T3); no persistence (P3 T5); no query run ids (P5 T16). Where this plan and a P-plan touch the same lines, "P3, P4 and P5 absorption notes" says who keeps what.
- **Wave rule.** T1 part a runs after b and c because it consumes `Motion` and `style::install`. No T2 part calls a symbol another T2 part introduces: every cross-file call goes to a T1 symbol (frozen list in Global Constraints). The pre-declared fields in `src/app/mod.rs` exist so that no T2 part edits that file.
- **Checked against the tree at `3ce8c01`:** `src/app/layout.rs` (634 lines, header 104-268, form 272-480, banner 500-528, toolbar 530-571, panels 573-585), `src/app/theme.rs` (132), `src/colors.rs` (42), `src/app/mod.rs` (468, `test_app` at 382, `dark_mode: true` at 305), `src/ui/topic_tree.rs` (1311; `plus_minus_icon` 75-100, rows 735-940, `leaf_icon` 985-1007, `details_texts` 1108), `src/ui/messages.rs` (limits 118-153, rows 191-235), `src/ui/publish.rs`, `src/ui/query.rs`, `src/ui/help.rs`, `src/types/message.rs` (`DetailView` 205-212, `color()` 157-163, 229-237), `src/types/tree.rs` (`compute_visible_paths` 181-211), `src/events/mod.rs` (event arms 49-163, tests at 215), `src/transfer.rs::format_size` (no KB tier).
- **Checked against egui 0.29.1 sources:** `Context::{set_visuals_of (1868), style_mut_of (1838), set_theme (1771), theme (1759), enable_accesskit (3223), read_response (1181)}`, `request_repaint_after` subtracting `predicted_dt` (context.rs:187-190), `Button::{frame, min_size, fill}`, `Painter::{set, rect_stroke (3 args)}`, `Sense` public fields, `CollapsingResponse::header_response`, `popup_below_widget` with `PopupCloseBehavior`, `DragValue::{range, suffix, speed, update_while_editing}`, `Fonts::has_glyph`, `egui::accesskit` re-export (lib.rs:435) and accesskit 0.16.3 `Node::{name, role, bounds}`, `Shape` variants (`LineSegment` uses `PathStroke`), `RectShape::new` (4 args), `Theme` into `ThemePreference`.
- **Values checked numerically:** every text pair in `palette_text_pairs_meet_aa` and boundary pair in `palette_boundaries_meet_3_to_1`, the greyscale key-role separation, `latched_is_distinct_from_focus`, `badge_inks_meet_aa`, `json_tokens_meet_aa`, T3's installed-contrast pairs and the motion edges and inks on the grey base (`motion_inks_suit_each_base`) were computed with the WCAG formula for both palettes before writing, after the user's dark-mode decision replaced graphite with the neutral-grey `DARK` (all pass; the lowest `DARK` ratios are in Step b1-2's table; `DARK`'s greys, focus, status and badge inks, selection tint and the dark motion inks are derived for this plan). The glyphs in `UI_GLYPHS` were checked against the bundled fonts' cmaps with the review's `t6/glyph_coverage.py` parser (`✓`, `🧾`, `🗂` have no glyph and are not used).
- **Not compiled.** No code in this plan was built (disk rule while planning). The Global Constraint applies: the smallest compile or clippy fix consistent with the step's intent, reported as a deviation; the likeliest adjustments are small signature details (`TextShape` colour fields, `ProgressBar::fill`, `Label::sense`), none of which changes a test's meaning.
- **User decisions applied (2026-09-26).** Full adoption; neutral-grey `DARK` palette with ivory default (T1 b1, b2; T1 c inks; T3 stage 1); reduced motion fully static (T1 c2, c6; T3 stage 2); the Reduce motion toggle in the header beside the theme selector, not persisted (T2 part a; its code and test moved out of part i, whose files it no longer touches; absorption note 3); fixed header slots that fit 720 pt with truncation and hover, the notice slot for the drop counters and memory warning (T2 part a; absorption note 4 for P3 T7); the structural split (T1 part a; absorption notes 2 and 12); key heights unchanged.
- **Disk.** Ten T2 worktrees share `target/debug` by APFS clone; the batch rule (≤ 4 building, ≥ 3 GiB free, delete on completion) keeps the peak under the ~11 GiB measured free.

## Open questions for the user

Settled by the user and removed from this list: Q1 (full adoption), Q2 (dark mode: neutral greys, ivory default), Q4 (the reduced-motion toggle in the header, not persisted), Q5 (reduced motion fully static), the header width (fixed slots at 720 pt) and the structural split. They are marked "decided by the user" under "Defaults this plan assumes". The questions below keep this plan's default until answered.

1. **Q9 — connection settings.** A Connection view (selected at launch while disconnected and on each new error). Alternatives: a popover from the header, or a constant-height strip. P5 T23 later builds its profile panel in whatever place you pick.
2. **Q10 — tree click from Publish or Query.** This plan keeps today's jump to Topics (plus an immediate repaint). The alternative: stay on the view and prefill Key or Selector (append `/**` for a branch?).
3. **Q12 — link length.** 16 pt gutters (the spike's width, a readable link). Alternatives: 4 pt gutters with a stub link, or painting the link inside the source panel's padding (not built by the spike).
4. **CP-A3 T12 (editable Encoding combo)** is left out: it needs P3 T12's helpers and duplicates P5 T13's presets. Give it to P5 T13, or add it to P3 after T12?
5. **Import read errors.** Inline under Import (this plan). P3 T12 also raises a `UiAlert::Error` for a failed import, which would show in the strip too. Keep both, or inline only?
6. **Alert expiry.** Success leaves after 6 s, Warning after 10 s, Error stays until Dismiss. Other durations?
7. **Ring scope.** Rings are painted on tabs, tree rows, the expander, the Subscribe header, every painted key and the branch-summary child list. ComboBox options (Transport, Mode) keep egui's own look. Include them?
8. **Selected-row accent bar and ring geometry.** The bar uses `selected_bar` (rust #9c5539 in ivory, light grey #e0e0e0 in the neutral-grey dark palette); rings are 2 pt at a 2 pt outset, inset 1 pt on tree rows. OK?
9. **Row pitch.** Exactly 24 pt with no gap between rows (contiguous targets). Or 24 pt targets with 3 pt gaps (27 pt pitch, less dense)?
10. **Filter auto-open.** Only branches that do not match themselves open (filter `demo` shows `demo` collapsed; `temp` opens `demo` and `demo/sensors`). Or should matching be per path segment?
11. **Ports and timeout.** They stay `String` fields shown as DragValues (P3 T5 and the worker keep parsing strings), and "Timeout (ms):" becomes "Timeout:" with a " ms" suffix. Or change the fields to numbers (touches P3 T5 and P5 T22/T23)?
12. **`✓` and bold.** `✓` is replaced by `✔` (no font file added) and labels get a Label style (14 pt, secondary ink) instead of a bold face. Would you rather bundle a font with `✓` and a bold weight (a download and a licence file)?
13. **Message order.** Newest at the bottom in both All Messages and History. Or newest first in both?
14. **P3 text amendments.** Add the absorption notes (1–10, including the required P3 T5 and P3 T7 amendments of notes 3 and 4) to the P3 plan now, before it is accepted, or leave them to P3's general "keep Snow White's work" rule?
15. **Query commit without run ids.** Until P5 T16, the Query reveal binds to the first `QueryReply` message or `QueryNoResponses` while a Query is pending. A reply from an earlier, slower query could reveal a newer one. Accept until P5 T16?
16. **CP-C T10 needs your screen.** The real-app timing needs an unlocked, visible window (the `ZE_MOTION_DEMO` driver clicks nothing; it switches tabs itself). If the run happens while the screen is locked, T3 stage 3 records "unmeasured". May the agent run it when you are at the machine, and may the `ZE_MOTION_DEMO` diagnostic stay in the code?
