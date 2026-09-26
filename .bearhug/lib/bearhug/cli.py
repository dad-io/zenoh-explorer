"""The ``bearhug`` command line.

Seven subcommands, one per phase of the plan. Phase 0 wires them all up; later phases replace the
stubs. A stub raises rather than printing a friendly nothing, so an unbuilt phase can never be
mistaken for a phase that ran and found nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from bearhug import __version__
from bearhug.cli_arch import add_arch_parser
from bearhug.model import SEVERITY_ORDER, Severity, dumps
from bearhug.paths import (
    BARRACUDA_ROOT,
    PATCHES_DIR,
    REPO_ROOT,
    REPORTS_DIR,
    SNAPSHOTS_DIR,
    assert_writable,
)
from bearhug.snapshot import compute_drift, take_snapshot
from bearhug.snapshot.toolchain import write_toolchain_report


def _not_yet(phase: str, task: str):
    def run(_args: argparse.Namespace) -> int:
        raise NotImplementedError(f"{phase} is not built yet — see plan task {task}")

    return run


def _latest_snapshot() -> Path:
    """The most recent snapshot by label. Labels are dates, so lexical order is chronological."""
    candidate = _latest_snapshot_or_none()
    if candidate is None:
        raise SystemExit(
            f"no snapshot found in {SNAPSHOTS_DIR} — run `bearhug snapshot` first"
        )
    return candidate


def _latest_snapshot_or_none() -> Path | None:
    candidates = sorted(d for d in SNAPSHOTS_DIR.glob("*") if (d / "manifest.json").is_file())
    return candidates[-1] if candidates else None


def _resolve_snapshot(given: str | None) -> Path:
    if not given:
        return _latest_snapshot()
    candidate = Path(given)
    if not (candidate / "manifest.json").is_file() and not candidate.is_absolute():
        # A bare label ("2026-08-28b") resolves under snapshots/; a path already pointing at a
        # snapshot is taken as given, rather than being prefixed into snapshots/snapshots/.
        candidate = SNAPSHOTS_DIR / given
    if not (candidate / "manifest.json").is_file():
        raise SystemExit(
            f"{candidate} holds no manifest.json; run `bearhug snapshot` first — a snapshot "
            f"must live under {SNAPSHOTS_DIR} (or pass the path to one that already exists)"
        )
    return candidate


def _validate_since(given: str | None) -> None:
    if given is None:
        return
    try:
        parsed = dt.date.fromisoformat(given)
    except ValueError as exc:
        raise SystemExit(f"invalid --since date {given!r}; expected YYYY-MM-DD") from exc
    if parsed.isoformat() != given:
        raise SystemExit(f"invalid --since date {given!r}; expected YYYY-MM-DD")


def _cmd_snapshot(args: argparse.Namespace) -> int:
    label = args.date or dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%d")
    result = take_snapshot(SNAPSHOTS_DIR, label=label, dry_run=args.dry_run, force=args.force)
    if args.dry_run:
        for target in result.planned:
            print(f"{target.layer}/{target.relpath}")
        print(f"\n{len(result.planned)} files would be captured into {result.path}")
        return 0
    layers = result.manifest["layers"]
    for name, block in sorted(layers.items()):
        print(f"{name:8s} {len(block['files']):4d} files", end="")
        if block["missing"]:
            print(f"   missing: {', '.join(block['missing'])}", end="")
        print()
    memex = result.manifest["memex"]
    print(f"memex    {memex['count']:4d} records, {memex['parse_failures']} unparsed")
    subject = result.manifest["subject"]["barracuda"]
    print(f"subject  {subject['head']} on {subject['branch']}, {len(subject['dirty'])} dirty paths")
    # G01: the same identity the manifest carries, as a dated artifact beside the evidence.
    toolchain = write_toolchain_report(label, block=result.manifest["toolchain"])
    go = result.manifest["toolchain"]["tools"]["go"]["version"] or "unknown"
    dlv = result.manifest["toolchain"]["tools"]["dlv"]["version"] or "unknown"
    print(f"toolchain go={go}, dlv={dlv}  -> {toolchain.name}")
    print(f"\nwrote {result.path}  (cite this as {result.snapshot_id})")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    """Launch the vendored Go dashboard.

    The TUI is the read surface of the harness while you are working in it, not a tool you
    remember to reach for afterwards — see plan Phase 8. It only ever reads: every git call it
    makes is log/status/worktree/rev-parse, asserted by tests/test_tui.py.
    """
    run_locator = getattr(args, "run_locator", None)
    if run_locator is not None:
        if any(
            value is not None
            for value in (
                args.repo,
                args.snapshot,
                args.session,
                args.provider_work_observation,
                args.work_binding,
                args.active_plan,
                args.board_row,
            )
        ) or args.build or args.authority_scope != "full":
            raise SystemExit(
                "native campaign TUI mode cannot be combined with repository, build, "
                "or ordinary cockpit options"
            )
        from bearhug.campaign.cockpit_runner import run_capsule_tui

        try:
            return run_capsule_tui(run_locator, interval=args.refresh, web=args.web)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(f"native campaign TUI failed: {exc}") from exc

    tui_dir = REPO_ROOT / "tui"
    if not (tui_dir / "go.mod").is_file():
        raise SystemExit(f"no vendored dashboard at {tui_dir}")
    if shutil.which("go") is None:
        raise SystemExit("the dashboard needs a Go toolchain on PATH (`brew install go`)")

    target = Path(args.repo).expanduser() if args.repo else BARRACUDA_ROOT
    if args.build:
        if any(
            value is not None
            for value in (
                args.snapshot,
                args.session,
                args.provider_work_observation,
                args.work_binding,
                args.active_plan,
                args.board_row,
            )
        ) or args.authority_scope != "full" or args.refresh != 30.0:
            raise SystemExit("--build cannot be combined with live cockpit projection options")
        return subprocess.call(["go", "build", "-o", "tui", "."], cwd=tui_dir)

    from bearhug.tui_runner import LiveTUIError, run_live_tui

    try:
        return run_live_tui(
            subject=target,
            snapshot=(
                _resolve_snapshot(args.snapshot)
                if args.snapshot is not None
                else _latest_snapshot_or_none()
            ),
            refresh_seconds=args.refresh,
            session_id=args.session,
            provider_work_observation=args.provider_work_observation,
            work_binding=args.work_binding,
            authority_scope=args.authority_scope,
            active_plan=args.active_plan,
            board_row=args.board_row,
            web=args.web,
        )
    except LiveTUIError as exc:
        raise SystemExit(f"TUI failed: {exc}") from exc


def _cmd_provider(args: argparse.Namespace) -> int:
    """Run one provider turn with raw two-sided custody and a cockpit observation."""

    if args.action != "run":
        raise SystemExit(f"unknown provider action: {args.action}")
    from bearhug.providers import (
        ClaudeLaunchContract,
        CodexAppServerContract,
        run_claude,
        run_codex_app_server,
    )
    from bearhug.providers.policy import (
        CAPABILITIES_BY_PROVIDER,
        ProviderPolicyError,
        load_provider_policy,
        provider_role,
    )

    if bool(args.policy) != bool(args.role):
        raise SystemExit("--policy and --role must be supplied together")
    selected = None
    if args.policy:
        try:
            selected = provider_role(load_provider_policy(args.policy), args.role)
        except ProviderPolicyError as exc:
            raise SystemExit(f"provider policy failed: {exc}") from exc
    provider = args.provider or (selected.provider if selected else None)
    if provider is None:
        raise SystemExit("provider is required: pass --provider or select one with --policy/--role")
    model = args.model or (selected.model if selected else None)
    effort = args.effort or (selected.effort if selected else None)
    if model is None or effort is None:
        raise SystemExit("model and effort are required: pass them or select a configured role")
    sandbox = args.sandbox or (selected.sandbox if selected else "read-only")
    approval_policy = args.approval_policy or (selected.approval_policy if selected else "never")
    required_capabilities = selected.required_capabilities if selected else ()
    if selected and provider != selected.provider:
        missing = sorted(set(selected.required_capabilities) - CAPABILITIES_BY_PROVIDER[provider])
        if missing:
            raise SystemExit(
                f"provider override {provider!r} cannot satisfy role {args.role!r}: "
                f"missing {missing}"
            )
        if args.model is None or args.effort is None:
            raise SystemExit(
                "a provider override must also supply --model and --effort; model ids are not "
                "translated or guessed"
            )

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"provider cwd is not a directory: {cwd}")
    if args.prompt_file == "-":
        prompt = sys.stdin.read()
    else:
        prompt_path = Path(args.prompt_file).expanduser()
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"cannot read prompt file {prompt_path}: {exc}") from exc
    if not prompt.strip():
        raise SystemExit("provider prompt must not be empty")
    executable = args.provider_bin or provider
    if not Path(executable).is_file() and shutil.which(executable) is None:
        raise SystemExit(f"{provider} executable not found: {executable}")
    try:
        version_result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"cannot read {provider} version from {executable}: {exc}") from exc
    adapter_version = (version_result.stdout or version_result.stderr).strip()
    if version_result.returncode != 0 or not adapter_version:
        raise SystemExit(f"{provider} version probe failed for {executable}")
    contract_type = CodexAppServerContract if provider == "codex" else ClaudeLaunchContract
    contract = contract_type(
        cwd=str(cwd),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        adapter_version=adapter_version,
        requested_model=model,
        requested_reasoning_effort=effort,
        sandbox=sandbox,
        approval_policy=approval_policy,
    )
    run_options = {
        "executable": executable,
        "timeout_s": args.timeout,
        "role": args.role,
        "required_capabilities": required_capabilities,
    }
    try:
        if provider == "codex":
            run = run_codex_app_server(prompt, contract, **run_options)
            raw_path = run.server_events_path
            request_path = run.client_events_path
        else:
            run = run_claude(prompt, contract, **run_options)
            raw_path = run.raw_events_path
            request_path = run.request_path
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"provider run failed: {exc}") from exc
    observation = run.observation
    configured = observation["configured_model"] or observation["requested_model"] or "unobserved"
    final = observation["final_observed_model"] or configured
    print(
        f"{observation['provider']} session {observation['session_id']} "
        f"{observation['terminal_state']}; model {configured} -> {final}"
    )
    print(f"raw events: {raw_path}")
    print(f"request: {request_path}")
    print(f"receipt: {run.receipt_path}")
    if provider == "codex":
        print(f"work observation: {run.work_observation_path}")
    else:
        print(
            "work observation: use `bearhug work bind --claude-task-store "
            "<explicit-session-directory> ...`"
        )
    print("promotion: NOT ELIGIBLE (see receipt promotion_blockers)")
    return 0 if observation["terminal_state"] == "completed" else 1


def _closed_json_file(path: Path | str, label: str) -> dict:
    """Read one explicit JSON object without accepting duplicate keys."""

    def closed_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_bytes(), object_pairs_hook=closed_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"cannot read explicit {label} {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"explicit {label} {source} must contain one JSON object")
    return value


def _board_candidate(value: str) -> dict[str, str]:
    """Parse the CLI's explicit ROW=AUTHORITY_PATH join without inferring either side."""

    row, separator, authority = value.partition("=")
    if not separator or not row or not authority:
        raise argparse.ArgumentTypeError("candidate must be ROW=AUTHORITY_PATH")
    return {"board_row": row, "authority_path": authority}


def _cmd_work(args: argparse.Namespace) -> int:
    """Prepare and persist an explicit provider-work to project authority binding."""

    if args.action != "bind":
        raise SystemExit(f"unknown work action: {args.action}")
    from bearhug.providers.claude_work import ingest_claude_task_store
    from bearhug.providers.work_store import (
        persist_work_authority_artifacts,
        prepare_work_authority_artifacts,
    )

    observed_at = dt.datetime.now(tz=dt.UTC)
    try:
        if args.claude_task_store:
            observation = ingest_claude_task_store(
                args.claude_task_store,
                observed_at=observed_at,
            )
        else:
            observation = _closed_json_file(
                args.observation,
                "provider work observation",
            )
        artifacts = prepare_work_authority_artifacts(
            observation=observation,
            subject=args.subject,
            task_id=args.task_id,
            active_plan_path=args.active_plan,
            candidates=args.candidate,
            created_at=observed_at,
        )
        persisted = persist_work_authority_artifacts(
            artifacts,
            output_root=args.output_root,
        )
    except ValueError as exc:
        raise SystemExit(f"work binding failed: {exc}") from exc
    print(f"provider: {artifacts.observation['provider']}")
    print(f"resolution: {artifacts.binding['resolution']}")
    print(f"work observation: {persisted.observation_path}")
    print(f"work binding: {persisted.binding_path}")
    print(
        "cockpit: bearhug cockpit --provider-work-observation "
        f"{persisted.observation_path} --work-binding {persisted.binding_path}"
    )
    return 0


def _cmd_harness(args: argparse.Namespace) -> int:
    """Validate or transact one explicit Claude Code or Codex harness bundle.

    Every input is a path supplied by the operator.  This command intentionally has no provider
    discovery or implicit projection: a manifest, complete source tree, and policy are the
    authority consumed by the installer.
    """
    from bearhug.harness_attestation import attest_installed_harness
    from bearhug.harness_installer import (
        HarnessInstallError,
        SealedNativeMaterializationBundle,
        TransactionalHarnessInstaller,
        inspect_target_checkout,
    )
    from bearhug.harness_policy_v2 import compile_harness_policy_v2
    from bearhug.native_materialization import (
        NativeMaterializationError,
        load_native_materialization_manifest,
    )

    action = args.action
    if action == "validate":
        if not args.policy or not args.manifest:
            raise SystemExit("harness validate requires --policy and --manifest")
    elif action in {"dry-run", "install", "upgrade"}:
        if not args.policy or not args.manifest or not args.source_root:
            raise SystemExit(f"harness {action} requires --policy, --manifest, and --source-root")
    elif action in {"uninstall", "read-installed", "attest"} and not args.provider:
        raise SystemExit(f"harness {action} requires --provider")
    if action != "validate" and not args.subject:
        raise SystemExit(f"harness {action} requires --subject")
    if action != "validate" and not args.state_root:
        raise SystemExit(f"harness {action} requires --state-root")
    try:
        policy = (
            compile_harness_policy_v2(_closed_json_file(args.policy, "policy"))
            if args.policy
            else None
        )
        manifest = None
        bundle = None
        if args.manifest:
            manifest = load_native_materialization_manifest(args.manifest, policy=policy)
            if args.provider and manifest.document["provider"] != args.provider:
                raise SystemExit("--provider disagrees with the manifest provider")
        if args.source_root:
            if manifest is None or policy is None:
                raise SystemExit("--source-root requires --policy and --manifest")
            bundle = SealedNativeMaterializationBundle.from_directory(
                manifest=manifest, policy=policy, source_root=args.source_root
            )
        if action == "validate":
            output = {
                "provider": manifest.document["provider"],
                "native_materialization_sha256": manifest.sha256,
                "file_count": len(manifest.files)
                if hasattr(manifest, "files")
                else len(manifest.document["files"]),
                "source_root_verified": bundle is not None,
            }
        else:
            target = inspect_target_checkout(args.subject)
            installer = TransactionalHarnessInstaller(state_root=args.state_root)
            if action == "read-installed":
                output = installer.read_installed(provider=args.provider, target=target)
            elif action == "attest":
                output = attest_installed_harness(
                    installer=installer, provider=args.provider, target=target
                )
            elif action == "uninstall":
                output = installer.uninstall(provider=args.provider, target=target).receipt
            else:
                if bundle is None:
                    raise SystemExit("an install operation requires a sealed source bundle")
                operation = {
                    "dry-run": installer.dry_run_install,
                    "install": installer.install,
                    "upgrade": installer.upgrade,
                }[action]
                result = operation(bundle=bundle, target=target)
                output = result.receipt
    except (NativeMaterializationError, HarnessInstallError, ValueError) as exc:
        raise SystemExit(f"harness {action} failed: {exc}") from exc
    if args.format == "json":
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    elif output is None:
        print("no installed harness")
    elif action == "validate":
        print(f"validated {output['provider']} manifest {output['native_materialization_sha256']}")
        print(f"files={output['file_count']} source_root_verified={output['source_root_verified']}")
    elif action == "attest":
        print(
            f"{output['provider']} installed_bytes_verified="
            f"{output['claims']['installed_bytes_verified']} "
            "effective_sources=unavailable trust=unavailable"
        )
    elif action == "read-installed":
        print(f"installed {output['provider']} manifest {output['content_sha256']}")
    else:
        print(f"{output['operation']} {output['outcome']} ({output['content_sha256']})")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    """4.3/4.4/4.5 — what the harness's Stop gates actually did, and how well each rule holds."""
    from bearhug.replay import CorpusError, select_corpus

    _validate_since(args.since)
    if args.action == "jobs-census":
        return _cmd_jobs_census()
    try:
        with select_corpus(args.corpus) as corpus:
            return _cmd_replay_selected(args, corpus)
    except CorpusError as exc:
        raise SystemExit(str(exc)) from exc


def _cmd_jobs_census() -> int:
    """M16 — read the ruled job-timeline surface without selecting a transcript corpus."""
    from bearhug.paths import JOBS_DIR, REPO_ROOT, transcripts_dir
    from bearhug.replay.jobs import (
        excluded_jobs,
        job_census,
        render_jobs_census,
        write_jobs_census,
    )

    if not JOBS_DIR.is_dir():
        raise SystemExit(f"Claude Code jobs directory not found: {JOBS_DIR}")
    excluded = excluded_jobs(JOBS_DIR, transcripts_dir(REPO_ROOT))
    census = job_census(JOBS_DIR, exclude=excluded)
    json_path, md_path = write_jobs_census(census)
    print(render_jobs_census(census))
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


def _cmd_replay_selected(args: argparse.Namespace, corpus) -> int:
    """Run one replay action against a corpus that has already been explicitly resolved."""
    import collections

    from bearhug.replay import (
        blocking_firings,
        build_findings,
        build_injection_findings,
        census_fields,
        compute_injection_stats,
        compute_ledger,
        corpus_metrics,
        render_coverage_table,
        render_injection_table,
        render_table,
        stacked_blocks,
    )
    from bearhug.replay.report import write_replay_report

    paths = list(corpus.paths)
    window = f" since {args.since}" if args.since else ""

    if args.action == "report":
        snapshot = _resolve_snapshot(args.snapshot or "2026-08-29")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        json_path, md_path, manifest_path = write_replay_report(
            corpus, snapshot_id=snapshot_id, since=args.since
        )
        print(md_path.read_text(encoding="utf-8"))
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        print(f"wrote {manifest_path}")
        findings = json.loads(json_path.read_text(encoding="utf-8"))["findings"]
        return 1 if any(f["severity"] == Severity.BROKEN.value for f in findings) else 0

    if args.action == "injection":
        from bearhug.report import write_findings

        stats = compute_injection_stats(paths, since=args.since)
        print(render_injection_table(stats))

        snapshot = _resolve_snapshot(args.snapshot or "2026-08-29")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        findings = build_injection_findings(
            stats, snapshot_id=snapshot_id, since=args.since, window=window.strip() or "all"
        )
        path = write_findings(findings, snapshot_id=snapshot_id, prefix="injection")
        print(f"\nwrote {path}  ({len(findings)} findings)")
        print(
            "\nLIMIT: correlation only, never causation (docs/METHOD.md). A rare token "
            "reappearing after an injection is not proof the payload was used, and its absence "
            "is not proof it was ignored — see each finding's own limit."
        )
        return 0

    if args.action == "ground-truth":
        from bearhug.replay.groundtruth import (
            CLASSIFIERS,
            LABELS_DIR,
            load_label_set,
            sample_locators,
            score_labels,
            write_label_set,
        )

        if args.classifier not in CLASSIFIERS:
            raise SystemExit(
                f"unknown classifier {args.classifier!r}; choose {', '.join(CLASSIFIERS)}"
            )
        if args.score:
            # Scoring needs the KEY and the labels, not the corpus: a live directory moves under
            # a labelled set within hours, so --label-set names the pinned folder directly.
            folder = (
                Path(args.label_set)
                if args.label_set
                else LABELS_DIR / f"{args.classifier}-{corpus.kind}-{corpus.digest[:12]}"
            )
            key_path, labels_path = folder / "key.json", folder / "labels.json"
            if not key_path.is_file():
                raise SystemExit(f"no key at {key_path}; sample first (run without --score)")
            if not labels_path.is_file():
                raise SystemExit(f"no labels at {labels_path}; nothing has been labelled")
            key = load_label_set(key_path)
            try:
                score = score_labels(key, json.loads(labels_path.read_text(encoding="utf-8")))
            except ValueError as exc:
                raise SystemExit(f"labels refused: {exc}") from exc
            name = f"ground-truth-{key.classifier}-{key.corpus_kind}-{key.corpus_digest[:12]}.json"
            out = assert_writable(REPORTS_DIR / name)
            report = score.as_dict(approved_by=args.approve_thresholds)
            out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2, sort_keys=True))
            print(f"\nwrote {out}")
            return 0
        label_set = sample_locators(
            corpus, args.classifier, per_class=args.per_class, seed=args.seed
        )
        for path in write_label_set(label_set):
            print(f"wrote {path}")
        print(
            f"population by predicted class: {json.dumps(label_set.population, sort_keys=True)}; "
            f"sampled {len(label_set.items)} locators. Nothing is validated until labels.json "
            "exists and --score clears the thresholds."
        )
        return 0

    if args.action == "harness-versions":
        from bearhug.replay.harness_attribution import (
            ingest_attribution,
            render_attribution,
            write_attribution_report,
            write_handoff,
        )

        if not args.artifact:
            written = write_handoff(corpus)
            for path in written:
                print(f"wrote {path}")
            print(
                "\nThe historical-attribution run is project-owned (M13). Hand the directory "
                "above to a session in the selected repository and return the artifact with "
                "--artifact."
            )
            return 0
        manifest_path = PATCHES_DIR / "m13-historical-attribution" / "manifest.json"
        if not manifest_path.is_file():
            raise SystemExit(
                f"no shipped extractor manifest at {manifest_path}; run without --artifact first"
            )
        shipped = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
        try:
            attribution = ingest_attribution(
                artifact, corpus, expected_extractor_sha256=shipped["extractor_sha256"]
            )
        except ValueError as exc:
            raise SystemExit(f"artifact refused: {exc}") from exc
        print(render_attribution(attribution))
        json_path, md_path = write_attribution_report(attribution)
        print(f"wrote {json_path}\nwrote {md_path}")
        return 0

    if args.action == "dlv-depth":
        from bearhug.replay.dlv import (
            build_dlv_depth_findings,
            compute_dlv_depth,
            render_dlv_depth,
            write_dlv_depth,
        )
        from bearhug.report import write_findings

        snapshot = _resolve_snapshot(args.snapshot or "2026-08-29")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        depth = compute_dlv_depth(paths, since=args.since)
        print(render_dlv_depth(depth, corpus_label=corpus.label))
        json_path, md_path = write_dlv_depth(
            depth, corpus_label=corpus.label, corpus_digest=corpus.digest, snapshot_id=snapshot_id
        )
        findings = build_dlv_depth_findings(
            depth, snapshot_id=snapshot_id, corpus_label=corpus.label
        )
        path = write_findings(findings, snapshot_id=snapshot_id, prefix=f"dlv-depth-{corpus.kind}")
        print(f"wrote {json_path}\nwrote {md_path}\nwrote {path}  ({len(findings)} findings)")
        return 0

    if args.action == "go-coverage":
        from bearhug.replay.go_coverage import (
            compute_go_coverage,
            load_m14_validation,
            render_go_coverage,
            write_go_coverage,
        )

        coverage = compute_go_coverage(paths)
        try:
            validation = load_m14_validation(
                REPORTS_DIR, corpus_kind=corpus.kind, corpus_digest=corpus.digest
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        json_path, md_path = write_go_coverage(
            coverage,
            corpus_kind=corpus.kind,
            corpus_digest=corpus.digest,
            classifier_validation=validation,
        )
        print(
            render_go_coverage(
                coverage,
                corpus_kind=corpus.kind,
                corpus_digest=corpus.digest,
                classifier_validation=validation,
            )
        )
        print(f"wrote {json_path}\nwrote {md_path}")
        return 0

    if args.action == "ledger":
        from bearhug.report import write_findings

        attribution = None
        if args.attribution:
            # M13: join each transcript to the harness version the returned artifact resolved
            report = json.loads(Path(args.attribution).read_text(encoding="utf-8"))
            if report.get("corpus_digest") != corpus.digest:
                raise SystemExit(
                    f"attribution report is for corpus {str(report.get('corpus_digest'))[:12]}, "
                    f"not {corpus.digest[:12]}; the join would name the wrong files"
                )
            by_transcript = report.get("by_transcript") or {}
            attribution = {
                path: (by_transcript.get(corpus.member_for(path).relpath) or {}).get(
                    "harness_commit_date"
                )
                for path in paths
            }
            resolved = sum(1 for v in attribution.values() if v)
            print(
                f"harness attribution: {resolved} of {len(attribution)} transcripts resolved; "
                "the rest land in `unattributed`"
            )
        data = compute_ledger(paths, since=args.since, attribution=attribution)
        print(render_table(data))

        snapshot = _resolve_snapshot(args.snapshot or "2026-08-29")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        from bearhug.replay.groundtruth import load_validation_reports, validated_rules

        findings = build_findings(
            data,
            snapshot_id=snapshot_id,
            since=args.since,
            validated=validated_rules(
                load_validation_reports(REPORTS_DIR, corpus_kind=corpus.kind)
            ),
        )
        # the frozen ledger keeps its historical name; a live ledger is filed beside it,
        # never over it
        prefix = "ledger" if corpus.kind == "frozen" else f"ledger-{corpus.kind}"
        if attribution is not None:
            prefix += "-attributed"
        path = write_findings(findings, snapshot_id=snapshot_id, prefix=prefix)
        print(f"\nwrote {path}  ({len(findings)} findings)")
        print(
            "\nLIMIT: rates and co-occurrence only, never causation (docs/METHOD.md). Every "
            "rule's pre/post split is at the date its OWN gate landed, per the selected project's "
            "git history — a rate computed over the whole corpus would attribute behaviour to a "
            "gate most of those sessions never ran under."
        )
        return 1 if any(f.severity is Severity.BROKEN for f in findings) else 0

    # args.action == "metrics"
    report = corpus_metrics(paths, since=args.since)
    start, end = report.date_range
    print(
        f"corpus: {corpus.label} · {len(report.sessions)} sessions + "
        f"{len(report.subagents)} subagent transcripts = {len(paths)} files{window}, {start}..{end}"
    )
    print(
        f"\nturns — sessions: {report.total_human_turns} human, "
        f"{report.total_system_turns} harness-written user records, "
        f"{report.total_tool_result_turns} tool_result (all buckets); "
        f"subagents: {report.total_subagent_turns} turns (kept separate, not merged in)"
    )
    print(f"subagent dispatches (Agent/Task tool_use in sessions): {report.total_dispatches}")
    print(f"materialised subagent transcripts on disk: {len(report.subagents)}")

    print("\ntokens (top-level message.usage; repeated message IDs collapsed by component max):")
    for label, tokens in (
        ("sessions", report.session_tokens),
        ("subagents", report.subagent_tokens),
    ):
        coverage = tokens.coverage_percentage()
        coverage_text = "unavailable" if coverage is None else f"{coverage:.1f}% complete"
        print(
            f"  {label:10s} input={tokens.totals['input_tokens']:,} "
            f"output={tokens.totals['output_tokens']:,} "
            f"cache-create={tokens.totals['cache_creation_input_tokens']:,} "
            f"cache-read={tokens.totals['cache_read_input_tokens']:,} · "
            f"coverage={coverage_text} ({tokens.complete_messages}/"
            f"{tokens.assistant_messages} assistant messages) · "
            f"malformed={sum(tokens.malformed_values.values())}"
        )

    print("\nduration (record timestamp span; NOT model/API latency):")
    for label, durations in (
        ("sessions", report.session_durations),
        ("subagents", report.subagent_durations),
    ):
        coverage = durations.transcript_coverage_percentage
        coverage_text = "unavailable" if coverage is None else f"{coverage:.1f}%"
        print(
            f"  {label:10s} measured={durations.transcripts_with_span}/"
            f"{durations.transcripts} transcripts ({coverage_text}) · "
            f"sum-spans={durations.sum_observed_transcript_spans_ms:.0f}ms · "
            f"malformed={durations.malformed_timestamp_records} · "
            f"out-of-order={durations.out_of_order_records}"
        )
    print("  LIMIT: overlapping transcript spans are not elapsed wall time.")

    print("\ncompactions (system.compactMetadata object records only):")
    for label, compactions in (
        ("sessions", report.session_compactions),
        ("subagents", report.subagent_compactions),
    ):
        print(
            f"  {label:10s} explicit={compactions.explicit_compactions} · "
            f"malformed={compactions.malformed_compaction_records} · "
            f"ignored-lookalikes={compactions.ignored_structural_lookalikes}"
        )
    print("  LIMIT: new compaction record shapes are unmeasured until the detector is revised.")

    field_census = census_fields(paths, since=args.since)
    print("\nmetric field coverage (structural counts only; transcript values are not retained):")
    print(render_coverage_table(field_census))
    print(
        f"records censused: {field_census.records_seen}; malformed lines: "
        f"{field_census.malformed_lines}; non-object lines: {field_census.non_object_lines}"
    )

    print("\ntool-call histogram (sessions + subagents):")
    for name, n in report.tool_histogram().most_common():
        print(f"  {name:36s}{n:>6d}")

    firings = [
        f for path in paths for f in blocking_firings(path) if not args.since or f.day >= args.since
    ]
    if not firings:
        print(f"\nno Stop-gate blocks in {len(paths)} transcripts{window}")
        return 0

    per: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for firing in firings:
        per[firing.gate][firing.day] += 1
    days = sorted({f.day for f in firings})

    print(f"\nStop-gate BLOCKS across {len(paths)} transcripts{window}")
    print(f"{'gate':22s}" + "".join(f"{d[5:]:>9s}" for d in days) + "    total")
    for gate in sorted(per):
        print(
            f"{gate:22s}"
            + "".join(f"{per[gate][d]:>9d}" for d in days)
            + f"{sum(per[gate].values()):>9d}"
        )
    print(
        f"{'TOTAL':22s}"
        + "".join(f"{sum(1 for f in firings if f.day == d):>9d}" for d in days)
        + f"{len(firings):>9d}"
    )

    stacks = stacked_blocks(firings)
    if stacks:
        print(f"\nturns blocked by 2+ gates in the same second: {len(stacks)}")
        for second, gates in sorted(stacks.items()):
            print(f"  {second}  {' + '.join(gates)}")
    print(
        "\nLIMIT: these are gate COMPLAINTS, not gate activity. A Stop gate that passes emits\n"
        "hook_success with no message, so 'which gate passed' is not in the record at all."
    )
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    """2.9 — every static check against one snapshot, ranked and emitted."""
    from bearhug.lint import build_budget_findings, parse_sections, run_all_checks
    from bearhug.report import render_markdown, write_findings

    snapshot = _resolve_snapshot(args.snapshot)
    if args.dump_tree:
        text = (snapshot / "project" / "CLAUDE.md").read_text(encoding="utf-8")
        for section in parse_sections(text):
            print(
                f"§{section.id:<4s} {section.line_start:>5d}-{section.line_end:<5d} "
                f"{section.top_level_bullets:>3d} bullets  {section.title[:60]}"
            )
        return 0

    if args.observability:
        from bearhug.replay.observability import (
            build_observability_inventory,
            render_observability_inventory,
            write_observability_inventory,
        )

        inventory = build_observability_inventory(snapshot)
        json_path, md_path = write_observability_inventory(inventory)
        if args.format == "json":
            print(f"wrote {json_path}  ({len(inventory.rows)} rows)")
        else:
            print(render_observability_inventory(inventory))
            print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        return 0

    if args.ci_runner:
        from bearhug.report.ci_proposal import emit_ci_patch

        path = emit_ci_patch(snapshot)
        print(f"wrote {path}")
        print(
            "LIMIT: a proposal. Where it runs and on what trigger is a project-owned CI "
            "decision; the runner needs nothing from the product tree and bounds each suite."
        )
        return 0

    if args.gate_inventory:
        from bearhug.report.gate_inventory import emit_gate_inventory_patch

        path = emit_gate_inventory_patch(snapshot)
        print(f"wrote {path}")
        print(
            "LIMIT: a GENERATED inventory; .claude/settings.json is the authority and this copy "
            "is stale the moment it changes (its marker carries the settings digest)."
        )
        return 0

    if args.run_configs:
        from bearhug.lint.runconfig import (
            check_run_configs,
            parse_run_configs,
            render_run_configs,
        )
        from bearhug.report import write_findings

        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        directory = snapshot / "project" / ".idea" / "runConfigurations"
        if not directory.is_dir():
            print(f"no run configurations captured in {snapshot.name}")
            return 1
        configs = parse_run_configs(directory)
        findings = check_run_configs(directory, snapshot=snapshot_id)
        if args.format == "json":
            print(dumps([f.to_dict() for f in findings]))
        else:
            print(render_run_configs(configs))
            for finding in sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity]):
                print(f"{finding.severity.value.upper():9s} {finding.check:20s} {finding.summary}")
        path = write_findings(findings, snapshot_id=snapshot_id, prefix="runconfig")
        print(f"\n{len(configs)} configurations, {len(findings)} findings -> {path}")
        print(
            "LIMIT: structural validity is not runnability, and target existence is "
            "project-owned and reported `unverified`."
        )
        return 0

    if args.nag_vs_gate:
        from bearhug.replay import CorpusError, select_corpus
        from bearhug.replay.groundtruth import load_validation_reports, validated_rules
        from bearhug.replay.ledger import compute_ledger
        from bearhug.replay.nag_vs_gate import (
            build_nag_vs_gate,
            load_stop_census,
            render_nag_vs_gate,
            write_nag_vs_gate,
        )

        # The table's counts come from the effectiveness ledger over the FROZEN corpus, so the
        # frozen labels are what validate its detectors (a live score neither adds nor shadows).
        try:
            with select_corpus("frozen") as corpus:
                ledger = compute_ledger(corpus.paths)
        except CorpusError as exc:
            raise SystemExit(str(exc)) from exc
        table = build_nag_vs_gate(
            snapshot,
            stop_census=load_stop_census(snapshot),
            ledger=ledger,
            validated_detectors=validated_rules(
                load_validation_reports(REPORTS_DIR, corpus_kind="frozen")
            ),
        )
        json_path, md_path = write_nag_vs_gate(table)
        if args.format == "json":
            print(
                f"wrote {json_path}  ({len(table.rules)} rules, {len(table.mechanisms)} mechanisms)"
            )
        else:
            print(render_nag_vs_gate(table))
            print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        print(
            "\nLIMIT: no violation rate is reported for a detector M14 has not validated; every "
            "class count above is a join by explicit reference, and an ambiguity is reported, "
            "never resolved."
        )
        return 0

    if args.budget:
        from bearhug.paths import transcripts_dir
        from bearhug.replay.transcript import session_transcripts

        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        snapshot_id = manifest.get("snapshot_id", snapshot.name)
        paths = session_transcripts(transcripts_dir())
        findings = build_budget_findings(
            snapshot, snapshot_id=snapshot_id, transcript_paths=paths, since=args.since
        )
        if args.format == "json":
            path = write_findings(findings, snapshot_id=snapshot_id, prefix="budget")
            print(f"wrote {path}  ({len(findings)} findings)")
        else:
            print(render_markdown(findings, snapshot_id=snapshot_id, include_info=True))
        return 0

    findings = run_all_checks(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)

    if args.format == "json":
        path = write_findings(findings, snapshot_id=snapshot_id)
        print(f"wrote {path}  ({len(findings)} findings)")
    else:
        print(render_markdown(findings, snapshot_id=snapshot_id, include_info=args.all))
    return 1 if any(f.severity is Severity.BROKEN for f in findings) else 0


def _cmd_hooks(args: argparse.Namespace) -> int:
    """3.8 — run every snapshotted gate against the fixture corpus."""
    import tempfile

    from bearhug.hooks import audit, build_fixture_repo
    from bearhug.report import write_findings

    snapshot = _resolve_snapshot(args.snapshot if hasattr(args, "snapshot") else None)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)

    if args.action == "stop-census":
        return _cmd_stop_census(snapshot, snapshot_id)

    if args.action == "write-detection":
        return _cmd_write_detection(snapshot, snapshot_id)

    scratch = Path(tempfile.mkdtemp(prefix="bearhug-hooks-"))
    repo = build_fixture_repo(scratch / "repo")
    findings, results = audit(snapshot, repo, snapshot_id=snapshot_id)

    if args.action == "run":
        for _spec, run in results:
            if args.hook and args.hook not in run.hook:
                continue
            if args.event and args.event != run.fixture:
                continue
            state = (
                "ERRORED"
                if run.errored
                else "BLOCK"
                if run.blocked
                else "inject"
                if run.injected
                else "silent"
            )
            print(
                f"{run.hook:24s} {run.fixture:32s} {state:8s} "
                f"exit={run.exit_code:<3d} {run.duration_ms:>6.0f}ms"
            )
        print(f"\n{len(results)} runs · fixture repo {repo}")
        return 0

    for finding in sorted(findings, key=lambda f: (f.severity.value, f.check)):
        print(f"[{finding.severity.value:8s}] {finding.check:12s} {finding.summary}")
    path = write_findings(findings, snapshot_id=snapshot_id, prefix="hooks")
    print(f"\n{len(results)} hook-runs · wrote {path}")
    print("LIMIT: a gate silent here is silent across THESE fixtures, never dead.")
    return 1 if any(f.severity is Severity.BROKEN for f in findings) else 0


def _cmd_write_detection(snapshot: Path, snapshot_id: str) -> int:
    """R04 — who shares the write resolver, who reimplements it, and who cannot see Bash."""
    from bearhug.write_detection import build_inventory, write_inventory

    inventory = build_inventory(snapshot, snapshot_id=snapshot_id)
    json_path, md_path = write_inventory(inventory)

    print("MATCHER-BLIND — registered where Bash is observable, and cannot see it:")
    for script in inventory["blind_write_detectors"]:
        print(f"  {script}")
    print()
    print("SEES BASH — at least one registration matches Bash:")
    for script in inventory["bash_visible_write_detectors"]:
        print(f"  {script}")
    print()
    print("READS TRANSCRIPT — no matcher, so matcher blindness does not apply:")
    for script in inventory["transcript_reading_write_detectors"]:
        print(f"  {script}")
    print()
    print("codewrites.py consumers:")
    for script, names in sorted(inventory["codewrites_consumers"].items()):
        print(f"  {script:38s} {', '.join(names)}")
    print()
    print("reimplementers:")
    for script, row in sorted(inventory["reimplementers"].items()):
        mark = "deliberate" if row.get("deliberate") else "undeclared"
        print(f"  {script:38s} ({mark})")
    print()
    for defect in inventory["defects"]:
        print(f"[{defect['severity']:6s}] {defect['id']:24s} {defect['summary']}")
    print()
    print(f"wrote {json_path}\n      {md_path}")
    print("LIMIT: " + inventory["shared_parser_does_not_fix_blindness"])
    return 0


def _cmd_stop_census(snapshot: Path, snapshot_id: str) -> int:
    """D01 — every registered Stop command, what it did, and what cannot be known about it."""
    import tempfile

    from bearhug.hooks.stopscope import render_markdown, stop_census
    from bearhug.paths import REPORTS_DIR

    scratch = Path(tempfile.mkdtemp(prefix="bearhug-stopscope-"))
    census = stop_census(snapshot, scratch, snapshot_id=snapshot_id)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"stop-contract-{snapshot_id}"
    json_path = REPORTS_DIR / f"{stem}.json"
    md_path = REPORTS_DIR / f"{stem}.md"
    json_path.write_text(json.dumps(census, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(census), encoding="utf-8")

    summary = census["summary"]
    for reg in census["registrations"]:
        state = {True: "captured", False: "NOT CAPTURED", None: "unknown"}[reg["contract_captured"]]
        timeout = "unbounded" if reg["timeout"] is None else f"{reg['timeout']}s"
        print(
            f"{reg['order']} g{reg['group_index']}.{reg['position_in_group']} "
            f"{(reg['script'] or reg['command']):40s} {timeout:>10s}  {state}"
        )
    print(
        f"\n{summary['registrations']} registered Stop commands; roadmap 9.4 names "
        f"{summary['roadmap_coordinator_names']}; {summary['undeclared_by_roadmap']} unruled."
    )
    print(
        f"contract captured {summary['contract_captured']}, "
        f"not captured {summary['contract_not_captured']}"
    )
    print(f"wrote {json_path}\n      {md_path}")
    print(
        "LIMIT: silence across THESE fixtures is not inertness, and an external delegate "
        "that resolves here is not snapshot evidence."
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """6.1 — merge lint, hooks, replay, and available eval results into one ranked list."""
    from bearhug.evals import build_eval_findings, load_results
    from bearhug.replay import CorpusError, select_corpus
    from bearhug.replay.report import build_replay_findings
    from bearhug.report import compare_reports, write_report

    if args.build_manifest:
        from bearhug.report.regression import write_regression_manifest

        try:
            path = write_regression_manifest(
                args.build_manifest[0],
                args.build_manifest[1],
                corpus_kind=args.corpus,
                since=args.since,
                repo_root=REPO_ROOT,
            )
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"wrote {path}")
        print("Compare it with: bearhug report --compare-manifest " + str(path))
        return 0

    if args.compare_manifest:
        from bearhug.report.regression import compare_phases, validate_regression_manifest

        manifest = json.loads(Path(args.compare_manifest).read_text(encoding="utf-8"))
        # A missing phase is reported as a coverage gap in the comparison; only a structural
        # problem (identity, corpus label reuse, duplicate ids, a third snapshot) refuses it.
        problems = validate_regression_manifest(manifest, allow_coverage_gaps=True)
        if problems:
            raise SystemExit("regression manifest refused:\n  " + "\n  ".join(problems))
        comparison = compare_phases(manifest, root=REPO_ROOT)
        print(comparison.render())
        return 0

    if args.compare:
        old_dir = _resolve_snapshot(args.compare[0])
        new_dir = _resolve_snapshot(args.compare[1])
        diff = compare_reports(old_dir, new_dir)
        print(diff.render())
        return 0

    _validate_since(args.since)
    snapshot = _resolve_snapshot(args.snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    from bearhug.lint import Traffic

    try:
        with select_corpus(args.corpus) as corpus:
            replay_findings = build_replay_findings(
                corpus, snapshot_id=snapshot_id, since=args.since
            )
            # M17: reachability shares are counted from the SAME selection the replay read, and
            # the merge refuses two digests in one report.
            traffic = Traffic.from_corpus(corpus)
    except CorpusError as exc:
        raise SystemExit(str(exc)) from exc
    eval_findings = build_eval_findings(
        load_results(snapshot_id=snapshot_id), snapshot_id=snapshot_id
    )
    # S05: drift first, so a stale snapshot visibly degrades trust in every finding below.
    json_path, md_path = write_report(
        snapshot,
        traffic=traffic,
        replay_findings=replay_findings,
        eval_findings=eval_findings,
        report_label=corpus.label,
        drift=compute_drift(snapshot),
    )
    findings = json.loads(json_path.read_text(encoding="utf-8"))["findings"]
    print(md_path.read_text(encoding="utf-8"))
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    if args.emit_patches:
        from bearhug.model import Evidence, Finding
        from bearhug.report import emit_report_patches

        typed = [
            Finding(
                id=f["id"],
                check=f["check"],
                severity=Severity(f["severity"]),
                summary=f["summary"],
                snapshot=f["snapshot"],
                evidence=tuple(Evidence(**e) for e in f.get("evidence", ())),
                limit=f.get("limit"),
                detail=f.get("detail"),
            )
            for f in findings
        ]
        outcome = emit_report_patches(snapshot, typed)
        print(outcome.render())
        print(
            "LIMIT: a patch is a PROPOSAL diffed against the snapshotted CLAUDE.md. bear-hug "
            "never applies it; one generator exists (STALECOUNT word swaps), and every other "
            "finding is skipped with its reason above."
        )
    return 1 if any(f["severity"] == Severity.BROKEN.value for f in findings) else 0


def _cmd_cockpit(args: argparse.Namespace) -> int:
    """T02/T04/T05 — emit reports/cockpit.json, the one artifact the TUI reads (T01 ruling)."""
    interval = getattr(args, "watch", None)
    if not interval:
        return _emit_cockpit(args, announce=True)
    if interval < 5:
        raise SystemExit("--watch: 5 seconds is the floor; the cockpit reads every transcript")
    try:
        from bearhug.watcher import new_watcher_id

        watcher_id = new_watcher_id()
        heartbeat_sequence = 0
        while True:
            heartbeat_sequence += 1
            _emit_cockpit(
                args,
                announce=False,
                watcher=(watcher_id, heartbeat_sequence, max(10, int(interval * 2))),
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _emit_cockpit(
    args: argparse.Namespace,
    *,
    announce: bool,
    watcher: tuple[str, int, int] | None = None,
) -> int:
    """Build and write one cockpit; `announce` prints the summary a one-shot run wants."""
    from bearhug.paths import PROVIDER_OBSERVATIONS_DIR, transcripts_dir
    from bearhug.replay.cockpit import build_cockpit, write_cockpit
    from bearhug.watcher import watcher_record

    snapshot = _resolve_snapshot(args.snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    # S05: the cockpit carries the drift flag for the snapshot it reports on.
    # K02: the subject is explicit; without it the artifact says unobserved, not healthy.
    cockpit = build_cockpit(
        snapshot_id=snapshot_id,
        live_dir=transcripts_dir(),
        drift=compute_drift(snapshot),
        subject_root=BARRACUDA_ROOT,
        home=Path.home(),
        session_id=args.session,
        provider_observations_dir=PROVIDER_OBSERVATIONS_DIR,
        provider_work_observation=getattr(args, "provider_work_observation", None),
        work_binding=getattr(args, "work_binding", None),
    )
    if watcher is not None:
        watcher_id, heartbeat_sequence, stale_after_seconds = watcher
        heartbeat_at = dt.datetime.strptime(
            cockpit["generated_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.UTC)
        cockpit["watcher"] = watcher_record(
            watcher_id=watcher_id,
            scope="persistent",
            subject=BARRACUDA_ROOT,
            heartbeat_sequence=heartbeat_sequence,
            heartbeat_at=heartbeat_at,
            stale_after_seconds=stale_after_seconds,
        )
    path = write_cockpit(cockpit)
    freshness = cockpit["freshness"]
    announce and print(
        f"snapshot {cockpit['snapshot_id']} (current={freshness['current']}); "
        f"{cockpit['findings']['count']} findings carried; stale findings files: "
        f"{', '.join(freshness['stale_findings']) or 'none'}; sessions: {len(cockpit['sessions'])}"
    )
    announce and print(
        f"runtime {cockpit['runtime']['status']} (drift {cockpit['runtime']['drift']}); "
        f"coordinator {cockpit['coordinator']['status']}; tasks {cockpit['tasks']['status']}"
    )
    announce and print(f"wrote {path}")
    announce and print(f"point the dashboard at it: BEARHUG_COCKPIT={path} bearhug tui")
    return 0


def _safe_id(snapshot_id: str) -> str:
    return snapshot_id.replace("@", "-at-").replace("/", "-")


def _cmd_eval(args: argparse.Namespace) -> int:
    """5.3/5.8 — run a paid headless fixture eval or report already-persisted results."""
    from bearhug.evals import run_eval, write_eval_report

    if args.action == "judge":
        from bearhug.evals.judge import JudgeConfig, judge_run
        from bearhug.paths import RUNS_DIR

        if not args.run_id or Path(args.run_id).name != args.run_id:
            raise SystemExit("eval judge requires one --run-id directory name under runs/")
        run_root = RUNS_DIR / args.run_id
        try:
            final, judgment = judge_run(
                run_root,
                config=JudgeConfig(max_cost_usd=args.judge_max_cost_usd),
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"{final.upper()} {args.run_id} · judge {judgment.status} · "
            f"cost={judgment.cost_usd if judgment.cost_usd is not None else 'unreported'}"
        )
        return 0 if final == "pass" else 1

    snapshot = _resolve_snapshot(args.snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    snapshot_id = manifest.get("snapshot_id", snapshot.name)
    if args.action == "adjudicate":
        from bearhug.evals.adjudicate import adjudicate_run

        if not args.run_id or not args.verdict or not args.by:
            raise SystemExit("eval adjudicate requires --run-id, --verdict and --by")
        try:
            from bearhug import paths as _paths

            result = adjudicate_run(
                _paths.RUNS_DIR / args.run_id, verdict=args.verdict, by=args.by, note=args.note
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"{result['final_verdict'].upper()} {args.run_id} · {result['final_reason']}")
        return 0

    if args.action == "trimmed":
        from bearhug.evals.trimmed import build_trimmed_candidate

        inventory = REPORTS_DIR / f"rule-observability-{_safe_id(snapshot_id)}.json"
        if not inventory.is_file():
            raise SystemExit(
                f"no directive inventory at {inventory}; run `bearhug lint --observability` for "
                "this snapshot first"
            )
        rows = [
            r
            for r in json.loads(inventory.read_text(encoding="utf-8")).get("rows", [])
            if r.get("source_type") == "claude_directive"
        ]
        manifest = build_trimmed_candidate(
            snapshot, directives=rows, out_dir=REPO_ROOT / "evals" / "variants" / "trimmed"
        )
        crit = manifest["criteria"]
        print(
            f"trimmed candidate: {manifest['bytes']} bytes ≈ {manifest['estimated_tokens']} "
            f"tokens (source ≈ {manifest['source']['estimated_tokens']}); directives "
            f"{manifest['directives']['kept']}/{manifest['directives']['source']} kept; anchors "
            f"cited {manifest['anchors']['cited']}, broken {len(manifest['anchors']['broken'])}; "
            f"budget met={crit['budget']['met']}; prime-eval {crit['prime_eval']['status']}"
        )
        print(f"wrote {REPO_ROOT / 'evals' / 'variants' / 'trimmed'}")
        return 0

    if args.action == "battery" and (args.arm or args.runtime == "sealed"):
        from bearhug.evals.runner import run_sealed_battery

        if args.runtime != "sealed" or not args.arm:
            raise SystemExit(
                "the only battery beyond E10's is Sam's 2026-09-02 ruling: --runtime sealed with "
                "--arm full or --arm trimmed"
            )
        try:
            battery = run_sealed_battery(snapshot, snapshot_id=snapshot_id, arm=args.arm)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"{battery.stop_reason.upper()} {battery.battery_id} · arm {args.arm} · sealed "
            f"runtime · {len(battery.results)}/{battery.planned_runs} runs · "
            f"spent=${battery.spent_usd:.6f} · {battery.manifest}"
        )
        return 0 if battery.stop_reason == "completed" else 1

    if args.action == "battery":
        from bearhug.evals import run_approved_battery

        try:
            battery = run_approved_battery(snapshot, snapshot_id=snapshot_id)
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"{battery.stop_reason.upper()} {battery.battery_id} · "
            f"{len(battery.results)}/{battery.planned_runs} runs · "
            f"spent=${battery.spent_usd:.6f} · {battery.manifest}"
        )
        return 0 if battery.stop_reason == "completed" else 1
    if args.action == "report":
        try:
            json_path, md_path, findings = write_eval_report(
                snapshot_id=snapshot_id,
                runtime=None if args.pooled else args.runtime,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(md_path.read_text(encoding="utf-8"))
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        return 1 if any(finding.severity is Severity.BROKEN for finding in findings) else 0

    if args.action == "matrix":
        from bearhug.evals import load_results
        from bearhug.evals.matrix import EffectRule, build_matrix, render_matrix, write_matrix

        results = load_results(snapshot_id=snapshot_id)
        if not results:
            raise SystemExit(f"no persisted eval results for snapshot {snapshot_id}")
        arms = (
            tuple(args.arms.split(","))
            if args.arms
            else tuple(sorted({str(r.get("variant")) for r in results}))
        )
        scenarios = (
            tuple(args.scenarios.split(","))
            if args.scenarios
            else tuple(sorted({str(r.get("scenario")) for r in results}))
        )
        rule = None
        given = (args.rule_min_n, args.rule_min_delta, args.rule_approved_by)
        if any(v is not None for v in given):
            if not all(v is not None for v in given):
                raise SystemExit(
                    "an effect rule needs all of --rule-min-n, --rule-min-delta and "
                    "--rule-approved-by; a partial rule is not an approval"
                )
            rule = EffectRule(args.rule_min_n, args.rule_min_delta, args.rule_approved_by)
        matrix = build_matrix(
            results,
            arms=arms,
            scenarios=scenarios,
            snapshot_id=snapshot_id,
            runtime=None if args.pooled else args.runtime,
        )
        print(render_matrix(matrix, rule=rule))
        json_path, md_path = write_matrix(matrix, rule=rule)
        print(f"wrote {json_path}\nwrote {md_path}")
        return 0

    if not args.variant or not args.scenario:
        raise SystemExit("eval run requires --variant and --scenario")
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    failed = False
    for _ in range(args.repeat):
        try:
            result = run_eval(
                snapshot,
                snapshot_id=snapshot_id,
                variant=args.variant,
                scenario_name=args.scenario,
                max_budget_usd=args.max_budget_usd,
            )
        except (RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        state = "PASS" if result.passed else "FAIL"
        print(
            f"{state} {result.run_id} · {result.duration_ms:.0f}ms · {result.reason} · "
            f"{result.stream}"
        )
        failed = failed or not result.passed
    return 1 if failed else 0


def _cmd_drift(args: argparse.Namespace) -> int:
    report = compute_drift(_resolve_snapshot(args.snapshot))
    print(report.render())
    return 1 if report.moved else 0


def _cmd_prime(args: argparse.Namespace) -> int:
    """Print a non-mutating preflight and the exact commands an operator may run next."""
    from bearhug.prime import PrimeError, build_prime_report

    try:
        report = build_prime_report(
            args.subject,
            watcher_interval=args.watch,
            expected_watcher_id=args.watcher_id,
        )
    except PrimeError as exc:
        raise SystemExit(f"prime failed: {exc}") from exc
    print(report.render_json() if args.format == "json" else report.render())
    return 0


def _cmd_campaign(args: argparse.Namespace) -> int:
    """Prepare, run and control one native capsule campaign by its prepared locator."""

    from bearhug.campaign.cli import CampaignCommandError, run_campaign_command

    try:
        result = run_campaign_command(args)
    except CampaignCommandError as exc:
        raise SystemExit(f"campaign {args.action} failed: {exc}") from exc
    print(
        json.dumps(result.report, sort_keys=True, separators=(",", ":"))
        if args.format == "json"
        else result.human
    )
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bearhug",
        description=(
            "An internal Claude Code and Codex development harness for planning, provider "
            "execution, review, evidence, and live operator visibility."
        ),
    )
    parser.add_argument("--version", action="version", version=f"bearhug {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    from bearhug.project_setup import add_setup_parser
    from bearhug.startup import add_startup_parser

    add_setup_parser(sub)

    from bearhug.statusline import main as run_statusline

    statusline = sub.add_parser(
        "statusline",
        help="print one Bear Hug segment for a provider status line (payload on stdin)",
    )
    statusline.set_defaults(func=lambda args: run_statusline(None))

    from bearhug.project_hook_runtime import configure_parser as configure_automation
    from bearhug.project_hook_runtime import run as run_automation

    automation = sub.add_parser("automation", help="inspect and admit project automation")
    configure_automation(automation)
    automation.set_defaults(func=run_automation)

    from bearhug.project_work import configure_parser as configure_project_work
    from bearhug.project_work import run as run_project_work

    project_work = sub.add_parser(
        "project-work", help="accept plans and manage native project tasks"
    )
    configure_project_work(project_work)
    project_work.set_defaults(func=run_project_work)

    add_startup_parser(sub)
    from bearhug.terminal_driver import add_terminal_parser

    add_terminal_parser(sub)

    p = sub.add_parser("snapshot", help="freeze the harness under study into snapshots/<date>/")
    p.add_argument("--date", help="snapshot label (default: today, UTC)")
    p.add_argument("--dry-run", action="store_true", help="list what would be captured")
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite a label that already holds a DIFFERENT snapshot",
    )
    p.set_defaults(func=_cmd_snapshot)

    p = sub.add_parser("drift", help="has the live harness moved away from a snapshot?")
    p.add_argument("snapshot", nargs="?", help="snapshot id (default: the most recent)")
    p.set_defaults(func=_cmd_drift)

    p = sub.add_parser("prime", help="inspect one explicit subject and print watcher/TUI commands")
    p.add_argument("subject", help="explicit path inside the Git worktree to observe")
    p.add_argument(
        "--watch",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="watcher interval to print (default: 30; minimum: 5)",
    )
    p.add_argument(
        "--watcher-id",
        help="expected watcher heartbeat identity for replacement detection",
    )
    p.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="stdout rendering (default: human; no report file is written)",
    )
    p.set_defaults(func=_cmd_prime)

    p = sub.add_parser(
        "campaign",
        help="prepare, run and control one native capsule campaign; integrate accepted candidates",
    )
    p.add_argument(
        "action",
        choices=(
            "prepare",
            "run",
            "status",
            "answer",
            "resume",
            "recover",
            "stop",
            "integrate",
        ),
    )
    p.add_argument(
        "locator",
        nargs="?",
        help="prepared.json/run locator; required for every action except prepare and integrate",
    )
    p.add_argument("--subject", help="prepare: exact clean physical Git worktree root")
    p.add_argument("--intent", help="prepare: exact approved intent-envelope JSON path")
    p.add_argument(
        "--capsule-plan",
        dest="capsule_plan",
        help="prepare: optional explicit capsule plan; otherwise compile one from the intent",
    )
    p.add_argument("--campaign-id", help="exact campaign id")
    p.add_argument("--run-id", help="exact run id")
    p.add_argument("--state-root", help="explicit durable directory for this prepared campaign")
    p.add_argument("--lease-root", help="explicit durable campaign lease-store directory")
    p.add_argument(
        "--worktree-parent",
        help="existing owner-only directory where a deterministic bh-* worktree is created",
    )
    p.add_argument("--policy", help="prepare: provider-role policy JSON path")
    p.add_argument(
        "--execution-config",
        dest="execution_config",
        help="prepare: one closed executable-authority JSON file",
    )
    p.add_argument(
        "--qualification-index",
        help="exact local capture-backed provider qualification index",
    )
    p.add_argument("--review-role", help="read-only provider-policy role used for review")
    p.add_argument(
        "--question",
        "--question-id",
        dest="question_id",
        help="answer only: exact active HIL question id",
    )
    p.add_argument("--answer-file", help="answer only: bounded UTF-8 response file")
    p.add_argument("--answer", help="answer only: bounded response value")
    p.add_argument(
        "--disposition",
        choices=("approve", "deny", "amend", "defer", "stop"),
        help="answer only: response disposition",
    )
    p.add_argument(
        "--recovery-outcome",
        choices=("failed", "blocked", "hil_required"),
        help="recover only: explicit durable recovery outcome",
    )
    p.add_argument("--episode-id", help="recover: exact episode identity")
    p.add_argument("--review-id", help="recover: exact review identity")
    p.add_argument(
        "--successor-plan",
        dest="successor_plan",
        help="answer/resume/recover: successor capsule-plan JSON path",
    )
    p.add_argument("--reason", help="stop only: bounded operator reason")
    p.add_argument(
        "--integration-state-root",
        help="integrate only: owner-only directory for integration receipts",
    )
    p.add_argument(
        "--integration-target",
        help="integrate only: explicit clean owner-only Git worktree to mutate",
    )
    p.add_argument(
        "--integration-base-oid",
        help="integrate only: full Git object id at the target's clean starting HEAD",
    )
    p.add_argument(
        "--integration-id",
        help="integrate only: exact create-only integration receipt id",
    )
    p.add_argument(
        "--integrator-id",
        help="integrate only: exact local integration-owner identity",
    )
    p.add_argument(
        "--integration-inputs",
        help="integrate only: canonical JSON array of accepted candidate inputs",
    )
    p.add_argument(
        "--integration-check",
        action="append",
        metavar="COMMAND",
        help=(
            "integrate only: one shell-free argv string; repeat for ordered checks, "
            "for example 'git status --porcelain'"
        ),
    )
    p.add_argument(
        "--integration-check-timeout",
        type=float,
        default=28800.0,
        metavar="SECONDS",
        help="integrate only: timeout per merge/check (default: 28800)",
    )
    p.add_argument(
        "--integration-review-store",
        help="integrate only: owner-only review receipt store for reviewed integration",
    )
    p.add_argument(
        "--integration-review",
        action="append",
        metavar="WORK_UNIT_ID=REVIEW_ID",
        help=(
            "integrate only: eligible review receipt for one work unit; repeat for quorum "
            "and every input"
        ),
    )
    p.add_argument(
        "--integration-min-approvals",
        type=int,
        default=1,
        metavar="COUNT",
        help="integrate only: required independent approvals when review receipts are supplied",
    )
    p.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="stdout rendering (default: human; no report file is written)",
    )
    p.set_defaults(func=_cmd_campaign)

    p = sub.add_parser("lint", help="static analysis of a snapshotted CLAUDE.md")
    p.add_argument("snapshot", nargs="?", help="snapshot id (default: the most recent)")
    p.add_argument("--format", choices=("md", "json"), default="md")
    p.add_argument("--dump-tree", action="store_true", help="print the parsed section tree only")
    p.add_argument("--all", action="store_true", help="include INFO findings in the report")
    p.add_argument(
        "--budget",
        action="store_true",
        help="2.2 — cold-session token budget (CLAUDE.md, ambient, MCP, real hook_inject traffic)",
    )
    p.add_argument("--since", help="--budget only: only hook_inject traffic on or after this date")
    p.add_argument(
        "--observability",
        action="store_true",
        help="M07 — inventory stated rules, hook registrations, and existing detectors",
    )
    p.add_argument(
        "--ci-runner",
        action="store_true",
        help="H05 — emit a patch adding a bounded per-suite CI runner for scripts/hooks/*_test.py",
    )
    p.add_argument(
        "--gate-inventory",
        action="store_true",
        help="H02 — emit a patch adding a GENERATED gate inventory derived from settings.json",
    )
    p.add_argument(
        "--nag-vs-gate",
        action="store_true",
        help="M09 — classify rules as prose-only / advisory / blocking and mechanisms by reach",
    )
    p.add_argument(
        "--run-configs",
        action="store_true",
        help="G02 — conformance of the captured GoLand run configurations",
    )
    p.set_defaults(func=_cmd_lint)

    p = sub.add_parser("hooks", help="run the hook conformance battery")
    p.add_argument("action", choices=("run", "audit", "stop-census", "write-detection"))
    p.add_argument("--hook", help="a single hook script to exercise")
    p.add_argument("--event", help="event fixture name")
    p.add_argument("snapshot", nargs="?", help="snapshot id (default: the most recent)")
    p.set_defaults(func=_cmd_hooks)

    p = sub.add_parser("replay", help="mine real session transcripts")
    p.add_argument(
        "action",
        choices=(
            "metrics",
            "ledger",
            "report",
            "injection",
            "dlv-depth",
            "go-coverage",
            "harness-versions",
            "ground-truth",
            "jobs-census",
        ),
    )
    p.add_argument("--classifier", help="ground-truth only: dlv or go-write")
    p.add_argument("--per-class", type=int, default=40, help="ground-truth only: sample cap")
    p.add_argument("--seed", type=int, default=2026, help="ground-truth only: sampling seed")
    p.add_argument(
        "--score",
        action="store_true",
        help="ground-truth only: score labels/<set>/labels.json and write the validation report",
    )
    p.add_argument(
        "--attribution",
        metavar="REPORT",
        help="ledger only: a harness-versions report (M13) — bucket pre/post by each transcript's "
        "resolved harness version instead of the gate's landing date",
    )
    p.add_argument(
        "--label-set",
        metavar="DIR",
        help="ground-truth --score only: the pinned labels/<set> folder to score, whatever the "
        "corpus digest is now",
    )
    p.add_argument(
        "--approve-thresholds",
        metavar="WHO",
        help="ground-truth --score only: record who approved the thresholds (e.g. 'sam "
        "2026-09-01'); without an approval on file a validated score publishes no rate",
    )
    p.add_argument(
        "--artifact",
        help="harness-versions only: the M11 artifact a project-owned session returned",
    )
    p.add_argument("--corpus", choices=("frozen", "live"), default="frozen")
    p.add_argument("--since", help="only events on or after this date (YYYY-MM-DD)")
    p.add_argument(
        "--snapshot", help="snapshot id `ledger` findings are filed against (default: 2026-08-29)"
    )
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("eval", help="A/B harness variants through headless claude -p")
    p.add_argument(
        "action", choices=("run", "report", "matrix", "battery", "judge", "trimmed", "adjudicate")
    )
    p.add_argument(
        "--arm", choices=("full", "trimmed"), help="battery only: the sealed-runtime arm"
    )
    p.add_argument(
        "--pooled",
        action="store_true",
        help="matrix/report: pool every runtime instead of restricting to --runtime (default: "
        "the snapshot's own runtime; the file names carry the choice)",
    )
    p.add_argument(
        "--runtime",
        choices=("snapshot", "sealed"),
        default="snapshot",
        help="battery/matrix: which runtime the fixture ran (default: the snapshot's own)",
    )
    p.add_argument("--verdict", choices=("pass", "fail", "unscored"), help="adjudicate only")
    p.add_argument("--by", help="adjudicate only: who ruled, e.g. 'sam 2026-09-02'")
    p.add_argument("--note", default="", help="adjudicate only: why")
    p.add_argument("--arms", help="matrix only: comma-separated variants (default: all present)")
    p.add_argument(
        "--scenarios", help="matrix only: comma-separated scenarios (default: all present)"
    )
    p.add_argument("--rule-min-n", type=int, help="matrix only: approved minimum runs per arm")
    p.add_argument(
        "--rule-min-delta", type=float, help="matrix only: approved minimum pass-rate delta"
    )
    p.add_argument("--rule-approved-by", help="matrix only: who approved the effect rule (E08)")
    p.add_argument("--variant")
    p.add_argument("--scenario")
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--max-budget-usd", type=float, default=2.0)
    p.add_argument("--run-id", help="judge only: persisted directory name under runs/")
    p.add_argument(
        "--judge-max-cost-usd",
        type=float,
        default=0.10,
        help="judge only: conservative per-judgment ceiling (default: 0.10)",
    )
    p.add_argument("--snapshot", help="snapshot id (default: the most recent)")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("cockpit", help="emit reports/cockpit.json, the artifact the TUI reads")
    p.add_argument("--snapshot", help="snapshot id to carry findings for (default: the latest)")
    p.add_argument("--session", help="session id whose live task store to join to the BOARD")
    p.add_argument(
        "--provider-work-observation",
        help="exact provider-work-observation.v1 file to project; never directory-scanned",
    )
    p.add_argument(
        "--work-binding",
        help="exact work-binding.v1 file to verify against the selected subject",
    )
    p.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="rewrite the artifact on this interval until interrupted (K07b); the TUI dates it",
    )
    p.set_defaults(func=_cmd_cockpit)

    p = sub.add_parser(
        "work",
        help=(
            "bind one explicit Claude Code task store or Codex goal observation to project "
            "authority"
        ),
    )
    p.add_argument("action", choices=("bind",))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--observation",
        help="exact provider-work-observation.v1 JSON file (normally emitted by a Codex run)",
    )
    source.add_argument(
        "--claude-task-store",
        help="exact Claude Code session task-store directory; no newest-session discovery",
    )
    p.add_argument("--subject", required=True, help="explicit project Git worktree")
    p.add_argument(
        "--task-id",
        help="Claude Code task id to bind; omit for Codex or an empty Claude Code task store",
    )
    p.add_argument(
        "--active-plan",
        required=True,
        help="repository-relative Markdown path of the selected active plan",
    )
    p.add_argument(
        "--candidate",
        action="append",
        type=_board_candidate,
        default=[],
        metavar="ROW=AUTHORITY_PATH",
        help="explicit BOARD row and parsed authority path; repeat for ambiguity, omit for missing",
    )
    p.add_argument(
        "--output-root",
        required=True,
        help="existing absolute Bear Hug artifact root outside the subject checkout",
    )
    p.set_defaults(func=_cmd_work)

    p = sub.add_parser(
        "provider", help="run a provider turn with raw event custody and cockpit observation"
    )
    p.add_argument("action", choices=("run",))
    p.add_argument("--provider", choices=("codex", "claude"))
    p.add_argument("--policy", help="closed provider-role policy JSON")
    p.add_argument("--role", help="role in --policy (for example author or reviewer)")
    p.add_argument("--cwd", required=True, help="absolute or user-relative workspace directory")
    p.add_argument("--model", help="exact provider model id")
    p.add_argument("--effort", help="exact requested reasoning effort")
    p.add_argument(
        "--prompt-file",
        required=True,
        help="UTF-8 prompt file; use - to read stdin and keep the prompt out of argv",
    )
    p.add_argument("--sandbox", choices=("read-only", "workspace-write"))
    p.add_argument(
        "--approval-policy",
        choices=("never", "on-request", "untrusted"),
        default=None,
        help="interactive requests fail closed in this non-interactive adapter",
    )
    p.add_argument("--timeout", type=float, default=28800.0)
    p.add_argument(
        "--provider-bin", help="provider executable; defaults to the selected provider name"
    )
    p.set_defaults(func=_cmd_provider)

    p = sub.add_parser(
        "harness",
        aliases=("native",),
        help="validate and transact one explicit sealed provider harness bundle",
    )
    p.add_argument(
        "action",
        choices=(
            "validate",
            "dry-run",
            "install",
            "upgrade",
            "uninstall",
            "read-installed",
            "attest",
        ),
    )
    p.add_argument("--policy", help="exact harness-policy.v2 JSON")
    p.add_argument("--manifest", help="exact provider-materialization manifest JSON")
    p.add_argument("--source-root", help="complete manifest-declared bundle directory")
    p.add_argument("--subject", help="exact clean Git worktree root")
    p.add_argument("--state-root", help="private installer state directory outside the checkout")
    p.add_argument("--provider", choices=("claude", "codex"))
    p.add_argument("--format", choices=("human", "json"), default="human")
    p.set_defaults(func=_cmd_harness)

    p = sub.add_parser(
        "tui",
        help="live session dashboard over a repository's worktrees",
        description=(
            "Live session dashboard over a repository's worktrees. The CAMPAIGN tile is "
            "populated automatically when <repo> has an attached campaign "
            "(.bearhug/campaign.json) -- there is no separate flag to name a campaign or "
            "state root for it. Use --run-locator to project one explicit native "
            "prepared-campaign locator instead of a repo."
        ),
    )
    p.add_argument("--web", action="store_true", help="show the same Go cockpit in a local browser")
    p.add_argument(
        "repo",
        nargs="?",
        help="repository/worktree to watch (default: configured subject); "
        "an attached campaign is shown automatically, no extra flags needed",
    )
    p.add_argument("--build", action="store_true", help="compile to tui/tui instead of running")
    p.add_argument("--snapshot", help="snapshot id or path to carry into the live cockpit")
    p.add_argument(
        "--refresh",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="private cockpit refresh interval (default: 30; minimum: 5)",
    )
    p.add_argument("--session", help="explicit legacy Claude Code session id to project")
    p.add_argument(
        "--provider-work-observation",
        help="exact provider-work-observation.v1 file to project",
    )
    p.add_argument("--work-binding", help="exact work-binding.v1 file to verify and project")
    p.add_argument(
        "--authority-scope",
        choices=("full", "bound"),
        default="full",
        help="PLAN/BOARD parser scope (bound requires --active-plan and --board-row)",
    )
    p.add_argument(
        "--active-plan",
        help="repository-relative active PLAN path for --authority-scope bound",
    )
    p.add_argument(
        "--board-row",
        help="explicit current BOARD row for --authority-scope bound",
    )
    p.add_argument(
        "--run-locator",
        help="native prepared.json/run locator to project in the campaign TUI",
    )
    p.set_defaults(func=_cmd_tui)

    p = sub.add_parser("report", help="merge every phase into one ranked defect list")
    p.add_argument("snapshot", nargs="?", help="snapshot id (default: the most recent)")
    p.add_argument("--corpus", choices=("frozen", "live"), default="frozen")
    p.add_argument("--since", help="only replay events on or after this date (YYYY-MM-DD)")
    p.add_argument(
        "--compare",
        nargs=2,
        metavar=("OLD", "NEW"),
        help="print resolved/new/unchanged findings between two snapshots, by finding id",
    )
    p.add_argument(
        "--build-manifest",
        nargs=2,
        metavar=("OLD_ID", "NEW_ID"),
        help="S03 — write reports/regression-<old>-<new>.json naming the persisted phase "
        "artifacts, corpus digest, installed runtime hashes and classifier versions for two "
        "snapshot ids (label@hash)",
    )
    p.add_argument(
        "--compare-manifest",
        metavar="MANIFEST",
        help="S04 — compare the persisted phase artifacts a regression-run manifest names",
    )
    p.add_argument(
        "--emit-patches",
        action="store_true",
        help="S01 — write patches/<finding-id>.diff for every mechanically fixable finding and "
        "list every skipped finding with its reason",
    )
    p.set_defaults(func=_cmd_report)

    add_arch_parser(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
