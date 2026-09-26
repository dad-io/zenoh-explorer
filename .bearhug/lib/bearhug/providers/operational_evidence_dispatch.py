"""Route one operational evidence record to the validator its own record kind names.

This lives in its own module rather than inside either validator. Putting it in
``operational_evidence.py`` would have that module import ``claude_operational_evidence.py`` at
module level while the Claude module imports ``_canonical``, ``_read_source`` and
``_source_record`` back from it -- an import cycle. A third module depends on both leaves and is
depended on by neither.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_PROVIDER_BY_KIND = {
    "codex_operational_evidence": "openai-codex",
    "claude_operational_evidence": "anthropic-claude",
}


class OperationalEvidenceDispatchError(ValueError):
    """The evidence record names an unsupported kind, or one this receipt disowns."""


def validate_linked_operational_evidence(
    value: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any] | None,
    raw_events: bytes,
    request: bytes,
    argv: bytes,
    stderr: bytes,
    expected_executable_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate one operational evidence record through the validator its own kind names."""

    if not isinstance(value, Mapping):
        raise OperationalEvidenceDispatchError("operational evidence must be an object")
    record_kind = value.get("record_kind")
    provider = _PROVIDER_BY_KIND.get(record_kind) if isinstance(record_kind, str) else None
    if provider is None:
        raise OperationalEvidenceDispatchError(
            f"operational evidence record kind is unsupported: {record_kind!r}"
        )
    if receipt is not None and receipt.get("provider") != provider:
        raise OperationalEvidenceDispatchError(
            "operational evidence provider does not match its record kind"
        )
    # Imported here, not at module level: custody already re-imports this module fresh on every
    # call (`custody.py`'s own `_operational_evidence_file`) so a test can swap either provider
    # module in `sys.modules` per call. Binding these two functions into a table at this module's
    # own first import would freeze whichever module was live at that moment instead, defeating
    # that pattern for every call after the first. Each validator still enforces its own
    # record_kind (`operational_evidence.py:637` still rejects a Claude record).
    #
    # Caught here, at the import site itself, and
    # converted to this module's own error type -- not left to `custody.py`'s broad
    # `except (ImportError, AttributeError)` around the whole call, which used to also catch an
    # `AttributeError` raised anywhere inside either validator's own logic and mislabel it
    # "validator is unavailable" instead of "operational evidence is invalid: …".
    try:
        if record_kind == "codex_operational_evidence":
            from bearhug.providers.operational_evidence import validate_operational_evidence

            validator = validate_operational_evidence
        else:
            from bearhug.providers.claude_operational_evidence import (
                validate_claude_operational_evidence,
            )

            validator = validate_claude_operational_evidence
    except (ImportError, AttributeError) as exc:
        raise OperationalEvidenceDispatchError(
            f"operational evidence validator is unavailable: {exc}"
        ) from exc
    # The Codex branch's call stays byte-identical -- the keyword is
    # forwarded only on the Claude branch, so `validate_operational_evidence`'s signature is
    # never touched.
    if record_kind == "codex_operational_evidence":
        return validator(
            value, receipt=receipt, raw_events=raw_events, request=request, argv=argv, stderr=stderr
        )
    return validator(
        value,
        receipt=receipt,
        raw_events=raw_events,
        request=request,
        argv=argv,
        stderr=stderr,
        expected_executable_sha256=expected_executable_sha256,
    )


__all__ = ["OperationalEvidenceDispatchError", "validate_linked_operational_evidence"]
