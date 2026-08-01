from __future__ import annotations

"""Read-only, privacy-safe coverage probe for Dreamz Gym Assistant Journal.jtx.

The command intentionally has one source contract: the active Dreamz data
directory must be ``C:\\Gym Assistant 2.6\\Data`` and the observed file must be
its exact ``Journal.jtx`` child.  It never falls back to a backup, copied file,
environment override or alternate locator.  Output is JSON on stdout only and
contains hashes, counts and reason codes, never journal rows or paths.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import sys
from typing import Sequence

# A normal ``python script.py`` invocation must remain stdout-only. Set this
# before importing the local classifier so the CLI cannot create __pycache__
# or .pyc artifacts beside either source file. Library imports keep the
# caller's existing interpreter policy.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from ga_journal_snapshot import CLASSIFIER_VERSION, _valid_journal_header


PROBE_SCHEMA = "dreamz.ga.journal.source-coverage-probe.v1"
EXPECTED_CLASSIFIER_VERSION = "dreamz.ga.journal.member-lines.v1"
SOURCE_KIND = "gym_assistant_live_journal"
EXPECTED_DATA_ROOT_TEXT = "c:/gym assistant 2.6/data"
EXPECTED_DATA_ROOT_FINGERPRINT_SHA256 = hashlib.sha256(
    EXPECTED_DATA_ROOT_TEXT.encode("utf-8")
).hexdigest()
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
HEADER_TOKEN_COUNT = 11
MEMBER_NUMBER_FIELD_ZERO_BASED_INDEX = 5
MAX_READ_ATTEMPTS = 3

KNOWN_REASON_CODES = frozenset({
    "classifier_version_mismatch",
    "data_root_binding_mismatch",
    "data_root_reparse_point",
    "install_root_reparse_point",
    "journal_empty",
    "member_number_field_unproven",
    "possible_historical_journal_segments",
    "probe_internal_error",
    "source_identity_changed",
    "source_missing",
    "source_reparse_point",
    "source_tree_reparse_point",
    "source_tree_scan_incomplete",
    "source_tree_unstable",
    "source_unavailable",
    "source_unstable",
    "unsupported_record_grammar",
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
    data: bytes
    byte_length: int
    mtime_ns: int
    file_identity: tuple[int, int]
    source_sha256: str
    file_identity_sha256: str
    stable_read_count: int


@dataclass(frozen=True)
class _TreeScan:
    complete: bool
    reparse_point_observed: bool
    candidate_fingerprints: tuple[str, ...]


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_path_text(path: Path) -> str:
    normalized = str(Path(path).resolve(strict=True)).replace("\\", "/").rstrip("/")
    if os.name == "nt":
        normalized = normalized.casefold()
    return normalized


def _production_data_root_lexically_exact(value: object) -> bool:
    """Check the local C: locator without touching the filesystem.

    This check must precede lstat/resolve/open so a UNC, mapped drive or other
    caller-controlled path cannot trigger SMB/DNS/network I/O.
    """
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
        path_stat = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return bool(
        path.is_symlink()
        or (
            int(getattr(path_stat, "st_file_attributes", 0))
            & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _file_identity(stat_result: os.stat_result) -> tuple[int, int]:
    identity = (int(stat_result.st_dev), int(stat_result.st_ino))
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
        if _path_is_symlink_or_reparse_point(path):
            raise ProbeSourceIdentityError()
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
    last_unstable: ProbeSourceUnstableError | None = None
    for _attempt in range(max_attempts):
        try:
            first = _read_exact_file_once(path)
            second = _read_exact_file_once(path)
        except ProbeSourceUnstableError as exc:
            last_unstable = exc
            continue
        if first.file_identity != second.file_identity:
            raise ProbeSourceIdentityError()
        if (
            first.data == second.data
            and first.byte_length == second.byte_length
            and first.mtime_ns == second.mtime_ns
            and first.file_identity_sha256 == second.file_identity_sha256
        ):
            return _StableRead(
                data=second.data,
                byte_length=second.byte_length,
                mtime_ns=second.mtime_ns,
                file_identity=second.file_identity,
                source_sha256=hashlib.sha256(second.data).hexdigest(),
                file_identity_sha256=second.file_identity_sha256,
                stable_read_count=2,
            )
        last_unstable = ProbeSourceUnstableError()
    raise ProbeSourceUnstableError() from last_unstable


def _confirm_stable_snapshot_after_tree_scan(
    path: Path,
    stable: _StableRead,
) -> None:
    confirmation = _read_exact_file_once(path)
    if confirmation.file_identity != stable.file_identity:
        raise ProbeSourceIdentityError()
    if (
        confirmation.data != stable.data
        or confirmation.byte_length != stable.byte_length
        or confirmation.mtime_ns != stable.mtime_ns
        or confirmation.file_identity_sha256 != stable.file_identity_sha256
    ):
        raise ProbeSourceUnstableError()


def _journal_candidate_name(name: str) -> bool:
    folded = name.casefold()
    return "journal" in folded or ".jtx" in folded


def _relative_locator_fingerprint(path: Path, data_root: Path) -> str:
    relative = os.path.relpath(path, data_root).replace("\\", "/")
    if os.name == "nt":
        relative = relative.casefold()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def _scan_possible_historical_segments(
    data_root: Path,
    live_journal: Path,
) -> _TreeScan:
    candidate_fingerprints: set[str] = set()
    complete = True
    reparse_observed = False

    def scan_error(_error: OSError) -> None:
        nonlocal complete
        complete = False

    try:
        for current_text, directory_names, file_names in os.walk(
            data_root,
            topdown=True,
            onerror=scan_error,
            followlinks=False,
        ):
            current = Path(current_text)
            safe_directories: list[str] = []
            for name in directory_names:
                directory = current / name
                if _path_is_symlink_or_reparse_point(directory):
                    reparse_observed = True
                    continue
                safe_directories.append(name)
            directory_names[:] = safe_directories

            for name in file_names:
                if not _journal_candidate_name(name):
                    continue
                candidate = current / name
                if _path_is_symlink_or_reparse_point(candidate):
                    reparse_observed = True
                try:
                    is_live = candidate.resolve(strict=True) == live_journal
                except (FileNotFoundError, OSError):
                    complete = False
                    is_live = False
                if not is_live:
                    candidate_fingerprints.add(
                        _relative_locator_fingerprint(candidate, data_root)
                    )
    except OSError:
        complete = False

    return _TreeScan(
        complete=complete,
        reparse_point_observed=reparse_observed,
        candidate_fingerprints=tuple(sorted(candidate_fingerprints)),
    )


def _classify_all_records(data: bytes) -> dict:
    record_count = 0
    supported_count = 0
    malformed_count = 0
    invalid_member_field_count = 0

    for line in re.split(rb"\r\n|\n|\r", data):
        if not line.strip(b" \t"):
            continue
        record_count += 1
        parsed = _valid_journal_header(line)
        if parsed is not None:
            supported_count += 1
            continue
        malformed_count += 1
        header = line.split(b"|", 1)[0]
        tokens = header.split()
        member_field_valid = False
        if len(tokens) == HEADER_TOKEN_COUNT:
            member_token = tokens[MEMBER_NUMBER_FIELD_ZERO_BASED_INDEX]
            if member_token.isascii() and member_token.isdigit():
                try:
                    member_field_valid = int(member_token) > 0
                except ValueError:
                    member_field_valid = False
        if not member_field_valid:
            invalid_member_field_count += 1

    return {
        "record_count": record_count,
        "supported_record_count": supported_count,
        "malformed_record_count": malformed_count,
        "invalid_member_field_count": invalid_member_field_count,
    }


def _empty_result() -> dict:
    return {
        "schema": PROBE_SCHEMA,
        "mode": "read_only",
        "status": "blocked",
        "coverage_proven": False,
        "classifier_version": CLASSIFIER_VERSION,
        "reason_codes": [],
        "source": None,
        "coverage": {
            "header_token_count_expected": HEADER_TOKEN_COUNT,
            "member_number_field_zero_based_index": (
                MEMBER_NUMBER_FIELD_ZERO_BASED_INDEX
            ),
            "record_count": 0,
            "supported_record_count": 0,
            "malformed_record_count": 0,
            "invalid_member_field_count": 0,
            "historical_segment_candidate_count": 0,
            "historical_segment_set_sha256": _canonical_json_sha256([]),
            "tree_scan_stable_count": 0,
        },
    }


def probe_journal_source_coverage(
    data_root: Path,
    *,
    max_read_attempts: int = MAX_READ_ATTEMPTS,
) -> dict:
    """Return a fail-closed sanitized coverage result without writing files."""
    result = _empty_result()
    reasons: set[str] = set()

    if CLASSIFIER_VERSION != EXPECTED_CLASSIFIER_VERSION:
        reasons.add("classifier_version_mismatch")
    if not _production_data_root_lexically_exact(data_root):
        reasons.add("wrong_data_root")
        result["reason_codes"] = sorted(reasons)
        return result
    try:
        data_root = Path(data_root)
        if data_root.name.casefold() != "data":
            raise ValueError("wrong data root")
        if data_root.parent.name.casefold() != "gym assistant 2.6":
            raise ValueError("wrong install root")
        if _path_is_symlink_or_reparse_point(data_root.parent):
            reasons.add("install_root_reparse_point")
            raise ProbeSourceIdentityError()
        if _path_is_symlink_or_reparse_point(data_root):
            reasons.add("data_root_reparse_point")
            raise ProbeSourceIdentityError()
        resolved_data_root = data_root.resolve(strict=True)
        if not resolved_data_root.is_dir():
            raise ProbeSourceMissingError()
    except ProbeSourceError as exc:
        reasons.add(exc.reason_code)
        result["reason_codes"] = sorted(reasons)
        return result
    except (ValueError, RuntimeError):
        reasons.add("wrong_data_root")
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
    try:
        data_root_fingerprint = _path_fingerprint(resolved_data_root)
        if data_root_fingerprint != EXPECTED_DATA_ROOT_FINGERPRINT_SHA256:
            reasons.add("data_root_binding_mismatch")
            result["reason_codes"] = sorted(reasons)
            return result

        candidate = resolved_data_root / "Journal.jtx"
        if _path_is_symlink_or_reparse_point(candidate):
            reasons.add("source_reparse_point")
            raise ProbeSourceIdentityError()
        if not candidate.is_file():
            raise ProbeSourceMissingError()
        live_journal = candidate.resolve(strict=True)
        if (
            live_journal.parent != resolved_data_root
            or live_journal.name.casefold() != "journal.jtx"
        ):
            raise ProbeSourceIdentityError()

        stable = _read_stable_file_twice(
            live_journal,
            max_attempts=max_read_attempts,
        )
        first_tree = _scan_possible_historical_segments(
            resolved_data_root,
            live_journal,
        )
        second_tree = _scan_possible_historical_segments(
            resolved_data_root,
            live_journal,
        )
        _confirm_stable_snapshot_after_tree_scan(live_journal, stable)
    except ProbeSourceError as exc:
        reasons.add(exc.reason_code)
        result["reason_codes"] = sorted(reasons)
        return result
    except (FileNotFoundError, OSError):
        reasons.add("source_unavailable")
        result["reason_codes"] = sorted(reasons)
        return result
    except Exception:
        reasons.add("probe_internal_error")
        result["reason_codes"] = sorted(reasons)
        return result

    if not first_tree.complete or not second_tree.complete:
        reasons.add("source_tree_scan_incomplete")
    if first_tree.reparse_point_observed or second_tree.reparse_point_observed:
        reasons.add("source_tree_reparse_point")
    tree_stable = (
        first_tree.candidate_fingerprints == second_tree.candidate_fingerprints
        and first_tree.complete == second_tree.complete
        and first_tree.reparse_point_observed == second_tree.reparse_point_observed
    )
    if not tree_stable:
        reasons.add("source_tree_unstable")
    historical_candidates = second_tree.candidate_fingerprints
    if historical_candidates:
        reasons.add("possible_historical_journal_segments")

    record_counts = _classify_all_records(stable.data)
    if record_counts["record_count"] == 0:
        reasons.add("journal_empty")
    if record_counts["malformed_record_count"]:
        reasons.add("unsupported_record_grammar")
    if record_counts["invalid_member_field_count"]:
        reasons.add("member_number_field_unproven")

    result["source"] = {
        "kind": SOURCE_KIND,
        "data_root_fingerprint_sha256": data_root_fingerprint,
        "expected_data_root_fingerprint_sha256": (
            EXPECTED_DATA_ROOT_FINGERPRINT_SHA256
        ),
        "locator_fingerprint_sha256": _path_fingerprint(live_journal),
        "file_identity_sha256": stable.file_identity_sha256,
        "byte_length": stable.byte_length,
        "source_sha256": stable.source_sha256,
        "stable_read_count": stable.stable_read_count,
        "post_scan_confirmation_count": 1,
    }
    result["coverage"].update(record_counts)
    result["coverage"].update({
        "historical_segment_candidate_count": len(historical_candidates),
        "historical_segment_set_sha256": _canonical_json_sha256(
            list(historical_candidates)
        ),
        "tree_scan_stable_count": 2 if tree_stable else 0,
    })
    result["reason_codes"] = sorted(reasons)
    if not reasons:
        result["status"] = "passed"
        result["coverage_proven"] = True
    return result


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only privacy-safe Dreamz Journal.jtx source coverage probe"
        )
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
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0 if result["coverage_proven"] else 2


if __name__ == "__main__":
    sys.exit(main())
