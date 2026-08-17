from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Iterable


CLASSIFIER_VERSION = "dreamz.ga.journal.export-records.v2"
VOIDED_EVENT_MASK = 0x8000
EXPORT_VERSION_RECORD = b"2.0c"

_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP_RE = re.compile(rb"c\d{8}!\d{4}")
_HEX_RE = re.compile(rb"[0-9A-Fa-f]+")
_UNSIGNED_DECIMAL_RE = re.compile(rb"[0-9]+")
_SIGNED_DECIMAL_RE = re.compile(rb"-?[0-9]+")


@dataclass(frozen=True)
class MemberJournalScopeIssue:
    line_number: int
    code: str


@dataclass(frozen=True)
class OfficialJournalExportCoverage:
    classifier_version: str
    complete: bool
    byte_length: int
    source_sha256: str
    record_count: int
    supported_record_count: int
    member_record_count: int
    global_record_count: int
    version_record_count: int
    header_only_record_count: int
    empty_payload_record_count: int
    embedded_lf_record_count: int
    issues: tuple[MemberJournalScopeIssue, ...]

    def as_sanitized_dict(self) -> dict:
        return {
            "classifier_version": self.classifier_version,
            "complete": self.complete,
            "byte_length": self.byte_length,
            "source_sha256": self.source_sha256,
            "record_count": self.record_count,
            "supported_record_count": self.supported_record_count,
            "member_record_count": self.member_record_count,
            "global_record_count": self.global_record_count,
            "version_record_count": self.version_record_count,
            "header_only_record_count": self.header_only_record_count,
            "empty_payload_record_count": self.empty_payload_record_count,
            "embedded_lf_record_count": self.embedded_lf_record_count,
            "issue_count": len(self.issues),
            "issue_codes": sorted({issue.code for issue in self.issues}),
        }


@dataclass(frozen=True)
class MemberScopedJournalSnapshot:
    member_number: str
    classifier_version: str
    complete: bool
    member_record_count: int
    member_record_multiset_sha256: str
    member_record_hashes: tuple[str, ...]
    issues: tuple[MemberJournalScopeIssue, ...]

    def as_evidence_scope(self) -> dict:
        """Return only the sanitized fields allowed in an evidence envelope."""
        return {
            "classifier_version": self.classifier_version,
            "complete": self.complete,
            "member_record_count": self.member_record_count,
            "member_record_multiset_sha256": self.member_record_multiset_sha256,
            "issue_count": len(self.issues),
        }


def _canonical_member_number(value: str | int) -> str:
    member_number = str(value).strip()
    if not member_number.isascii() or not member_number.isdigit():
        raise ValueError("member number must contain ASCII digits only")
    canonical = str(int(member_number))
    if canonical == "0":
        raise ValueError("member number must be positive")
    return canonical


def _canonical_json_bytes(value: dict) -> bytes:
    # This payload uses only strings, non-negative integers and arrays, for
    # which sorted compact JSON is byte-identical to RFC 8785/JCS.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_member_record_multiset_sha256(
    member_number: str | int,
    record_hashes: Iterable[str],
    *,
    classifier_version: str = CLASSIFIER_VERSION,
) -> str:
    canonical_member = _canonical_member_number(member_number)
    hashes = tuple(sorted(str(value) for value in record_hashes))
    if any(not _HEX_SHA256_RE.fullmatch(value) for value in hashes):
        raise ValueError("member record hashes must be lowercase SHA-256 values")
    manifest = {
        "classifier_version": classifier_version,
        "member_number": canonical_member,
        "member_record_count": len(hashes),
        "member_record_sha256": list(hashes),
    }
    return sha256(_canonical_json_bytes(manifest)).hexdigest()


def _split_export_records(data: bytes) -> list[bytes]:
    """Split Gym Assistant export records without splitting payload newlines.

    The official export terminates records with CRLF. Lone LF bytes can occur
    inside an opaque payload and therefore remain part of the record hash.
    Lone CR bytes are not part of the observed export grammar.
    """
    return data.split(b"\r\n")


def _record_header(record: bytes) -> bytes:
    return record.split(b"|", 1)[0]


def _attributable_member_number(record: bytes) -> str | None:
    header = _record_header(record)
    tokens = header.split()
    if (
        len(tokens) != 11
        or not tokens[5].isascii()
        or not _UNSIGNED_DECIMAL_RE.fullmatch(tokens[5])
    ):
        return None
    try:
        member_number = str(int(tokens[5]))
    except ValueError:
        return None
    return member_number if member_number != "0" else None


def _valid_journal_header(record: bytes) -> tuple[str, int] | None:
    """Return the member scope for one complete official-export record.

    Payload presence and contents are deliberately irrelevant. Gym Assistant
    emits both header-only records and records with an empty payload. The one
    observed signed sentinel is restricted to header field 7; all other
    decimal fields retain their unsigned 32-bit contract.
    """
    if b"\r" in record:
        return None
    header = _record_header(record)
    tokens = header.split()
    if len(tokens) != 11 or any(not token for token in tokens):
        return None
    if not _TIMESTAMP_RE.fullmatch(tokens[0]) or not _HEX_RE.fullmatch(tokens[1]):
        return None
    if any(
        not (
            _SIGNED_DECIMAL_RE.fullmatch(token)
            if index == 7
            else _UNSIGNED_DECIMAL_RE.fullmatch(token)
        )
        for index, token in enumerate(tokens[2:], start=2)
    ):
        return None
    try:
        datetime.strptime(tokens[0].decode("ascii"), "c%Y%m%d!%H%M")
        int(tokens[1], 16)
        numeric_fields = [int(token, 10) for token in tokens[2:]]
    except (UnicodeDecodeError, ValueError):
        return None
    if any(
        value > 0xFFFFFFFF
        or (value < 0 and not (index == 7 and value == -1))
        for index, value in enumerate(numeric_fields, start=2)
    ):
        return None
    member_number = numeric_fields[3]
    raw_event_type = numeric_fields[4]
    if member_number < 0 or raw_event_type < 0 or raw_event_type > 0xFFFF:
        return None
    return str(member_number), raw_event_type


def validate_official_journal_export_bytes(
    data: bytes,
) -> OfficialJournalExportCoverage:
    """Validate the complete grammar of one official Gym Assistant export.

    Journal payloads stay opaque. Coverage is proven only when the export has
    exactly one leading version record and every remaining nonempty CRLF
    record has the observed fixed 11-field header grammar.
    """
    if not isinstance(data, bytes):
        raise TypeError("journal export input must be bytes")

    record_count = 0
    supported_record_count = 0
    member_record_count = 0
    global_record_count = 0
    version_record_count = 0
    header_only_record_count = 0
    empty_payload_record_count = 0
    embedded_lf_record_count = 0
    issues: list[MemberJournalScopeIssue] = []

    for raw_record in _split_export_records(data):
        if not raw_record:
            continue
        record_count += 1
        if raw_record == EXPORT_VERSION_RECORD:
            version_record_count += 1
            if record_count != 1 or version_record_count != 1:
                issues.append(
                    MemberJournalScopeIssue(
                        record_count,
                        "unexpected_export_version_record",
                    )
                )
            continue

        parsed_header = _valid_journal_header(raw_record)
        if parsed_header is None:
            issues.append(
                MemberJournalScopeIssue(record_count, "unsupported_export_record")
            )
            continue

        supported_record_count += 1
        member_number, _raw_event_type = parsed_header
        if member_number == "0":
            global_record_count += 1
        else:
            member_record_count += 1
        if b"|" not in raw_record:
            header_only_record_count += 1
        else:
            payload = raw_record.split(b"|", 1)[1]
            if not payload:
                empty_payload_record_count += 1
            if b"\n" in payload:
                embedded_lf_record_count += 1

    if record_count == 0:
        issues.append(MemberJournalScopeIssue(0, "journal_export_empty"))
    if supported_record_count == 0:
        issues.append(MemberJournalScopeIssue(0, "journal_export_no_data_records"))
    if version_record_count == 0:
        issues.append(MemberJournalScopeIssue(0, "export_version_missing"))
    elif version_record_count > 1:
        issues.append(MemberJournalScopeIssue(0, "export_version_not_unique"))

    return OfficialJournalExportCoverage(
        classifier_version=CLASSIFIER_VERSION,
        complete=not issues,
        byte_length=len(data),
        source_sha256=sha256(data).hexdigest(),
        record_count=record_count,
        supported_record_count=supported_record_count,
        member_record_count=member_record_count,
        global_record_count=global_record_count,
        version_record_count=version_record_count,
        header_only_record_count=header_only_record_count,
        empty_payload_record_count=empty_payload_record_count,
        embedded_lf_record_count=embedded_lf_record_count,
        issues=tuple(issues),
    )


def parse_member_scoped_journal_snapshot_bytes(
    data: bytes,
    *,
    member_number: str | int,
    source_coverage_proven: bool = False,
) -> MemberScopedJournalSnapshot:
    """Build a privacy-safe multiset proof for every target-member journal row.

    Payload formats and event types are deliberately not interpreted. A row is
    selected only through the fixed member field in a structurally valid
    11-token header, then hashed byte-for-byte without its CR/LF terminator.
    """
    if not isinstance(data, bytes):
        raise TypeError("journal snapshot input must be bytes")
    canonical_member = _canonical_member_number(member_number)
    record_hashes: list[str] = []
    issues: list[MemberJournalScopeIssue] = []

    coverage = validate_official_journal_export_bytes(data)
    if not coverage.complete:
        issues.append(MemberJournalScopeIssue(0, "source_coverage_invalid"))

    version_seen = False
    record_number = 0
    for raw_record in _split_export_records(data):
        if not raw_record.strip(b" \t"):
            continue
        record_number += 1
        if raw_record == EXPORT_VERSION_RECORD:
            if record_number == 1 and not version_seen:
                version_seen = True
                continue
            issues.append(
                MemberJournalScopeIssue(
                    record_number,
                    "unexpected_export_version_record",
                )
            )
            continue

        parsed_header = _valid_journal_header(raw_record)
        if parsed_header is not None:
            row_member_number, _raw_event_type = parsed_header
            if row_member_number == canonical_member:
                record_hashes.append(sha256(raw_record).hexdigest())
            continue

        attributable_member = _attributable_member_number(raw_record)
        if attributable_member == canonical_member:
            issues.append(
                MemberJournalScopeIssue(record_number, "malformed_target_record")
            )
        elif attributable_member is None:
            issues.append(
                MemberJournalScopeIssue(
                    record_number,
                    "malformed_record_scope_ambiguous",
                )
            )

    if not source_coverage_proven:
        issues.append(MemberJournalScopeIssue(0, "source_coverage_unproven"))

    sorted_hashes = tuple(sorted(record_hashes))
    return MemberScopedJournalSnapshot(
        member_number=canonical_member,
        classifier_version=CLASSIFIER_VERSION,
        complete=not issues,
        member_record_count=len(sorted_hashes),
        member_record_multiset_sha256=(
            canonical_member_record_multiset_sha256(
                canonical_member,
                sorted_hashes,
            )
        ),
        member_record_hashes=sorted_hashes,
        issues=tuple(issues),
    )
