"""Remove Bear Hug's own lease worktrees once a campaign is retired, and find leftovers.

Nothing here creates or adopts a worktree. It only ever acts on a worktree this exact
process can prove Bear Hug created: a ``bh-*`` branch (``inventory_worktrees``'s own
naming rule) *and* a matching custody registration published by
``bearhug.campaign.launcher.provision_campaign_worktree`` under some campaign's
``state_root/worktree-registrations/``. Branch naming alone never authorizes a removal;
see ``load_registration`` and every caller of it below.

A worktree is removed with plain ``git worktree remove`` (no ``--force``): this module's
own ``git status`` check already proved it clean before that call runs, so Git's own
cleanliness check inside ``remove`` is a second, independent confirmation rather than
something ``--force`` needs to override. The one exception is
``remove_missing_registered_entries``: there the worktree's own directory is already gone
(Git's ``prunable`` flag), so there is nothing left to be dirty, and ``--force`` only
overrides Git's refusal to prune a *locked* entry -- which this module never does, since a
locked entry is reported kept, not removed, exactly like every other case here.

Its branch is deleted only when it is provably safe: never through ``git branch -d``'s
"merged into HEAD or its upstream" (whichever branch a caller happens to have checked out
answers a different question -- see the module's history for the incident this replaced),
but through ``git update-ref -d``, and only once the branch's own tip is proven identical
to the exact commit it was leased from (its recorded ``base_oid``). Every removal this
module ever makes is of a capsule that was never accepted (``plan_removal`` never removes
an accepted capsule's worktree), so a branch with commits beyond its own base was never
integrated by Bear Hug's own tracking either -- there is no "target branch" on file to
check it against, and this module never guesses one. Such a branch is kept and named,
exactly as an unremovable worktree would be.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearhug.campaign.worktrees import (
    WorktreeInventoryError,
    WorktreeRegistration,
    inventory_worktrees,
)
from bearhug.host_git import run_git

_STATUS_ARGS = ("status", "--porcelain=v1", "-z", "--untracked-files=all")


def _safe_git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    """``run_git``, but an OS-level failure or a hung process becomes ``None`` instead of an
    uncaught exception -- a retire that already wrote its detach, or a `prune` scanning many
    worktrees, must report the one it could not touch as kept, not crash and mislabel a
    completed retire as failed (or take down the whole prune run over one balky entry)."""

    try:
        return run_git(cwd, *args)
    except (OSError, subprocess.TimeoutExpired):
        return None


class WorktreeCleanupError(RuntimeError):
    """A cleanup decision could not be made without guessing about custody."""


@dataclass(frozen=True, slots=True)
class WorktreeCleanupAction:
    """One decision about one worktree: what was done to it, and why."""

    path: str
    branch: str | None
    removed: bool
    branch_deleted: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "removed": self.removed,
            "branch_deleted": self.branch_deleted,
            "reason": self.reason,
        }


def kept(worktree: Any, branch: str | None, reason: str) -> WorktreeCleanupAction:
    """A public constructor for a "kept" action, for callers reporting their own reasons
    (e.g. a worktree belonging to a still-live campaign, found before this module is asked
    to plan anything)."""

    return WorktreeCleanupAction(str(worktree), branch, False, False, reason)


_kept = kept


def _read_registration_document(registration_path: Any) -> dict[str, Any] | None:
    if not registration_path:
        return None
    path = Path(registration_path)
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_registration(registration_path: Any) -> WorktreeRegistration | None:
    """Read back the exact custody evidence a launch published, or ``None`` if it cannot be
    trusted. This is the one thing that turns a ``bh-*`` branch name into proof."""

    data = _read_registration_document(registration_path)
    if data is None:
        return None
    try:
        return WorktreeRegistration(
            repository_common_dir_sha256=data["repository_common_dir_sha256"],
            worktree_sha256=data["worktree_sha256"],
            branch=data["branch"],
        )
    except (KeyError, TypeError):
        return None


def load_base_oid(registration_path: Any) -> str | None:
    """The exact commit a launch's branch was leased from, straight from its own published
    custody evidence -- the one fact that lets a branch's removal be proven safe (see the
    module docstring) without ever consulting an unrecorded "target branch"."""

    data = _read_registration_document(registration_path)
    if not isinstance(data, dict):
        return None
    value = data.get("base_oid")
    return value if isinstance(value, str) and value else None


def _registration_matches_launch(
    registration_path: Any, *, worktree: Any, branch: Any
) -> bool:
    """A registration is trusted only for the exact worktree and branch its own campaign
    launch recorded -- one file forged or copied onto a different launch's fields must not
    borrow another worktree's custody."""

    data = _read_registration_document(registration_path)
    if not isinstance(data, dict):
        return False
    recorded_worktree = data.get("worktree")
    if not isinstance(recorded_worktree, str) or not worktree:
        return False
    try:
        if Path(recorded_worktree).resolve(strict=False) != Path(worktree).resolve(strict=False):
            return False
    except OSError:
        return False
    return data.get("branch") == branch


def launch_records(campaign) -> list[dict[str, Any]]:
    """One record per capsule this exact campaign launched a worktree for."""

    state = campaign.state or {}
    launches = state.get("launches", {})
    accepted = state.get("accepted", {})
    paths = getattr(campaign, "record", {}).get("paths", {}) if hasattr(campaign, "record") else {}
    worktree_parent = paths.get("worktree_parent") if isinstance(paths, dict) else None
    records: list[dict[str, Any]] = []
    if not isinstance(launches, dict):
        return records
    for cid, launch in launches.items():
        if not isinstance(launch, dict):
            continue
        worktree = launch.get("worktree")
        branch = launch.get("branch")
        registration_path = launch.get("registration_path")
        registration = load_registration(registration_path)
        if registration is not None and not _registration_matches_launch(
            registration_path, worktree=worktree, branch=branch
        ):
            registration = None
        outside_parent = False
        if worktree_parent and worktree:
            try:
                Path(worktree).resolve(strict=False).relative_to(
                    Path(worktree_parent).resolve(strict=False)
                )
            except (OSError, ValueError):
                outside_parent = True
        records.append(
            {
                "capsule_id": cid,
                "worktree": worktree,
                "branch": branch,
                "accepted": cid in accepted,
                "registration": registration,
                "base_oid": load_base_oid(registration_path) if registration is not None else None,
                "outside_worktree_parent": outside_parent,
            }
        )
    return records


def _branch_tip(subject: Path, branch: str) -> str | None:
    result = _safe_git(subject, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()


def _maybe_delete_branch(subject: Path, branch: str, base_oid: str | None) -> tuple[bool, str]:
    """Delete ``branch`` only when it is provably safe; otherwise keep it and say why.

    "Provably safe" here means the branch never moved past the exact commit it was leased
    from: nothing to lose, because there is no commit on it Bear Hug's own tracking never
    saw. ``git update-ref -d`` is used instead of ``git branch -d``/``-D`` so the delete is
    atomic on that exact expected value and never depends on which branch some worktree
    happens to have checked out (see the module docstring for the incident this replaced).
    """

    if not base_oid:
        return False, f"branch {branch!r} kept: its leased base commit is unknown"
    tip = _branch_tip(subject, branch)
    if tip is None:
        return False, f"branch {branch!r} kept: cannot read its current tip"
    if tip != base_oid:
        return False, (
            f"branch {branch!r} kept: it has commits beyond its leased base, and Bear Hug "
            "never recorded them as integrated anywhere"
        )
    deletion = _safe_git(subject, "update-ref", "-d", f"refs/heads/{branch}", base_oid)
    if deletion is None:
        return False, f"branch {branch!r} kept: cannot run Git to delete it"
    if deletion.returncode != 0:
        detail = deletion.stderr.decode("utf-8", errors="replace").strip()
        return False, f"branch {branch!r} kept: {detail}"
    return True, ""


def _remove(
    subject: Path, path: Path, branch: str | None, base_oid: str | None
) -> WorktreeCleanupAction:
    status = _safe_git(path, *_STATUS_ARGS)
    if status is None:
        return _kept(path, branch, "cannot run Git to inspect the worktree")
    if status.returncode != 0:
        detail = status.stderr.decode("utf-8", errors="replace").strip()
        return _kept(path, branch, f"cannot read Git status for it: {detail}")
    if status.stdout:
        return _kept(path, branch, "it has uncommitted changes")

    removal = _safe_git(subject, "worktree", "remove", str(path))
    if removal is None:
        return _kept(path, branch, "cannot run Git to remove it")
    if removal.returncode != 0:
        detail = removal.stderr.decode("utf-8", errors="replace").strip()
        return _kept(path, branch, f"git worktree remove refused it: {detail}")

    branch_deleted = False
    reason = "removed"
    if branch:
        branch_deleted, note = _maybe_delete_branch(subject, branch, base_oid)
        if not branch_deleted:
            reason = f"removed; {note}"
    return WorktreeCleanupAction(str(path), branch, True, branch_deleted, reason)


def plan_removal(
    *,
    subject: Path,
    common_dir: Path,
    records: list[dict[str, Any]],
    dry_run: bool = False,
    protect_all: bool = False,
    protect_reason: str = "kept (--keep-worktrees)",
) -> list[WorktreeCleanupAction]:
    """Decide what happens to every one of ``records``' worktrees, and act unless dry-run.

    A record is removed only when: it is not protected, its capsule was never accepted, it
    is inside its campaign's declared worktree parent, it carries a loadable custody
    registration naming this exact worktree and branch (``launch_records``/
    ``load_registration``), the live Git inventory agrees the registration makes it
    ``bearhug-owned`` (not merely a ``bh-*`` name), it is not locked, and it is clean.
    Everything else is kept, with the reason named.
    """

    actions: list[WorktreeCleanupAction] = []
    registry = [r["registration"] for r in records if r.get("registration") is not None]
    try:
        inventory = inventory_worktrees(subject=subject, common_dir=common_dir, registry=registry)
    except WorktreeInventoryError as exc:
        return [
            _kept(
                r.get("worktree"), r.get("branch"), f"cannot read the Git worktree inventory: {exc}"
            )
            for r in records
            if r.get("worktree")
        ]
    by_path = {entry.path: entry for entry in inventory.entries}

    for record in records:
        worktree = record.get("worktree")
        branch = record.get("branch")
        if not worktree:
            continue
        path = Path(worktree)
        if protect_all:
            actions.append(_kept(path, branch, protect_reason))
            continue
        if record.get("accepted"):
            # `accepted` is never cleared by a later integration, so this cannot claim the
            # work is still *un*integrated -- only that it was accepted, which is reason
            # enough to leave the decision to an operator rather than guess.
            actions.append(_kept(path, branch, "it holds accepted capsule work"))
            continue
        if record.get("outside_worktree_parent"):
            actions.append(
                _kept(path, branch, "it is outside its campaign's declared worktree parent")
            )
            continue
        if record.get("registration") is None:
            actions.append(
                _kept(
                    path,
                    branch,
                    "no Bear Hug custody registration was found for it; "
                    "a bh-* name alone is not enough",
                )
            )
            continue
        entry = by_path.get(path)
        if entry is None:
            try:
                entry = by_path.get(path.resolve(strict=False))
            except OSError:
                entry = None
        if entry is None:
            actions.append(_kept(path, branch, "it is already absent from the Git worktree list"))
            continue
        if entry.custody != "bearhug-owned":
            actions.append(
                _kept(path, branch, f"Git custody reads {entry.custody!r}, not provably Bear Hug's")
            )
            continue
        if entry.locked:
            actions.append(
                _kept(path, branch, f"it is locked ({entry.locked_reason or 'no reason given'})")
            )
            continue
        base_oid = record.get("base_oid")
        if dry_run:
            status = _safe_git(path, *_STATUS_ARGS)
            if status is None:
                actions.append(_kept(path, branch, "cannot run Git to inspect the worktree"))
                continue
            if status.returncode != 0 or status.stdout:
                actions.append(_kept(path, branch, "it has uncommitted changes"))
                continue
            actions.append(
                WorktreeCleanupAction(str(path), branch, False, False, "would remove (dry run)")
            )
            continue
        actions.append(_remove(subject, path, branch, base_oid))
    return actions


def remove_missing_registered_entries(
    *,
    subject: Path,
    common_dir: Path,
    records: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[WorktreeCleanupAction]:
    """Sweep Git's *administrative* worktree entries whose own directory is already gone.

    ``git worktree prune`` acts on every entry in the repository this way, Bear Hug's or
    not, and does it silently -- exactly what a user's own worktree on a temporarily
    unmounted drive must never be exposed to (it holds that worktree's index; losing the
    entry loses staged work). This instead inspects each ``prunable`` entry one at a time:
    a Bear Hug-owned one (a ``bh-*`` branch *and* a registration in ``records`` naming it,
    the same custody rule ``plan_removal`` uses) is removed with ``git worktree remove
    --force`` -- forcing only ever overrides Git's refusal to prune a *locked* prunable
    entry, since there is no directory left to be dirty -- and every other prunable entry,
    Bear Hug's naming or not, is reported kept: "not Bear Hug's, left alone". A missing
    entry whose own launch record was accepted, or was launched outside its campaign's
    declared worktree parent, is kept for exactly the same reason ``plan_removal`` would
    keep it if its folder still existed: a missing folder is not permission to skip either
    check.
    """

    registry = [r["registration"] for r in records if r.get("registration") is not None]
    try:
        inventory = inventory_worktrees(subject=subject, common_dir=common_dir, registry=registry)
    except WorktreeInventoryError as exc:
        return [_kept(subject, None, f"cannot read the Git worktree inventory: {exc}")]
    by_resolved_path = {
        Path(r["worktree"]).resolve(strict=False): r for r in records if r.get("worktree")
    }

    actions: list[WorktreeCleanupAction] = []
    for entry in inventory.entries:
        if not entry.prunable:
            continue
        if entry.custody != "bearhug-owned":
            actions.append(_kept(entry.path, entry.branch, "not Bear Hug's, left alone"))
            continue
        record = by_resolved_path.get(entry.path)
        if record is not None and record.get("accepted"):
            actions.append(_kept(entry.path, entry.branch, "it holds accepted capsule work"))
            continue
        if record is not None and record.get("outside_worktree_parent"):
            actions.append(
                _kept(
                    entry.path, entry.branch,
                    "it is outside its campaign's declared worktree parent",
                )
            )
            continue
        if dry_run:
            actions.append(
                WorktreeCleanupAction(
                    str(entry.path), entry.branch, False, False,
                    "would remove its stale entry (dry run; its directory is already missing)",
                )
            )
            continue
        removal = _safe_git(subject, "worktree", "remove", "--force", str(entry.path))
        if removal is None:
            actions.append(_kept(entry.path, entry.branch, "cannot run Git to remove it"))
            continue
        if removal.returncode != 0:
            detail = removal.stderr.decode("utf-8", errors="replace").strip()
            actions.append(
                _kept(entry.path, entry.branch, f"git worktree remove --force refused it: {detail}")
            )
            continue
        branch_deleted = False
        reason = "removed its stale entry (its directory was already missing)"
        if entry.branch:
            known_base_oid = record.get("base_oid") if record is not None else None
            branch_deleted, note = _maybe_delete_branch(subject, entry.branch, known_base_oid)
            if not branch_deleted:
                reason = f"{reason}; {note}"
        actions.append(
            WorktreeCleanupAction(str(entry.path), entry.branch, True, branch_deleted, reason)
        )
    return actions


__all__ = [
    "WorktreeCleanupAction",
    "WorktreeCleanupError",
    "kept",
    "launch_records",
    "load_base_oid",
    "load_registration",
    "plan_removal",
    "remove_missing_registered_entries",
]
