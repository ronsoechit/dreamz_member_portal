from __future__ import annotations

"""Read-only preflight for Dreamz Gym Assistant's official Journal export.

The active ``Data/Journal.dat`` is a proprietary binary source and is never
parsed here. The probe verifies only the exact production locator, stable file
identities, the Gym Assistant executable and the known historical segment set.
Actual record coverage is proven dynamically from a fresh official ``Export
Journal`` result by ``gymassistant_journal_export.py``.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import stat
import sys
from typing import Sequence

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from ga_journal_snapshot import CLASSIFIER_VERSION


PROBE_SCHEMA = "dreamz.ga.journal.export-preflight-probe.v2"
EXPECTED_CLASSIFIER_VERSION = "dreamz.ga.journal.export-records.v2"
SOURCE_KIND = "gym_assistant_official_journal_export"
EXPECTED_DATA_ROOT_TEXT = "c:/gym assistant 2.6/data"
EXPECTED_DATA_ROOT_FINGERPRINT_SHA256 = hashlib.sha256(
    EXPECTED_DATA_ROOT_TEXT.encode("utf-8")
).hexdigest()
ACTIVE_JOURNAL_NAME = "Journal.dat"
GYMASSISTANT_EXECUTABLE_NAME = "Gym Assistant 26.exe"
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_READ_ATTEMPTS = 3

KNOWN_REASON_CODES = frozenset({
    "active_journal_empty",
    "classifier_version_mismatch",
    "data_root_binding_mismatch",
    "data_root_reparse_point",
    "executable_empty",
    "executable_missing",
    "executable_reparse_point",
    "install_root_reparse_point",
    "probe_internal_error",
    "source_identity_changed",
    "source_missing",
    "source_reparse_point",
    "source_tree_reparse_point",
    "source_tree_scan_incomplete",
    "source_tree_unstable",
    "source_unavailable",
    "source_unstable",
    "wrong_data_root",
})


class ProbeSourceError(RuntimeError):
    reason_code = "source_unavailable"


class ProbeSourceMissingError(ProbeSourceError):
    reason_code = "source_missing"


class ProbeSourceIdentityError(ProbeSourceError):
    reason_code = "source_identity_changed"


class ProbeSourceUnstableError(ProbeSourceError):
    reason_code = "source_unstable"


@dataclass(frozen=True)
class _ExactRead:
    data: bytes
    byte_length: int
    mtime_ns: int
    file_identity: tuple[int, int]
    file_identity_sha256: str


@dataclass(frozen=True)
class _StableRead:
    byte_length: int
    mtime_ns: int
    file_identity: tuple[int, int]
    source_sha256: str
    file_identity_sha256: str
    stable_read_count: int


@dataclass(frozen=True)
class _SegmentScan:
    complete: bool
    reparse_point_observed: bool
    segment_fingerprints: tuple[str, ...]


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _canonical_path_text(path: Path) -> str:
    normalized = str(Path(path).resolve(strict=True)).replace("\\", "/").rstrip("/")
    return normalized.casefold() if os.name == "nt" else normalized


def _production_data_root_lexically_exact(value: object) -> bool:
    if not isinstance(value, (str, Path)):
        return False
    raw = str(value)
    if not raw or raw != raw.strip() or "\x00" in raw:
        return False
    windows_text = raw.replace("/", "\\")
    if windows_text.startswith("\\\\"):
        return False
    components = windows_text.split("\\")
    if any(component in {".", ".."} for component in components):
        return False
    normalized = ntpath.normpath(windows_text).rstrip("\\")
    return normalized.casefold() == r"c:\gym assistant 2.6\data"


def _path_fingerprint(path: Path) -> str:
    return hashlib.sha256(_canonical_path_text(path).encode("utf-8")).hexdigest()


def _path_is_symlink_or_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return bool(
        path.is_symlink()
        or (
            int(getattr(metadata, "st_file_attributes", 0))
            & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    if identity[1] <= 0:
        raise ProbeSourceIdentityError()
    return identity


def _read_exact_file_once(path: Path) -> _ExactRead:
    try:
        if _path_is_symlink_or_reparse_point(path):
            raise ProbeSourceIdentityError()
        resolved_before = path.resolve(strict=True)
        before = resolved_before.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ProbeSourceIdentityError()
        with resolved_before.open("rb") as handle:
            descriptor_before = os.fstat(handle.fileno())
            data = handle.read()
            descriptor_after = os.fstat(handle.fileno())
        resolved_after = path.resolve(strict=True)
        after = resolved_after.stat()
    except ProbeSourceError:
        raise
    except FileNotFoundError as exc:
        raise ProbeSourceMissingError() from exc
    except OSError as exc:
        raise ProbeSourceUnstableError() from exc

    identities = {
        _file_identity(before),
        _file_identity(descriptor_before),
        _file_identity(descriptor_after),
        _file_identity(after),
    }
    if resolved_before != resolved_after or len(identities) != 1:
        raise ProbeSourceIdentityError()
    if (
        before.st_size != descriptor_before.st_size
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_after.st_size != after.st_size
        or before.st_mtime_ns != descriptor_before.st_mtime_ns
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or descriptor_after.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise ProbeSourceUnstableError()
    identity = identities.pop()
    return _ExactRead(
        data=data,
        byte_length=len(data),
        mtime_ns=int(after.st_mtime_ns),
        file_identity=identity,
        file_identity_sha256=_canonical_json_sha256(
            {"device": str(identity[0]), "file_index": str(identity[1])}
        ),
    )


def _read_stable_file_twice(
    path: Path,
    *,
    max_attempts: int = MAX_READ_ATTEMPTS,
) -> _StableRead:
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    last_error: ProbeSourceError | None = None
    for _attempt in range(max_attempts):
        try:
            first = _read_exact_file_once(path)
            second = _read_exact_file_once(path)
        except ProbeSourceUnstableError as exc:
            last_error = exc
            continue
        except ProbeSourceError:
            raise
        if first.file_identity != second.file_identity:
            raise ProbeSourceIdentityError()
        if (
            first.data == second.data
            and first.byte_length == second.byte_length
            and first.mtime_ns == second.mtime_ns
            and first.file_identity_sha256 == second.file_identity_sha256
        ):
            return _StableRead(
                byte_length=second.byte_length,
                mtime_ns=second.mtime_ns,
                file_identity=second.file_identity,
                source_sha256=hashlib.sha256(second.data).hexdigest(),
                file_identity_sha256=second.file_identity_sha256,
                stable_read_count=2,
            )
        last_error = ProbeSourceUnstableError()
    raise ProbeSourceUnstableError() from last_error


def _confirm_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        if _path_is_symlink_or_reparse_point(path):
            raise ProbeSourceIdentityError()
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except ProbeSourceError:
        raise
    except FileNotFoundError as exc:
        raise ProbeSourceMissingError() from exc
    except OSError as exc:
        raise ProbeSourceUnstableError() from exc
    if _file_identity(metadata) != expected:
        raise ProbeSourceIdentityError()


def _journal_segment_name(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith("journal") and Path(folded).suffix in {
        ".dat",
        ".tmp",
        ".jtx",
    }


def _scan_historical_segments(data_root: Path) -> _SegmentScan:
    roots = (
        ("data", data_root),
        ("backup", data_root.parent / "Backup"),
        ("data-backup", data_root / "Backup"),
    )
    fingerprints: list[str] = []
    complete = True
    reparse_observed = False
    for label, root in roots:
        if not root.exists():
            continue
        if _path_is_symlink_or_reparse_point(root):
            reparse_observed = True
            continue
        try:
            entries = tuple(root.iterdir())
        except OSError:
            complete = False
            continue
        for candidate in entries:
            if (
                not candidate.is_file()
                or not _journal_segment_name(candidate.name)
                or (
                    label == "data"
                    and candidate.name.casefold() == ACTIVE_JOURNAL_NAME.casefold()
                )
            ):
                continue
            if _path_is_symlink_or_reparse_point(candidate):
                reparse_observed = True
                continue
            try:
                snapshot = _read_stable_file_twice(candidate)
            except ProbeSourceError:
                complete = False
                continue
            fingerprints.append(
                _canonical_json_sha256({
                    "logical_name_sha256": hashlib.sha256(
                        f"{label}/{candidate.name.casefold()}".encode("utf-8")
                    ).hexdigest(),
                    "file_identity_sha256": snapshot.file_identity_sha256,
                    "byte_length": snapshot.byte_length,
                    "source_sha256": snapshot.source_sha256,
                })
            )
    return _SegmentScan(
        complete=complete,
        reparse_point_observed=reparse_observed,
        segment_fingerprints=tuple(sorted(fingerprints)),
    )


def _empty_result() -> dict:
    return {
        "schema": PROBE_SCHEMA,
        "mode": "read_only",
        "status": "blocked",
        "export_preflight_ready": False,
        "coverage_proven": False,
        "classifier_version": CLASSIFIER_VERSION,
        "official_export_required": True,
        "reason_codes": [],
        "source": None,
        "inventory": {
            "historical_segment_count": 0,
            "historical_segment_set_sha256": _canonical_json_sha256([]),
            "tree_scan_stable_count": 0,
        },
    }


def probe_journal_source_coverage(
    data_root: Path,
    *,
    max_read_attempts: int = MAX_READ_ATTEMPTS,
) -> dict:
    """Return sanitized export readiness; never claim record coverage."""
    result = _empty_result()
    reasons: set[str] = set()
    if CLASSIFIER_VERSION != EXPECTED_CLASSIFIER_VERSION:
        reasons.add("classifier_version_mismatch")
    if not _production_data_root_lexically_exact(data_root):
        result["reason_codes"] = ["wrong_data_root"]
        return result

    try:
        data_root = Path(data_root)
        install_root = data_root.parent
        if _path_is_symlink_or_reparse_point(install_root):
            reasons.add("install_root_reparse_point")
            raise ProbeSourceIdentityError()
        if _path_is_symlink_or_reparse_point(data_root):
            reasons.add("data_root_reparse_point")
            raise ProbeSourceIdentityError()
        resolved_data_root = data_root.resolve(strict=True)
        if not resolved_data_root.is_dir():
            raise ProbeSourceMissingError()
        data_root_fingerprint = _path_fingerprint(resolved_data_root)
        if data_root_fingerprint != EXPECTED_DATA_ROOT_FINGERPRINT_SHA256:
            reasons.add("data_root_binding_mismatch")
            result["reason_codes"] = sorted(reasons)
            return result

        active_path = resolved_data_root / ACTIVE_JOURNAL_NAME
        if _path_is_symlink_or_reparse_point(active_path):
            reasons.add("source_reparse_point")
            raise ProbeSourceIdentityError()
        active = _read_stable_file_twice(
            active_path,
            max_attempts=max_read_attempts,
        )

        executable_path = resolved_data_root.parent / GYMASSISTANT_EXECUTABLE_NAME
        if not executable_path.is_file():
            reasons.add("executable_missing")
            result["reason_codes"] = sorted(reasons)
            return result
        if _path_is_symlink_or_reparse_point(executable_path):
            reasons.add("executable_reparse_point")
            raise ProbeSourceIdentityError()
        executable = _read_stable_file_twice(
            executable_path,
            max_attempts=max_read_attempts,
        )

        first_scan = _scan_historical_segments(resolved_data_root)
        second_scan = _scan_historical_segments(resolved_data_root)
        _confirm_identity(active_path, active.file_identity)
        _confirm_identity(executable_path, executable.file_identity)
    except ProbeSourceError as exc:
        reasons.add(exc.reason_code)
        result["reason_codes"] = sorted(reasons)
        return result
    except FileNotFoundError:
        reasons.add("source_missing")
        result["reason_codes"] = sorted(reasons)
        return result
    except OSError:
        reasons.add("source_unavailable")
        result["reason_codes"] = sorted(reasons)
        return result
    except Exception:
        reasons.add("probe_internal_error")
        result["reason_codes"] = sorted(reasons)
        return result

    if active.byte_length == 0:
        reasons.add("active_journal_empty")
    if executable.byte_length == 0:
        reasons.add("executable_empty")
    if not first_scan.complete or not second_scan.complete:
        reasons.add("source_tree_scan_incomplete")
    if first_scan.reparse_point_observed or second_scan.reparse_point_observed:
        reasons.add("source_tree_reparse_point")
    tree_stable = first_scan == second_scan
    if not tree_stable:
        reasons.add("source_tree_unstable")

    result["source"] = {
        "kind": SOURCE_KIND,
        "data_root_fingerprint_sha256": data_root_fingerprint,
        "active_locator_fingerprint_sha256": _path_fingerprint(active_path),
        "active_file_identity_sha256": active.file_identity_sha256,
        "active_byte_length": active.byte_length,
        "active_source_sha256": active.source_sha256,
        "active_stable_read_count": active.stable_read_count,
        "executable_locator_fingerprint_sha256": _path_fingerprint(
            executable_path
        ),
        "executable_file_identity_sha256": executable.file_identity_sha256,
        "executable_byte_length": executable.byte_length,
        "executable_source_sha256": executable.source_sha256,
        "executable_stable_read_count": executable.stable_read_count,
    }
    result["inventory"] = {
        "historical_segment_count": len(second_scan.segment_fingerprints),
        "historical_segment_set_sha256": _canonical_json_sha256(
            list(second_scan.segment_fingerprints)
        ),
        "tree_scan_stable_count": 2 if tree_stable else 0,
    }
    result["reason_codes"] = sorted(reasons)
    if not reasons:
        result["status"] = "ready"
        result["export_preflight_ready"] = True
    return result


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Dreamz official Journal export preflight"
    )
    parser.add_argument(
        "--data-root",
        required=True,
        type=Path,
        help="Exact active Gym Assistant Data directory",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = probe_journal_source_coverage(args.data_root)
    except Exception:
        result = _empty_result()
        result["reason_codes"] = ["probe_internal_error"]
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0 if result["export_preflight_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
