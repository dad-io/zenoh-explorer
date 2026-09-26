"""Project lifecycle integration for the existing A01–A06 architecture tools.

Only generated observations under .bearhug/architecture are written. Project decisions,
rules and canonical indexes remain authored inputs. No provider-specific gate is invented.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from bearhug.arch.documentation import observe as documentation_coverage
from bearhug.arch.extract import extract, repository_state
from bearhug.arch.freshness import verdict_for
from bearhug.arch.render import render_markdown
from bearhug.arch.rules import evaluate, load_rules

DIRECTORY = ".bearhug/architecture"
LIMIT = (
    "Go structure, Memex decision links and Claude hook registrations; "
    "not a general schema catalog."
)


def _path(root: Path, relative: str) -> Path:
    path = root
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise ValueError(f"Architecture path is symlinked: {relative}")
    return path


def _write(path: Path, value: object) -> None:
    content = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".refresh-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def refresh(root: Path) -> dict:
    """Refresh on a lifecycle boundary; concurrent hooks leave the active writer alone."""
    directory = _path(root, DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    with _path(root, f"{DIRECTORY}/refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return status(root)
        report = {
            "status": "refreshing",
            "reason": (
                "Refresh unfinished; interrupted refreshes retry at the next lifecycle event."
            ),
            "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "index": f"{DIRECTORY}/index.json",
            "rendered": f"{DIRECTORY}/ARCHITECTURE.generated.md",
            "limits": LIMIT,
        }
        target = _path(root, f"{DIRECTORY}/status.json")
        _write(target, report)
        try:
            report["documentation"] = documentation_coverage(root)
            artifact = extract(root)
            head, dirty, _ = repository_state(root)
            validation = verdict_for(artifact, head=head, dirty=dirty).as_dict()
            counts = dict(Counter(record["kind"] for record in artifact["records"]))
            report.update(
                status=validation["verdict"],
                reason="; ".join(validation["problems"]),
                validation=validation,
                counts=counts,
                head=head,
                rules={
                    "status": "not_configured",
                    "reason": "No project docs/arch-rules.json; no rules inferred.",
                },
                canonical={"status": "absent", "path": "docs/architecture/index.json"},
            )
            if not counts.get("package"):
                report.update(status="partial", reason="No Go packages indexed; " + LIMIT)
            rules = _path(root, "docs/arch-rules.json")
            if rules.is_file():
                results = evaluate(load_rules(rules), artifact["records"])
                report["rules"] = {
                    "status": "evaluated",
                    "path": "docs/arch-rules.json",
                    "results": [
                        {
                            "id": item.rule_id,
                            "verdict": item.verdict.value,
                            "offenders": list(item.offender_ids),
                            "reason": item.reason,
                        }
                        for item in results
                    ],
                }
            canonical = _path(root, "docs/architecture/index.json")
            if canonical.is_file():
                report["canonical"] = {
                    "path": "docs/architecture/index.json",
                    **verdict_for(
                        json.loads(canonical.read_text()), head=head, dirty=dirty
                    ).as_dict(),
                }
            _write(_path(root, report["index"]), artifact)
            _write(_path(root, report["rendered"]), render_markdown(artifact))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            report.update(status="error", reason=f"Architecture refresh failed: {exc}")
        _write(target, report)
        return report


def status(root: Path) -> dict:
    """Read-only dashboard projection; never invoke extraction to manufacture freshness."""
    try:
        path = _path(root, f"{DIRECTORY}/status.json")
        if not path.is_file():
            return {
                "status": "unreported",
                "reason": "Awaiting SessionStart or Stop hook.",
                "limits": LIMIT,
            }
        report = json.loads(path.read_text())
        if report["status"] in {"error", "refreshing"}:
            return report
        head, dirty, _ = repository_state(root)
        if head != report.get("head"):
            report.update(status="stale", reason="Project commit changed since the last refresh.")
        elif dirty and report["status"] == "fresh":
            report.update(
                status="partial", reason="Project has uncommitted changes since the last refresh."
            )
        return report
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "reason": f"Cannot read architecture observation: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("operation", choices=("refresh", "status"), default="status", nargs="?")
    args = parser.parse_args()
    report = (refresh if args.operation == "refresh" else status)(args.root.resolve(strict=True))
    print(json.dumps(report))
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
