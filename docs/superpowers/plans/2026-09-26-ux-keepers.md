# UX Keepers Implementation Plan (five Snow White behaviours on the current look)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. T1 is one board task: a sequential Step 0 by the coordinator, then three parallel agents in git worktrees ("How T1 runs"), then one integration run. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring back five behaviours from the reverted Snow White UI, and only those, on the current (P1 + P2) look:

1. Alerts clear themselves: a Success alert after 6 s, a Warning after 10 s; an Error stays until ✖ is pressed. The frame asks egui to wake it when the alert is due, so the banner clears without input.
2. Minimum click-target size: tree rows and interactive targets are at least 24 pt tall; icon-only buttons are at least 24 × 24 pt.
3. "More in Help" links that open the Help view scrolled to the right section.
4. Filter-match highlighting and counts in the topic tree: the matched part of a row's name is highlighted and "n of m topics" is shown beside the filter. While filtering, every visible branch still opens by default, as today (P1); Snow White's "only ancestors of a match open" rule is not brought back (user decision, Open question 2).
5. On-screen reasons for disabled controls: a short line beside the disabled control (Subscribe, Save File, Publish, Enable Queryable, Key Pattern, Query, Connect, Disconnect). The existing hover texts stay.

**Architecture:**
- **Shared pieces first (Step 0, one agent, sequential).** Four files that every part reads are finished before any worktree exists: `src/app/theme.rs` (a `MIN_TARGET = 24.0` constant, `interact_size.y = MIN_TARGET` in `apply_theme`, and an `icon_button` builder), `src/app/mod.rs` (three new fields and two module declarations), a new test-only harness `src/app/headless.rs` (fixed-size `Context::run` frames, painted texts, AccessKit node bounds, clicks, repaint delay), and `src/ui/help.rs` (section-name constants, `help_link`, scroll to the target heading, and a corrected Troubleshooting line). That is all of behaviour 3's core and the base of behaviour 2.
- **Three parallel parts on disjoint files.** Part a owns `src/app/layout.rs` (behaviour 1, the header/tabs/banner/Connect targets, the Connect and Disconnect reasons, the connection-error Help link). Part b owns `src/ui/topic_tree.rs` and `src/types/tree.rs` (behaviour 4, tree rows and tree icon buttons, the Subscribe and Save File reasons, two Help links). Part c owns `src/ui/publish.rs`, `src/ui/query.rs` and `src/ui/messages.rs` (the Publish, Enable Queryable, Key Pattern and Query reasons, four Help links, and the target checks in those views).
- **No new look.** Colours come only from `src/colors.rs` (`ExplorerColors`) and the visuals `apply_theme` already installs. No palette, font, glass, key restyle, header slot, motion, tab bank or layout change. The only global style change is `interact_size.y`, which is sizing, not colour.

**Tech Stack:** Rust 1.94 locally (MSRV 1.88, checked by P2's CI), egui/eframe 0.29.1 with AccessKit 0.16.3 (already enabled through eframe's `accesskit` feature, `Cargo.toml:18`), headless `egui::Context::run`. No new dependency.

**Spec:** the user's request relayed with this plan's brief (the five behaviours above, "on the current look", keep all P1/P2 functionality); the Snow White plan `docs/superpowers/plans/2026-09-26-snow-white-ui.md` ("Local tooling and disk" for the expander guard script, "Protected expander"); the Snow White review `docs/superpowers/reviews/2026-09-24-ui-ux-snow-white-review.md` (findings F-T3-2 alert expiry, F-T4-10 24 pt targets, F-T7-3 visible reasons, F-T13-1 filter highlight; F-T13-2 default-open is not adopted). Logic and tests are adapted from these Snow White commits, read with `git show`: `49ff045` (`alert_expired`, `track_alert`, test `alert_expiry_rules`), `7e2ada1` and `99b28ef` (tree rows, tests `tree_row_pitch_is_24*`, `expander_rect_unchanged`), `8626e9d` (`targets_are_at_least_24`), `b737b16` (`help_link`, `section`), `011cc7d` (scroll to target, tests `help_target_scrolls_into_view`, `troubleshooting_is_last`), `f023a8c` (`count_filter_matches`, `match_range` and their tests; its default-open helper is not used), `7f19889` (`subscribe_blocked_reason`), `34bc309` (`save_reason_is_visible`).

**Depends on:** the current tree. HEAD is `d548051` on `bearhug-mode-test`, which restored `src/` to `f605391` (the end of P1 + P2); `git diff --stat f605391 HEAD -- src` is empty. None of the Snow White files exist (`src/style/`, `src/motion/`, `src/app/header.rs`, `src/app/probe.rs`, `src/ui/connection.rs`, `src/ui/limits.rs`, `src/ui/topic_details.rs`, `src/ui/message_row.rs`); every change here lands in the pre-Snow-White file layout.

**Decisions:** no memex decisions. Recorded user decisions that apply: `ui_alert` stays `Option<UiAlert>` (P1); the Snow White look was reverted (`d548051`) and only these five behaviours come back. **Kind of change:** UI behaviour and sizing only. No worker, protocol, dependency or colour-constant change.

## Global Constraints

- **Current look only.** No new colour constants and no edit to `src/colors.rs`. New text uses `self.text_secondary_color()` (`src/app/theme.rs:106`) at `TEXT_SMALL_SIZE` (`src/types/mod.rs:22`), exactly like the existing neutral notes (for example `src/ui/publish.rs:319-323`). The filter highlight uses `ui.visuals().selection.bg_fill` (installed by `apply_theme`, `src/app/theme.rs:50` dark and `:81` light) plus an underline in `ExplorerColors::PRIMARY` / `DARK_PRIMARY`. The Help link uses `ui.visuals().hyperlink_color`. `style.animation_time` stays `0.001` (`src/app/theme.rs:21`).
- **Protected expander.** `fn plus_minus_icon` (`src/ui/topic_tree.rs:75-100`) is not edited, and the expander's rect stays egui's `(spacing.indent, spacing.icon_width)`. Checked by the brace-matching script `$UXRUN/fn_body.py` ("Local tooling and disk") at `$BASE` and at HEAD, and by the unit test `expander_rect_unchanged` (part b). The expander is the one interactive target that stays under 24 pt (Open question 3).
- **Everything P1/P2 did stays.** Every existing test keeps passing unchanged. Existing hover texts stay (`on_disabled_hover_text` at `src/ui/topic_tree.rs:417` and `src/ui/publish.rs:345`); the new visible reason is added beside them, not instead of them. The tree's filter cache and throttle (`filter_cache_is_stale`, `filter_repaint_after`, `src/types/tree.rs:216-243`) are reused, not replaced.
- **Words.** A disabled control's reason is one short line, same row, right of the control: "Connect first", "Fix the key above", "Fix the selector above", "Fix the timeout above", "Fix the pattern above", "Fix the Port field", "Fix the Listen Port field", "Already subscribed to this key", "Subscribing…", "Available once connected", the existing Save File reasons ("No payload stored yet", "Waiting for N more chunks"), and the existing pattern lock text "Untick Enable Queryable to change the pattern". The link text is "More in Help".
- **Sizing only through plain egui.** `interact_size.y` in `apply_theme`, `Button::min_size`, and `Button::small()` for icon buttons (via `icon_button`). No custom painting for targets.
- **No `|` inside the task table cells.** No new dependency, no `Cargo.toml` change, no `--release` build, no `rustup` change.
- **Step 0 allowances.** Items Step 0 adds for the parts carry `#[allow(dead_code)] // UXK-STEP0: …`. The integration run removes every one of them; `grep -rn 'UXK-STEP0' src` must print nothing at the end.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Local tooling and disk

- Present on this Mac: `cargo` 1.94.0, `rustc` 1.94.0, `git`, `python3`, `grep`. Nothing needs installing.
- **Disk.** On 2026-09-26 the data volume had 7 GiB free (`df -g /System/Volumes/Data`) and `target/debug` is 5.7 GiB. Each part's `CARGO_TARGET_DIR` is an APFS clone (`cp -Rc`) of `target/debug`, which shares blocks until rebuilt; a part's rebuild of this crate and its test binary writes roughly 0.5–1 GiB. Before a part's first build it runs `df -g /System/Volumes/Data | awk 'NR==2{print $4}'` and waits while the free space is under 3 GiB. A part's target directory is deleted after its branch is merged and its verifier has reported.
- **Ignored tests.** The five `#[ignore = "opens network sessions"]` tests bind fixed ports. Only the integration run executes `cargo test --locked -- --ignored`, once; no part runs `--ignored`.
- **Expander guard script** (copied from the Snow White plan, "Local tooling and disk"). The coordinator writes it once in "How T1 runs" step 1. It prints `fn <name>`'s full text, from `fn` through the matching closing brace, skipping braces inside string and char literals and `//` comments, and exits non-zero if the function is missing or unbalanced; a function that moves keeps the same text:

  ```bash
  cat > "$UXRUN/fn_body.py" <<'EOF'
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
  git show "<rev>":src/ui/topic_tree.rs | python3 "$UXRUN/fn_body.py" plus_minus_icon > "$UXRUN/pmi-base.txt" &&
  python3 "$UXRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$UXRUN/pmi-head.txt" &&
  cmp "$UXRUN/pmi-base.txt" "$UXRUN/pmi-head.txt" && echo expander-unchanged
  ```

  Both extractions must succeed and the texts must be byte-identical; anything else prints no `expander-unchanged`.

- **Not checked by tests, and not claimed:** how the highlight and the link look to a person in a real window, and wall-clock repaint timing in the real app. The headless tests check the painted sections, rects, AccessKit bounds and the requested repaint delay. The T1 evidence records the real-window look as "not run".

## Review Focus

- **An alert nobody touches:** a Success must disappear by itself 6 s after it appeared and a Warning after 10 s, with no mouse or key input, while an Error stays until ✖. Pinned by part a `alert_expiry_rules`, `expiry_wakes_the_ui_when_due` (the frame requests a repaint at the due time, sooner than the 1 s idle tick) and `alerts_clear_themselves_in_the_frame`.
- **A new alert replacing an old one:** the timer restarts for the new alert, so a fresh Success is not cleared early by an old timestamp. Pinned by `alerts_clear_themselves_in_the_frame` (the replacement step). An identical alert raised again keeps its first timestamp (Open question 5).
- **Keyboard and pointer targets:** every button, selectable label, checkbox, combo box, collapsing header and "More in Help" link is at least 24 pt tall, icon-only buttons (✖, 💾, ☀/🌙) at least 24 × 24 pt, and tree rows never overlap. Pinned by Step 0 `headless_reads_texts_nodes_clicks_and_repaints`, part a `header_tabs_banner_and_connect_are_at_least_24`, part b `tree_rows_are_at_least_24` and `tree_icon_buttons_are_24_square`, part c `publish_reasons_are_visible`, `query_reason_and_links_are_visible` and `limits_link_and_controls_are_24`.
- **The protected expander:** the ＋/− icon code and its rect are unchanged. Pinned by the `fn_body.py` compare and `expander_rect_unchanged`.
- **Branches while filtering keep P1's default:** while the filter is non-empty every visible branch opens by default, including a branch whose own name matches; part b changes neither the default nor the filtered state id. Pinned by the part b regression guard `filtering_opens_every_visible_branch`.
- **Highlight is not colour alone:** the matched part carries an underline as well as the selection fill, so it still shows on a selected row (whose fill is the same selection colour). Pinned by part b `filter_shows_counts_and_highlights_the_match`.
- **Reason and state never disagree:** every Subscribe, Publish and Query state with a disabled button has a reason, and an enabled one has none. Pinned by `subscribe_reason_matches_enabled` (part b) and the `*_blocked_reason_rules` tests (parts a and c).
- **A Help link from any view lands on its section:** including links drawn before the Help view in the same frame (tree panel) and after it (detail views). Pinned by Step 0 `help_target_scrolls_into_view` and `help_link_opens_its_section`, and the click tests in parts a, b and c.
- **Merge safety:** a part that edits a file it does not own would conflict or break another part. Pinned by the ownership check (`git diff --name-only`) and the `--no-ff` merge rule in "How T1 runs".

---

## Tasks

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Merged · Step 0, then parallel parts a, b, c · owns `src/app/theme.rs`, `src/app/mod.rs`, `src/app/headless.rs` (new), `src/ui/help.rs` (Step 0); `src/app/layout.rs` (a); `src/ui/topic_tree.rs`, `src/types/tree.rs` (b); `src/ui/publish.rs`, `src/ui/query.rs`, `src/ui/messages.rs` (c)] Bring back five Snow White behaviours on the current look. Step 0 (coordinator, sequential): the 24 pt target floor in `apply_theme` and `icon_button`, three app fields, the headless test harness, and Help section names, `help_link` and scroll-to-section. a: alerts clear themselves (Success 6 s, Warning 10 s, Error stays) with a scheduled repaint, `frame_ui` split out of `update`, 24 pt header, tabs, banner ✖ and Connect/Disconnect, visible Connect and Disconnect reasons, Troubleshooting link on a connection error. b: `match_range` and `count_filter_matches` in the tree model; highlighted match, "n of m topics", default open while filtering unchanged (every visible branch, as P1); 24 pt tree rows and icon buttons; visible Subscribe and Save File reasons; Help links on the Subscribe key error and the empty tree. c: visible Publish, Enable Queryable, Key Pattern and Query reasons; Help links on key and selector errors, connection notices and the Limits row; 24 pt checks for the Publish, Query and message-list controls | — | Step 0: `cargo test --locked app::headless` and `cargo test --locked ui::help` pass (tests `headless_reads_texts_nodes_clicks_and_repaints`, `help_target_scrolls_into_view`, `help_link_opens_its_section`, `section_names_are_the_headings`, `troubleshooting_is_last`, `troubleshooting_says_reasons_are_visible`), clippy with `-D warnings` is clean, and `$UXRUN/pmi-base.txt` is written. Part a: `cargo test --locked app::layout` passes including `alert_expiry_rules`, `expiry_wakes_the_ui_when_due`, `alerts_clear_themselves_in_the_frame`, `alert_dismiss_is_24_square_and_clears`, `header_tabs_banner_and_connect_are_at_least_24`, `connect_blocked_reason_rules`, `connect_reasons_are_visible` and `connection_error_links_troubleshooting`. Part b: `cargo test --locked types::tree` and `cargo test --locked ui::topic_tree` pass including `filter_counts_leaf_topics`, `match_range_is_case_insensitive_and_char_safe`, `subscribe_reason_matches_enabled`, `subscribe_reason_is_visible`, `save_reason_is_visible`, `tree_rows_are_at_least_24`, `tree_icon_buttons_are_24_square`, `expander_rect_unchanged`, `filter_shows_counts_and_highlights_the_match`, `filtering_opens_every_visible_branch` and `tree_help_links_open_their_sections`, and the expander guard prints `expander-unchanged`. Part c: `cargo test --locked ui::publish`, `ui::query` and `ui::messages` pass including `publish_blocked_reason_rules`, `publish_reasons_are_visible`, `query_blocked_reason_rules`, `query_reason_and_links_are_visible` and `limits_link_and_controls_are_24`. For every part `git diff --name-only $BASE uxk-<part>` lists only its owned files and the three `--no-ff` merges are conflict-free. Integration on the merged tree: `grep -rn 'UXK-STEP0' src` and `grep -rn 'small_button' src` print nothing, `grep -c 'request_repaint_after' src/app/layout.rs` prints 4, the expander guard prints `expander-unchanged`, and `cargo fmt --all -- --check`, `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` and `cargo test --locked -- --ignored` pass. One read-only verifier each for Step 0 and parts a, b, c reports no open finding. The real-window look is recorded as not run |

## How the work is split

```
T1  Step 0 (coordinator, main checkout, sequential)
      theme.rs + mod.rs + headless.rs (new) + help.rs      → commit, BASE
      │
      ├── part a  src/app/layout.rs                          (alerts, header/banner/connect targets, reasons, link)
      ├── part b  src/ui/topic_tree.rs, src/types/tree.rs    (filter highlight/counts/open, rows, reasons, links)
      └── part c  src/ui/publish.rs, query.rs, messages.rs   (reasons, links, target checks)
      │
      ownership check → --no-ff merges a, b, c → drop UXK-STEP0 allowances → one integration run → 4 read-only verifiers
```

The maximum width is three agents. Step 0 is sequential because every part calls `help_link`, `icon_button` or `MIN_TARGET`, reads a field Step 0 adds, and tests with `headless.rs`.

**How T1 runs.**

1. **Start, tools and names (coordinator, main checkout).** Start T1 on the board once. Then:

   ```bash
   UXRUN=${TMPDIR:-/tmp}/uxrun
   mkdir -p "$UXRUN"
   START=$(git rev-parse HEAD)          # d548051 plus any board-only commits; record it in the evidence
   git diff --stat f605391 "$START" -- src | tail -1   # prints nothing: src/ is the pre-Snow-White tree
   ```

   Write `$UXRUN/fn_body.py` from "Local tooling and disk" and record the base expander text:

   ```bash
   git show "$START":src/ui/topic_tree.rs | python3 "$UXRUN/fn_body.py" plus_minus_icon > "$UXRUN/pmi-base.txt" && wc -l < "$UXRUN/pmi-base.txt"
   ```

   Expected: `26` (lines 75-100).

2. **Step 0 (coordinator, main checkout on `bearhug-mode-test`).** Do "Step 0" below in the main checkout with the default `target/`. It ends with one commit. Then:

   ```bash
   BASE=$(git rev-parse HEAD)   # the Step 0 commit; record it in the evidence
   ```

3. **Worktrees and target directories.** One worktree and branch per part, all from `$BASE`. All three parts compile the crate, so each target directory is seeded with an APFS clone of the main checkout's `target/debug` (which now holds the Step 0 build):

   ```bash
   for p in a b c; do
     git worktree add -b "uxk-$p" "$UXRUN/wt-$p" "$BASE"
     mkdir -p "$UXRUN/tgt-$p" && cp -Rc target/debug "$UXRUN/tgt-$p/debug"
   done
   ```

4. **Parts run in parallel**, one agent each, in `"$UXRUN/wt-<part>"` with `export UXRUN=… CARGO_TARGET_DIR="$UXRUN/tgt-<part>" BASE=…`. A part edits only its owned files, runs the disk check before its first build, runs the checks in its section (never `--ignored`, never `--release`), and commits only its owned files on its branch. Each part's section lists its commits.

5. **Ownership check**, in the main checkout, for each part:

   ```bash
   for p in a b c; do echo "== $p"; git diff --name-only "$BASE" "uxk-$p"; done
   ```

   Expected, exactly: part a `src/app/layout.rs`; part b `src/types/tree.rs`, `src/ui/topic_tree.rs`; part c `src/ui/messages.rs`, `src/ui/publish.rs`, `src/ui/query.rs`. Anything else: stop and report.

6. **Merge**, in the main checkout on `bearhug-mode-test`, in the order a, b, c:

   ```bash
   for p in a b c; do
     git merge --no-ff "uxk-$p" -m "merge(uxk-t1): part $p

   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" || { echo "conflict in part $p"; break; }
   done
   ```

   Owned files are disjoint, so every merge is conflict-free. A conflict means a part edited a file it does not own: stop and report it.

7. **Drop the Step 0 allowances (coordinator).** Every `UXK-STEP0` item now has a caller. Delete each `#[allow(dead_code)] // UXK-STEP0: …` line (three fields or functions: `ui_alert_since` and `tree_filter_counts` in `src/app/mod.rs`, `icon_button` in `src/app/theme.rs`, `help_link` in `src/ui/help.rs`; four lines in all):

   ```bash
   grep -rn 'UXK-STEP0' src          # before: 4 lines
   ```

   Remove them with the Edit tool, then commit:

   ```bash
   git add src/app/mod.rs src/app/theme.rs src/ui/help.rs
   git commit -m "chore(uxk-t1): drop the Step 0 dead-code allowances

   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   ```

8. **One integration run** on the merged tree, in the main checkout:

   ```bash
   export CARGO_TARGET_DIR="$UXRUN/tgt-int"
   [ -d "$CARGO_TARGET_DIR" ] || { mkdir -p "$CARGO_TARGET_DIR" && cp -Rc "$UXRUN/tgt-b/debug" "$CARGO_TARGET_DIR/debug"; }
   grep -rn 'UXK-STEP0' src; grep -rn 'small_button' src; echo greps-done
   grep -c 'request_repaint_after' src/app/layout.rs
   python3 "$UXRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$UXRUN/pmi-head.txt" &&
     cmp "$UXRUN/pmi-base.txt" "$UXRUN/pmi-head.txt" && echo expander-unchanged
   cargo fmt --all -- --check
   cargo clippy --all-targets --locked -- -D warnings
   cargo test --locked
   cargo test --locked -- --ignored
   ```

   Expected: the two greps print nothing before `greps-done`; `4`; `expander-unchanged`; fmt, clippy and both test runs exit 0 (`test result: ok` on every binary). A failure is fixed by the coordinator in the file that caused it, in a separate commit whose message names the owning part.

9. **Verify and close.** Four read-only verifiers (Step 0, a, b, c) each read `git diff "$BASE" uxk-<part>` (for Step 0: `git diff "$START" "$BASE"`) against that section of this plan and report findings; they edit nothing. The coordinator fixes a finding in the owning file in a separate commit naming the part, then re-runs step 8. Then complete T1 on the board with the evidence: `$START`, `$BASE`, each part's test output, the ownership-check output, the merge commits, the integration output, the verifier verdicts, and "real-window look: not run". Remove the worktrees, branches and part target directories:

   ```bash
   for p in a b c; do git worktree remove "$UXRUN/wt-$p" && git branch -d "uxk-$p" && rm -rf "$UXRUN/tgt-$p"; done
   ```

**Coupling checked.** No part calls a function another part adds: part a uses only Step 0 items and its own functions; part b uses Step 0 items, `src/types/tree.rs` (its own) and its own functions; part c uses Step 0 items and `connection_notice` (`src/ui/query.rs:215`, its own). The one cross-part runtime effect is that part a's full-frame tests draw the tree and detail panels of `$BASE`, which is why every part-a assertion targets only controls in `src/app/layout.rs`. The one widget name shared across parts is "More in Help" (parts b and c also draw it in the same full frame, and `nodes` iterates a hash map), so `connection_error_links_troubleshooting` picks the link nearest below the error text and asserts `help_target == Some(section::TROUBLESHOOTING)` rather than taking the first name match.

**File-ownership matrix.**

| File | Owner | Why no conflict |
|---|---|---|
| `src/app/theme.rs` | Step 0 (allowance line removed in step 7) | sequential, before worktrees |
| `src/app/mod.rs` | Step 0 (allowance lines removed in step 7) | sequential, before worktrees |
| `src/app/headless.rs` (new, test-only) | Step 0 | sequential, before worktrees |
| `src/ui/help.rs` | Step 0 (allowance line removed in step 7) | sequential, before worktrees |
| `src/app/layout.rs` | part a | single owner |
| `src/ui/topic_tree.rs` | part b | single owner |
| `src/types/tree.rs` | part b | single owner |
| `src/ui/publish.rs` | part c | single owner |
| `src/ui/query.rs` | part c | single owner |
| `src/ui/messages.rs` | part c | single owner |
| any of the above (fix-ups) | coordinator, steps 8–9 | after all merges, named per part |

---

## Task T1: Five Snow White behaviours on the current look

Owns, per part: see the task table. Worktrees, merges and integration are under "How T1 runs".

### Step 0 (coordinator, sequential): target floor, fields, headless harness, Help links

**Files:**
- Modify: `src/app/theme.rs` (add `MIN_TARGET` and `icon_button`; one line in `apply_theme` after `style.animation_time = 0.001;` at line 21)
- Modify: `src/app/mod.rs` (lines 3-4 module declarations; three fields after `pending_subscribes` at line 227; three initialisers after `pending_subscribes: HashSet::new(),` at line 345)
- Create: `src/app/headless.rs` (test-only)
- Modify: `src/ui/help.rs` (headings at lines 12, 16, 30, 40, 49 become `section::` constants; the Troubleshooting line at 53; `show_help_tab` at 68-89; new `help_link`; tests at 92-134)

**Interfaces (Produces, frozen for the parts):**
- `crate::app::theme::MIN_TARGET: f32` = `24.0`.
- `crate::app::theme::icon_button<'a>(text: impl Into<egui::WidgetText>) -> egui::Button<'a>`: `Button::new(text).small().min_size(vec2(MIN_TARGET, MIN_TARGET))`.
- `apply_theme` sets `style.spacing.interact_size.y = MIN_TARGET` in both themes.
- `ZenohExplorer` fields: `ui_alert_since: Option<(UiAlert, Instant)>`, `help_target: Option<&'static str>`, `tree_filter_counts: Option<(usize, usize)>`, all `None` in `new`.
- `crate::ui::help::section::{WHAT_IT_IS, GETTING_STARTED, KEY_EXPRESSIONS, LIMITS, TROUBLESHOOTING}: &str`.
- `ZenohExplorer::help_link(&mut self, ui: &mut egui::Ui, section: &'static str) -> egui::Response`: draws "More in Help" (link colour, underlined, small), at least `MIN_TARGET` tall; a click sets `detail_view = DetailView::Help`, `help_target = Some(section)` and requests a repaint.
- `show_help_tab` scrolls the heading named by `help_target` to the top of the Help scroll area, then clears `help_target`.
- `crate::app::headless` (cfg(test)): `WIDE`, `PanelFn`, `Headless::{new, run, panel, click_panel}`, `click_events`, `text_shapes`, `texts`, `text`, `nodes`, `node`, `repaint_delay`, `Painted { text, rect }`, `Node { name, rect }`.

- [ ] **Step 1: Write the harness and its test first** (red: `MIN_TARGET` does not exist yet).

Change `src/app/mod.rs:3-4` from

```rust
mod layout;
mod theme;
```

to

```rust
#[cfg(test)]
pub(crate) mod headless;
mod layout;
pub(crate) mod theme;
```

Create `src/app/headless.rs`:

```rust
//! Headless egui frames for tests: a fixed screen, the texts painted with
//! their rects, and AccessKit nodes with their bounds (no window, no GPU).
//! Replaces nothing: P1's `details_texts` helper keeps working as it is.

use egui::epaint::TextShape;
use egui::{
    pos2, Context, Event, FullOutput, Modifiers, PointerButton, Pos2, RawInput, Rect, Shape, Vec2,
};
use std::time::Duration;

use crate::app::ZenohExplorer;

/// A 1400 × 900 pt window.
pub(crate) const WIDE: Vec2 = egui::vec2(1400.0, 900.0);

/// A panel body under test, such as `|a, ui| a.show_tree_panel(ui)`.
pub(crate) type PanelFn = fn(&mut ZenohExplorer, &mut egui::Ui);

/// A text the frame painted, with its rect in points.
#[derive(Debug, Clone)]
pub(crate) struct Painted {
    pub text: String,
    pub rect: Rect,
}

/// An AccessKit node with a name and bounds (points; pixels_per_point is 1).
#[derive(Debug, Clone)]
pub(crate) struct Node {
    pub name: String,
    pub rect: Rect,
}

/// One egui context driven frame by frame at a fixed window size.
pub(crate) struct Headless {
    pub ctx: Context,
    size: Vec2,
}

impl Headless {
    pub fn new(size: Vec2) -> Self {
        let ctx = Context::default();
        ctx.enable_accesskit();
        Self { ctx, size }
    }

    /// One frame of `f` in the window.
    pub fn run(&self, events: Vec<Event>, f: impl FnMut(&Context)) -> FullOutput {
        let input = RawInput {
            screen_rect: Some(Rect::from_min_size(Pos2::ZERO, self.size)),
            events,
            ..Default::default()
        };
        self.ctx.run(input, f)
    }

    /// One frame of `f` inside a CentralPanel, with the app's theme applied.
    pub fn panel(&self, app: &mut ZenohExplorer, events: Vec<Event>, f: PanelFn) -> FullOutput {
        self.run(events, |ctx| {
            app.apply_theme(ctx);
            egui::CentralPanel::default().show(ctx, |ui| f(app, ui));
        })
    }

    /// A primary click at `at` (press frame, release frame), then a settled frame.
    pub fn click_panel(&self, app: &mut ZenohExplorer, at: Pos2, f: PanelFn) -> FullOutput {
        let (press, release) = click_events(at);
        let _ = self.panel(app, press, f);
        let _ = self.panel(app, release, f);
        self.panel(app, Vec::new(), f)
    }
}

/// Press events and release events for a primary click at `at`.
pub(crate) fn click_events(at: Pos2) -> (Vec<Event>, Vec<Event>) {
    let press = vec![
        Event::PointerMoved(at),
        Event::PointerButton {
            pos: at,
            button: PointerButton::Primary,
            pressed: true,
            modifiers: Modifiers::NONE,
        },
    ];
    let release = vec![Event::PointerButton {
        pos: at,
        button: PointerButton::Primary,
        pressed: false,
        modifiers: Modifiers::NONE,
    }];
    (press, release)
}

fn walk<'a>(shape: &'a Shape, out: &mut Vec<&'a TextShape>) {
    match shape {
        Shape::Vec(v) => v.iter().for_each(|s| walk(s, out)),
        Shape::Text(t) => out.push(t),
        _ => {}
    }
}

/// Every text shape the frame painted (galleys keep their layout sections).
pub(crate) fn text_shapes(out: &FullOutput) -> Vec<&TextShape> {
    let mut v = Vec::new();
    for clipped in &out.shapes {
        walk(&clipped.shape, &mut v);
    }
    v
}

pub(crate) fn texts(out: &FullOutput) -> Vec<Painted> {
    text_shapes(out)
        .into_iter()
        .map(|t| Painted {
            text: t.galley.text().to_string(),
            rect: Rect::from_min_size(t.pos, t.galley.size()),
        })
        .collect()
}

pub(crate) fn text(out: &FullOutput, exact: &str) -> Option<Painted> {
    texts(out).into_iter().find(|t| t.text == exact)
}

pub(crate) fn nodes(out: &FullOutput) -> Vec<Node> {
    let Some(update) = &out.platform_output.accesskit_update else {
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
                rect: Rect::from_min_max(
                    pos2(b.x0 as f32, b.y0 as f32),
                    pos2(b.x1 as f32, b.y1 as f32),
                ),
            })
        })
        .collect()
}

pub(crate) fn node(out: &FullOutput, name: &str) -> Option<Node> {
    nodes(out).into_iter().find(|n| n.name == name)
}

/// How long egui was asked to wait before the next frame.
pub(crate) fn repaint_delay(out: &FullOutput) -> Duration {
    out.viewport_output[&egui::ViewportId::ROOT].repaint_delay
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::theme::MIN_TARGET;

    #[test]
    fn headless_reads_texts_nodes_clicks_and_repaints() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let show: PanelFn = |a, ui| {
            ui.label("Hello");
            if ui.button("Hi").clicked() {
                a.tree_filter.push('x');
            }
            ui.ctx().request_repaint_after(Duration::from_millis(250));
        };
        let out = h.panel(&mut app, Vec::new(), show);
        assert!(text(&out, "Hello").is_some(), "{:?}", texts(&out));
        assert!(!text_shapes(&out).is_empty());
        let hi = node(&out, "Hi").expect("the button's AccessKit node");
        assert!(
            hi.rect.height() >= MIN_TARGET,
            "apply_theme makes a button {MIN_TARGET} pt tall: {:?}",
            hi.rect
        );
        assert!(repaint_delay(&out) <= Duration::from_millis(250));
        let _ = h.click_panel(&mut app, hi.rect.center(), show);
        assert_eq!(app.tree_filter, "x", "the click reached the button");
        assert!(nodes(&out).len() >= 2);
    }
}
```

Run: `cargo test --locked app::headless 2>&1 | tail -5`
Expected: compile error `cannot find value MIN_TARGET in module crate::app::theme` (or `unresolved import`).

- [ ] **Step 2: The 24 pt floor in `apply_theme`.** In `src/app/theme.rs`, after the `use` lines (line 7), add:

```rust
/// The smallest click target, in points (WCAG 2.2 SC 2.5.8, Snow White F-T4-10).
pub(crate) const MIN_TARGET: f32 = 24.0;

/// An icon-only or small button with a `MIN_TARGET` square hit area and egui's
/// small padding; its look is egui's normal button.
#[allow(dead_code)] // UXK-STEP0: called by parts a, b and c
pub(crate) fn icon_button<'a>(text: impl Into<egui::WidgetText>) -> egui::Button<'a> {
    egui::Button::new(text)
        .small()
        .min_size(egui::vec2(MIN_TARGET, MIN_TARGET))
}
```

and in `apply_theme`, directly after `style.animation_time = 0.001;` (line 21):

```rust
            // Buttons, selectable labels (tabs, tree rows), checkboxes, combo
            // boxes and collapsing headers are at least MIN_TARGET tall.
            style.spacing.interact_size.y = MIN_TARGET;
```

Run: `cargo test --locked app::headless 2>&1 | tail -3`
Expected: `test app::headless::tests::headless_reads_texts_nodes_clicks_and_repaints ... ok`, `test result: ok. 1 passed`.

- [ ] **Step 3: Three fields.** In `src/app/mod.rs`, after `pub(crate) pending_subscribes: HashSet<String>,` (line 227):

```rust
    /// The alert `ui_alert` held when it was first seen, and when; the
    /// banner's expiry reads it (Success 6 s, Warning 10 s).
    #[allow(dead_code)] // UXK-STEP0: read by part a
    pub(crate) ui_alert_since: Option<(UiAlert, Instant)>,
    /// A Help heading to scroll into view once; set by `help_link`.
    pub(crate) help_target: Option<&'static str>,
    /// (leaf topics whose path matches the filter, all leaf topics), computed
    /// with the filter cache; None when not filtering.
    #[allow(dead_code)] // UXK-STEP0: read by part b
    pub(crate) tree_filter_counts: Option<(usize, usize)>,
```

and after `pending_subscribes: HashSet::new(),` (line 345):

```rust
            ui_alert_since: None,
            help_target: None,
            tree_filter_counts: None,
```

- [ ] **Step 4: Help tests first** (red). Replace the tests module of `src/ui/help.rs` (lines 92-134) with the existing two tests unchanged plus:

```rust
    #[test]
    fn section_names_are_the_headings() {
        let headings: Vec<&str> = HELP_SECTIONS.iter().map(|(h, _)| *h).collect();
        assert_eq!(
            headings,
            [
                section::WHAT_IT_IS,
                section::GETTING_STARTED,
                section::KEY_EXPRESSIONS,
                section::LIMITS,
                section::TROUBLESHOOTING,
            ]
        );
    }

    #[test]
    fn troubleshooting_is_last() {
        // regression guard: P1's Help already ends with Troubleshooting
        assert_eq!(HELP_SECTIONS.last().unwrap().0, section::TROUBLESHOOTING);
    }

    #[test]
    fn troubleshooting_says_reasons_are_visible() {
        let t = all_text();
        assert!(!t.contains("hover says why"), "reasons are no longer hover-only");
        assert!(t.contains("the reason is written beside it"));
    }

    fn help_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_help_tab(ui)
    }

    #[test]
    fn help_target_scrolls_into_view() {
        use crate::app::headless::{text, Headless};
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(egui::vec2(1000.0, 300.0));
        let out = h.panel(&mut app, vec![], help_panel);
        assert!(
            text(&out, section::TROUBLESHOOTING).is_none(),
            "precondition: Troubleshooting is below a 300 pt window"
        );
        app.help_target = Some(section::TROUBLESHOOTING);
        let _ = h.panel(&mut app, vec![], help_panel);
        let out = h.panel(&mut app, vec![], help_panel);
        let heading = text(&out, section::TROUBLESHOOTING).expect("heading in view");
        assert!(
            heading.rect.min.y >= 0.0 && heading.rect.max.y <= 300.0,
            "{:?}",
            heading.rect
        );
        assert_eq!(app.help_target, None, "scrolled once");
    }

    #[test]
    fn help_link_opens_its_section() {
        use crate::app::headless::{node, Headless, PanelFn, WIDE};
        use crate::app::theme::MIN_TARGET;
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let show: PanelFn = |a, ui| {
            a.help_link(ui, section::LIMITS);
        };
        let out = h.panel(&mut app, vec![], show);
        let link = node(&out, "More in Help").expect("link");
        assert!(link.rect.height() >= MIN_TARGET, "{:?}", link.rect);
        let _ = h.click_panel(&mut app, link.rect.center(), show);
        assert_eq!(app.detail_view, DetailView::Help);
        assert_eq!(app.help_target, Some(section::LIMITS));
    }
```

Run: `cargo test --locked ui::help 2>&1 | grep -E '^error|test result'`
Expected: compile errors naming `section` and `help_link`.

- [ ] **Step 5: Section names, link and scroll.** In `src/ui/help.rs`, after the `use` lines (line 6), add:

```rust
/// Help headings, the targets of `help_link`.
pub(crate) mod section {
    pub const WHAT_IT_IS: &str = "What it is";
    pub const GETTING_STARTED: &str = "Getting started";
    pub const KEY_EXPRESSIONS: &str = "Key expressions";
    pub const LIMITS: &str = "Limits";
    pub const TROUBLESHOOTING: &str = "Troubleshooting";
}

impl ZenohExplorer {
    /// A small "More in Help" link that opens the Help view at `section`. A
    /// frameless button, so its hit area is `MIN_TARGET` tall.
    #[allow(dead_code)] // UXK-STEP0: called by parts a, b and c
    pub(crate) fn help_link(&mut self, ui: &mut egui::Ui, section: &'static str) -> egui::Response {
        let text = RichText::new("More in Help")
            .size(TEXT_SMALL_SIZE)
            .underline()
            .color(ui.visuals().hyperlink_color);
        let response = ui
            .add(
                egui::Button::new(text)
                    .frame(false)
                    .min_size(egui::vec2(0.0, crate::app::theme::MIN_TARGET)),
            )
            .on_hover_cursor(egui::CursorIcon::PointingHand)
            .on_hover_text(format!("Help: {section}"));
        if response.clicked() {
            self.detail_view = DetailView::Help;
            self.help_target = Some(section);
            // A link in a view drawn after Help this frame still lands next frame.
            ui.ctx().request_repaint();
        }
        response
    }
}
```

Replace the five heading strings in `HELP_SECTIONS` (lines 12, 16, 30, 40, 49) with `section::WHAT_IT_IS`, `section::GETTING_STARTED`, `section::KEY_EXPRESSIONS`, `section::LIMITS` and `section::TROUBLESHOOTING` (same text, so the Help view reads the same). Replace line 53 with:

```rust
            "A button is disabled: the reason is written beside it; invalid input is also named under its field.",
```

In `show_help_tab` (lines 75-88) make the scroll area non-animated and scroll to the target:

```rust
        egui::ScrollArea::vertical()
            .id_salt("help")
            .auto_shrink([false; 2])
            // A Help link jumps, it does not glide: egui 0.29.1 applies an
            // animated scroll target a frame late (Snow White 011cc7d).
            .animated(false)
            .show(ui, |ui| {
                for (i, (heading, lines)) in HELP_SECTIONS.iter().enumerate() {
                    if i > 0 {
                        ui.separator();
                    }
                    let r = ui.label(RichText::new(*heading).strong());
                    if self.help_target == Some(*heading) {
                        r.scroll_to_me(Some(egui::Align::TOP));
                    }
                    for line in *lines {
                        ui.label(*line);
                    }
                }
            });
        // Scrolled once; a target that names no heading is dropped too.
        self.help_target = None;
```

Run: `cargo test --locked ui::help 2>&1 | grep -E '^test |test result'`
Expected: 8 tests `ok` (`help_names_only_real_places`, `help_claims_match_limits` and the six new ones), `test result: ok. 8 passed`. If `help_target_scrolls_into_view` fails its precondition because Help fits in 300 pt, lower the window height to 200 pt and record that.

- [ ] **Step 6: Checks and commit.**

```bash
cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked 2>&1 | grep -E 'test result|FAILED'
grep -rn 'UXK-STEP0' src | wc -l      # 4
git add src/app/headless.rs src/app/mod.rs src/app/theme.rs src/ui/help.rs
git commit -m "feat(uxk-t1-step0): 24 pt target floor, headless harness, Help section links

MIN_TARGET and icon_button in theme.rs; three app fields for parts a and b;
section names, help_link and scroll-to-section in help.rs.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: fmt and clippy silent, every `test result: ok`, no `FAILED`; `4`.

---

### Part a: alerts that clear themselves, header and connection targets, Connect/Disconnect reasons

**Files:**
- Modify: `src/app/layout.rs` only. Anchors: imports lines 3-14; `update` lines 70-591; header theme button line 114; connection error line 406-408; Connect button lines 424-477; Disconnect lines 481-497; alert banner lines 500-528; tabs lines 531-570; idle repaint line 590; tests lines 594-634.

**Interfaces:**
- Consumes (Step 0): `MIN_TARGET`, `icon_button`, `ui_alert_since`, `help_link`, `section::TROUBLESHOOTING`, `crate::app::headless`.
- Produces: `ZenohExplorer::frame_ui(&mut self, ctx: &egui::Context)` (one whole frame; `update` calls it); `fn expire_alert(&mut self, ctx: &egui::Context, now: Instant)`; `fn show_alert_banner(&mut self, ui: &mut egui::Ui)`; `pub(crate) fn alert_lifetime(&UiAlert) -> Option<Duration>`; `pub(crate) fn alert_time_left(&UiAlert, Duration) -> Option<Duration>`; `fn connect_blocked_reason(mode, address, port, listen_port) -> Option<&'static str>`; `fn disconnect_blocked_reason(&ConnectionStatus) -> Option<&'static str>`.

- [ ] **Step 1: Tests first** (red). Append to the tests module of `src/app/layout.rs` (before its closing brace at line 634):

```rust
    use crate::app::headless::{click_events, node, nodes, repaint_delay, text, Headless, WIDE};
    use crate::app::theme::MIN_TARGET;
    use crate::ui::help::section;

    fn frame(h: &Headless, app: &mut ZenohExplorer) -> egui::FullOutput {
        h.run(Vec::new(), |ctx| app.frame_ui(ctx))
    }

    fn ago(d: Duration) -> Instant {
        Instant::now().checked_sub(d).expect("uptime")
    }

    #[test]
    fn alert_expiry_rules() {
        let s = Duration::from_secs;
        let left = |a: UiAlert, age| alert_time_left(&a, s(age));
        assert_eq!(left(UiAlert::Success("x".into()), 5), Some(s(1)));
        assert_eq!(left(UiAlert::Success("x".into()), 6), Some(Duration::ZERO));
        assert_eq!(left(UiAlert::Warning("x".into()), 9), Some(s(1)));
        assert_eq!(left(UiAlert::Warning("x".into()), 10), Some(Duration::ZERO));
        assert_eq!(left(UiAlert::Error("x".into()), 3600), None, "errors stay until ✖");
    }

    #[test]
    fn expiry_wakes_the_ui_when_due() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let ctx = egui::Context::default();
        let _ = ctx.run(egui::RawInput::default(), |_| {}); // settle egui's start-up repaint
        let now = Instant::now();
        let ok = UiAlert::Success("ok".into());
        app.ui_alert = Some(ok.clone());
        app.ui_alert_since = Some((ok, now - Duration::from_millis(5_500)));
        let out = ctx.run(egui::RawInput::default(), |ctx| app.expire_alert(ctx, now));
        let d = repaint_delay(&out);
        assert!(
            d <= Duration::from_millis(500) && d >= Duration::from_millis(450),
            "woken when the 6 s are up, not at the 1 s idle tick: {d:?}"
        );
        app.ui_alert = Some(UiAlert::Error("e".into()));
        let out = ctx.run(egui::RawInput::default(), |ctx| app.expire_alert(ctx, now));
        assert!(repaint_delay(&out) > Duration::from_secs(3600), "an Error schedules nothing");
    }

    #[test]
    fn alerts_clear_themselves_in_the_frame() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let saved = UiAlert::Success("Saved to /tmp/x".into());
        app.ui_alert = Some(saved.clone());
        let out = frame(&h, &mut app);
        assert!(text(&out, "Saved to /tmp/x").is_some());
        app.ui_alert_since = Some((saved.clone(), ago(Duration::from_secs(6))));
        let out = frame(&h, &mut app);
        assert!(app.ui_alert.is_none(), "a Success leaves after 6 s");
        assert!(text(&out, "Saved to /tmp/x").is_none());

        let warn = UiAlert::Warning("w".into());
        app.ui_alert = Some(warn.clone());
        app.ui_alert_since = Some((warn.clone(), ago(Duration::from_secs(9))));
        let _ = frame(&h, &mut app);
        assert!(app.ui_alert.is_some(), "a Warning stays 10 s");
        app.ui_alert_since = Some((warn.clone(), ago(Duration::from_secs(10))));
        let _ = frame(&h, &mut app);
        assert!(app.ui_alert.is_none(), "a Warning leaves after 10 s");

        // A new alert replacing an old one restarts the clock.
        app.ui_alert = Some(UiAlert::Success("new".into()));
        app.ui_alert_since = Some((saved, ago(Duration::from_secs(60))));
        let out = frame(&h, &mut app);
        assert!(text(&out, "new").is_some(), "the old timestamp does not apply");

        let err = UiAlert::Error("e".into());
        app.ui_alert = Some(err.clone());
        app.ui_alert_since = Some((err, ago(Duration::from_secs(3600))));
        let out = frame(&h, &mut app);
        assert!(text(&out, "Error: e").is_some(), "an Error stays until ✖");
    }

    #[test]
    fn alert_dismiss_is_24_square_and_clears() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        app.ui_alert = Some(UiAlert::Error("x".into()));
        let _ = frame(&h, &mut app);
        let out = frame(&h, &mut app);
        let row = text(&out, "Error: x").expect("banner").rect;
        let dismiss = nodes(&out)
            .into_iter()
            .find(|n| n.name == "✖" && n.rect.min.y <= row.center().y && row.center().y <= n.rect.max.y)
            .expect("✖ beside the banner text");
        assert!(
            dismiss.rect.width() >= MIN_TARGET && dismiss.rect.height() >= MIN_TARGET,
            "{:?}",
            dismiss.rect
        );
        let (press, release) = click_events(dismiss.rect.center());
        let _ = h.run(press, |ctx| app.frame_ui(ctx));
        let _ = h.run(release, |ctx| app.frame_ui(ctx));
        assert!(app.ui_alert.is_none());
    }

    #[test]
    fn header_tabs_banner_and_connect_are_at_least_24() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let _ = frame(&h, &mut app);
        let out = frame(&h, &mut app);
        let n = |name: &str| node(&out, name).unwrap_or_else(|| panic!("{name} is on screen"));
        let theme = n("☀"); // dark mode is the default (src/app/mod.rs:305)
        assert!(
            theme.rect.width() >= MIN_TARGET && theme.rect.height() >= MIN_TARGET,
            "theme toggle {:?}",
            theme.rect
        );
        for name in ["📊 Topics", "📤 Publish", "🔍 Query", "❓ Help", "Connect"] {
            assert!(n(name).rect.height() >= MIN_TARGET, "{name} {:?}", n(name).rect);
        }
        app.connection_status = ConnectionStatus::Connected;
        let out = frame(&h, &mut app);
        let d = node(&out, "Disconnect").expect("Disconnect");
        assert!(d.rect.height() >= MIN_TARGET, "{:?}", d.rect);
    }

    #[test]
    fn connect_blocked_reason_rules() {
        assert_eq!(connect_blocked_reason("client", "localhost", "7447", "7448"), None);
        assert_eq!(
            connect_blocked_reason("client", "localhost", "abc", "7448"),
            Some("Fix the Port field")
        );
        assert_eq!(
            connect_blocked_reason("client", "", "abc", "7448"),
            None,
            "Port is unused without an address"
        );
        assert_eq!(
            connect_blocked_reason("peer", "", "7447", "80"),
            Some("Fix the Listen Port field")
        );
        assert_eq!(
            disconnect_blocked_reason(&ConnectionStatus::ConnectingMonitor),
            Some("Available once connected")
        );
        assert_eq!(disconnect_blocked_reason(&ConnectionStatus::Connected), None);
    }

    #[test]
    fn connect_reasons_are_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        app.connect_address = "localhost".into();
        app.connect_port = "abc".into();
        let out = frame(&h, &mut app);
        let reason = text(&out, "Fix the Port field").expect("reason beside Connect");
        let connect = node(&out, "Connect").expect("Connect");
        assert!(reason.rect.left() >= connect.rect.right(), "right of the button");
        assert!((reason.rect.center().y - connect.rect.center().y).abs() < MIN_TARGET / 2.0);
        app.connection_status = ConnectionStatus::ConnectingPublishing;
        let out = frame(&h, &mut app);
        assert!(text(&out, "Available once connected").is_some());
    }

    #[test]
    fn connection_error_links_troubleshooting() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        app.connection_status = ConnectionStatus::Error("boom".into());
        let out = frame(&h, &mut app);
        // after the merge the frame has other "More in Help" links (empty
        // tree, Limits row) and `nodes` comes from a hash map, so pick the
        // one nearest below the error text
        let err = text(&out, "Error: boom").expect("error text").rect;
        let link = nodes(&out)
            .into_iter()
            .filter(|n| n.name == "More in Help" && n.rect.top() >= err.bottom() - 1.0)
            .min_by(|a, b| a.rect.top().total_cmp(&b.rect.top()))
            .expect("link under the error");
        assert!(link.rect.top() - err.bottom() < MIN_TARGET, "directly under the error");
        assert!(
            (link.rect.left() - err.left()).abs() < MIN_TARGET,
            "in the connection panel, not another view"
        );
        let (press, release) = click_events(link.rect.center());
        let _ = h.run(press, |ctx| app.frame_ui(ctx));
        let _ = h.run(release, |ctx| app.frame_ui(ctx));
        assert_eq!(app.detail_view, DetailView::Help);
        assert_eq!(app.help_target, Some(section::TROUBLESHOOTING), "its own section");
    }
```

Run: `cargo test --locked app::layout 2>&1 | grep -E '^error' | sort -u | head`
Expected: errors for `frame_ui`, `expire_alert`, `alert_time_left`, `connect_blocked_reason`, `disconnect_blocked_reason`.

- [ ] **Step 2: Split `frame_ui` out of `update`.** In `src/app/layout.rs`, keep only the first-frame block (lines 71-78) in `update` and move the rest of its body (lines 79-590) verbatim into a new inherent method; `update` then calls it:

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
    /// `Frame` cannot be constructed outside eframe.
    pub(crate) fn frame_ui(&mut self, ctx: &egui::Context) {
        // Process any pending events from the Zenoh worker
        self.process_events();
        // Success and Warning alerts leave on their own; wake the UI for it
        self.expire_alert(ctx, Instant::now());
        // … the rest of the old body, unchanged, down to the idle repaint (old line 590)
    }
}
```

- [ ] **Step 3: Expiry and the banner.** Replace the banner block (old lines 500-528, from `// Global alert banner` through the closing `}` of `if let Some(alert)`) with:

```rust
                // Global alert banner (export errors, warnings) — visible on every tab
                self.show_alert_banner(ui);
```

and add to the `impl ZenohExplorer` block:

```rust
    /// Remembers when the current alert appeared, clears a Success or Warning
    /// whose time is up, and otherwise asks egui to wake the UI when it will
    /// be, so the banner leaves without input (P1 repaints on events only).
    fn expire_alert(&mut self, ctx: &egui::Context, now: Instant) {
        let seen = self.ui_alert_since.as_ref().map(|(a, _)| a);
        if self.ui_alert.as_ref() != seen {
            self.ui_alert_since = self.ui_alert.clone().map(|a| (a, now));
        }
        let (Some(alert), Some((_, since))) = (&self.ui_alert, &self.ui_alert_since) else {
            return;
        };
        let left = alert_time_left(alert, now.saturating_duration_since(*since));
        match left {
            Some(left) if left.is_zero() => {
                self.ui_alert = None;
                self.ui_alert_since = None;
            }
            Some(left) => ctx.request_repaint_after(left),
            None => {}
        }
    }

    /// The banner for `ui_alert`; ✖ dismisses it (the only way for an Error).
    fn show_alert_banner(&mut self, ui: &mut egui::Ui) {
        let Some(alert) = self.ui_alert.clone() else {
            return;
        };
        egui::TopBottomPanel::top("alert_banner").show_inside(ui, |ui| {
            ui.horizontal(|ui| {
                let (text, color) = match &alert {
                    UiAlert::Success(_) => (
                        alert.text().to_string(),
                        if self.dark_mode {
                            ExplorerColors::DARK_SUCCESS
                        } else {
                            ExplorerColors::SUCCESS
                        },
                    ),
                    UiAlert::Warning(_) => (
                        format!("Warning: {}", alert.text()),
                        ExplorerColors::WARNING,
                    ),
                    UiAlert::Error(_) => (
                        format!("Error: {}", alert.text()),
                        ExplorerColors::ERROR,
                    ),
                };
                ui.label(RichText::new(text).color(color));
                if ui.add(icon_button("✖")).on_hover_text("Dismiss").clicked() {
                    self.ui_alert = None;
                }
            });
        });
    }
```

and, next to the other free functions near the top of the file (after `listen_port_error`, line 65):

```rust
/// How long an alert stays: Success 6 s, Warning 10 s; an Error stays until ✖.
pub(crate) fn alert_lifetime(alert: &UiAlert) -> Option<Duration> {
    match alert {
        UiAlert::Success(_) => Some(Duration::from_secs(6)),
        UiAlert::Warning(_) => Some(Duration::from_secs(10)),
        UiAlert::Error(_) => None,
    }
}

/// Time left before `alert` clears itself at `age` (zero once due); None for an Error.
pub(crate) fn alert_time_left(alert: &UiAlert, age: Duration) -> Option<Duration> {
    alert_lifetime(alert).map(|life| life.saturating_sub(age))
}
```

Add `use crate::app::theme::icon_button;` to the imports (line 9 block).

- [ ] **Step 4: 24 pt header toggle.** At line 113-117 replace `ui.button(if self.dark_mode { "☀" } else { "🌙" })` with `ui.add(icon_button(if self.dark_mode { "☀" } else { "🌙" }))`. The tabs (lines 531-570), Connect, Disconnect and combo boxes are 24 pt through Step 0's `interact_size.y`; no change.

- [ ] **Step 5: Connect and Disconnect reasons, and the Troubleshooting link.** Add after `listen_port_error` (line 65):

```rust
/// Why Connect is disabled, written beside it; None when it is enabled.
fn connect_blocked_reason(
    mode: &str,
    address: &str,
    port: &str,
    listen_port: &str,
) -> Option<&'static str> {
    if connect_port_error(address, port).is_some() {
        Some("Fix the Port field")
    } else if listen_port_error(mode, listen_port).is_some() {
        Some("Fix the Listen Port field")
    } else {
        None
    }
}

/// Why Disconnect is disabled: only while a connect attempt is running.
fn disconnect_blocked_reason(status: &ConnectionStatus) -> Option<&'static str> {
    match status {
        ConnectionStatus::ConnectingPublishing | ConnectionStatus::ConnectingMonitor => {
            Some("Available once connected")
        }
        _ => None,
    }
}
```

Replace the connection error line (406-408) so the link sits on the line under the message (a `horizontal` would stop a long error from wrapping):

```rust
                        let error_text = match &self.connection_status {
                            ConnectionStatus::Error(err) => Some(format!("Error: {}", err)),
                            _ => None,
                        };
                        if let Some(error_text) = error_text {
                            ui.colored_label(ExplorerColors::ERROR, error_text);
                            self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);
                        }
```

Replace `let ports_ok = …; if ui.add_enabled(ports_ok, egui::Button::new("Connect")).clicked() {` (lines 424-431) with the following; the body of the `if` (lines 432-476) is unchanged:

```rust
                        let blocked = connect_blocked_reason(
                            &self.connection_mode,
                            &self.connect_address,
                            &self.connect_port,
                            &self.listen_port,
                        );
                        let clicked = ui
                            .horizontal(|ui| {
                                let clicked = ui
                                    .add_enabled(blocked.is_none(), egui::Button::new("Connect"))
                                    .clicked();
                                if let Some(reason) = blocked {
                                    ui.label(
                                        RichText::new(reason)
                                            .size(TEXT_SMALL_SIZE)
                                            .color(self.text_secondary_color()),
                                    );
                                }
                                clicked
                            })
                            .inner;
                        if clicked {
```

In the Disconnect row (lines 481-497), after the `if ui.add_enabled(…, egui::Button::new("Disconnect")).clicked() { … }` block and inside the same `ui.horizontal`, add:

```rust
                        if let Some(reason) = disconnect_blocked_reason(&self.connection_status) {
                            ui.label(
                                RichText::new(reason)
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        }
```

- [ ] **Step 6: Green, then checks and commit.**

```bash
df -g /System/Volumes/Data | awk 'NR==2{print $4}'   # wait while under 3
cargo test --locked app::layout 2>&1 | grep -E '^test |test result'
cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked 2>&1 | grep -E 'test result|FAILED'
grep -c 'request_repaint_after' src/app/layout.rs     # 4 (was 3)
grep -c 'small_button' src/app/layout.rs              # 0
git add src/app/layout.rs
git commit -m "feat(uxk-t1-a): alerts clear themselves, 24 pt header and banner, Connect reasons

Success 6 s, Warning 10 s, Error until the ✖; expiry requests a repaint at
the due time. frame_ui split out of update for headless tests.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: the ten layout tests (`connection_hints_match_the_form`, `form_locators_trim_inputs` and the eight new ones) `ok`; fmt and clippy silent; no `FAILED`; `4`; `0`. `request_repaint_after` counts the connecting spinner, the health pulse, the idle tick and the new expiry call. If `expiry_wakes_the_ui_when_due` sees a delay of zero after the settle frame, add a second settle frame and record it.

---

### Part b: tree filter highlight and counts, 24 pt tree targets, Subscribe and Save File reasons, tree Help links

**Files:**
- Modify: `src/types/tree.rs` (new functions after `compute_visible_paths`, which ends at line 211; tests appended before the module's closing brace, line 421)
- Modify: `src/ui/topic_tree.rs`. Anchors: imports lines 3-14; filter row 165-173; Subscribe key error 188-191; Subscribe button 192-202; Unsubscribe ✖ 213; filter cache 245-274; empty tree 285-305; Save File 405-421; chunk "💾 Save" 503; leaf row 760-845 (label at 793, row save 💾 at 833); branch row 846-935 (`load_with_default_open` 855-859 stays unchanged, label 901-902); `subscribe_enabled` 976-982; tests 1074-1311.

**Interfaces:**
- Consumes (Step 0): `MIN_TARGET`, `icon_button`, `tree_filter_counts`, `help_link`, `section::{KEY_EXPRESSIONS, GETTING_STARTED}`, `crate::app::headless`.
- Produces: `crate::types::{count_filter_matches(&ZenohNode, &str) -> (usize, usize), match_range(&str, &str) -> Option<Range<usize>>}`; `ZenohExplorer::subscribe_blocked_reason(&self) -> Option<&'static str>`; `fn row_label(ui, icon, key, filter_lower, underline) -> egui::WidgetText`; `fn filter_count_text(n, m) -> String`.

- [ ] **Step 1: Tree-model tests first** (red; from `f023a8c`). Append to the tests module of `src/types/tree.rs`:

```rust
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

Run: `cargo test --locked types::tree 2>&1 | grep -E '^error' | sort -u`
Expected: `cannot find function` errors for the two names.

- [ ] **Step 2: Tree-model helpers** (verbatim from `f023a8c`, without its default-open helper). In `src/types/tree.rs`, after `compute_visible_paths` (line 211):

```rust
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

Run: `cargo test --locked types::tree 2>&1 | grep 'test result'`
Expected: `test result: ok. 12 passed` (10 existing + 2). Clippy may flag the two functions as unused until Step 5 wires them; that is expected until then.

- [ ] **Step 3: Tree-panel tests first** (red). Append to the tests module of `src/ui/topic_tree.rs` (before its closing brace, line 1311):

```rust
    use crate::app::headless::{node, nodes, text, text_shapes, texts, Headless, WIDE};
    use crate::app::theme::MIN_TARGET;
    use crate::ui::help::section;

    fn tree_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_tree_panel(ui)
    }

    fn details_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_topic_details(ui)
    }

    /// An app with `paths` in the tree and the branches in `open` expanded.
    fn tree_app(paths: &[&str], open: &[&str]) -> (ZenohExplorer, Headless) {
        let (app, _tx) = ZenohExplorer::test_app();
        for p in paths {
            app.browse_tree.write().unwrap().insert_path(p);
        }
        let h = Headless::new(WIDE);
        for b in open {
            let mut s = egui::collapsing_header::CollapsingState::load_with_default_open(
                &h.ctx,
                egui::Id::new(("treenode", *b)),
                true,
            );
            s.set_open(true);
            s.store(&h.ctx);
        }
        (app, h)
    }

    /// Two frames (layout settles), with "Subscribe to Topics" opened by a click.
    fn open_subscribe(h: &Headless, app: &mut ZenohExplorer) -> egui::FullOutput {
        let out = h.panel(app, vec![], tree_panel);
        let header = node(&out, "Subscribe to Topics").expect("header").rect;
        h.click_panel(app, header.center(), tree_panel)
    }

    #[test]
    fn subscribe_reason_matches_enabled() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.subscribe_key = "demo/**".into();
        assert_eq!(app.subscribe_blocked_reason(), Some("Connect first"));
        app.connection_status = ConnectionStatus::Connected;
        assert_eq!(app.subscribe_blocked_reason(), None);
        assert!(app.subscribe_enabled());
        app.pending_subscribes.insert("demo/**".into());
        assert_eq!(app.subscribe_blocked_reason(), Some("Subscribing…"));
        app.pending_subscribes.clear();
        app.subscriptions.push(Subscription {
            id: "1".into(),
            key_expr: "demo/**".into(),
            reliability: String::new(),
            mode: String::new(),
        });
        assert_eq!(app.subscribe_blocked_reason(), Some("Already subscribed to this key"));
        app.subscribe_key = "demo//x".into();
        assert_eq!(app.subscribe_blocked_reason(), Some("Fix the key above"));
        for status in [ConnectionStatus::Disconnected, ConnectionStatus::Connected] {
            app.connection_status = status;
            for key in ["demo/**", "demo//x", "other/*"] {
                app.subscribe_key = key.into();
                assert_eq!(
                    app.subscribe_blocked_reason().is_none(),
                    app.subscribe_enabled(),
                    "{key}: a reason exactly when disabled"
                );
            }
        }
    }

    #[test]
    fn subscribe_reason_is_visible() {
        let (mut app, h) = tree_app(&[], &[]);
        app.subscribe_key = "demo/**".into();
        let out = open_subscribe(&h, &mut app);
        let reason = text(&out, "Connect first").expect("reason beside Subscribe");
        let button = node(&out, "Subscribe").expect("Subscribe");
        assert!(reason.rect.left() >= button.rect.right(), "right of the button");
        app.connection_status = ConnectionStatus::Connected;
        let out = h.panel(&mut app, vec![], tree_panel);
        assert!(text(&out, "Connect first").is_none());
    }

    #[test]
    fn save_reason_is_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        app.browse_tree
            .write()
            .unwrap()
            .insert_path("demo/x")
            .update_data(
                "v".to_string(),
                "text/plain".to_string(),
                false,
                SampleKindView::Put,
                None,
            );
        app.selected_topic = Some("demo/x".to_string());
        let texts = details_texts(&mut app);
        assert!(
            texts.iter().any(|t| t == "No payload stored yet"),
            "painted, not only a hover: {texts:?}"
        );
        let h = Headless::new(WIDE);
        let out = h.panel(&mut app, vec![], details_panel);
        let pause = node(&out, "⏸ Pause list").expect("Pause list");
        assert!(pause.rect.height() >= MIN_TARGET, "{:?}", pause.rect);
    }

    #[test]
    fn tree_rows_are_at_least_24() {
        let (mut app, h) = tree_app(&["demo/a", "demo/b"], &["demo"]);
        let _ = h.panel(&mut app, vec![], tree_panel);
        let out = h.panel(&mut app, vec![], tree_panel);
        let a = node(&out, "💾 a").expect("row a").rect;
        let b = node(&out, "💾 b").expect("row b").rect;
        let branch = node(&out, "🌐 demo").expect("branch row").rect;
        for r in [a, b, branch] {
            assert!(r.height() >= MIN_TARGET, "{r:?}");
        }
        assert!(b.top() - a.top() >= MIN_TARGET, "rows do not overlap");
    }

    #[test]
    fn tree_icon_buttons_are_24_square() {
        let (mut app, h) = tree_app(&["demo/b"], &["demo"]);
        app.payload_store.write().unwrap().insert(
            "demo/b".into(),
            PayloadEntry {
                bytes: b"hello".to_vec(),
                received_at: chrono::Utc::now(),
                filename: None,
            },
        );
        app.connection_status = ConnectionStatus::Connected;
        app.subscriptions.push(Subscription {
            id: "1".into(),
            key_expr: "demo/**".into(),
            reliability: String::new(),
            mode: String::new(),
        });
        let out = open_subscribe(&h, &mut app);
        let icons: Vec<_> = nodes(&out)
            .into_iter()
            .filter(|n| n.name == "✖" || n.name == "💾")
            .collect();
        assert_eq!(icons.len(), 3, "clear filter, unsubscribe and the row's save: {icons:?}");
        for n in icons {
            assert!(
                n.rect.width() >= MIN_TARGET && n.rect.height() >= MIN_TARGET,
                "{} is {:?}",
                n.name,
                n.rect.size()
            );
        }
    }

    /// Regression guard, not a red/green test: it passes before and after this
    /// part and fails only if the row work moves or resizes egui's expander.
    #[test]
    fn expander_rect_unchanged() {
        let (mut app, h) = tree_app(&["demo/a"], &[]);
        let _ = h.panel(&mut app, vec![], tree_panel);
        let _ = h.panel(&mut app, vec![], tree_panel);
        let spacing = h.ctx.style().spacing.clone();
        let toggle = h
            .ctx
            .read_response(egui::Id::new(("treenode", "demo")))
            .expect("expander response")
            .rect;
        assert_eq!(toggle.size(), egui::vec2(spacing.indent, spacing.icon_width));
    }

    #[test]
    fn filter_shows_counts_and_highlights_the_match() {
        let (mut app, h) = tree_app(&["a/x", "a/y", "b/x"], &[]);
        let out = h.panel(&mut app, vec![], tree_panel);
        assert!(
            texts(&out).iter().all(|t| !t.text.contains(" of ")),
            "no count without a filter"
        );
        app.tree_filter = "x".into();
        let _ = h.panel(&mut app, vec![], tree_panel);
        let out = h.panel(&mut app, vec![], tree_panel);
        assert!(text(&out, "2 of 3 topics").is_some(), "{:?}", texts(&out));
        let fill = h.ctx.style().visuals.selection.bg_fill;
        let rows: Vec<_> = text_shapes(&out)
            .into_iter()
            .filter(|t| t.galley.text() == "💾 x")
            .collect();
        assert_eq!(rows.len(), 2, "a/x and b/x");
        for t in rows {
            let hit = t
                .galley
                .job
                .sections
                .iter()
                .find(|s| s.format.background == fill)
                .expect("the matched part is highlighted");
            assert_eq!(&t.galley.job.text[hit.byte_range.clone()], "x");
            assert!(hit.format.underline.width > 0.0, "not colour alone");
        }
    }

    /// Regression guard, not a red/green test: it passes before and after this
    /// part, because P1 already opens every visible branch while filtering
    /// (`load_with_default_open(ui.ctx(), id, filtering)`), and fails only if
    /// part b narrows that default (for example to ancestors of a match only).
    /// Both branches contain a match, and both also match themselves ("ax",
    /// "bx"), so an ancestors-only rule would leave them closed.
    #[test]
    fn filtering_opens_every_visible_branch() {
        let (mut app, h) = tree_app(&["ax/x", "bx/sub/x"], &[]);
        app.tree_filter = "x".into();
        let _ = h.panel(&mut app, vec![], tree_panel);
        let out = h.panel(&mut app, vec![], tree_panel);
        let leaves = text_shapes(&out)
            .into_iter()
            .filter(|t| t.galley.text() == "💾 x")
            .count();
        assert_eq!(leaves, 2, "ax, bx and bx/sub are all open: {:?}", texts(&out));
    }

    #[test]
    fn tree_help_links_open_their_sections() {
        let (mut app, h) = tree_app(&[], &[]);
        let out = h.panel(&mut app, vec![], tree_panel);
        let link = node(&out, "More in Help").expect("empty tree links Getting started");
        let _ = h.click_panel(&mut app, link.rect.center(), tree_panel);
        assert_eq!(app.detail_view, DetailView::Help);
        assert_eq!(app.help_target, Some(section::GETTING_STARTED));
        app.subscribe_key = "demo//x".into();
        let out = open_subscribe(&h, &mut app);
        assert_eq!(
            nodes(&out).iter().filter(|n| n.name == "More in Help").count(),
            2,
            "the key error links Key expressions too"
        );
    }
```

Run: `cargo test --locked ui::topic_tree 2>&1 | grep -E '^error' | sort -u`
Expected: `no method named subscribe_blocked_reason`.

- [ ] **Step 4: Subscribe, Unsubscribe, Save File and filter row.** In `src/ui/topic_tree.rs`:

Add `use crate::app::theme::icon_button;` to the imports (lines 7-14).

Filter row (lines 165-173) becomes:

```rust
            ui.horizontal(|ui| {
                ui.label("🔍");
                ui.text_edit_singleline(&mut self.tree_filter)
                    .on_hover_text("Filter topics");
                if ui.add(icon_button("✖")).on_hover_text("Clear filter").clicked() {
                    self.tree_filter.clear();
                }
                if !self.tree_filter.is_empty() {
                    if let Some((n, m)) = self.tree_filter_counts {
                        ui.label(
                            RichText::new(filter_count_text(n, m))
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                        );
                    }
                }
            });
```

Subscribe key error (lines 189-191) gains the link on the next line:

```rust
                if let Some(err) = &key_err {
                    ui.colored_label(ExplorerColors::ERROR, err);
                    self.help_link(ui, crate::ui::help::section::KEY_EXPRESSIONS);
                }
```

Subscribe button (lines 192-202): replace `let button = …; if ui.add_enabled(self.subscribe_enabled(), button).clicked() {` with the following; the body of the `if` is unchanged:

```rust
                let clicked = ui
                    .horizontal(|ui| {
                        let clicked = ui
                            .add_enabled(self.subscribe_enabled(), egui::Button::new("Subscribe"))
                            .clicked();
                        // The reason sits beside the button, not only in a hover (F-T7-3)
                        if let Some(reason) = self.subscribe_blocked_reason() {
                            ui.label(
                                RichText::new(reason)
                                    .size(TEXT_SMALL_SIZE)
                                    .color(self.text_secondary_color()),
                            );
                        }
                        clicked
                    })
                    .inner;
                if clicked {
```

Unsubscribe (line 213): `if ui.small_button("✖").clicked() {` becomes `if ui.add(icon_button("✖")).on_hover_text("Unsubscribe").clicked() {`.

Save File (after line 421, still inside the `ui.horizontal` and before the Pause button): the hover stays and the reason is painted:

```rust
                if !saveable {
                    ui.label(
                        RichText::new(&reason)
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_secondary_color()),
                    );
                }
```

and at line 417 pass `reason.clone()` to `on_disabled_hover_text` so `reason` is still available. Chunk block (line 503): `ui.small_button("💾 Save")` becomes `ui.add(icon_button("💾 Save"))`.

Add to the `impl ZenohExplorer` block after `subscribe_enabled` (line 982):

```rust
    /// Why Subscribe is disabled, written beside it; None exactly when it is
    /// enabled (the order follows `subscribe_enabled`).
    pub(crate) fn subscribe_blocked_reason(&self) -> Option<&'static str> {
        let key = self.subscribe_key.trim();
        if !matches!(self.connection_status, ConnectionStatus::Connected) {
            Some("Connect first")
        } else if crate::validation::key_expr_error(&self.subscribe_key).is_some() {
            Some("Fix the key above")
        } else if self.subscriptions.iter().any(|s| s.key_expr == key) {
            Some("Already subscribed to this key")
        } else if self.pending_subscribes.contains(key) {
            Some("Subscribing…")
        } else {
            None
        }
    }
```

and next to `counted` (line 1019):

```rust
/// "2 of 3 topics" beside the tree filter.
fn filter_count_text(n: usize, m: usize) -> String {
    format!("{n} of {}", counted(m, "topic", "topics"))
}
```

- [ ] **Step 5: Counts, highlight, rows, links.** Filter cache (lines 245-274): directly after `self.tree_filter_cache = Some((filter_lower.clone(), self.tree_version, now, visible));` (lines 256-257) add

```rust
                    let counts = count_filter_matches(tree, &filter_lower);
                    if self.tree_filter_counts != Some(counts) {
                        self.tree_filter_counts = Some(counts);
                        // the count row was drawn above with the old value
                        ui.ctx().request_repaint();
                    }
```

and in the `else` branch (line 273) add `self.tree_filter_counts = None;` after `self.tree_filter_cache = None;`.

Empty tree (after the `💡 Try demo/**` label, line 296-300, inside the same `vertical_centered`): `self.help_link(ui, crate::ui::help::section::GETTING_STARTED);`.

Add a free function after `render_transfer_progress` (line 141):

```rust
/// The row label "{icon} {key}". While filtering, the part of `key` that
/// matches is drawn on the selection colour and underlined, so it still shows
/// on a selected row (whose fill is the same colour) and not by colour alone.
fn row_label(
    ui: &egui::Ui,
    icon: &str,
    key: &str,
    filter_lower: &str,
    underline: egui::Color32,
) -> egui::WidgetText {
    let Some(r) = match_range(key, filter_lower) else {
        return format!("{icon} {key}").into();
    };
    let plain = egui::TextFormat {
        font_id: egui::TextStyle::Button.resolve(ui.style()),
        // PLACEHOLDER takes the widget's own text colour, as a plain String does
        color: egui::Color32::PLACEHOLDER,
        ..Default::default()
    };
    let hit = egui::TextFormat {
        background: ui.visuals().selection.bg_fill,
        underline: egui::Stroke::new(1.0, underline),
        ..plain.clone()
    };
    let mut job = egui::text::LayoutJob::default();
    job.append(&format!("{icon} {}", &key[..r.start]), 0.0, plain.clone());
    job.append(&key[r.clone()], 0.0, hit);
    job.append(&key[r.end..], 0.0, plain);
    job.into()
}
```

In `show_tree_node`, after the visibility check (line 755) add

```rust
        let filter_lower = if self.tree_filter_cache.is_some() {
            self.tree_filter.to_lowercase()
        } else {
            String::new()
        };
        let underline = if self.dark_mode {
            ExplorerColors::DARK_PRIMARY
        } else {
            ExplorerColors::PRIMARY
        };
```

Leaf label (line 793): `ui.selectable_label(is_selected, format!("{} {}", icon, node.key))` becomes `ui.selectable_label(is_selected, row_label(ui, icon, &node.key, &filter_lower, underline))`. Branch label (lines 901-902): same change. Leaf row save (line 833): `ui.small_button("💾")` becomes `ui.add(icon_button("💾"))`. The branch default open and its state id (lines 846-859, including the comment and `load_with_default_open(ui.ctx(), id, filtering)`) are not edited: while filtering every visible branch opens by default, as in P1 (user decision, Open question 2); `filtering_opens_every_visible_branch` guards it. The rows themselves need no change: Step 0's `interact_size.y` makes each `ui.horizontal` row and its `selectable_label` 24 pt tall, and `plus_minus_icon` and `show_toggle_button` stay untouched.

- [ ] **Step 6: Green, guard, checks and commit.**

```bash
df -g /System/Volumes/Data | awk 'NR==2{print $4}'   # wait while under 3
cargo test --locked types::tree 2>&1 | grep 'test result'
cargo test --locked ui::topic_tree 2>&1 | grep -E '^test |test result'
python3 "$UXRUN/fn_body.py" plus_minus_icon < src/ui/topic_tree.rs > "$UXRUN/pmi-head-b.txt" &&
  cmp "$UXRUN/pmi-base.txt" "$UXRUN/pmi-head-b.txt" && echo expander-unchanged
grep -c 'small_button' src/ui/topic_tree.rs          # 0
cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked 2>&1 | grep -E 'test result|FAILED'
git add src/types/tree.rs src/ui/topic_tree.rs
git commit -m "feat(uxk-t1-b): filter highlight and counts, 24 pt tree targets, visible Subscribe and Save reasons

match_range and count_filter_matches from Snow White f023a8c; the match
is drawn on the selection colour and underlined. Every visible branch
still opens while filtering (P1 behaviour kept).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: `types::tree` 12 passed; every `ui::topic_tree` test `ok` (the 7 existing and the 9 new, one of them the regression guard `filtering_opens_every_visible_branch`); `expander-unchanged`; `0`; fmt and clippy silent; no `FAILED`. If `tree_icon_buttons_are_24_square` finds a number other than 3, print the list before changing anything: the leaf row's label is named `💾 b`, not `💾`.

---

### Part c: Publish, Queryable and Query reasons, Help links, 24 pt checks in those views

**Files:**
- Modify: `src/ui/publish.rs`. Anchors: imports lines 3-9; connection notice 52-56; key error 63-65; Publish button 248-316; Key Pattern row 338-346; pattern error 347-350; Enable Queryable row 353-397; tests 402-432.
- Modify: `src/ui/query.rs`. Anchors: connection notice 18-22; selector error 68-71; Query button 84-107; `connection_notice` 215-227; tests 254-315.
- Modify: `src/ui/messages.rs`. Anchors: the Memory Limit row `ui.horizontal` 119-153; tests 239-285.

**Interfaces:**
- Consumes (Step 0): `MIN_TARGET`, `help_link`, `section::{KEY_EXPRESSIONS, TROUBLESHOOTING, LIMITS}`, `crate::app::headless`.
- Produces: `fn publish_blocked_reason(connected: bool, key_invalid: bool) -> Option<&'static str>`; `fn queryable_blocked_reason(connected: bool, pattern_invalid: bool) -> Option<&'static str>`; `const PATTERN_LOCKED: &str`; `fn query_blocked_reason(connected: bool, selector_invalid: bool, timeout_invalid: bool) -> Option<&'static str>`.

- [ ] **Step 1: Tests first** (red). Append to the tests module of `src/ui/publish.rs` (before line 432's closing brace):

```rust
    use crate::app::headless::{node, nodes, text, Headless, WIDE};
    use crate::app::theme::MIN_TARGET;

    fn publish_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_publish_tab(ui)
    }

    #[test]
    fn publish_blocked_reason_rules() {
        assert_eq!(publish_blocked_reason(false, false), Some("Connect first"));
        assert_eq!(publish_blocked_reason(false, true), Some("Connect first"));
        assert_eq!(publish_blocked_reason(true, true), Some("Fix the key above"));
        assert_eq!(publish_blocked_reason(true, false), None);
        assert_eq!(queryable_blocked_reason(true, true), Some("Fix the pattern above"));
        assert_eq!(queryable_blocked_reason(false, true), None, "Off: not connected says it");
        assert_eq!(queryable_blocked_reason(true, false), None);
    }

    #[test]
    fn publish_reasons_are_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let out = h.panel(&mut app, vec![], publish_panel);
        let publish = node(&out, "Publish").expect("Publish button");
        let reason = text(&out, "Connect first").expect("reason beside Publish");
        assert!(reason.rect.left() >= publish.rect.right(), "right of the button");
        assert!((reason.rect.center().y - publish.rect.center().y).abs() < MIN_TARGET / 2.0);
        for name in ["Publish", "Import File", "Enable Queryable"] {
            let n = node(&out, name).unwrap_or_else(|| panic!("{name}"));
            assert!(n.rect.height() >= MIN_TARGET, "{name} {:?}", n.rect);
        }
        assert!(node(&out, "More in Help").is_some(), "the notice links Troubleshooting");
        app.connection_status = ConnectionStatus::Connected;
        app.publish_key = "demo//x".into();
        app.queryable_pattern = "a//b".into();
        let out = h.panel(&mut app, vec![], publish_panel);
        assert!(text(&out, "Fix the key above").is_some());
        assert!(text(&out, "Fix the pattern above").is_some());
        assert_eq!(
            nodes(&out).iter().filter(|n| n.name == "More in Help").count(),
            1,
            "the key error links Key expressions"
        );
        app.publish_key = "demo/x".into();
        app.queryable_pattern = "**".into();
        app.queryable_enabled = true;
        let out = h.panel(&mut app, vec![], publish_panel);
        assert!(text(&out, PATTERN_LOCKED).is_some(), "the lock reason is painted");
    }
```

Append to the tests module of `src/ui/query.rs` (before line 315's closing brace):

```rust
    use crate::app::headless::{node, text, Headless, WIDE};
    use crate::app::theme::MIN_TARGET;
    use crate::ui::help::section;

    fn query_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
        a.show_query_tab(ui)
    }

    #[test]
    fn query_blocked_reason_rules() {
        assert_eq!(query_blocked_reason(false, false, false), Some("Connect first"));
        assert_eq!(query_blocked_reason(true, true, true), Some("Fix the selector above"));
        assert_eq!(query_blocked_reason(true, false, true), Some("Fix the timeout above"));
        assert_eq!(query_blocked_reason(true, false, false), None);
    }

    #[test]
    fn query_reason_and_links_are_visible() {
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let out = h.panel(&mut app, vec![], query_panel);
        let query = node(&out, "Query").expect("Query button");
        let reason = text(&out, "Connect first").expect("reason beside Query");
        assert!(reason.rect.left() >= query.rect.right());
        assert!(query.rect.height() >= MIN_TARGET, "{:?}", query.rect);
        let link = node(&out, "More in Help").expect("the notice links Troubleshooting");
        let _ = h.click_panel(&mut app, link.rect.center(), query_panel);
        assert_eq!(app.detail_view, DetailView::Help);
        assert_eq!(app.help_target, Some(section::TROUBLESHOOTING));
        app.connection_status = ConnectionStatus::Connected;
        app.query_selector = "demo//x".into();
        let out = h.panel(&mut app, vec![], query_panel);
        assert!(text(&out, "Fix the selector above").is_some());
        let link = node(&out, "More in Help").expect("the selector error links Key expressions");
        let _ = h.click_panel(&mut app, link.rect.center(), query_panel);
        assert_eq!(app.help_target, Some(section::KEY_EXPRESSIONS));
    }
```

Append to the tests module of `src/ui/messages.rs` (before line 285's closing brace):

```rust
    #[test]
    fn limits_link_and_controls_are_24() {
        use crate::app::headless::{node, Headless, WIDE};
        use crate::app::theme::MIN_TARGET;
        fn messages_panel(a: &mut ZenohExplorer, ui: &mut egui::Ui) {
            a.show_messages_tab(ui)
        }
        let (mut app, _tx) = ZenohExplorer::test_app();
        let h = Headless::new(WIDE);
        let out = h.panel(&mut app, vec![], messages_panel);
        for name in ["Clear", "Auto-scroll", "Dedup"] {
            let n = node(&out, name).unwrap_or_else(|| panic!("{name}"));
            assert!(n.rect.height() >= MIN_TARGET, "{name} {:?}", n.rect);
        }
        let link = node(&out, "More in Help").expect("the Limits row links Limits");
        let _ = h.click_panel(&mut app, link.rect.center(), messages_panel);
        assert_eq!(app.help_target, Some(crate::ui::help::section::LIMITS));
    }
```

Run: `cargo test --locked ui:: 2>&1 | grep -E '^error' | sort -u`
Expected: errors for `publish_blocked_reason`, `queryable_blocked_reason`, `PATTERN_LOCKED`, `query_blocked_reason` (`limits_link_and_controls_are_24` compiles and fails at run time).

- [ ] **Step 2: Publish view.** In `src/ui/publish.rs`, after `publish_button_label` (line 43):

```rust
/// Why Publish is disabled, written beside it; None when it is enabled.
fn publish_blocked_reason(connected: bool, key_invalid: bool) -> Option<&'static str> {
    if !connected {
        Some("Connect first")
    } else if key_invalid {
        Some("Fix the key above")
    } else {
        None
    }
}

/// Why Enable Queryable is disabled while connected ("Off: not connected"
/// already says it when disconnected).
fn queryable_blocked_reason(connected: bool, pattern_invalid: bool) -> Option<&'static str> {
    (connected && pattern_invalid).then_some("Fix the pattern above")
}

/// Why the Key Pattern field is locked (hover and painted line).
const PATTERN_LOCKED: &str = "Untick Enable Queryable to change the pattern";
```

Connection notice (lines 53-56) gains the link before the separator:

```rust
        if let Some(notice) = connection_notice(&self.connection_status) {
            ui.label(RichText::new(notice).color(self.text_secondary_color()));
            self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);
            ui.separator();
        }
```

Key error (lines 64-65): after `ui.colored_label(ExplorerColors::ERROR, err);` add `self.help_link(ui, crate::ui::help::section::KEY_EXPRESSIONS);`.

Publish button (lines 255-264): replace from `let button = …` through `.clicked() && !pending {` with the following; the body of the `if` is unchanged:

```rust
            let button = egui::Button::new(publish_button_label(payload_is_empty, pending));
            let connected = matches!(self.connection_status, ConnectionStatus::Connected);
            let blocked = publish_blocked_reason(connected, key_err.is_some());
            let clicked = ui
                .horizontal(|ui| {
                    let clicked = ui.add_enabled(blocked.is_none(), button).clicked();
                    if let Some(reason) = blocked {
                        ui.label(
                            RichText::new(reason)
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                        );
                    }
                    clicked
                })
                .inner;
            if clicked && !pending {
```

Key Pattern row (lines 338-346):

```rust
            ui.horizontal(|ui| {
                ui.label("Key Pattern:");
                // Locked while enabled: the declared queryable keeps its pattern.
                ui.add_enabled(
                    !self.queryable_enabled,
                    egui::TextEdit::singleline(&mut self.queryable_pattern),
                )
                .on_disabled_hover_text(PATTERN_LOCKED);
                if self.queryable_enabled {
                    ui.label(
                        RichText::new(PATTERN_LOCKED)
                            .size(TEXT_SMALL_SIZE)
                            .color(self.text_secondary_color()),
                    );
                }
            });
```

Enable Queryable status (lines 360-383): insert a branch between `if !connected { … }` and `else if self.queryable_enabled { … }`:

```rust
                } else if let Some(reason) =
                    queryable_blocked_reason(connected, pattern_err.is_some())
                {
                    ui.label(
                        RichText::new(reason)
                            .color(self.text_secondary_color())
                            .size(TEXT_SMALL_SIZE),
                    );
```

- [ ] **Step 3: Query view.** In `src/ui/query.rs`, after `connection_notice` (line 227):

```rust
/// Why Query is disabled, written beside it; None when it is enabled.
fn query_blocked_reason(
    connected: bool,
    selector_invalid: bool,
    timeout_invalid: bool,
) -> Option<&'static str> {
    if !connected {
        Some("Connect first")
    } else if selector_invalid {
        Some("Fix the selector above")
    } else if timeout_invalid {
        Some("Fix the timeout above")
    } else {
        None
    }
}
```

Connection notice (lines 19-22): add `self.help_link(ui, crate::ui::help::section::TROUBLESHOOTING);` before `ui.separator();`. Selector error (line 70): after the `colored_label` add `self.help_link(ui, crate::ui::help::section::KEY_EXPRESSIONS);`. Query button (lines 85-94): replace `let button = …; if ui.add_enabled(…, button).clicked() {` with the following; the body is unchanged:

```rust
            let blocked = query_blocked_reason(
                matches!(self.connection_status, ConnectionStatus::Connected),
                selector_err.is_some(),
                timeout_err.is_some(),
            );
            let clicked = ui
                .horizontal(|ui| {
                    let clicked = ui
                        .add_enabled(blocked.is_none(), egui::Button::new("Query"))
                        .clicked();
                    if let Some(reason) = blocked {
                        ui.label(
                            RichText::new(reason)
                                .size(TEXT_SMALL_SIZE)
                                .color(self.text_secondary_color()),
                        );
                    }
                    clicked
                })
                .inner;
            if clicked {
```

- [ ] **Step 4: Limits link.** In `src/ui/messages.rs`, as the last widget of the Memory Limit row (before the row's closing `});` at line 153): `self.help_link(ui, crate::ui::help::section::LIMITS);`.

- [ ] **Step 5: Green, checks and commit.**

```bash
df -g /System/Volumes/Data | awk 'NR==2{print $4}'   # wait while under 3
cargo test --locked ui::publish 2>&1 | grep -E '^test |test result'
cargo test --locked ui::query 2>&1 | grep -E '^test |test result'
cargo test --locked ui::messages 2>&1 | grep -E '^test |test result'
cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked 2>&1 | grep -E 'test result|FAILED'
git add src/ui/publish.rs src/ui/query.rs src/ui/messages.rs
git commit -m "feat(uxk-t1-c): visible Publish, Queryable and Query reasons; Help links; 24 pt checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected: `ui::publish` 4 passed, `ui::query` 4 passed, `ui::messages` 3 passed; fmt and clippy silent; no `FAILED`.

---

## Self-review notes

- **Coverage of the five behaviours:** 1 alerts: part a (`alert_lifetime`, `alert_time_left`, `expire_alert`, banner). 2 targets: Step 0 (`interact_size.y`, `icon_button`, link height), part a (header, tabs, banner ✖, Connect, Disconnect), part b (rows, filter ✖, Unsubscribe ✖, row 💾, chunk 💾 Save, Pause list), part c (Publish, Import File, Enable Queryable, Query, Clear, Auto-scroll, Dedup). 3 Help links: Step 0 (`section`, `help_link`, scroll), placements in parts a (connection error), b (Subscribe key error, empty tree), c (Publish and Query notices and key/selector errors, Limits row). 4 filter: part b. 5 reasons: part a (Connect, Disconnect), part b (Subscribe, Save File), part c (Publish, Enable Queryable, Key Pattern, Query).
- **Not brought back from Snow White:** the status strip, Dismiss key, tab bank, header slots, palette, fonts, glass, keys, motion, `LeafKind` glyphs, the full-row click catcher, focus rings, the "Reading the tree" Help section, context-only row dimming while filtering, and the Connection view.
- **Checked against the current tree (HEAD `d548051`, `src/` equal to `f605391`):** every file:line above was read in this tree; `UiAlert` derives `PartialEq` (`src/app/mod.rs:20`); `IDLE_REPAINT_SECS` is 1 (`src/app/mod.rs:17`); `dark_mode` defaults to true (`src/app/mod.rs:305`); `test_app` exists (`src/app/mod.rs:379-388`); `DetailView` derives `PartialEq, Debug` (`src/types/message.rs:206`); `Subscription` and `PayloadEntry` fields match the tests; `request_repaint_after` appears 3 times in `layout.rs` and `small_button` 4 times in `src/`.
- **Checked against egui 0.29.1 source:** `Button` applies `interact_size.y` unless `small` and then `min_size` (`widgets/button.rs:262-266`); `SelectableLabel` uses `interact_size.y` (`widgets/selected_label.rs:50`) and paints its galley with the widget text colour as fallback (`:80`); `Checkbox`, `ComboBox` and `CollapsingHeader` use `interact_size`; `TextEdit` does not; the toggle button is `(spacing.indent, spacing.icon_width)` and interacts with the state id (`containers/collapsing_header.rs:106-112`); `ScrollArea::animated`, `Response::scroll_to_me`, `Context::run(RawInput, impl FnMut(&Context))`, `ViewportOutput::repaint_delay` and `From<LayoutJob> for WidgetText` exist. AccessKit 0.16.3 nodes expose `name()` and `bounds()`, as Snow White's probe used on the same versions.
- **Clippy during parts:** Step 0's four `UXK-STEP0` allowances keep `-D warnings` clean in every worktree; they are removed only after all merges (step 7), when each item has a caller.

## Open questions for the user

1. **Highlight style.** Default: the matched part of a tree row's name gets the current selection fill (`SELECTED_BACKGROUND` / `DARK_SELECTED_BACKGROUND`) plus a 1 pt underline in the current primary blue, so it still shows on a selected row. Alternative: underline only, as Snow White did. Say if you want the underline only, or a different existing colour.
2. **Branches while filtering.** Decided by the user: keep P1's all-visible-branches-open behaviour. While the filter is non-empty every visible branch opens by default (`load_with_default_open(ui.ctx(), id, filtering)`, `src/ui/topic_tree.rs:855-859`); Snow White's F-T13-2 rule (only ancestors of a match open) is not adopted. Guarded by part b `filtering_opens_every_visible_branch`.
3. **What counts as a 24 pt target.** Default: buttons, tabs, tree rows, checkboxes, combo boxes, collapsing headers and Help links reach 24 pt; icon-only buttons reach 24 × 24 pt. Two exceptions stay as they are: single-line text fields (egui sizes them by font, about 20 pt; Snow White did not change them either) and the tree's ＋/− expander (18 × 14 pt, protected). Enlarging text fields means a `min_size` on each field in parts a, b and c; say if you want it.
4. **Reason for Subscribe with a bad key.** Default: "Fix the key above" beside the disabled button, so every disabled button has a reason. Snow White showed nothing there because the key error is directly above. Keep Snow White's silence instead?
5. **The same alert raised twice.** Default (Snow White `track_alert`): an alert equal to the one already showing (for example saving to the same path twice within 6 s) keeps the first timestamp, so it leaves 6 s after the first save. The alternative needs every producer to reset the timer, which touches the save and memory code outside this plan's files.
