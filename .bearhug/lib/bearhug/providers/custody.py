"""Durable lookup from canonical provider receipts to their private raw custody.

The campaign verifier receives privacy-bounded receipts, not transport objects.  This store lets it
reopen the exact receipt/request/raw-event bytes after a controller restart without scanning for a
"newest" provider run.  When a receipt links runtime attestation, the store also reopens and
validates that attestation against the provider/session and raw/request custody. Records are
create-only and keyed by the canonical receipt digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from bearhug.providers.receipt import ProviderReceiptError, validate_provider_receipt
from bearhug.providers.runtime_attestation import (
    RuntimeAttestationError,
    validate_runtime_attestation,
)


class ProviderCustodyError(ValueError):
    """Provider custody cannot be correlated or has changed since publication."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "provider",
        "adapter",
        "session_id",
        "cwd",
        "receipt_sha256",
        "receipt_path",
        "receipt_file_sha256",
        "raw_events_path",
        "raw_events_sha256",
        "request_path",
        "request_sha256",
        "argv_path",
        "argv_sha256",
        "stderr_path",
        "stderr_sha256",
        "operational_evidence_path",
        "operational_evidence_sha256",
        "runtime_attestation_path",
        "runtime_attestation_sha256",
        "content_sha256",
    }
)
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_RECORD_BYTES = 64 * 1024


def _canonical(value: Mapping[str, Any], *, omit_digest: bool = False) -> bytes:
    material = dict(value)
    if omit_digest:
        material.pop("content_sha256", None)
    try:
        return (
            json.dumps(
                material,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderCustodyError(f"provider custody is not canonical JSON: {exc}") from exc


def _receipt_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).rstrip(b"\n")).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_argv(raw: bytes, expected_digest: str) -> None:
    """Validate the canonical, bounded argv bytes paired with a provider receipt."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value {value}")

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderCustodyError(f"provider argv is not canonical JSON: {exc}") from exc
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in value
        )
        or json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") != raw
    ):
        raise ProviderCustodyError("provider argv is not a canonical non-empty string array")
    if _sha256(raw) != expected_digest:
        raise ProviderCustodyError("provider argv bytes do not match provider receipt")


def _read_regular(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProviderCustodyError(f"cannot read provider custody artifact {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size > maximum
        ):
            raise ProviderCustodyError(
                f"provider custody artifact is not a bounded user-owned file: {path}"
            )
        value = bytearray()
        while len(value) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        after = os.fstat(descriptor)
        if len(value) > maximum or (
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
            raise ProviderCustodyError(f"provider custody artifact changed while reading: {path}")
        return bytes(value)
    finally:
        os.close(descriptor)


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderCustodyError(f"provider custody JSON repeats key {key!r}")
        result[key] = value
    return result


def _private_directory(path: Path, *, create: bool) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.is_symlink():
            raise ProviderCustodyError(f"provider custody directory may not be a symlink: {path}")
        resolved = path.resolve(strict=True)
        observed = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ProviderCustodyError(f"provider custody directory is unavailable: {path}") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or stat.S_IMODE(observed.st_mode) & 0o077
    ):
        raise ProviderCustodyError(
            f"provider custody directory must be owner-only and user-owned: {resolved}"
        )
    return resolved


def _inside(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProviderCustodyError(f"{label} must be a non-empty absolute path")
    requested = Path(value)
    if not requested.is_absolute() or str(requested) != value or requested.is_symlink():
        raise ProviderCustodyError(f"{label} must be a physical canonical absolute path")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ProviderCustodyError(f"{label} is unavailable: {requested}") from exc
    if root != resolved and root not in resolved.parents:
        raise ProviderCustodyError(f"{label} escapes the provider output root")
    relative = resolved.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ProviderCustodyError(f"{label} traverses a symlink")
    return resolved


def _create_only(path: Path, value: Mapping[str, Any]) -> None:
    raw = _canonical(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ProviderCustodyError(
                f"refusing to replace provider custody record {path}"
            ) from exc
        parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _run_paths(
    run: Any,
) -> tuple[Path, Path, Path, Path, Path, Path | None, Path | None]:
    receipt = getattr(run, "receipt_path", None)
    stderr = getattr(run, "stderr_path", None)
    raw = getattr(run, "raw_events_path", None)
    request = getattr(run, "request_path", None)
    argv = getattr(run, "argv_path", None)
    runtime_attestation = getattr(run, "runtime_attestation_path", None)
    operational_evidence = getattr(run, "operational_evidence_path", None)
    if raw is None:
        raw = getattr(run, "server_events_path", None)
    if request is None:
        request = getattr(run, "client_events_path", None)
    if not all(isinstance(path, Path) for path in (receipt, raw, request, argv, stderr)) or (
        runtime_attestation is not None and not isinstance(runtime_attestation, Path)
    ) or (
        operational_evidence is not None and not isinstance(operational_evidence, Path)
    ):
        raise ProviderCustodyError(
            "provider run does not expose complete receipt/raw/request custody"
        )
    return receipt, raw, request, argv, stderr, runtime_attestation, operational_evidence


def _operational_evidence_file(
    path: Path,
    *,
    receipt: Mapping[str, Any],
    expected_digest: str,
    raw_events: bytes,
    request: bytes,
    argv: bytes,
    stderr: bytes,
    expected_executable_sha256: str | None = None,
) -> bytes:
    """Reopen and revalidate the boundary-owned operational evidence record.

    The validator receives all raw custody bytes so it can prove exact executable/request
    identity, provider-reported settings and hook responses rather than accepting a copied
    eligibility boolean.  A missing validator is a hard error for linked evidence.
    """

    # This artifact embeds provider responses (including hooks and thread observations),
    # so it can exceed the small receipt/index limit during an ordinary successful turn.
    raw = _read_regular(path, maximum=_MAX_ARTIFACT_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderCustodyError(
            f"operational evidence file is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise ProviderCustodyError("operational evidence file is not canonical JSON")
    content_digest = value.get("content_sha256")
    if not isinstance(content_digest, str) or _sha256(_canonical({
        key: item for key, item in value.items() if key != "content_sha256"
    })) != content_digest:
        raise ProviderCustodyError("operational evidence content digest is false")
    try:
        from bearhug.providers.operational_evidence_dispatch import (
            validate_linked_operational_evidence,
        )
    except (ImportError, AttributeError) as exc:
        raise ProviderCustodyError(
            "linked operational evidence validator is unavailable"
        ) from exc
    try:
        evidence = validate_linked_operational_evidence(
            value,
            receipt=receipt,
            raw_events=raw_events,
            request=request,
            argv=argv,
            stderr=stderr,
            expected_executable_sha256=expected_executable_sha256,
        )
    # The dispatcher's own routed validators are imported
    # inside its function body (deliberately, for the `sys.modules` swap pattern its docstring
    # explains), one level below the import this function's own guard just above wraps -- a
    # missing or broken provider-specific validator module used to escape this call as a bare
    # ImportError instead of the ProviderCustodyError this docstring promises. The dispatcher now
    # catches that itself, at its own import site,
    # and raises OperationalEvidenceDispatchError (a ValueError subclass, caught below) naming
    # the validator unavailable specifically -- narrower than this clause, which previously also
    # caught (and relabelled "validator is unavailable") an AttributeError raised anywhere
    # *inside* a validator's own logic on a tampered record, not only a missing import. Folded
    # into the same "operational evidence is invalid" branch as ValueError/TypeError instead, so
    # a validator-logic AttributeError is still handled (never an uncaught crash out of custody)
    # but never misreported as a missing module.
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProviderCustodyError(f"operational evidence is invalid: {exc}") from exc
    if not isinstance(evidence, Mapping) or evidence != value:
        raise ProviderCustodyError("operational evidence validator changed its record")
    if evidence.get("content_sha256") != expected_digest:
        raise ProviderCustodyError("operational evidence digest does not match provider receipt")
    return raw


def _runtime_attestation_file(
    path: Path,
    *,
    receipt: Mapping[str, Any],
    expected_digest: str,
) -> bytes:
    raw = _read_regular(path, maximum=_MAX_RECORD_BYTES)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderCustodyError(
            f"runtime attestation file is invalid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise ProviderCustodyError("runtime attestation file is not canonical JSON")
    try:
        attestation = validate_runtime_attestation(value)
    except RuntimeAttestationError as exc:
        raise ProviderCustodyError(f"runtime attestation is invalid: {exc}") from exc
    if attestation["content_sha256"] != expected_digest:
        raise ProviderCustodyError("runtime attestation digest does not match provider receipt")
    for field in (
        "provider",
        "adapter",
        "adapter_version",
        "session_id",
        "thread_id",
        "turn_id",
    ):
        if attestation[field] != receipt[field]:
            raise ProviderCustodyError(f"runtime attestation {field} differs from provider receipt")
    if (
        attestation["raw_event_sha256"] != receipt["raw_event_sha256"]
        or attestation["input_sha256"] != receipt["request_sha256"]
        or (
            receipt["identity"]["execution_identity_attestation"] == "per_turn_attested"
            and not attestation["promotion_eligible"]
        )
    ):
        raise ProviderCustodyError("runtime attestation evidence differs from provider receipt")
    return raw


class ProviderCustodyStore:
    """Create-only receipt index and restart-safe verifier callback."""

    def __init__(
        self,
        state_root: Path | str,
        provider_output_root: Path | str,
        *,
        qualification_index: Any = None,
    ) -> None:
        self.root = _private_directory(Path(state_root).expanduser(), create=True)
        self.provider_output_root = _private_directory(
            Path(provider_output_root).expanduser(), create=True
        )
        # Optional so every existing test/offline construction
        # site keeps compiling; all three production construction sites
        # (`capsule_campaign.py` x2, `capsule_runtime.py` x1) must supply one -- a Claude record
        # reaching validation through a store with no index, with a receipt attached, refuses
        # with a named error rather than validating the executable digest against itself.
        self.qualification_index = qualification_index

    def _expected_claude_executable_sha256(self, receipt: Mapping[str, Any]) -> str | None:
        if self.qualification_index is None or receipt.get("provider") != "anthropic-claude":
            return None
        qualified = self.qualification_index.providers.get("claude")
        return qualified.executable_sha256 if qualified is not None else None

    def _path(self, receipt_sha256: str) -> Path:
        if _SHA256.fullmatch(receipt_sha256) is None:
            raise ProviderCustodyError("receipt digest must be lowercase SHA-256")
        return self.root / f"{receipt_sha256}.json"

    def record_run(self, run: Any) -> dict[str, Any]:
        try:
            receipt = validate_provider_receipt(dict(run.receipt))
        except (AttributeError, TypeError, ProviderReceiptError) as exc:
            raise ProviderCustodyError(f"provider run has no valid receipt: {exc}") from exc
        receipt_sha256 = _receipt_digest(receipt)
        (
            receipt_path,
            raw_path,
            request_path,
            argv_path,
            stderr_path,
            runtime_attestation_path,
            operational_evidence_path,
        ) = _run_paths(run)
        receipt_path, raw_path, request_path, argv_path, stderr_path = (
            _inside(self.provider_output_root, str(path.resolve()), label)
            for path, label in zip(
                (receipt_path, raw_path, request_path, argv_path, stderr_path),
                (
                    "receipt_path",
                    "raw_events_path",
                    "request_path",
                    "argv_path",
                    "stderr_path",
                ),
                strict=True,
            )
        )
        receipt_raw = _read_regular(receipt_path, maximum=_MAX_RECORD_BYTES)
        try:
            on_disk = json.loads(receipt_raw.decode("utf-8"), object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError(f"provider receipt file is invalid JSON: {exc}") from exc
        if on_disk != receipt:
            raise ProviderCustodyError("provider receipt object differs from its durable file")
        raw = _read_regular(raw_path, maximum=_MAX_ARTIFACT_BYTES)
        request = _read_regular(request_path, maximum=_MAX_ARTIFACT_BYTES)
        argv = _read_regular(argv_path, maximum=_MAX_RECORD_BYTES)
        stderr = _read_regular(stderr_path, maximum=_MAX_ARTIFACT_BYTES)
        if _sha256(raw) != receipt["raw_event_sha256"]:
            raise ProviderCustodyError("raw event bytes do not match provider receipt")
        if _sha256(request) != receipt["request_sha256"]:
            raise ProviderCustodyError("request bytes do not match provider receipt")
        _validate_argv(argv, receipt["launch"]["argv_sha256"])
        operational_path: Path | None = None
        operational_raw: bytes | None = None
        operational_digest = receipt.get("operational_evidence_sha256")
        if operational_digest is not None:
            if operational_evidence_path is None:
                raise ProviderCustodyError(
                    "provider receipt links operational evidence without durable custody"
                )
            operational_path = _inside(
                self.provider_output_root,
                str(operational_evidence_path.resolve()),
                "operational_evidence_path",
            )
            operational_raw = _operational_evidence_file(
                operational_path,
                receipt=receipt,
                expected_digest=operational_digest,
                raw_events=raw,
                request=request,
                argv=argv,
                stderr=stderr,
                expected_executable_sha256=self._expected_claude_executable_sha256(receipt),
            )
        elif operational_evidence_path is not None:
            raise ProviderCustodyError(
                "provider run exposes operational evidence without a receipt link"
            )
        attestation_path: Path | None = None
        attestation_raw: bytes | None = None
        attestation_digest = receipt.get("runtime_attestation_sha256")
        if attestation_digest is not None:
            if runtime_attestation_path is None:
                raise ProviderCustodyError(
                    "provider receipt links runtime attestation without durable custody"
                )
            attestation_path = _inside(
                self.provider_output_root,
                str(runtime_attestation_path.resolve()),
                "runtime_attestation_path",
            )
            attestation_raw = _runtime_attestation_file(
                attestation_path,
                receipt=receipt,
                expected_digest=attestation_digest,
            )
        elif runtime_attestation_path is not None:
            raise ProviderCustodyError(
                "provider run exposes runtime attestation without a receipt link"
            )
        record: dict[str, Any] = {
            "schema_version": "1",
            "record_kind": "provider_custody_record",
            "provider": receipt["provider"],
            "adapter": receipt["adapter"],
            "session_id": receipt["session_id"],
            "cwd": receipt["cwd"],
            "receipt_sha256": receipt_sha256,
            "receipt_path": str(receipt_path),
            "receipt_file_sha256": _sha256(receipt_raw),
            "raw_events_path": str(raw_path),
            "raw_events_sha256": _sha256(raw),
            "request_path": str(request_path),
            "request_sha256": _sha256(request),
            "argv_path": str(argv_path),
            "argv_sha256": _sha256(argv),
            "stderr_path": str(stderr_path),
            "stderr_sha256": _sha256(stderr),
            "operational_evidence_path": (
                str(operational_path) if operational_raw is not None else None
            ),
            "operational_evidence_sha256": (
                operational_digest if operational_raw is not None else None
            ),
            "runtime_attestation_path": (
                str(attestation_path) if attestation_raw is not None else None
            ),
            "runtime_attestation_sha256": (
                attestation_digest if attestation_raw is not None else None
            ),
        }
        record["content_sha256"] = _sha256(_canonical(record))
        path = self._path(receipt_sha256)
        if path.exists() or path.is_symlink():
            existing = self._read_record(path)
            if existing != record:
                raise ProviderCustodyError("provider receipt already has different custody")
        else:
            _create_only(path, record)
        return record

    def read_operational_evidence(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Return the linked operational evidence record as a parsed mapping.

        ``validate`` and ``record_run`` can only prove a linked record is valid; neither hands
        the parsed record back to its caller. This calls the same private
        ``_operational_evidence_file`` helper they use and ``json.loads`` its returned canonical
        bytes -- nothing in the validation path changes, so a record that fails validation still
        raises ``ProviderCustodyError`` here exactly as it does from ``validate``.
        """

        try:
            receipt_value = validate_provider_receipt(dict(receipt))
        except (TypeError, ProviderReceiptError) as exc:
            raise ProviderCustodyError(f"provider evidence receipt is invalid: {exc}") from exc
        receipt_sha256 = _receipt_digest(receipt_value)
        record = self._read_record(self._path(receipt_sha256))
        operational_digest = record["operational_evidence_sha256"]
        operational_evidence_path = record["operational_evidence_path"]
        if operational_digest is None or operational_evidence_path is None:
            raise ProviderCustodyError("provider custody record has no linked operational evidence")
        raw_path = _inside(self.provider_output_root, record["raw_events_path"], "raw_events_path")
        request_path = _inside(self.provider_output_root, record["request_path"], "request_path")
        argv_path = _inside(self.provider_output_root, record["argv_path"], "argv_path")
        stderr_path = _inside(self.provider_output_root, record["stderr_path"], "stderr_path")
        operational_path = _inside(
            self.provider_output_root, operational_evidence_path, "operational_evidence_path"
        )
        raw = _operational_evidence_file(
            operational_path,
            receipt=receipt_value,
            expected_digest=operational_digest,
            raw_events=_read_regular(raw_path, maximum=_MAX_ARTIFACT_BYTES),
            request=_read_regular(request_path, maximum=_MAX_ARTIFACT_BYTES),
            argv=_read_regular(argv_path, maximum=_MAX_RECORD_BYTES),
            stderr=_read_regular(stderr_path, maximum=_MAX_ARTIFACT_BYTES),
            expected_executable_sha256=self._expected_claude_executable_sha256(receipt_value),
        )
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError(f"operational evidence file is invalid JSON: {exc}") from exc

    def _read_record(self, path: Path) -> dict[str, Any]:
        raw = _read_regular(path, maximum=_MAX_RECORD_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError(f"provider custody record is invalid JSON: {exc}") from exc
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ProviderCustodyError("provider custody record is not closed")
        if (
            value["schema_version"] != "1"
            or value["record_kind"] != "provider_custody_record"
            or value["content_sha256"] != _sha256(_canonical(value, omit_digest=True))
        ):
            raise ProviderCustodyError("provider custody record identity or digest changed")
        for field in (
            "receipt_sha256",
            "receipt_file_sha256",
            "raw_events_sha256",
            "request_sha256",
            "argv_sha256",
            "stderr_sha256",
        ):
            if not isinstance(value[field], str) or _SHA256.fullmatch(value[field]) is None:
                raise ProviderCustodyError(f"provider custody {field} is not SHA-256")
        if value["operational_evidence_sha256"] is None:
            if value["operational_evidence_path"] is not None:
                raise ProviderCustodyError(
                    "operational evidence path exists without its digest"
                )
        elif (
            _SHA256.fullmatch(value["operational_evidence_sha256"]) is None
            or not isinstance(value["operational_evidence_path"], str)
            or not value["operational_evidence_path"]
        ):
            raise ProviderCustodyError("operational evidence digest or path is invalid")
        if value["runtime_attestation_sha256"] is None:
            if value["runtime_attestation_path"] is not None:
                raise ProviderCustodyError("runtime attestation path exists without its digest")
        elif (
            _SHA256.fullmatch(value["runtime_attestation_sha256"]) is None
            or not isinstance(value["runtime_attestation_path"], str)
            or not value["runtime_attestation_path"]
        ):
            raise ProviderCustodyError("runtime attestation digest has no path")
        return value

    def validate(self, _label: str, receipt_value: Mapping[str, Any]) -> None:
        try:
            receipt = validate_provider_receipt(dict(receipt_value))
        except (TypeError, ProviderReceiptError) as exc:
            raise ProviderCustodyError(f"provider evidence receipt is invalid: {exc}") from exc
        receipt_sha256 = _receipt_digest(receipt)
        path = self._path(receipt_sha256)
        record = self._read_record(path)
        if (
            path.name != f"{record['receipt_sha256']}.json"
            or record["receipt_sha256"] != receipt_sha256
        ):
            raise ProviderCustodyError("provider custody record has foreign receipt identity")
        for field in ("provider", "adapter", "session_id", "cwd"):
            if record[field] != receipt[field]:
                raise ProviderCustodyError(f"provider custody {field} differs from receipt")
        if record["operational_evidence_sha256"] != receipt.get(
            "operational_evidence_sha256"
        ):
            raise ProviderCustodyError(
                "provider custody operational evidence differs from receipt"
            )
        receipt_path = _inside(
            self.provider_output_root, record["receipt_path"], "receipt_path"
        )
        raw_path = _inside(
            self.provider_output_root, record["raw_events_path"], "raw_events_path"
        )
        request_path = _inside(
            self.provider_output_root, record["request_path"], "request_path"
        )
        argv_path = _inside(self.provider_output_root, record["argv_path"], "argv_path")
        stderr_path = _inside(
            self.provider_output_root, record["stderr_path"], "stderr_path"
        )
        operational_evidence_path = None
        if record["operational_evidence_path"] is not None:
            operational_evidence_path = _inside(
                self.provider_output_root,
                record["operational_evidence_path"],
                "operational_evidence_path",
            )
        attestation_path = None
        if record["runtime_attestation_path"] is not None:
            attestation_path = _inside(
                self.provider_output_root,
                record["runtime_attestation_path"],
                "runtime_attestation_path",
            )
        receipt_raw = _read_regular(receipt_path, maximum=_MAX_RECORD_BYTES)
        raw = _read_regular(raw_path, maximum=_MAX_ARTIFACT_BYTES)
        request = _read_regular(request_path, maximum=_MAX_ARTIFACT_BYTES)
        argv = _read_regular(argv_path, maximum=_MAX_RECORD_BYTES)
        stderr = _read_regular(stderr_path, maximum=_MAX_ARTIFACT_BYTES)
        if operational_evidence_path is not None:
            _operational_evidence_file(
                operational_evidence_path,
                receipt=receipt,
                expected_digest=record["operational_evidence_sha256"],
                raw_events=raw,
                request=request,
                argv=argv,
                stderr=stderr,
                expected_executable_sha256=self._expected_claude_executable_sha256(receipt),
            )
        elif receipt.get("operational_evidence_sha256") is not None:
            raise ProviderCustodyError(
                "provider receipt operational evidence has no custody path"
            )
        elif record["operational_evidence_sha256"] is not None:
            raise ProviderCustodyError(
                "provider custody operational evidence path is incomplete"
            )
        checks = (
            (_sha256(receipt_raw), record["receipt_file_sha256"], "receipt file"),
            (_sha256(raw), record["raw_events_sha256"], "raw events"),
            (_sha256(request), record["request_sha256"], "request"),
            (_sha256(argv), record["argv_sha256"], "argv"),
            (_sha256(stderr), record["stderr_sha256"], "stderr"),
        )
        for observed, expected, label in checks:
            if observed != expected:
                raise ProviderCustodyError(f"provider {label} changed after publication")
        if attestation_path is not None:
            _runtime_attestation_file(
                attestation_path,
                receipt=receipt,
                expected_digest=record["runtime_attestation_sha256"],
            )
        elif receipt.get("runtime_attestation_sha256") is not None:
            raise ProviderCustodyError("provider receipt runtime attestation has no custody path")
        elif record["runtime_attestation_sha256"] is not None:
            raise ProviderCustodyError("provider custody runtime attestation path is incomplete")
        try:
            on_disk = json.loads(receipt_raw.decode("utf-8"), object_pairs_hook=_closed_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderCustodyError("provider receipt file is invalid JSON") from exc
        if on_disk != receipt:
            raise ProviderCustodyError("provider receipt file differs from verifier evidence")
        if _sha256(raw) != receipt["raw_event_sha256"] or _sha256(request) != receipt[
            "request_sha256"
        ]:
            raise ProviderCustodyError("provider raw/request custody does not match receipt")
        _validate_argv(argv, receipt["launch"]["argv_sha256"])


__all__ = ["ProviderCustodyError", "ProviderCustodyStore"]
