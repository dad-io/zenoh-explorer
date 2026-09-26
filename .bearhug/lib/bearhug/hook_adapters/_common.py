"""Shared, closed parsing helpers for provider-native hook boundaries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from bearhug.hook_dispatcher import TransientHookCustody
from bearhug.normalized_hooks import validate_normalized_hook_event


class HookAdapterError(ValueError):
    """Provider-native input or output cannot be represented safely."""


MAX_NATIVE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class AdapterCustody:
    """Transient provider bytes kept beside, never inside, normalized records or journals."""

    provider: str
    native_event: str
    raw_native_input: bytes = field(repr=False)
    raw_provider_extension: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        identifier(self.provider, where="custody.provider", maximum=128)
        identifier(self.native_event, where="custody.native_event", maximum=128)
        if (
            not isinstance(self.raw_native_input, bytes)
            or not 1 <= len(self.raw_native_input) <= MAX_NATIVE_BYTES
        ):
            raise HookAdapterError(
                f"custody.raw_native_input must contain 1..{MAX_NATIVE_BYTES} exact bytes"
            )
        if self.raw_provider_extension is not None and (
            not isinstance(self.raw_provider_extension, bytes)
            or len(self.raw_provider_extension) > MAX_NATIVE_BYTES
        ):
            raise HookAdapterError(
                f"custody.raw_provider_extension must contain 0..{MAX_NATIVE_BYTES} exact bytes"
            )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_native_input).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self.raw_native_input)


class UnsupportedProviderDecision(HookAdapterError):
    """The normalized outcome has no sealed provider-native representation."""

    code = "unsupported_block"


def _reject_constant(value: str) -> None:
    raise HookAdapterError(f"non-finite JSON number is forbidden: {value}")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HookAdapterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_native_json(raw: bytes, *, where: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_NATIVE_BYTES:
        raise HookAdapterError(f"{where} must contain 1..{MAX_NATIVE_BYTES} exact bytes")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise HookAdapterError(f"{where} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise HookAdapterError(f"{where} is not one JSON value") from exc
    if not isinstance(value, dict):
        raise HookAdapterError(f"{where} must be an object")
    return value


def require_fields(
    value: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    where: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HookAdapterError(f"{where} must be an object")
    fields = set(value)
    if not required <= fields or not fields <= required | optional:
        raise HookAdapterError(f"{where} has missing or unknown fields")
    return value


def bounded_text(
    value: Any,
    *,
    where: str,
    maximum: int = MAX_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise HookAdapterError(f"{where} must be a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HookAdapterError(f"{where} is not valid UTF-8 text") from exc
    if (not allow_empty and not value) or len(raw) > maximum:
        minimum = 0 if allow_empty else 1
        raise HookAdapterError(f"{where} must contain {minimum}..{maximum} UTF-8 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HookAdapterError(f"{where} contains control characters")
    return value


def identifier(value: Any, *, where: str, maximum: int = 256) -> str:
    result = bounded_text(value, where=where, maximum=maximum)
    if result != result.strip():
        raise HookAdapterError(f"{where} must not have surrounding whitespace")
    return result


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HookAdapterError(f"value is not canonical JSON: {exc}") from exc


def validate_repository(value: Mapping[str, Any]) -> dict[str, Any]:
    """Delegate repository validation to the normalized contract without retaining paths."""

    if not isinstance(value, Mapping):
        raise HookAdapterError("repository must be a mapping")
    return dict(value)


def verify_adapter_custody(custody: AdapterCustody, event: Mapping[str, Any]) -> bytes:
    """Return raw bytes only after their provider, event, digest, and count all match."""

    if not isinstance(custody, AdapterCustody):
        raise HookAdapterError("adapter custody has the wrong type")
    normalized = validate_normalized_hook_event(event)
    source = normalized["source"]
    if (
        source["provider"] != custody.provider
        or source["native_event"] != custody.native_event
        or source["input_sha256"] != custody.sha256
        or source["input_byte_count"] != custody.byte_count
    ):
        raise HookAdapterError("adapter custody does not match normalized event")
    extension = normalized["provider_extension"]
    if extension is None:
        if custody.raw_provider_extension is not None:
            raise HookAdapterError("adapter custody has an undeclared provider extension")
    elif custody.raw_provider_extension is None or (
        extension["sha256"] != hashlib.sha256(custody.raw_provider_extension).hexdigest()
        or extension["byte_count"] != len(custody.raw_provider_extension)
    ):
        raise HookAdapterError("adapter provider extension does not match normalized event")
    return custody.raw_native_input


def to_dispatch_custody(custody: AdapterCustody, event: Mapping[str, Any]) -> TransientHookCustody:
    """Revalidate adapter custody before granting it to declared sensitive evaluators."""

    verify_adapter_custody(custody, event)
    return TransientHookCustody(
        native_input=custody.raw_native_input,
        provider_extension=custody.raw_provider_extension,
    )
