"""Exact, fixture-bound provider adapter compatibility policy.

Campaign authority never assumes a new CLI version is compatible.  Each adapter/version pair is
classified explicitly and its protocol fixtures are content-bound.  Standalone provider runs do
not consult this policy; the campaign wrapper must call ``require_supported`` before launch.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class ProviderCompatibilityError(ValueError):
    """A compatibility policy is malformed, stale, or does not authorize the adapter version."""


@dataclass(frozen=True, slots=True)
class CompatibilityFixture:
    path: str
    sha256: str
    scenario: str
    origin: str


@dataclass(frozen=True, slots=True)
class CompatibilityVersion:
    version: str
    status: str
    fixtures: tuple[CompatibilityFixture, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompatibilityAdapter:
    adapter: str
    provider: str
    versions: tuple[CompatibilityVersion, ...]


@dataclass(frozen=True, slots=True)
class ProviderCompatibilityPolicy:
    adapters: tuple[CompatibilityAdapter, ...]


_TOP = frozenset({"schema_version", "record_kind", "adapters"})
_ADAPTER = frozenset({"adapter", "provider", "versions"})
_VERSION = frozenset({"version", "status", "fixtures", "limitations"})
_FIXTURE = frozenset({"path", "sha256", "scenario", "origin"})


def _object(value: Any, fields: frozenset[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderCompatibilityError(f"{where} has missing or unknown fields")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderCompatibilityError(f"{where} must be a non-empty string")
    return value


def _sha256(value: Any, where: str) -> str:
    value = _string(value, where)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ProviderCompatibilityError(f"{where} must be lowercase SHA-256")
    return value


def _fixture_path(value: Any, where: str) -> str:
    value = _string(value, where)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("~")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ProviderCompatibilityError(f"{where} must be a canonical repository-relative path")
    return value


def validate_compatibility_policy(value: Any) -> ProviderCompatibilityPolicy:
    policy = _object(value, _TOP, "compatibility policy")
    if policy["schema_version"] != "1" or policy["record_kind"] != "provider_compatibility_policy":
        raise ProviderCompatibilityError("unsupported compatibility policy schema or kind")
    raw_adapters = policy["adapters"]
    if not isinstance(raw_adapters, list) or not raw_adapters:
        raise ProviderCompatibilityError("compatibility policy must name at least one adapter")
    adapters: list[CompatibilityAdapter] = []
    seen_adapters: set[str] = set()
    for adapter_index, raw_adapter in enumerate(raw_adapters):
        raw_adapter = _object(raw_adapter, _ADAPTER, f"adapters[{adapter_index}]")
        adapter_id = _string(raw_adapter["adapter"], f"adapters[{adapter_index}].adapter")
        if adapter_id in seen_adapters:
            raise ProviderCompatibilityError(f"duplicate adapter {adapter_id!r}")
        seen_adapters.add(adapter_id)
        provider = _string(raw_adapter["provider"], f"adapters[{adapter_index}].provider")
        if provider not in {"anthropic-claude", "openai-codex"}:
            raise ProviderCompatibilityError(f"unsupported provider {provider!r}")
        raw_versions = raw_adapter["versions"]
        if not isinstance(raw_versions, list) or not raw_versions:
            raise ProviderCompatibilityError(f"adapter {adapter_id!r} has no classified versions")
        versions: list[CompatibilityVersion] = []
        seen_versions: set[str] = set()
        for version_index, raw_version in enumerate(raw_versions):
            where = f"adapters[{adapter_index}].versions[{version_index}]"
            raw_version = _object(raw_version, _VERSION, where)
            version = _string(raw_version["version"], f"{where}.version")
            if version in seen_versions:
                raise ProviderCompatibilityError(
                    f"adapter {adapter_id!r} repeats version {version!r}"
                )
            seen_versions.add(version)
            status = raw_version["status"]
            if status not in {"supported", "blocked"}:
                raise ProviderCompatibilityError(f"{where}.status is unsupported")
            limitations = raw_version["limitations"]
            if not isinstance(limitations, list) or not all(
                isinstance(item, str) and item for item in limitations
            ) or len(limitations) != len(set(limitations)):
                raise ProviderCompatibilityError(f"{where}.limitations is invalid")
            raw_fixtures = raw_version["fixtures"]
            if not isinstance(raw_fixtures, list):
                raise ProviderCompatibilityError(f"{where}.fixtures must be an array")
            fixtures: list[CompatibilityFixture] = []
            seen_paths: set[str] = set()
            for fixture_index, raw_fixture in enumerate(raw_fixtures):
                fixture_where = f"{where}.fixtures[{fixture_index}]"
                raw_fixture = _object(raw_fixture, _FIXTURE, fixture_where)
                path = _fixture_path(raw_fixture["path"], f"{fixture_where}.path")
                if path in seen_paths:
                    raise ProviderCompatibilityError(f"{where} repeats fixture path {path!r}")
                seen_paths.add(path)
                scenario = raw_fixture["scenario"]
                if scenario not in {"success", "provider_failure", "protocol_failure"}:
                    raise ProviderCompatibilityError(f"{fixture_where}.scenario is unsupported")
                origin = raw_fixture["origin"]
                if origin not in {"captured", "constructed"}:
                    raise ProviderCompatibilityError(f"{fixture_where}.origin is unsupported")
                fixtures.append(
                    CompatibilityFixture(
                        path,
                        _sha256(raw_fixture["sha256"], f"{fixture_where}.sha256"),
                        scenario,
                        origin,
                    )
                )
            scenarios = {fixture.scenario for fixture in fixtures}
            if status == "supported" and (
                "success" not in scenarios
                or not scenarios.intersection({"provider_failure", "protocol_failure"})
                or any(fixture.origin != "captured" for fixture in fixtures)
            ):
                raise ProviderCompatibilityError(
                    f"supported {adapter_id} {version} needs captured success and failure fixtures"
                )
            if status == "blocked" and not limitations:
                raise ProviderCompatibilityError(
                    f"blocked {adapter_id} {version} must state at least one limitation"
                )
            versions.append(
                CompatibilityVersion(version, status, tuple(fixtures), tuple(limitations))
            )
        adapters.append(CompatibilityAdapter(adapter_id, provider, tuple(versions)))
    return ProviderCompatibilityPolicy(tuple(adapters))


def load_compatibility_policy(path: Path | str) -> ProviderCompatibilityPolicy:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderCompatibilityError(f"cannot read compatibility policy {path}: {exc}") from exc
    return validate_compatibility_policy(value)


def classified_version(
    policy: ProviderCompatibilityPolicy,
    adapter: str,
    version: str,
) -> CompatibilityVersion:
    for entry in policy.adapters:
        if entry.adapter == adapter:
            for classified in entry.versions:
                if classified.version == version:
                    return classified
            raise ProviderCompatibilityError(
                f"adapter {adapter!r} version {version!r} is unclassified"
            )
    raise ProviderCompatibilityError(f"adapter {adapter!r} is unclassified")


def verify_compatibility_fixtures(
    classified: CompatibilityVersion,
    repository_root: Path | str,
) -> None:
    root = Path(repository_root).resolve()
    for fixture in classified.fixtures:
        unresolved = root / fixture.path
        path = unresolved.resolve()
        if root != path and root not in path.parents:
            raise ProviderCompatibilityError(f"fixture escapes repository root: {fixture.path}")
        relative_parts = PurePosixPath(fixture.path).parts
        components = [
            root.joinpath(*relative_parts[:index])
            for index in range(1, len(relative_parts) + 1)
        ]
        if not path.is_file() or any(component.is_symlink() for component in components):
            raise ProviderCompatibilityError(f"fixture is missing or symlinked: {fixture.path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != fixture.sha256:
            raise ProviderCompatibilityError(f"fixture digest changed: {fixture.path}")


def require_supported(
    policy: ProviderCompatibilityPolicy,
    *,
    adapter: str,
    version: str,
    repository_root: Path | str,
) -> CompatibilityVersion:
    classified = classified_version(policy, adapter, version)
    verify_compatibility_fixtures(classified, repository_root)
    if classified.status != "supported":
        detail = ", ".join(classified.limitations)
        raise ProviderCompatibilityError(
            f"adapter {adapter!r} version {version!r} is blocked: {detail}"
        )
    return classified


__all__ = [
    "CompatibilityAdapter",
    "CompatibilityFixture",
    "CompatibilityVersion",
    "ProviderCompatibilityError",
    "ProviderCompatibilityPolicy",
    "classified_version",
    "load_compatibility_policy",
    "require_supported",
    "validate_compatibility_policy",
    "verify_compatibility_fixtures",
]
