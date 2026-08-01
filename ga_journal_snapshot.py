from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Iterable


CLASSIFIER_VERSION = "dreamz.ga.journal.member-lines.v1"
VOIDED_EVENT_MASK = 0x8000

_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_JOURNAL_HEADER_RE = re.compile(
    rb"^(c\d{8}!\d{4})[ \t]+([0-9A-Fa-f]+)"
    rb"(?:[ \t]+([0-9]+)){9}\|(.+)$",
    flags=re.DOTALL,
)


@dataclass(frozen=True)
class MemberJournalScopeIssue:
    line_number: int
    code: str


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


def _split_cr_lf_lines(data: bytes) -> list[bytes]:
    """Split only CR/LF terminators; other byte values remain record bytes."""
    return re.split(rb"\r\n|\n|\r", data)


def _attributable_member_number(line: bytes) -> str | None:
    header = line.split(b"|", 1)[0]
    tokens = header.split()
    if len(tokens) != 11 or not tokens[5].isascii() or not tokens[5].isdigit():
        return None
    try:
        member_number = str(int(tokens[5]))
    except ValueError:
        return None
    return member_number if member_number != "0" else None


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
        int(tokens[1], 16)
        numeric_fields = [int(token, 10) for token in tokens[2:]]
    except (UnicodeDecodeError, ValueError):
        return None
    if any(value < 0 or value > 0xFFFFFFFF for value in numeric_fields):
        return None
    member_number = numeric_fields[3]
    raw_event_type = numeric_fields[4]
    if member_number <= 0 or raw_event_type > 0xFFFF:
        return None
    return str(member_number), raw_event_type


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

    for line_number, line in enumerate(_split_cr_lf_lines(data), start=1):
        if not line.strip(b" \t"):
            continue
        parsed_header = _valid_journal_header(line)
        if parsed_header is not None:
            row_member_number, _raw_event_type = parsed_header
            if row_member_number == canonical_member:
                record_hashes.append(sha256(line).hexdigest())
            continue

        attributable_member = _attributable_member_number(line)
        if attributable_member == canonical_member:
            issues.append(
                MemberJournalScopeIssue(line_number, "malformed_target_record")
            )
        elif attributable_member is None:
            issues.append(
                MemberJournalScopeIssue(
                    line_number,
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
