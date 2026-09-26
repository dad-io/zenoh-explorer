"""One explicit prepared locator drives sequential native work and durable recovery.

The run index stores scheduling and operator actions. Capsule journals, provider custody,
validation receipts and acceptance proofs retain their existing authority. No subject promotion
is performed by this controller.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from bearhug.campaign.capsule_runtime import (
    CapsuleRuntime,
    _canonical,
    _create_only,
    _private_directory,
    _read_json,
    bind_author_observations,
)
from bearhug.campaign.capsule_storage import CapsuleObjectStore
from bearhug.campaign.capsules import validate_capsule_plan, validate_intent_envelope
from bearhug.campaign.launcher import (
    CampaignLauncherError,
    CampaignLauncherOrphanedError,
    derive_campaign_worktree_target,
    provision_campaign_worktree,
)
from bearhug.campaign.leases import CampaignLeaseConflict, CampaignLeaseStore
from bearhug.campaign.materialize import (
    LeaseMaterializationError,
    materialize_declared_paths,
)
from bearhug.campaign.review import canonical_json_sha256, worktree_sha256
from bearhug.providers.claude import validation_programs
from bearhug.providers.custody import ProviderCustodyStore
from bearhug.providers.receipt import capture_launch_repository

# What an author may contribute to a reconciliation record. Every other key in the record is
# supplied by the campaign, never by the provider.
AUTHOR_OBSERVATION_FIELDS = frozenset({"bindings", "observed_surface"})

# Ceiling on one provider call when the campaign declares no deadline, and the cap
# applied to whatever a declared deadline leaves. Deliberately generous: see the
# operating limits note in README.md.
MAX_PROVIDER_CALL_SECONDS = 8 * 60 * 60.0


class CapsuleCampaignError(ValueError):
    """The selected run cannot safely advance from its recorded evidence."""


class CapsuleCampaignBusy(CapsuleCampaignError):
    """An operation already owns this exact run locator."""


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _blob(root, digest):
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise CapsuleCampaignError("invalid durable evidence reference")
    path = Path(root) / f"{digest}.bin"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise CapsuleCampaignError("durable evidence bytes are unavailable")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise CapsuleCampaignError("durable evidence bytes changed")
    return raw


def read_capsule_evidence(root: Path, capsule_id: str):
    """Read completion evidence without reviving a released mutation lease."""
    physical = root / _key(capsule_id)
    states = physical / "states"
    if not states.exists():
        return None
    previous = "0" * 64
    latest = None
    for ordinal, path in enumerate(sorted(states.iterdir())):
        if path.is_symlink() or not path.is_file():
            raise CapsuleCampaignError("capsule state custody is not physical")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if path.name != f"{ordinal:020d}-{digest}.json":
            raise CapsuleCampaignError("capsule state sequence or digest changed")
        latest = _read_json(path)
        if latest.get("sequence") != ordinal or latest.get("previous_state_sha256") != previous:
            raise CapsuleCampaignError("capsule state chain changed")
        previous = digest
    if latest is None or latest.get("capsule_id") != capsule_id:
        raise CapsuleCampaignError("capsule state identity is missing or mismatched")
    objects = CapsuleObjectStore(physical / "objects")
    plan = objects.get(latest["plan_sha256"], record_kind="capsule_plan")
    plan, intent = objects.resolve_plan(
        latest["plan_sha256"],
        expected_subject=plan["subject"],
        expected_revision_id=latest["revision_id"],
    )
    if validate_intent_envelope(intent).digest != latest["intent_envelope_sha256"]:
        raise CapsuleCampaignError("capsule state intent binding changed")
    journal = objects.get(latest["journal_sha256"], record_kind="capsule_journal")
    result = (
        objects.get(latest["result_sha256"], record_kind="capsule_result")
        if latest.get("result_sha256")
        else None
    )
    grounding = (
        json.loads(_blob(physical / "blobs", latest["grounding_sha256"]))
        if latest.get("grounding_sha256")
        else None
    )
    return {
        "root": physical,
        "state": latest,
        "intent": intent,
        "plan": plan,
        "journal": journal,
        "result": result,
        "grounding": grounding,
        "reconciliation": latest.get("reconciliation"),
        "hil_request": latest.get("hil_request"),
    }


class CapsuleCampaign:
    """A small local scheduler; every spend delegates to an existing custody boundary."""

    def __init__(self, prepared, *, provider_runner=None, clock=time.time, read_only=False):
        self.prepared = prepared
        self.record = prepared.record
        self.root = prepared.root
        self.intent, self.plan = prepared.intent, prepared.plan
        self._execution_config_sha256 = canonical_json_sha256(self.record["config"])
        self.runner, self.clock = provider_runner, clock
        self.paths = {key: Path(value) for key, value in self.record["paths"].items()}
        self.read_only = bool(read_only)
        self.capsule_root = _private_directory(self.root / "capsules", create=not read_only)
        self.states = _private_directory(self.root / "run-states", create=not read_only)
        self.state = self._load()
        if self.state and self.state.get("active_plan_sha256"):
            self._load_active_plan()

    def _load_active_plan(self):
        store = CapsuleObjectStore(self.root / "active-plans")
        plan = store.get(self.state["active_plan_sha256"], record_kind="capsule_plan")
        self.plan, intent = store.resolve_plan(
            self.state["active_plan_sha256"],
            expected_subject=self.prepared.plan["subject"],
            expected_revision_id=plan["revision"]["revision_id"],
        )
        if validate_intent_envelope(intent).digest != validate_intent_envelope(self.intent).digest:
            raise CapsuleCampaignError("active plan changed prepared semantic authority")

    def _load(self):
        previous = "0" * 64
        latest = None
        for ordinal, path in enumerate(sorted(self.states.iterdir())):
            if path.is_symlink() or not path.is_file():
                raise CapsuleCampaignError("run state custody is not physical")
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            value = _read_json(path)
            if (
                path.name != f"{ordinal:020d}-{digest}.json"
                or value.get("sequence") != ordinal
                or value.get("previous_sha256") != previous
                or value.get("prepared_sha256") != self.record["content_sha256"]
            ):
                raise CapsuleCampaignError("run locator state chain or authority changed")
            previous, latest = digest, value
        self.state_digest = previous
        return latest

    def _save(self, **changes):
        value = (
            copy.deepcopy(self.state)
            if self.state
            else {
                "record_kind": "capsule_campaign_state",
                "prepared_sha256": self.record["content_sha256"],
                "status": "prepared",
                "reason": "explicit prepared authority is ready",
                "started_at": None,
                "first_action_at": None,
                "active_capsule_id": None,
                "launching_capsule": None,
                "launches": {},
                "accepted": {},
                "phase": None,
                "stopped": False,
                "operator_actions": [{"action": "prepare", "at": self.record.get("created_at")}],
            }
        )
        value.update(changes)
        value["sequence"] = 0 if self.state is None else self.state["sequence"] + 1
        value["previous_sha256"] = self.state_digest
        raw = _canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        _create_only(self.states / f"{value['sequence']:020d}-{digest}.json", value)
        self.state, self.state_digest = value, digest

    @contextmanager
    def lock(self):
        path = self.root / "campaign.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CapsuleCampaignBusy("this run already has an active operation") from exc
            self.state = self._load()
            if self.state and self.state.get("active_plan_sha256"):
                self._load_active_plan()
            if self.state is None:
                self._save()
            yield

    def action(self, name):
        self._save(
            operator_actions=[*self.state["operator_actions"], {"action": name, "at": self.clock()}]
        )

    @property
    def deadline(self):
        maximum = self.intent["campaign_envelope"]["budget"]["max_seconds"]
        return (
            None
            if maximum is None or self.state["started_at"] is None
            else self.state["started_at"] + maximum
        )

    def remaining(self):
        budget = self.intent["campaign_envelope"]["budget"]
        if any(
            budget[key] is not None for key in ("max_provider_tokens", "max_provider_spend_cents")
        ):
            raise CapsuleCampaignError("finite provider token/spend budget cannot be measured")
        seconds = (
            MAX_PROVIDER_CALL_SECONDS
            if self.deadline is None
            else min(MAX_PROVIDER_CALL_SECONDS, self.deadline - self.clock())
        )
        if seconds <= 0:
            raise CapsuleCampaignError("approved cumulative campaign time budget is exhausted")
        return seconds

    def leases(self, *, writable: bool = False):
        """The campaign's own lease store.

        ``writable=True`` is the one deliberate exception to this campaign's own read_only
        intent: the documented orphan-resolution route (`recover --resolve-orphan`) must be
        able to release a lease through a campaign opened read-only for every other purpose.
        Every other caller keeps today's behaviour unchanged.
        """
        return CampaignLeaseStore(
            self.paths["lease_root"],
            subject_root=self.record["subject"].get(
                "lease_subject_root", self.record["subject"]["path"]
            ),
            repository_common_dir_sha256=self.record["subject"]["repository_common_dir_sha256"],
            read_only=False if writable else self.read_only,
        )

    def _fresh_subject(self):
        subject = self.record["subject"]
        actual = capture_launch_repository(Path(subject["path"]))
        if (actual.head_oid, actual.tree_oid, actual.repository_common_dir_sha256) != (
            subject["head_oid"],
            subject["tree_oid"],
            subject["repository_common_dir_sha256"],
        ):
            raise CapsuleCampaignError(
                "explicit subject changed since prepare; reconcile its authority"
            )

    def _dependency_base(self, capsule):
        from bearhug.campaign.capsule_candidate import candidate_sha256, compose_dependency_base

        declared = set(capsule.get("depends_on", ()))
        if not declared:
            return None
        bundles = self._bundles(complete=False)
        accepted = {row["capsule"]["capsule_id"]: row for row in bundles}
        if not declared <= set(accepted):
            raise CapsuleCampaignError("declared predecessor acceptance is unavailable")
        predecessors = []
        for cid in sorted(declared):
            candidate = accepted[cid]["packet"]["candidate"]
            predecessors.append(
                {
                    "capsule_id": cid,
                    "candidate": candidate,
                    "candidate_sha256": candidate_sha256(candidate),
                }
            )
        return compose_dependency_base(
            self.record["subject"]["path"],
            original_base_oid=self.plan["subject"]["base_oid"],
            repository_common_dir_sha256=self.record["subject"]["repository_common_dir_sha256"],
            predecessors=predecessors,
        )

    def _launch(self, capsule):
        cid = capsule["capsule_id"]
        dependency_base = self._dependency_base(capsule)
        if cid in self.state["launches"]:
            launch = self.state["launches"][cid]
            if launch.get("dependency_base") != dependency_base:
                raise CapsuleCampaignError("launched dependency base differs from accepted custody")
            return launch
        if self.state["launching_capsule"] is not None:
            raise CapsuleCampaignError(
                "worktree launch was interrupted; recover this exact run before retry"
            )
        self._fresh_subject()
        self.remaining()
        self._save(launching_capsule=cid, active_capsule_id=cid)
        suffix = _key(cid)[:20]
        try:
            result = provision_campaign_worktree(
                subject=self.record["subject"]["path"],
                common_dir=self.record["subject"]["common_dir"],
                state_root=self.root,
                worktree_parent=self.paths["worktree_parent"],
                lease_store=self.leases(),
                lease_subject_root=self.record["subject"].get("lease_subject_root"),
                campaign_id=self.record["campaign_id"],
                run_id=self.record["run_id"],
                controller_authority_sha256=self.record["content_sha256"],
                work_unit_id=cid,
                session_id=f"session.{suffix}",
                claimant_id=f"claimant.{suffix}",
                subject_head_oid=self.record["subject"]["head_oid"],
                base_oid=dependency_base["base_oid"]
                if dependency_base
                else self.plan["subject"]["base_oid"],
                **(
                    {
                        "dependency_base": dependency_base,
                        "subject_base_oid": self.plan["subject"]["base_oid"],
                    }
                    if dependency_base
                    else {}
                ),
                claim_set=capsule["mutation_envelope"],
                ttl_seconds=3600.0,
            )
        except CampaignLeaseConflict:
            # Acquisition rejected before any product mutation: there is no crash to recover.
            self._save(
                launching_capsule=None,
                status="blocked",
                reason="mutation claim is held by another run",
            )
            raise
        except CampaignLauncherOrphanedError:
            # Custody may survive the failure, so the launch marker must stand: only
            # `_recover_launch` may clear it, and only against a complete registration.
            raise
        except CampaignLauncherError as exc:
            # Provisioning proved its own rollback before raising this class, so no worktree,
            # branch, lease or registration survives. Leaving `launching_capsule` set here
            # would report custody that does not exist, and every later stop, continue,
            # onboard and setup would refuse against it forever.
            self._save(
                launching_capsule=None,
                status="blocked",
                reason=f"worktree provisioning failed before any custody was created: {exc}",
            )
            raise
        # Only now that provisioning has proved the lease clean and custody is held: carry
        # the declared Git-ignored evidence across. Every declared path is ignored in the
        # subject, so the worktree stays clean and the launcher's invariant is untouched.
        try:
            materialized = materialize_declared_paths(
                subject=self.record["subject"]["path"],
                destination=result.worktree,
                declared=self.record["config"].get("lease_materialized_paths") or (),
            )
        except LeaseMaterializationError as exc:
            self._save(
                launching_capsule=None,
                status="blocked",
                reason=f"declared lease evidence could not be materialized: {exc}",
            )
            raise CapsuleCampaignError(
                f"declared lease evidence could not be materialized: {exc}"
            ) from exc
        launch = {
            "lease_id": result.lease_identity.lease_id,
            "lease_identity": asdict(result.lease_identity),
            "worktree": str(result.worktree),
            "branch": result.branch,
            "claimant_id": result.lease_identity.claimant_id,
            "registration_path": str(result.registration_path),
            "dependency_base": dependency_base,
            "materialized": [asdict(row) for row in materialized],
        }
        self._save(launching_capsule=None, launches={**self.state["launches"], cid: launch})
        return launch

    def _grounding_sources(self, launch):
        """Materialize sealed grounding rows from the leased worktree as P1-P3 packet sources."""
        from bearhug.campaign.grounding import runtime_sources

        rows = self.record["config"].get("grounding_sources", [])
        if not rows:
            return []
        return runtime_sources(rows, worktree=Path(launch["worktree"]))

    def _guidance(self, capsule):
        refs = [
            ref for profile in capsule["validation_profiles"] for ref in profile["command_refs"]
        ]
        value = {
            "instruction": "Implement and commit the whole capsule. Commit before you finish: "
            "uncommitted work is discarded, however correct it is. Only plain git add <paths> "
            "and git commit -m <message>, run from the working directory alone or with &&, "
            "are approved; do not use git -C, --git-dir, --work-tree, or any other git "
            "subcommand. Anything else is denied and the work is discarded. Return only one "
            "JSON object. "
            "This episode is headless and has no next turn: when it ends, anything still "
            "running is killed. Never end it waiting on a background task or a pending "
            "command -- wait for the result inside the episode, or stop and return "
            "outcome continue_with_evidence so another episode carries the work. "
            "The harness runs the validation commands itself after this episode ends, so you "
            "need not run them to prove the work; run one only when you need the signal to "
            "decide what to do next, and wait for it rather than backgrounding it. "
            "Use another episode for local work. Preserve approved meaning. Cite "
            "approved source IDs. Leave evidence_refs empty; the harness attaches provider "
            "receipt, actual Git and command evidence. "
            "Bindings and surface assessments need independent review; they are observations.",
            # The episode runs with the project's CLAUDE.md, hooks and skills disabled, so it
            # cannot learn the toolchain's rules from the project. Anything not listed here
            # needs an approval no headless session can give and fails on the spot: measured
            # on row 240 T1, where turns went to `go version` before the capsule found the
            # governed wrapper the sealed commands already named.
            "executable_programs": {
                "allowed": list(
                    validation_programs(
                        tuple(self.record["config"]["validation_commands"][ref]) for ref in refs
                    )
                ),
                "rule": (
                    "These are the only programs this episode may run through the shell. "
                    "Every other shell command is refused without asking anyone -- not just "
                    "one that builds or runs something, but ls, cat and grep too, and a bare "
                    "toolchain binary when the project supplies a governed wrapper for it. "
                    "git add <paths> and git commit -m <message> are approved above but are "
                    "not validation-command programs, so neither is listed here. "
                    "Inspect files with your own file-reading and search tools rather than "
                    "the shell, and do not probe for alternatives. A refusal does not change "
                    "what you return: report it inside the JSON object, never as prose "
                    "instead of it."
                ),
            },
            "validation_commands": {
                ref: self.record["config"]["validation_commands"][ref] for ref in refs
            },
            "response_contract": {
                "outcome": (
                    "continue_with_evidence|candidate_ready|local_repair_required|"
                    "reconciliation_required|hil_required|blocked|failed"
                ),
                "changed_facts": ["bounded material changes"],
                "discoveries": [],
                "unresolved_decisions": [],
                "next_action": "one concrete next action",
                "proposed_plan_revision": (
                    "optional exact successor plan for affected future work; authority checks apply"
                ),
                "obligation_coverage": [
                    {
                        "source_id": row["source_id"],
                        "obligation_id": row["obligation_id"],
                        "status": "pass|fail|unavailable",
                        "evidence_refs": [],
                    }
                    for row in capsule["obligation_coverage"]
                ],
                "invariants": [
                    {
                        "invariant_id": key,
                        "status": "pass|fail|unavailable",
                        "evidence_refs": [],
                    }
                    for key in capsule["invariant_refs"]
                ],
                "reconciliation_observations": {
                    "bindings": {
                        row["binding_id"]: {
                            "status": "match|changed|conflict|unavailable",
                            "evidence_refs": [],
                        }
                        for row in self.intent["bindings"]
                    },
                    "observed_surface": capsule["expected_surface"],
                },
            },
        }
        return {
            "source_id": "execution.response-contract",
            "content": _canonical(value),
            "kind": "observation",
            "tier": "p0",
            "reason": "prepared command authority and required episode return protocol",
        }

    def runtime(self, capsule, *, recovery=False, release_only=False):
        qualified = None
        if not recovery:
            # Qualification is a precondition to acquiring worktree/lease custody.  A stale
            # provider must fail before _launch can mutate Git or reserve a lease.
            qualified = self.prepared.qualification_index.require(
                self.prepared.provider_policy.roles["author"].provider
            )
        elif capsule["capsule_id"] not in self.state["launches"]:
            raise CapsuleCampaignError("recovery requires an existing capsule launch")
        launch = self._launch(capsule)
        store = self.leases()
        historical = read_capsule_evidence(self.capsule_root, capsule["capsule_id"])
        execution_plan = historical["plan"] if historical else self.plan
        return CapsuleRuntime(
            intent_envelope=self.intent,
            capsule_plan=execution_plan,
            capsule=capsule,
            worktree=launch["worktree"],
            state_root=self.capsule_root,
            lease=store.get(launch["lease_id"]),
            lease_store=store,
            lease_ttl_seconds=3600.0,
            policy_sha256=canonical_json_sha256(self.record["provider_policy"]),
            source_contents=[
                *self.prepared.source_contents,
                *self._grounding_sources(launch),
                self._guidance(capsule),
            ],
            provider_runner=self.runner,
            # The programs these name are pre-authorized for the episode: under --safe-mode
            # nothing that compiles or executes is permitted otherwise, and a capsule whose
            # obligation is to run a check could never run it.
            validation_commands=tuple(
                tuple(self.record["config"]["validation_commands"][ref])
                for profile in capsule["validation_profiles"]
                for ref in profile["command_refs"]
                if ref in self.record["config"]["validation_commands"]
            ),
            provider_policy=self.prepared.provider_policy,
            qualification_index=self.prepared.qualification_index,
            campaign_root=self.paths["campaign_root"],
            campaign_id=self.record["campaign_id"],
            claimant_id=launch["claimant_id"],
            branch=launch["branch"],
            compatibility_repository_root=(
                qualified.bundle_root if qualified is not None else None
            ),
            provider_output_root=self.paths["provider_output_root"],
            recovery=recovery,
            release_only=release_only,
            dependency_base=launch.get("dependency_base"),
            campaign_deadline=self.deadline,
            clock=self.clock,
        )

    def _preflight_findings(self, capsule):
        """Select only the original prepared findings, with unchanged sealed config custody."""
        from bearhug.campaign.prepared import (
            _MAX_JSON_BYTES,
            PreparedCampaign,
            _absolute_file,
            _read_regular,
            _record_digest,
            validate_preflight_findings,
        )

        config = self.record["config"]
        if isinstance(self.prepared, PreparedCampaign) and (
            _record_digest(self.record) != self.record["content_sha256"]
        ):
            raise CapsuleCampaignError("prepared finding/config authority digest changed")
        if canonical_json_sha256(config) != self._execution_config_sha256:
            raise CapsuleCampaignError(
                "prepared execution config changed before semantic preflight"
            )
        findings = validate_preflight_findings(
            config.get("preflight_findings", []), intent=self.prepared.intent,
            plan=self.prepared.plan,
        )
        if not findings:
            return None
        source = self.record.get("inputs", {}).get("execution_config_path")
        if source is not None:
            path = _absolute_file(Path(source), label="sealed preflight config")
            raw = _read_regular(path, maximum=_MAX_JSON_BYTES)
            if hashlib.sha256(raw).hexdigest() != self.record["digests"]["execution_config_sha256"]:
                raise CapsuleCampaignError("sealed preflight execution config bytes changed")
        return next((row for row in findings if row["capsule_id"] == capsule["capsule_id"]), None)

    def _semantic_preflight(self, runtime, findings):
        """Route admitted findings before spend; journal evidence is the atomic consume marker."""
        if findings is None:
            return
        from bearhug.campaign.prepared import _MAX_SOURCE_BYTES, _absolute_file, _read_regular

        refs = {ref for row in findings["discoveries"] for ref in row["evidence_refs"]}
        available = set()
        for source in self.prepared.source_contents:
            content = source.get("content", source.get("text"))
            if isinstance(content, str):
                content = content.encode("utf-8")
            if not isinstance(content, bytes):
                continue
            expected = source.get("content_sha256", source.get("source_sha256"))
            observed = hashlib.sha256(content).hexdigest()
            if expected is not None and observed != expected:
                raise CapsuleCampaignError("prepared preflight source bytes changed")
            if observed not in refs:
                continue
            if source.get("blob_path"):
                path = _absolute_file(Path(source["blob_path"]), label="preflight source blob")
                if _read_regular(path, maximum=_MAX_SOURCE_BYTES) != content:
                    raise CapsuleCampaignError("prepared preflight source blob changed")
            available.add(observed)
        if refs - available:
            raise CapsuleCampaignError("cited preflight evidence bytes are unavailable in custody")
        marker = {
            "record_kind": "prepared_semantic_preflight",
            "prepared_sha256": self.record["content_sha256"],
            "intent_sha256": validate_intent_envelope(self.prepared.intent).digest,
            "plan_sha256": validate_capsule_plan(self.prepared.plan).digest,
            "findings": findings,
        }
        marker_raw = _canonical(marker)
        marker_sha = hashlib.sha256(marker_raw).hexdigest()
        with runtime._candidate_operation():
            # Historical snapshots retain their original journals after plan revision. The
            # runtime constructor has already validated their contiguous content-addressed chain.
            for _, _, path in runtime._state_paths():
                state = _read_json(path)
                journal = runtime.objects.get(
                    state["journal_sha256"], record_kind="capsule_journal",
                )
                if any(
                    entry["event_kind"] in {"reconciliation_completed", "hil_requested"}
                    and marker_sha in entry["evidence_refs"]
                    for entry in journal["entries"]
                ):
                    if _blob(runtime._blobs, marker_sha) != marker_raw:
                        raise CapsuleCampaignError("durable preflight marker changed")
                    return
            if runtime._state["episode_count"] or runtime._state.get("active_episode"):
                raise CapsuleCampaignError(
                    "prepared semantic findings were not routed before spend"
                )
            runtime._put_blob(marker_raw)
            runtime.reconcile(
                discoveries=findings["discoveries"],
                evidence_refs=sorted(refs | {marker_sha}),
                actor="prepared.preflight",
            )

    def _validated_reconciliation(self, runtime):
        from bearhug.campaign.capsule_review import _durable_final_output

        receipt = runtime._state["provider_receipts"][-1]
        final = _durable_final_output(runtime.custody, receipt)
        receipt_sha = canonical_json_sha256(receipt)
        final = bind_author_observations(final, receipt_sha)
        author_state = bind_author_observations(runtime._state, receipt_sha)
        observations = copy.deepcopy(final.get("reconciliation_observations", {}))
        if not isinstance(observations, dict):
            raise CapsuleCampaignError(
                "author reconciliation observations are not an object: "
                f"{type(observations).__name__}"
            )
        # Take the two fields that carry author meaning; the raw receipt keeps the rest.
        # Refusing on an extra name discarded a committed, validated capsule when an author
        # nested the top-level `unresolved_decisions` one brace too deep.
        observations = {
            key: value
            for key, value in observations.items()
            if key in AUTHOR_OBSERVATION_FIELDS
        }
        candidate, _ = runtime._candidate(runtime._state["provider_receipts"])
        observations.update(
            {
                "intent": {
                    "intent_envelope_sha256": runtime.intent_sha256,
                    "plan_sha256": runtime.plan_sha256,
                    "revision_id": runtime.revision_id,
                    "capsule_id": runtime.capsule_id,
                },
                "candidate": {
                    key: value
                    for key, value in candidate.items()
                    if key != "repository_common_dir_sha256"
                },
                "validation": runtime._state["validation"],
                "obligation_coverage": author_state["obligation_coverage"],
                "invariants": {
                    row["invariant_id"]: {
                        "status": {
                            "pass": "match",
                            "fail": "conflict",
                            "unavailable": "unavailable",
                        }[row["status"]],
                        "evidence_refs": row["evidence_refs"],
                    }
                    for row in author_state["invariants"]
                },
            }
        )
        return runtime.reconcile(
            observations,
            evidence_refs=[
                runtime._state["validation_receipt_sha256"],
                canonical_json_sha256(receipt),
            ],
            actor="provider.author",
            _route=False,
        )

    def _validate(self, runtime):
        from bearhug.campaign.capsule_validation import execute_capsule_validation

        candidate, _ = runtime._candidate(runtime._state["provider_receipts"])
        profiles = runtime.capsule["validation_profiles"]
        refs = {ref for profile in profiles for ref in profile["command_refs"]}
        commands = {ref: self.record["config"]["validation_commands"][ref] for ref in refs}
        # The explicit prepared configuration authorizes commands. Existing capsule operation
        # locks and heartbeats fence this real execution; no caller success flags are accepted.
        stop = threading.Event()
        errors = []

        def heartbeat():
            while not stop.wait(20):
                try:
                    runtime._heartbeat()
                except Exception as exc:
                    errors.append(exc)
                    return

        with runtime._candidate_operation():
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            try:
                result = execute_capsule_validation(
                    candidate_worktree=runtime.worktree,
                    candidate=candidate,
                    profiles=profiles,
                    commands=commands,
                    state_root=runtime._blobs,
                    timeout_s=self.remaining() / len(commands),
                )
            finally:
                stop.set()
                thread.join(timeout=1)
            if errors:
                raise errors[0]
            self.remaining()
            if result["record"]["status"] != "pass":
                runtime._transition(
                    "validation_observed",
                    evidence_refs=[result["receipt_sha256"]],
                    state="locally_repairing",
                    last_outcome="local_repair_required",
                    validation_receipt_sha256=None,
                    validation_artifact_refs={},
                    acceptance_proof_sha256=None,
                    next_action="repair the actual failed validation in this capsule",
                    state_delta={
                        "changed_facts": ["actual validation failed"],
                        "candidate": None,
                        "git": None,
                        "validation_state_changes": [result["receipt_sha256"]],
                        "discoveries": [],
                        "unresolved_decisions": [],
                        "next_action": "repair failed validation",
                    },
                )
                return False
            rows = {row["command_ref"]: row for row in result["record"]["commands"]}
            artifacts = {
                ref: rows[binding["command_ref"]][f"{binding['stream']}_sha256"]
                for ref, binding in self._artifact_bindings(runtime.capsule).items()
                if ref in runtime.capsule["completion_boundary"]["required_artifact_refs"]
            }
            runtime._validate_candidate(result["receipt_sha256"], artifact_refs=artifacts)
        self._validated_reconciliation(runtime)
        return runtime.state == "candidate_ready"

    def _accept(self, runtime):
        from bearhug.campaign.capsule_review import build_capsule_acceptance_proof

        bundle = runtime.acceptance_bundle()
        proof = build_capsule_acceptance_proof(
            capsule_id=runtime.capsule_id,
            revision_id=runtime.revision_id,
            intent_envelope_sha256=runtime.intent_sha256,
            plan_sha256=runtime.plan_sha256,
            candidate=bundle["packet"]["candidate"],
            packet=bundle["packet"],
            author_receipts=bundle["author_receipts"],
            reviewer_receipts=bundle["reviewer_receipts"],
            review_receipts=bundle["review_receipts"],
            policy_sha256=runtime.policy_sha256,
            quorum=self.intent["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
                "minimum_approvals"
            ],
            dependency_base=runtime.dependency_base,
        )
        runtime.accept_candidate(
            proof=proof, dependency_acceptance_bundles=self._bundles(complete=False)
        )
        self._save(
            accepted={**self.state["accepted"], runtime.capsule_id: str(runtime.root)},
            active_capsule_id=None,
        )
        self.leases().release(
            runtime.lease.identity, reason="capsule accepted; custody remains readable"
        )

    def run(self):
        if self._consume_stop() or self.state["stopped"]:
            self._finish_stop()
            return
        if self.state["status"] == "phase_validated":
            self._phase(replay_only=True)
            return
        if self.state["started_at"] is None:
            self._save(started_at=self.clock())
        self._fresh_subject()
        self._release_accepted_leases()
        pending = {
            row["capsule_id"]: row
            for row in self.plan["capsules"]
            if row["capsule_id"] not in self.state["accepted"]
        }
        while pending:
            ready = [
                row
                for row in pending.values()
                if set(row["depends_on"]) <= set(self.state["accepted"])
            ]
            if not ready:
                raise CapsuleCampaignError("no dependency-eligible capsule remains")
            capsule = ready[0]
            findings = self._preflight_findings(capsule)
            self._save(
                active_capsule_id=capsule["capsule_id"],
                status="running",
                reason="executing eligible capsule",
            )
            runtime = self.runtime(capsule)
            self._semantic_preflight(runtime, findings)
            # A crash after acceptance but before updating the scheduler never spends again.
            if runtime.state == "completed":
                self._save(
                    accepted={**self.state["accepted"], runtime.capsule_id: str(runtime.root)},
                    active_capsule_id=None,
                )
                self.leases().release(runtime.lease.identity, reason="recovered accepted capsule")
            else:
                while runtime.state != "completed":
                    if self._consume_stop():
                        self._finish_stop()
                        return
                    self.remaining()
                    if runtime.state == "reconciling" and self._automatic_revision(runtime):
                        continue
                    if runtime.state in {"awaiting_hil", "blocked", "reconciling"}:
                        self._save(status=runtime.state, reason=runtime._state.get("next_action"))
                        return
                    if self.state["first_action_at"] is None:
                        self._save(first_action_at=self.clock())
                    if runtime.state == "candidate_ready":
                        if not runtime._state.get(
                            "validation_receipt_sha256"
                        ) and not self._validate(runtime):
                            continue
                        quorum = self.intent["campaign_envelope"]["policy_snapshot"]["policies"][
                            "review"
                        ]["minimum_approvals"]
                        from bearhug.campaign.capsule_review_runtime import _round_approvals

                        candidate, _ = runtime._candidate(runtime._state["provider_receipts"])
                        packet_sha = runtime._state.get("review_round_packet_sha256")
                        approved = (
                            _round_approvals(
                                runtime,
                                candidate_sha256=canonical_json_sha256(candidate),
                                packet_sha256=packet_sha,
                            )
                            if packet_sha
                            else 0
                        )
                        if approved < quorum:
                            runtime.review_candidate(
                                review_role_name=self.record["config"].get(
                                    "review_role", "reviewer"
                                ),
                                provider_runner=self.runner,
                            )
                            if runtime.state != "candidate_ready":
                                continue
                            continue
                        self._accept(runtime)
                    elif (
                        runtime.state == "failed"
                        and runtime._state.get("last_failed_stage") == "review"
                        and runtime._state.get("active_review") is None
                    ):
                        # Explicit recovery disposed of the interrupted review operation.  The
                        # author candidate remains valid, so obtain a new independent review
                        # without spending another author episode.
                        runtime.review_candidate(
                            review_role_name=self.record["config"].get(
                                "review_role", "reviewer"
                            ),
                            provider_runner=self.runner,
                        )
                    else:
                        outcome = runtime.run_episode()
                        if outcome.outcome == "failed":
                            self._save(
                                status="failed",
                                reason=outcome.next_action
                                or "provider episode failed; inspect custody before recovery",
                            )
                            return
            pending = {
                row["capsule_id"]: row
                for row in self.plan["capsules"]
                if row["capsule_id"] not in self.state["accepted"]
            }
            if self.record["config"].get("checkpoint") == "capsule":
                self._save(status="checkpoint", reason="configured capsule checkpoint")
                return
        if self.record["config"].get("checkpoint") == "phase" and not self.state.get(
            "phase_checkpoint_taken"
        ):
            self._save(
                status="checkpoint",
                reason="configured phase checkpoint",
                phase_checkpoint_taken=True,
            )
            return
        self._phase()

    def _artifact_bindings(self, capsule):
        direct = self.record["config"]["artifact_bindings"].get(capsule["capsule_id"])
        if direct is not None:
            return direct
        matches = [
            self.record["config"]["artifact_bindings"][row["capsule_id"]]
            for row in self.prepared.plan["capsules"]
            if row["validation_profiles"] == capsule["validation_profiles"]
            and row["completion_boundary"] == capsule["completion_boundary"]
        ]
        if not matches or any(row != matches[0] for row in matches):
            raise CapsuleCampaignError("revised artifact bindings are unavailable or ambiguous")
        return matches[0]

    def _automatic_revision(self, runtime):
        from bearhug.campaign.capsule_review import _durable_final_output

        receipts = runtime._state["provider_receipts"]
        if not receipts or runtime.hil_request is not None:
            return False
        final = _durable_final_output(runtime.custody, receipts[-1])
        successor = final.get("proposed_plan_revision")
        if successor is None:
            return False
        self.activate_revision(successor, runtime=runtime)
        return True

    def activate_revision(self, successor, *, runtime=None):
        if runtime is None:
            cid = self.state["active_capsule_id"]
            capsule = next((row for row in self.plan["capsules"] if row["capsule_id"] == cid), None)
            if capsule is None:
                raise CapsuleCampaignError("revision has no exact active capsule boundary")
            runtime = self.runtime(capsule)
        mode = successor.get("revision", {}).get("approval_mode")
        answer = runtime._state.get("last_hil_answer") if mode == "hil_approved" else None
        already = runtime._state.get("future_plan_sha256")
        if already and already == validate_capsule_plan(successor).digest:
            activated = runtime.objects.get(already, record_kind="capsule_plan")
        else:
            activated = runtime.activate_plan_revision(
                successor, approval_mode=mode, hil_answer=answer
            )
        store = CapsuleObjectStore(self.root / "active-plans", create=True)
        store.put(self.intent)
        cursor = activated
        while True:
            store.put(cursor)
            predecessor = cursor["revision"]["predecessor_sha256"]
            if predecessor is None:
                break
            cursor = runtime.objects.get(predecessor, record_kind="capsule_plan")
        self.plan = activated
        self._save(
            active_plan_sha256=store.put(activated),
            reason="affected future plan revision activated",
        )
        if runtime.state == "reconciling" and runtime.hil_request is None:
            runtime._transition(
                "reconciliation_completed",
                evidence_refs=[store.put(activated)],
                state="continuing",
                last_outcome="continue_with_evidence",
                next_action="continue the preserved capsule under unchanged authority",
                state_delta={
                    "changed_facts": ["affected future plan revised within existing authority"],
                    "candidate": None,
                    "git": None,
                    "validation_state_changes": [],
                    "discoveries": [],
                    "unresolved_decisions": [],
                    "next_action": "continue the preserved capsule",
                },
            )

    def request_stop(self, reason="operator requested stop"):
        if not isinstance(reason, str) or not 1 <= len(reason) <= 3500:
            raise CapsuleCampaignError("stop requires a bounded reason")
        path = self.root / "stop-request.json"
        value = {"prepared_sha256": self.record["content_sha256"], "reason": reason}
        if path.exists():
            if _read_json(path).get("prepared_sha256") != self.record["content_sha256"]:
                raise CapsuleCampaignError("stop request belongs to another run")
            return
        _create_only(path, value)

    def _release_accepted_leases(self):
        # Reopen proof before finishing a release interrupted after durable acceptance.
        for bundle in self._bundles(complete=False, historical_only=True):
            lease = self.leases().get(
                self.state["launches"][bundle["capsule"]["capsule_id"]]["lease_id"]
            )
            if lease.state == "active":
                self.leases().release(
                    lease.identity, reason="accepted custody reopened after restart"
                )

    def _finish_stop(self):
        """Release a conclusively idle stopped capsule; retain uncertain execution custody."""
        self._release_accepted_leases()
        if "phase.integration" in self.state["launches"]:
            integration_id = "integration." + _key(self.record["run_id"])[:20]
            result_path = (
                self.paths["integration_root"] / "integration-receipts"
                / f"{integration_id}.phase-result.json"
            )
            if result_path.exists():
                self._phase(replay_only=True)
            # An unfinished phase keeps its lease until explicit recovery inspects its fences.
            return
        cid = self.state["active_capsule_id"]
        capsule = next((row for row in self.plan["capsules"] if row["capsule_id"] == cid), None)
        if capsule is None or cid not in self.state["launches"]:
            return
        evidence = read_capsule_evidence(self.capsule_root, cid)
        state = evidence["state"] if evidence else {}
        if state.get("active_episode") or state.get("active_review"):
            return
        lease = self.leases().get(self.state["launches"][cid]["lease_id"])
        if lease.state == "released":
            return
        # The runtime verifies the launch, authority and candidate inspection lock. Expired
        # or uncertain ownership still follows existing orphan recovery, never an unsafe clear.
        # This reads two state keys and spends nothing, so it must not require a renewable
        # fence: requiring one left a lease that expired while the campaign was stopped with
        # no supported release at all. See CapsuleRuntime._candidate_inspection.
        #
        # release_only additionally tolerates a checkout that has moved past the durable
        # episode head, when that episode is already closed `failed`/`blocked`: a provider
        # process that outlived its own SIGKILLed controller can keep committing into this
        # same worktree after the controller's own recovery closed the episode. Without it
        # this open refuses and the lease can never be released. The late commit is never
        # adopted: the durable head this runtime loads is untouched, and a fresh attempt
        # never reuses this worktree. See CapsuleRuntime.__init__.
        runtime = self.runtime(capsule, recovery=True, release_only=True)
        with runtime._candidate_inspection():
            if runtime._state.get("active_episode") or runtime._state.get("active_review"):
                return
        reason = "operator stopped idle capsule"
        if runtime.observed_late_head_oid is not None:
            reason = (
                f"released after a closed {runtime._state.get('state')} episode; checkout "
                f"HEAD {runtime.observed_late_head_oid} is ahead of the durable head "
                f"{runtime._state.get('head_oid')} and was not adopted"
            )
        self.leases().release(runtime.lease.identity, reason=reason)

    def _consume_stop(self):
        path = self.root / "stop-request.json"
        if not path.exists():
            return False
        value = _read_json(path)
        if (
            set(value) != {"prepared_sha256", "reason"}
            or value["prepared_sha256"] != self.record["content_sha256"]
        ):
            raise CapsuleCampaignError("stop request differs from selected run authority")
        if not self.state["stopped"]:
            self._save(
                stopped=True,
                status="stopped",
                reason=value["reason"],
                operator_actions=[
                    *self.state["operator_actions"],
                    {"action": "stop", "at": self.clock()},
                ],
            )
        return True

    def _bundles(self, *, complete=True, historical_only=False):
        from bearhug.campaign.capsule_review import verify_capsule_acceptance

        pending = list(self.plan["capsules"])
        ordered, seen = [], set()
        while pending:
            row = next((row for row in pending if set(row["depends_on"]) <= seen), None)
            if row is None:
                raise CapsuleCampaignError("capsule dependency cycle")
            pending.remove(row)
            seen.add(row["capsule_id"])
            ordered.append(row)
        bundles = []
        custody = ProviderCustodyStore(
            self.paths["campaign_root"] / "provider-custody",
            self.paths["provider_output_root"],
            qualification_index=self.prepared.qualification_index,
        )
        for capsule in ordered:
            cid = capsule["capsule_id"]
            if not complete and cid not in self.state["accepted"]:
                continue
            evidence = read_capsule_evidence(self.capsule_root, cid)
            if (
                cid not in self.state["accepted"]
                or evidence is None
                or evidence["state"]["state"] != "completed"
            ):
                raise CapsuleCampaignError("accepted capsule completion evidence is unavailable")
            if str(evidence["root"]) != self.state["accepted"][cid]:
                raise CapsuleCampaignError("accepted capsule locator differs from run custody")
            state, root = evidence["state"], evidence["root"]
            proof = json.loads(_blob(root / "blobs", state["acceptance_proof_sha256"]))
            packet_sha = proof["packet_sha256"]
            rows = [
                row for row in state["acceptance_bundles"] if row["packet_sha256"] == packet_sha
            ]
            reviewers = {canonical_json_sha256(row): row for row in state["reviewer_receipts"]}
            previous = evidence["plan"]["revision"]["predecessor_sha256"]
            bundle = {
                "_historical_only": historical_only,
                "intent_envelope": evidence["intent"],
                "capsule_plan": evidence["plan"],
                "capsule": capsule,
                "packet": json.loads(_blob(root / "blobs", packet_sha)),
                "candidate_worktree": self.state["launches"][cid]["worktree"],
                "author_receipts": state["provider_receipts"],
                "reviewer_receipts": [reviewers[row["reviewer_receipt_sha256"]] for row in rows],
                "review_receipts": [
                    json.loads(_blob(root / "blobs", row["review_record_sha256"])) for row in rows
                ],
                "proof": proof,
                "custody": custody,
                "policy_sha256": canonical_json_sha256(self.record["provider_policy"]),
                "provider_policy": self.prepared.provider_policy,
                "qualification_index": self.prepared.qualification_index,
                "dependency_base": state.get("dependency_base"),
                "validation_state_root": root / "blobs",
                "validation_receipt_sha256": state["validation_receipt_sha256"],
                "validation_commands": {
                    ref: self.record["config"]["validation_commands"][ref]
                    for profile in capsule["validation_profiles"]
                    for ref in profile["command_refs"]
                },
                "previous_plan": CapsuleObjectStore(root / "objects").get(
                    previous, record_kind="capsule_plan"
                )
                if previous
                else None,
            }
            verify_capsule_acceptance(**bundle, dependency_acceptance_bundles=bundles)
            bundles.append(bundle)
        return bundles

    def _phase_lease(self, launch):
        from bearhug.campaign.leases import _canonical_claim_set

        store = self.leases()
        lease = store.get(launch["lease_id"])
        if launch.get("lease_identity") is not None and launch["lease_identity"] != asdict(
            lease.identity
        ):
            raise CapsuleCampaignError("phase lease fencing identity changed since launch")
        suffix = _key("phase.integration")[:20]
        expected = {
            "campaign_id": self.record["campaign_id"],
            "run_id": self.record["run_id"],
            "controller_authority_sha256": self.record["content_sha256"],
            "repository_common_dir_sha256": self.record["subject"]["repository_common_dir_sha256"],
            "worktree_sha256": worktree_sha256(launch["worktree"]),
            "branch": launch["branch"],
            "base_oid": self.plan["subject"]["base_oid"],
            "session_id": f"session.{suffix}",
            "claimant_id": f"claimant.{suffix}",
        }
        if any(getattr(lease.identity, key) != value for key, value in expected.items()):
            raise CapsuleCampaignError("phase lease identity differs from the prepared run")
        claims = self.intent["campaign_envelope"]["mutation_envelope"]
        if lease.claim_set != _canonical_claim_set(claims)[0]:
            raise CapsuleCampaignError("phase lease mutation envelope differs")
        registration_path = Path(launch["registration_path"])
        registration = _read_json(registration_path)
        material = {key: value for key, value in registration.items() if key != "content_sha256"}
        digest = hashlib.sha256(_canonical(material)).hexdigest()
        if (
            registration.get("content_sha256") != digest
            or registration_path.name != f"{digest}.json"
            or registration.get("work_unit_id") != "phase.integration"
            or registration.get("worktree") != launch["worktree"]
            or registration.get("lease_epoch") != lease.identity.epoch
        ):
            raise CapsuleCampaignError("phase launch registration custody differs")
        identity = lease.identity
        if any(
            registration.get(key) != getattr(identity, key)
            for key in (
                "lease_id",
                "campaign_id",
                "run_id",
                "session_id",
                "claimant_id",
                "controller_authority_sha256",
                "repository_common_dir_sha256",
                "worktree_sha256",
                "branch",
                "base_oid",
            )
        ):
            raise CapsuleCampaignError("phase lease differs from its launch registration")
        return store, lease

    def _recover_integration(self, store, integration_id, launch, bundles, outcome):
        from bearhug.campaign.claims import active_claims, build_claim
        from bearhug.campaign.integration import (
            _content_receipt,
            recover_integration_attempt,
        )

        integrator = self.intent["campaign_envelope"]["policy_snapshot"]["policies"]["integration"][
            "integrator_work_unit_id"
        ]
        inputs, reviews = [], []
        for index, bundle in enumerate(bundles):
            inputs.append(
                {
                    "work_unit_id": bundle["capsule"]["capsule_id"],
                    "merge_order": index + 1,
                    "provider_receipt_sha256": canonical_json_sha256(bundle["author_receipts"][-1]),
                    "candidate": bundle["packet"]["candidate"],
                    **(
                        {"dependency_base": bundle["dependency_base"]}
                        if bundle.get("dependency_base") is not None
                        else {}
                    ),
                }
            )
            reviews.extend(
                {
                    "work_unit_id": bundle["capsule"]["capsule_id"],
                    "review_id": f"capsule-review-{index}-{number}",
                    "receipt_sha256": digest,
                }
                for number, digest in enumerate(
                    sorted(
                        canonical_json_sha256(receipt) for receipt in bundle["reviewer_receipts"]
                    )
                )
            )
        expected = {
            "campaign_id": self.record["campaign_id"],
            "run_id": self.record["run_id"],
            "integration_id": integration_id,
            "integrator_id": integrator,
            "target": launch["worktree"],
            "base_oid": self.plan["subject"]["base_oid"],
            "inputs": inputs,
            "reviews": reviews,
            "checks": self.record["config"]["system_checks"],
        }
        if store.attempt_path(integration_id).exists():
            attempt = store.read_attempt(integration_id)
            if any(attempt.get(key) != value for key, value in expected.items()):
                raise CapsuleCampaignError("integration attempt differs from prepared acceptance")
            if not isinstance(attempt.get("claim"), dict):
                raise CapsuleCampaignError("integration attempt lacks its exact owner claim")
            claim = attempt["claim"]
        else:
            # The process may stop after claiming the target but before the engine's input fence.
            # Only one complete matching owner claim at the still-clean base can close that gap.
            claims = active_claims(self.paths["campaign_root"])
            if not claims:
                actual = capture_launch_repository(Path(launch["worktree"]))
                if (
                    actual.head_oid != expected["base_oid"]
                    or actual.repository_common_dir_sha256
                    != self.record["subject"]["repository_common_dir_sha256"]
                ):
                    raise CapsuleCampaignError("unclaimed integration changed its launch base")
                return None
            if len(claims) != 1:
                raise CapsuleCampaignError("interrupted integration lacks one exact owner claim")
            claim = claims[0]
        envelope = self.intent["campaign_envelope"]["mutation_envelope"]
        expected_claim = build_claim(
            campaign_id=self.record["campaign_id"],
            claimant_id=integrator,
            role="integrator",
            repository_common_dir_sha256=self.record["subject"]["repository_common_dir_sha256"],
            worktree_sha256=worktree_sha256(launch["worktree"]),
            branch=launch["branch"],
            base_oid=self.plan["subject"]["base_oid"],
            path_prefixes=envelope["path_prefixes"],
            semantic_resources=envelope["semantic_resources"],
            created_at=claim["created_at"],
        )
        if claim != expected_claim:
            raise CapsuleCampaignError("integration owner claim differs from prepared authority")
        if not store.attempt_path(integration_id).exists():
            actual = capture_launch_repository(Path(launch["worktree"]))
            if (
                actual.head_oid != expected["base_oid"]
                or actual.repository_common_dir_sha256
                != expected_claim["repository_common_dir_sha256"]
            ):
                raise CapsuleCampaignError("unfenced integration changed its launch base")
            store.write_attempt(
                _content_receipt(
                    {
                        "schema_version": "1",
                        "record_kind": "campaign_integration_attempt",
                        **expected,
                        "branch": launch["branch"],
                        "repository_common_dir_sha256": actual.repository_common_dir_sha256,
                        "initial_tree_oid": actual.tree_oid,
                        "claim": claim,
                    }
                )
            )
        resume = True
        timeout = 1.0
        if not store._path(integration_id).exists():
            try:
                timeout = self.remaining() / max(1, len(expected["checks"]))
            except CapsuleCampaignError:
                if outcome != "failed":
                    raise
                resume = False
        return recover_integration_attempt(
            state_root=self.paths["integration_root"],
            campaign_root=self.paths["campaign_root"],
            integration_id=integration_id,
            disposition=outcome,
            check_timeout_s=timeout,
            resume=resume,
        ).receipt

    def _phase_specs(self, receipt, *, role=None, recovery=False):
        """Derive the same independent reviewer worktrees used by phase execution/recovery."""
        role = role or self.record["config"].get("review_role", "reviewer")
        select = (
            self.prepared.qualification_index.recovery_binding
            if recovery else self.prepared.qualification_index.require
        )
        qualified = select(
            self.prepared.provider_policy.roles[role].provider
        )
        quorum = self.intent["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
            "minimum_approvals"
        ]
        specs = []
        for ordinal in range(quorum):
            target = (
                self.paths["worktree_parent"]
                / f"phase-review-{_key(self.record['run_id'])[:16]}-{ordinal}"
            )
            if not target.exists() and not recovery:
                from bearhug.host_git import run_git

                run_git(
                    Path(receipt["target"]), "worktree", "add", "--detach", "-q",
                    str(target), receipt["candidate"]["head_oid"], timeout=30,
                ).check_returncode()
            specs.append(
                {
                    "review_worktree": target,
                    "role_name": role,
                    "qualified_provider": qualified,
                    "provider_output_dir": self.paths["provider_output_root"]
                    / "phase"
                    / str(ordinal),
                }
            )
        return specs

    def _phase(self, *, replay_only=False, publish=True, recovery_outcome=None):
        if publish and not replay_only and recovery_outcome is None and self._consume_stop():
            return
        from bearhug.campaign.capsule_integration import (
            integrate_accepted_capsules,
            recover_phase_review,
            resume_phase_review,
            review_integrated_phase,
            verify_phase_result,
        )
        from bearhug.campaign.integration import CampaignIntegrationStore

        historical_only = recovery_outcome is not None or (replay_only and self.state["stopped"])
        bundles = self._bundles(historical_only=historical_only)
        integration_id = "integration." + _key(self.record["run_id"])[:20]
        store = CampaignIntegrationStore(self.paths["integration_root"] / "integration-receipts")
        custody = ProviderCustodyStore(
            self.paths["campaign_root"] / "provider-custody",
            self.paths["provider_output_root"],
            qualification_index=self.prepared.qualification_index,
        )
        predecessor = self.plan["revision"]["predecessor_sha256"]
        previous_plan = (
            CapsuleObjectStore(self.root / "active-plans").get(
                predecessor, record_kind="capsule_plan"
            )
            if predecessor
            else None
        )
        kwargs = dict(
            previous_plan=previous_plan,
            intent_envelope=self.intent,
            capsule_plan=self.plan,
            acceptance_bundles=bundles,
            integration_store=store,
            integration_id=integration_id,
            authority_sources=self.prepared.source_contents,
            check_commands=self.record["config"]["system_checks"],
            provider_policy=self.prepared.provider_policy,
            custody=custody,
        )
        result_path = store.root / f"{integration_id}.phase-result.json"
        if result_path.exists():
            result = verify_phase_result(
                **kwargs, qualification_index=self.prepared.qualification_index,
                _historical_only=historical_only,
            )
            if not publish:
                return result
            launch = self.state["launches"].get("phase.integration")
            if launch is not None:
                lease_store, lease = self._phase_lease(launch)
                if lease.state == "active":
                    lease_store.release(lease.identity, reason="durable phase assessment recovered")
            self._save(
                status=result["status"],
                phase={
                    "integration_id": integration_id,
                    "result_path": str(result_path),
                    "result": result,
                },
                active_capsule_id=None,
                reason="combined-system evidence reopened without provider spend",
            )
            return
        if replay_only:
            raise CapsuleCampaignError("completed phase result custody is unavailable")
        if recovery_outcome is None:
            self.remaining()
        integration_capsule = {
            "capsule_id": "phase.integration",
            "mutation_envelope": self.intent["campaign_envelope"]["mutation_envelope"],
        }
        launch = self._launch(integration_capsule)
        lease_store, lease = self._phase_lease(launch)
        if lease.state != "active":
            raise CapsuleCampaignError(
                "phase recovery requires its active lease or explicit orphan resolution"
            )
        errors, stop = [], threading.Event()

        def heartbeat():
            while not stop.wait(20):
                try:
                    lease_store.heartbeat(lease.identity, ttl_seconds=3600.0)
                except Exception as exc:
                    errors.append(exc)
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            if recovery_outcome is not None:
                receipt = self._recover_integration(
                    store, integration_id, launch, bundles, recovery_outcome
                )
            elif store._path(integration_id).exists():
                from bearhug.campaign.claims import active_claims

                if active_claims(self.paths["campaign_root"]):
                    raise CapsuleCampaignError("integration claim remains active; recover it first")
                receipt = store.read(integration_id)
            else:
                self._save(
                    status="integrating",
                    reason="separate owner integrating accepted capsule candidates",
                )
                result, _ = integrate_accepted_capsules(
                    intent_envelope=self.intent,
                    capsule_plan=self.plan,
                    previous_plan=previous_plan,
                    acceptance_bundles=bundles,
                    state_root=self.paths["integration_root"],
                    campaign_root=self.paths["campaign_root"],
                    campaign_id=self.record["campaign_id"],
                    run_id=self.record["run_id"],
                    integration_id=integration_id,
                    integrator_id=self.intent["campaign_envelope"]["policy_snapshot"]["policies"][
                        "integration"
                    ]["integrator_work_unit_id"],
                    target=launch["worktree"],
                    checks=self.record["config"]["system_checks"],
                    check_timeout_s=self.remaining()
                    / max(1, len(self.record["config"]["system_checks"])),
                )
                receipt = result.receipt
            if receipt is None:
                if self.state["stopped"] and recovery_outcome is not None:
                    lease_store.release(lease.identity, reason="stopped unstarted integration")
                    self._save(
                        status="stopped", active_capsule_id=None,
                        reason="unstarted integration custody released after recovery",
                    )
                    return
                self._save(
                    status="preflighted", reason="integration has not started; resume its owner"
                )
                return
            if receipt["status"] != "passed":
                if recovery_outcome == "failed":
                    lease_store.release(lease.identity, reason="integration explicitly failed")
                self._save(
                    status="blocked",
                    reason="combined-system integration or validation failed",
                    phase={"integration_id": integration_id, "integration_receipt": receipt},
                    **({"active_capsule_id": None} if recovery_outcome == "failed" else {}),
                )
                return
            phase_started = (store.root / f"{integration_id}.phase-review-started.json").exists()
            if recovery_outcome is not None:
                recovered = (
                    recover_phase_review(
                        **kwargs,
                        reviewer_specs=self._phase_specs(receipt, recovery=True),
                        qualification_index=self.prepared.qualification_index,
                        disposition=recovery_outcome,
                        historical_only=True,
                    )
                    if phase_started
                    else None
                )
                if recovered is not None:
                    # Reopen the durable result through the same verifier/release path as a restart.
                    return self._phase(replay_only=True)
                if self.state["stopped"] and (
                    not phase_started or recovery_outcome == "failed"
                ):
                    lease_store.release(lease.identity, reason="stopped phase explicitly recovered")
                    self._save(
                        status="stopped", active_capsule_id=None,
                        phase={
                            "integration_id": integration_id,
                            "recovery": "stopped_after_disposition",
                        },
                        reason="phase custody resolved without further provider execution",
                    )
                    return
                self._save(
                    status="preflighted"
                    if not phase_started or recovery_outcome == "failed"
                    else "blocked",
                    phase={
                        "integration_id": integration_id,
                        "recovery": "resume"
                        if not phase_started or recovery_outcome == "failed"
                        else "failed_disposition_required",
                    },
                    reason="integration recovered; resume independent phase review"
                    if not phase_started
                    else "phase custody preserved; resume the explicitly disposed review attempt"
                    if recovery_outcome == "failed"
                    else (
                        "phase review interrupted; explicit failed disposition "
                        "required for uncertain spend"
                    ),
                )
                return
            quorum = self.intent["campaign_envelope"]["policy_snapshot"]["policies"]["review"][
                "minimum_approvals"
            ]
            specs = self._phase_specs(receipt)
            self._save(
                status="phase_review",
                reason="independent original-goal and system-invariant assessment",
            )
            from bearhug.campaign.reviewer import _default_runner

            def fenced_runner(provider, prompt, contract, options):
                if self._consume_stop():
                    raise CapsuleCampaignError("operator stopped before the next phase review")
                if errors:
                    raise errors[0]
                remaining = self.remaining()
                options = dict(options)
                options["timeout_s"] = min(options.get("timeout_s", remaining), remaining)
                return (self.runner or _default_runner)(provider, prompt, contract, options)

            reviewer = resume_phase_review if phase_started else review_integrated_phase
            result = reviewer(
                **kwargs,
                reviewer_specs=specs,
                runner=fenced_runner,
                timeout_s=self.remaining() / quorum,
                **(
                    {"qualification_index": self.prepared.qualification_index}
                    if phase_started
                    else {}
                ),
            )
            if errors:
                raise errors[0]
            self.remaining()
            verify_phase_result(**kwargs, qualification_index=self.prepared.qualification_index)
            self._save(
                status=result["status"],
                phase={
                    "integration_id": integration_id,
                    "result_path": str(result_path),
                    "result": result,
                },
                active_capsule_id=None,
                reason="combined system validated"
                if result["status"] == "phase_validated"
                else "combined-system semantic review blocked completion",
            )
            lease_store.release(lease.identity, reason="phase assessment durably recorded")
        finally:
            stop.set()
            thread.join(timeout=1)

    def answer(self, *, question_id, decision, answer="", amendment=None):
        cid = self.state["active_capsule_id"]
        capsule = next((row for row in self.plan["capsules"] if row["capsule_id"] == cid), None)
        if capsule is None:
            raise CapsuleCampaignError("run has no active capsule question")
        runtime = self.runtime(capsule)
        request = runtime.hil_request
        if request is None or request["request_id"] != question_id:
            raise CapsuleCampaignError("question is stale or belongs to another run")
        with runtime._candidate_operation():
            runtime.answer_hil(
                question_id,
                decision,
                token=request["resume_token_sha256"],
                answer=answer,
                amendment=amendment,
            )
        self._save(status=runtime.state, reason=runtime._state["next_action"])
        if decision in {"deny", "stop"}:
            self._save(stopped=True)
            self._finish_stop()

    def _launch_leases(self, cid):
        """Live leases this capsule's launch would have taken, matched on its derived identity."""

        suffix = _key(cid)[:20]
        session_id, claimant_id = f"session.{suffix}", f"claimant.{suffix}"
        return tuple(
            record
            for record in self.leases().list()
            if record.identity.session_id == session_id
            and record.identity.claimant_id == claimant_id
            and record.identity.campaign_id == self.record["campaign_id"]
            and record.identity.run_id == self.record["run_id"]
        )

    def _recover_launch(self):
        cid = self.state["launching_capsule"]
        capsule = next((row for row in self.plan["capsules"] if row["capsule_id"] == cid), None)
        dependency_base = self._dependency_base(capsule) if capsule else None
        base_oid = (
            dependency_base["base_oid"] if dependency_base else self.plan["subject"]["base_oid"]
        )
        matches = []
        directory = self.root / "worktree-registrations"
        if directory.is_dir():
            for path in directory.iterdir():
                row = _read_json(path)
                if row.get("work_unit_id") != cid:
                    continue
                digest = row.get("content_sha256")
                material = {key: value for key, value in row.items() if key != "content_sha256"}
                if (
                    path.name != f"{digest}.json"
                    or hashlib.sha256(_canonical(material)).hexdigest() != digest
                ):
                    raise CapsuleCampaignError("interrupted launch registration digest changed")
                if (
                    row.get("campaign_id"),
                    row.get("run_id"),
                    row.get("controller_authority_sha256"),
                ) != (
                    self.record["campaign_id"],
                    self.record["run_id"],
                    self.record["content_sha256"],
                ):
                    raise CapsuleCampaignError("interrupted launch belongs to another authority")
                matches.append((path, row))
        if not matches and not self._launch_leases(cid):
            # A worker killed between the launch marker and provisioning (SIGKILL, OOM, a
            # crashed provider) leaves the marker set with nothing behind it. No registration
            # and no live lease is positive proof that no custody was created, so the run is
            # recoverable; refusing here would wedge the campaign permanently.
            self._save(
                launching_capsule=None,
                status="blocked",
                reason="interrupted launch created no custody; nothing to recover",
            )
            return
        if len(matches) != 1:
            raise CapsuleCampaignError(
                "interrupted launch lacks one complete registration; "
                "retain its lease for advanced orphan recovery"
            )
        path, row = matches[0]
        suffix = _key(cid)[:20]
        target = derive_campaign_worktree_target(
            worktree_parent=self.paths["worktree_parent"],
            campaign_id=self.record["campaign_id"],
            run_id=self.record["run_id"],
            controller_authority_sha256=self.record["content_sha256"],
            work_unit_id=cid,
            session_id=f"session.{suffix}",
            claimant_id=f"claimant.{suffix}",
            repository_common_dir_sha256=self.record["subject"]["repository_common_dir_sha256"],
            base_oid=base_oid,
        )
        if row["worktree"] != str(target.path) or row["branch"] != target.branch:
            raise CapsuleCampaignError("interrupted launch does not match its derived target")
        lease = self.leases().get(row["lease_id"])
        identity = lease.identity
        expected = {
            "campaign_id": self.record["campaign_id"],
            "run_id": self.record["run_id"],
            "controller_authority_sha256": self.record["content_sha256"],
            "base_oid": base_oid,
            "branch": row["branch"],
            "worktree_sha256": row["worktree_sha256"],
            "session_id": f"session.{suffix}",
            "claimant_id": f"claimant.{suffix}",
        }
        if any(getattr(identity, key) != value for key, value in expected.items()):
            raise CapsuleCampaignError("interrupted launch lease identity differs")
        from bearhug.campaign.leases import _canonical_claim_set

        capsule = next((item for item in self.plan["capsules"] if item["capsule_id"] == cid), None)
        expected_claims = (
            capsule["mutation_envelope"]
            if capsule is not None
            else self.intent["campaign_envelope"]["mutation_envelope"]
        )
        if lease.claim_set != _canonical_claim_set(expected_claims)[0]:
            raise CapsuleCampaignError("interrupted launch mutation claims differ")
        branch = subprocess.run(
            ["git", "-C", row["worktree"], "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if branch != row["branch"]:
            raise CapsuleCampaignError("interrupted launch branch changed")
        actual = capture_launch_repository(Path(row["worktree"]))
        if (
            actual.head_oid != base_oid
            or actual.repository_common_dir_sha256
            != self.record["subject"]["repository_common_dir_sha256"]
        ):
            raise CapsuleCampaignError("interrupted launch worktree changed")
        self.leases().heartbeat(identity, ttl_seconds=3600.0)
        launch = {
            "lease_id": identity.lease_id,
            "worktree": row["worktree"],
            "branch": row["branch"],
            "claimant_id": identity.claimant_id,
            "registration_path": str(path),
            "dependency_base": dependency_base,
        }
        self._save(
            launching_capsule=None,
            launches={**self.state["launches"], cid: launch},
            status="preflighted",
            reason="completed worktree launch recovered without provider spend",
        )

    def recover(self, *, outcome="blocked", episode_id=None, review_id=None):
        if self.state.get("launching_capsule"):
            self._recover_launch()
            if self.state["stopped"]:
                self._finish_stop()
            return
        if self.state["stopped"] and self.state["active_capsule_id"] != "phase.integration":
            cid = self.state["active_capsule_id"]
            evidence = read_capsule_evidence(self.capsule_root, cid) if cid else None
            if evidence is None or not (
                evidence["state"].get("active_episode") or evidence["state"].get("active_review")
            ):
                self._finish_stop()
                return
        cid = self.state["active_capsule_id"]
        capsule = next((row for row in self.plan["capsules"] if row["capsule_id"] == cid), None)
        if capsule is None:
            if cid != "phase.integration":
                raise CapsuleCampaignError("run has no unresolved capsule or integration boundary")
            if episode_id is not None or review_id is not None:
                raise CapsuleCampaignError(
                    "phase recovery does not accept capsule episode/review ids"
                )
            self._fresh_subject()
            self._phase(recovery_outcome=outcome)
            return
        runtime = self.runtime(capsule, recovery=True)
        if runtime._state.get("active_review"):
            expected = runtime._state["active_review"]["review_id"]
            if review_id is not None and review_id != expected:
                raise CapsuleCampaignError("review recovery identity differs from selected run")
            runtime.recover_review(expected, outcome=outcome)
        elif runtime._state.get("active_episode"):
            expected = runtime._state["active_episode"]["episode_id"]
            if episode_id is not None and episode_id != expected:
                raise CapsuleCampaignError("episode recovery identity differs from selected run")
            runtime.recover_episode(expected, outcome=outcome)
        else:
            raise CapsuleCampaignError("run has no unresolved episode/review spend fence")
        self._save(status=runtime.state, reason=runtime._state["next_action"])
        if self.state["stopped"]:
            self._finish_stop()

    def report(self):
        observed_state = copy.deepcopy(self.state)
        if self.state["status"] == "phase_validated":
            try:
                self._phase(replay_only=True, publish=False)
            except Exception as exc:
                observed_state.update(
                    status="blocked", reason=f"completed evidence is stale or unavailable: {exc}"
                )
        capsules = []
        for capsule in self.plan["capsules"]:
            evidence = read_capsule_evidence(self.capsule_root, capsule["capsule_id"])
            launch = self.state["launches"].get(capsule["capsule_id"], {})
            capsules.append(
                {
                    "capsule": capsule,
                    **(
                        evidence
                        or {
                            "state": None,
                            "journal": None,
                            "result": None,
                            "reconciliation": None,
                            "hil_request": None,
                            "grounding": None,
                        }
                    ),
                    "custody": {
                        **launch,
                        "runtime_root": str(self.capsule_root / _key(capsule["capsule_id"])),
                        "provider_output_root": str(self.paths["provider_output_root"]),
                    },
                }
            )
        metrics = {
            key: sum((row["state"] or {}).get(key, 0) for row in capsules)
            for key in (
                "episode_count",
                "repair_episodes",
                "review_launches",
                "prompt_bytes",
                "repeated_context_bytes",
            )
        }
        phase = (self.state.get("phase") or {}).get("result") or {}
        for key in ("review_launches", "prompt_bytes", "repeated_context_bytes"):
            metrics[key] += phase.get(key, 0)
        metrics.update(
            operator_commands=len(self.state["operator_actions"]),
            time_to_first_action_seconds=None,
            time_to_first_dispatch_seconds=None
            if self.state["first_action_at"] is None
            else self.state["first_action_at"]
            - self.state.get("prepared_started_at", _timestamp(self.record["created_at"])),
            first_action_limitation=(
                "dispatch timestamp is measured; provider first-tool-action timing is unavailable"
            ),
            provider_launches=None,
            provider_launch_limitation=(
                "episode/review spend fences count attempts; "
                "interrupted transport launch is not inferred"
            ),
            provider_usage_tokens=None,
            provider_usage_limitation="provider accounting is unavailable",
            hil_requests=sum(
                sum(
                    entry["event_kind"] == "hil_requested"
                    for entry in (row["journal"] or {}).get("entries", [])
                )
                for row in capsules
            ),
            hil_answers=sum(len((row["state"] or {}).get("hil_answers", [])) for row in capsules),
        )
        hil_idle = 0.0
        for row in capsules:
            requested = None
            for event in (row["journal"] or {}).get("entries", []):
                if event["event_kind"] == "hil_requested":
                    requested = _timestamp(event["occurred_at"])
                elif event["event_kind"] == "hil_answered" and requested is not None:
                    hil_idle += max(0.0, _timestamp(event["occurred_at"]) - requested)
                    requested = None
            if requested is not None:
                hil_idle += max(0.0, self.clock() - requested)
        metrics["hil_idle_seconds"] = hil_idle
        metrics["elapsed_campaign_seconds"] = max(
            0.0,
            self.clock()
            - self.state.get("prepared_started_at", _timestamp(self.record["created_at"])),
        )
        metrics["time_precision_limitation"] = "journal HIL timestamps have one-second precision"
        run = {**observed_state, "metrics": metrics}
        from bearhug.campaign.capsule_cockpit import build_capsule_cockpit

        cockpit = build_capsule_cockpit(
            prepared=self.record, intent=self.intent, plan=self.plan, run=run, capsules=capsules
        )
        return {
            "locator": self.record["locator"],
            "mode": "native_v2",
            "status": observed_state["status"],
            "reason": observed_state["reason"],
            "derived": self.record,
            "metrics": metrics,
            "cockpit": cockpit,
            "promotion_authorized": False,
        }


def run_prepared_command(args, *, provider_runner=None):
    """CLI dispatch, shared with the recorded-provider end-to-end qualification fixture."""
    from bearhug.campaign.cli import CampaignCommandError, CampaignCommandResult
    from bearhug.campaign.prepared import load_prepared, prepare_campaign

    command_started = time.time()
    try:
        if args.action == "prepare":
            from bearhug.campaign.proposal import prepare_from_intent

            prepare = prepare_campaign if args.capsule_plan else prepare_from_intent
            plan_options = {"plan_path": args.capsule_plan} if args.capsule_plan else {}
            prepared = prepare(
                subject=args.subject,
                intent_path=args.intent,
                policy_path=args.policy,
                execution_path=args.execution_config,
                state_root=args.state_root,
                worktree_parent=args.worktree_parent,
                campaign_id=args.campaign_id,
                run_id=args.run_id,
                **plan_options,
            )
        else:
            prepared = load_prepared(args.locator, recovery=args.action in {"stop", "recover"})
            for arg, expected in (
                ("campaign_id", prepared.record["campaign_id"]),
                ("run_id", prepared.record["run_id"]),
                ("state_root", str(prepared.root)),
                ("subject", prepared.record["subject"]["path"]),
            ):
                supplied = getattr(args, arg, None)
                if supplied is not None and supplied != expected:
                    raise CapsuleCampaignError(
                        f"--{arg.replace('_', '-')} differs from the exact run locator"
                    )
        campaign = CapsuleCampaign(prepared, provider_runner=provider_runner)
        if args.action == "stop":
            campaign.request_stop(args.reason or "operator stopped this run")
        with campaign.lock():
            if args.action == "prepare":
                campaign._save(prepared_started_at=command_started)
            if args.action not in {"prepare", "stop"}:
                campaign.action(args.action)
            successor = (
                _read_json(Path(args.successor_plan))
                if getattr(args, "successor_plan", None)
                else None
            )
            if successor is not None and args.action != "answer":
                campaign.activate_revision(successor)
            if args.action in {"run", "resume"}:
                try:
                    campaign.run()
                except Exception as exc:
                    if not campaign.state["stopped"]:
                        campaign._save(status="blocked", reason=str(exc))
            elif args.action == "answer":
                answer = getattr(args, "answer", None) or ""
                if getattr(args, "answer_file", None):
                    path = Path(args.answer_file)
                    if path.stat().st_size > 14000:
                        raise CapsuleCampaignError("HIL answer file is too large")
                    answer = path.read_text()
                amendment = None
                if successor is not None:
                    digest = validate_capsule_plan(
                        successor, intent_envelope=campaign.intent, previous_plan=campaign.plan
                    ).digest
                    amendment = {"successor_plan_sha256": digest}
                campaign.answer(
                    question_id=args.question_id,
                    decision=args.disposition,
                    answer=answer,
                    amendment=amendment,
                )
                if successor is not None:
                    campaign.activate_revision(successor)
                if args.disposition in {"approve", "amend"}:
                    try:
                        campaign.run()
                    except Exception as exc:
                        if not campaign.state["stopped"]:
                            campaign._save(status="blocked", reason=str(exc))
            elif args.action == "repair":
                cid = campaign.state.get("active_capsule_id")
                capsule = next((row for row in campaign.plan["capsules"]
                                if row["capsule_id"] == cid), None)
                if campaign.state["stopped"] or capsule is None:
                    raise CapsuleCampaignError("repair requires an active rejected capsule")
                campaign.runtime(capsule).request_review_repair()
                campaign.run()
            elif args.action == "stop":
                campaign._consume_stop()
                campaign._finish_stop()
            elif args.action == "recover":
                campaign.recover(
                    outcome=getattr(args, "recovery_outcome", "blocked"),
                    episode_id=getattr(args, "episode_id", None),
                    review_id=getattr(args, "review_id", None),
                )
            report = campaign.report()
        proof = report["cockpit"]["observed_proof_next_correction"]
        remediation = report["cockpit"]["remediation"]
        human = (
            f"{report['status']}: {report['reason']}\n"
            f"Observed proof: {proof['summary']}\n"
            f"Next correction: {proof['correction_class']}; {proof['reason']}\n"
            f"Next action: {remediation['command'] or proof['next_action'] or 'none'}\n"
            f"Run: {report['locator']}\n" + json.dumps(report["derived"], indent=2, sort_keys=True)
        )
        code = 1 if report["status"] in {"blocked", "failed"} else 0
        return CampaignCommandResult(code, report, human)
    except CapsuleCampaignBusy as exc:
        if args.action == "stop":
            return CampaignCommandResult(
                0,
                {
                    "locator": prepared.record["locator"],
                    "status": "stop_requested",
                    "mode": "native_v2",
                    "promotion_authorized": False,
                },
                "Stop recorded; the active run will pause before its next operation.",
            )
        raise CampaignCommandError(str(exc)) from exc
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        raise CampaignCommandError(str(exc)) from exc


def snapshot_capsule_campaign(locator):
    """Read an exact run for the live cockpit without adding operator actions or state events."""
    from bearhug.campaign.prepared import load_prepared

    campaign = CapsuleCampaign(load_prepared(locator), read_only=True)
    if campaign.state is None:
        raise CapsuleCampaignError("prepared run has not been initialized by the prepare command")
    return campaign.report()["cockpit"]
