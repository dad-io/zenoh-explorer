"""Typed, immutable evaluator results conforming to `evaluator-result.schema.json` v1.

Every evaluator returns one of these. It prints nothing, writes nothing, and exits nothing: the
coordinator owns the sole Stop decision, so an evaluator that printed would emit a second decision
alongside it.

**There is deliberately no schema validator here.** The plan's instruction is explicit — do not add
a third, subtly different validator. A validator that disagreed with the schema in some corner
would be worse than none, because it would pass results the coordinator's own contract rejects.
What this module does instead is refuse to *construct* an invalid result, and the lab's conformance
tests validate the serialized output against the schema itself with a real validator.

The constructors reject rather than coerce. A block with no remediation is an unsatisfiable stop,
and it should be unbuildable rather than caught later at serialization time when the gate that
built it is off the stack.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The schema says `{"const": "1"}` — a STRING. Kept as a string here so the constant and the
#: serialized value cannot drift; R01 first declared it as an integer, which would have serialized
#: to `1` and failed validation.
SCHEMA_VERSION = "1"

#: Mirrors the schema's `gate_id` and `reason_code` patterns. Duplicated deliberately and pinned by
#: a test that reads the schema: the runtime is stdlib-only and cannot load the JSON schema at
#: evaluation time, so the patterns live here and a lab test proves they still agree.
_GATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

_APPLICABILITY = ("applicable", "not_applicable")
_VERDICTS = ("pass", "block", "error")

#: The coordinator's alone. An independent evaluator cannot truthfully emit it, because it does not
#: know the evaluator set exists — so it is rejected here by name rather than merely absent.
_COORDINATOR_ONLY = ("not_reached",)

#: Evidence caps, from the schema. The allowlist is what keeps prompts, model output, and file
#: contents out of a result and therefore out of telemetry.
MAX_EVIDENCE_ITEMS = 32
MAX_EVIDENCE_VALUE = 512


@dataclass(frozen=True, slots=True)
class Evidence:
    """One bounded, allowlisted mechanical fact."""

    #: Mirrors the schema's evidence `kind` enum; a lab test asserts the two agree.
    KINDS = ("event_field", "path", "state", "tool_call")

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in self.KINDS:
            raise ValueError(
                f"evidence kind {self.kind!r} is not allowlisted; expected one of {self.KINDS}"
            )
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("evidence value must be a non-empty string")
        if len(self.value) > MAX_EVIDENCE_VALUE:
            raise ValueError(
                f"evidence value is {len(self.value)} characters; the cap is "
                f"{MAX_EVIDENCE_VALUE}"
            )

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True, slots=True)
class EvaluatorResult:
    """One evaluator's answer about one event. Immutable, and silent."""

    gate_id: str
    gate_version: str
    event_id: str
    applicability: str
    verdict: str | None
    reason_code: str
    remediation: str | None
    duration_ms: float
    evidence: tuple[Evidence, ...] = field(default=())

    def __post_init__(self) -> None:
        # Types first. Adversarial review found five inputs that constructed successfully and
        # then produced a schema-invalid payload — deferring the failure to a point where the
        # gate that built it is off the stack, which is precisely what this module's docstring
        # says it exists to prevent.
        for field_name in ("gate_id", "gate_version", "event_id", "reason_code"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(
                    f"{field_name} must be a string, got {type(value).__name__}: {value!r}"
                )
        if self.remediation is not None and not isinstance(self.remediation, str):
            raise ValueError(
                f"remediation must be a string or None, got {type(self.remediation).__name__}"
            )
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, Evidence) for item in self.evidence
        ):
            raise ValueError(
                "evidence must be a tuple of Evidence; a bare string constructed successfully "
                "and then raised AttributeError inside to_dict()"
            )
        # `isinstance(True, int)` is True, so a bool passed the numeric check and serialized as
        # `true` where the schema requires a number.
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, (int, float)):
            raise ValueError(
                f"duration_ms must be a non-negative number, got "
                f"{type(self.duration_ms).__name__}: {self.duration_ms!r}"
            )

        if self.verdict in _COORDINATOR_ONLY:
            raise ValueError(
                f"{self.verdict!r} is a coordinator execution state, not an evaluator verdict: "
                "an evaluator does not know the evaluator set exists and cannot truthfully "
                "report that a slot was not reached"
            )
        if self.applicability in _COORDINATOR_ONLY:
            raise ValueError(
                f"{self.applicability!r} is not an applicability; expected one of "
                f"{_APPLICABILITY}"
            )
        if self.applicability not in _APPLICABILITY:
            raise ValueError(
                f"applicability {self.applicability!r} must be one of {_APPLICABILITY}"
            )
        if not _GATE_ID.match(self.gate_id or ""):
            raise ValueError(
                f"gate_id {self.gate_id!r} must match {_GATE_ID.pattern} — the coordinator's "
                "schema rejects anything else, so building it here would only defer the failure"
            )
        if not _REASON_CODE.match(self.reason_code or ""):
            raise ValueError(f"reason_code {self.reason_code!r} must match {_REASON_CODE.pattern}")
        if not self.gate_version:
            raise ValueError("gate_version must be a non-empty string")
        if not self.event_id:
            raise ValueError(
                "event_id must be non-empty: it is created once by the coordinator and must match "
                "across every result and telemetry record for the event"
            )
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms must be a non-negative number, got {self.duration_ms!r}")

        if self.applicability == "not_applicable":
            if self.verdict is not None:
                raise ValueError(
                    "a not_applicable result carries no verdict; a `pass` here would claim the "
                    "gate approved something it never examined"
                )
            if self.remediation is not None:
                raise ValueError("a not_applicable result carries no remediation")
        else:
            if self.verdict not in _VERDICTS:
                raise ValueError(f"an applicable result's verdict must be one of {_VERDICTS}")
            if self.verdict in ("block", "error") and not self.remediation:
                raise ValueError(
                    f"a {self.verdict} carries a non-empty remediation; blocking a turn without "
                    "telling the model what to do is an unsatisfiable stop"
                )

        if len(self.evidence) > MAX_EVIDENCE_ITEMS:
            raise ValueError(
                f"evidence has {len(self.evidence)} items; the cap is {MAX_EVIDENCE_ITEMS}"
            )

    # --- constructors, so a caller cannot build an incoherent combination ---------------------

    @classmethod
    def passed(cls, *, gate_id, gate_version, event_id, reason_code, duration_ms, evidence=()):
        return cls(
            gate_id=gate_id, gate_version=gate_version, event_id=event_id,
            applicability="applicable", verdict="pass", reason_code=reason_code,
            remediation=None, duration_ms=duration_ms, evidence=tuple(evidence),
        )

    @classmethod
    def blocked(cls, *, gate_id, gate_version, event_id, reason_code, remediation, duration_ms,
                evidence=()):
        return cls(
            gate_id=gate_id, gate_version=gate_version, event_id=event_id,
            applicability="applicable", verdict="block", reason_code=reason_code,
            remediation=remediation, duration_ms=duration_ms, evidence=tuple(evidence),
        )

    @classmethod
    def errored(cls, *, gate_id, gate_version, event_id, reason_code, remediation, duration_ms,
                evidence=()):
        """An evaluator that could not evaluate. Distinct from `block` AND from `pass`.

        Whether this stops the turn is the coordinator's decision under the failure-policy table;
        the recorded verdict is `error` either way.
        """
        return cls(
            gate_id=gate_id, gate_version=gate_version, event_id=event_id,
            applicability="applicable", verdict="error", reason_code=reason_code,
            remediation=remediation, duration_ms=duration_ms, evidence=tuple(evidence),
        )

    @classmethod
    def not_applicable(cls, *, gate_id, gate_version, event_id, reason_code, duration_ms,
                       evidence=()):
        return cls(
            gate_id=gate_id, gate_version=gate_version, event_id=event_id,
            applicability="not_applicable", verdict=None, reason_code=reason_code,
            remediation=None, duration_ms=duration_ms, evidence=tuple(evidence),
        )

    def to_dict(self) -> dict:
        """Exactly the schema's keys, in the schema's types. The schema is closed."""
        return {
            "schema_version": SCHEMA_VERSION,
            "gate_id": self.gate_id,
            "gate_version": self.gate_version,
            "event_id": self.event_id,
            "applicability": self.applicability,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "remediation": self.remediation,
            "evidence": [item.to_dict() for item in self.evidence],
            "duration_ms": self.duration_ms,
        }
