"""A02 — is this architecture index still describing the tree?

The artifact is Barracuda's (A01 §5). Bear Hug cannot correct a record in it; the only useful
thing the lab can say is *how much of it is still true*, and it must be able to say that from the
artifact plus a head commit and a dirty flag — **without re-reading product source**. Nothing in
this module opens a file, and a test enforces that by making `open` raise.

Four verdicts, in strict precedence:

``unknown_schema``
    The artifact's own self-description is broken — unsupported version, a missing required key, a
    record with no provenance, an aggregate identity that does not recompute. Nothing in it can be
    trusted, so no weaker verdict may be reported.
``stale``
    Structurally sound, but it describes a different commit (or no commit at all). A rename
    invalidates the directory ownership of every package beneath it, so there is no partial credit.
``partial``
    Sound and commit-matching, but it does not describe the tree completely or consistently — a
    dirty tree, a parse failure, an `unknown` record, a duplicate id, an edge to a package no
    record declares, or no head supplied to check against.
``fresh``
    Sound, commit-matching, complete, self-consistent.

Precedence matters for a reason that is not cosmetic: the cheaper verdict would let a reader act on
a record that the more serious problem has already invalidated.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bearhug.arch import SCHEMA_VERSION

#: Top-level keys the artifact must carry. A missing one is `unknown_schema`, never a default.
REQUIRED_TOP_KEYS = (
    "schema_version",
    "generated_at",
    "extractor",
    "repository",
    "records",
    "parse_failures",
    "identity",
)

_REQUIRED_EXTRACTOR_KEYS = ("name", "version", "sha256")
_REQUIRED_REPOSITORY_KEYS = ("head", "dirty", "dirty_paths", "source_scope")
_REQUIRED_IDENTITY_KEYS = ("algorithm", "records_sha256")
_REQUIRED_RECORD_KEYS = ("kind", "id", "authority", "provenance")
_AUTHORITIES = frozenset({"product", "measured"})
_RECORD_KEYS: dict[str, frozenset[str]] = {
    "package": frozenset(
        {"kind", "id", "import_path", "name", "dir", "module", "authority", "provenance"}
    ),
    "edge": frozenset(
        {"kind", "id", "from", "to", "from_module", "to_module", "authority", "provenance"}
    ),
    "dependency": frozenset(
        {"kind", "id", "package", "module_path", "authority", "provenance"}
    ),
    "entry_point": frozenset(
        {"kind", "id", "package", "dir", "convention", "authority", "provenance"}
    ),
    "generated_file": frozenset(
        {"kind", "id", "path", "marker_line", "authority", "provenance"}
    ),
    "test_mapping": frozenset(
        {"kind", "id", "package", "test_files", "test_function_count", "authority", "provenance"}
    ),
    "decision_link": frozenset(
        {"kind", "id", "decision_id", "cites_path", "fragment", "authority", "provenance"}
    ),
    "harness_component": frozenset(
        {"kind", "id", "script", "registrations", "authority", "provenance"}
    ),
    "decision": frozenset(
        {"kind", "id", "decision_id", "cites_code", "exempt", "code", "authority", "provenance"}
    ),
    "unknown": frozenset(
        {"kind", "id", "subject", "reason", "authority", "provenance"}
    ),
}
_IDENTIFIER_RE = re.compile(r"^[\s\S]{1,512}$")
_RELATIVE_PATH_RE = re.compile(r"^[^/].{0,1023}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DECISION_ID_RE = re.compile(r"^[0-9]{1,6}$")
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class Freshness(StrEnum):
    """The four things Bear Hug is willing to say about a returned artifact."""

    FRESH = "fresh"
    STALE = "stale"
    PARTIAL = "partial"
    UNKNOWN_SCHEMA = "unknown_schema"


#: Worst first. Used only to make the precedence rule explicit rather than implied by control flow.
_PRECEDENCE = (Freshness.UNKNOWN_SCHEMA, Freshness.STALE, Freshness.PARTIAL, Freshness.FRESH)


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    """A verdict, every problem that produced it, and the dirty paths as disclosed."""

    verdict: Freshness
    problems: list[str] = field(default_factory=list)
    dirty_paths: tuple[str, ...] = ()
    recorded_head: str | None = None

    @property
    def usable(self) -> bool:
        """Only a fresh index may be read as an architectural claim (A05)."""
        return self.verdict is Freshness.FRESH

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "problems": list(self.problems),
            "dirty_paths": list(self.dirty_paths),
            "recorded_head": self.recorded_head,
        }


def canonical_record(record: Any) -> str:
    """One record as canonical JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def records_identity(records: Any) -> str:
    """The aggregate identity: sha256 over the canonical records, sorted.

    Sorting makes the hash independent of emission order, so two extractions that found the same
    facts prove identical even if they walked the tree differently. Duplicates are NOT collapsed —
    a duplicated record changes the hash, because it is a defect the artifact must not hide.
    """
    if not isinstance(records, list):
        return hashlib.sha256(b"").hexdigest()
    lines = sorted(canonical_record(record) for record in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _relpath(value: Any) -> bool:
    return isinstance(value, str) and _RELATIVE_PATH_RE.fullmatch(value) is not None


def _reason(value: Any) -> bool:
    return isinstance(value, str) and _REASON_RE.fullmatch(value) is not None


def _decision_id(value: Any) -> bool:
    return isinstance(value, str) and _DECISION_ID_RE.fullmatch(value) is not None


def _record_field_problems(record: dict[str, Any], where: Any, kind: str) -> list[str]:
    """Validate record values consumed by rules and context selection.

    Exact keys alone are insufficient: a list in ``from_module`` or a mapping in ``package``
    reaches the rules with a TypeError (or an accidental match). Keep this small hand-written
    check in the shared freshness boundary so every architecture consumer fails closed together.
    """
    problems: list[str] = []

    def require_identifier(field: str) -> None:
        if not _identifier(record.get(field)):
            problems.append(f"record_{field}_invalid: {where}")

    def require_relpath(field: str) -> None:
        if not _relpath(record.get(field)):
            problems.append(f"record_{field}_invalid: {where}")

    if kind == "package":
        require_identifier("import_path")
        if not isinstance(record.get("name"), str) or not _PACKAGE_NAME_RE.fullmatch(
            record.get("name", "")
        ):
            problems.append(f"record_name_invalid: {where}")
        require_relpath("dir")
        require_identifier("module")
    elif kind == "edge":
        for field in ("from", "to", "from_module", "to_module"):
            require_identifier(field)
    elif kind == "dependency":
        require_identifier("package")
        require_identifier("module_path")
    elif kind == "entry_point":
        require_identifier("package")
        require_relpath("dir")
        convention = record.get("convention")
        if not isinstance(convention, str) or convention not in {"cmd", "other"}:
            problems.append(f"record_convention_invalid: {where}")
    elif kind == "generated_file":
        require_relpath("path")
        marker_line = record.get("marker_line")
        if type(marker_line) is not int or marker_line < 1:
            problems.append(f"record_marker_line_invalid: {where}")
    elif kind == "test_mapping":
        require_identifier("package")
        test_files = record.get("test_files")
        if not isinstance(test_files, list) or any(
            not _relpath(path) for path in (test_files or [])
        ):
            problems.append(f"record_test_files_invalid: {where}")
        elif len(test_files) != len(set(test_files)):
            problems.append(f"record_test_files_not_unique: {where}")
        count = record.get("test_function_count")
        if type(count) is not int or count < 0:
            problems.append(f"record_test_function_count_invalid: {where}")
    elif kind == "decision_link":
        if not _decision_id(record.get("decision_id")):
            problems.append(f"record_decision_id_invalid: {where}")
        cites_path = record.get("cites_path")
        if not isinstance(cites_path, str) or not 1 <= len(cites_path) <= 1024:
            problems.append(f"record_cites_path_invalid: {where}")
        fragment = record.get("fragment")
        if fragment is not None and (
            not isinstance(fragment, str) or not 1 <= len(fragment) <= 256
        ):
            problems.append(f"record_fragment_invalid: {where}")
    elif kind == "harness_component":
        require_relpath("script")
        registrations = record.get("registrations")
        if not isinstance(registrations, list):
            problems.append(f"record_registrations_invalid: {where}")
        else:
            for registration in registrations:
                if not isinstance(registration, dict) or set(registration) != {"event", "matcher"}:
                    problems.append(f"record_registration_invalid: {where}")
                    continue
                event = registration.get("event")
                matcher = registration.get("matcher")
                if not isinstance(event, str) or not 1 <= len(event) <= 64:
                    problems.append(f"record_registration_event_invalid: {where}")
                if matcher is not None and (
                    not isinstance(matcher, str) or len(matcher) > 256
                ):
                    problems.append(f"record_registration_matcher_invalid: {where}")
    elif kind == "decision":
        if not _decision_id(record.get("decision_id")):
            problems.append(f"record_decision_id_invalid: {where}")
        for field in ("cites_code", "exempt"):
            if type(record.get(field)) is not bool:
                problems.append(f"decision_{field}_not_boolean: {where}")
        code = record.get("code")
        if not isinstance(code, str) or code not in {"none", "pending", "absent"}:
            problems.append(f"decision_code_invalid: {where}")
    elif kind == "unknown":
        subject = record.get("subject")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
            problems.append(f"unknown_subject_invalid: {where}")
        if not _reason(record.get("reason")):
            problems.append(f"unknown_reason_invalid: {where}")
    return problems


def _schema_problems(artifact: Any) -> list[str]:
    """Everything that makes the artifact's self-description unusable."""
    problems: list[str] = []
    if not isinstance(artifact, dict):
        return [f"not_a_mapping: {type(artifact).__name__}"]

    missing = [key for key in REQUIRED_TOP_KEYS if key not in artifact]
    if missing:
        problems.append(f"missing_top_level_keys: {', '.join(missing)}")
    extra = sorted(set(artifact) - set(REQUIRED_TOP_KEYS))
    if extra:
        problems.append(f"unknown_top_level_keys: {', '.join(extra)}")

    version = artifact.get("schema_version")
    if version != SCHEMA_VERSION:
        problems.append(f"unsupported_schema_version: {version!r}")
    generated_at = artifact.get("generated_at")
    if not isinstance(generated_at, str) or not _TIMESTAMP_RE.fullmatch(generated_at):
        problems.append(f"generated_at_invalid: {generated_at!r}")

    extractor = artifact.get("extractor")
    if not isinstance(extractor, dict):
        problems.append("extractor_not_a_mapping")
    else:
        absent = [key for key in _REQUIRED_EXTRACTOR_KEYS if key not in extractor]
        if absent:
            problems.append(f"extractor_missing: {', '.join(absent)}")
        extra = sorted(set(extractor) - set(_REQUIRED_EXTRACTOR_KEYS))
        if extra:
            problems.append(f"extractor_unknown_keys: {', '.join(extra)}")
        if not isinstance(extractor.get("name"), str) or not extractor.get("name"):
            problems.append("extractor_name_invalid")
        if not isinstance(extractor.get("version"), str) or not _VERSION_RE.fullmatch(
            extractor.get("version", "")
        ):
            problems.append("extractor_version_invalid")
        if not isinstance(extractor.get("sha256"), str) or not _SHA256_RE.fullmatch(
            extractor.get("sha256", "")
        ):
            problems.append("extractor_sha256_invalid")

    repository = artifact.get("repository")
    if not isinstance(repository, dict):
        problems.append("repository_not_a_mapping")
    else:
        absent = [key for key in _REQUIRED_REPOSITORY_KEYS if key not in repository]
        if absent:
            problems.append(f"repository_missing: {', '.join(absent)}")
        extra = sorted(set(repository) - set(_REQUIRED_REPOSITORY_KEYS))
        if extra:
            problems.append(f"repository_unknown_keys: {', '.join(extra)}")
        head = repository.get("head")
        if head is not None and (not isinstance(head, str) or not _COMMIT_RE.fullmatch(head)):
            problems.append("repository_head_invalid")
        if type(repository.get("dirty")) is not bool:
            problems.append("repository_dirty_invalid")
        dirty_paths = repository.get("dirty_paths")
        if not isinstance(dirty_paths, list) or any(
            not isinstance(path, str) or not path for path in (dirty_paths or [])
        ):
            problems.append("repository_dirty_paths_invalid")
        source_scope = repository.get("source_scope")
        if not isinstance(source_scope, dict):
            problems.append("source_scope_not_a_mapping")
        else:
            required_scope = {"roots", "modules", "excluded", "max_file_bytes"}
            missing_scope = sorted(required_scope - set(source_scope))
            extra_scope = sorted(set(source_scope) - required_scope)
            if missing_scope:
                problems.append(f"source_scope_missing: {', '.join(missing_scope)}")
            if extra_scope:
                problems.append(f"source_scope_unknown_keys: {', '.join(extra_scope)}")
            roots = source_scope.get("roots")
            if not isinstance(roots, list) or any(
                not isinstance(path, str) or not _RELATIVE_PATH_RE.fullmatch(path)
                for path in (roots or [])
            ):
                problems.append("source_scope_roots_invalid")
            modules = source_scope.get("modules")
            if not isinstance(modules, list) or any(
                not isinstance(module, dict)
                or set(module) != {"path", "dir"}
                or not isinstance(module.get("path"), str)
                or not _IDENTIFIER_RE.fullmatch(module.get("path", ""))
                or not isinstance(module.get("dir"), str)
                or not _RELATIVE_PATH_RE.fullmatch(module.get("dir", ""))
                for module in (modules or [])
            ):
                problems.append("source_scope_modules_invalid")
            excluded = source_scope.get("excluded")
            if not isinstance(excluded, list) or any(
                not isinstance(path, str) or not path for path in (excluded or [])
            ):
                problems.append("source_scope_excluded_invalid")
            max_file_bytes = source_scope.get("max_file_bytes")
            if type(max_file_bytes) is not int or max_file_bytes < 1:
                problems.append("source_scope_max_file_bytes_invalid")

    identity = artifact.get("identity")
    if not isinstance(identity, dict):
        problems.append("identity_not_a_mapping")
    else:
        absent = [key for key in _REQUIRED_IDENTITY_KEYS if key not in identity]
        if absent:
            problems.append(f"identity_missing: {', '.join(absent)}")
        extra = sorted(set(identity) - set(_REQUIRED_IDENTITY_KEYS))
        if extra:
            problems.append(f"identity_unknown_keys: {', '.join(extra)}")
        if identity.get("algorithm") != "sha256":
            problems.append("identity_algorithm_invalid")
        if not isinstance(identity.get("records_sha256"), str) or not _SHA256_RE.fullmatch(
            identity.get("records_sha256", "")
        ):
            problems.append("identity_records_sha256_invalid")

    records = artifact.get("records")
    if not isinstance(records, list):
        problems.append("records_not_a_list")
    else:
        problems.extend(_record_shape_problems(records))

    parse_failures = artifact.get("parse_failures")
    if not isinstance(parse_failures, list):
        problems.append("parse_failures_not_a_list")
    else:
        for index, failure in enumerate(parse_failures):
            if not isinstance(failure, dict):
                problems.append(f"parse_failure_not_a_mapping: index {index}")
                continue
            if set(failure) - {"path", "reason", "line"}:
                problems.append(f"parse_failure_unknown_keys: index {index}")
            if not isinstance(failure.get("path"), str) or not _RELATIVE_PATH_RE.fullmatch(
                failure.get("path", "")
            ):
                problems.append(f"parse_failure_path_invalid: index {index}")
            if not isinstance(failure.get("reason"), str) or not _REASON_RE.fullmatch(
                failure.get("reason", "")
            ):
                problems.append(f"parse_failure_reason_invalid: index {index}")
            line = failure.get("line")
            if line is not None and (type(line) is not int or line < 1):
                problems.append(f"parse_failure_line_invalid: index {index}")

    if (
        isinstance(identity, dict)
        and isinstance(records, list)
        and isinstance(identity.get("records_sha256"), str)
    ):
        recorded = identity.get("records_sha256")
        try:
            recomputed = records_identity(records)
        except (TypeError, ValueError):
            problems.append("records_identity_invalid")
        else:
            if recorded != recomputed:
                problems.append(
                    f"identity_mismatch: recorded {str(recorded)[:12]}, "
                    f"recomputed {recomputed[:12]}"
                )
    return problems


def _record_shape_problems(records: list[Any]) -> list[str]:
    """Per-record structure. A record with no citation is unusable, not merely weaker."""
    problems: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            problems.append(f"record_not_a_mapping: index {index}")
            continue
        where = record.get("id", f"index {index}")
        kind = record.get("kind")
        if not isinstance(kind, str) or kind not in _RECORD_KEYS:
            problems.append(f"record_kind_invalid: {where} -> {kind!r}")
            continue
        if set(record) != _RECORD_KEYS[kind]:
            problems.append(
                f"record_fields_invalid: {where} ({kind}) has {sorted(record)}, "
                f"expected {sorted(_RECORD_KEYS[kind])}"
            )
        for key in _REQUIRED_RECORD_KEYS:
            if key not in record:
                problems.append(f"record_missing_{key}: {where}")
        if not isinstance(record.get("id"), str) or not _IDENTIFIER_RE.fullmatch(
            record.get("id", "")
        ):
            problems.append(f"record_id_invalid: {where}")
        provenance = record.get("provenance")
        if "provenance" in record:
            if not isinstance(provenance, dict):
                problems.append(f"record_provenance_not_a_mapping: {where}")
            elif set(provenance) != {"path", "line"}:
                problems.append(f"record_provenance_incomplete: {where}")
            elif (
                not isinstance(provenance.get("path"), str)
                or not _RELATIVE_PATH_RE.fullmatch(provenance.get("path", ""))
                or (
                    provenance.get("line") is not None
                    and (
                        type(provenance.get("line")) is not int
                        or provenance.get("line") < 1
                    )
                )
            ):
                problems.append(f"record_provenance_invalid: {where}")
        authority = record.get("authority")
        if "authority" in record and (
            not isinstance(authority, str) or authority not in _AUTHORITIES
        ):
            problems.append(f"record_authority_invalid: {where} -> {authority!r}")
        problems.extend(_record_field_problems(record, where, kind))
    return problems


def schema_problems(artifact: Any) -> list[str]:
    """Return closed-artifact validation errors for every architecture consumer.

    Rules, context selection, and freshness must agree about whether an index is usable. Keeping
    this boundary here prevents the rules CLI from evaluating a malformed envelope independently
    of the A02 freshness reader.
    """
    return _schema_problems(artifact)


def _completeness_problems(artifact: dict, *, dirty: bool) -> list[str]:
    """Everything that makes a sound, commit-matching index less than complete."""
    problems: list[str] = []
    repository = artifact.get("repository", {})
    records = artifact.get("records", [])

    if dirty:
        problems.append("dirty_tree: the caller reported a dirty working tree")
    if repository.get("dirty"):
        paths = repository.get("dirty_paths") or []
        named = ", ".join(str(path) for path in paths[:5]) or "none named"
        problems.append(f"dirty_tree_disclosed: {len(paths)} path(s) -- {named}")

    failures = artifact.get("parse_failures") or []
    for failure in failures:
        path = failure.get("path") if isinstance(failure, dict) else failure
        reason = failure.get("reason") if isinstance(failure, dict) else "unknown"
        problems.append(f"parse_failure: {path} ({reason})")

    seen: set[str] = set()
    declared: set[str] = set()
    for record in records:
        record_id = record.get("id")
        if record_id in seen:
            problems.append(f"duplicate_record_id: {record_id}")
        seen.add(record_id)
        if record.get("kind") == "package":
            declared.add(record.get("import_path"))

    for record in records:
        if record.get("kind") == "unknown":
            problems.append(
                f"unknown_record: {record.get('id')} ({record.get('reason')})"
            )
        elif record.get("kind") == "edge":
            for end in ("from", "to"):
                if record.get(end) not in declared:
                    problems.append(
                        f"unresolved_edge: {record.get('id')} -- "
                        f"{end} {record.get(end)!r} is not a declared package"
                    )
    return problems


def verdict_for(artifact: Any, *, head: str | None, dirty: bool = False) -> FreshnessResult:
    """Validate ``artifact`` against a head commit and a dirty flag.

    ``head`` is the commit the reader believes the tree is at. Passing ``None`` does not mean
    "assume it matches" — it means the commit was never verified, which is a `partial` result.
    """
    problems = schema_problems(artifact)
    if problems:
        recorded = None
        if isinstance(artifact, dict) and isinstance(artifact.get("repository"), dict):
            recorded = artifact["repository"].get("head")
        return FreshnessResult(Freshness.UNKNOWN_SCHEMA, problems, (), recorded)

    repository = artifact["repository"]
    recorded_head = repository.get("head")
    dirty_paths = tuple(str(path) for path in (repository.get("dirty_paths") or []))

    if recorded_head is None:
        return FreshnessResult(
            Freshness.STALE,
            ["no_recorded_head: the artifact describes no commit, so it matches none"],
            dirty_paths,
            None,
        )
    if head is not None and recorded_head != head:
        return FreshnessResult(
            Freshness.STALE,
            [f"head_mismatch: artifact records {recorded_head[:12]}, tree is at {head[:12]}"],
            dirty_paths,
            recorded_head,
        )

    problems = _completeness_problems(artifact, dirty=dirty)
    if head is None:
        problems.append(
            "head_not_verified: no head was supplied, so the recorded commit was never checked"
        )
    if problems:
        return FreshnessResult(Freshness.PARTIAL, problems, dirty_paths, recorded_head)
    return FreshnessResult(Freshness.FRESH, [], dirty_paths, recorded_head)


def worst(*verdicts: Freshness) -> Freshness:
    """The most serious of several verdicts, by the documented precedence."""
    for candidate in _PRECEDENCE:
        if candidate in verdicts:
            return candidate
    return Freshness.FRESH


__all__ = [
    "REQUIRED_TOP_KEYS",
    "Freshness",
    "FreshnessResult",
    "canonical_record",
    "records_identity",
    "schema_problems",
    "verdict_for",
    "worst",
]
