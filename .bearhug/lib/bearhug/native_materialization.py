"""Closed, content-addressed descriptions of provider-native harness candidates.

The manifest is deliberately not an installer receipt or a projection attestation.  It seals the
bytes and provider mechanics which a later compiler/installer may use, and binds them to one
validated ``harness-policy.v2``.  Compilation in this module is pure: it reads no ambient provider
state and writes no files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.harness_policy import ALL_CAPABILITIES
from bearhug.harness_policy_v2 import HarnessPolicyV2, compile_harness_policy_v2


class NativeMaterializationError(ValueError):
    """A native-materialization manifest is malformed or overclaims its support."""


@dataclass(frozen=True, slots=True)
class NativeMaterializationManifest:
    """One canonical candidate manifest and its exact byte identity."""

    document: dict[str, Any]
    canonical_bytes: bytes
    sha256: str


_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_COMMAND = re.compile(r"^[A-Za-z0-9._+-]+$")

_PROVIDERS = frozenset({"claude", "codex"})
_MODES = frozenset({"0600", "0644", "0755"})
_PURPOSES = frozenset(
    {
        "agent-definition",
        "hook-configuration",
        "hook-handler",
        "instruction",
        "mcp-configuration",
        "provider-configuration",
        "rule",
        "runtime-library",
        "skill",
    }
)
_DEPENDENCY_KINDS = frozenset({"data-bundle", "go-module", "python-package", "runtime-package"})
_EQUIVALENCE = frozenset(
    {"equivalent", "capability-equivalent", "provider-specific-approved-substitute", "unsupported"}
)
_REQUIREMENTS = frozenset({"optional", "required"})
_FAILURE_POLICIES = frozenset({"block", "continue", "observe-only"})
_EXECUTION_MODES = frozenset({"ordered", "approved-concurrent"})

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "provider_version_bounds",
        "compiler",
        "source_policy",
        "file_set_complete",
        "destination_allowlist",
        "files",
        "hooks",
        "dependencies",
        "executables",
        "capability_mappings",
        "claims",
        "limitations",
    }
)
_VERSION_BOUND_FIELDS = frozenset({"minimum_inclusive", "maximum_inclusive"})
_IDENTITY_FIELDS = frozenset({"sha256", "bytes"})
_COMPILER_FIELDS = frozenset({"name", "version", "identity"})
_SOURCE_FIELDS = frozenset({"id", "version", "sha256"})
_ALLOWLIST_FIELDS = frozenset({"path", "kind"})
_FILE_FIELDS = frozenset({"path", "purpose", "mode", "sha256", "bytes", "entry_type", "link_count"})
_HOOK_FIELDS = frozenset(
    {
        "event_policy_id",
        "evaluator_id",
        "semantic_event",
        "source_event_sha256",
        "source_evaluator_sha256",
        "provider_event",
        "matcher",
        "invocation",
        "timeout_ms",
        "execution_mode",
        "order",
        "concurrency_approval_sha256",
        "event_failure_policy",
        "failure_policy",
        "requirement",
        "equivalence",
        "substitute_approval_sha256",
        "limitations",
    }
)
_MATCHER_FIELDS = frozenset({"tool_families", "native_expression"})
_INVOCATION_FIELDS = frozenset(
    {
        "executable_id",
        "entrypoint_path",
        "arguments",
        "environment",
        "working_directory",
    }
)
_DEPENDENCY_FIELDS = frozenset({"id", "kind", "version", "identity", "purpose"})
_EXECUTABLE_FIELDS = frozenset({"id", "command", "version", "identity", "purpose"})
_CAPABILITY_FIELDS = frozenset(
    {
        "capability",
        "requirement",
        "equivalence",
        "native_capabilities",
        "evidence_sha256",
        "substitute_approval_sha256",
        "limitations",
    }
)
_CLAIM_FIELDS = frozenset(
    {"installed", "effective_sources_verified", "provider_projection_verified", "runtime_observed"}
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise NativeMaterializationError(f"manifest is not canonical JSON: {exc}") from exc
    return (rendered + "\n").encode()


def _closed(value: Any, fields: frozenset[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise NativeMaterializationError(f"{context} has missing or unknown fields")
    return value


def _string(value: Any, context: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise NativeMaterializationError(f"{context} must be a non-empty bounded string")
    if unicodedata.normalize("NFC", value) != value:
        raise NativeMaterializationError(f"{context} is not NFC-normalized")
    for character in value:
        codepoint = ord(character)
        if (
            codepoint <= 0x08
            or codepoint in {0x0B, 0x0C}
            or 0x0E <= codepoint <= 0x1F
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            raise NativeMaterializationError(f"{context} contains a forbidden code point")
    return value


def _identifier(value: Any, context: str) -> str:
    value = _string(value, context, maximum=64)
    if _ID.fullmatch(value) is None:
        raise NativeMaterializationError(f"{context} is not an identifier: {value!r}")
    return value


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise NativeMaterializationError(f"{context} must be lowercase SHA-256")
    return value


def _integer(value: Any, context: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise NativeMaterializationError(f"{context} is outside its integer bounds")
    return value


def _version(
    value: Any, context: str
) -> tuple[int, int, int, tuple[tuple[int, int, int | str], ...]]:
    value = _string(value, context, maximum=128)
    match = _VERSION.fullmatch(value)
    if match is None:
        raise NativeMaterializationError(f"{context} is not a semantic version: {value!r}")
    prerelease: list[tuple[int, int, int | str]] = []
    if match.group(4) is None:
        prerelease.append((1, 0, ""))
    else:
        for part in match.group(4).split("."):
            if part.isdigit():
                if len(part) > 1 and part.startswith("0"):
                    raise NativeMaterializationError(
                        f"{context} has a leading-zero prerelease identifier"
                    )
                prerelease.append((0, 0, int(part)))
            else:
                prerelease.append((0, 1, part))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(prerelease)


def _repo_path(value: Any, context: str) -> str:
    value = _string(value, context, maximum=1024)
    if (
        value in {".", ".."}
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise NativeMaterializationError(f"{context} is not a canonical repository-relative path")
    path = PurePosixPath(value)
    if str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise NativeMaterializationError(f"{context} is not a canonical repository-relative path")
    if any(_PATH_SEGMENT.fullmatch(part) is None for part in path.parts):
        raise NativeMaterializationError(f"{context} contains an unsupported path segment")
    return value


def _identity(value: Any, context: str) -> tuple[str, int]:
    record = _closed(value, _IDENTITY_FIELDS, context)
    return (
        _sha256(record["sha256"], f"{context}.sha256"),
        _integer(record["bytes"], f"{context}.bytes", minimum=0, maximum=1 << 40),
    )


def _strings(value: Any, context: str, *, sorted_unique: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise NativeMaterializationError(f"{context} must be an array")
    checked = [_string(item, f"{context}[{index}]") for index, item in enumerate(value)]
    if sorted_unique and checked != sorted(set(checked)):
        raise NativeMaterializationError(f"{context} must be sorted and contain no duplicates")
    return checked


def _equivalence(record: dict[str, Any], context: str, *, required: bool) -> str:
    value = record["equivalence"]
    if value not in _EQUIVALENCE:
        raise NativeMaterializationError(f"{context}.equivalence is unsupported: {value!r}")
    approval = record["substitute_approval_sha256"]
    limitations = _strings(record["limitations"], f"{context}.limitations", sorted_unique=True)
    if value == "unsupported":
        if required:
            raise NativeMaterializationError(f"{context} leaves required behavior unsupported")
        if approval is not None or not limitations:
            raise NativeMaterializationError(
                f"{context} unsupported behavior requires limitations and no approval"
            )
    elif value == "provider-specific-approved-substitute":
        _sha256(approval, f"{context}.substitute_approval_sha256")
        if not limitations:
            raise NativeMaterializationError(f"{context} approved substitute needs a limitation")
    elif approval is not None:
        raise NativeMaterializationError(f"{context} carries an inapplicable substitute approval")
    return value


def _expected_capabilities(policy: HarnessPolicyV2) -> dict[str, str]:
    expected = {row["id"]: row["requirement"] for row in policy.document["capabilities"]}
    for role in policy.document["agent_roles"]:
        if role["requirement"] == "required":
            for capability in role["required_capabilities"]:
                expected[capability] = "required"
    return expected


def _canonical_document(raw: dict[str, Any]) -> dict[str, Any]:
    document = copy.deepcopy(raw)
    document["destination_allowlist"] = sorted(
        document["destination_allowlist"], key=lambda row: (row["path"], row["kind"])
    )
    document["files"] = sorted(document["files"], key=lambda row: row["path"])
    document["hooks"] = sorted(
        document["hooks"],
        key=lambda row: (row["semantic_event"], row["event_policy_id"], row["evaluator_id"]),
    )
    document["dependencies"] = sorted(document["dependencies"], key=lambda row: row["id"])
    document["executables"] = sorted(document["executables"], key=lambda row: row["id"])
    document["capability_mappings"] = sorted(
        document["capability_mappings"], key=lambda row: row["capability"]
    )
    document["limitations"] = sorted(document["limitations"])
    return document


def compile_native_materialization_manifest(
    value: Mapping[str, Any] | NativeMaterializationManifest,
    *,
    policy: Mapping[str, Any] | HarnessPolicyV2,
) -> NativeMaterializationManifest:
    """Validate and canonicalize a candidate manifest without materializing or installing it."""

    compiled_policy = compile_harness_policy_v2(policy)
    if isinstance(value, NativeMaterializationManifest):
        rebuilt = compile_native_materialization_manifest(value.document, policy=compiled_policy)
        if rebuilt.canonical_bytes != value.canonical_bytes or rebuilt.sha256 != value.sha256:
            raise NativeMaterializationError("compiled manifest no longer matches its document")
        return rebuilt
    if not isinstance(value, Mapping):
        raise NativeMaterializationError("native materialization manifest must be an object")
    raw = _closed(copy.deepcopy(dict(value)), _TOP_FIELDS, "native materialization manifest")
    if raw["schema_version"] != "1" or raw["record_kind"] != "native_materialization_manifest":
        raise NativeMaterializationError("unsupported native materialization manifest identity")
    if raw["provider"] not in _PROVIDERS:
        raise NativeMaterializationError(f"unsupported provider: {raw['provider']!r}")

    bounds = _closed(
        raw["provider_version_bounds"], _VERSION_BOUND_FIELDS, "provider_version_bounds"
    )
    minimum = _version(bounds["minimum_inclusive"], "provider_version_bounds.minimum_inclusive")
    maximum = _version(bounds["maximum_inclusive"], "provider_version_bounds.maximum_inclusive")
    if minimum > maximum:
        raise NativeMaterializationError("provider version bounds are reversed")

    compiler = _closed(raw["compiler"], _COMPILER_FIELDS, "compiler")
    _identifier(compiler["name"], "compiler.name")
    _version(compiler["version"], "compiler.version")
    _identity(compiler["identity"], "compiler.identity")

    source = _closed(raw["source_policy"], _SOURCE_FIELDS, "source_policy")
    expected_source = {
        "id": compiled_policy.document["policy_id"],
        "version": compiled_policy.document["policy_version"],
        "sha256": compiled_policy.sha256,
    }
    if source != expected_source:
        raise NativeMaterializationError("source_policy does not match the supplied policy bytes")
    if raw["file_set_complete"] is not True:
        raise NativeMaterializationError("file_set_complete must be true")

    allowlist = raw["destination_allowlist"]
    if not isinstance(allowlist, list) or not allowlist:
        raise NativeMaterializationError("destination_allowlist must be a non-empty array")
    allow_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(allowlist):
        context = f"destination_allowlist[{index}]"
        item = _closed(item, _ALLOWLIST_FIELDS, context)
        path = _repo_path(item["path"], f"{context}.path")
        if item["kind"] not in {"directory", "file"}:
            raise NativeMaterializationError(f"{context}.kind is unsupported")
        key = (path, item["kind"])
        if key in allow_keys or any(existing[0] == path for existing in allow_keys):
            raise NativeMaterializationError(f"destination_allowlist repeats path {path!r}")
        allow_keys.add(key)

    files = raw["files"]
    if not isinstance(files, list) or not files:
        raise NativeMaterializationError("files must be a non-empty complete array")
    paths: set[str] = set()
    for index, item in enumerate(files):
        context = f"files[{index}]"
        item = _closed(item, _FILE_FIELDS, context)
        path = _repo_path(item["path"], f"{context}.path")
        if path in paths:
            raise NativeMaterializationError(f"files repeat path {path!r}")
        paths.add(path)
        if item["purpose"] not in _PURPOSES:
            raise NativeMaterializationError(f"{context}.purpose is unsupported")
        if item["mode"] not in _MODES:
            raise NativeMaterializationError(f"{context}.mode is unsupported")
        (
            _sha256(item["sha256"], f"{context}.sha256"),
            _integer(item["bytes"], f"{context}.bytes", minimum=0, maximum=1 << 40),
        )
        if (
            item["entry_type"] != "regular-file"
            or type(item["link_count"]) is not int
            or item["link_count"] != 1
        ):
            raise NativeMaterializationError(f"{context} makes a symlink or hardlink claim")
        permitted = any(
            path == allowed_path if kind == "file" else path.startswith(f"{allowed_path}/")
            for allowed_path, kind in allow_keys
        )
        if not permitted:
            raise NativeMaterializationError(f"file destination {path!r} is outside the allowlist")

    executable_ids: set[str] = set()
    dependencies = raw["dependencies"]
    if not isinstance(dependencies, list):
        raise NativeMaterializationError("dependencies must be an array")
    dependency_ids: set[str] = set()
    for index, item in enumerate(dependencies):
        context = f"dependencies[{index}]"
        item = _closed(item, _DEPENDENCY_FIELDS, context)
        identifier = _identifier(item["id"], f"{context}.id")
        if identifier in dependency_ids:
            raise NativeMaterializationError(f"dependencies repeat id {identifier!r}")
        dependency_ids.add(identifier)
        if item["kind"] not in _DEPENDENCY_KINDS:
            raise NativeMaterializationError(f"{context}.kind is unsupported")
        _version(item["version"], f"{context}.version")
        _identity(item["identity"], f"{context}.identity")
        _string(item["purpose"], f"{context}.purpose")

    executables = raw["executables"]
    if not isinstance(executables, list):
        raise NativeMaterializationError("executables must be an array")
    for index, item in enumerate(executables):
        context = f"executables[{index}]"
        item = _closed(item, _EXECUTABLE_FIELDS, context)
        identifier = _identifier(item["id"], f"{context}.id")
        if identifier in executable_ids or identifier in dependency_ids:
            raise NativeMaterializationError(f"manifest repeats dependency identity {identifier!r}")
        executable_ids.add(identifier)
        command = _string(item["command"], f"{context}.command", maximum=256)
        if _COMMAND.fullmatch(command) is None:
            raise NativeMaterializationError(f"{context}.command must be a bare executable name")
        _version(item["version"], f"{context}.version")
        _identity(item["identity"], f"{context}.identity")
        _string(item["purpose"], f"{context}.purpose")

    hooks = raw["hooks"]
    if not isinstance(hooks, list):
        raise NativeMaterializationError("hooks must be an array")
    expected_hooks: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for event in compiled_policy.document["event_policies"]:
        for evaluator in event["evaluators"]:
            expected_hooks[(event["id"], evaluator["id"])] = (event, evaluator)
    seen_hooks: set[tuple[str, str]] = set()
    for index, item in enumerate(hooks):
        context = f"hooks[{index}]"
        item = _closed(item, _HOOK_FIELDS, context)
        key = (
            _identifier(item["event_policy_id"], f"{context}.event_policy_id"),
            _identifier(item["evaluator_id"], f"{context}.evaluator_id"),
        )
        if key in seen_hooks:
            raise NativeMaterializationError(f"hooks repeat policy identity {key!r}")
        seen_hooks.add(key)
        if key not in expected_hooks:
            raise NativeMaterializationError(f"{context} does not identify a policy evaluator")
        event, evaluator = expected_hooks[key]
        exact = {
            "semantic_event": event["event"],
            "source_event_sha256": event["semantic_sha256"],
            "source_evaluator_sha256": evaluator["semantic_sha256"],
            "timeout_ms": evaluator["timeout"]["value"],
            "execution_mode": event["execution"]["mode"],
            "order": evaluator["order"],
            "concurrency_approval_sha256": (
                event["execution"]["concurrency_approval"]["decision_sha256"]
                if event["execution"]["concurrency_approval"] is not None
                else None
            ),
            "event_failure_policy": event["event_failure_policy"],
            "failure_policy": evaluator["failure_policy"],
            "requirement": evaluator["requirement"],
        }
        for field, expected in exact.items():
            if item[field] != expected:
                raise NativeMaterializationError(f"{context}.{field} does not match source policy")
        equivalence = _equivalence(item, context, required=evaluator["requirement"] == "required")
        if equivalence == "unsupported":
            if (
                item["provider_event"] is not None
                or item["matcher"] is not None
                or item["invocation"] is not None
            ):
                raise NativeMaterializationError(
                    f"{context} unsupported hook carries native mechanics"
                )
            continue
        _string(item["provider_event"], f"{context}.provider_event", maximum=128)
        matcher = _closed(item["matcher"], _MATCHER_FIELDS, f"{context}.matcher")
        if matcher["tool_families"] != evaluator["matcher"]["tool_families"]:
            raise NativeMaterializationError(f"{context}.matcher.tool_families changed semantics")
        _string(matcher["native_expression"], f"{context}.matcher.native_expression")
        invocation = _closed(item["invocation"], _INVOCATION_FIELDS, f"{context}.invocation")
        if invocation["executable_id"] not in executable_ids:
            raise NativeMaterializationError(f"{context}.invocation names an unknown executable")
        entrypoint = _repo_path(
            invocation["entrypoint_path"], f"{context}.invocation.entrypoint_path"
        )
        entrypoint_rows = [row for row in files if row["path"] == entrypoint]
        if not entrypoint_rows or entrypoint_rows[0]["purpose"] != "hook-handler":
            raise NativeMaterializationError(
                f"{context}.invocation.entrypoint_path is not a declared hook handler"
            )
        _strings(invocation["arguments"], f"{context}.invocation.arguments")
        environment = _strings(
            invocation["environment"], f"{context}.invocation.environment", sorted_unique=True
        )
        if any(_ENVIRONMENT_NAME.fullmatch(name) is None for name in environment):
            raise NativeMaterializationError(
                f"{context}.invocation.environment has an invalid name"
            )
        if invocation["working_directory"] != "repository-root":
            raise NativeMaterializationError(
                f"{context}.invocation.working_directory is unsupported"
            )
    if seen_hooks != set(expected_hooks):
        missing = sorted(set(expected_hooks) - seen_hooks)
        raise NativeMaterializationError(f"hooks omit policy evaluators: {missing}")

    mappings = raw["capability_mappings"]
    if not isinstance(mappings, list):
        raise NativeMaterializationError("capability_mappings must be an array")
    expected_capabilities = _expected_capabilities(compiled_policy)
    seen_capabilities: set[str] = set()
    for index, item in enumerate(mappings):
        context = f"capability_mappings[{index}]"
        item = _closed(item, _CAPABILITY_FIELDS, context)
        capability = item["capability"]
        if capability not in ALL_CAPABILITIES:
            raise NativeMaterializationError(f"{context}.capability is unsupported")
        if capability in seen_capabilities:
            raise NativeMaterializationError(f"capability_mappings repeat {capability!r}")
        seen_capabilities.add(capability)
        if capability not in expected_capabilities:
            raise NativeMaterializationError(f"{context} maps an undeclared capability")
        requirement = item["requirement"]
        if requirement not in _REQUIREMENTS or requirement != expected_capabilities[capability]:
            raise NativeMaterializationError(f"{context}.requirement does not match source policy")
        equivalence = _equivalence(item, context, required=requirement == "required")
        native = _strings(
            item["native_capabilities"], f"{context}.native_capabilities", sorted_unique=True
        )
        evidence = item["evidence_sha256"]
        if equivalence == "unsupported":
            if native or evidence is not None:
                raise NativeMaterializationError(f"{context} unsupported mapping carries evidence")
        else:
            if not native:
                raise NativeMaterializationError(
                    f"{context} supported mapping names no native capability"
                )
            _sha256(evidence, f"{context}.evidence_sha256")
    if seen_capabilities != set(expected_capabilities):
        missing = sorted(set(expected_capabilities) - seen_capabilities)
        raise NativeMaterializationError(f"capability_mappings omit required mappings: {missing}")

    claims = _closed(raw["claims"], _CLAIM_FIELDS, "claims")
    if any(value is not False for value in claims.values()):
        raise NativeMaterializationError(
            "candidate manifest may not claim projection, installation, or runtime proof"
        )
    _strings(raw["limitations"], "limitations", sorted_unique=True)

    document = _canonical_document(raw)
    blob = _canonical_bytes(document)
    return NativeMaterializationManifest(
        document=document,
        canonical_bytes=blob,
        sha256=hashlib.sha256(blob).hexdigest(),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise NativeMaterializationError(f"manifest JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def load_native_materialization_manifest(
    path: Path | str,
    *,
    policy: Mapping[str, Any] | HarnessPolicyV2,
) -> NativeMaterializationManifest:
    """Load JSON with duplicate-key rejection, then perform semantic validation."""

    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except NativeMaterializationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeMaterializationError(
            f"cannot read native materialization manifest {path}: {exc}"
        ) from exc
    return compile_native_materialization_manifest(value, policy=policy)


__all__ = [
    "NativeMaterializationError",
    "NativeMaterializationManifest",
    "compile_native_materialization_manifest",
    "load_native_materialization_manifest",
]
