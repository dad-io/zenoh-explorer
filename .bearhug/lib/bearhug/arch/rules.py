"""A06 -- architecture rules: data, checked against the index, never inferred from source.

A rule is not code that reads Go, and not a heuristic over file names. It is a small parametrised
check over records the extractor already produced (A01-A05), and it reports one of three verdicts
per rule:

``PASS``
    Every record the rule inspected satisfied it.
``FAIL``
    At least one record did not, and every offending record's id is named -- never a count alone.
``UNEVALUABLE``
    The index carries **no record at all** of the kind this rule needs. CLAUDE.md's own lesson
    applies here exactly as it applies to a detector: silence is not absence. `dlv-verify-gate`
    was blind to 132 of 132 `.go` writes and reported a clean run; a rule that reports PASS
    because its input kind is empty would repeat that mistake with an architecture rule instead
    of a hook. UNEVALUABLE is the honest alternative to a vacuous PASS.

Three rule types, and nothing else. An unrecognised ``type`` is refused at :func:`load_rules`,
not silently skipped -- a rule the loader does not recognise is a rule that never ran, which is
the same silence-is-not-absence failure at the loader instead of the checker.

``layering``
    A module never imports another module, checked against every ``edge`` record's
    ``from_module``/``to_module`` (A01 Q2).
``tests_required``
    Every ``package`` whose ``import_path`` contains a substring has at least one ``test_mapping``
    record (A01 Q1, Q5).
``decisions_cite_code``
    Every ``decision`` record (A06; see ``extract.read_decisions``) cites code -- has a companion
    ``decision_link`` -- or is exempt (frontmatter ``code: none``).

Rules are checked against records the index already carries; this module opens no file and reads
no product source. Callers select a project-owned rules file explicitly; there is no global ruled
set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, Severity

RULES_SCHEMA_NAME = "architecture-rules.schema.json"
RULES_SCHEMA_VERSION = "1"

_LAYERING_KEYS = frozenset({"id", "type", "from_module", "to_module"})
_TESTS_REQUIRED_KEYS = frozenset({"id", "type", "import_path_contains"})
_DECISIONS_CITE_CODE_KEYS = frozenset({"id", "type"})
_EXPECTED_KEYS: dict[str, frozenset[str]] = {
    "layering": _LAYERING_KEYS,
    "tests_required": _TESTS_REQUIRED_KEYS,
    "decisions_cite_code": _DECISIONS_CITE_CODE_KEYS,
}


class RulesLoadError(ValueError):
    """The rules file is malformed, or names a rule type this module does not recognise."""


class RuleVerdict(StrEnum):
    """What one rule concluded. See the module docstring for what each means."""

    PASS = "pass"
    FAIL = "fail"
    UNEVALUABLE = "uneval"


@dataclass(frozen=True, slots=True)
class RuleResult:
    """One rule, checked once against one set of records.

    ``offenders`` carries the offending records themselves, not just their ids, so a caller can
    build a citation (path, line) without re-querying the index -- the same reason every A01
    record carries its own provenance.
    """

    rule_id: str
    rule_type: str
    verdict: RuleVerdict
    offenders: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    reason: str | None = None  # populated only for UNEVALUABLE

    @property
    def offender_ids(self) -> tuple[str, ...]:
        return tuple(offender["id"] for offender in self.offenders)

    def line(self) -> str:
        """One line: `PASS`, `FAIL n` with the offending ids, or `UNEVALUABLE` with why."""
        if self.verdict is RuleVerdict.PASS:
            return f"{self.rule_id}: PASS"
        if self.verdict is RuleVerdict.FAIL:
            ids = self.offender_ids
            return f"{self.rule_id}: FAIL {len(ids)} -- {', '.join(ids)}"
        return f"{self.rule_id}: UNEVALUABLE -- {self.reason}"


def load_rules(path: Path | str) -> dict[str, Any]:
    """Load and validate a rules file.

    Deliberately hand-rolled rather than a `jsonschema` validation: `bearhug`'s own runtime
    dependencies are stdlib-only (`pyproject.toml`), the same reason `arch/freshness.py` validates
    the index artifact by hand instead of importing a validator. `docs/schemas/architecture-rules
    .schema.json` is the schema this logic must never drift from; a test asserts `docs/arch-rules
    .json` conforms to both.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RulesLoadError(f"rules file is not a mapping: {type(payload).__name__}")
    if payload.get("schema_version") != RULES_SCHEMA_VERSION:
        raise RulesLoadError(
            f"unsupported rules schema_version: {payload.get('schema_version')!r}"
        )
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RulesLoadError("rules must be a non-empty list")

    seen_ids: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise RulesLoadError(f"a rule entry is not a mapping: {rule!r}")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise RulesLoadError(f"a rule is missing a valid id: {rule!r}")
        if rule_id in seen_ids:
            raise RulesLoadError(f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)
        rule_type = rule.get("type")
        expected = _EXPECTED_KEYS.get(rule_type)
        if expected is None:
            raise RulesLoadError(f"rule {rule_id!r} names an unrecognised type: {rule_type!r}")
        if set(rule) != expected:
            raise RulesLoadError(
                f"rule {rule_id!r} ({rule_type}) has keys {sorted(rule)}, "
                f"expected exactly {sorted(expected)}"
            )
    return payload


def _by_kind(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("kind") == kind]


def _check_layering(rule: dict[str, Any], records: list[dict[str, Any]]) -> RuleResult:
    edges = _by_kind(records, "edge")
    if not edges:
        return RuleResult(
            rule["id"],
            "layering",
            RuleVerdict.UNEVALUABLE,
            reason="no edge records in the index -- edges were never extracted, not proven absent",
        )
    offenders = tuple(
        sorted(
            (
                edge
                for edge in edges
                if edge.get("from_module") == rule["from_module"]
                and edge.get("to_module") == rule["to_module"]
            ),
            key=lambda edge: edge["id"],
        )
    )
    verdict = RuleVerdict.FAIL if offenders else RuleVerdict.PASS
    return RuleResult(rule["id"], "layering", verdict, offenders)


def _check_tests_required(rule: dict[str, Any], records: list[dict[str, Any]]) -> RuleResult:
    packages = _by_kind(records, "package")
    if not packages:
        return RuleResult(
            rule["id"],
            "tests_required",
            RuleVerdict.UNEVALUABLE,
            reason=(
                "no package records in the index -- packages were never extracted, "
                "not proven absent"
            ),
        )
    substring = rule["import_path_contains"]
    tested = {mapping["package"] for mapping in _by_kind(records, "test_mapping")}
    offenders = tuple(
        sorted(
            (
                package
                for package in packages
                if substring in package.get("import_path", "")
                and package["import_path"] not in tested
            ),
            key=lambda package: package["id"],
        )
    )
    verdict = RuleVerdict.FAIL if offenders else RuleVerdict.PASS
    return RuleResult(rule["id"], "tests_required", verdict, offenders)


def _check_decisions_cite_code(rule: dict[str, Any], records: list[dict[str, Any]]) -> RuleResult:
    decisions = _by_kind(records, "decision")
    if not decisions:
        return RuleResult(
            rule["id"],
            "decisions_cite_code",
            RuleVerdict.UNEVALUABLE,
            reason=(
                "no decision records in the index -- an index built before A06 has none, "
                "which is not the same as every decision citing code"
            ),
        )
    offenders = tuple(
        sorted(
            (
                decision
                for decision in decisions
                if not decision.get("cites_code") and not decision.get("exempt")
            ),
            key=lambda decision: decision["id"],
        )
    )
    verdict = RuleVerdict.FAIL if offenders else RuleVerdict.PASS
    return RuleResult(rule["id"], "decisions_cite_code", verdict, offenders)


_CHECKERS = {
    "layering": _check_layering,
    "tests_required": _check_tests_required,
    "decisions_cite_code": _check_decisions_cite_code,
}


def evaluate(rules_payload: dict[str, Any], records: list[dict[str, Any]]) -> list[RuleResult]:
    """Check every rule in ``rules_payload["rules"]`` against ``records``, in the given order.

    Assumes ``rules_payload`` already passed :func:`load_rules`; this raises `KeyError` on a rule
    naming a type this module does not check, rather than silently skipping it.
    """
    return [_CHECKERS[rule["type"]](rule, records) for rule in rules_payload["rules"]]


_SEVERITY_FOR_VERDICT: dict[RuleVerdict, Severity] = {
    RuleVerdict.PASS: Severity.INFO,
    # A FAIL is a contract violation; an UNEVALUABLE rule is a gate that cannot see, which
    # CLAUDE.md's dlv-verify-gate precedent treats as equally serious, not merely informational.
    RuleVerdict.FAIL: Severity.BROKEN,
    RuleVerdict.UNEVALUABLE: Severity.BROKEN,
}

_LIMIT_FOR_TYPE: dict[str, str] = {
    "layering": (
        "Proves only that no statically-extracted import edge crosses the forbidden direction. "
        "Reflection, plugin loading, build tags and NATS subjects produce coupling no import "
        "records (A01 8)."
    ),
    "tests_required": (
        "A test_mapping record counts test files and functions; it does not show a test asserts "
        "anything about the package it sits beside, or that it passes."
    ),
    "decisions_cite_code": (
        "cites_code is true iff the decision produced at least one decision_link -- Q6's own "
        "definition of citing a path. It does not judge whether the cited path still supports "
        "the decision."
    ),
}


def to_findings(results: list[RuleResult], *, snapshot: str) -> list[Finding]:
    """One `Finding` per rule, PASS included.

    A rule that never ran (UNEVALUABLE) is exactly as reportable as one that failed -- suppressing
    the PASS and UNEVALUABLE cases would make the finding set look like a list of only-bad-news,
    which is its own kind of silence. ``snapshot`` is the index's own recorded commit (or
    "unknown" if it describes none) -- CLAUDE.md: a finding without one is an opinion.
    """
    findings = []
    for result in results:
        evidence = tuple(
            Evidence(
                file=(offender.get("provenance") or {}).get("path"),
                line=(offender.get("provenance") or {}).get("line"),
                excerpt=offender["id"],
            )
            for offender in result.offenders
        )
        if result.verdict is RuleVerdict.PASS:
            summary = f"{result.rule_type}: no offenders"
        elif result.verdict is RuleVerdict.FAIL:
            summary = f"{result.rule_type}: {len(result.offenders)} offender(s)"
        else:
            summary = f"{result.rule_type}: unevaluable -- {result.reason}"
        findings.append(
            Finding(
                id=f"arch-rule:{result.rule_id}",
                check=f"arch_rules.{result.rule_type}",
                severity=_SEVERITY_FOR_VERDICT[result.verdict],
                summary=summary,
                snapshot=snapshot,
                evidence=evidence,
                limit=_LIMIT_FOR_TYPE.get(result.rule_type),
                detail=result.reason if result.verdict is RuleVerdict.UNEVALUABLE else None,
            )
        )
    return findings


def render_lines(results: list[RuleResult]) -> list[str]:
    """One line per rule -- the `md`/default CLI format."""
    return [result.line() for result in results]


__all__ = [
    "RULES_SCHEMA_NAME",
    "RULES_SCHEMA_VERSION",
    "RuleResult",
    "RuleVerdict",
    "RulesLoadError",
    "evaluate",
    "load_rules",
    "render_lines",
    "to_findings",
]
