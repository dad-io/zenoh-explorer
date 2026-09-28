# CI and Release Hardening Implementation Plan (P2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. T1 runs seven parallel agents in git worktrees ("How T1 runs"); T2 runs one agent ("How T2 runs"). Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2 (2026-09-25, after P1).** P1 finished at `c5cdb1a` on branch `bearhug-mode-test`. Revision 1 of this plan (14 tasks, digest `9fb1cbd4…`) was written before P1 ran and was never accepted. This revision is a new accept. It merges the 14 tasks into two board tasks to cut board overhead: **T1** (seven parallel parts a–g on disjoint files) and **T2** (one agent, `release.yml` stages 1–4, then integration as stage 5). Every old task's text survives as a part or stage whose heading says "(was Tn)". Inside those sections, old task IDs are rewritten to their new names (map below), and the pre-flight amendments listed under "Changes from revision 1" are applied in place.

| Old task | New place | File(s) |
|---|---|---|
| T1 | T1 part a1 | `.github/workflows/ci.yml`, `.github/workflows/build.yml` |
| T2 | T1 part a2 | `.github/workflows/ci.yml` |
| T3 | T1 part a3 | `.github/workflows/build.yml` |
| T4 | T1 part b | `.github/workflows/msrv.yml` |
| T5 | T1 part c | `.github/dependabot.yml` |
| T6 | T1 part d | `scripts/release-tag.sh`, `scripts/test-release-tag.sh` |
| T7 | T1 part e | `scripts/bundle-macos.sh`, `scripts/test-bundle-macos.sh` |
| T8 | T1 part f | `Cargo.toml` |
| T9 | T1 part g | `README.md` |
| T10 | T2 stage 1 | `.github/workflows/release.yml` |
| T11 | T2 stage 2 | `.github/workflows/release.yml` |
| T12 | T2 stage 3 | `.github/workflows/release.yml` |
| T13 | T2 stage 4 | `.github/workflows/release.yml` |
| T14 | T2 stage 5 | none planned (fix-ups only) |

**Goal:** Make the GitHub Actions pipeline supply-chain safe (SHA pins, least-privilege tokens, no script injection), cheaper on pull requests, honest about what it tests (all three desktop OSes, MSRV, network tests), and make releases produce signed-when-possible, attested, version-checked artifacts with debug symbols.

**Architecture:**
- **Workflows are split by concern** so each part owns one file: `ci.yml` (lint, test matrix, network tests, audit), `build.yml` (per-target check on PRs, release builds on `main`), `msrv.yml` (Rust 1.88 check), `release.yml` (tag-triggered release). Dependabot config lives in `.github/dependabot.yml`.
- **Release logic that can be tested locally lives in scripts:** `scripts/release-tag.sh` (tag format and tag/version check, with `scripts/test-release-tag.sh`) and `scripts/bundle-macos.sh` (the existing `.app` assembler, extended with an output directory and version stamping, with `scripts/test-bundle-macos.sh`).
- **Signing is optional by design:** each signing step runs only when its secrets exist; otherwise the job prints a `::warning::` and ships an unsigned artifact, so forks and secret-less repos still build.
- **The GitHub release is created with the runner's preinstalled `gh` CLI**, not a third-party action (see Global Constraints).

**Tech Stack:** GitHub Actions (hosted runners `ubuntu-latest`, `ubuntu-24.04-arm`, `macos-14`, `macos-latest`, `windows-latest`), bash, PowerShell, Cargo profiles, Apple `codesign`/`notarytool`/`stapler`, Azure Artifact Signing (formerly Trusted Signing), GitHub artifact attestations, Dependabot, `gh`. Local linters: `actionlint` 1.7.12, `zizmor` 1.30.1, `shellcheck`, `check-jsonschema`.

**Spec:** `docs/superpowers/reviews/2026-09-25-zenoh-explorer-deep-review.md`, section "Deferred to later plans" (bullets "CI and release hardening" and "README corrections"), plus the Cargo/CI reviewer findings restated in the parts and stages below.

**Depends on plan:** P1 `docs/superpowers/plans/2026-09-25-correctness-and-hardening.md` is complete (HEAD `c5cdb1a`). Its end state is what revision 1 assumed, checked on the tree: `rust-version = "1.88"` at `Cargo.toml:5`; no `[[bin]]` or `[profile.dev]`; `[profile.release]` is the last section (`Cargo.toml:27-32`); `repository` is at `Cargo.toml:9`; `.cargo/audit.toml` has 4 ignores; an unpinned `audit` job is at `ci.yml:77-84` (P1 T1 Step 6); and there are five `#[ignore = "opens network sessions"]` tests (`src/worker/connect.rs:500,516`, `src/worker/query.rs:143`, `src/worker/session.rs:151,212`). P1 did not touch `ci.yml:1-75`, `README.md` or `release.yml` (132 lines, last changed in `b6d7a94`), so the "pre-P1" line numbers quoted below for those files are also the current ones. P1 renumbered its own tasks (T5, T6, T9, T17, T18, T20–T26 became T27 parts and T28 steps); this plan cites only P1 T1, which kept its number.

**Decisions:** no memex decisions. Recorded P1 execution decision: P3 moves egui/eframe to 0.36, which needs Rust 1.95, and removes the two quick-xml ignores from `.cargo/audit.toml`. This plan keeps MSRV 1.88 (see T1 part b). **Kind of change:** CI, release, build-profile and README only. No application code changes.

## Changes from revision 1 (pre-flight review against the post-P1 tree)

Severity: blocker (task fails as written), major (wrong behaviour or false statement if done literally), minor.

| ID | Severity | Where applied | Change |
|---|---|---|---|
| G2-1 | blocker | T2 stage 1, Global Constraints, every zizmor check | zizmor 1.30.1 `superfluous-actions` flags `softprops/action-gh-release` (Informational severity, High confidence, default Regular persona), so "No findings" was unreachable in old T10–T14. `Create release` is now a `gh release create` run step with the same name; softprops is dropped from the pins. |
| G1-1 | major | T1 part a2 (job comment, Step 3 note) | The ignored tests are not loopback-only: they listen on `[::]` on fixed ports 27501, 27601, 27602, 27701, 27802, use default multicast scouting, and one dials the unroutable 10.255.255.1 (`connect.rs:43,190-207,228,505,519-520`; `query.rs:166-170`; `session.rs:162-166,223-227`). Comment and note rewritten. |
| G2-2 | major | T2 stage 3 | `Azure/artifact-signing-action@c7ab2a86` is composite and restores its signing tools with `actions/cache` by default (`action.yml:191-198,238-275`). Added `cache-dependencies: false` plus a Done-when grep. Removed the moot `insecure-url-scheme` note (that audit only checks pre-commit `repo:` URLs in 1.30.1). |
| G2-3 | major | T1 Step 0, Local tooling, T2 stage 5 Step 0 | actionlint, zizmor, shellcheck, check-jsonschema are not installed and no task installed them. Installation is now T1 Step 0, approved by the user; if declined, T1 is blocked. The `pipx` fallback is replaced by `uv tool install check-jsonschema` (pipx is missing, uv is at `~/.local/bin/uv`). |
| G3-1 | major | T1 part g | README Troubleshooting and Features were false after P1 ("Worker Unresponsive" no longer exists; the queryable toggle is in the Publish tab; an empty query reads "No replies"). Part g now rewrites README.md:26-36 and 70-82 to match `src/ui/help.rs` and adds a Done-when grep. |
| G1-2 | minor | T1 part a2 Interfaces, T2 stage 5 checklist | Required status checks are listed by their rendered names, not job ids. |
| G1-3 | minor | T1 part a1 | The moved line now quotes `"$GITHUB_ENV"`, so `actionlint` exits 0 without `-ignore`. |
| G1-4 | minor | T1 part b, Global Constraints | When P3 raises `rust-version` to 1.95 it must update `msrv.yml`, README and Cargo.toml together and own `msrv.yml`. |
| G2-4 | minor | T1 part e Step 2 | The red run deletes and replaces `target/Zenoh Explorer.app`. In a part-e worktree that is the worktree's own empty `target/`, so the red run is kept; it must never be run in the main checkout. |
| G2-5 | minor | T2 stage 2, T2 stage 5 checklist, Open question 4 | `environment: release` covers all five build rows; the caveats (reviewers gate all 5 jobs, tag-only deployment rules refuse `workflow_dispatch`) go into the checklist, and a partial Apple secret set failing on purpose is stated. |
| G2-6 | minor | T2 stage 1 notes | Dispatch only from a branch that contains `scripts/release-tag.sh` (normally `main`); `release.yml:99` added to the pin list. |
| G3-2 | minor | T1 part g Step 3 | `checksums-sha256.txt` appears on 3 lines of the new block, not 2. |
| G3-3 | minor | T1 part g, Open question 1 | Citation is `Cargo.toml:9`. Part g waits for Open question 1 (T1 Step 0). The README says attestations exist only for public-repository releases. |
| G3-4 | minor | T1 part f | Panic hook citation is `src/main.rs:28-41`; "Worker Unresponsive" replaced by "stale connection state". |
| G3-5 | minor | T1 part f Done-when, T2 stage 4, T2 stage 5 | The dSYM check uses `find … -name 'zenoh-explorer*.dSYM'` instead of a fixed path. |
| G3-6 | minor | T2 stage 5 Step 0, every zizmor expectation | Tool/toolchain presence check before integration; zizmor output is checked as "starts with `No findings to report`" (it may append a suppressed count). |
| N1 (new) | minor | T1 part g | README says the query timeout defaults to 5 seconds; `src/app/mod.rs:298` sets `"10000"` ms and `src/ui/query.rs:77` labels it "Timeout (ms)". The Features rewrite says 10 seconds. README.md:28 "5GB+ file support" is replaced by what the app does (`src/ui/help.rs` step 5: the file is read into memory). |
| N2 (new, merge coupling) | minor | T1 parts e and f, T2 stages 2, 4, 5 | Each worktree builds into its own `CARGO_TARGET_DIR`, so hard-coded `target/release/...` paths would find nothing. Part f and the T2 stages use `"$CARGO_TARGET_DIR"`. Part e checks the script's default arguments with a stand-in binary at the worktree's `target/release/zenoh-explorer` instead of a second release build; the real-binary run is in the T1 integration. |
| N3 (new, merge coupling) | minor | T1 Step 0, parts a3 and b | `rustup target add` (old T3) and `rustup toolchain install 1.88` (old T4) change shared rustup state and would race when parts run in parallel. Both moved to T1 Step 0; the parts only check that they are present. |
| N4 (new, merge coupling) | minor | How T1 runs | `release.yml` stays in its old form until T2, so `zizmor .github/` and bare `actionlint` during T1 would report its known findings. T1 lints the files it owns by name; the whole-directory lint is T2 stage 5. |

## Global Constraints

- No file under `src/` is modified by this plan. Windows file logging (the app has no console because of `windows_subsystem = "windows"` at `src/main.rs:2`) is **out of scope**; it is app code and belongs to a later plan.
- Every `uses:` reference is a full 40-hex commit SHA followed by `# <tag>`. The pins for this plan (resolved 2026-09-25 with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`; checkout, rust-toolchain and rust-cache re-checked in the pre-flight):
  - `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`
  - `dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1` (always with an explicit `toolchain:` input)
  - `Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2`
  - `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1`
  - `actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1`
  - `actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2`
  - `Azure/artifact-signing-action@c7ab2a863ab5f9a846ddb8265964877ef296ee82 # v2.0.0` (always with `cache-dependencies: false`)
- The GitHub release is created by a `run:` step calling the runner's preinstalled `gh release create`. `softprops/action-gh-release` is not used: zizmor 1.30.1's `superfluous-actions` audit flags it under the default persona (`crates/zizmor/src/audit/superfluous_actions.rs:59-63` at tag v1.30.1), which would make every "No findings" check fail.
- Every `actions/checkout` step sets `persist-credentials: false`.
- Every workflow has top-level `permissions: contents: read`. Only `release.yml`'s `release` job gets `contents: write` (plus `id-token: write` and `attestations: write` for provenance).
- No `${{ … }}` expression inside a `run:` script body. Values reach scripts through `env:` and are read as shell variables (`"$TARGET"`, or `$env:TARGET` in PowerShell).
- Every workflow sets `defaults: run: shell: bash`, so Windows `run:` steps are bash unless a step says `shell: pwsh`.
- `release.yml` uses no cache action (zizmor `cache-poisoning`), directly or through a composite action's defaults (hence `cache-dependencies: false` on the Azure signing action).
- Every cargo build, check, clippy and test command in CI passes `--locked`.
- Release asset names (README, `release.yml` and the checksum file must agree):
  - `zenoh-explorer-aarch64-apple-darwin.zip`, `zenoh-explorer-x86_64-apple-darwin.zip` (each holds `Zenoh Explorer.app`, `README.md`, `LICENSE`)
  - `zenoh-explorer-x86_64-pc-windows-msvc.zip` (holds `zenoh-explorer.exe`, `README.md`, `LICENSE`)
  - `zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz`, `zenoh-explorer-aarch64-unknown-linux-gnu.tar.gz` (hold `zenoh-explorer`, `README.md`, `LICENSE`)
  - `zenoh-explorer-<target>-debug-symbols.<zip|tar.gz>` (same extension as that target's main archive)
  - `checksums-sha256.txt` (output of `sha256sum` over all of the above)
- Signing secrets (names are fixed; the README and workflow use exactly these). They are stored in a GitHub **environment** named `release`:
  - macOS: `MACOS_CERTIFICATE_P12` (base64 of a Developer ID Application `.p12`), `MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGN_IDENTITY` (for example `Developer ID Application: Jane Doe (ABCDE12345)`), `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` (app-specific password).
  - Windows: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_SIGNING_ENDPOINT` (for example `https://eus.codesigning.azure.net/`), `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`.
- Tag format: `^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$`, and the tag must equal `v` + the `Cargo.toml` package version.
- MSRV is `1.88` (set by P1). `msrv.yml`, `README.md` and `Cargo.toml` must all say 1.88. When P3 raises `rust-version` (planned 1.95), P3 must update the `msrv.yml` `toolchain:` input and job name ("Check on Rust …"), the README MSRV and `Cargo.toml` together, and P3's owned files must include `.github/workflows/msrv.yml`.
- Each commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Local tooling

Checked in the pre-flight on this Mac: `actionlint`, `zizmor`, `shellcheck`, `check-jsonschema` and `pipx` are **not installed**; `brew` (`/opt/homebrew/bin/brew`), `uv` (`~/.local/bin/uv`), `go`, `jq`, `codesign`, `plutil`, `dwarfdump` are present; `gh` is logged in, so `GH_TOKEN=$(gh auth token)` works for zizmor's online audits (`impostor-commit`, `stale-action-refs`, `known-vulnerable-actions`); rustup has `stable`, `1.93.0` and `1.95`, not `1.88`. Installing is a change to the machine, so it happens once, in T1 Step 0, only after the user approves it in chat:

```bash
brew install actionlint shellcheck zizmor check-jsonschema
rustup toolchain install 1.88 --profile minimal
rustup target add x86_64-apple-darwin
actionlint -version   # expect 1.7.12 or newer
zizmor --version      # expect 1.30.1
```

Without Homebrew: `go install github.com/rhysd/actionlint/cmd/actionlint@v1.7.12`, `cargo install zizmor --locked --version 1.30.1`, `uv tool install check-jsonschema`. `actionlint` runs `shellcheck` on every `run:` block when `shellcheck` is on PATH. zizmor may end its clean output with a suppressed count (`No findings to report. Good job! (N suppressed)`), so every zizmor expectation in this plan means "output starts with `No findings to report`".

**What cannot be checked locally:** whether a job actually runs green on GitHub's runners, runner-label availability, secret gating in a real run, environment protection rules, notarization, Azure signing, attestation upload, a real tag push or `workflow_dispatch`, required status checks and Dependabot. The repository has **no git remote** (`git remote -v` is empty). These are listed as "GitHub-only" in each part or stage, with the local check that substitutes for each, and collected in the T2 stage 5 checklist.


## Review Focus
- **Fork or secret-less repository runs the release workflow:** it must still build and publish unsigned artifacts with a visible `::warning::`, not fail. Pinned by T2 stage 2 and T2 stage 3 (the `if: env.HAS_…_SIGNING != 'true'` warning steps) and checked by grep in their Done-when.
- **Hostile or malformed tag (`v1.0.0$(id)`, `v1.0.0\nfoo`, `1.0.0`, `v1.0`):** the release must stop in the `prepare` job before any build. Pinned by T1 part d `scripts/test-release-tag.sh` cases.
- **Tag that does not match `Cargo.toml` (`v0.9.2` while Cargo says `0.9.1`):** the release must stop with a clear error. Pinned by T1 part d case `mismatch`.
- **Private repository on a plan without artifact attestations:** the release must still publish, with a warning instead of a failed attestation step. Pinned by T2 stage 4 (the `github.event.repository.private` gate).
- **Missing `README.md` or `LICENSE` at package time:** packaging must fail loudly instead of shipping an archive without a licence. Pinned by T2 stage 1 (the `|| true` and `-ErrorAction SilentlyContinue` removals and grep in Done-when).

---

## Tasks

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T1 | [Wave 0 · Merged · Step 0 then parallel parts a–g · owns `.github/workflows/ci.yml`, `.github/workflows/build.yml` (a); `.github/workflows/msrv.yml` (b); `.github/dependabot.yml` (c); `scripts/release-tag.sh`, `scripts/test-release-tag.sh` (d); `scripts/bundle-macos.sh`, `scripts/test-bundle-macos.sh` (e); `Cargo.toml` (f); `README.md` (g)] CI, build, supply chain and packaging. Step 0 (coordinator): lint tools, the Rust 1.88 toolchain and the x86_64-apple-darwin target installed with the user's approval; repository URL confirmed (Open question 1). a (was T1 → T2 → T3, one agent): move the build job into `build.yml`, then harden `ci.yml` (SHA pins, least privilege, concurrency, 3-OS tests, ignored network tests on Linux, pinned cargo-audit), then harden `build.yml` (check-only PRs, release builds on main, native arm64 Linux). b (was T4): `msrv.yml` on Rust 1.88. c (was T5): Dependabot for actions and cargo. d (was T6): release tag validation script with an 11-case test. e (was T7): macOS bundle script takes out-dir and version. f (was T8): release profile with split line-table debuginfo. g (was T9): README download/verify section, MSRV 1.88, clone URL, and Features/Troubleshooting that match the app | — | Each part's checks pass in its own worktree, and the T1 integration run passes on the merged tree. Part a: `grep -c '^  build:' .github/workflows/ci.yml` = 0; `actionlint .github/workflows/ci.yml .github/workflows/build.yml` exits 0 with no `-ignore`; `GH_TOKEN=$(gh auth token) zizmor` on each of the two files prints output starting `No findings to report`; `grep -nE 'uses: [^ ]+@[^0-9a-f]'` on both prints nothing; `grep -c gcc-aarch64-linux-gnu .github/workflows/build.yml` = 0; `grep -c 'ubuntu-24.04-arm' .github/workflows/build.yml` = 1; `grep -c '27501' .github/workflows/ci.yml` = 1 (the network-tests comment; 0 before part a); `cargo clippy --all-targets --locked -- -D warnings`, `cargo test --locked` and `cargo test --locked -- --ignored` pass; the a1 commit message records the baseline zizmor summary line. Part b: actionlint and zizmor clean on `msrv.yml`; `cargo +1.88 check --locked --all-targets` passes; `grep -q 'toolchain: "1.88"' .github/workflows/msrv.yml && grep -q 'rust-version = "1.88"' Cargo.toml` exits 0. Part c: `check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml` prints `ok -- validation done`; zizmor clean on it. Part d: `bash scripts/test-release-tag.sh` prints `all 11 cases passed`; shellcheck clean. Part e: `bash scripts/test-bundle-macos.sh` prints `bundle test passed`; shellcheck clean; with no arguments the script builds `target/Zenoh Explorer.app` from `target/release/zenoh-explorer`, checked with a stand-in binary in the worktree only; the integration run bundles the real release binary with an explicit out-dir. Part f: after `cargo build --release --locked`, `find "$CARGO_TARGET_DIR/release" -maxdepth 2 -name 'zenoh-explorer*.dSYM'` finds a dSYM whose `dwarfdump --uuid` matches the binary's; `nm` on the binary lists a `zenoh_explorer` symbol; sizes before/after and the dSYM path are in the commit message. Part g: in README.md `grep -c 'Rust 1.70'` = 0, `grep -c '<repository-url>'` = 0, `grep -c 'checksums-sha256.txt'` = 3, `grep -c 'gh attestation verify'` = 1, `grep -c 'Rust 1.88'` ≥ 1, and the stale-text grep in part g Step 3 (five patterns: Worker Unresponsive, No queryables available, In the Query tab, enable, 5GB, default: 5 seconds) prints `0`. Copy that grep from Step 3, not from this cell. For every part, `git diff --name-only $BASE p2-t1-<part>` lists only its owned files, all seven `--no-ff` merges are conflict-free, and one read-only verifier per part reports no open finding. GitHub-only items (runs on hosted runners, Dependabot) are carried into the T2 stage 5 checklist |
| T2 | [Wave 1 · Merged · stages 1 → 2 → 3 → 4 → 5, one agent · owns `.github/workflows/release.yml`; stage 5 may fix up any P2 file] Release workflow and integration. 1 (was T10): restructure `release.yml`: `prepare` job validates the tag with `scripts/release-tag.sh` through `env:` (fixes the `release.yml:115-116` injection) and checks out the tag; top-level `contents: read`; SHA pins; no cache; `--locked`; no or-true fallback; native arm runner; release created with `gh release create`. 2 (was T11): macOS `.app` via the bundle script, codesign with hardened runtime, notarize and staple when secrets exist, ad-hoc sign and warn otherwise, ship `.zip`. 3 (was T12): Windows exe signed with Azure Artifact Signing (no dependency cache) when secrets exist, warn otherwise, verify the signature. 4 (was T13): debug-symbol assets per target, checksums over everything, build provenance attestation skipped with a warning on private repos. 5 (was T14): full local verification of every P2 file and the GitHub-only checklist | T1 | Stage 1: `actionlint .github/workflows/release.yml` exits 0; zizmor output on it starts `No findings to report`; the six-pattern count loop in stage 1 Step 3 (github.event.inputs, the shell or-true fallback, SilentlyContinue, rust-cache, gcc-aarch64, softprops) prints `0` for every pattern; copy the loop from Step 3, not from this cell; the local prepare-logic check prints `prepare-logic-ok`. Stage 2: `grep -c 'HAS_APPLE_SIGNING'` ≥ 4 and `grep -c 'notarytool submit'` = 1; the local unsigned-path check prints the `unzip` line and `valid on disk`. Stage 3: `grep -c 'HAS_WINDOWS_SIGNING'` ≥ 4, `grep -c 'Azure/artifact-signing-action@c7ab2a86'` = 1, `grep -c 'cache-dependencies: false'` = 1, and the order check prints `order-ok`. Stage 4: `grep -c 'debug-symbols'` ≥ 4, `grep -c 'attestations: write'` = 1, `grep -c 'contents: write'` = 1; the local dSYM zip lists `Contents/Resources/DWARF`. Each stage re-runs actionlint and zizmor on `release.yml` clean before its commit. Stage 5: every command in its Steps 0–4 exits 0 with the stated output (whole-directory actionlint and zizmor, pin/injection/privilege greps, scripts, dependabot schema, fmt, clippy, tests, ignored tests, the 1.88 check, the release build and dSYM check), and the final commit message holds the GitHub-only checklist with the rendered required-check names and the `release` environment caveats. Not runnable locally (listed in the checklist, not claimed): hosted-runner runs, a tag push or dispatch, secrets gating, notarization, Azure signing and attestation upload |

## How the work is split

```
T1  Step 0 (coordinator: tools, toolchain, repo URL)
      │
      ├── part a  ci.yml + build.yml   (a1 → a2 → a3, one agent; was T1 → T2 → T3)
      ├── part b  msrv.yml             (was T4)
      ├── part c  dependabot.yml       (was T5)
      ├── part d  release-tag scripts  (was T6)
      ├── part e  bundle-macos scripts (was T7)
      ├── part f  Cargo.toml profile   (was T8)
      └── part g  README.md            (was T9)
      │
      ownership check → --no-ff merges a..g → one integration run → 7 read-only verifiers
      │
T2  stage 1 → 2 → 3 → 4  (release.yml, one agent; was T10 → T11 → T12 → T13)
      └── stage 5 integration (was T14)
```

The maximum width is seven agents, in T1. T2 is serial because every stage edits `release.yml`, and it depends on T1 because stage 1 calls part d's script, stage 2 calls part e's script and stage 4 packages the debug symbols part f's profile produces.

**How T1 runs.**

1. **Step 0 (coordinator, main checkout, before any worktree).** Run the check below. If anything is missing, ask the user in chat to approve (or to run) the "Local tooling" installs; the agent does not install without that approval. If the user declines, block T1 on the board with the reason "lint tools not installed" instead of starting parts. Also get an answer to Open question 1 (repository URL) before part g starts; part g uses the answer, or the `Cargo.toml:9` URL if the user confirms it.

   ```bash
   for t in actionlint shellcheck zizmor check-jsonschema jq gh; do command -v "$t" >/dev/null || echo "missing $t"; done
   actionlint -version; zizmor --version
   rustup toolchain list | grep '^1.88'
   rustup target list --installed | grep -x x86_64-apple-darwin
   test -n "$(gh auth token)" && echo gh-token-ok
   ```

2. **Start and base.** Start T1 on the board once. Then:

   ```bash
   P2RUN=${TMPDIR:-/tmp}/p2run
   BASE=$(git rev-parse HEAD)   # c5cdb1a plus any board-only commits; record it in the evidence
   mkdir -p "$P2RUN"
   ```

3. **Worktrees and target directories.** One worktree and branch per part, all from `$BASE`. Parts a and f compile the crate with the stable toolchain, so their target directories are seeded with an APFS clone of the main checkout's `target/debug` (the dependencies then do not recompile). Part b uses Rust 1.88, which cannot reuse stable artifacts, so it starts empty. Parts c, d, e and g run no cargo build.

   ```bash
   for p in a b c d e f g; do
     git worktree add -b "p2-t1-$p" "$P2RUN/wt-$p" "$BASE"
   done
   for p in a f; do
     mkdir -p "$P2RUN/tgt-$p" && cp -Rc target/debug "$P2RUN/tgt-$p/debug"
   done
   mkdir -p "$P2RUN/tgt-b"
   ```

4. **Parts run in parallel**, one agent each, in `"$P2RUN/wt-<part>"` with `export CARGO_TARGET_DIR="$P2RUN/tgt-<part>"` (parts a, b, f) and `export P2RUN=…`. A part edits only its owned files, runs the checks in its section, and commits only those files on its branch. Part a runs its stages a1, a2, a3 in order and commits after each. Only part a runs `cargo test -- --ignored` during the parallel phase (the ignored tests listen on fixed ports 27501–27802), so no two agents bind the same port. No part runs `rustup` install commands (Step 0 did). Part f's release build is the only `--release` build in the parallel phase.

5. **Ownership check**, in the main checkout, for each part:

   ```bash
   git diff --name-only "$BASE" "p2-t1-$p"
   ```

   The output must equal the part's owned files exactly (part a: `.github/workflows/build.yml`, `.github/workflows/ci.yml`; part b: `.github/workflows/msrv.yml`; part c: `.github/dependabot.yml`; part d: `scripts/release-tag.sh`, `scripts/test-release-tag.sh`; part e: `scripts/bundle-macos.sh`, `scripts/test-bundle-macos.sh`; part f: `Cargo.toml`; part g: `README.md`). Anything else: stop and report.

6. **Merge**, in the main checkout on the task branch, in the order a, b, c, d, e, f, g:

   ```bash
   git merge --no-ff "p2-t1-$p" -m "merge(p2-t1): part $p

   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   ```

   Owned files are disjoint, so every merge is conflict-free. A conflict means a part edited a file it does not own: stop and report it.

7. **One integration run** on the merged tree, in the main checkout. `release.yml` is still the old file here (T2 rewrites it), so linters are given the T1 files by name (change N4).

   ```bash
   export CARGO_TARGET_DIR="$P2RUN/tgt-int"
   [ -d "$CARGO_TARGET_DIR" ] || cp -Rc "$P2RUN/tgt-f" "$CARGO_TARGET_DIR"   # seed once; a rerun reuses it
   actionlint .github/workflows/ci.yml .github/workflows/build.yml .github/workflows/msrv.yml
   for f in .github/workflows/ci.yml .github/workflows/build.yml .github/workflows/msrv.yml .github/dependabot.yml; do
     GH_TOKEN=$(gh auth token) zizmor "$f" | head -n1
   done                                                 # each line starts "No findings to report"
   check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml
   shellcheck scripts/release-tag.sh scripts/test-release-tag.sh scripts/bundle-macos.sh scripts/test-bundle-macos.sh
   bash scripts/test-release-tag.sh && bash scripts/test-bundle-macos.sh
   cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings
   cargo test --locked && cargo test --locked -- --ignored
   CARGO_TARGET_DIR="$P2RUN/tgt-b" cargo +1.88 check --locked --all-targets
   cargo build --release --locked
   find "$CARGO_TARGET_DIR/release" -maxdepth 2 -name 'zenoh-explorer*.dSYM' | grep -q . && echo dsym-ok
   scripts/bundle-macos.sh "$CARGO_TARGET_DIR/release/zenoh-explorer" "$P2RUN/int-bundle" | tail -n1
   plutil -extract CFBundleShortVersionString raw "$P2RUN/int-bundle/Zenoh Explorer.app/Contents/Info.plist"   # 0.9.1
   grep -c 'Rust 1.70' README.md; grep -c '<repository-url>' README.md; grep -c 'checksums-sha256.txt' README.md
   grep -c 'gh attestation verify' README.md; grep -c 'Rust 1.88' README.md   # 1, then >= 1
   grep -cE 'Worker Unresponsive|No queryables available|In the Query tab, enable|5GB|default: 5 seconds' README.md
   ```

   Expected: every command exits 0 except the `grep -c` lines, which print `0`, `0`, `3`, `1`, `0`; `dsym-ok`; the bundle path; `0.9.1`.

8. **Verify and close.** One read-only verifier per part (seven) reads `git diff "$BASE" "p2-t1-$p"` against that part's section in this plan and reports findings; verifiers edit nothing. Fix-ups for a finding are made by the coordinator in the owning file in a separate commit that names the part. Then complete T1 on the board with the combined evidence (base commit, each part's check output, merge commits, integration output, verifier verdicts), and remove the worktrees and branches (`git worktree remove "$P2RUN/wt-$p"`, `git branch -d "p2-t1-$p"`). Keep `$P2RUN/tgt-int` and `$P2RUN/tgt-b` for T2; the other target directories may be deleted.

No part calls or reads a file another part changes during the parallel phase: parts d, e and f produce inputs only for T2; part b reads `rust-version`, which part f does not touch; part g documents the asset names fixed in Global Constraints. The couplings the pre-flight did find (hard-coded `target/` paths, shared rustup state, the old `release.yml` under whole-directory linting, fixed test ports) are handled above and listed as N2–N4.

**How T2 runs.** One agent, in the main checkout on the task branch, after T1 is complete. `export P2RUN=${TMPDIR:-/tmp}/p2run CARGO_TARGET_DIR="$P2RUN/tgt-int"` (T1's integration directory, which already holds the release build with the new profile). Before stage 4, read the dSYM path part f recorded: `git log --grep '^build(profile)' --format=%B -n1`. Stages 1, 2, 3 and 4 run in order; each ends with actionlint and zizmor on `release.yml` and its own commit. Stage 5 then runs the whole-repository verification. Its fix-ups go into the file that caused them, in a separate commit naming the part or stage that owns it. Complete T2 on the board with each stage's check output and stage 5's full output as evidence; the GitHub-only checklist is recorded as not run, never as passed.

**Not runnable locally, and the local substitute:**

| Check | Why not local | Local substitute |
|---|---|---|
| CI, Build and MSRV jobs green on hosted runners (3 OSes, `ubuntu-24.04-arm`) | no git remote; hosted runners | actionlint + zizmor; the same cargo commands run on this Mac (fmt, clippy, test, `--ignored`, `+1.88 check`, `--target x86_64-apple-darwin` check) |
| Release on a real tag push or `workflow_dispatch` | no remote, no tags pushed | stage 1's prepare-logic simulation with `scripts/release-tag.sh`; part d's 11-case test |
| macOS signing and notarization | needs Developer ID certificate and Apple secrets | stage 2's unsigned path end to end (bundle, ad-hoc sign, zip, `codesign --verify`) |
| Windows Azure signing | needs Azure Artifact Signing secrets and a Windows runner | zizmor/actionlint, the step-order `awk` check, the `cache-dependencies: false` grep |
| Linux `.dwp` location | needs a Linux build | none on this Mac; Open question 3 holds the fallback |
| Provenance attestation upload and `gh attestation verify` | needs a public repository on GitHub | the `private` gate is checked by reading the YAML; none executable |
| `release` environment rules, required status checks, Dependabot runs | repository settings on GitHub | `check-jsonschema` on `dependabot.yml`; the checklist in stage 5's commit |

**File-ownership matrix.**

| File | Owner | Why no conflict |
|---|---|---|
| `.github/workflows/ci.yml` | T1 part a (a1, a2) | one agent, in order |
| `.github/workflows/build.yml` (new) | T1 part a (a1, a3) | one agent, in order |
| `.github/workflows/msrv.yml` (new) | T1 part b | single owner |
| `.github/dependabot.yml` (new) | T1 part c | single owner |
| `scripts/release-tag.sh`, `scripts/test-release-tag.sh` (new) | T1 part d | single owner |
| `scripts/bundle-macos.sh`, `scripts/test-bundle-macos.sh` (new test) | T1 part e | single owner |
| `Cargo.toml` | T1 part f | single owner |
| `README.md` | T1 part g | single owner |
| `.github/workflows/release.yml` | T2 stages 1–4 | one agent, in order; T2 depends on T1 |
| any P2 file (fix-ups) | T2 stage 5 | runs after everything else |

---

## Task T1: CI, build, supply chain and packaging

Owns, per part: see the task table. Step 0, worktrees, merges and integration are under "How T1 runs" above. Each part below is the old task's text with the pre-flight amendments applied.

### Part a, stage a1 (was T1): Split the build job out of `ci.yml`

**Files:**
- Modify: `.github/workflows/ci.yml` (delete lines 36-75, the `build:` job, unchanged by P1; P1's appended `audit:` job at lines 77-84 stays)
- Create: `.github/workflows/build.yml`

**Interfaces:**
- Produces: `.github/workflows/build.yml` with a single job `build` (T1 part a3 rewrites it) and `ci.yml` without a `build` job (T1 part a2 rewrites it).

- [ ] **Step 1: Baseline the linters** (installed in T1 Step 0; stop if `command -v actionlint zizmor shellcheck` fails):

Run: `actionlint .github/workflows/*.yml; GH_TOKEN=$(gh auth token) zizmor .github/ 2>&1 | tail -3`
Expected: actionlint may print shellcheck warnings (for example SC2086 on `>> $GITHUB_ENV` at ci.yml:72). zizmor prints a summary line such as `N findings (…)` including `unpinned-uses`, `artipacked`, `excessive-permissions`, `template-injection` (release.yml:116). Copy the summary line; it goes in the commit message.

- [ ] **Step 2: Create `.github/workflows/build.yml`** with the job moved verbatim, minus `needs: check` (a job cannot need a job in another workflow), and with `$GITHUB_ENV` quoted so actionlint's shellcheck pass (SC2086) is clean (pre-flight G1-3; T1 part a3 deletes that line anyway):

```yaml
name: Build

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

env:
  CARGO_TERM_COLOR: always

jobs:
  build:
    strategy:
      matrix:
        include:
          - target: aarch64-apple-darwin
            os: macos-14
          - target: x86_64-apple-darwin
            os: macos-14
          - target: x86_64-pc-windows-msvc
            os: windows-latest
          - target: aarch64-unknown-linux-gnu
            os: ubuntu-latest
          - target: x86_64-unknown-linux-gnu
            os: ubuntu-latest

    runs-on: ${{ matrix.os }}

    steps:
      - uses: actions/checkout@v4

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@stable
        with:
          targets: ${{ matrix.target }}

      - name: Rust cache
        uses: Swatinem/rust-cache@v2
        with:
          key: ci-${{ matrix.target }}

      - name: Install cross-compilation tools (Linux ARM)
        if: matrix.target == 'aarch64-unknown-linux-gnu'
        run: |
          sudo apt-get update
          sudo apt-get install -y gcc-aarch64-linux-gnu
          echo "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc" >> "$GITHUB_ENV"

      - name: Build
        run: cargo build --release --target ${{ matrix.target }}
```

- [ ] **Step 3: Delete the `build:` job from `ci.yml`.** Remove everything from the line `  build:` (line 36) through `        run: cargo build --release --target ${{ matrix.target }}` (line 75), plus the blank line before it. Leave `check:` and P1's `audit:` job untouched.

- [ ] **Step 4: Verify.**

Run: `grep -c '^  build:' .github/workflows/ci.yml; grep -c '^  build:' .github/workflows/build.yml; actionlint .github/workflows/ci.yml .github/workflows/build.yml`
Expected: `0`, `1`, and actionlint exit code 0 with no `-ignore` flag (the moved `>> "$GITHUB_ENV"` line is quoted, pre-flight G1-3). If actionlint reports anything on the `check` job left in `ci.yml`, record it in the commit; part a2 rewrites that file.

- [ ] **Step 5: Commit.**

```bash
git add .github/workflows/ci.yml .github/workflows/build.yml
git commit -m "ci: move release build matrix into build.yml

Baseline zizmor: <paste summary line from Step 1>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part a, stage a2 (was T2): Harden `ci.yml`

**Files:**
- Modify (full rewrite): `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: P1's `audit` job (reproduced below, now pinned) and P1's `#[ignore = "opens network sessions"]` tests.
- Produces: job ids `lint`, `test`, `network-tests`, `audit`. GitHub shows checks by each job's `name:`, so the required-status-check names are `Format and clippy`, `Test (ubuntu-latest)`, `Test (macos-latest)`, `Test (windows-latest)`, `Network-session tests (ignored set)` and `Security audit` (T2 stage 5 lists them in the GitHub-only checklist).

- [ ] **Step 1: Write the failing check.**

Run: `grep -nE 'uses: [^ ]+@[^0-9a-f]' .github/workflows/ci.yml; GH_TOKEN=$(gh auth token) zizmor .github/workflows/ci.yml`
Expected: grep lists `actions/checkout@v4`, `dtolnay/rust-toolchain@stable`, `Swatinem/rust-cache@v2`; zizmor reports `unpinned-uses`, `artipacked` and `excessive-permissions`.

- [ ] **Step 2: Replace `.github/workflows/ci.yml` with:**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash

env:
  CARGO_TERM_COLOR: always

jobs:
  lint:
    name: Format and clippy
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable
          components: rustfmt, clippy

      - name: Rust cache
        uses: Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2

      - name: Format
        run: cargo fmt --all -- --check

      - name: Clippy
        run: cargo clippy --all-targets --locked -- -D warnings

  test:
    name: Test (${{ matrix.os }})
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable

      - name: Rust cache
        uses: Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2

      - name: Test
        run: cargo test --locked

  network-tests:
    name: Network-session tests (ignored set)
    # These tests are #[ignore = "opens network sessions"]. They open real
    # peer-mode Zenoh sessions that listen on [::] (fixed ports 27501, 27601,
    # 27602, 27701, 27802), use default multicast scouting, and one test dials
    # the unroutable 10.255.255.1 to check that connect fails. Run them in one
    # Linux job only.
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable

      - name: Rust cache
        uses: Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2

      - name: Ignored tests
        run: cargo test --locked -- --ignored

  audit:
    name: Security audit
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable

      - name: Install cargo-audit
        run: cargo install cargo-audit --locked --version 0.22.2

      - name: Audit
        run: cargo audit
```

`cargo check` from the old `check` job (ci.yml:27-28) is dropped: `clippy` performs the same type-check. `cargo-audit` 0.22.2 is the latest `cargo-audit/v*` tag in rustsec/rustsec as of 2026-09-25 (`gh api repos/rustsec/rustsec/releases`).

- [ ] **Step 3: Verify locally.**

Run: `actionlint .github/workflows/ci.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/ci.yml && grep -nE 'uses: [^ ]+@[^0-9a-f]' .github/workflows/ci.yml; echo "grep exit $?"`
Expected: actionlint silent, zizmor output starting `No findings to report`, and `grep exit 1` (no tag-pinned uses).

Run (in the part-a worktree, `CARGO_TARGET_DIR="$P2RUN/tgt-a"`): `cargo clippy --all-targets --locked -- -D warnings && cargo test --locked && cargo test --locked -- --ignored && grep -c '27501' .github/workflows/ci.yml`
Expected: all pass, then `1` (the port list in the `network-tests` comment; today's `ci.yml` gives `0`; the `--ignored` set is P1's five network tests; they need free listen ports, multicast scouting and outbound TCP, not loopback only). Part a is the only T1 part that runs `--ignored`.

GitHub-only: `Format and clippy`, `Test (ubuntu-latest)`, `Test (macos-latest)`, `Test (windows-latest)`, `Network-session tests (ignored set)` and `Security audit` are green on the first PR. Local substitute: the cargo commands above on macOS. If Windows `cargo test` fails on a P1 test that is not `#[ignore]` (P1 made in-process test sessions bind no port, `d38410e`), that is a P1 bug to report, not a reason to drop Windows from the matrix. If a hosted runner blocks multicast or the listen ports, `Network-session tests` fails; report it rather than weakening the tests.

- [ ] **Step 4: Commit.**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: pin actions by SHA, least-privilege token, 3-OS test matrix, locked builds

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part a, stage a3 (was T3): Harden `build.yml` (cheap PRs, native ARM runner)

**Files:**
- Modify (full rewrite): `.github/workflows/build.yml`

**Interfaces:**
- Consumes: `build.yml` from T1 part a1.
- Produces: job `build` with matrix targets identical to `release.yml` (T2 stage 1 reuses the same runner labels).

**Runner choice:** `ubuntu-24.04-arm` is a standard GitHub-hosted Linux arm64 label for public and private repositories (https://docs.github.com/en/actions/reference/runners/github-hosted-runners, read 2026-09-25). A native runner is chosen over `cross` because `ring` (in `Cargo.lock`) compiles C through `cc`, which needs a target C toolchain; the native runner has one, and it avoids a Docker-based tool. `x86_64-apple-darwin` stays a cross-compile on `macos-14` (Apple's Xcode clang targets both architectures).

- [ ] **Step 1: Write the failing check.**

Run: `grep -c gcc-aarch64-linux-gnu .github/workflows/build.yml; GH_TOKEN=$(gh auth token) zizmor .github/workflows/build.yml | tail -1`
Expected: `1`, and zizmor reports findings (`unpinned-uses`, `artipacked`, `excessive-permissions`, `github-env`).

- [ ] **Step 2: Replace `.github/workflows/build.yml` with:**

```yaml
name: Build

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash

env:
  CARGO_TERM_COLOR: always

jobs:
  build:
    name: Build (${{ matrix.target }})
    strategy:
      fail-fast: false
      matrix:
        include:
          - target: aarch64-apple-darwin
            os: macos-14
          - target: x86_64-apple-darwin
            os: macos-14
          - target: x86_64-pc-windows-msvc
            os: windows-latest
          - target: aarch64-unknown-linux-gnu
            os: ubuntu-24.04-arm
          - target: x86_64-unknown-linux-gnu
            os: ubuntu-latest
    runs-on: ${{ matrix.os }}
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable
          targets: ${{ matrix.target }}

      - name: Rust cache
        uses: Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2
        with:
          key: build-${{ matrix.target }}
          save-if: ${{ github.ref == 'refs/heads/main' }}

      - name: Check (pull requests)
        if: github.event_name == 'pull_request'
        env:
          TARGET: ${{ matrix.target }}
        run: cargo check --locked --all-targets --target "$TARGET"

      - name: Release build (main)
        if: github.event_name == 'push'
        env:
          TARGET: ${{ matrix.target }}
        run: cargo build --release --locked --target "$TARGET"
```

- [ ] **Step 3: Verify locally.**

Run: `actionlint .github/workflows/build.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/build.yml && grep -c gcc-aarch64-linux-gnu .github/workflows/build.yml; grep -c 'ubuntu-24.04-arm' .github/workflows/build.yml`
Expected: actionlint silent, zizmor output starting `No findings to report`, then `0` and `1`.

Run (on the macOS dev machine, part-a worktree; the target was added in T1 Step 0): `rustup target list --installed | grep -qx x86_64-apple-darwin && cargo check --locked --all-targets --target x86_64-apple-darwin`
Expected: `Finished` with no errors.

GitHub-only (local substitute: the cross-target check above): on a PR, the five `Build (<target>)` jobs run `Check` and skip `Release build`; on a push to `main` they do the reverse; `Build (aarch64-unknown-linux-gnu)` shows runner `ubuntu-24.04-arm`.

- [ ] **Step 4: Commit.**

```bash
git add .github/workflows/build.yml
git commit -m "ci(build): check-only on PRs, release builds on main, native arm64 Linux runner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part b (was T4): MSRV workflow

**Files:**
- Create: `.github/workflows/msrv.yml`

**Interfaces:**
- Consumes: `rust-version = "1.88"` in `Cargo.toml:5` (P1 T1). Every locked crate declares `rust-version` ≤ 1.88 (pre-flight scan of the local registry).
- Future change (pre-flight G1-4): when P3 raises `rust-version` (planned 1.95 for egui/eframe 0.36), P3 must update this file's `toolchain:` input and job name ("Check on Rust …"), the README MSRV and `Cargo.toml` together, and P3's owned files must include `.github/workflows/msrv.yml`.

- [ ] **Step 1: Confirm the MSRV builds locally (this is the failing-first check if P1 set the wrong value).**

Run (part-b worktree, `CARGO_TARGET_DIR="$P2RUN/tgt-b"`; the toolchain was installed in T1 Step 0): `grep -n 'rust-version' Cargo.toml && rustup toolchain list | grep -q '^1.88' && cargo +1.88 check --locked --all-targets`
Expected: `rust-version = "1.88"` and `Finished`. If 1.88 fails to compile, stop and report: the MSRV claim from P1 is wrong and must be raised in `Cargo.toml` by a P1 follow-up, not by this task.

- [ ] **Step 2: Create `.github/workflows/msrv.yml`:**

```yaml
name: MSRV

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash

env:
  CARGO_TERM_COLOR: always

jobs:
  msrv:
    # Keep this toolchain equal to `rust-version` in Cargo.toml.
    name: Check on Rust 1.88
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install Rust 1.88
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: "1.88"

      - name: Rust cache
        uses: Swatinem/rust-cache@6323deb102c322ba6fcbdcafc7e3dddab59af2b6 # v2.9.2
        with:
          key: msrv

      - name: Check
        run: cargo check --locked --all-targets
```

- [ ] **Step 3: Verify.**

Run: `actionlint .github/workflows/msrv.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/msrv.yml && grep -q 'toolchain: "1.88"' .github/workflows/msrv.yml && grep -q 'rust-version = "1.88"' Cargo.toml && echo MSRV-consistent`
Expected: zizmor output starting `No findings to report`, then `MSRV-consistent`.

GitHub-only: `Check on Rust 1.88` is green. Local substitute: the `cargo +1.88 check` above.

- [ ] **Step 4: Commit.**

```bash
git add .github/workflows/msrv.yml
git commit -m "ci: add MSRV (Rust 1.88) check workflow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part c (was T5): Dependabot

**Files:**
- Create: `.github/dependabot.yml`

Syntax source: https://docs.github.com/en/code-security/dependabot/working-with-dependabot/dependabot-options-reference (`package-ecosystem: github-actions` with `directory: /` scans `.github/workflows`; `cooldown.default-days`; `groups`; `ignore.update-types`). Dependabot updates SHA-pinned actions and rewrites the trailing `# vX` comment.

- [ ] **Step 1: Create `.github/dependabot.yml`:**

```yaml
version: 2
updates:
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    groups:
      github-actions:
        patterns: ["*"]

  - package-ecosystem: cargo
    directory: /
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    open-pull-requests-limit: 5
    groups:
      cargo-minor-and-patch:
        update-types: [minor, patch]
    ignore:
      # egui/eframe 0.29 → 0.3x and rfd 0.14 → 0.1x are real ports tracked
      # in a later plan (deep review, "Deferred"). For 0.x crates a minor bump is breaking.
      - dependency-name: egui
        update-types: ["version-update:semver-major", "version-update:semver-minor"]
      - dependency-name: eframe
        update-types: ["version-update:semver-major", "version-update:semver-minor"]
      - dependency-name: rfd
        update-types: ["version-update:semver-major", "version-update:semver-minor"]
```

- [ ] **Step 2: Verify.**

Run: `check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml && zizmor .github/dependabot.yml`
Expected: `ok -- validation done` and zizmor output starting `No findings to report` (zizmor's `dependabot-cooldown` audit requires ≥ 7 days; `dependabot-execution` is not triggered).

GitHub-only (local substitute: the schema check above): after merge to `main`, Insights → Dependency graph → Dependabot lists `github-actions` and `cargo` with "Last checked".

- [ ] **Step 3: Commit.**

```bash
git add .github/dependabot.yml
git commit -m "ci: weekly Dependabot for actions and cargo with 7-day cooldown

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part d (was T6): Release tag validation script

**Files:**
- Create: `scripts/release-tag.sh`
- Create: `scripts/test-release-tag.sh`

**Interfaces:**
- Produces: `scripts/release-tag.sh <tag> [cargo-version]`.
  - With one argument: exits 0 and prints the tag if it matches `^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$`, else exits 1.
  - With two arguments: additionally exits 1 unless `tag == "v$cargo_version"`.
  - Wrong argument count: exits 2.
  - It never prints an unvalidated tag (a rejected tag could contain `::` workflow-command text).

- [ ] **Step 1: Write the failing test** `scripts/test-release-tag.sh`:

```bash
#!/usr/bin/env bash
# Table-driven tests for scripts/release-tag.sh. Run: bash scripts/test-release-tag.sh
set -uo pipefail

script="$(cd "$(dirname "$0")" && pwd)/release-tag.sh"
pass=0
fail=0

check() { # name expected_exit args...
  local name=$1 expected=$2
  shift 2
  "$script" "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" -eq "$expected" ]; then
    pass=$((pass + 1))
  else
    echo "FAIL $name: expected exit $expected, got $got" >&2
    fail=$((fail + 1))
  fi
}

check plain            0 v0.9.1
check prerelease       0 v1.2.3-rc.1
check matches-version  0 v0.9.1 0.9.1
check prerelease-match 0 v1.2.3-rc.1 1.2.3-rc.1
check no-v             1 0.9.1
check two-parts        1 v0.9
check injection        1 'v1.0.0$(id)'
check newline          1 $'v1.0.0\nfoo'
check space            1 'v1.0.0 x'
check mismatch         1 v0.9.2 0.9.1
check no-args          2

if [ "$fail" -eq 0 ]; then
  echo "all $pass cases passed"
else
  echo "$fail of $((pass + fail)) cases failed" >&2
  exit 1
fi
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `bash scripts/test-release-tag.sh`
Expected: exit 1 with `FAIL plain: expected exit 0, got 127` (script missing) and similar lines.

- [ ] **Step 3: Write `scripts/release-tag.sh`:**

```bash
#!/usr/bin/env bash
# Validate a release tag before any build uses it.
# Usage: scripts/release-tag.sh <tag> [cargo-version]
#   1 arg : tag must match ^v<major>.<minor>.<patch>(-<prerelease>)?$
#   2 args: tag must also equal "v<cargo-version>"
# Prints the tag on success. Never echoes a tag that failed validation.
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <tag> [cargo-version]" >&2
  exit 2
fi

tag=$1
re='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$'

if ! [[ $tag =~ $re ]]; then
  echo "::error title=Invalid release tag::the tag does not match $re" >&2
  exit 1
fi

if [ "$#" -eq 2 ]; then
  version=$2
  if [ "$tag" != "v$version" ]; then
    echo "::error title=Tag/version mismatch::tag $tag but Cargo.toml version is $version" >&2
    exit 1
  fi
fi

echo "$tag"
```

Run: `chmod +x scripts/release-tag.sh scripts/test-release-tag.sh`

- [ ] **Step 4: Run tests and lint.**

Run: `bash scripts/test-release-tag.sh && shellcheck scripts/release-tag.sh scripts/test-release-tag.sh`
Expected: `all 11 cases passed`, shellcheck silent.

- [ ] **Step 5: Commit.**

```bash
git add scripts/release-tag.sh scripts/test-release-tag.sh
git commit -m "build: add release tag validation script with tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part e (was T7): macOS `.app` bundle script

**Files:**
- Modify: `scripts/bundle-macos.sh` (whole file, 43 lines today)
- Create: `scripts/test-bundle-macos.sh`
- Read only: `assets/Info.plist` (its `CFBundleVersion`/`CFBundleShortVersionString` are stale at `0.1.0`, lines 12-15; the script overwrites them in the bundle copy, so the asset file is not edited)

**Choice: keep the hand-written `Info.plist` + script, not `cargo-bundle`.** The repository already has a working `scripts/bundle-macos.sh` and `assets/Info.plist` with the bundle identifier `io.dad.zenoh-explorer`. `cargo-bundle` would add a tool install to every release job and a second source of bundle metadata (`[package.metadata.bundle]`) that must be kept in sync with the plist. The script needs only macOS built-ins (`plutil`, `cp`), which the test below exercises locally.

**Interfaces:**
- Produces: `scripts/bundle-macos.sh [binary] [out-dir] [version]`
  - `binary` default `target/release/zenoh-explorer`; `out-dir` default `target`; `version` default = `cargo metadata --no-deps --format-version 1 | jq -r '.packages[0].version'`.
  - Creates `<out-dir>/Zenoh Explorer.app` with `Contents/MacOS/zenoh-explorer` (always this name, matching `CFBundleExecutable`), `Contents/Info.plist` with both version keys set to `version`, and the icon if `assets/ZenohExplorer.icns` exists.
  - Prints the bundle path as its last stdout line.

- [ ] **Step 1: Write the failing test** `scripts/test-bundle-macos.sh`:

```bash
#!/usr/bin/env bash
# Tests scripts/bundle-macos.sh on a fake binary. macOS only (needs plutil).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

printf '#!/bin/sh\necho fake\n' > "$tmp/some-binary"
chmod +x "$tmp/some-binary"

out="$("$here/bundle-macos.sh" "$tmp/some-binary" "$tmp/out" 1.2.3-rc.1 | tail -n1)"
app="$tmp/out/Zenoh Explorer.app"

[ "$out" = "$app" ] || { echo "FAIL: printed '$out', expected '$app'" >&2; exit 1; }
[ -x "$app/Contents/MacOS/zenoh-explorer" ] || { echo "FAIL: executable missing" >&2; exit 1; }
plutil -lint "$app/Contents/Info.plist" >/dev/null
v1="$(plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist")"
v2="$(plutil -extract CFBundleVersion raw "$app/Contents/Info.plist")"
[ "$v1" = "1.2.3-rc.1" ] || { echo "FAIL: short version '$v1'" >&2; exit 1; }
[ "$v2" = "1.2.3-rc.1" ] || { echo "FAIL: bundle version '$v2'" >&2; exit 1; }
exe="$(plutil -extract CFBundleExecutable raw "$app/Contents/Info.plist")"
[ "$exe" = "zenoh-explorer" ] || { echo "FAIL: CFBundleExecutable '$exe'" >&2; exit 1; }

# The source asset must not be modified.
grep -q '<string>0.1.0</string>' "$here/../assets/Info.plist" || { echo "FAIL: assets/Info.plist was edited" >&2; exit 1; }

echo "bundle test passed"
```

- [ ] **Step 2: Run it to verify it fails.**

Run: `chmod +x scripts/test-bundle-macos.sh && bash scripts/test-bundle-macos.sh`
Expected: FAIL. The current script ignores arguments 2 and 3 and writes to `target/Zenoh Explorer.app`, so the test prints `FAIL: printed 'Bundle created at: …'`.

This red run deletes and replaces `$PROJECT_ROOT/target/Zenoh Explorer.app` with a fake binary (`scripts/bundle-macos.sh:13,24,31`). In the part-e worktree that is the worktree's own, otherwise empty `target/`, so it is harmless there. Never run the red step in the main checkout, where it would overwrite the developer's real bundle (pre-flight G2-4). Delete the worktree's `target/` afterwards: `rm -rf target`.

- [ ] **Step 3: Replace `scripts/bundle-macos.sh` with:**

```bash
#!/bin/bash
set -euo pipefail

# Assembles a macOS .app bundle from a compiled binary.
# Usage: ./scripts/bundle-macos.sh [binary] [out-dir] [version]
#   binary   default: target/release/zenoh-explorer
#   out-dir  default: target
#   version  default: package version from `cargo metadata`
# Prints the bundle path as the last line of stdout.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BINARY="${1:-$PROJECT_ROOT/target/release/zenoh-explorer}"
OUT_DIR="${2:-$PROJECT_ROOT/target}"
VERSION="${3:-}"
APP_NAME="Zenoh Explorer"
EXECUTABLE="zenoh-explorer" # must equal CFBundleExecutable in assets/Info.plist

if [ ! -f "$BINARY" ]; then
    echo "Error: Binary not found at $BINARY" >&2
    echo "Run 'cargo build --release' first." >&2
    exit 1
fi

if [ -z "$VERSION" ]; then
    VERSION="$(cd "$PROJECT_ROOT" && cargo metadata --no-deps --format-version 1 | jq -r '.packages[0].version')"
fi

mkdir -p "$OUT_DIR"
BUNDLE_DIR="$(cd "$OUT_DIR" && pwd)/${APP_NAME}.app"

echo "Assembling ${APP_NAME}.app (version ${VERSION}) ..." >&2
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/Contents/MacOS" "$BUNDLE_DIR/Contents/Resources"

cp "$BINARY" "$BUNDLE_DIR/Contents/MacOS/$EXECUTABLE"
chmod +x "$BUNDLE_DIR/Contents/MacOS/$EXECUTABLE"

cp "$PROJECT_ROOT/assets/Info.plist" "$BUNDLE_DIR/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$BUNDLE_DIR/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$BUNDLE_DIR/Contents/Info.plist"

if [ -f "$PROJECT_ROOT/assets/ZenohExplorer.icns" ]; then
    cp "$PROJECT_ROOT/assets/ZenohExplorer.icns" "$BUNDLE_DIR/Contents/Resources/"
else
    echo "Warning: No icon found at assets/ZenohExplorer.icns (app will use default icon)" >&2
fi

echo "$BUNDLE_DIR"
```

`CFBundleVersion` is documented by Apple as a period-separated list of integers; a prerelease string such as `1.2.3-rc.1` is accepted by `plutil` and by Gatekeeper but App Store tools would reject it. The app is not distributed through the App Store, so the full version is used for both keys.

- [ ] **Step 4: Run tests and lint.**

Run: `bash scripts/test-bundle-macos.sh && shellcheck scripts/bundle-macos.sh scripts/test-bundle-macos.sh`
Expected: `bundle test passed`, shellcheck silent.

Run (default arguments still work; part e runs no release build, so a stand-in binary sits at the default path in the worktree, change N2):
`mkdir -p target/release && printf '#!/bin/sh\necho stand-in\n' > target/release/zenoh-explorer && chmod +x target/release/zenoh-explorer && scripts/bundle-macos.sh | tail -n1 && plutil -extract CFBundleShortVersionString raw "target/Zenoh Explorer.app/Contents/Info.plist" && rm -rf target`
Expected: `…/wt-e/target/Zenoh Explorer.app` and the `Cargo.toml` version (`0.9.1`). The same script runs on the real release binary in the T1 integration run.

- [ ] **Step 5: Commit.**

```bash
git add scripts/bundle-macos.sh scripts/test-bundle-macos.sh
git commit -m "build(macos): bundle script takes out-dir and stamps version into Info.plist

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Part f (was T8): Release profile with separate debug symbols

**Files:**
- Modify: `Cargo.toml` `[profile.release]` (lines 27-32, the last section of the file after P1)

**Decision and justification:**
- **Keep `panic = "abort"`.** The Windows panic hook at `src/main.rs:28-41` runs before the abort, so crash logs still get written. With unwinding, a panic on the tokio worker thread would kill only that thread and leave the UI showing a stale connection state; aborting makes such failures visible. It also keeps the binary smaller.
- **Add `debug = "line-tables-only"`.** This is the minimum that gives file:line in backtraces (https://doc.rust-lang.org/cargo/reference/profiles.html#debug).
- **Add `split-debuginfo = "packed"`.** It moves that debug info into a `.dSYM` (macOS), `.pdb` (Windows MSVC) or `.dwp` (Linux) next to the binary (https://doc.rust-lang.org/rustc/codegen-options/index.html#split-debuginfo). T2 stage 4 uploads these as release assets so user crash reports can be symbolized without shipping large binaries.
- **Change `strip = true` to `strip = "none"`.** `strip = true` means `"symbols"`, which rustc's docs warn makes backtraces "incomprehensible" for anything that wants crash reporting (https://doc.rust-lang.org/rustc/codegen-options/index.html#strip). With packed split debuginfo the DWARF is already outside the binary, so keeping the symbol table costs little. On Linux it also keeps the skeleton units the `.dwp` needs to match the binary.

**Interfaces:**
- Produces (for T2 stage 4): after `cargo build --release --target <t>`, `target/<t>/release/zenoh-explorer.dSYM` (macOS), `target/<t>/release/zenoh_explorer.pdb` (Windows), and a `*.dwp` file within 2 directory levels of `target/<t>/release` (Linux; the exact name is confirmed in CI by T2 stage 4).

All paths below are under the part-f worktree's `CARGO_TARGET_DIR` (`$P2RUN/tgt-f`), not `target/` (change N2).

- [ ] **Step 1: Record the baseline.**

Run: `cargo build --release --locked && ls -l "$CARGO_TARGET_DIR/release/zenoh-explorer" | awk '{print $5}' && find "$CARGO_TARGET_DIR/release" -maxdepth 2 -name 'zenoh-explorer*.dSYM'`
Expected: a byte size (note it) and no dSYM path printed. This is the failing check.

- [ ] **Step 2: Edit `Cargo.toml`.** Replace

```toml
panic = "abort"
strip = true
```

with

```toml
panic = "abort"
# Line tables go to a separate .dSYM/.pdb/.dwp file that the release workflow
# uploads as a debug-symbols asset. The symbol table stays in the binary so that
# RUST_BACKTRACE output names functions.
debug = "line-tables-only"
split-debuginfo = "packed"
strip = "none"
```

- [ ] **Step 3: Build and verify the symbols.**

Run: `T="$CARGO_TARGET_DIR/release"; cargo build --release --locked && ls -l "$T/zenoh-explorer" | awk '{print $5}' && dsym="$(find "$T" -maxdepth 2 -name 'zenoh-explorer*.dSYM' | head -n1)" && echo "$dsym" && dwarfdump --uuid "$T/zenoh-explorer" && dwarfdump --uuid "$dsym" && nm "$T/zenoh-explorer" | grep -c zenoh_explorer`
Expected: the new size (note it), the dSYM path (normally `…/release/zenoh-explorer.dSYM`), two identical `UUID:` lines, and a count > 0.

Record the dSYM path relative to the target directory (for example `release/zenoh-explorer.dSYM`, or a `release/deps/…` path) in the commit message; T2 stage 4 reads it from there. If no dSYM exists at all, fall back to `split-debuginfo` unset (the rustc default on macOS is `packed`) and record that.

- [ ] **Step 4: Check that tests and clippy are unaffected.**

Run: `cargo test --locked && cargo clippy --all-targets --locked -- -D warnings`
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add Cargo.toml
git commit -m "build(profile): keep panic=abort; split line-table debuginfo; keep symbol table

macOS arm64 release binary: <before> bytes -> <after> bytes.
dSYM: <path relative to the target directory>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Replace `<before>`/`<after>` with the numbers from Steps 1 and 3, and `<path…>` with the dSYM path from Step 3.)

---
### Part g (was T9): README installation, features and troubleshooting

**Files:**
- Modify: `README.md` lines 26-32 (Features: publishing and query bullets), 43-54 (the `## Installation` section through the closing fence of the build snippet) and 70-82 (`## Troubleshooting` through the "Enable the built-in queryable" bullet). Edit bottom-up (70-82, then 43-54, then 26-32) or by the exact anchors given, so earlier line numbers stay valid.
- Read only: `src/ui/help.rs:16-56` (the Help text P1 T28 made match the app), `src/ui/publish.rs:12,357`, `src/app/mod.rs:298`, `src/ui/query.rs:77`.

**Interfaces:**
- Consumes: asset names and secrets from Global Constraints. The repository URL is `https://github.com/zenoh-project/zenoh-explorer`, taken from `Cargo.toml:9` (`repository = …`). Part g starts only after T1 Step 0 has an answer to Open question 1; if the user gives a different URL, use theirs everywhere below.

- [ ] **Step 1: Write the failing check.**

Run: `grep -c 'Rust 1.70' README.md; grep -c '<repository-url>' README.md; grep -c 'checksums-sha256.txt' README.md; grep -cE 'Worker Unresponsive|No queryables available|In the Query tab, enable|5GB|default: 5 seconds' README.md`
Expected: `1`, `1`, `0`, `5`.

- [ ] **Step 2: Replace README lines 43-54** (from `## Installation` through the closing fence after `cargo build --release`) with:

````markdown
## Installation

### Download a release

Each [GitHub release](https://github.com/zenoh-project/zenoh-explorer/releases) has these assets:

| Platform | Asset |
|---|---|
| macOS, Apple silicon | `zenoh-explorer-aarch64-apple-darwin.zip` (contains `Zenoh Explorer.app`) |
| macOS, Intel | `zenoh-explorer-x86_64-apple-darwin.zip` (contains `Zenoh Explorer.app`) |
| Windows x64 | `zenoh-explorer-x86_64-pc-windows-msvc.zip` |
| Linux x64 | `zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz` |
| Linux arm64 | `zenoh-explorer-aarch64-unknown-linux-gnu.tar.gz` |

`checksums-sha256.txt` lists the SHA-256 of every asset. The `*-debug-symbols.*` assets are only needed to symbolize crash backtraces.

**Verify the checksum** (macOS/Linux) before running a download:

```bash
grep ' zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz$' checksums-sha256.txt | shasum -a 256 -c -
```

On Windows (PowerShell), compare the output with the line in `checksums-sha256.txt`:

```powershell
(Get-FileHash .\zenoh-explorer-x86_64-pc-windows-msvc.zip -Algorithm SHA256).Hash.ToLower()
```

**Verify the build provenance** (optional, needs the [GitHub CLI](https://cli.github.com/); attestations exist only for releases built from a public repository):

```bash
gh attestation verify zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz --repo zenoh-project/zenoh-explorer
```

**Unsigned builds.** Releases are code-signed only when the maintainers' signing credentials are configured. For an unsigned build:
- macOS: Gatekeeper blocks the first launch. Right-click `Zenoh Explorer.app` → Open, or run `xattr -d com.apple.quarantine "Zenoh Explorer.app"`.
- Windows: SmartScreen shows "Windows protected your PC". Choose More info → Run anyway.

### Building from source

Prerequisites: Rust 1.88 or later.

```bash
git clone https://github.com/zenoh-project/zenoh-explorer.git
cd zenoh-explorer
cargo build --release --locked
```

On macOS, `scripts/bundle-macos.sh` wraps the built binary into `target/Zenoh Explorer.app`.
````

- [ ] **Step 2b: Make Troubleshooting and Features match the app** (pre-flight G3-1, N1). After P1, README.md:77 names a "Worker Unresponsive" state that no longer exists (`grep -rn Unresponsive src` is empty); README.md:81-82 name a "No queryables available" alert (the app says "No replies", `src/ui/help.rs:54`) and put the queryable toggle in the Query tab (it is in the Publish tab, `src/ui/publish.rs:357`); README.md:30 says the query timeout defaults to 5 seconds (`src/app/mod.rs:298` sets 10000 ms); README.md:28 promises "5GB+ file support" while the app reads an imported file into memory (`src/ui/help.rs` step 5).

Replace README lines 70-82, from `## Troubleshooting` through the bullet that starts `- **Enable the built-in queryable**`, with:

````markdown
## Troubleshooting

### Connecting
- **Peer mode** (the default) finds other peers on the local network by multicast (UDP 7446). Leave the address empty for multicast discovery, or give an endpoint in `tcp/ip:port` form. Use a different Listen Port for each copy of the app on one machine.
- **Client mode** connects to a router and needs its address (for example `localhost`, port 7447).
- **Connection Retry Behavior**: When you specify a TCP locator in peer mode (e.g., `tcp/localhost:7447`), Zenoh will continuously attempt to connect to that endpoint with exponential backoff. This is normal behavior - Zenoh peers persistently try to establish connections to configured endpoints, even if they're unreachable. The retry period starts at 1 second and increases (1s, 2s, 4s, 4s...) up to a maximum period. This ensures peers can automatically reconnect when endpoints become available.
- **Connection error**: the red message in the connection panel, above the Connect button, names the cause; the header shows its first words.
- **Connected but the tree stays empty**: check the peer count in the header ("no peers" means no Zenoh peer or router is linked to this app). If the header says "monitor off", subscribe to a key expression such as `demo/**`.
- For more detail, run the app with `RUST_LOG=zenoh_explorer=info`.

### Query Functionality
- Queries need a queryable on the network whose key expression matches.
- A query that says "No replies" matched no queryable, or the ones that matched had nothing to return. A timeout with no replies is shown as an error.
- **Built-in queryable**: in the Publish tab, turn on Enable Queryable. This app then answers queries with the last value it published on each key (typed text only, up to 10 MB; not imported files).
````

The "Connection Retry Behavior" bullet is the current README text, kept verbatim; the other bullets restate `src/ui/help.rs:18-19,50-56` and `src/ui/publish.rs:12`.

Then replace README lines 26-32, from `- **Data Publishing**` through `  - Test request/response patterns without external services`, with:

````markdown
- **Data Publishing**: Send test data to any key in the network
  - Support for different encodings
  - **File import**: Import a file and publish it (the whole file is read into memory; payloads above 64 MiB are sent in chunks)
  - **Built-in queryable** (Publish tab): answers queries with the last value this app published on each key (typed text only, up to 10 MB; not imported files)
- **Query Interface**: Request data from the network with a configurable timeout (default: 10 seconds)
  - Test request/response patterns against the built-in queryable without external services
````

- [ ] **Step 3: Verify.**

Run: `grep -c 'Rust 1.70' README.md; grep -c '<repository-url>' README.md; grep -c 'checksums-sha256.txt' README.md; grep -c 'gh attestation verify' README.md; grep -cE 'Worker Unresponsive|No queryables available|In the Query tab, enable|5GB|default: 5 seconds' README.md`
Expected: `0`, `0`, `3`, `1`, `0` (`checksums-sha256.txt` is on three lines of the new block).

Run (proves the checksum command works on this machine's `shasum`; run it in a subshell so the agent's working directory stays in the worktree): `( cd "$(mktemp -d)" && echo hi > zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz && shasum -a 256 zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz > checksums-sha256.txt && grep ' zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz$' checksums-sha256.txt | shasum -a 256 -c - )`
Expected: `zenoh-explorer-x86_64-unknown-linux-gnu.tar.gz: OK`.

- [ ] **Step 4: Commit.**

```bash
git add README.md
git commit -m "docs(readme): download/verify instructions, MSRV 1.88, real clone URL; features and troubleshooting match the app

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task T2: Release workflow and integration

Owns `.github/workflows/release.yml`; stage 5 may fix up any P2 file. Runs as one agent, stages in order (see "How T2 runs"). `CARGO_TARGET_DIR="$P2RUN/tgt-int"` throughout.

### Stage 1 (was T10): Restructure `release.yml` (tag validation, least privilege, pins)

**Files:**
- Modify (full rewrite): `.github/workflows/release.yml` (132 lines today)

Findings fixed:
- `release.yml:115-116`: `${{ github.event.inputs.tag }}` inside `run:` is script injection → `env:` + `scripts/release-tag.sh`.
- `release.yml:18`: no top-level `permissions:` → top-level `contents: read`.
- `release.yml:46,49,54,87,99,102,122`: tag pins → SHA pins; `softprops/action-gh-release` (line 122) → `gh release create`.
- `release.yml:53-56`: `rust-cache` in a release workflow → removed (zizmor `cache-poisoning`).
- `release.yml:58-63`: gcc cross linker → native `ubuntu-24.04-arm`.
- `release.yml:66`: no `--locked`.
- `release.yml:73,83`: `|| true` / `-ErrorAction SilentlyContinue` hide a missing `LICENSE`.
- Dispatch built the dispatched branch, not the tag → every job checks out `refs/tags/<tag>`.

**Interfaces:**
- Consumes: `scripts/release-tag.sh` (T1 part d).
- Produces (for T2 stage 2–T2 stage 4):
  - Job ids `prepare` (output `tag`), `build` (matrix with `target`, `os`, `archive`), `release`.
  - Named steps in `build`: `Build`, `Package (Linux)`, `Package (macOS)`, `Package (Windows)`, `Upload artifact`.
  - Named steps in `release`: `Download all artifacts`, `Generate checksums`, `Create release`.

- [ ] **Step 1: Write the failing check.**

Run: `GH_TOKEN=$(gh auth token) zizmor .github/workflows/release.yml | grep -E 'template-injection|cache-poisoning|unpinned-uses' | head; grep -c '|| true' .github/workflows/release.yml`
Expected: at least one `template-injection` line (release.yml:116), `cache-poisoning`, `unpinned-uses`; and `1`.

- [ ] **Step 2: Replace `.github/workflows/release.yml` with:**

```yaml
name: Build and Release

on:
  push:
    tags:
      - 'v*'
  workflow_dispatch:
    inputs:
      tag:
        description: 'Existing tag to release (e.g. v1.0.0); must equal "v" + the Cargo.toml version'
        required: true
        type: string

permissions:
  contents: read

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

defaults:
  run:
    shell: bash

env:
  CARGO_TERM_COLOR: always
  BINARY_NAME: zenoh-explorer

jobs:
  prepare:
    name: Validate tag
    runs-on: ubuntu-latest
    timeout-minutes: 10
    outputs:
      tag: ${{ steps.version.outputs.tag }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Validate tag format
        id: format
        env:
          EVENT_NAME: ${{ github.event_name }}
          INPUT_TAG: ${{ inputs.tag }}
        run: |
          if [ "$EVENT_NAME" = "workflow_dispatch" ]; then
            tag="$INPUT_TAG"
          else
            tag="$GITHUB_REF_NAME"
          fi
          scripts/release-tag.sh "$tag" > /dev/null
          echo "tag=$tag" >> "$GITHUB_OUTPUT"

      - name: Check out the tag
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: refs/tags/${{ steps.format.outputs.tag }}
          persist-credentials: false

      - name: Tag equals Cargo.toml version
        id: version
        env:
          TAG: ${{ steps.format.outputs.tag }}
        run: |
          version="$(cargo metadata --no-deps --format-version 1 | jq -r '.packages[0].version')"
          scripts/release-tag.sh "$TAG" "$version" > /dev/null
          echo "tag=$TAG" >> "$GITHUB_OUTPUT"

  build:
    name: Build (${{ matrix.target }})
    needs: prepare
    strategy:
      fail-fast: false
      matrix:
        include:
          - target: aarch64-apple-darwin
            os: macos-14
            archive: tar.gz
          - target: x86_64-apple-darwin
            os: macos-14
            archive: tar.gz
          - target: x86_64-pc-windows-msvc
            os: windows-latest
            archive: zip
          - target: aarch64-unknown-linux-gnu
            os: ubuntu-24.04-arm
            archive: tar.gz
          - target: x86_64-unknown-linux-gnu
            os: ubuntu-latest
            archive: tar.gz
    runs-on: ${{ matrix.os }}
    timeout-minutes: 90
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: refs/tags/${{ needs.prepare.outputs.tag }}
          persist-credentials: false

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@02cb101ec7c40f2c49e1d9714d64511d8e1b74de # v1
        with:
          toolchain: stable
          targets: ${{ matrix.target }}

      - name: Build
        env:
          TARGET: ${{ matrix.target }}
        run: cargo build --release --locked --target "$TARGET"

      - name: Package (Linux)
        if: runner.os == 'Linux'
        env:
          TARGET: ${{ matrix.target }}
        run: |
          mkdir -p dist
          cp "target/$TARGET/release/$BINARY_NAME" dist/
          cp README.md LICENSE dist/
          cd dist
          tar -czvf "../$BINARY_NAME-$TARGET.tar.gz" -- *

      - name: Package (macOS)
        if: runner.os == 'macOS'
        env:
          TARGET: ${{ matrix.target }}
        run: |
          mkdir -p dist
          cp "target/$TARGET/release/$BINARY_NAME" dist/
          cp README.md LICENSE dist/
          cd dist
          tar -czvf "../$BINARY_NAME-$TARGET.tar.gz" -- *

      - name: Package (Windows)
        if: runner.os == 'Windows'
        shell: pwsh
        env:
          TARGET: ${{ matrix.target }}
        run: |
          New-Item -ItemType Directory -Force -Path dist | Out-Null
          Copy-Item "target/$env:TARGET/release/$env:BINARY_NAME.exe" -Destination dist/ -ErrorAction Stop
          Copy-Item README.md, LICENSE -Destination dist/ -ErrorAction Stop
          Compress-Archive -Path dist/* -DestinationPath "$env:BINARY_NAME-$env:TARGET.zip"

      - name: Upload artifact
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: ${{ env.BINARY_NAME }}-${{ matrix.target }}
          path: ${{ env.BINARY_NAME }}-${{ matrix.target }}.${{ matrix.archive }}
          if-no-files-found: error

  release:
    name: Publish release
    needs: [prepare, build]
    runs-on: ubuntu-latest
    timeout-minutes: 15
    permissions:
      contents: write # create the GitHub release and upload its assets
    steps:
      - name: Download all artifacts
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          path: artifacts
          merge-multiple: true

      - name: Generate checksums
        run: |
          cd artifacts
          sha256sum -- * > checksums-sha256.txt
          cat checksums-sha256.txt

      - name: Create release
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          TAG: ${{ needs.prepare.outputs.tag }}
        run: |
          flags=(--title "Release $TAG" --generate-notes --verify-tag)
          if [[ $TAG == *-* ]]; then flags+=(--prerelease); fi
          gh release create "$TAG" "${flags[@]}" artifacts/*
```

Notes for the implementer:
- `Create release` uses the `gh` CLI preinstalled on GitHub-hosted runners instead of `softprops/action-gh-release`, which zizmor 1.30.1's `superfluous-actions` audit flags (pre-flight G2-1). The step keeps the name `Create release` because stage 4 inserts its steps before it. Mapping of the old inputs: `name` → `--title`, `generate_release_notes` → `--generate-notes`, `prerelease` when the tag contains `-` → `--prerelease`, `files: artifacts/*` → the `artifacts/*` arguments; `fail_on_unmatched_files` is implicit, because `gh` fails when a file argument does not exist (an unmatched glob stays literal). `--verify-tag` makes `gh` refuse to create a tag that is not already on the remote. The token comes from `GH_TOKEN: ${{ github.token }}` in `env:`; the job has no checkout, so `GH_REPO` names the repository.
- Dispatch only from a branch that contains `scripts/release-tag.sh`, normally `main` (pre-flight G2-6). The first checkout in `prepare` has no `ref:`, so on `workflow_dispatch` `Validate tag format` runs the script from the dispatching branch; a branch that predates T1 part d fails with "No such file". The release still stops, but the message is misleading.
- `upload-artifact` v7.0.1 still has `name`, `path` and `if-no-files-found`; `download-artifact` v8.0.1 still has `path` and `merge-multiple` (both checked in their `action.yml` at the pinned SHA).
- The workflow-level `BINARY_NAME` env is read as a shell variable in `run:`. That is not a template expansion, so zizmor does not flag it.

- [ ] **Step 3: Verify locally.**

Run: `actionlint .github/workflows/release.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/release.yml`
Expected: actionlint silent, zizmor output starting `No findings to report`.

Run: `for p in 'github.event.inputs' '|| true' 'SilentlyContinue' 'rust-cache' 'gcc-aarch64' 'softprops'; do printf '%s: ' "$p"; grep -cF -- "$p" .github/workflows/release.yml; done`
Expected: every count `0`.

Run (simulate the prepare job's logic locally): `v=$(cargo metadata --no-deps --format-version 1 | jq -r '.packages[0].version'); scripts/release-tag.sh "v$v" "$v" && ! scripts/release-tag.sh 'v1.0.0$(id)' 2>/dev/null && echo prepare-logic-ok`
Expected: `v0.9.1` (the current version) then `prepare-logic-ok`.

GitHub-only (local substitute: the prepare-logic simulation above and T1 part d's 11 cases): dispatching with tag `v0.0.0-x` fails in `Validate tag` at the `Tag equals Cargo.toml version` step (or at `Check out the tag` if that tag does not exist), and no `build` job starts.

- [ ] **Step 4: Commit.**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): validate tag via env (fix script injection), least privilege, SHA pins, no cache, locked builds

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Stage 2 (was T11): macOS `.app`, code signing and notarization

**Files:**
- Modify: `.github/workflows/release.yml` (the `build` job's matrix `archive` for the two macOS rows, a new `environment:`/`env:` block on the `build` job, and the `Package (macOS)` step replaced by the steps below)

**Interfaces:**
- Consumes: `scripts/bundle-macos.sh <binary> <out-dir> <version>` (T1 part e), which prints the bundle path; step names from T2 stage 1.
- Produces: macOS assets `zenoh-explorer-<target>.zip`; job-level env `HAS_APPLE_SIGNING` (`'true'`/`'false'`).

Commands verified against Apple's documentation for `notarytool` ("Customizing the notarization workflow", https://developer.apple.com/documentation/security/customizing-the-notarization-workflow) and `codesign --options runtime` (hardened runtime is required for notarization).

- [ ] **Step 1: Write the failing check.**

Run: `grep -c 'HAS_APPLE_SIGNING' .github/workflows/release.yml; grep -c 'notarytool submit' .github/workflows/release.yml`
Expected: `0`, `0`.

- [ ] **Step 2: Change the two macOS matrix rows** from `archive: tar.gz` to `archive: zip`:

```yaml
          - target: aarch64-apple-darwin
            os: macos-14
            archive: zip
          - target: x86_64-apple-darwin
            os: macos-14
            archive: zip
```

- [ ] **Step 3: Add the environment and signing flag to the `build` job**, directly after `timeout-minutes: 90`:

```yaml
    # Signing secrets live in the "release" environment. Forks and repositories
    # without them build unsigned artifacts and print a warning instead of failing.
    environment: release
    env:
      HAS_APPLE_SIGNING: ${{ secrets.MACOS_CERTIFICATE_P12 != '' && secrets.APPLE_APP_PASSWORD != '' }}
```

Notes on this block (pre-flight G2-5):
- `environment: release` sits on the whole `build` job, so all five matrix rows, Linux included, create a deployment to `release`. If the user adds required reviewers to that environment, all five jobs wait for approval. If the user adds a deployment rule that allows only `v*` tags, `workflow_dispatch` runs are refused, because GitHub checks the run's ref, which is the dispatching branch. Both caveats go into the stage 5 checklist; Open question 4 offers a per-row environment instead.
- The gate checks only `MACOS_CERTIFICATE_P12` and `APPLE_APP_PASSWORD`. A partial set (those two present, but `MACOS_SIGN_IDENTITY`, `APPLE_ID` or `APPLE_TEAM_ID` missing) fails the job on purpose instead of warning: a maintainer who configured signing should learn it is broken rather than ship an unsigned build silently.

- [ ] **Step 4: Replace the whole `Package (macOS)` step** (from T2 stage 1) with these steps, in this order:

```yaml
      - name: Bundle Zenoh Explorer.app (macOS)
        if: runner.os == 'macOS'
        env:
          TARGET: ${{ matrix.target }}
          TAG: ${{ needs.prepare.outputs.tag }}
        run: |
          scripts/bundle-macos.sh "target/$TARGET/release/$BINARY_NAME" dist "${TAG#v}"

      - name: Import signing certificate (macOS)
        if: runner.os == 'macOS' && env.HAS_APPLE_SIGNING == 'true'
        env:
          MACOS_CERTIFICATE_P12: ${{ secrets.MACOS_CERTIFICATE_P12 }}
          MACOS_CERTIFICATE_PASSWORD: ${{ secrets.MACOS_CERTIFICATE_PASSWORD }}
        run: |
          keychain="$RUNNER_TEMP/signing.keychain-db"
          keychain_password="$(openssl rand -base64 24)"
          security create-keychain -p "$keychain_password" "$keychain"
          security set-keychain-settings -lut 21600 "$keychain"
          security unlock-keychain -p "$keychain_password" "$keychain"
          printf '%s' "$MACOS_CERTIFICATE_P12" | base64 --decode > "$RUNNER_TEMP/cert.p12"
          security import "$RUNNER_TEMP/cert.p12" -k "$keychain" -P "$MACOS_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
          rm -f "$RUNNER_TEMP/cert.p12"
          security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$keychain_password" "$keychain"
          security list-keychains -d user -s "$keychain" "$HOME/Library/Keychains/login.keychain-db"

      - name: Code sign (macOS)
        if: runner.os == 'macOS' && env.HAS_APPLE_SIGNING == 'true'
        env:
          MACOS_SIGN_IDENTITY: ${{ secrets.MACOS_SIGN_IDENTITY }}
        run: |
          codesign --force --options runtime --timestamp --sign "$MACOS_SIGN_IDENTITY" "dist/Zenoh Explorer.app"
          codesign --verify --strict --verbose=2 "dist/Zenoh Explorer.app"

      - name: Notarize and staple (macOS)
        if: runner.os == 'macOS' && env.HAS_APPLE_SIGNING == 'true'
        env:
          APPLE_ID: ${{ secrets.APPLE_ID }}
          APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
          APPLE_APP_PASSWORD: ${{ secrets.APPLE_APP_PASSWORD }}
        run: |
          ditto -c -k --keepParent "dist/Zenoh Explorer.app" "$RUNNER_TEMP/notarize.zip"
          xcrun notarytool submit "$RUNNER_TEMP/notarize.zip" \
            --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" \
            --wait --timeout 30m
          xcrun stapler staple "dist/Zenoh Explorer.app"
          spctl --assess --type execute --verbose=2 "dist/Zenoh Explorer.app"

      - name: Ad-hoc sign unsigned build (macOS)
        if: runner.os == 'macOS' && env.HAS_APPLE_SIGNING != 'true'
        run: |
          echo "::warning title=Unsigned macOS build::MACOS_CERTIFICATE_P12/APPLE_APP_PASSWORD are not set in the 'release' environment; shipping an ad-hoc signed, un-notarized app."
          codesign --force --sign - "dist/Zenoh Explorer.app"

      - name: Package (macOS)
        if: runner.os == 'macOS'
        env:
          TARGET: ${{ matrix.target }}
        run: |
          cp README.md LICENSE dist/
          ditto -c -k --sequesterRsrc dist "$BINARY_NAME-$TARGET.zip"
          unzip -l "$BINARY_NAME-$TARGET.zip" | grep -q 'Zenoh Explorer.app/Contents/MacOS/zenoh-explorer'
```

`notarytool submit --wait` exits non-zero when Apple returns `Invalid`, which fails the job. That is the intended behaviour when secrets exist but signing is wrong.

- [ ] **Step 5: Verify locally.**

Run: `actionlint .github/workflows/release.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/release.yml && grep -c 'HAS_APPLE_SIGNING' .github/workflows/release.yml && grep -c 'notarytool submit' .github/workflows/release.yml`
Expected: zizmor output starting `No findings to report`, then a count ≥ 4, then `1`. If zizmor reports `secrets-outside-env`, check that `environment: release` is on the `build` job.

Run (the unsigned path, end to end on this Mac): `cargo build --release --locked && rm -rf "$P2RUN/p2dist" "$P2RUN/p2.zip" && scripts/bundle-macos.sh "$CARGO_TARGET_DIR/release/zenoh-explorer" "$P2RUN/p2dist" 0.9.1 >/dev/null && codesign --force --sign - "$P2RUN/p2dist/Zenoh Explorer.app" && cp README.md LICENSE "$P2RUN/p2dist/" && ditto -c -k --sequesterRsrc "$P2RUN/p2dist" "$P2RUN/p2.zip" && unzip -l "$P2RUN/p2.zip" | grep 'Zenoh Explorer.app/Contents/MacOS/zenoh-explorer' && codesign --verify --verbose=2 "$P2RUN/p2dist/Zenoh Explorer.app"`
Expected: the `unzip` line is printed and `valid on disk` / `satisfies its Designated Requirement`.

GitHub-only (local substitute: the unsigned-path run above; no local substitute exists for Developer ID signing or notarization): an unsigned run shows the `Unsigned macOS build` warning annotation. A signed run passes `spctl --assess` with `source=Notarized Developer ID`.

- [ ] **Step 6: Commit.**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): ship macOS .app; codesign + notarize when secrets exist, warn otherwise

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Stage 3 (was T12): Windows code signing

**Files:**
- Modify: `.github/workflows/release.yml` (the `build` job `env:` block from T2 stage 2, plus new steps between `Build` and `Package (Windows)`)

**Choice: Azure Artifact Signing (formerly "Trusted Signing") via `Azure/artifact-signing-action` v2.0.0, not `signtool` with a PFX.** The certificate lives in Microsoft's HSM, so no private key is stored as a GitHub secret. Signatures are timestamped and carry Microsoft-issued identity, which builds SmartScreen reputation. `signtool` would need a purchased OV/EV certificate exported to a secret (EV certificates cannot be exported at all). The inputs used below (`azure-tenant-id`, `azure-client-id`, `azure-client-secret`, `endpoint`, `signing-account-name`, `certificate-profile-name`, `files`, `file-digest`, `timestamp-rfc3161`, `timestamp-digest`) were checked in that action's `action.yml` at SHA `c7ab2a86…` (https://github.com/Azure/artifact-signing-action/blob/v2.0.0/action.yml). The old name `azure/trusted-signing-action` redirects to this repository. The action's README recommends OIDC. Client-secret authentication is used here because gating on "secret present" must work without an extra `azure/login` step; moving to OIDC is a later improvement.

**Interfaces:**
- Consumes: T2 stage 1's `Build` and `Package (Windows)` steps; T2 stage 2's `build.env` block.
- Produces: job-level env `HAS_WINDOWS_SIGNING`; a signed `target/x86_64-pc-windows-msvc/release/zenoh-explorer.exe` before packaging.

- [ ] **Step 1: Write the failing check.**

Run: `grep -c 'HAS_WINDOWS_SIGNING' .github/workflows/release.yml`
Expected: `0`.

- [ ] **Step 2: Extend the `build` job `env:`** (added in T2 stage 2) so it reads:

```yaml
    env:
      HAS_APPLE_SIGNING: ${{ secrets.MACOS_CERTIFICATE_P12 != '' && secrets.APPLE_APP_PASSWORD != '' }}
      HAS_WINDOWS_SIGNING: ${{ secrets.AZURE_CLIENT_SECRET != '' && secrets.AZURE_SIGNING_ENDPOINT != '' }}
```

- [ ] **Step 3: Insert after the `Build` step and before `Package (Windows)`:**

```yaml
      - name: Sign executable (Windows)
        if: runner.os == 'Windows' && env.HAS_WINDOWS_SIGNING == 'true'
        uses: Azure/artifact-signing-action@c7ab2a863ab5f9a846ddb8265964877ef296ee82 # v2.0.0
        with:
          azure-tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          azure-client-id: ${{ secrets.AZURE_CLIENT_ID }}
          azure-client-secret: ${{ secrets.AZURE_CLIENT_SECRET }}
          endpoint: ${{ secrets.AZURE_SIGNING_ENDPOINT }}
          signing-account-name: ${{ secrets.AZURE_SIGNING_ACCOUNT }}
          certificate-profile-name: ${{ secrets.AZURE_CERT_PROFILE }}
          files: ${{ github.workspace }}\target\x86_64-pc-windows-msvc\release\zenoh-explorer.exe
          file-digest: SHA256
          timestamp-rfc3161: http://timestamp.acs.microsoft.com
          timestamp-digest: SHA256
          # The action is composite and by default restores its signing tools
          # with actions/cache; a release workflow uses no cache.
          cache-dependencies: false

      - name: Verify signature (Windows)
        if: runner.os == 'Windows' && env.HAS_WINDOWS_SIGNING == 'true'
        shell: pwsh
        run: |
          $sig = Get-AuthenticodeSignature "target/x86_64-pc-windows-msvc/release/zenoh-explorer.exe"
          Write-Host "Signature status: $($sig.Status) by $($sig.SignerCertificate.Subject)"
          if ($sig.Status -ne 'Valid') { throw "Authenticode signature is $($sig.Status)" }

      - name: Warn about unsigned build (Windows)
        if: runner.os == 'Windows' && env.HAS_WINDOWS_SIGNING != 'true'
        run: echo "::warning title=Unsigned Windows build::AZURE_CLIENT_SECRET/AZURE_SIGNING_ENDPOINT are not set in the 'release' environment; shipping an unsigned executable."
```

`timestamp-rfc3161` uses `http://` because that is the action's documented default and Microsoft's timestamp service endpoint (RFC 3161 responses are signed, so transport encryption is not needed).

`cache-dependencies: false` (pre-flight G2-2): `Azure/artifact-signing-action@c7ab2a86` is a composite action (`action.yml:215`) whose `cache-dependencies` input defaults to `'true'` (`action.yml:191-198`) and then runs four `actions/cache` steps for the PowerShell module, build tools, timestamp client and sign CLI (`action.yml:238-275`). A poisoned cache entry could replace the tool that signs the shipped exe. zizmor does not audit inside third-party composite actions, so only the grep below guards this.

- [ ] **Step 4: Verify locally.**

Run: `actionlint .github/workflows/release.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/release.yml && grep -c 'HAS_WINDOWS_SIGNING' .github/workflows/release.yml && grep -c 'Azure/artifact-signing-action@c7ab2a86' .github/workflows/release.yml && grep -c 'cache-dependencies: false' .github/workflows/release.yml`
Expected: zizmor output starting `No findings to report`, a count ≥ 4, `1` and `1`.

Run (the sign step comes before packaging): `awk '/name: Sign executable \(Windows\)/{s=NR} /name: Package \(Windows\)/{p=NR} END{exit !(s && p && s<p)}' .github/workflows/release.yml && echo order-ok`
Expected: `order-ok`.

GitHub-only (local substitute: the greps and the order check above; Authenticode signing cannot run on this Mac): an unsigned run shows the `Unsigned Windows build` warning. A signed run prints `Signature status: Valid`.

- [ ] **Step 5: Commit.**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): sign Windows exe with Azure Artifact Signing when secrets exist, warn otherwise

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Stage 4 (was T13): Debug-symbol assets and build provenance

**Files:**
- Modify: `.github/workflows/release.yml` (new steps in `build` after the package steps; the `Upload artifact` step's `path`; the `release` job's `permissions` and steps)

**Interfaces:**
- Consumes: T1 part f's split debuginfo layout; T2 stage 1 step names.
- Produces: `zenoh-explorer-<target>-debug-symbols.<archive>` assets; the provenance attestation for every `zenoh-explorer-*` asset.

`actions/attest-build-provenance` v4 is a wrapper over `actions/attest` (its README at v4.2.2). It needs `id-token: write` and `attestations: write`. Attestations work for public repositories on every plan, but for private repositories only on GitHub Enterprise Cloud (same README), hence the `private` gate.

- [ ] **Step 1: Write the failing check.**

Run: `grep -c 'debug-symbols' .github/workflows/release.yml; grep -c 'attest-build-provenance' .github/workflows/release.yml`
Expected: `0`, `0`.

- [ ] **Step 2: Insert after `Package (Windows)` and before `Upload artifact`:**

```yaml
      - name: Package debug symbols (macOS)
        if: runner.os == 'macOS'
        env:
          TARGET: ${{ matrix.target }}
        run: |
          dsym="target/$TARGET/release/$BINARY_NAME.dSYM"
          test -d "$dsym" || { echo "::error::missing $dsym"; exit 1; }
          ditto -c -k --keepParent "$dsym" "$BINARY_NAME-$TARGET-debug-symbols.zip"

      - name: Package debug symbols (Linux)
        if: runner.os == 'Linux'
        env:
          TARGET: ${{ matrix.target }}
        run: |
          dwp="$(find "target/$TARGET/release" -maxdepth 2 -name '*.dwp' -print | head -n1)"
          test -n "$dwp" || { echo "::error::no .dwp under target/$TARGET/release"; exit 1; }
          echo "Using $dwp"
          mkdir -p symbols
          cp "$dwp" "symbols/$BINARY_NAME.dwp"
          tar -czvf "$BINARY_NAME-$TARGET-debug-symbols.tar.gz" -C symbols .

      - name: Package debug symbols (Windows)
        if: runner.os == 'Windows'
        shell: pwsh
        env:
          TARGET: ${{ matrix.target }}
        run: |
          $pdb = "target/$env:TARGET/release/zenoh_explorer.pdb"
          if (-not (Test-Path $pdb)) { throw "missing $pdb" }
          Compress-Archive -Path $pdb -DestinationPath "$env:BINARY_NAME-$env:TARGET-debug-symbols.zip"
```

T1 part f's commit message records the dSYM path relative to the target directory (`git log --grep '^build(profile)' --format=%B -n1`). If it is not `release/zenoh-explorer.dSYM` (for example a `release/deps/…` path), use that path, under `target/$TARGET/`, in the macOS step.

- [ ] **Step 3: Change `Upload artifact`'s `path:`** to upload both files:

```yaml
          path: |
            ${{ env.BINARY_NAME }}-${{ matrix.target }}.${{ matrix.archive }}
            ${{ env.BINARY_NAME }}-${{ matrix.target }}-debug-symbols.${{ matrix.archive }}
```

- [ ] **Step 4: Change the `release` job's `permissions:`** to:

```yaml
    permissions:
      contents: write     # create the GitHub release and upload its assets
      id-token: write     # OIDC token for the Sigstore signing certificate (attestation)
      attestations: write # store the build provenance attestation
```

- [ ] **Step 5: Insert between `Generate checksums` and `Create release`:**

```yaml
      - name: Attest build provenance
        if: ${{ !github.event.repository.private }}
        uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2
        with:
          subject-path: artifacts/zenoh-explorer-*

      - name: Warn about missing provenance
        if: ${{ github.event.repository.private }}
        run: echo "::warning title=No provenance::artifact attestations need a public repository or GitHub Enterprise Cloud; skipped."
```

- [ ] **Step 6: Verify locally.**

Run: `actionlint .github/workflows/release.yml && GH_TOKEN=$(gh auth token) zizmor .github/workflows/release.yml && grep -c 'debug-symbols' .github/workflows/release.yml && grep -c 'attestations: write' .github/workflows/release.yml && grep -c 'contents: write' .github/workflows/release.yml`
Expected: zizmor output starting `No findings to report`, a count ≥ 4, `1`, `1`.

Run (the macOS symbol packaging, on this Mac with T1 part f applied): `cargo build --release --locked && dsym="$(find "$CARGO_TARGET_DIR/release" -maxdepth 2 -name 'zenoh-explorer*.dSYM' | head -n1)" && rm -f "$P2RUN/p2-sym.zip" && ditto -c -k --keepParent "$dsym" "$P2RUN/p2-sym.zip" && unzip -l "$P2RUN/p2-sym.zip" | grep -c 'Contents/Resources/DWARF'`
Expected: a count ≥ 1.

GitHub-only (local substitute: the macOS symbol packaging above; the Linux `.dwp` and Windows `.pdb` paths and attestation upload have none): every `Build (<target>)` job uploads two files. The Linux jobs log `Using target/<t>/release/…dwp`; if they fail with `no .dwp`, apply the fallback in Open Question 3. The release page lists 11 assets (5 archives, 5 symbol archives, 1 checksum file). `gh attestation verify <asset> --repo <owner>/<repo>` succeeds for a downloaded asset.

- [ ] **Step 7: Commit.**

```bash
git add .github/workflows/release.yml
git commit -m "ci(release): publish debug symbols per target and attest build provenance

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Stage 5 (was T14): Integration verification

**Files:** none planned. Any fix-up found here goes into the file that caused it, in a separate commit whose message names the task that owned that file.

- [ ] **Step 0: Tools and toolchain present** (pre-flight G3-6).

Run: `for t in actionlint zizmor shellcheck check-jsonschema; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done; rustup toolchain list | grep -q '^1.88' && echo tools-ok` (run it in a subshell, `bash -c '…'`, so `exit` does not close your shell)
Expected: `tools-ok`. If it fails, stop and ask the user to run the "Local tooling" installs; do not install without approval.

- [ ] **Step 1: Workflow lint.**

Run: `actionlint && GH_TOKEN=$(gh auth token) zizmor .github/`
Expected: actionlint exit 0 with no output (it now lints all four workflows, including the rewritten `release.yml`); zizmor output starting `No findings to report`.

- [ ] **Step 2: Pins, injection and privilege greps.**

Run:
```bash
grep -nE 'uses: [^ ]+@[^0-9a-f]' .github/workflows/*.yml; echo "tag-pins exit $?"
grep -nE 'uses: [^ ]+@[0-9a-f]{40}$' .github/workflows/*.yml; echo "missing-comment exit $?"
grep -n 'github.event.inputs' .github/workflows/*.yml; echo "inputs exit $?"
grep -c '^permissions:' .github/workflows/ci.yml .github/workflows/build.yml .github/workflows/msrv.yml .github/workflows/release.yml
grep -L 'persist-credentials: false' .github/workflows/*.yml; echo "persist exit $?"
```
Expected: `tag-pins exit 1`, `missing-comment exit 1`, `inputs exit 1`, each file shows `:1`, and `grep -L` prints no file names.

- [ ] **Step 3: Scripts.**

Run: `shellcheck scripts/release-tag.sh scripts/test-release-tag.sh scripts/bundle-macos.sh scripts/test-bundle-macos.sh && bash scripts/test-release-tag.sh && bash scripts/test-bundle-macos.sh && check-jsonschema --builtin-schema vendor.dependabot .github/dependabot.yml`
Expected: `all 11 cases passed`, `bundle test passed`, `ok -- validation done`.

- [ ] **Step 4: Rust.**

Run (`CARGO_TARGET_DIR="$P2RUN/tgt-int"`): `cargo fmt --all -- --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked && cargo test --locked -- --ignored && CARGO_TARGET_DIR="$P2RUN/tgt-b" cargo +1.88 check --locked --all-targets && cargo build --release --locked && find "$CARGO_TARGET_DIR/release" -maxdepth 2 -name 'zenoh-explorer*.dSYM' | grep -q . && echo rust-ok`
Expected: `rust-ok`.

- [ ] **Step 5: Commit the checklist** (an empty commit if Steps 1–4 needed no fix-ups):

```bash
git commit --allow-empty -m "chore(ci): P2 CI/release hardening verified locally

GitHub-only checks (repository has no remote yet):
- [ ] PR: 'Format and clippy', 'Test (ubuntu-latest)', 'Test (macos-latest)',
      'Test (windows-latest)', 'Network-session tests (ignored set)',
      'Security audit', 'Build (<target>)' x5 (check only), 'Check on Rust 1.88' green
- [ ] 'Build (aarch64-unknown-linux-gnu)' ran on ubuntu-24.04-arm
- [ ] push to main: 'Build (<target>)' x5 run release builds
- [ ] Create GitHub environment 'release' and add signing secrets (names in
      plan Global Constraints). Caveats: all 5 release build jobs deploy to it,
      so required reviewers gate all 5 (Linux too); a tag-only deployment rule
      refuses workflow_dispatch runs (the run's ref is the dispatching branch)
- [ ] Mark as required status checks, by these rendered names:
      'Format and clippy', 'Test (ubuntu-latest)', 'Test (macos-latest)',
      'Test (windows-latest)', 'Network-session tests (ignored set)',
      'Security audit', the five 'Build (<target>)' checks, 'Check on Rust 1.88'
- [ ] Tag v<Cargo version>: release has 11 assets; macOS/Windows signed or warned
- [ ] workflow_dispatch (from main) with a mismatched tag fails in 'Validate tag'
- [ ] gh attestation verify <asset> --repo <owner>/<repo> succeeds (public repo only)
- [ ] Dependabot lists github-actions and cargo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

## Self-review notes

- **Coverage of the reviewer's findings:**
  - SHA pins: T1 parts a2, a3, b; T2 stages 1–4. Dependabot: T1 part c.
  - Permissions: T1 parts a2, a3, b; T2 stages 1 and 4. Clippy `--all-targets`, `--locked` and the 3-OS matrix: T1 part a2. Ignored network tests: T1 part a2.
  - PR cost and concurrency: T1 part a3 (plus concurrency in parts a2, b and T2 stage 1). aarch64 Linux: T1 part a3, T2 stage 1.
  - Injection and tag/version check: T1 part d, T2 stage 1.
  - macOS `.app`, signing and notarization: T1 part e, T2 stage 2. Windows signing: T2 stage 3. Provenance: T2 stage 4. Checksums: kept in T2 stage 1.
  - `--locked` and `|| true` in the release: T2 stage 1. MSRV job: T1 part b. Profile and debug symbols: T1 part f, T2 stage 4.
  - README download, MSRV, clone URL, and the Features/Troubleshooting corrections: T1 part g. Windows logging: declared out of scope in Global Constraints.
- **Checked against pinned sources:** every action input used in this plan is in that action's `action.yml` at the pinned SHA; the Azure action's `cache-dependencies` input and composite cache steps were read at `c7ab2a86`; zizmor's `superfluous-actions` list was read at tag v1.30.1 (it lists `softprops/action-gh-release`, and `dtolnay/rust-toolchain` only under the Pedantic persona; `actions/attest-build-provenance` is not listed). The runner labels and Cargo/rustc profile semantics are cited from their docs above.
- **Checked against the post-P1 tree:** `ci.yml:36-75` and `:77-84`, `Cargo.toml:5,9,27-32`, `README.md:26-32,43-54,70-82`, `release.yml` (132 lines), `scripts/bundle-macos.sh:13,24,31`, `assets/Info.plist:13,15` (`0.1.0`), `src/main.rs:2,28-41`, the five ignored tests and their ports, `src/ui/help.rs`, `src/ui/publish.rs:12,357`, `src/app/mod.rs:298`.
- **Not confirmed locally, with fallbacks written in:** the Linux `.dwp` location (T2 stage 4, Open question 3), Windows `cargo test` and multicast on hosted runners (T1 part a2), and the real signing and notarization runs (T2 stages 2 and 3). The lint tools are not installed yet, so no amended YAML has been linted; T1 Step 0 must pass before any "No findings" claim.

## Open questions for the user

1. **Repository URL** (needed before T1 part g starts). There is no git remote. `Cargo.toml:9` says `https://github.com/zenoh-project/zenoh-explorer`, and part g uses that for the clone command, the releases link and `gh attestation verify --repo`. Is that the real home, or should part g use a different owner/repo? Attestation verification in the README only works if the repository is public.
2. **Signing accounts.** Do you have (or want) an Apple Developer ID (99 USD/yr) and an Azure Artifact Signing account? Azure Artifact Signing eligibility is limited to organisations and individual developers in certain countries. Without them, releases stay unsigned with warnings, which the plan supports. If you would rather not have Windows signing wired at all, T2 stage 3 can be dropped.
3. **Linux debug symbols fallback.** If CI shows that `split-debuginfo = "packed"` leaves no `.dwp` on Linux (T2 stage 4 fails with `no .dwp`), should the Linux jobs instead set `CARGO_PROFILE_RELEASE_SPLIT_DEBUGINFO=off` and extract symbols with `objcopy --only-keep-debug` plus `--add-gnu-debuglink`, or should they simply skip Linux symbol assets?
4. **`release` environment scope** (pre-flight G2-5). The plan keeps `environment: release` on all five build rows and documents the caveats in the checklist. Alternatively the macOS and Windows rows could carry `env_name: release` and Linux an empty value, with `environment: ${{ matrix.env_name }}`, so reviewers and deployment rules touch only the jobs that use secrets. That variant was not adopted because how GitHub treats an empty environment name was not verified. Which do you want?
5. **Tool installs** (T1 Step 0). May the agent run `brew install actionlint shellcheck zizmor check-jsonschema`, `rustup toolchain install 1.88 --profile minimal` and `rustup target add x86_64-apple-darwin`, or will you run them? Without them T1 is blocked.
