"""G02 — conformance of the captured GoLand run configurations.

Roadmap 1.9 captured eight configurations under `.idea/runConfigurations/` and called them the
*human* half of the same observe-before-claiming rule `dlv-verify-gate` enforces on the model. No
phase since has validated them, so "the human half exists" has never been more than a file count.

**What this proves and what it does not.** Structural validity is not runnability. A configuration
that declares a working directory, a coherent set of flags and a unique port may still fail the
moment it is launched, and nothing here launches anything. The check reads XML.

**Target existence is deliberately NOT checked** (A.5's Stop clause). Whether a configuration's
package or binary exists is a Barracuda-owned verification; the snapshot holds the harness surface
only, so resolving `$PROJECT_DIR$/barracuda/cmd/barracuda` against it would report four
configurations broken purely because the product tree was never captured. The declared target is
reported and its existence marked `unverified`.

**Type awareness is load-bearing.** Three declaration shapes exist for a working directory and
`GoRemoteDebugConfigurationType` has no such field at all. A blanket rule would fault the one real
configuration that lacks one for a field GoLand gives it no slot for — a false positive on the
only interesting case, which is how a check teaches its reader to ignore it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from bearhug.model import Evidence, Finding, Severity

#: Every check this module can emit. A finding whose check is not here is a reporting hole, and a
#: check named here that never fires is the silence CLAUDE.md refuses to read as absence.
RUN_CONFIG_CHECKS: tuple[str, ...] = (
    "RUNCONFIG-PARSE",
    "RUNCONFIG-WORKDIR",
    "RUNCONFIG-TOOL",
    "RUNCONFIG-PORT",
    "RUNCONFIG-PURPOSE",
    "RUNCONFIG-WORKLOAD",
    "RUNCONFIG-TARGET",
)

#: Configuration types that HAVE a working-directory field, by the shape they declare it in.
_ELEMENT_WORKDIR = frozenset({"GoApplicationRunConfiguration", "GoTestRunConfiguration"})
_OPTION_WORKDIR = frozenset({"ShConfigurationType"})

#: Types with no working-directory field in GoLand at all. Absence here is not a defect.
_NO_WORKDIR_FIELD = frozenset({"GoRemoteDebugConfigurationType"})

#: Flags that request a profile. Each needs a defined workload to profile.
_PROFILE_FLAGS = ("-cpuprofile", "-memprofile", "-mutexprofile", "-blockprofile")

#: Flags that define WHAT runs under a profile.
_WORKLOAD_FLAGS = ("-bench", "-run")

#: A `host:port` inside a script body, or an explicit listen flag. Kept narrow on purpose: a
#: loose `:\d+` also matches a timestamp, a slice bound and a `sed` address.
_URL_PORT = re.compile(r"//[\w.\-]+:(\d{2,5})\b")
_LISTEN_PORT = re.compile(r"--(?:listen|port)[= ]:?(\d{2,5})\b")

#: A port asserted in a configuration's own NAME, e.g. `Go Remote (attach dlv :2345)`.
_NAME_PORT = re.compile(r":(\d{2,5})\b")

LIMIT = (
    "Structural validity is not runnability: nothing here launches a configuration, and a "
    "configuration that passes every check may still fail on execution. Target existence is "
    "Barracuda-owned and reported `unverified` — the snapshot captures the harness surface, not "
    "the product tree, so a path check would fault configurations for evidence never captured."
)

LIMIT_PORT = (
    "Ports are read from the `port` attribute and from `host:port`/`--listen=` inside a script "
    "body. A port a configuration reaches through a config FILE (the two production runtimes both "
    "load `deploy/barracuda-prod.yaml`) is invisible here, so a clean port report is a floor: it "
    "proves no collision is DECLARED, not that two configurations can run together."
)

LIMIT_PURPOSE = (
    "The purpose is read from the configuration's NAME. A name is documentation, so a mismatch "
    "may mean the flags are wrong or the name is stale; this says they disagree, not which is "
    "right."
)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """One parsed run configuration."""

    path: str
    name: str
    type: str
    factory: str
    working_directory: str | None = None
    go_parameters: str = ""
    parameters: str = ""
    script_text: str = ""
    ports: tuple[int, ...] = ()
    before_run_tools: tuple[str, ...] = ()
    kind: str | None = None
    package: str | None = None
    parse_error: str | None = None

    @property
    def declares_workdir_field(self) -> bool:
        return self.type not in _NO_WORKDIR_FIELD

    @property
    def all_flags(self) -> str:
        return f"{self.go_parameters} {self.parameters} {self.script_text}"


@dataclass(slots=True)
class _Options:
    values: dict[str, str] = field(default_factory=dict)


def _options(configuration: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for option in configuration.findall("option"):
        name = option.get("name")
        if name is not None:
            out[name] = option.get("value") or ""
    return out


def _child_value(configuration: ET.Element, tag: str) -> str | None:
    element = configuration.find(tag)
    return None if element is None else (element.get("value") or "")


def _ports(configuration: ET.Element, script_text: str, options: dict[str, str]) -> tuple[int, ...]:
    found: list[int] = []
    attribute = configuration.get("port")
    if attribute and attribute.isdigit():
        found.append(int(attribute))
    haystack = " ".join([script_text, options.get("SCRIPT_OPTIONS", "") or ""])
    for pattern in (_URL_PORT, _LISTEN_PORT):
        for match in pattern.finditer(haystack):
            found.append(int(match.group(1)))
    # Stable and de-duplicated: a port declared twice in one configuration is one port.
    return tuple(sorted(set(found)))


def _before_run_tools(configuration: ET.Element) -> tuple[str, ...]:
    """External tools a before-run task references, by tool name.

    GoLand writes these as `actionId="Tool_<toolset>_<tool>"` inside `<method>`. A bare
    `<method v="2" />` declares nothing, which is what all eight captured configurations do.
    """
    tools: list[str] = []
    method = configuration.find("method")
    if method is None:
        return ()
    for option in method.findall("option"):
        action = option.get("actionId") or ""
        if action.startswith("Tool_"):
            tools.append(action.split("_")[-1])
        name = option.get("name") or ""
        if name in {"ToolBeforeRunTask", "RunConfigurationTask"} and not action:
            tools.append(option.get("value") or "unnamed")
    return tuple(tools)


def parse_run_config(path: Path) -> RunConfig:
    """Parse one configuration file. A file that cannot be parsed is reported, never raised."""
    relative = path.name
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return RunConfig(
            path=relative, name=path.stem, type="unknown", factory="unknown",
            parse_error=f"{type(exc).__name__}: {exc}",
        )
    configuration = root.find("configuration")
    if configuration is None:
        return RunConfig(
            path=relative, name=path.stem, type="unknown", factory="unknown",
            parse_error="no <configuration> element",
        )

    options = _options(configuration)
    script_text = options.get("SCRIPT_TEXT", "")
    kind = _child_value(configuration, "kind")
    workdir = _child_value(configuration, "working_directory")
    if workdir is None:
        workdir = options.get("SCRIPT_WORKING_DIRECTORY") or None

    return RunConfig(
        path=relative,
        name=configuration.get("name") or path.stem,
        type=configuration.get("type") or "unknown",
        factory=configuration.get("factoryName") or "unknown",
        working_directory=workdir or None,
        go_parameters=_child_value(configuration, "go_parameters") or "",
        parameters=_child_value(configuration, "parameters") or "",
        script_text=script_text,
        ports=_ports(configuration, script_text, options),
        before_run_tools=_before_run_tools(configuration),
        kind=kind,
        package=_child_value(configuration, "package"),
    )


def parse_run_configs(directory: Path) -> list[RunConfig]:
    """Every `*.xml` under ``directory``, in stable filename order."""
    return [parse_run_config(path) for path in sorted(Path(directory).glob("*.xml"))]


def _defined_external_tools(config_dir: Path) -> set[str]:
    """Tool names defined anywhere under the `.idea/` that holds ``config_dir``.

    None of the captured eight declares a before-run task and the captured `.idea/` has no
    `tools/` directory, so this returns an empty set against the real evidence — which is why the
    check that consumes it is proven against a constructed positive rather than trusted.
    """
    idea = Path(config_dir).parent
    names: set[str] = set()
    for path in sorted(idea.rglob("*.xml")):
        if path.parent.name == "runConfigurations":
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for tool in root.iter("tool"):
            name = tool.get("name")
            if name:
                names.add(name)
    return names


def _purpose_findings(config: RunConfig, *, snapshot: str) -> list[Finding]:
    """Does the name's declared purpose appear in the flags?"""
    findings: list[Finding] = []
    lowered = config.name.lower()
    flags = config.all_flags
    stem = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")

    if "race" in lowered and "-race" not in flags:
        findings.append(
            Finding(
                id=f"runconfig-purpose-race-{stem}",
                check="RUNCONFIG-PURPOSE",
                severity=Severity.BROKEN,
                summary=f"{config.name!r} declares a race purpose and carries no -race flag",
                snapshot=snapshot,
                evidence=(Evidence(file=config.path),),
                detail=(
                    f"type={config.type}; go_parameters={config.go_parameters!r}. The two "
                    f"captured `Production runtime` configurations differ ONLY by "
                    f"`<go_parameters value=\"-race\" />`, so the name is the sole signal a "
                    f"reader has for which one instruments."
                ),
                limit=LIMIT_PURPOSE,
            )
        )

    if "profile" in lowered and not any(flag in flags for flag in _PROFILE_FLAGS):
        findings.append(
            Finding(
                id=f"runconfig-purpose-profile-{stem}",
                check="RUNCONFIG-PURPOSE",
                severity=Severity.BROKEN,
                summary=f"{config.name!r} declares a profile purpose and requests no profile",
                snapshot=snapshot,
                evidence=(Evidence(file=config.path),),
                detail=f"go_parameters={config.go_parameters!r}; none of {_PROFILE_FLAGS}.",
                limit=LIMIT_PURPOSE,
            )
        )

    for match in _NAME_PORT.finditer(config.name):
        asserted = int(match.group(1))
        if config.ports and asserted not in config.ports:
            findings.append(
                Finding(
                    id=f"runconfig-purpose-port-{stem}-{asserted}",
                    check="RUNCONFIG-PURPOSE",
                    severity=Severity.BROKEN,
                    summary=(
                        f"{config.name!r} asserts port {asserted} but declares "
                        f"{', '.join(str(p) for p in config.ports)}"
                    ),
                    snapshot=snapshot,
                    evidence=(Evidence(file=config.path),),
                    detail="The name is what a human reads before attaching a debugger.",
                    limit=LIMIT_PURPOSE,
                )
            )
    return findings


def _workload_findings(config: RunConfig, *, snapshot: str) -> list[Finding]:
    """A profile with no defined workload profiles whatever happens to run.

    This is the one defect class with a real positive in the captured eight: `contention profile
    (mutex+block)` carries `-mutexprofile`/`-blockprofile` and neither `-bench` nor `-run`, while
    its sibling `codec bench` pairs its profile flags with `-bench=... -benchtime=3s`.
    """
    flags = config.all_flags
    requested = [flag for flag in _PROFILE_FLAGS if flag in flags]
    if not requested:
        return []
    if any(flag in flags for flag in _WORKLOAD_FLAGS):
        return []
    stem = re.sub(r"[^a-z0-9]+", "-", config.name.lower()).strip("-")
    return [
        Finding(
            id=f"runconfig-workload-{stem}",
            check="RUNCONFIG-WORKLOAD",
            severity=Severity.COSTLY,
            summary=(
                f"{config.name!r} requests {', '.join(requested)} with no -bench or -run, so it "
                f"profiles whatever the package's tests happen to run"
            ),
            snapshot=snapshot,
            evidence=(Evidence(file=config.path),),
            detail=(
                f"go_parameters={config.go_parameters!r}. A profile without a defined workload "
                f"produces a pprof whose contents depend on the test set, so two runs are not "
                f"comparable and neither is attributable to a change."
            ),
            limit=(
                "Proves no workload is DECLARED in the configuration. The package's own tests may "
                "constitute a deliberate workload; that is a Barracuda judgement, which is why "
                "this is COSTLY rather than BROKEN."
            ),
        )
    ]


def check_run_configs(directory: Path, *, snapshot: str) -> list[Finding]:
    """Every conformance finding over the configurations in ``directory``."""
    directory = Path(directory)
    configs = parse_run_configs(directory)
    defined_tools = _defined_external_tools(directory)
    findings: list[Finding] = []

    for config in configs:
        stem = re.sub(r"[^a-z0-9]+", "-", config.name.lower()).strip("-") or config.path

        if config.parse_error:
            findings.append(
                Finding(
                    id=f"runconfig-parse-{config.path}",
                    check="RUNCONFIG-PARSE",
                    severity=Severity.BROKEN,
                    summary=f"{config.path} could not be parsed as a run configuration",
                    snapshot=snapshot,
                    evidence=(Evidence(file=config.path),),
                    detail=config.parse_error,
                    limit=(
                        "A file bear-hug cannot read is reported rather than skipped: a parser "
                        "that drops what it cannot handle reports a clean sweep over evidence it "
                        "never examined."
                    ),
                )
            )
            continue

        # --- (a) a missing working directory, by declaration shape -------------------------
        if config.working_directory is None:
            if config.type in _NO_WORKDIR_FIELD:
                findings.append(
                    Finding(
                        id=f"runconfig-workdir-na-{stem}",
                        check="RUNCONFIG-WORKDIR",
                        severity=Severity.INFO,
                        summary=(
                            f"{config.name!r} declares no working directory, and its type "
                            f"({config.type}) has no field for one"
                        ),
                        snapshot=snapshot,
                        evidence=(Evidence(file=config.path),),
                        detail=(
                            "Recorded rather than faulted. What IS undeclared is the attach "
                            "source: everything needed to satisfy this configuration's name "
                            "(a headless dlv bound to its port) lives outside the file, so "
                            "nothing in the configuration states how the listener starts."
                        ),
                        limit=LIMIT,
                    )
                )
            elif config.type in _ELEMENT_WORKDIR or config.type in _OPTION_WORKDIR:
                findings.append(
                    Finding(
                        id=f"runconfig-workdir-{stem}",
                        check="RUNCONFIG-WORKDIR",
                        severity=Severity.BROKEN,
                        summary=(
                            f"{config.name!r} ({config.type}) declares no working directory"
                        ),
                        snapshot=snapshot,
                        evidence=(Evidence(file=config.path),),
                        detail=(
                            "A Go run/test configuration with no working directory resolves "
                            "relative paths against whatever GoLand last used, so the same "
                            "configuration behaves differently between machines and after an "
                            "IDE restart."
                        ),
                        limit=LIMIT,
                    )
                )

        # --- (b) an absent external tool ---------------------------------------------------
        for tool in config.before_run_tools:
            if tool in defined_tools:
                continue
            findings.append(
                Finding(
                    id=f"runconfig-tool-{stem}-{tool}",
                    check="RUNCONFIG-TOOL",
                    severity=Severity.BROKEN,
                    summary=(
                        f"{config.name!r} runs before-run tool {tool!r}, which is not defined "
                        f"in this .idea/"
                    ),
                    snapshot=snapshot,
                    evidence=(Evidence(file=config.path),),
                    detail=(
                        f"defined external tools: "
                        f"{', '.join(sorted(defined_tools)) or 'none captured'}. A before-run "
                        f"task naming a tool the IDE cannot resolve fails the launch, and the "
                        f"configuration looks correct in the file."
                    ),
                    limit=LIMIT,
                )
            )

        findings.extend(_purpose_findings(config, snapshot=snapshot))
        findings.extend(_workload_findings(config, snapshot=snapshot))

    # --- (c) a port declared by more than one configuration --------------------------------
    by_port: dict[int, list[RunConfig]] = defaultdict(list)
    for config in configs:
        for port in config.ports:
            by_port[port].append(config)
    for port, sharing in sorted(by_port.items()):
        if len(sharing) < 2:
            continue
        findings.append(
            Finding(
                id=f"runconfig-port-{port}",
                check="RUNCONFIG-PORT",
                severity=Severity.BROKEN,
                summary=(
                    f"port {port} is declared by {len(sharing)} configurations: "
                    f"{', '.join(repr(c.name) for c in sharing)}"
                ),
                snapshot=snapshot,
                evidence=tuple(Evidence(file=c.path) for c in sharing),
                detail=(
                    "Two configurations bound to one port cannot run together; the second to "
                    "start fails to bind, and a debugger attach silently reaches the wrong "
                    "process when the first is already listening."
                ),
                limit=LIMIT_PORT,
            )
        )

    # --- the declared targets, existence unverified ----------------------------------------
    targets = [(c.name, c.package or c.kind or "—") for c in configs if not c.parse_error]
    if targets:
        findings.append(
            Finding(
                id="runconfig-targets",
                check="RUNCONFIG-TARGET",
                severity=Severity.INFO,
                summary=(
                    f"{len(targets)} configurations declare a target; existence is unverified"
                ),
                snapshot=snapshot,
                evidence=(Evidence(file=str(directory.name)),),
                detail="; ".join(f"{name} -> {target}" for name, target in targets),
                limit=(
                    "Existence is Barracuda-owned (A.5). The snapshot captures the harness "
                    "surface, not the product tree, so resolving these would report four "
                    "configurations broken purely because their packages were never captured. "
                    "`unverified` is the honest state, not `absent`."
                ),
            )
        )
    return findings


def render_run_configs(configs: list[RunConfig]) -> str:
    """A table of what was parsed, for the CLI."""
    lines = [
        "| configuration | type | kind | working dir | ports | before-run | flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for config in sorted(configs, key=lambda c: c.name):
        flags = (config.go_parameters or config.parameters or "—").strip() or "—"
        lines.append(
            f"| {config.name} | {config.type.replace('Configuration', '')} | "
            f"{config.kind or '—'} | {config.working_directory or 'NONE'} | "
            f"{', '.join(str(p) for p in config.ports) or '—'} | "
            f"{', '.join(config.before_run_tools) or '—'} | {flags} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "RUN_CONFIG_CHECKS",
    "RunConfig",
    "check_run_configs",
    "parse_run_config",
    "parse_run_configs",
    "render_run_configs",
]
