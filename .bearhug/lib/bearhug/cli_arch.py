"""`bearhug arch …` — the architecture-index commands (A03, A05).

Kept out of `cli.py` on purpose: the A-track registers one subcommand tree through
`add_arch_parser`, so the shared CLI carries a two-line hook rather than another 200 lines.

    bearhug arch extract  <repo-root> --out <path>
    bearhug arch validate <artifact> [--head <commit>] [--dirty]
    bearhug arch render   <artifact> --out <md>              (A04)
    bearhug arch context  <artifact> --query <term>          (A05)
    bearhug arch rules    <artifact> [--rules <path>] [--format md|json]   (A06)

`extract` reads a tree and writes an artifact somewhere the charter permits; pointed at
`project-barracuda` it still refuses to write *into* it, because the write goes through
`paths.assert_writable`. Producing Barracuda's canonical `docs/architecture/index.json` is a
Barracuda-owned action — see `patches/A04-ARCHITECTURE-INDEX-HANDOFF.md`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bearhug.arch.extract import extract, write_artifact
from bearhug.arch.freshness import Freshness, schema_problems, verdict_for


def add_arch_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register `arch` and its subcommands on the shared `bearhug` parser."""
    parser = subparsers.add_parser(
        "arch",
        help="the canonical architecture index: extract, validate, render, and query it",
    )
    parser.set_defaults(func=cmd_arch, arch_command=None)
    inner = parser.add_subparsers(dest="arch_command", metavar="SUBCOMMAND", required=True)

    p = inner.add_parser("extract", help="extract an architecture index from a tree")
    p.add_argument("root", help="the repository root to read (read-only)")
    p.add_argument("--out", required=True, help="where to write the artifact")
    p.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="size cap; a file above it is a disclosed parse failure, never a silent omission",
    )
    p.set_defaults(func=cmd_arch)

    p = inner.add_parser("validate", help="is this artifact still describing the tree?")
    p.add_argument("artifact", help="the architecture index to validate")
    p.add_argument("--head", help="the commit the tree is at; omitted means never verified")
    p.add_argument("--dirty", action="store_true", help="the working tree has uncommitted changes")
    p.set_defaults(func=cmd_arch)

    p = inner.add_parser("render", help="render ARCHITECTURE.generated.md from an artifact")
    p.add_argument("artifact", help="the architecture index to render")
    p.add_argument("--out", required=True, help="where to write the markdown projection")
    p.set_defaults(func=cmd_arch)

    p = inner.add_parser("context", help="select bounded, cited context from a fresh artifact")
    p.add_argument("artifact", help="the architecture index to query")
    p.add_argument("--query", required=True, help="term to match against indexed facts")
    p.add_argument("--head", help="the commit the tree is at; omitted means never verified")
    p.set_defaults(func=cmd_arch)

    p = inner.add_parser("rules", help="check ruled architecture rules against an artifact")
    p.add_argument("artifact", help="the architecture index to check")
    p.add_argument(
        "--rules", default=None, help="explicit project rules file; no global rules are assumed"
    )
    p.add_argument("--format", choices=("md", "json"), default="md", help="output format")
    p.set_defaults(func=cmd_arch)

    return parser


def cmd_arch(args: argparse.Namespace) -> int:
    """Dispatch one `arch` subcommand. Exit codes carry the verdict, not just success."""
    command = getattr(args, "arch_command", None)
    if command == "extract":
        return _extract(args)
    if command == "validate":
        return _validate(args)
    if command == "render":
        return _render(args)
    if command == "context":
        return _context(args)
    if command == "rules":
        return _rules(args)
    raise SystemExit("bearhug arch: choose extract, validate, render, context, or rules")


def _extract(args: argparse.Namespace) -> int:
    kwargs = {}
    if args.max_file_bytes is not None:
        kwargs["max_file_bytes"] = args.max_file_bytes
    artifact = extract(args.root, **kwargs)
    written = write_artifact(artifact, args.out)
    repository = artifact["repository"]
    kinds: dict[str, int] = {}
    for record in artifact["records"]:
        kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
    print(f"wrote {written}")
    print(f"  head      {repository['head'] or 'unknown'}")
    print(f"  dirty     {repository['dirty']} ({len(repository['dirty_paths'])} path(s))")
    print(f"  modules   {len(repository['source_scope']['modules'])}")
    print(f"  records   {len(artifact['records'])}  " + "  ".join(
        f"{kind}={count}" for kind, count in sorted(kinds.items())
    ))
    print(f"  failures  {len(artifact['parse_failures'])}")
    print(f"  identity  {artifact['identity']['records_sha256'][:12]}")
    return 0


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate(args: argparse.Namespace) -> int:
    result = verdict_for(_load(args.artifact), head=args.head, dirty=args.dirty)
    print(f"verdict: {result.verdict.value}")
    for problem in result.problems:
        print(f"  - {problem}")
    # A non-fresh index must not exit 0: a caller that ignores the verdict would otherwise read a
    # stale artifact as an architectural claim, which is the failure A05 exists to prevent.
    return 0 if result.verdict is Freshness.FRESH else 1


def _render(args: argparse.Namespace) -> int:
    from bearhug.arch.render import render_markdown
    from bearhug.paths import assert_writable

    target = assert_writable(Path(args.out))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(_load(args.artifact)), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def _context(args: argparse.Namespace) -> int:
    from bearhug.arch.context import select

    result = select(_load(args.artifact), args.query, head=args.head)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


def _rules(args: argparse.Namespace) -> int:
    from bearhug.arch.rules import RulesLoadError, RuleVerdict, evaluate, load_rules, to_findings
    if not args.rules:
        print("No project rules selected; supply --rules /path/to/project/docs/arch-rules.json")
        return 1
    rules_path = args.rules
    try:
        rules_payload = load_rules(rules_path)
    except RulesLoadError as error:
        print(f"error loading {rules_path}: {error}")
        return 1

    artifact = _load(args.artifact)
    problems = schema_problems(artifact)
    if problems:
        print(f"error validating {args.artifact}:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    results = evaluate(rules_payload, artifact.get("records", []))

    if args.format == "json":
        snapshot = (artifact.get("repository") or {}).get("head") or "unknown"
        findings = to_findings(results, snapshot=snapshot)
        print(json.dumps([finding.to_dict() for finding in findings], indent=2, sort_keys=True))
    else:
        for result in results:
            print(result.line())

    # A FAIL and an UNEVALUABLE are both a reason not to trust the rule set silently; only a
    # clean sweep of PASS may exit 0, the same non-negotiable a caller gets from `validate`.
    return 0 if all(result.verdict is RuleVerdict.PASS for result in results) else 1


__all__ = ["add_arch_parser", "cmd_arch"]
