"""Local immutable capsule objects; run state carries their exact digests.

This store has no 'latest' lookup and grants no execution authority. Identifiers never become
paths. Plan activation and mutable run state belong to the controller.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from bearhug.campaign.capsules import (
    CapsuleContractError,
    canonical_capsule_bytes,
    validate_capsule_plan,
    validate_capsule_record,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 256 * 1024 * 1024


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapsuleContractError(f"stored record repeats JSON key {key!r}")
        result[key] = value
    return result


class CapsuleObjectStore:
    """One canonical object per digest in an explicitly selected private local directory."""

    def __init__(self, root: Path, *, create: bool = False):
        root = Path(root).absolute()
        if ".." in root.parts or root.is_symlink():
            raise CapsuleContractError("capsule store must be a physical directory")
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root
        # Resolve system aliases (e.g. /tmp) once; reject a symlink at the store itself.
        with self._directory():
            self.root = root.resolve(strict=True)

    @contextmanager
    def _directory(self):
        descriptor = None
        try:
            descriptor = os.open(
                self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise CapsuleContractError("capsule store must be owner-only and user-owned")
            yield descriptor
        except OSError as exc:
            raise CapsuleContractError(f"capsule store I/O failed: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _name(digest: str) -> str:
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise CapsuleContractError("capsule object reference must be a SHA-256 digest")
        return f"{digest}.json"

    def put(self, record: Mapping[str, Any]) -> str:
        validated = validate_capsule_record(record)
        raw = validated.canonical_bytes
        if len(raw) > _MAX_BYTES:
            raise CapsuleContractError("capsule object exceeds the 4 MiB limit")
        name = self._name(validated.digest)
        temporary = f".pending-{secrets.token_hex(16)}"
        with self._directory() as directory:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    # Idempotent only when the existing immutable bytes still verify.
                    self.get(validated.digest)
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=directory)
        return validated.digest

    def get(self, digest: str, *, record_kind: str | None = None) -> dict[str, Any]:
        name = self._name(digest)
        with self._directory() as directory:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                    raise CapsuleContractError("capsule object must be a user-owned regular file")
                raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise CapsuleContractError("capsule object exceeds the 4 MiB limit")
        try:
            record = json.loads(raw, object_pairs_hook=_pairs)
        except CapsuleContractError:
            raise
        except (UnicodeError, ValueError) as exc:
            raise CapsuleContractError("capsule object is not UTF-8 JSON") from exc
        validated = validate_capsule_record(record)
        if validated.digest != digest or canonical_capsule_bytes(record) != raw:
            raise CapsuleContractError("capsule object has wrong digest or noncanonical bytes")
        if record_kind is not None and validated.record_kind != record_kind:
            raise CapsuleContractError(f"capsule object must have kind {record_kind!r}")
        return record

    def resolve_plan(
        self, digest: str, *, expected_subject: Mapping[str, Any], expected_revision_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve a pinned plan and its complete predecessor chain, checking each transition."""
        seen: set[str] = set()
        chain: list[tuple[dict[str, Any], dict[str, Any]]] = []
        cursor: str | None = digest
        while cursor is not None:
            if cursor in seen:
                raise CapsuleContractError("capsule plan predecessor cycle")
            seen.add(cursor)
            plan = self.get(cursor, record_kind="capsule_plan")
            if plan["subject"] != expected_subject:
                raise CapsuleContractError("capsule plan subject differs from run binding")
            intent = self.get(plan["intent_envelope_sha256"], record_kind="intent_envelope")
            chain.append((plan, intent))
            cursor = plan["revision"]["predecessor_sha256"]
        predecessor = None
        for plan, intent in reversed(chain):
            validate_capsule_plan(plan, intent_envelope=intent, previous_plan=predecessor)
            predecessor = plan
        plan, intent = chain[0]
        if plan["revision"]["revision_id"] != expected_revision_id:
            raise CapsuleContractError("capsule plan revision is stale for this run binding")
        return plan, intent
