"""Execute declared validation profiles and retain exact candidate/command/output evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bearhug.campaign.capsule_runtime import _create_blob_only
from bearhug.campaign.integration import _command, _private_directory, _run_command
from bearhug.campaign.review import canonical_json_sha256
from bearhug.providers.receipt import LaunchRepository, capture_close_repository


class CapsuleValidationError(ValueError):
    """Validation did not establish exact clean candidate evidence."""


# Ceiling for one validation run, and the default below it. Deliberately generous:
# see the operating limits note in README.md.
MAX_VALIDATION_TIMEOUT_SECONDS = 28800.0
MAX_VALIDATION_OUTPUT_BYTES = 256 * 1024 * 1024
# Only a standalone token is shell glue. The same characters inside an argument are
# inert without a shell -- `python3 -c "import time; time.sleep(1)"` is one argument.
_SHELL_GLUE = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "2>&1"})


def _resolve_argv(argv, worktree: Path) -> tuple[Path, tuple[str, ...]]:
    """Read a sealed command the way the launcher's allowlist already reads it.

    Validation runs argv without a shell, so ``cd barracuda && prog`` executed the program
    ``cd`` with the rest as arguments. macOS ships /usr/bin/cd as a real binary that does
    nothing and exits 0, so the check never ran and was recorded as passed -- measured on
    row 240, where a deliberately failing Go test produced a passing validation receipt.

    A leading ``cd`` becomes the working directory. Any other shell construction is refused
    here rather than handed to a program as an argument, because a gate that cannot run the
    command it was given must say so instead of passing.
    """

    root = worktree.resolve()
    cwd = root
    tokens = [str(token) for token in argv]
    index = 0
    while index + 1 < len(tokens) and tokens[index] == "cd":
        target = (cwd / tokens[index + 1]).resolve()
        if not target.is_dir() or (target != root and root not in target.parents):
            raise CapsuleValidationError(
                f"validation command changes directory outside the candidate: {tokens[index + 1]}"
            )
        cwd = target
        index += 2
        if index < len(tokens) and tokens[index] == "&&":
            index += 1
    rest = tokens[index:]
    if not rest or rest[0] == "cd":
        raise CapsuleValidationError("validation command runs no program")
    for token in rest:
        if token in _SHELL_GLUE:
            raise CapsuleValidationError(
                f"validation command needs a shell, which validation never uses: {token!r}; "
                "put it in a script and name that script instead"
            )
    return cwd, tuple(rest)

def _bytes(value):
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    ).encode()


def _capture(worktree, candidate):
    tree = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", f"{candidate['base_oid']}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _, observed = capture_close_repository(
        Path(worktree),
        LaunchRepository(
            candidate["repository_common_dir_sha256"], candidate["base_oid"], tree, True
        ),
        require_unchanged=False,
    )
    if observed is None or observed.to_dict() != candidate:
        raise CapsuleValidationError("validation candidate is stale or dirty")


def _required(profiles, commands):
    refs = {ref for profile in profiles for ref in profile["command_refs"]}
    if not refs or refs != set(commands):
        raise CapsuleValidationError(
            "commands must exactly resolve the sealed validation profile refs"
        )
    return {ref: _command(commands[ref], label=ref) for ref in sorted(refs)}


def execute_capsule_validation(
    *,
    candidate_worktree: Path | str,
    candidate: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    commands: Mapping[str, Sequence[str]],
    state_root: Path | str,
    timeout_s: float = 28800.0,
) -> dict[str, Any]:
    """Run actual argv without a shell, failing if checks change the committed candidate."""
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not 0 < timeout_s <= MAX_VALIDATION_TIMEOUT_SECONDS
    ):
        raise CapsuleValidationError(
            f"validation timeout must be bounded to {MAX_VALIDATION_TIMEOUT_SECONDS:.0f} seconds"
        )
    declared = _required(profiles, commands)
    root = _private_directory(state_root, create=True)
    _capture(candidate_worktree, candidate)
    rows = []
    for ref, argv in declared.items():
        cwd, program = _resolve_argv(argv, Path(candidate_worktree))
        run = _run_command(subprocess.run, program, cwd=cwd, timeout=timeout_s)
        outputs = {}
        for stream, raw in (("stdout", run.stdout), ("stderr", run.stderr)):
            if len(raw) > MAX_VALIDATION_OUTPUT_BYTES:
                raise CapsuleValidationError("validation output exceeds the evidence bound")
            digest = hashlib.sha256(raw).hexdigest()
            _create_blob_only(root / f"{digest}.bin", raw)
            outputs[f"{stream}_sha256"] = digest
        rows.append(
            {
                "command_ref": ref,
                "argv": list(argv),
                "status": run.status,
                "returncode": run.returncode,
                **outputs,
            }
        )
        _capture(candidate_worktree, candidate)
    record = {
        "record_kind": "capsule_validation",
        "candidate": dict(candidate),
        "profiles": list(profiles),
        "commands": rows,
        "status": "pass" if all(row["status"] == "passed" for row in rows) else "fail",
    }
    digest = canonical_json_sha256(record)
    _create_blob_only(root / f"{digest}.bin", _bytes(record))
    return {"receipt_sha256": digest, "record": record}


def verify_capsule_validation(
    *,
    state_root: Path | str,
    receipt_sha256: str,
    candidate_worktree: Path | str,
    candidate: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    commands: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Reopen all command/output bytes; provider claims and arbitrary evidence refs do not pass."""
    if (
        not isinstance(receipt_sha256, str)
        or len(receipt_sha256) != 64
        or any(char not in "0123456789abcdef" for char in receipt_sha256)
    ):
        raise CapsuleValidationError("validation receipt digest is invalid")
    root = _private_directory(state_root, create=False)
    path = root / f"{receipt_sha256}.bin"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise CapsuleValidationError("validation receipt is unavailable")
    raw = path.read_bytes()
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CapsuleValidationError("validation receipt is invalid JSON") from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"record_kind", "candidate", "profiles", "commands", "status"}
        or _bytes(record) != raw
        or canonical_json_sha256(record) != receipt_sha256
    ):
        raise CapsuleValidationError("validation receipt content changed")
    if record["record_kind"] != "capsule_validation" or record["candidate"] != candidate:
        raise CapsuleValidationError("validation receipt binds another candidate")
    if record["profiles"] != list(profiles):
        raise CapsuleValidationError("validation receipt binds other sealed profiles")
    _capture(candidate_worktree, candidate)
    rows = record["commands"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or set(row)
        != {"command_ref", "argv", "status", "returncode", "stdout_sha256", "stderr_sha256"}
        for row in rows
    ):
        raise CapsuleValidationError("validation command evidence is invalid")
    observed_commands = {row["command_ref"]: row["argv"] for row in rows}
    if len(observed_commands) != len(rows):
        raise CapsuleValidationError("validation repeats a command ref")
    _required(profiles, observed_commands)
    if commands is not None and observed_commands != {
        key: list(argv) for key, argv in commands.items()
    }:
        raise CapsuleValidationError("validation command definitions changed")
    if record["status"] != "pass" or any(
        row["status"] != "passed" or type(row["returncode"]) is not int or row["returncode"] != 0
        for row in rows
    ):
        raise CapsuleValidationError("actual validation commands did not pass")
    for row in rows:
        for stream in ("stdout", "stderr"):
            digest = row[f"{stream}_sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise CapsuleValidationError("validation output digest is invalid")
            path = root / f"{digest}.bin"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                raise CapsuleValidationError("validation output custody is unavailable")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise CapsuleValidationError("validation output bytes changed")
    return record
