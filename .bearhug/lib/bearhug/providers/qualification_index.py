"""Sealed local authority for exact provider binaries and capture-backed adapters.

Campaign typesets choose neutral model roles.  This module binds those roles to the exact local
provider executable, its version, the distributable fixture bundle, and the physically separate
private capture evidence that qualified that bundle.  The index is local Bear Hug state: it may
contain absolute paths, but never raw provider bytes or prompt/model prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bearhug.providers.compatibility import (
    CompatibilityAdapter,
    CompatibilityFixture,
    CompatibilityVersion,
    ProviderCompatibilityPolicy,
)
from bearhug.providers.qualification import (
    ProviderQualificationError,
    QualificationBundle,
    validate_qualification_bundle,
)


class ProviderQualificationIndexError(ValueError):
    """The local qualification authority is missing, stale, or ambiguous."""


_TOP = frozenset({"schema_version", "record_kind", "entries"})
_ENTRY = frozenset(
    {
        "provider",
        "adapter",
        "adapter_version",
        "executable",
        "executable_sha256",
        "bundle_root",
        "manifest_path",
        "capture_evidence_root",
        "settings_path",
        "settings_sha256",
        "rules_path",
        "rules_sha256",
    }
)
_PAIRS = {
    "claude": ("anthropic-claude", "claude-code-stream-json"),
    "codex": ("openai-codex", "codex-app-server-stdio"),
}
_MAX_INDEX_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderQualificationIndexError(f"qualification index repeats key {key!r}")
        result[key] = value
    return result


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProviderQualificationIndexError(f"{label} must be lowercase SHA-256")
    return value


def _text(value: Any, label: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="strict")) > maximum
        or "\x00" in value
    ):
        raise ProviderQualificationIndexError(f"{label} must be bounded non-empty text")
    return value


def _absolute(value: Any, label: str) -> Path:
    raw = _text(value, label)
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw:
        raise ProviderQualificationIndexError(f"{label} must be a canonical absolute path")
    try:
        if path.is_symlink():
            raise ProviderQualificationIndexError(f"{label} may not be a symlink")
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProviderQualificationIndexError(f"{label} is unavailable: {path}") from exc
    if resolved != path:
        raise ProviderQualificationIndexError(
            f"{label} must not contain aliases, parent traversal, or symlinked ancestors"
        )
    return resolved


def _relative(value: Any, label: str) -> str:
    raw = _text(value, label, maximum=512)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or raw.startswith("~")
        or raw.endswith("/")
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in raw.split("/"))
    ):
        raise ProviderQualificationIndexError(f"{label} must be repository-relative")
    return raw


def _read_regular(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProviderQualificationIndexError(f"cannot read {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > maximum
        ):
            raise ProviderQualificationIndexError(f"not a bounded user-owned file: {path}")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > maximum or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ProviderQualificationIndexError(f"file changed while reading: {path}")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _owned_directory(path: Path, label: str, *, private: bool) -> Path:
    if not path.is_dir():
        raise ProviderQualificationIndexError(f"{label} must be a physical directory")
    observed = path.stat(follow_symlinks=False)
    mode = stat.S_IMODE(observed.st_mode)
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid() or mode & forbidden:
        qualifier = "owner-only" if private else "not group/other writable"
        raise ProviderQualificationIndexError(f"{label} must be user-owned and {qualifier}: {path}")
    return path


@dataclass(frozen=True, slots=True)
class QualifiedProvider:
    selection_name: str
    provider: str
    adapter: str
    adapter_version: str
    executable: Path
    executable_sha256: str
    bundle_root: Path
    manifest_path: str
    capture_evidence_root: Path
    settings_path: Path
    settings_sha256: str
    rules_path: Path
    rules_sha256: str
    qualification: QualificationBundle

    def compatibility_policy(self) -> ProviderCompatibilityPolicy:
        evidence = self.qualification.compatibility_fixtures()
        rows = evidence.policy_fixtures(
            expected_provider=self.provider,
            expected_adapter=self.adapter,
            expected_adapter_version=self.adapter_version,
            expected_bundle_id=self.qualification.bundle_id,
            expected_manifest_sha256=self.qualification.manifest_sha256,
        )
        fixtures = tuple(
            CompatibilityFixture(
                path=row["path"],
                sha256=row["sha256"],
                scenario=row["scenario"],
                origin=row["origin"],
            )
            for row in rows
        )
        return ProviderCompatibilityPolicy(
            (
                CompatibilityAdapter(
                    self.adapter,
                    self.provider,
                    (CompatibilityVersion(self.adapter_version, "supported", fixtures, ()),),
                ),
            )
        )

    def revalidate_runtime_files(self) -> None:
        checks = (
            # Desktop Codex ships a universal macOS binary larger than 128 MiB.
            (self.executable, self.executable_sha256, "provider executable", 512 * 1024 * 1024),
            (self.settings_path, self.settings_sha256, "provider settings", _MAX_INDEX_BYTES),
            (self.rules_path, self.rules_sha256, "provider rules", _MAX_INDEX_BYTES),
        )
        for path, expected, label, maximum in checks:
            if _sha256(_read_regular(path, maximum=maximum)) != expected:
                raise ProviderQualificationIndexError(f"{label} digest changed: {path}")
        try:
            completed = subprocess.run(
                (str(self.executable), "--version"),
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderQualificationIndexError(
                f"cannot probe {self.selection_name} executable version: {exc}"
            ) from exc
        # Startup diagnostics on stderr are not part of the executable's version string.
        raw = completed.stdout.strip() or completed.stderr.strip()
        try:
            version = raw.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise ProviderQualificationIndexError("provider version output is not UTF-8") from exc
        if completed.returncode != 0 or version != self.adapter_version:
            raise ProviderQualificationIndexError(
                f"{self.selection_name} executable version changed: expected "
                f"{self.adapter_version!r}, observed {version!r}"
            )


@dataclass(frozen=True, slots=True)
class ProviderRecoveryBinding:
    """Sealed historical identity only: deliberately has no provider-launch methods."""

    selection_name: str
    adapter: str
    adapter_version: str
    settings_sha256: str
    rules_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderQualificationIndex:
    path: Path
    sha256: str
    providers: dict[str, QualifiedProvider]

    def recovery_binding(self, selection_name: str) -> ProviderRecoveryBinding:
        """Reopen only index-sealed execution identity, without a qualification claim.

        Historical raw/request/argv/candidate custody is verified by the recovery caller. An
        external manifest, fixture or capture is not needed to dispose of that existing custody;
        none of this binding can be used to launch or qualify new work.
        """
        bound = load_bound_provider_qualification_index(self.path)
        if bound.sha256 != self.sha256:
            raise ProviderQualificationIndexError("qualification index changed after binding")
        _path, _raw, value = _index_document(self.path)
        if _sha256(_raw) != self.sha256:
            raise ProviderQualificationIndexError("qualification index changed while binding")
        for entry in value["entries"]:
            if entry["provider"] == selection_name:
                return ProviderRecoveryBinding(
                    selection_name,
                    entry["adapter"],
                    entry["adapter_version"],
                    entry["settings_sha256"],
                    entry["rules_sha256"],
                )
        raise ProviderQualificationIndexError("sealed recovery provider is absent")

    def require(self, selection_name: str) -> QualifiedProvider:
        current = _read_regular(self.path, maximum=_MAX_INDEX_BYTES)
        if _sha256(current) != self.sha256:
            raise ProviderQualificationIndexError("qualification index changed after binding")
        # Reloading revalidates the manifest, every distributable fixture, and the private capture
        # evidence as well as the executable/config files.  Do not rely on the dataclass snapshot
        # across an operator edit or ordinary file loss between HIL and provider spend.
        refreshed = load_provider_qualification_index(self.path)
        if refreshed.sha256 != self.sha256:
            raise ProviderQualificationIndexError("qualification index changed while revalidating")
        try:
            provider = refreshed.providers[selection_name]
        except KeyError as exc:
            raise ProviderQualificationIndexError(
                f"qualification index has no {selection_name!r} entry"
            ) from exc
        return provider


def _index_document(path: Path | str) -> tuple[Path, bytes, dict[str, Any]]:
    requested = Path(path).expanduser()
    index_path = _absolute(str(requested), "qualification index")
    raw = _read_regular(index_path, maximum=_MAX_INDEX_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderQualificationIndexError(
            f"qualification index is not one UTF-8 JSON object: {exc}"
        ) from exc
    if not isinstance(value, dict) or set(value) != _TOP:
        raise ProviderQualificationIndexError("qualification index is not closed")
    if value["schema_version"] != "1" or value["record_kind"] != "provider_qualification_index":
        raise ProviderQualificationIndexError("unsupported qualification index identity")
    return index_path, raw, value


def load_provider_qualification_index(path: Path | str) -> ProviderQualificationIndex:
    index_path, raw, value = _index_document(path)
    return _validate_index_document(index_path, raw, value)


def load_bound_provider_qualification_index(path: Path | str) -> ProviderQualificationIndex:
    """Load only the sealed index identity for non-spending recovery.

    Recovery must remain possible after custody is acquired even when a provider executable,
    settings file, rules file, fixture bundle, or private capture has since changed.  The exact
    index bytes and their closed bindings are still validated, but external qualification
    resources are deliberately not dereferenced and no provider can be selected from this view.
    """

    index_path, raw, value = _index_document(path)
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise ProviderQualificationIndexError("qualification index must contain entries")
    selections: set[str] = set()
    for ordinal, entry in enumerate(entries):
        label = f"entries[{ordinal}]"
        if not isinstance(entry, dict) or set(entry) != _ENTRY:
            raise ProviderQualificationIndexError(f"{label} is not closed")
        selection = _text(entry["provider"], f"{label}.provider", maximum=32)
        if selection not in _PAIRS or selection in selections:
            raise ProviderQualificationIndexError(f"{label}.provider is unsupported or duplicated")
        selections.add(selection)
        if _text(entry["adapter"], f"{label}.adapter", maximum=128) != _PAIRS[selection][1]:
            raise ProviderQualificationIndexError(f"{label} crosses provider/adapter identity")
        _text(entry["adapter_version"], f"{label}.adapter_version", maximum=128)
        for field in (
            "executable",
            "bundle_root",
            "capture_evidence_root",
            "settings_path",
            "rules_path",
        ):
            raw_path = _text(entry[field], f"{label}.{field}")
            candidate = Path(raw_path)
            if not candidate.is_absolute() or str(candidate) != raw_path:
                raise ProviderQualificationIndexError(
                    f"{label}.{field} must be a canonical absolute path"
                )
        _relative(entry["manifest_path"], f"{label}.manifest_path")
        for field in ("executable_sha256", "settings_sha256", "rules_sha256"):
            _digest(entry[field], f"{label}.{field}")
    return ProviderQualificationIndex(index_path, _sha256(raw), {})


def bind_current_provider_runtime(
    path: Path | str, selection_name: str
) -> tuple[str, dict[str, Any]]:
    """Propose current settings/rules for a new campaign without rewriting capture authority.

    A provider's user settings and rules files change in ordinary use (Claude Code rewrites
    `~/.claude/settings.json` on a model switch; Codex rewrites `config.toml`). Freezing their
    digests at capture time would turn every such edit into a re-qualification, so onboarding
    binds the *current* digests into a reviewed, separate campaign index instead. Adapter
    fixtures, private captures, executable and version still require full validation here. The
    caller must seal this proposal into its reviewed draft before execution; existing campaign
    indexes continue using strict runtime-file validation.
    """
    if selection_name not in _PAIRS:
        raise ProviderQualificationIndexError(f"unsupported provider selection: {selection_name}")
    index_path, raw, value = _index_document(path)
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise ProviderQualificationIndexError("qualification index must contain entries")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("provider") == selection_name:
            for source in ("settings", "rules"):
                source_path = _absolute(entry.get(f"{source}_path"), f"provider {source}")
                # Check historical digest syntax even though the new run binds current bytes.
                _digest(entry.get(f"{source}_sha256"), f"provider {source} digest")
                entry[f"{source}_sha256"] = _sha256(
                    _read_regular(source_path, maximum=_MAX_INDEX_BYTES)
                )
    validated = _validate_index_document(index_path, raw, value)
    if selection_name not in validated.providers:
        raise ProviderQualificationIndexError(
            f"qualification index has no {selection_name!r} entry"
        )
    return _sha256(raw), value


def bind_current_codex_runtime(path: Path | str) -> tuple[str, dict[str, Any]]:
    """Codex-only name kept for existing callers; see `bind_current_provider_runtime`."""
    return bind_current_provider_runtime(path, "codex")


def _validate_index_document(
    index_path: Path, raw: bytes, value: dict[str, Any]
) -> ProviderQualificationIndex:
    entries = value["entries"]
    if not isinstance(entries, list) or not entries:
        raise ProviderQualificationIndexError("qualification index must contain entries")
    providers: dict[str, QualifiedProvider] = {}
    for ordinal, entry in enumerate(entries):
        label = f"entries[{ordinal}]"
        if not isinstance(entry, dict) or set(entry) != _ENTRY:
            raise ProviderQualificationIndexError(f"{label} is not closed")
        selection_name = _text(entry["provider"], f"{label}.provider", maximum=32)
        if selection_name not in _PAIRS or selection_name in providers:
            raise ProviderQualificationIndexError(f"{label}.provider is unsupported or duplicated")
        provider_id, expected_adapter = _PAIRS[selection_name]
        adapter = _text(entry["adapter"], f"{label}.adapter", maximum=128)
        if adapter != expected_adapter:
            raise ProviderQualificationIndexError(f"{label} crosses provider/adapter identity")
        adapter_version = _text(entry["adapter_version"], f"{label}.adapter_version", maximum=128)
        executable = _absolute(entry["executable"], f"{label}.executable")
        executable_sha256 = _digest(entry["executable_sha256"], f"{label}.executable_sha256")
        bundle_root = _owned_directory(
            _absolute(entry["bundle_root"], f"{label}.bundle_root"),
            f"{label}.bundle_root",
            private=False,
        )
        manifest_path = _relative(entry["manifest_path"], f"{label}.manifest_path")
        manifest_file = (bundle_root / manifest_path).resolve()
        if bundle_root != manifest_file and bundle_root not in manifest_file.parents:
            raise ProviderQualificationIndexError(f"{label}.manifest_path escapes bundle root")
        manifest_raw = _read_regular(manifest_file, maximum=_MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"), object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderQualificationIndexError(
                f"{label} manifest is not one UTF-8 JSON object: {exc}"
            ) from exc
        capture_root = _owned_directory(
            _absolute(entry["capture_evidence_root"], f"{label}.capture_evidence_root"),
            f"{label}.capture_evidence_root",
            private=True,
        )
        settings_path = _absolute(entry["settings_path"], f"{label}.settings_path")
        settings_sha256 = _digest(entry["settings_sha256"], f"{label}.settings_sha256")
        rules_path = _absolute(entry["rules_path"], f"{label}.rules_path")
        rules_sha256 = _digest(entry["rules_sha256"], f"{label}.rules_sha256")
        try:
            qualification = validate_qualification_bundle(
                manifest,
                bundle_root,
                expected_provider=provider_id,
                expected_adapter=adapter,
                expected_adapter_version=adapter_version,
                capture_evidence_root=capture_root,
            )
            qualification.compatibility_fixtures()
        except ProviderQualificationError as exc:
            raise ProviderQualificationIndexError(
                f"{label} is not qualification-ready: {exc}"
            ) from exc
        qualified = QualifiedProvider(
            selection_name=selection_name,
            provider=provider_id,
            adapter=adapter,
            adapter_version=adapter_version,
            executable=executable,
            executable_sha256=executable_sha256,
            bundle_root=bundle_root,
            manifest_path=manifest_path,
            capture_evidence_root=capture_root,
            settings_path=settings_path,
            settings_sha256=settings_sha256,
            rules_path=rules_path,
            rules_sha256=rules_sha256,
            qualification=qualification,
        )
        qualified.revalidate_runtime_files()
        providers[selection_name] = qualified
    return ProviderQualificationIndex(index_path, _sha256(raw), providers)


__all__ = [
    "ProviderQualificationIndex",
    "ProviderQualificationIndexError",
    "QualifiedProvider",
    "load_bound_provider_qualification_index",
    "load_provider_qualification_index",
    "bind_current_codex_runtime",
    "bind_current_provider_runtime",
]
