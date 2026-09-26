"""Validate a caller-authored plan-corpus declaration without interpreting its prose.

The owning project supplies lifecycle, authority role, scopes, and relations. Bear Hug validates
that declaration's closed shape, identities, graph integrity, canonical digest, and—only when a
caller explicitly supplies a repository root—the declared file availability and content hash.
Source files are read as opaque bytes. Nothing in this module writes to a repository or executes
content found in one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[\s\S]+$")
_URI_WHITESPACE = frozenset(
    "\u0020\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000"
)

CANONICAL_ALGORITHM = "bearhug-plan-corpus-canonical-json-sha256/1"

_SOURCE_FIELDS = {
    "schema_version",
    "record_kind",
    "source_id",
    "locator",
    "content_sha256",
    "lifecycle",
    "availability",
    "authority",
}
_LIFECYCLES = {
    "active_authority",
    "executable_authority",
    "historical_evidence",
    "generated_evidence",
    "external_reference",
}
_AUTHORITY_ROLES = {"authoritative", "derived", "evidence", "reference"}
_LIFECYCLE_ROLE = {
    "active_authority": "authoritative",
    "executable_authority": "authoritative",
    "historical_evidence": "evidence",
    "generated_evidence": "derived",
    "external_reference": "reference",
}
_RELATION_KINDS = {"supersedes", "derives_from", "conflicts_with", "corroborates"}
_DIRECTED_RELATION_KINDS = {"supersedes", "derives_from"}


class CorpusManifestError(ValueError):
    """The manifest is malformed, internally inconsistent, stale, or content-mismatched."""

    def __init__(self, issues: Sequence[str] | str):
        self.issues = (issues,) if isinstance(issues, str) else tuple(issues)
        detail = "\n".join(f"- {issue}" for issue in self.issues)
        super().__init__(f"invalid plan corpus manifest:\n{detail}")


class _DuplicateJSONKeyError(ValueError):
    """A JSON object repeated a key before it could be collapsed into a dict."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CorpusDiagnostic:
    """A valid declaration that is incomplete or blocks execution because a source is missing."""

    code: str
    severity: Literal["blocking", "incomplete"]
    source_id: str
    message: str


@dataclass(frozen=True, slots=True)
class CorpusValidationResult:
    """Reproducible identity and readiness of one structurally valid declaration."""

    corpus_id: str
    digest: str
    diagnostics: tuple[CorpusDiagnostic, ...]

    @property
    def ready(self) -> bool:
        """Whether no caller-declared missing source blocks executable use."""
        return all(item.severity != "blocking" for item in self.diagnostics)

    @property
    def complete(self) -> bool:
        """Whether every declared source is present."""
        return not self.diagnostics


def _object(
    value: Any,
    path: str,
    *,
    required: set[str],
    allowed: set[str],
    issues: list[str],
) -> bool:
    if not isinstance(value, Mapping):
        issues.append(f"{path}: expected object")
        return False
    keys: set[str] = set()
    for field in value:
        if not isinstance(field, str):
            issues.append(f"{path}: object key must be a string, got {field!r}")
        else:
            keys.add(field)
    for field in sorted(required - keys):
        issues.append(f"{path}: missing required field {field!r}")
    for field in sorted(keys - allowed):
        issues.append(f"{path}: unknown field {field!r}")
    return True


def _forbidden_code_point(value: str) -> int | None:
    for character in value:
        code_point = ord(character)
        if (
            code_point <= 0x001F
            or 0x007F <= code_point <= 0x009F
            or 0xD800 <= code_point <= 0xDFFF
        ):
            return code_point
    return None


def _validate_string_policy(value: Any, path: str, issues: list[str]) -> None:
    """Apply one Unicode policy to every key and string value before field semantics."""
    pending: list[tuple[str, Any]] = [(path, value)]
    while pending:
        current_path, current = pending.pop()
        if isinstance(current, str):
            forbidden = _forbidden_code_point(current)
            if forbidden is not None:
                issues.append(
                    f"{current_path}: forbidden Unicode control or surrogate code point "
                    f"U+{forbidden:04X}"
                )
        elif isinstance(current, Mapping):
            for key, child in current.items():
                key_path = f"{current_path}/<key>"
                if isinstance(key, str):
                    pending.append((key_path, key))
                    pending.append((f"{current_path}/{key}", child))
                else:
                    pending.append((f"{current_path}/{key!r}", child))
        elif isinstance(current, list):
            for index, child in enumerate(current):
                pending.append((f"{current_path}/{index}", child))


def _string(
    value: Any,
    path: str,
    issues: list[str],
    *,
    pattern: re.Pattern[str] | None = None,
    allowed: set[str] | None = None,
) -> bool:
    if not isinstance(value, str):
        issues.append(f"{path}: expected string")
        return False
    if pattern is not None and pattern.fullmatch(value) is None:
        issues.append(f"{path}: invalid value {value!r}")
        return False
    if allowed is not None and value not in allowed:
        issues.append(f"{path}: unsupported value {value!r}")
        return False
    return True


def _scopes(value: Any, path: str, issues: list[str]) -> None:
    if not isinstance(value, list) or not value:
        issues.append(f"{path}: expected non-empty array")
        return
    seen: set[str] = set()
    for index, scope in enumerate(value):
        if _string(scope, f"{path}/{index}", issues, pattern=_SCOPE):
            if scope in seen:
                issues.append(f"{path}/{index}: duplicate scope {scope!r}")
            seen.add(scope)


def _repository_path(value: Any, path: str, issues: list[str]) -> None:
    if not isinstance(value, str):
        issues.append(f"{path}: expected string")
        return
    raw_parts = value.split("/")
    candidate = PurePosixPath(value)
    unsafe = (
        not value
        or len(value) > 4096
        or candidate.is_absolute()
        or value.startswith("~")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in raw_parts)
    )
    if unsafe:
        issues.append(f"{path}: expected canonical repository-relative file path")


def _validate_locator(value: Any, path: str, issues: list[str]) -> None:
    if not isinstance(value, Mapping):
        issues.append(f"{path}: expected object")
        return
    kind = value.get("kind")
    if kind == "repository":
        fields = {"kind", "repository_id", "revision", "path"}
        if not _object(value, path, required=fields, allowed=fields, issues=issues):
            return
        _string(value.get("repository_id"), f"{path}/repository_id", issues, pattern=_IDENTIFIER)
        _string(value.get("revision"), f"{path}/revision", issues, pattern=_REVISION)
        _repository_path(value.get("path"), f"{path}/path", issues)
        return
    if kind == "external":
        required = {"kind", "uri"}
        allowed = required | {"revision"}
        if not _object(value, path, required=required, allowed=allowed, issues=issues):
            return
        uri = value.get("uri")
        if _string(uri, f"{path}/uri", issues, pattern=_URI):
            if uri.lower().startswith("file:") or len(uri) > 4096:
                issues.append(f"{path}/uri: file or overlong external URI is forbidden")
            if any(character in _URI_WHITESPACE for character in uri):
                issues.append(f"{path}/uri: external URI contains forbidden whitespace")
        if "revision" in value:
            revision = value["revision"]
            if not isinstance(revision, str) or not revision or len(revision) > 256:
                issues.append(f"{path}/revision: expected non-empty string up to 256 characters")
        return
    issues.append(f"{path}/kind: unsupported value {kind!r}")


def _validate_source(value: Any, index: int, issues: list[str]) -> None:
    path = f"sources/{index}"
    if not _object(
        value,
        path,
        required=_SOURCE_FIELDS,
        allowed=_SOURCE_FIELDS,
        issues=issues,
    ):
        return
    if value.get("schema_version") != 1 or type(value.get("schema_version")) is not int:
        issues.append(f"{path}/schema_version: expected integer 1")
    if value.get("record_kind") != "source_record":
        issues.append(f"{path}/record_kind: expected 'source_record'")
    _string(value.get("source_id"), f"{path}/source_id", issues, pattern=_IDENTIFIER)
    _string(value.get("content_sha256"), f"{path}/content_sha256", issues, pattern=_SHA256)
    _string(value.get("lifecycle"), f"{path}/lifecycle", issues, allowed=_LIFECYCLES)
    _string(
        value.get("availability"),
        f"{path}/availability",
        issues,
        allowed={"present", "missing"},
    )
    _validate_locator(value.get("locator"), f"{path}/locator", issues)

    authority = value.get("authority")
    fields = {"role", "scopes"}
    if _object(authority, f"{path}/authority", required=fields, allowed=fields, issues=issues):
        _string(
            authority.get("role"),
            f"{path}/authority/role",
            issues,
            allowed=_AUTHORITY_ROLES,
        )
        _scopes(authority.get("scopes"), f"{path}/authority/scopes", issues)


def _validate_relation(value: Any, index: int, issues: list[str]) -> None:
    path = f"relations/{index}"
    fields = {"kind", "source_id", "target_id", "scopes"}
    if not _object(value, path, required=fields, allowed=fields, issues=issues):
        return
    _string(value.get("kind"), f"{path}/kind", issues, allowed=_RELATION_KINDS)
    _string(value.get("source_id"), f"{path}/source_id", issues, pattern=_IDENTIFIER)
    _string(value.get("target_id"), f"{path}/target_id", issues, pattern=_IDENTIFIER)
    _scopes(value.get("scopes"), f"{path}/scopes", issues)


def _validate_shape(manifest: Any) -> None:
    issues: list[str] = []
    _validate_string_policy(manifest, "<root>", issues)
    fields = {"schema_version", "record_kind", "corpus_id", "sources", "relations"}
    if not _object(manifest, "<root>", required=fields, allowed=fields, issues=issues):
        raise CorpusManifestError(issues)
    if manifest.get("schema_version") != 1 or type(manifest.get("schema_version")) is not int:
        issues.append("schema_version: expected integer 1")
    if manifest.get("record_kind") != "plan_corpus":
        issues.append("record_kind: expected 'plan_corpus'")
    _string(manifest.get("corpus_id"), "corpus_id", issues, pattern=_IDENTIFIER)

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        issues.append("sources: expected non-empty array")
    else:
        for index, source in enumerate(sources):
            _validate_source(source, index, issues)

    relations = manifest.get("relations")
    if not isinstance(relations, list):
        issues.append("relations: expected array")
    else:
        for index, relation in enumerate(relations):
            _validate_relation(relation, index, issues)

    if issues:
        raise CorpusManifestError(issues)


def _locator_identity(source: Mapping[str, Any]) -> tuple[str, ...]:
    locator = source["locator"]
    if locator["kind"] == "repository":
        return (
            "repository",
            locator["repository_id"],
            locator["revision"],
            locator["path"],
        )
    return (
        "external",
        locator["uri"],
        locator.get("revision", ""),
    )


def _cycle(graph: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    state: dict[str, Literal["active", "complete"]] = {}
    nodes = set(graph)
    for targets in graph.values():
        nodes.update(targets)
    for start in sorted(nodes):
        if start in state:
            continue
        path = [start]
        path_index = {start: 0}
        state[start] = "active"
        stack: list[tuple[str, Iterator[str]]] = [
            (start, iter(sorted(graph.get(start, set()))))
        ]
        while stack:
            node, targets = stack[-1]
            try:
                target = next(targets)
            except StopIteration:
                stack.pop()
                path.pop()
                path_index.pop(node)
                state[node] = "complete"
                continue
            target_state = state.get(target)
            if target_state == "active":
                cycle_at = path_index[target]
                return tuple([*path[cycle_at:], target])
            if target_state == "complete":
                continue
            state[target] = "active"
            path_index[target] = len(path)
            path.append(target)
            stack.append((target, iter(sorted(graph.get(target, set())))))
    return None


def _validate_semantics(manifest: Mapping[str, Any]) -> None:
    issues: list[str] = []
    source_ids: set[str] = set()
    identities: dict[tuple[str, ...], str] = {}
    sources_by_id: dict[str, Mapping[str, Any]] = {}
    for source in manifest["sources"]:
        source_id = source["source_id"]
        if source_id in source_ids:
            issues.append(f"duplicate source_id {source_id!r}")
        source_ids.add(source_id)
        sources_by_id.setdefault(source_id, source)
        expected_role = _LIFECYCLE_ROLE[source["lifecycle"]]
        actual_role = source["authority"]["role"]
        if actual_role != expected_role:
            issues.append(
                f"{source_id!r}: lifecycle/authority role mismatch: "
                f"{source['lifecycle']!r} requires {expected_role!r}, got {actual_role!r}"
            )
        identity = _locator_identity(source)
        previous = identities.get(identity)
        if previous is not None:
            issues.append(
                f"duplicate source locator identity shared by {previous!r} and {source_id!r}"
            )
        else:
            identities[identity] = source_id

    relation_ids: set[tuple[str, str, str, tuple[str, ...]]] = set()
    graphs_by_scope: dict[str, dict[str, set[str]]] = {}
    for relation in manifest["relations"]:
        source_id = relation["source_id"]
        target_id = relation["target_id"]
        if source_id not in source_ids:
            issues.append(f"relation has unknown source_id {source_id!r}")
        if target_id not in source_ids:
            issues.append(f"relation has unknown target_id {target_id!r}")
        if source_id == target_id:
            issues.append(f"relation {relation['kind']!r} may not refer to itself: {source_id!r}")
        source = sources_by_id.get(source_id)
        target = sources_by_id.get(target_id)
        relation_scopes = set(relation["scopes"])
        if source is not None:
            missing = relation_scopes - set(source["authority"]["scopes"])
            if missing:
                issues.append(
                    f"relation {relation['kind']!r} source does not declare scopes "
                    f"{sorted(missing)!r}: {source_id!r}"
                )
            if relation["kind"] == "supersedes" and source["authority"]["role"] != "authoritative":
                issues.append(
                    "supersedes source must be authoritative for every claimed scope: "
                    f"{source_id!r}"
                )
        if target is not None:
            missing = relation_scopes - set(target["authority"]["scopes"])
            if missing:
                issues.append(
                    f"relation {relation['kind']!r} target does not declare scopes "
                    f"{sorted(missing)!r}: {target_id!r}"
                )
        identity = (relation["kind"], source_id, target_id, tuple(sorted(relation["scopes"])))
        if identity in relation_ids:
            issues.append(f"duplicate relation {identity!r}")
        relation_ids.add(identity)
        if relation["kind"] in _DIRECTED_RELATION_KINDS and target_id in source_ids:
            for scope in relation["scopes"]:
                graph = graphs_by_scope.setdefault(scope, {})
                graph.setdefault(source_id, set()).add(target_id)

    for scope, graph in sorted(graphs_by_scope.items()):
        found_cycle = _cycle(graph)
        if found_cycle is not None:
            issues.append(
                f"directed authority relation cycle in scope {scope!r}: "
                f"{' -> '.join(found_cycle)}"
            )
    if issues:
        raise CorpusManifestError(issues)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_repository_content(
    manifest: Mapping[str, Any], repository_roots: Mapping[str, Path | str]
) -> None:
    issues: list[str] = []
    roots: dict[str, Path] = {}
    for repository_id, root in repository_roots.items():
        if not isinstance(repository_id, str) or _IDENTIFIER.fullmatch(repository_id) is None:
            issues.append(f"repository_roots: invalid repository ID {repository_id!r}")
            continue
        try:
            supplied = Path(root)
        except TypeError:
            issues.append(f"repository_roots/{repository_id}: expected filesystem path")
            continue
        if supplied.is_symlink():
            issues.append(
                f"repository_roots/{repository_id}: symlink root is forbidden: {supplied}"
            )
            continue
        resolved = supplied.resolve()
        if not resolved.is_dir():
            issues.append(f"repository_roots/{repository_id}: not a directory: {resolved}")
            continue
        roots[repository_id] = resolved

    for source in manifest["sources"]:
        locator = source["locator"]
        if locator["kind"] != "repository" or locator["repository_id"] not in roots:
            continue
        root = roots[locator["repository_id"]]
        source_id = source["source_id"]
        relative_parts = PurePosixPath(locator["path"]).parts
        unresolved = root
        symlink: Path | None = None
        for part in relative_parts:
            unresolved = unresolved / part
            if unresolved.is_symlink():
                symlink = unresolved
                break
        if symlink is not None:
            issues.append(f"{source_id}: symlink component is forbidden: {symlink}")
            continue
        target = unresolved.resolve()
        if not target.is_relative_to(root):
            issues.append(f"{source_id}: path escapes repository root: {locator['path']!r}")
            continue
        exists = target.is_file()
        if source["availability"] == "present":
            if not exists:
                issues.append(f"{source_id}: declared present but is absent: {locator['path']}")
                continue
            actual = _file_sha256(target)
            if actual != source["content_sha256"]:
                issues.append(
                    f"{source_id}: content_sha256 mismatch: declared "
                    f"{source['content_sha256']}, actual {actual}"
                )
        elif target.exists():
            issues.append(f"{source_id}: declared missing but exists: {locator['path']}")
    if issues:
        raise CorpusManifestError(issues)


def _canonical_bytes_unchecked(manifest: Mapping[str, Any]) -> bytes:
    """Implement bearhug-plan-corpus-canonical-json-sha256/1.

    Valid Unicode is preserved byte-for-byte without NFC/NFD normalization. Object keys and the
    declared set-like arrays are sorted with Python's Unicode code-point ordering. JSON is compact,
    UTF-8, ``ensure_ascii=False``, and terminated by exactly one LF before SHA-256 is computed.
    """
    normalized = json.loads(json.dumps(manifest, ensure_ascii=False))
    for source in normalized["sources"]:
        source["authority"]["scopes"] = sorted(source["authority"]["scopes"])
    normalized["sources"].sort(key=lambda source: source["source_id"])
    for relation in normalized["relations"]:
        relation["scopes"] = sorted(relation["scopes"])
    normalized["relations"].sort(
        key=lambda relation: (
            relation["kind"],
            relation["source_id"],
            relation["target_id"],
            tuple(relation["scopes"]),
        )
    )
    text = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return text.encode("utf-8") + b"\n"


def _diagnostics(manifest: Mapping[str, Any]) -> tuple[CorpusDiagnostic, ...]:
    diagnostics = []
    for source in sorted(manifest["sources"], key=lambda item: item["source_id"]):
        if source["availability"] != "missing":
            continue
        lifecycle = source["lifecycle"]
        blocking = lifecycle in {"active_authority", "executable_authority"}
        diagnostics.append(
            CorpusDiagnostic(
                code=f"missing_{lifecycle}",
                severity="blocking" if blocking else "incomplete",
                source_id=source["source_id"],
                message=(
                    f"caller declared {source['source_id']!r} missing with lifecycle {lifecycle!r}"
                ),
            )
        )
    return tuple(diagnostics)


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    repository_roots: Mapping[str, Path | str] | None = None,
) -> CorpusValidationResult:
    """Validate one declaration and optionally verify explicitly rooted repository content.

    ``repository_roots`` is opt-in and maps stable repository IDs from the declaration to local
    checkout roots. The validator never discovers a checkout, follows an external URI, parses a
    source document, or writes to any supplied root.
    """
    _validate_shape(manifest)
    _validate_semantics(manifest)
    if repository_roots is not None:
        if not isinstance(repository_roots, Mapping):
            raise CorpusManifestError("repository_roots: expected mapping")
        _verify_repository_content(manifest, repository_roots)
    canonical = _canonical_bytes_unchecked(manifest)
    return CorpusValidationResult(
        corpus_id=manifest["corpus_id"],
        digest=hashlib.sha256(canonical).hexdigest(),
        diagnostics=_diagnostics(manifest),
    )


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON after validating the declaration, without mutating it."""
    validate_manifest(manifest)
    return _canonical_bytes_unchecked(manifest)


def load_manifest(
    path: Path | str,
    *,
    repository_roots: Mapping[str, Path | str] | None = None,
) -> CorpusValidationResult:
    """Read and validate a JSON declaration. This function never writes or resolves prose."""
    source = Path(path)
    try:
        text = source.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorpusManifestError(f"invalid UTF-8 in {source}: {exc}") from exc
    except OSError as exc:
        raise CorpusManifestError(f"cannot read {source}: {exc}") from exc
    try:
        manifest = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateJSONKeyError as exc:
        raise CorpusManifestError(f"invalid JSON in {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CorpusManifestError(f"invalid JSON in {source}: {exc}") from exc
    return validate_manifest(manifest, repository_roots=repository_roots)


__all__ = [
    "CANONICAL_ALGORITHM",
    "CorpusDiagnostic",
    "CorpusManifestError",
    "CorpusValidationResult",
    "canonical_manifest_bytes",
    "load_manifest",
    "validate_manifest",
]
