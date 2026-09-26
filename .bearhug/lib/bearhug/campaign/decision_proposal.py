"""Turn an architectural discovery into a proposed Memex decision record, outside the project.

The Sep 5 algorithm's discovery loop ends in ``PROPOSE_ADR`` and ``FLAG_ADR_FOR_RECONCILIATION``.
Bear Hug may not write into a subject's ``docs/memex/`` (the no-second-authority rule), so the
proposal is written into the capsule's own custody as a ``STAGING-*.md`` draft in the exact
shape the project's Memex schema and ``/decide`` skill expect: ``status: proposed``, ``id: TBD``,
and a ``ruling_verbatim`` that says plainly that no operator ruling exists yet.  The operator, or
the ``decide`` skill acting on the operator's confirmation, moves it into the corpus.

Everything here is a pure projection over an already classified reconciliation discovery; no
provider is launched and no project file is read or written.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1"
RECORD_KIND = "capsule_decision_proposal"

# Discovery kinds whose correct disposition is a proposed decision record.
PROPOSAL_KINDS = frozenset(
    {
        "architectural_decision",
        "architecture",
        "adr",
        "invariant_conflict",
        "meaning_conflict",
        "concept_drift",
        "plan_authority",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SUMMARY = 262144


class DecisionProposalError(ValueError):
    """The discovery cannot be projected into a proposal."""


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    """One proposal: the record Bear Hug keeps and the Markdown draft the operator can adopt."""

    record: dict[str, Any]
    markdown: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.markdown).hexdigest()

    @property
    def filename(self) -> str:
        return self.record["filename"]


def _slug(text: str, *, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-") or "discovery")


def _yaml_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def wants_proposal(discovery: Mapping[str, Any]) -> bool:
    """True when a classified discovery's kind calls for a proposed decision record."""

    kind = discovery.get("kind")
    return isinstance(kind, str) and kind in PROPOSAL_KINDS


def build_decision_proposal(
    *,
    discovery: Mapping[str, Any],
    capsule_id: str,
    intent_envelope: Mapping[str, Any],
    date: str,
    evidence_refs: Iterable[str] = (),
    related_decision_ids: Iterable[str] = (),
) -> DecisionProposal:
    """Project one classified discovery into a Memex-shaped ``status: proposed`` draft."""

    if not isinstance(discovery, Mapping) or not wants_proposal(discovery):
        raise DecisionProposalError("discovery kind does not call for a decision proposal")
    summary = discovery.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > _MAX_SUMMARY:
        raise DecisionProposalError("discovery summary must be bounded, non-empty text")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise DecisionProposalError("date must be YYYY-MM-DD")
    refs = sorted({*discovery.get("evidence_refs", []), *evidence_refs})
    for ref in refs:
        if not isinstance(ref, str) or _SHA256.fullmatch(ref) is None:
            raise DecisionProposalError("evidence references must be SHA-256 digests")
    related = sorted(
        {str(item) for item in related_decision_ids if re.fullmatch(r"\d{4}", str(item))}
    )
    goal = str(intent_envelope.get("goal", ""))[:2000]
    intent_id = str(intent_envelope.get("intent_envelope_id", ""))
    summary_line = " ".join(summary.split())
    slug = _slug(summary_line)
    filename = f"STAGING-{date}-{slug}.md"

    lines = [
        "---",
        f"title: {_yaml_text(summary_line[:160])}",
        "type: decision",
        "id: TBD",
        "status: proposed",
        f"date: {date}",
        "decided_by: <operator; not yet ruled>",
        "supersedes: []",
        "superseded_by: null",
        f"tags: [proposal, capsule, {json.dumps(str(discovery.get('kind')))}]",
        "ruling_verbatim: |",
        "  PROPOSED BY BEAR HUG CAPSULE RECONCILIATION. No operator ruling exists yet; this",
        "  record is a question, never authority. Replace this block with the operator's exact",
        "  wording when the ruling is issued, or reject the record.",
        "evidence: []",
        "spec: []",
        "sources:",
        f"  - capsule {capsule_id} reconciliation ({intent_id})",
        "related: [" + ", ".join(f"decisions/{item}" for item in related) + "]",
        "---",
        "",
        "## Context",
        f"Capsule `{capsule_id}` executing intent `{intent_id}` observed a discovery of kind "
        f"`{discovery.get('kind')}` that the sealed intent does not resolve.",
        "",
        f"Goal under execution: {goal}" if goal else "Goal under execution: (not recorded)",
        "",
        "Discovery, as classified by the runtime:",
        "",
        f"> {summary_line}",
        "",
        f"Correction class: `{discovery.get('correction_class', 'unknown')}`; "
        f"actor: `{discovery.get('actor', 'runtime')}`; confidence: "
        f"`{discovery.get('confidence', 'unknown')}`.",
        "",
        "Evidence digests (Bear Hug custody, content-addressed):",
        "",
        *([f"- `{ref}`" for ref in refs] or ["- none recorded"]),
        "",
        "## Decision",
        "Not yet ruled. The operator decides whether this discovery changes an accepted",
        "decision, adds a new one, or is local work inside the existing authority.",
        "",
        "## What this forbids",
        "Nothing until ruled. Until then the capsule stays paused at its HIL boundary.",
        "",
        "## Consequences",
        "If accepted, the affected future capsules are revised under the existing envelope and",
        "the record is assigned a permanent id. If rejected, the discovery is recorded as",
        "local work or as a finding.",
        "",
        "## Alternatives rejected",
        "Recorded when the operator rules.",
        "",
    ]
    markdown = ("\n".join(lines)).encode("utf-8")
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "capsule_id": capsule_id,
        "intent_envelope_id": intent_id,
        "discovery_id": str(discovery.get("discovery_id", "")),
        "kind": str(discovery.get("kind")),
        "summary": summary_line,
        "filename": filename,
        "markdown_sha256": hashlib.sha256(markdown).hexdigest(),
        "evidence_refs": refs,
        "related_decision_ids": related,
        "status": "proposed",
        "authority": "none; a Memex STAGING draft awaiting the operator's ruling",
    }
    return DecisionProposal(record=record, markdown=markdown)


def proposals_for(
    discoveries: Iterable[Mapping[str, Any]],
    *,
    capsule_id: str,
    intent_envelope: Mapping[str, Any],
    date: str,
    evidence_refs: Iterable[str] = (),
    related_decision_ids: Iterable[str] = (),
) -> list[DecisionProposal]:
    """Build one proposal per discovery whose kind calls for it; other kinds are skipped."""

    result = []
    for discovery in discoveries:
        if wants_proposal(discovery):
            result.append(
                build_decision_proposal(
                    discovery=discovery,
                    capsule_id=capsule_id,
                    intent_envelope=intent_envelope,
                    date=date,
                    evidence_refs=evidence_refs,
                    related_decision_ids=related_decision_ids,
                )
            )
    return result


__all__ = [
    "DecisionProposal",
    "DecisionProposalError",
    "PROPOSAL_KINDS",
    "build_decision_proposal",
    "proposals_for",
    "wants_proposal",
]
