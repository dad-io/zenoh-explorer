"""Read and seal one explicit project-authored campaign typeset.

The importer is deliberately read-only.  It accepts four repository-relative locators from the
caller, opens every local source without following symlinks, validates the existing plan-corpus
and campaign contracts, and binds them to the exact clean Git worktree named by the caller.  It
does not discover plans, infer work from prose, launch a provider, or create campaign state.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.campaign.contracts import (
    CampaignContractError,
    ContractValidationResult,
    validate_campaign_run,
    validate_campaign_template,
    validate_programme_campaign_index,
)
from bearhug.campaign.corpus import CorpusManifestError, CorpusValidationResult, validate_manifest
from bearhug.host_git import describe_dirty_status, run_git

_MAX_INPUT_BYTES = 64 * 1024 * 1024


class CampaignImportError(ValueError):
    """The explicit campaign typeset cannot be trusted for planning."""


@dataclass(frozen=True, slots=True)
class SubjectIdentity:
    """Exact, read-only identity of the supplied clean physical worktree."""

    root: Path
    repository_root_sha256: str
    common_dir: Path
    repository_common_dir_sha256: str
    head_oid: str
    tree_oid: str
    base_tree_sha256: str
    branch: str


@dataclass(frozen=True, slots=True)
class CampaignTypeset:
    """Validated values and identities used by the deterministic planner."""

    subject: SubjectIdentity
    corpus_path: str
    template_path: str
    run_path: str
    index_path: str
    corpus: dict[str, Any]
    template: dict[str, Any]
    run: dict[str, Any]
    index: dict[str, Any]
    corpus_validation: CorpusValidationResult
    template_validation: ContractValidationResult
    run_validation: ContractValidationResult
    index_validation: ContractValidationResult


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _repository_path(value: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise CampaignImportError(f"{label} must be a repository-relative path")
    parts = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 4096
        or path.is_absolute()
        or value.startswith("~")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise CampaignImportError(f"{label} must be one canonical repository-relative file path")
    return tuple(parts)


def _read_no_follow(root_fd: int, relative: str, label: str) -> bytes:
    """Open ``relative`` below an already opened root and return its exact regular-file bytes."""

    parts = _repository_path(relative, label)
    opened: list[int] = []
    parent_fd = root_fd
    try:
        for part in parts[:-1]:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_DIRECTORY", 0)
            try:
                child = os.open(part, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise CampaignImportError(
                    f"cannot open {label} without following links: {exc}"
                ) from exc
            opened.append(child)
            parent_fd = child
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=parent_fd)
        except OSError as exc:
            raise CampaignImportError(
                f"cannot open {label} without following links: {exc}"
            ) from exc
        opened.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CampaignImportError(f"{label} is not a regular file")
        if before.st_size > _MAX_INPUT_BYTES:
            raise CampaignImportError(f"{label} exceeds the {_MAX_INPUT_BYTES}-byte input limit")
        chunks: list[bytes] = []
        remaining = _MAX_INPUT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            raise CampaignImportError(f"{label} exceeds the {_MAX_INPUT_BYTES}-byte input limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise CampaignImportError(f"{label} changed while it was being read")
        return b"".join(chunks)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CampaignImportError(f"{label} repeats JSON key {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=closed)
    except CampaignImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignImportError(f"{label} is not one valid UTF-8 JSON document: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignImportError(f"{label} must contain one JSON object")
    return value


def _git(root: Path, *arguments: str) -> bytes:
    """Run one Git command against ``root`` through the shared, hardened ``run_git``.

    Delegating here (rather than keeping this module's own hand-rolled environment) is what
    lets this function's ``status`` call see the same user global-ignore authority as every
    other cleanliness verdict in Bear Hug: one environment, one answer, about one repository.
    """

    try:
        result = run_git(root, *arguments)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignImportError(f"cannot inspect subject Git identity: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise CampaignImportError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout


def _one_line(root: Path, label: str, *arguments: str) -> str:
    raw = _git(root, *arguments)
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CampaignImportError(f"Git returned non-UTF-8 {label}") from exc
    if not value or "\n" in value or "\r" in value:
        raise CampaignImportError(f"Git returned invalid {label}")
    return value


def inspect_subject(subject: Path | str) -> SubjectIdentity:
    """Require one explicit, clean, named-branch physical Git worktree."""

    requested = Path(subject).expanduser()
    try:
        if requested.is_symlink():
            raise CampaignImportError(f"subject worktree may not be a symlink: {requested}")
        root = requested.resolve(strict=True)
    except OSError as exc:
        raise CampaignImportError(f"cannot resolve subject worktree {requested}: {exc}") from exc
    if not root.is_dir():
        raise CampaignImportError(f"subject is not a directory: {root}")
    top = Path(_one_line(root, "worktree root", "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise CampaignImportError(f"--subject must name the exact worktree root, observed {top}")
    common_raw = _one_line(
        root, "common directory", "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    common_requested = Path(common_raw)
    if common_requested.is_symlink():
        raise CampaignImportError("Git common directory must be a physical directory")
    common = common_requested.resolve(strict=True)
    if not common.is_dir():
        raise CampaignImportError("Git common directory must be a physical directory")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status:
        raise CampaignImportError(
            "campaign validation requires a clean subject worktree: "
            + describe_dirty_status(root, status)
        )
    head = _one_line(root, "HEAD", "rev-parse", "--verify", "HEAD")
    tree = _one_line(root, "HEAD tree", "rev-parse", "--verify", "HEAD^{tree}")
    branch = _one_line(root, "branch", "symbolic-ref", "--quiet", "--short", "HEAD")
    for label, oid in (("HEAD", head), ("HEAD tree", tree)):
        if len(oid) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise CampaignImportError(f"Git returned invalid {label} object id")
    return SubjectIdentity(
        root=root,
        repository_root_sha256=_sha256(os.fsencode(root)),
        common_dir=common,
        repository_common_dir_sha256=_sha256(os.fsencode(common)),
        head_oid=head,
        tree_oid=tree,
        base_tree_sha256=_sha256(tree.encode("ascii")),
        branch=branch,
    )


def _is_ancestor(root: Path, ancestor: str) -> bool:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            "HEAD",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.decode(errors="replace").strip()
        raise CampaignImportError(f"cannot verify campaign base ancestry: {detail}")
    return result.returncode == 0


def _is_ancestor_of(root: Path, ancestor: str, descendant: str) -> bool:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    result = subprocess.run(
        (
            "git",
            "--no-optional-locks",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.decode(errors="replace").strip()
        raise CampaignImportError(f"cannot verify sealed campaign ancestry: {detail}")
    return result.returncode == 0


def _verify_repository_binding(typeset_run: dict[str, Any], subject: SubjectIdentity) -> str:
    repository = typeset_run["repository"]
    expected = {
        "repository_root_sha256": subject.repository_root_sha256,
        "repository_common_dir_sha256": subject.repository_common_dir_sha256,
        "base_branch": subject.branch,
    }
    for field, observed in expected.items():
        if repository[field] != observed:
            raise CampaignImportError(
                f"campaign run repository.{field} mismatch: declared {repository[field]!r}, "
                f"observed {observed!r}"
            )
    base_oid = repository["base_oid"]
    try:
        canonical_base = _one_line(
            subject.root, "campaign base", "rev-parse", "--verify", f"{base_oid}^{{commit}}"
        )
        base_tree = _one_line(
            subject.root, "campaign base tree", "rev-parse", "--verify", f"{base_oid}^{{tree}}"
        )
    except CampaignImportError as exc:
        raise CampaignImportError(
            "campaign run base_oid is unavailable in the subject repository"
        ) from exc
    if canonical_base != base_oid or not _is_ancestor(subject.root, base_oid):
        raise CampaignImportError("campaign run base_oid is not an exact ancestor of subject HEAD")
    actual_tree_sha256 = _sha256(base_tree.encode("ascii"))
    if repository["base_tree_sha256"] != actual_tree_sha256:
        raise CampaignImportError(
            "campaign run repository.base_tree_sha256 does not match base_oid^{tree}"
        )
    return repository["repository_id"]


def _verify_cross_digests(
    *,
    corpus: dict[str, Any],
    template: dict[str, Any],
    run: dict[str, Any],
    index: dict[str, Any],
    corpus_result: CorpusValidationResult,
    template_result: ContractValidationResult,
    index_result: ContractValidationResult,
    template_path: str,
) -> None:
    expected_corpus = corpus_result.digest
    for label, declared in (
        ("template.plan_corpus_sha256", template["plan_corpus_sha256"]),
        ("run.plan_corpus_sha256", run["plan_corpus_sha256"]),
        ("index.plan_corpus_sha256", index["plan_corpus_sha256"]),
    ):
        if declared != expected_corpus:
            raise CampaignImportError(f"{label} does not bind the validated plan corpus")
    if run["template_sha256"] != template_result.digest:
        raise CampaignImportError("run.template_sha256 does not bind the validated template")
    if run["programme_index_sha256"] != index_result.digest:
        raise CampaignImportError("run.programme_index_sha256 does not bind the validated index")
    if (
        run["campaign_id"] != template["campaign_id"]
        or run["template_id"] != template["template_id"]
    ):
        raise CampaignImportError("campaign run identity does not match its template")
    if index["programme_id"] != template["programme_id"]:
        raise CampaignImportError("programme index identity does not match its template")
    matches = [
        entry for entry in index["campaigns"] if entry["campaign_id"] == template["campaign_id"]
    ]
    if len(matches) != 1:
        raise CampaignImportError("programme index must contain the selected campaign exactly once")
    indexed = matches[0]
    if (
        indexed["template_path"] != template_path
        or indexed["template_sha256"] != template_result.digest
    ):
        raise CampaignImportError("programme index template locator or digest is stale")

    sources = {source["source_id"]: source for source in corpus["sources"]}
    for authority in template["authority_refs"]:
        source = sources.get(authority["source_id"])
        if source is None:
            raise CampaignImportError(
                f"template authority {authority['source_id']!r} is absent from the plan corpus"
            )
        if source["content_sha256"] != authority["content_sha256"]:
            raise CampaignImportError(
                f"template authority {authority['source_id']!r} has a stale content digest"
            )
        undeclared = set(authority["scopes"]) - set(source["authority"]["scopes"])
        if undeclared:
            raise CampaignImportError(
                f"template authority {authority['source_id']!r} claims undeclared scopes "
                f"{sorted(undeclared)!r}"
            )


def revalidate_campaign_typeset(typeset: CampaignTypeset) -> CampaignTypeset:
    """Revalidate an imported typeset against its captured Git commit and cross-authority.

    Consumers must not treat independently valid replacement records as the imported authority.
    The four records are reread from the exact commit captured by ``SubjectIdentity``; this remains
    valid after providers create or advance separate candidate worktrees.
    """

    if not isinstance(typeset, CampaignTypeset):
        raise CampaignImportError("campaign typeset has the wrong runtime type")
    subject = typeset.subject
    try:
        root = subject.root.resolve(strict=True)
        common = subject.common_dir.resolve(strict=True)
    except OSError as exc:
        raise CampaignImportError(f"captured campaign repository is unavailable: {exc}") from exc
    if (
        root != subject.root
        or common != subject.common_dir
        or _sha256(os.fsencode(root)) != subject.repository_root_sha256
        or _sha256(os.fsencode(common)) != subject.repository_common_dir_sha256
    ):
        raise CampaignImportError("captured campaign repository identity changed")
    sealed_head = _one_line(root, "sealed campaign HEAD", "rev-parse", "--verify", subject.head_oid)
    sealed_tree = _one_line(
        root, "sealed campaign tree", "rev-parse", "--verify", f"{subject.head_oid}^{{tree}}"
    )
    if sealed_head != subject.head_oid or sealed_tree != subject.tree_oid:
        raise CampaignImportError("captured campaign commit identity changed")

    records: dict[str, dict[str, Any]] = {}
    for label, relative, expected in (
        ("plan corpus", typeset.corpus_path, typeset.corpus),
        ("campaign template", typeset.template_path, typeset.template),
        ("campaign run", typeset.run_path, typeset.run),
        ("programme index", typeset.index_path, typeset.index),
    ):
        _repository_path(relative, label)
        observed = _json_object(
            _git(root, "show", f"{subject.head_oid}:{relative}"),
            f"sealed {label}",
        )
        if observed != expected:
            raise CampaignImportError(f"{label} differs from the imported Git authority")
        records[label] = observed

    corpus = records["plan corpus"]
    template = records["campaign template"]
    run = records["campaign run"]
    index = records["programme index"]
    try:
        corpus_result = validate_manifest(
            corpus, repository_roots={run["repository"]["repository_id"]: root}
        )
        template_result = validate_campaign_template(template)
        index_result = validate_programme_campaign_index(index)
        run_result = validate_campaign_run(run, template=template, programme_index=index)
    except (CorpusManifestError, CampaignContractError, KeyError, TypeError) as exc:
        raise CampaignImportError(f"campaign typeset validation failed: {exc}") from exc
    if not corpus_result.complete:
        raise CampaignImportError(
            "an executable campaign requires every plan-corpus source present"
        )

    repository = run["repository"]
    expected_repository = {
        "repository_root_sha256": subject.repository_root_sha256,
        "repository_common_dir_sha256": subject.repository_common_dir_sha256,
        "base_branch": subject.branch,
    }
    for field, expected in expected_repository.items():
        if repository[field] != expected:
            raise CampaignImportError(f"campaign run repository.{field} changed after import")
    base_oid = repository["base_oid"]
    canonical_base = _one_line(
        root, "campaign base", "rev-parse", "--verify", f"{base_oid}^{{commit}}"
    )
    base_tree = _one_line(
        root, "campaign base tree", "rev-parse", "--verify", f"{base_oid}^{{tree}}"
    )
    if canonical_base != base_oid or not _is_ancestor_of(root, base_oid, subject.head_oid):
        raise CampaignImportError("campaign base is not an ancestor of the imported Git authority")
    if repository["base_tree_sha256"] != _sha256(base_tree.encode("ascii")):
        raise CampaignImportError("campaign base tree digest changed after import")
    _verify_cross_digests(
        corpus=corpus,
        template=template,
        run=run,
        index=index,
        corpus_result=corpus_result,
        template_result=template_result,
        index_result=index_result,
        template_path=typeset.template_path,
    )
    supplied_results = (
        (corpus_result.digest, typeset.corpus_validation.digest),
        (template_result.digest, typeset.template_validation.digest),
        (run_result.digest, typeset.run_validation.digest),
        (index_result.digest, typeset.index_validation.digest),
    )
    if any(recomputed != supplied for recomputed, supplied in supplied_results):
        raise CampaignImportError("campaign validation results changed after import")
    return CampaignTypeset(
        subject=subject,
        corpus_path=typeset.corpus_path,
        template_path=typeset.template_path,
        run_path=typeset.run_path,
        index_path=typeset.index_path,
        corpus=corpus,
        template=template,
        run=run,
        index=index,
        corpus_validation=corpus_result,
        template_validation=template_result,
        run_validation=run_result,
        index_validation=index_result,
    )


def import_campaign_typeset(
    *,
    subject: Path | str,
    plan_corpus_path: str,
    template_path: str,
    run_path: str,
    index_path: str,
) -> CampaignTypeset:
    """Read and validate one complete, exact campaign typeset without changing the subject."""

    identity = inspect_subject(subject)
    root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        root_fd = os.open(identity.root, root_flags)
    except OSError as exc:
        raise CampaignImportError(f"cannot open physical subject root: {exc}") from exc
    try:
        values = {
            "plan corpus": _json_object(
                _read_no_follow(root_fd, plan_corpus_path, "plan corpus"), "plan corpus"
            ),
            "campaign template": _json_object(
                _read_no_follow(root_fd, template_path, "campaign template"),
                "campaign template",
            ),
            "campaign run": _json_object(
                _read_no_follow(root_fd, run_path, "campaign run"), "campaign run"
            ),
            "programme index": _json_object(
                _read_no_follow(root_fd, index_path, "programme index"), "programme index"
            ),
        }
        corpus = values["plan corpus"]
        template = values["campaign template"]
        run = values["campaign run"]
        index = values["programme index"]
        try:
            corpus_result = validate_manifest(
                corpus, repository_roots={run["repository"]["repository_id"]: identity.root}
            )
            template_result = validate_campaign_template(template)
            index_result = validate_programme_campaign_index(index)
            run_result = validate_campaign_run(run, template=template, programme_index=index)
        except (CorpusManifestError, CampaignContractError, KeyError, TypeError) as exc:
            raise CampaignImportError(f"campaign typeset validation failed: {exc}") from exc
        expires_at = datetime.strptime(run["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise CampaignImportError("campaign run has expired")
        if not corpus_result.complete:
            raise CampaignImportError(
                "an executable campaign requires every plan-corpus source present"
            )
        repository_id = _verify_repository_binding(run, identity)
        _verify_cross_digests(
            corpus=corpus,
            template=template,
            run=run,
            index=index,
            corpus_result=corpus_result,
            template_result=template_result,
            index_result=index_result,
            template_path=template_path,
        )
        for source in corpus["sources"]:
            locator = source["locator"]
            if locator["kind"] != "repository":
                raise CampaignImportError(
                    f"source {source['source_id']!r} is not locally content-verifiable"
                )
            if locator["repository_id"] != repository_id:
                raise CampaignImportError(
                    f"source {source['source_id']!r} belongs to another repository"
                )
            revision = locator["revision"]
            try:
                canonical_revision = _one_line(
                    identity.root,
                    f"source {source['source_id']} revision",
                    "rev-parse",
                    "--verify",
                    f"{revision}^{{commit}}",
                )
            except CampaignImportError as exc:
                raise CampaignImportError(
                    f"source {source['source_id']!r} revision is unavailable"
                ) from exc
            if canonical_revision != revision or not _is_ancestor(identity.root, revision):
                raise CampaignImportError(
                    f"source {source['source_id']!r} revision is not an exact ancestor"
                )
            exact = _read_no_follow(root_fd, locator["path"], f"source {source['source_id']}")
            if _sha256(exact) != source["content_sha256"]:
                raise CampaignImportError(f"source {source['source_id']!r} content hash changed")
            historical = _git(identity.root, "cat-file", "blob", f"{revision}:{locator['path']}")
            if _sha256(historical) != source["content_sha256"]:
                raise CampaignImportError(
                    f"source {source['source_id']!r} content does not match its declared revision"
                )
        for unit in template["work_units"]:
            if unit["repository_id"] != repository_id:
                raise CampaignImportError(
                    f"work unit {unit['work_unit_id']!r} belongs to another repository"
                )
            artifact = unit["instruction_artifact"]
            if artifact["repository_id"] != repository_id:
                raise CampaignImportError(
                    f"work unit {unit['work_unit_id']!r} instruction belongs to another repository"
                )
            instruction = _read_no_follow(
                root_fd, artifact["path"], f"instruction {unit['work_unit_id']}"
            )
            if _sha256(instruction) != artifact["content_sha256"]:
                raise CampaignImportError(
                    f"instruction {unit['work_unit_id']!r} content hash mismatch"
                )
    finally:
        os.close(root_fd)
    closing_identity = inspect_subject(identity.root)
    if closing_identity != identity:
        raise CampaignImportError(
            "subject Git identity changed while the campaign typeset was read"
        )
    return CampaignTypeset(
        subject=identity,
        corpus_path=plan_corpus_path,
        template_path=template_path,
        run_path=run_path,
        index_path=index_path,
        corpus=corpus,
        template=template,
        run=run,
        index=index,
        corpus_validation=corpus_result,
        template_validation=template_result,
        run_validation=run_result,
        index_validation=index_result,
    )


__all__ = [
    "CampaignImportError",
    "CampaignTypeset",
    "SubjectIdentity",
    "import_campaign_typeset",
    "inspect_subject",
    "revalidate_campaign_typeset",
]
