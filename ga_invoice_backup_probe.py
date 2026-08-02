from __future__ import annotations

"""Fail-closed, read-only preflight for one exact Gym Assistant backup.

The production runner binds this generic probe to an exact local ``.gbu``
archive and to an exact private allowlist.  The probe never extracts the
archive, never calls the network and never writes a result file.  Its JSON
output deliberately contains only hashes, counts, booleans and fixed reason
codes.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
from typing import Sequence
import unicodedata
import zipfile
import zlib

# Keep direct CLI use free of bytecode side effects even when a caller forgets
# ``python -B``.  The release runner still uses ``-B`` as a second boundary.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from ga_journal import (
    VOIDED_EVENT_MASK,
    parse_gymassistant_billing_catalog_text,
    parse_membership_journal_line,
    service_period_matches_catalog_interval,
)


PROBE_SCHEMA = "dreamz.ga.invoice-backup-preflight.v1"
SOURCE_KIND = "gym_assistant_backup"
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MAX_ALLOWLIST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
MAX_ENTRY_BYTES = 128 * 1024 * 1024
MAX_JOURNAL_ENTRY_BYTES = 16 * 1024 * 1024
MAX_MEMBERS_ENTRY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000
MAX_TARGET_MEMBERSHIP_EVENTS = 20_000
MAX_JOURNAL_RECORD_BYTES = 64 * 1024
MAX_CATALOG_RECORDS = 250_000
MAX_CATALOG_OPTION_OR_ADDON_ROWS = 20_000
MAX_CATALOG_LINE_CHARS = 64 * 1024
READ_CHUNK_BYTES = 1024 * 1024
_ZIP_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_ZIP_CENTRAL_HEADER = struct.Struct("<4s6H3L5H2L")
_ZIP_END_OF_CENTRAL_DIRECTORY = struct.Struct("<4s4H2LH")
_ZIP_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
_ZIP_CENTRAL_HEADER_SIGNATURE = b"PK\x01\x02"
_ZIP_END_OF_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x05\x06"
_ZIP_DATA_DESCRIPTOR_SIGNATURE = b"PK\x07\x08"
_ZIP_FLAG_DATA_DESCRIPTOR = 0x08
_ZIP_FLAG_UTF8 = 0x800
_ZIP_ALLOWED_FLAGS = 0x080E
ALLOWED_COMPRESSION_METHODS = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)
_JOURNAL_HEADER_RE = re.compile(
    rb"^(c\d{8}!\d{4})[ \t]+([0-9A-Fa-f]+)"
    rb"(?:[ \t]+([0-9]+)){9}\|(.+)$",
    flags=re.DOTALL,
)
_CATALOG_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85]")

KNOWN_REASON_CODES = frozenset(
    {
        "allowlist_binding_mismatch",
        "allowlist_invalid",
        "archive_compression_ratio_exceeded",
        "archive_crc_invalid",
        "archive_duplicate_entry",
        "archive_duplicate_required_entry",
        "archive_encrypted_entry",
        "archive_entry_count_invalid",
        "archive_invalid",
        "archive_size_limit_exceeded",
        "archive_unsafe_entry",
        "archive_unsupported_entry",
        "backup_path_invalid",
        "catalog_ambiguous",
        "catalog_empty",
        "catalog_resource_limit_exceeded",
        "invalid_arguments",
        "journal_empty",
        "probe_internal_error",
        "required_entry_empty",
        "required_entry_missing",
        "source_hash_mismatch",
        "source_identity_changed",
        "source_length_mismatch",
        "source_missing",
        "source_not_regular",
        "source_reparse_point",
        "source_unstable",
        "target_membership_parse_issue",
        "target_event_limit_exceeded",
    }
)


class ProbeBlocked(RuntimeError):
    def __init__(self, reason_code: str):
        if reason_code not in KNOWN_REASON_CODES:
            reason_code = "probe_internal_error"
        super().__init__(reason_code)
        self.reason_code = reason_code


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - message is ignored
        raise ProbeBlocked("invalid_arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise ProbeBlocked("invalid_arguments")


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
    source_sha256: str
    file_identity_sha256: str
    stable_read_count: int


@dataclass(frozen=True)
class _ArchiveSnapshot:
    entry_count: int
    total_uncompressed_bytes: int
    journal_bytes: bytes
    members_bytes: bytes


@dataclass(frozen=True)
class _RawCentralEntry:
    raw_name: bytes
    decoded_name: str
    create_system: int
    extract_version: int
    flag_bits: int
    compress_type: int
    modified_time: int
    modified_date: int
    crc32: int
    compress_size: int
    file_size: int
    internal_attr: int
    external_attr: int
    header_offset: int
    central_extra: bytes


@dataclass(frozen=True)
class _RawZipIndex:
    entries: tuple[_RawCentralEntry, ...]
    central_offset: int


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _empty_result() -> dict:
    return {
        "schema": PROBE_SCHEMA,
        "mode": "read_only",
        "status": "blocked",
        "integrity_proven": False,
        "reason_codes": [],
        "source": None,
        "allowlist": {"count": 0, "set_sha256": None},
        "archive": {
            "entry_count": 0,
            "total_uncompressed_bytes": 0,
            "journal_byte_length": 0,
            "journal_sha256": None,
            "members_byte_length": 0,
            "members_sha256": None,
            "crc_verified": False,
        },
        "analysis": {
            "target_membership_event_count": 0,
            "target_membership_parse_issue_count": 0,
            "target_member_with_event_count": 0,
            "target_member_without_event_count": 0,
            "positive_membership_event_count": 0,
            "target_member_with_positive_event_count": 0,
            "target_member_without_positive_event_count": 0,
            "new_membership_event_count": 0,
            "renewal_event_count": 0,
            "voided_event_count": 0,
            "manual_review_event_count": 0,
            "nonpositive_membership_event_count": 0,
            "catalog_option_count": 0,
            "catalog_duplicate_key_count": 0,
            "catalog_match_count": 0,
            "catalog_missing_count": 0,
            "catalog_amount_match_count": 0,
            "catalog_amount_mismatch_count": 0,
            "catalog_period_match_count": 0,
            "catalog_period_mismatch_count": 0,
            "catalog_period_missing_count": 0,
        },
    }


def _lexically_local_absolute_windows_path(value: object) -> bool:
    """Reject network/device/relative/ambiguous locators before filesystem IO."""
    if not isinstance(value, (str, Path)):
        return False
    raw = str(value)
    if not raw or raw != raw.strip() or "\x00" in raw:
        return False
    windows_text = raw.replace("/", "\\")
    if windows_text.startswith("\\\\"):
        return False
    drive, tail = ntpath.splitdrive(windows_text)
    if (drive or "").casefold() != "c:" or not tail.startswith("\\"):
        return False
    if ":" in tail:
        return False
    components = [component for component in tail.split("\\") if component]
    if not components or any(component in {".", ".."} for component in components):
        return False
    return ntpath.isabs(windows_text)


def _path_is_symlink_or_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        raise ProbeBlocked("source_missing")
    except OSError:
        raise ProbeBlocked("source_unstable")
    return bool(
        path.is_symlink()
        or (
            int(getattr(path_stat, "st_file_attributes", 0))
            & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _reject_reparse_chain(path: Path) -> None:
    chain = [path, *path.parents]
    for candidate in reversed(chain):
        if _path_is_symlink_or_reparse_point(candidate):
            raise ProbeBlocked("source_reparse_point")


def _file_identity(stat_result: os.stat_result) -> tuple[int, int]:
    identity = (int(stat_result.st_dev), int(stat_result.st_ino))
    if identity[1] <= 0:
        raise ProbeBlocked("source_identity_changed")
    return identity


def _read_exact_file_once(
    path: Path,
    *,
    maximum_bytes: int,
    expected_length: int | None = None,
) -> _ExactRead:
    _reject_reparse_chain(path)
    try:
        resolved_before = path.resolve(strict=True)
        before = resolved_before.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ProbeBlocked("source_not_regular")
        if expected_length is not None and before.st_size != expected_length:
            raise ProbeBlocked("source_length_mismatch")
        if before.st_size > maximum_bytes:
            raise ProbeBlocked("archive_size_limit_exceeded")
        with resolved_before.open("rb") as handle:
            descriptor_before = os.fstat(handle.fileno())
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = handle.read(min(READ_CHUNK_BYTES, maximum_bytes + 1 - observed))
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
                if observed > maximum_bytes:
                    raise ProbeBlocked("archive_size_limit_exceeded")
            data = b"".join(chunks)
            descriptor_after = os.fstat(handle.fileno())
        _reject_reparse_chain(path)
        resolved_after = path.resolve(strict=True)
        after = resolved_after.stat()
    except ProbeBlocked:
        raise
    except FileNotFoundError:
        raise ProbeBlocked("source_missing")
    except OSError:
        raise ProbeBlocked("source_unstable")

    identities = {
        _file_identity(before),
        _file_identity(descriptor_before),
        _file_identity(descriptor_after),
        _file_identity(after),
    }
    if resolved_before != resolved_after or len(identities) != 1:
        raise ProbeBlocked("source_identity_changed")
    if (
        before.st_size != descriptor_before.st_size
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_after.st_size != after.st_size
        or before.st_mtime_ns != descriptor_before.st_mtime_ns
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or descriptor_after.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise ProbeBlocked("source_unstable")

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
    maximum_bytes: int,
    expected_length: int | None = None,
) -> _StableRead:
    first = _read_exact_file_once(
        path,
        maximum_bytes=maximum_bytes,
        expected_length=expected_length,
    )
    second = _read_exact_file_once(
        path,
        maximum_bytes=maximum_bytes,
        expected_length=expected_length,
    )
    if first.file_identity != second.file_identity:
        raise ProbeBlocked("source_identity_changed")
    if (
        first.data != second.data
        or first.byte_length != second.byte_length
        or first.mtime_ns != second.mtime_ns
        or first.file_identity_sha256 != second.file_identity_sha256
    ):
        raise ProbeBlocked("source_unstable")
    return _StableRead(
        data=second.data,
        byte_length=second.byte_length,
        source_sha256=sha256(second.data).hexdigest(),
        file_identity_sha256=second.file_identity_sha256,
        stable_read_count=2,
    )


def _canonical_allowlist(data: bytes) -> tuple[frozenset[str], str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ProbeBlocked("allowlist_invalid")
    values: list[str] = []
    for raw_line in text.splitlines():
        if (
            len(raw_line) > 10
            or raw_line != raw_line.strip()
            or not re.fullmatch(r"[1-9][0-9]*", raw_line)
        ):
            raise ProbeBlocked("allowlist_invalid")
        parsed_value = int(raw_line)
        canonical = str(parsed_value)
        if canonical != raw_line or parsed_value > 0xFFFFFFFF:
            raise ProbeBlocked("allowlist_invalid")
        values.append(canonical)
    if not values or len(values) != len(set(values)):
        raise ProbeBlocked("allowlist_invalid")
    ordered = sorted(values, key=int)
    canonical_bytes = "\n".join(ordered).encode("ascii")
    return frozenset(ordered), sha256(canonical_bytes).hexdigest()


def _iter_cr_lf_lines(data: bytes):
    """Yield CR/LF-framed records without materializing an amplified list."""
    start = 0
    line_number = 1
    for delimiter in re.finditer(rb"\r\n|\r|\n", data):
        yield line_number, data[start : delimiter.start()]
        start = delimiter.end()
        line_number += 1
    yield line_number, data[start:]


def _valid_journal_header(line: bytes) -> tuple[str, int] | None:
    match = _JOURNAL_HEADER_RE.fullmatch(line)
    if not match:
        return None
    header, _payload = line.split(b"|", 1)
    tokens = header.split()
    if len(tokens) != 11 or any(not token for token in tokens):
        return None
    try:
        datetime.strptime(tokens[0].decode("ascii"), "c%Y%m%d!%H%M")
        sequence = int(tokens[1], 16)
        numeric_fields = [int(token, 10) for token in tokens[2:]]
    except (UnicodeDecodeError, ValueError):
        return None
    if sequence < 0 or sequence > 0xFFFFFFFF:
        return None
    if any(value < 0 or value > 0xFFFFFFFF for value in numeric_fields):
        return None
    member_number = numeric_fields[3]
    raw_event_type = numeric_fields[4]
    if member_number <= 0 or raw_event_type > 0xFFFF:
        return None
    return str(member_number), raw_event_type


def _attributable_member_number(line: bytes) -> str | None:
    header = line.split(b"|", 1)[0]
    tokens = header.split()
    if len(tokens) != 11 or not tokens[5].isascii() or not tokens[5].isdigit():
        return None
    try:
        member_number = int(tokens[5], 10)
    except ValueError:
        return None
    if member_number <= 0 or member_number > 0xFFFFFFFF:
        return None
    return str(member_number)


def _parse_allowlisted_membership_events(
    journal_bytes: bytes,
    allowlist: frozenset[str],
) -> tuple[list, int]:
    """Parse exact CR/LF-framed target events and prove non-target scope.

    Gym Assistant Journal rows include many legitimate non-membership event
    types.  A structurally valid header proves whether a row belongs to the
    allowlist.  A malformed row is ignored only when its fixed member field is
    still a valid, bounded, non-allowlisted member number; every other malformed
    row is scope-ambiguous and blocks the integrity claim.
    """
    events: list = []
    for line_number, raw_line in _iter_cr_lf_lines(journal_bytes):
        if len(raw_line) > MAX_JOURNAL_RECORD_BYTES:
            return events, 1
        if not raw_line.strip(b" \t"):
            continue
        # The shared parser uses Latin-1 ``str.splitlines()`` followed by
        # Unicode ``str.strip()``.  Mirror those exact semantics here: a
        # Unicode-only line separator plus any Unicode whitespace must not be
        # able to hide a second journal header inside one authoritative CR/LF
        # record.
        unicode_lines = raw_line.decode("latin-1", errors="strict").splitlines()
        if any(
            re.match(r"^c\d{8}!\d{4}(?:\s|$)", hidden_line.strip())
            for hidden_line in unicode_lines[1:]
        ):
            return events, 1
        header = _valid_journal_header(raw_line)
        if header is None:
            attributable_member = _attributable_member_number(raw_line)
            if attributable_member is None or attributable_member in allowlist:
                return events, 1
            continue
        member_id, raw_event_type = header
        if member_id not in allowlist:
            continue
        event_type = raw_event_type & ~VOIDED_EVENT_MASK
        if event_type not in {1, 3}:
            continue
        try:
            decoded = raw_line.decode("latin-1", errors="strict")
            event = parse_membership_journal_line(decoded)
        except (TypeError, UnicodeDecodeError, ValueError):
            event = None
        if event is None or event.member_id != member_id:
            return events, 1
        else:
            events.append(event)
            if len(events) > MAX_TARGET_MEMBERSHIP_EVENTS:
                raise ProbeBlocked("target_event_limit_exceeded")
    return events, 0


def _safe_zip_entry_name(info: zipfile.ZipInfo) -> tuple[str, bool]:
    raw = info.filename
    original = str(getattr(info, "orig_filename", raw))
    if not raw or "\x00" in raw or "\x00" in original:
        raise ProbeBlocked("archive_unsafe_entry")
    normalized = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    is_directory = info.is_dir() or normalized.endswith("/")
    normalized = normalized.rstrip("/") if is_directory else normalized
    if not normalized or normalized.startswith("/") or normalized.startswith("//"):
        raise ProbeBlocked("archive_unsafe_entry")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ProbeBlocked("archive_unsafe_entry")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProbeBlocked("archive_unsafe_entry")
    return "/".join(parts).casefold(), is_directory


def _zip_entry_is_nonregular(info: zipfile.ZipInfo, *, is_directory: bool) -> bool:
    if int(info.create_system) not in {0, 3}:
        return True
    if int(info.create_system) == 0:
        windows_attributes = int(info.external_attr) & 0xFFFF
        # DOS volume-label entries are metadata records, not regular files.
        if windows_attributes & (0x08 | 0x40 | 0x400):
            return True
        if not is_directory and windows_attributes & 0x10:
            return True
    unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == 0:
        return False
    if is_directory:
        return file_type != stat.S_IFDIR
    return file_type != stat.S_IFREG


def _decode_zip_name(raw_name: bytes, flag_bits: int) -> str:
    if not raw_name or b"\x00" in raw_name:
        raise ProbeBlocked("archive_unsafe_entry")
    try:
        encoding = "utf-8" if flag_bits & _ZIP_FLAG_UTF8 else "cp437"
        return raw_name.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        raise ProbeBlocked("archive_invalid")


def _validate_zip_flags(flag_bits: int, compress_type: int) -> None:
    if flag_bits & (0x01 | 0x40 | 0x2000):
        raise ProbeBlocked("archive_encrypted_entry")
    if flag_bits & ~_ZIP_ALLOWED_FLAGS:
        raise ProbeBlocked("archive_unsupported_entry")
    if compress_type != zipfile.ZIP_DEFLATED and flag_bits & 0x06:
        raise ProbeBlocked("archive_unsupported_entry")


def _validate_extra_fields(extra: bytes) -> None:
    cursor = 0
    while cursor < len(extra):
        if cursor + 4 > len(extra):
            raise ProbeBlocked("archive_invalid")
        field_id, field_size = struct.unpack_from("<2H", extra, cursor)
        cursor += 4
        field_end = cursor + field_size
        if field_end < cursor or field_end > len(extra):
            raise ProbeBlocked("archive_invalid")
        if field_id == 0x0001:
            raise ProbeBlocked("archive_unsupported_entry")
        cursor = field_end


def _find_exact_eocd(data: bytes) -> tuple[int, tuple]:
    search_start = max(0, len(data) - 0xFFFF - _ZIP_END_OF_CENTRAL_DIRECTORY.size)
    cursor = len(data)
    candidates: list[tuple[int, tuple]] = []
    while True:
        position = data.rfind(
            _ZIP_END_OF_CENTRAL_DIRECTORY_SIGNATURE,
            search_start,
            cursor,
        )
        if position < 0:
            break
        if position + _ZIP_END_OF_CENTRAL_DIRECTORY.size <= len(data):
            fields = _ZIP_END_OF_CENTRAL_DIRECTORY.unpack_from(data, position)
            comment_length = fields[-1]
            if position + _ZIP_END_OF_CENTRAL_DIRECTORY.size + comment_length == len(data):
                candidates.append((position, fields))
        cursor = position
    if len(candidates) != 1:
        raise ProbeBlocked("archive_invalid")
    return candidates[0]


def _parse_raw_central_directory(data: bytes) -> _RawZipIndex:
    eocd_offset, eocd = _find_exact_eocd(data)
    (
        signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = eocd
    if signature != _ZIP_END_OF_CENTRAL_DIRECTORY_SIGNATURE:
        raise ProbeBlocked("archive_invalid")
    if disk_number or central_disk or entries_on_disk != total_entries:
        raise ProbeBlocked("archive_unsupported_entry")
    if (
        total_entries in {0, 0xFFFF}
        or total_entries > MAX_ARCHIVE_ENTRIES
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ProbeBlocked("archive_unsupported_entry")
    central_end = central_offset + central_size
    if central_end < central_offset or central_end != eocd_offset:
        raise ProbeBlocked("archive_invalid")

    entries: list[_RawCentralEntry] = []
    cursor = central_offset
    for _index in range(total_entries):
        if cursor + _ZIP_CENTRAL_HEADER.size > central_end:
            raise ProbeBlocked("archive_invalid")
        fields = _ZIP_CENTRAL_HEADER.unpack_from(data, cursor)
        (
            central_signature,
            version_made_by,
            extract_version,
            flag_bits,
            compress_type,
            modified_time,
            modified_date,
            crc32,
            compress_size,
            file_size,
            filename_length,
            extra_length,
            comment_length,
            disk_start,
            internal_attr,
            external_attr,
            header_offset,
        ) = fields
        if central_signature != _ZIP_CENTRAL_HEADER_SIGNATURE:
            raise ProbeBlocked("archive_invalid")
        variable_start = cursor + _ZIP_CENTRAL_HEADER.size
        entry_end = variable_start + filename_length + extra_length + comment_length
        if entry_end < variable_start or entry_end > central_end:
            raise ProbeBlocked("archive_invalid")
        raw_name = data[variable_start : variable_start + filename_length]
        extra_start = variable_start + filename_length
        central_extra = data[extra_start : extra_start + extra_length]
        if disk_start:
            raise ProbeBlocked("archive_unsupported_entry")
        if (
            compress_size == 0xFFFFFFFF
            or file_size == 0xFFFFFFFF
            or header_offset == 0xFFFFFFFF
        ):
            raise ProbeBlocked("archive_unsupported_entry")
        if compress_type not in ALLOWED_COMPRESSION_METHODS:
            raise ProbeBlocked("archive_unsupported_entry")
        if compress_type == zipfile.ZIP_STORED and compress_size != file_size:
            raise ProbeBlocked("archive_invalid")
        _validate_zip_flags(flag_bits, compress_type)
        _validate_extra_fields(central_extra)
        decoded_name = _decode_zip_name(raw_name, flag_bits)
        entries.append(
            _RawCentralEntry(
                raw_name=raw_name,
                decoded_name=decoded_name,
                create_system=(version_made_by >> 8) & 0xFF,
                extract_version=extract_version,
                flag_bits=flag_bits,
                compress_type=compress_type,
                modified_time=modified_time,
                modified_date=modified_date,
                crc32=crc32,
                compress_size=compress_size,
                file_size=file_size,
                internal_attr=internal_attr,
                external_attr=external_attr,
                header_offset=header_offset,
                central_extra=central_extra,
            )
        )
        cursor = entry_end
    if cursor != central_end:
        raise ProbeBlocked("archive_invalid")
    return _RawZipIndex(entries=tuple(entries), central_offset=central_offset)


def _bind_raw_entry_to_zipinfo(
    raw_entry: _RawCentralEntry,
    info: zipfile.ZipInfo,
) -> None:
    if (
        raw_entry.decoded_name != str(info.orig_filename)
        or raw_entry.create_system != int(info.create_system)
        or raw_entry.extract_version != int(info.extract_version)
        or raw_entry.flag_bits != int(info.flag_bits)
        or raw_entry.compress_type != int(info.compress_type)
        or raw_entry.crc32 != int(info.CRC)
        or raw_entry.compress_size != int(info.compress_size)
        or raw_entry.file_size != int(info.file_size)
        or raw_entry.internal_attr != int(info.internal_attr)
        or raw_entry.external_attr != int(info.external_attr)
        or raw_entry.header_offset != int(info.header_offset)
        or raw_entry.central_extra != bytes(info.extra)
    ):
        raise ProbeBlocked("archive_invalid")


def _validate_raw_compressed_payload(
    data: bytes,
    *,
    data_start: int,
    central_offset: int,
    raw_entry: _RawCentralEntry,
) -> int:
    data_end = data_start + raw_entry.compress_size
    if data_end < data_start or data_end > central_offset:
        raise ProbeBlocked("archive_invalid")
    compressed = memoryview(data)[data_start:data_end]
    if raw_entry.compress_type == zipfile.ZIP_STORED:
        if raw_entry.compress_size != raw_entry.file_size:
            raise ProbeBlocked("archive_invalid")
        if zlib.crc32(compressed) & 0xFFFFFFFF != raw_entry.crc32:
            raise ProbeBlocked("archive_crc_invalid")
        return data_end

    if raw_entry.compress_type != zipfile.ZIP_DEFLATED:
        raise ProbeBlocked("archive_unsupported_entry")
    decompressor = zlib.decompressobj(-15)
    output_length = 0
    output_crc32 = 0
    cursor = 0
    try:
        while cursor < len(compressed):
            chunk_end = min(cursor + READ_CHUNK_BYTES, len(compressed))
            pending = compressed[cursor:chunk_end]
            cursor = chunk_end
            while pending:
                previous_length = len(pending)
                output = decompressor.decompress(pending, READ_CHUNK_BYTES)
                output_length += len(output)
                if output_length > raw_entry.file_size:
                    raise ProbeBlocked("archive_invalid")
                output_crc32 = zlib.crc32(output, output_crc32)
                if decompressor.unused_data:
                    raise ProbeBlocked("archive_invalid")
                pending = decompressor.unconsumed_tail
                if pending and len(pending) >= previous_length and not output:
                    raise ProbeBlocked("archive_invalid")
                if decompressor.eof and (pending or cursor < len(compressed)):
                    raise ProbeBlocked("archive_invalid")
        while not decompressor.eof:
            output = decompressor.decompress(b"", READ_CHUNK_BYTES)
            if not output:
                break
            output_length += len(output)
            if output_length > raw_entry.file_size:
                raise ProbeBlocked("archive_invalid")
            output_crc32 = zlib.crc32(output, output_crc32)
    except ProbeBlocked:
        raise
    except zlib.error:
        raise ProbeBlocked("archive_invalid")
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or output_length != raw_entry.file_size
        or output_crc32 & 0xFFFFFFFF != raw_entry.crc32
    ):
        raise ProbeBlocked("archive_invalid")
    return data_end


def _validate_data_descriptor(
    data: bytes,
    *,
    descriptor_offset: int,
    raw_entry: _RawCentralEntry,
) -> int:
    matches: list[int] = []
    if descriptor_offset >= 0 and descriptor_offset + 12 <= len(data):
        values = struct.unpack_from("<3L", data, descriptor_offset)
        if values == (
            raw_entry.crc32,
            raw_entry.compress_size,
            raw_entry.file_size,
        ):
            matches.append(descriptor_offset + 12)
    if (
        descriptor_offset >= 0
        and descriptor_offset + 16 <= len(data)
        and data[descriptor_offset : descriptor_offset + 4]
        == _ZIP_DATA_DESCRIPTOR_SIGNATURE
    ):
        values = struct.unpack_from("<3L", data, descriptor_offset + 4)
        if values == (
            raw_entry.crc32,
            raw_entry.compress_size,
            raw_entry.file_size,
        ):
            matches.append(descriptor_offset + 16)
    if len(matches) != 1:
        raise ProbeBlocked("archive_invalid")
    return matches[0]


def _validate_raw_local_header(
    data: bytes,
    raw_entry: _RawCentralEntry,
    *,
    central_offset: int,
) -> tuple[int, int]:
    """Bind every central-directory entry to its exact local header."""
    offset = raw_entry.header_offset
    if offset < 0 or offset + _ZIP_LOCAL_HEADER.size > len(data):
        raise ProbeBlocked("archive_invalid")
    fields = _ZIP_LOCAL_HEADER.unpack_from(data, offset)
    (
        signature,
        version_needed,
        local_flags,
        local_compression,
        modified_time,
        modified_date,
        local_crc32,
        local_compressed_size,
        local_uncompressed_size,
        filename_length,
        extra_length,
    ) = fields
    if signature != _ZIP_LOCAL_HEADER_SIGNATURE:
        raise ProbeBlocked("archive_invalid")
    variable_start = offset + _ZIP_LOCAL_HEADER.size
    data_start = variable_start + filename_length + extra_length
    if data_start < variable_start or data_start > central_offset:
        raise ProbeBlocked("archive_invalid")
    raw_name = data[variable_start : variable_start + filename_length]
    local_extra_start = variable_start + filename_length
    local_extra = data[local_extra_start : local_extra_start + extra_length]
    _validate_extra_fields(local_extra)
    if raw_name != raw_entry.raw_name:
        raise ProbeBlocked("archive_unsafe_entry")
    if _decode_zip_name(raw_name, local_flags) != raw_entry.decoded_name:
        raise ProbeBlocked("archive_unsafe_entry")
    if version_needed != raw_entry.extract_version:
        raise ProbeBlocked("archive_invalid")
    if local_flags != raw_entry.flag_bits:
        raise ProbeBlocked("archive_invalid")
    if local_compression != raw_entry.compress_type:
        raise ProbeBlocked("archive_invalid")
    if (
        modified_time != raw_entry.modified_time
        or modified_date != raw_entry.modified_date
    ):
        raise ProbeBlocked("archive_invalid")
    _validate_zip_flags(local_flags, local_compression)
    if (
        local_compressed_size == 0xFFFFFFFF
        or local_uncompressed_size == 0xFFFFFFFF
    ):
        raise ProbeBlocked("archive_unsupported_entry")

    data_end = _validate_raw_compressed_payload(
        data,
        data_start=data_start,
        central_offset=central_offset,
        raw_entry=raw_entry,
    )

    uses_descriptor = bool(local_flags & _ZIP_FLAG_DATA_DESCRIPTOR)
    if uses_descriptor:
        if local_crc32 not in {0, raw_entry.crc32}:
            raise ProbeBlocked("archive_invalid")
        if local_compressed_size not in {0, raw_entry.compress_size}:
            raise ProbeBlocked("archive_invalid")
        if local_uncompressed_size not in {0, raw_entry.file_size}:
            raise ProbeBlocked("archive_invalid")
        entry_end = _validate_data_descriptor(
            data,
            descriptor_offset=data_end,
            raw_entry=raw_entry,
        )
    elif (
        local_crc32 != raw_entry.crc32
        or local_compressed_size != raw_entry.compress_size
        or local_uncompressed_size != raw_entry.file_size
    ):
        raise ProbeBlocked("archive_invalid")
    else:
        entry_end = data_end
    if entry_end < data_start or entry_end > central_offset:
        raise ProbeBlocked("archive_invalid")
    return offset, entry_end


def _read_required_zip_entry(
    backup: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        with backup.open(info, "r") as handle:
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = handle.read(
                    min(READ_CHUNK_BYTES, maximum_bytes + 1 - observed)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
                if observed > maximum_bytes:
                    raise ProbeBlocked("archive_size_limit_exceeded")
        data = b"".join(chunks)
    except ProbeBlocked:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise ProbeBlocked("archive_crc_invalid")
    if len(data) != info.file_size:
        raise ProbeBlocked("archive_crc_invalid")
    if not data:
        raise ProbeBlocked("required_entry_empty")
    return data


def _inspect_archive(data: bytes) -> _ArchiveSnapshot:
    raw_index = _parse_raw_central_directory(data)
    try:
        backup = zipfile.ZipFile(BytesIO(data), "r")
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise ProbeBlocked("archive_invalid")

    with backup:
        infos = backup.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ProbeBlocked("archive_entry_count_invalid")
        if len(infos) != len(raw_index.entries):
            raise ProbeBlocked("archive_invalid")
        canonical_names: set[str] = set()
        required: dict[str, list[zipfile.ZipInfo]] = {
            "journal.jtx": [],
            "members.btx": [],
        }
        total_uncompressed = 0
        bound_entries: list[tuple[_RawCentralEntry, zipfile.ZipInfo]] = []
        for raw_entry, info in zip(raw_index.entries, infos, strict=True):
            _bind_raw_entry_to_zipinfo(raw_entry, info)
            bound_entries.append((raw_entry, info))
            canonical_name, is_directory = _safe_zip_entry_name(info)
            if canonical_name in canonical_names:
                raise ProbeBlocked("archive_duplicate_entry")
            canonical_names.add(canonical_name)
            if int(info.flag_bits) & 0x1:
                raise ProbeBlocked("archive_encrypted_entry")
            if info.compress_type not in ALLOWED_COMPRESSION_METHODS:
                raise ProbeBlocked("archive_unsupported_entry")
            if _zip_entry_is_nonregular(info, is_directory=is_directory):
                raise ProbeBlocked("archive_unsupported_entry")
            if info.file_size < 0 or info.compress_size < 0:
                raise ProbeBlocked("archive_invalid")
            if info.file_size > MAX_ENTRY_BYTES:
                raise ProbeBlocked("archive_size_limit_exceeded")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ProbeBlocked("archive_size_limit_exceeded")
            if info.file_size > 0:
                if info.compress_size <= 0:
                    raise ProbeBlocked("archive_compression_ratio_exceeded")
                if info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
                    raise ProbeBlocked("archive_compression_ratio_exceeded")
            if not is_directory:
                basename = PurePosixPath(canonical_name).name
                if basename in required:
                    required_limit = (
                        MAX_JOURNAL_ENTRY_BYTES
                        if basename == "journal.jtx"
                        else MAX_MEMBERS_ENTRY_BYTES
                    )
                    if info.file_size > required_limit:
                        raise ProbeBlocked("archive_size_limit_exceeded")
                    required[basename].append(info)

        if any(len(matches) > 1 for matches in required.values()):
            raise ProbeBlocked("archive_duplicate_required_entry")
        if any(not matches for matches in required.values()):
            raise ProbeBlocked("required_entry_missing")

        local_ranges = [
            _validate_raw_local_header(
                data,
                raw_entry,
                central_offset=raw_index.central_offset,
            )
            for raw_entry, _info in bound_entries
        ]
        ordered_ranges = sorted(local_ranges)
        if not ordered_ranges or ordered_ranges[0][0] != 0:
            raise ProbeBlocked("archive_invalid")
        for previous, current in zip(
            ordered_ranges,
            ordered_ranges[1:],
        ):
            if previous[1] != current[0]:
                raise ProbeBlocked("archive_invalid")
        if ordered_ranges[-1][1] != raw_index.central_offset:
            raise ProbeBlocked("archive_invalid")

        try:
            bad_entry = backup.testzip()
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise ProbeBlocked("archive_crc_invalid")
        if bad_entry is not None:
            raise ProbeBlocked("archive_crc_invalid")

        journal_bytes = _read_required_zip_entry(
            backup,
            required["journal.jtx"][0],
            maximum_bytes=MAX_JOURNAL_ENTRY_BYTES,
        )
        members_bytes = _read_required_zip_entry(
            backup,
            required["members.btx"][0],
            maximum_bytes=MAX_MEMBERS_ENTRY_BYTES,
        )
        return _ArchiveSnapshot(
            entry_count=len(infos),
            total_uncompressed_bytes=total_uncompressed,
            journal_bytes=journal_bytes,
            members_bytes=members_bytes,
        )


def _iter_catalog_lines(text: str):
    """Mirror ``str.splitlines()`` for text decoded strictly as Latin-1."""
    start = 0
    for delimiter in _CATALOG_LINE_BREAK_RE.finditer(text):
        yield text[start : delimiter.start()]
        start = delimiter.end()
    if start < len(text):
        yield text[start:]


def _validate_catalog_resource_bounds(text: str) -> None:
    record_count = 0
    option_or_addon_count = 0
    for raw_line in _iter_catalog_lines(text):
        if len(raw_line) > MAX_CATALOG_LINE_CHARS:
            raise ProbeBlocked("catalog_resource_limit_exceeded")
        record_count += 1
        if record_count > MAX_CATALOG_RECORDS:
            raise ProbeBlocked("catalog_resource_limit_exceeded")
        stripped = raw_line.strip()
        if stripped.startswith(("OPTION=", "MONTH_ADDON=")):
            option_or_addon_count += 1
            if option_or_addon_count > MAX_CATALOG_OPTION_OR_ADDON_ROWS:
                raise ProbeBlocked("catalog_resource_limit_exceeded")


def _analyze_snapshot(
    snapshot: _ArchiveSnapshot,
    allowlist: frozenset[str],
) -> tuple[dict, list[str]]:
    if not snapshot.journal_bytes.strip():
        raise ProbeBlocked("journal_empty")
    try:
        members_text = snapshot.members_bytes.decode("latin-1", errors="replace")
        _validate_catalog_resource_bounds(members_text)
        events, scope_issue_lines = _parse_allowlisted_membership_events(
            snapshot.journal_bytes,
            allowlist,
        )
        options, _addons = parse_gymassistant_billing_catalog_text(members_text)
    except ProbeBlocked:
        raise
    except Exception:
        raise ProbeBlocked("probe_internal_error")

    option_counts = Counter(
        (option.membership_type_id, option.billing_option_code)
        for option in options
    )
    duplicate_key_count = sum(
        count - 1 for count in option_counts.values() if count > 1
    )
    unique_options = {
        (option.membership_type_id, option.billing_option_code): option
        for option in options
        if option_counts[(option.membership_type_id, option.billing_option_code)] == 1
    }

    members_with_event = {event.member_id for event in events}
    positive_events = [
        event for event in events if event.is_positive_membership_payment
    ]
    members_with_positive = {event.member_id for event in positive_events}
    catalog_match_count = 0
    catalog_missing_count = 0
    amount_match_count = 0
    amount_mismatch_count = 0
    period_match_count = 0
    period_mismatch_count = 0
    period_missing_count = 0
    for event in events:
        option = unique_options.get(
            (event.membership_type_id, event.billing_option_code)
        )
        if option is None:
            catalog_missing_count += 1
            period_missing_count += 1
            continue
        catalog_match_count += 1
        if event.dues_cents == option.base_amount_cents:
            amount_match_count += 1
        else:
            amount_mismatch_count += 1
        try:
            period_matches = service_period_matches_catalog_interval(
                event.service_period_start,
                event.service_period_end_exclusive,
                option.interval_count,
                option.interval_unit,
            )
        except (TypeError, ValueError):
            period_matches = False
        if period_matches:
            period_match_count += 1
        else:
            period_mismatch_count += 1

    analysis = {
        "target_membership_event_count": len(events),
        "target_membership_parse_issue_count": scope_issue_lines,
        "target_member_with_event_count": len(members_with_event),
        "target_member_without_event_count": len(allowlist - members_with_event),
        "positive_membership_event_count": len(positive_events),
        "target_member_with_positive_event_count": len(members_with_positive),
        "target_member_without_positive_event_count": len(
            allowlist - members_with_positive
        ),
        "new_membership_event_count": sum(
            event.event_type == 1 for event in events
        ),
        "renewal_event_count": sum(
            event.event_type == 3 for event in events
        ),
        "voided_event_count": sum(event.is_voided for event in events),
        "manual_review_event_count": sum(
            event.requires_manual_review for event in events
        ),
        "nonpositive_membership_event_count": sum(
            not event.is_positive_membership_payment for event in events
        ),
        "catalog_option_count": len(options),
        "catalog_duplicate_key_count": duplicate_key_count,
        "catalog_match_count": catalog_match_count,
        "catalog_missing_count": catalog_missing_count,
        "catalog_amount_match_count": amount_match_count,
        "catalog_amount_mismatch_count": amount_mismatch_count,
        "catalog_period_match_count": period_match_count,
        "catalog_period_mismatch_count": period_mismatch_count,
        "catalog_period_missing_count": period_missing_count,
    }
    reasons: list[str] = []
    if scope_issue_lines:
        reasons.append("target_membership_parse_issue")
    if not options:
        reasons.append("catalog_empty")
    if duplicate_key_count:
        reasons.append("catalog_ambiguous")
    return analysis, reasons


def probe_invoice_backup(
    backup_path: Path,
    *,
    expected_length: int,
    expected_sha256: str,
    allowlist_path: Path,
    expected_allowlist_count: int,
    expected_allowlist_set_sha256: str,
) -> dict:
    result = _empty_result()
    try:
        if (
            not _lexically_local_absolute_windows_path(backup_path)
            or not _lexically_local_absolute_windows_path(allowlist_path)
        ):
            raise ProbeBlocked("backup_path_invalid")
        if (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or expected_length < 1
            or expected_length > MAX_ARCHIVE_BYTES
        ):
            raise ProbeBlocked("invalid_arguments")
        expected_source_hash = str(expected_sha256 or "").strip().casefold()
        expected_allowlist_hash = str(
            expected_allowlist_set_sha256 or ""
        ).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_hash):
            raise ProbeBlocked("invalid_arguments")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_allowlist_hash):
            raise ProbeBlocked("invalid_arguments")
        if (
            not isinstance(expected_allowlist_count, int)
            or isinstance(expected_allowlist_count, bool)
            or expected_allowlist_count < 1
            or expected_allowlist_count > 100
        ):
            raise ProbeBlocked("invalid_arguments")

        allowlist_read = _read_stable_file_twice(
            Path(allowlist_path),
            maximum_bytes=MAX_ALLOWLIST_BYTES,
        )
        allowlist, allowlist_hash = _canonical_allowlist(allowlist_read.data)
        if (
            len(allowlist) != expected_allowlist_count
            or allowlist_hash != expected_allowlist_hash
        ):
            raise ProbeBlocked("allowlist_binding_mismatch")
        result["allowlist"] = {
            "count": len(allowlist),
            "set_sha256": allowlist_hash,
        }

        stable = _read_stable_file_twice(
            Path(backup_path),
            maximum_bytes=MAX_ARCHIVE_BYTES,
            expected_length=expected_length,
        )
        if stable.source_sha256 != expected_source_hash:
            raise ProbeBlocked("source_hash_mismatch")
        result["source"] = {
            "kind": SOURCE_KIND,
            "byte_length": stable.byte_length,
            "source_sha256": stable.source_sha256,
            "file_identity_sha256": stable.file_identity_sha256,
            "stable_read_count": stable.stable_read_count,
        }

        snapshot = _inspect_archive(stable.data)
        result["archive"] = {
            "entry_count": snapshot.entry_count,
            "total_uncompressed_bytes": snapshot.total_uncompressed_bytes,
            "journal_byte_length": len(snapshot.journal_bytes),
            "journal_sha256": sha256(snapshot.journal_bytes).hexdigest(),
            "members_byte_length": len(snapshot.members_bytes),
            "members_sha256": sha256(snapshot.members_bytes).hexdigest(),
            "crc_verified": True,
        }
        analysis, reasons = _analyze_snapshot(snapshot, allowlist)
        result["analysis"] = analysis
        result["reason_codes"] = sorted(set(reasons))
        if not reasons:
            result["status"] = "passed"
            result["integrity_proven"] = True
        return result
    except ProbeBlocked as exc:
        result["reason_codes"] = [exc.reason_code]
        return result
    except Exception:
        result["reason_codes"] = ["probe_internal_error"]
        return result


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--backup-path", required=True, type=Path)
    parser.add_argument("--expected-length", required=True, type=int)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--allowlist-path", required=True, type=Path)
    parser.add_argument("--expected-allowlist-count", required=True, type=int)
    parser.add_argument("--expected-allowlist-set-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        result = probe_invoice_backup(
            args.backup_path,
            expected_length=args.expected_length,
            expected_sha256=args.expected_sha256,
            allowlist_path=args.allowlist_path,
            expected_allowlist_count=args.expected_allowlist_count,
            expected_allowlist_set_sha256=args.expected_allowlist_set_sha256,
        )
    except Exception as exc:
        result = _empty_result()
        result["reason_codes"] = [
            exc.reason_code if isinstance(exc, ProbeBlocked) else "probe_internal_error"
        ]
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if result["integrity_proven"] else 2


if __name__ == "__main__":
    sys.exit(main())
