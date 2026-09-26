"""2.6 GATE-COVERAGE — is every mechanism that acts on a turn named in the golden master?

Bidirectional, because the two directions fail differently. A gate that blocks a turn and is
named nowhere in CLAUDE.md governs the model through a rule it was never told; a script
CLAUDE.md names that does not exist is a rule pointing at nothing.

The resolver is the load-bearing part and is tested first. Every hook command in the subject is
shell-quoted (`"$CLAUDE_PROJECT_DIR"/scripts/hooks/x.py`), so a naive whitespace split resolves
none of them — and a classifier reading no source returns False for every hook, which reads
exactly like "no gate blocks". That is the silence-is-not-absence failure, and it happened here
before the resolver was written properly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bearhug.model import Evidence, Finding, HookSpec, Severity
from bearhug.snapshot.spec import in_capture_scope

#: `$CLAUDE_PROJECT_DIR`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_PROJECT_DIR:-.}` — all mean "here".
PROJECT_DIR_VAR = re.compile(r"\$\{?CLAUDE_PROJECT_DIR(?::-[^}]*)?\}?")

SCRIPT_SUFFIXES = (".py", ".sh", ".cjs", ".js")

#: A hook blocks by emitting a decision key, or by exiting 2. Exit 1 does NOT block.
BLOCKS = re.compile(r'"block"|permissionDecision|sys\.exit\(2\)|exit 2\b')
INJECTS = re.compile(r"additionalContext")

LIMIT_FORWARD = (
    "Proves the script's name does not appear in CLAUDE.md at this snapshot. Does not prove "
    "the behaviour is undocumented — a rule may describe the gate without naming its file. "
    "It does mean a reader cannot get from the rule to the mechanism."
)

#: The limit that matters most, because it governs how a GREEN from this check must be read.
#: Three times in the corpus a gate was named, present, and blocking by source, and still did
#: not act: dlv-verify-gate.py matched Edit|Write and so saw 0 of 132 .go writes that went
#: through Bash; joinkey-lint.py exited 1 instead of emitting a decision and blocked nothing at
#: every Stop for weeks; response-shape.py's stamp raised NameError into its own bare except,
#: so the gate ran, passed, and was permanently invisible. Coverage is necessary, not sufficient.
LIMIT_COVERAGE_IS_NOT_EFFECT = (
    "A gate absent from this list is COVERED, never EFFECTIVE. Coverage is a property of the "
    "prose, not of the mechanism: it does not show the matcher can see the path the behaviour "
    "takes, that the exit contract blocks, or that the gate is observable when it fires. "
    "Phase 3 (contract, inertness) and Phase 4 (did it change behaviour) test those; a green "
    "here means only that a reader can get from the rule to the file."
)
LIMIT_REVERSE = (
    "Proves CLAUDE.md cites a path that is inside the capture spec and absent from the "
    "snapshot. Does not prove the rule is wrong — the script may have moved rather than been "
    "deleted."
)
LIMIT_OUT_OF_SCOPE = (
    "The cited path lies OUTSIDE what the snapshot captures, so this snapshot cannot say "
    "whether it exists. This is a gap in bear-hug's capture spec, not a defect in CLAUDE.md. "
    "Reported rather than skipped because the first version of this check called exactly such "
    "a path a dead reference, and it was on disk the whole time."
)
LIMIT_CLASSIFY = (
    "Blocking and injecting are read from the script's source text, not from running it. A gate "
    "that blocks through a helper module is not seen here."
)


def resolve_script(command: str) -> str | None:
    """The repo-relative script a hook command runs, or None if it runs something external."""
    cleaned = PROJECT_DIR_VAR.sub("", command.replace('"', " ").replace("'", " "))
    for token in cleaned.split():
        if token.endswith(SCRIPT_SUFFIXES):
            return token.lstrip("/")
    return None


def hook_arguments(command: str) -> list[str]:
    """The arguments a hook is configured with, after its script path.

    `joinkey-lint.py --check` blocks; `joinkey-lint.py` alone only reports. `memex-hook.sh`
    dispatches entirely on a subcommand. Running a gate without its configured arguments tests
    a different gate, so Phase 3 replays the whole invocation.
    """
    cleaned = PROJECT_DIR_VAR.sub("", command.replace('"', " ").replace("'", " "))
    tokens = cleaned.split()
    for index, token in enumerate(tokens):
        if token.endswith(SCRIPT_SUFFIXES):
            return [t for t in tokens[index + 1:] if not t.startswith("$")]
    return []


def parse_hooks(settings: dict[str, Any]) -> list[HookSpec]:
    """Every hook command declared in a settings.json, in declaration order."""
    specs: list[HookSpec] = []
    for event, matchers in (settings.get("hooks") or {}).items():
        for group_index, matcher in enumerate(matchers):
            for position, hook in enumerate(matcher.get("hooks", [])):
                command = hook.get("command", "")
                specs.append(
                    HookSpec(
                        event=event,
                        matcher=matcher.get("matcher"),
                        command=command,
                        timeout=hook.get("timeout"),
                        # `asyncRewake` exempts a long-running background gate from the
                        # implausible-timeout finding (deep-check.py, 600s). Never read before
                        # this, so `spec.async_rewake` was always False and the exemption at
                        # audit.py's LATENCY check was dead code — every hook, exempt or not,
                        # took the same branch.
                        async_rewake=bool(hook.get("asyncRewake", False)),
                        status_message=hook.get("statusMessage"),
                        group_index=group_index,
                        position_in_group=position,
                    )
                )
    return specs


def classify_hook(script: Path) -> dict[str, bool]:
    """Does this script block a turn, or inject context? Read from source, never executed."""
    try:
        source = Path(script).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"blocks": False, "injects": False, "readable": False}
    return {
        "blocks": bool(BLOCKS.search(source)),
        "injects": bool(INJECTS.search(source)),
        "readable": True,
    }


def _named_in(claude_md: str, script: str) -> bool:
    base = script.split("/")[-1]
    return base in claude_md or base.rsplit(".", 1)[0] in claude_md


def check_gate_coverage(
    *, settings: dict[str, Any], claude_md: str, project_root: Path, snapshot: str
) -> list[Finding]:
    """Both directions of the gate/prose correspondence.

    Read :data:`LIMIT_COVERAGE_IS_NOT_EFFECT` before treating an empty result as good news.
    """
    project_root = Path(project_root)
    findings: list[Finding] = []
    seen: set[str] = set()

    for spec in parse_hooks(settings):
        script = spec.script and resolve_script(spec.command)
        if not script or script in seen:
            continue
        seen.add(script)
        verdict = classify_hook(project_root / script)
        if not verdict["readable"]:
            findings.append(
                Finding(
                    id=f"gate-unreadable-{Path(script).name}",
                    check="GATE-COVERAGE-FORWARD",
                    severity=Severity.BROKEN,
                    summary=f"{Path(script).name} is wired as a hook but is not in the snapshot",
                    snapshot=snapshot,
                    evidence=(Evidence(file=".claude/settings.json", excerpt=spec.command),),
                    detail=f"{Path(script).name} declared on {spec.event}",
                    limit=LIMIT_CLASSIFY,
                )
            )
            continue
        if not (verdict["blocks"] or verdict["injects"]):
            continue
        if _named_in(claude_md, script):
            continue
        acts = "blocks the turn" if verdict["blocks"] else "injects context"
        findings.append(
            Finding(
                id=f"gate-uncovered-{Path(script).name}",
                check="GATE-COVERAGE-FORWARD",
                severity=Severity.COSTLY,
                summary=f"{Path(script).name} {acts} and is named nowhere in CLAUDE.md",
                snapshot=snapshot,
                evidence=(Evidence(file=script, excerpt=f"{spec.event} · {spec.matcher or '*'}"),),
                detail=f"{Path(script).name} on {spec.event} — {acts}",
                limit=f"{LIMIT_FORWARD} {LIMIT_COVERAGE_IS_NOT_EFFECT}",
            )
        )

    for match in re.finditer(r"`((?:scripts|\.claude|\.githooks)/[\w./-]+)`", claude_md):
        cited = match.group(1).rstrip("/")
        if cited in seen or (project_root / cited).exists():
            continue
        line = claude_md[: match.start()].count("\n") + 1
        in_scope = in_capture_scope(cited)
        findings.append(
            Finding(
                id=("gate-deadref-" if in_scope else "gate-unresolvable-") + str(line),
                check="GATE-COVERAGE-REVERSE",
                severity=Severity.BROKEN if in_scope else Severity.INFO,
                summary=(
                    f"CLAUDE.md cites {cited}, which does not exist in this snapshot"
                    if in_scope
                    else f"CLAUDE.md cites {cited}, which this snapshot does not capture"
                ),
                snapshot=snapshot,
                evidence=(Evidence(file="CLAUDE.md", line=line, excerpt=cited),),
                detail=f"{cited} cited; in capture scope: {in_scope}",
                limit=LIMIT_REVERSE if in_scope else LIMIT_OUT_OF_SCOPE,
            )
        )
    return findings
