"""Closed provider-role policy and capability negotiation.

The user selects a provider for a neutral role. Adapters translate provider mechanics; neither
model names nor installed binaries are used to guess a provider. A missing required capability is
an error, never an implicit downgrade or fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderPolicyError(ValueError):
    """A provider policy is malformed or requests an unsupported capability."""


CAPABILITIES_BY_PROVIDER: dict[str, frozenset[str]] = {
    "codex": frozenset(
        {
            "approval-lifecycle-observation",
            "model-configuration-observation",
            "os-sandbox",
            "raw-event-custody",
            "reasoning-effort-configuration-observation",
            "request-custody",
            "shell-tools",
            "workspace-editing",
        }
    ),
    "claude": frozenset(
        {
            "model-configuration-observation",
            "permission-denial-observation",
            "project-hooks",
            "raw-event-custody",
            "read-only-tool-profile",
            "request-custody",
            "workspace-editing",
        }
    ),
}
# Native continuation is a recognized request, but no adapter is qualified to provide it.
ALL_CAPABILITIES = frozenset({"native-continuation"}).union(*CAPABILITIES_BY_PROVIDER.values())
_ROLE = re.compile(r"^[a-z][a-z0-9._:/-]{0,127}$")
_TOP_FIELDS = frozenset({"schema_version", "roles"})
_ROLE_FIELDS = frozenset(
    {
        "provider",
        "model",
        "effort",
        "sandbox",
        "approval_policy",
        "required_capabilities",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderRole:
    provider: str
    model: str
    effort: str
    sandbox: str
    approval_policy: str
    required_capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    roles: dict[str, ProviderRole]


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderPolicyError(f"{field} must be a non-empty string")
    return value


def validate_provider_policy(value: Any) -> ProviderPolicy:
    """Validate a closed v1 role mapping and negotiate every required capability."""

    if not isinstance(value, dict) or set(value) != _TOP_FIELDS:
        raise ProviderPolicyError("provider policy must contain exactly schema_version and roles")
    if value["schema_version"] != "1":
        raise ProviderPolicyError("unsupported provider policy schema")
    raw_roles = value["roles"]
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise ProviderPolicyError("provider policy roles must be a non-empty object")
    roles: dict[str, ProviderRole] = {}
    for name, raw in raw_roles.items():
        if not isinstance(name, str) or _ROLE.fullmatch(name) is None:
            raise ProviderPolicyError(f"invalid provider role name: {name!r}")
        if not isinstance(raw, dict) or set(raw) != _ROLE_FIELDS:
            raise ProviderPolicyError(f"provider role {name!r} has missing or unknown fields")
        provider = _nonempty(raw["provider"], f"roles.{name}.provider")
        if provider not in CAPABILITIES_BY_PROVIDER:
            raise ProviderPolicyError(f"roles.{name}.provider is unsupported: {provider!r}")
        sandbox = _nonempty(raw["sandbox"], f"roles.{name}.sandbox")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ProviderPolicyError(f"roles.{name}.sandbox is unsupported: {sandbox!r}")
        approval = _nonempty(raw["approval_policy"], f"roles.{name}.approval_policy")
        if approval not in {"never", "on-request", "untrusted"}:
            raise ProviderPolicyError(
                f"roles.{name}.approval_policy is unsupported: {approval!r}"
            )
        required = raw["required_capabilities"]
        if not isinstance(required, list) or not all(
            isinstance(capability, str) and capability for capability in required
        ):
            raise ProviderPolicyError(
                f"roles.{name}.required_capabilities must be an array of strings"
            )
        if len(required) != len(set(required)):
            raise ProviderPolicyError(f"roles.{name}.required_capabilities contains duplicates")
        unknown = sorted(set(required) - ALL_CAPABILITIES)
        if unknown:
            raise ProviderPolicyError(f"roles.{name} names unknown capabilities: {unknown}")
        missing = sorted(set(required) - CAPABILITIES_BY_PROVIDER[provider])
        if missing:
            raise ProviderPolicyError(
                f"provider {provider!r} cannot satisfy role {name!r}: missing {missing}"
            )
        roles[name] = ProviderRole(
            provider=provider,
            model=_nonempty(raw["model"], f"roles.{name}.model"),
            effort=_nonempty(raw["effort"], f"roles.{name}.effort"),
            sandbox=sandbox,
            approval_policy=approval,
            required_capabilities=tuple(required),
        )
    return ProviderPolicy(roles)


def load_provider_policy(path: Path | str) -> ProviderPolicy:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderPolicyError(f"cannot read provider policy {path}: {exc}") from exc
    return validate_provider_policy(value)


def provider_role(policy: ProviderPolicy, role: str) -> ProviderRole:
    try:
        return policy.roles[role]
    except KeyError as exc:
        raise ProviderPolicyError(f"provider policy has no role {role!r}") from exc


__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITIES_BY_PROVIDER",
    "ProviderPolicy",
    "ProviderPolicyError",
    "ProviderRole",
    "load_provider_policy",
    "provider_role",
    "validate_provider_policy",
]
