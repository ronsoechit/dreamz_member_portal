from __future__ import annotations

"""Read-only, target-scoped Gym Assistant invoice readiness probe.

The historical Gym Assistant journal contains system and legacy fragments that
are unrelated to the explicitly selected invoice members.  This probe keeps
the archive and allowlist integrity gates from :mod:`ga_invoice_backup_probe`,
but its journal completeness claim is deliberately limited to the bound
allowlist.  It never authorizes invoice issuing and never returns member IDs,
names, dates, amounts, raw rows, paths, or per-record hashes.
"""

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence

# This probe promises a no-write execution. Set the interpreter policy before
# importing either local module so a direct invocation cannot create pyc files.
sys.dont_write_bytecode = True

import ga_invoice_backup_probe as archive_probe
from ga_journal import (
    VOIDED_EVENT_MASK,
    parse_gymassistant_billing_catalog_text,
    parse_membership_journal_line,
    service_period_matches_catalog_interval,
)


TARGET_PROBE_SCHEMA = "dreamz.ga.invoice-target-readiness.v1"
MAX_BOUNDARY_SCAN_BYTES = 512
MAX_JOURNAL_RECORDS = 500_000
MAX_IGNORED_SHORT_FRAGMENTS = 8
_CANONICAL_TIMESTAMP_START_RE = re.compile(rb"c\d{8}!\d{4}")


@dataclass(frozen=True)
class _TargetScan:
    events: tuple
    target_candidate_count: int
    ambiguous_target_candidate_count: int
    embedded_target_candidate_count: int
    cross_boundary_target_candidate_count: int
    target_issue_count: int
    ignored_short_fragment_count: int


@dataclass(frozen=True)
class _TargetCandidate:
    start: int
    end: int
    kind: str


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - sanitized CLI
        raise archive_probe.ProbeBlocked("invalid_arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise archive_probe.ProbeBlocked("invalid_arguments")


def _empty_result() -> dict:
    return {
        "schema": TARGET_PROBE_SCHEMA,
        "mode": "read_only",
        "scope": "exact_allowlist_only",
        "status": "blocked",
        "target_scope_proven": False,
        "ready_for_target_sync": False,
        "authorizes_invoice_issuing": False,
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
            "expected_target_membership_event_count": 0,
            "target_header_candidate_count": 0,
            "ambiguous_target_candidate_count": 0,
            "embedded_target_candidate_count": 0,
            "cross_boundary_target_candidate_count": 0,
            "target_issue_count": 0,
            "ignored_short_fragment_count": 0,
            "expected_short_fragment_count": 0,
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
        "privacy": {
            "member_ids_returned": False,
            "names_returned": False,
            "dates_returned": False,
            "payment_values_returned": False,
            "paths_returned": False,
            "raw_rows_returned": False,
            "per_record_hashes_returned": False,
            "files_written": False,
            "network_used": False,
        },
    }


def _normalized_target_member(token: bytes, allowlist: frozenset[str]) -> bool:
    if not token or not token.isascii() or not token.isdigit():
        return False
    try:
        value = int(token, 10)
    except ValueError:
        return False
    return 0 < value <= 0xFFFFFFFF and str(value) in allowlist


def _strict_header_fields(header: bytes) -> tuple[int, int] | None:
    """Return fixed member/event fields only for an exact GA header."""

    if archive_probe._JOURNAL_HEADER_CORE_RE.fullmatch(header) is None:
        return None
    tokens = header.split()
    if len(tokens) != 11:
        return None
    try:
        datetime.strptime(tokens[0].decode("ascii"), "c%Y%m%d!%H%M")
        sequence = int(tokens[1], 16)
        numeric_fields = [int(token, 10) for token in tokens[2:]]
    except (UnicodeDecodeError, ValueError):
        return None
    if sequence > 0xFFFFFFFF or any(
        value > 0xFFFFFFFF for value in numeric_fields
    ):
        return None
    member_number = numeric_fields[3]
    raw_event_type = numeric_fields[4]
    if raw_event_type > 0xFFFF:
        return None
    return member_number, raw_event_type


def _target_candidates(
    raw_line: bytes,
    allowlist: frozenset[str],
) -> tuple[_TargetCandidate, ...]:
    """Find target membership or ambiguous target headers at every byte offset.

    Every canonical timestamp start is inspected, not only byte zero.  This
    catches a target row appended behind a non-target row when a CR/LF record
    separator is missing.  A target member with a missing, nonnumeric, or
    out-of-range event token is ambiguous and therefore blocking.
    """

    candidates: list[_TargetCandidate] = []
    first_nonspace = len(raw_line) - len(raw_line.lstrip(b" \t"))
    seen_starts: set[int] = set()
    saw_target_start = False

    for match in _CANONICAL_TIMESTAMP_START_RE.finditer(raw_line):
        start = match.start()
        if start in seen_starts:
            continue
        seen_starts.add(start)
        tail = raw_line[start:]
        header, separator, _payload = tail.partition(b"|")
        tokens = header.split()
        end = start + len(header) + (1 if separator else 0)
        if len(tokens) <= 5:
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        if not tokens[5].isascii() or not tokens[5].isdigit():
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        if not _normalized_target_member(tokens[5], allowlist):
            continue
        saw_target_start = True
        if tokens[5] != str(int(tokens[5], 10)).encode("ascii"):
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        if len(tokens) <= 6:
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        raw_event_token = tokens[6]
        if not raw_event_token.isascii() or not raw_event_token.isdigit():
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        try:
            raw_event_type = int(raw_event_token, 10)
        except ValueError:
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        if raw_event_type > 0xFFFF:
            candidates.append(_TargetCandidate(start, end, "ambiguous"))
            continue
        event_type = raw_event_type & ~VOIDED_EVENT_MASK
        if event_type in {1, 3}:
            candidates.append(_TargetCandidate(start, end, "membership"))
        elif not separator and len(tokens) != 11:
            # A target header-shaped fragment with no complete event framing is
            # ambiguous even when its visible event token is non-membership.
            candidates.append(_TargetCandidate(start, end, "ambiguous"))

    # Preserve the older fail-closed handling for malformed line-start target
    # headers whose timestamp is not canonical and therefore was not found by
    # the any-offset timestamp scan above.
    attributable = archive_probe._attributable_member_number(raw_line)
    if attributable in allowlist and not saw_target_start:
        header = raw_line.split(b"|", 1)[0]
        tokens = header.split()
        if len(tokens) <= 6:
            candidates.append(
                _TargetCandidate(first_nonspace, len(raw_line), "ambiguous")
            )
        else:
            event_token = tokens[6]
            if not event_token.isascii() or not event_token.isdigit():
                candidates.append(
                    _TargetCandidate(
                        first_nonspace,
                        len(raw_line),
                        "ambiguous",
                    )
                )
            else:
                raw_event_type = int(event_token, 10)
                if raw_event_type > 0xFFFF:
                    candidates.append(
                        _TargetCandidate(
                            first_nonspace,
                            len(raw_line),
                            "ambiguous",
                        )
                    )
                elif raw_event_type & ~VOIDED_EVENT_MASK in {1, 3}:
                    candidates.append(
                        _TargetCandidate(
                            first_nonspace,
                            len(raw_line),
                            "membership",
                        )
                    )

    # A malformed target header can be appended behind another record and have
    # a non-canonical timestamp, or a missing/extra field can shift its member
    # and event pair. Inspect every fixed-size window before every pipe. This is
    # linear because the window size is constant and each physical record is
    # already capped. A strict non-target header may legitimately contain a
    # target-shaped number in another field; a malformed window may not.
    segment_start = 0
    for pipe_match in re.finditer(rb"\|", raw_line):
        segment = raw_line[segment_start : pipe_match.start()]
        token_matches = list(re.finditer(rb"[^ \t]+", segment))
        if len(token_matches) < 11:
            target_matches = tuple(
                token_match
                for token_match in token_matches
                if _normalized_target_member(
                    token_match.group(0),
                    allowlist,
                )
            )
            if target_matches:
                candidates.append(
                    _TargetCandidate(
                        segment_start + target_matches[0].start(),
                        pipe_match.end(),
                        "ambiguous",
                    )
                )
        for index in range(max(0, len(token_matches) - 10)):
            window = token_matches[index : index + 11]
            target_positions = tuple(
                position
                for position, token_match in enumerate(window)
                if _normalized_target_member(
                    token_match.group(0),
                    allowlist,
                )
            )
            if not target_positions:
                continue

            header = segment[window[0].start() : window[-1].end()]
            strict_fields = _strict_header_fields(header)
            kind = "ambiguous"
            if strict_fields is not None:
                member_number, raw_event_type = strict_fields
                fixed_member_token = window[5].group(0)
                fixed_is_target = str(member_number) in allowlist
                if fixed_is_target:
                    if (
                        fixed_member_token
                        != str(member_number).encode("ascii")
                    ):
                        kind = "ambiguous"
                    elif raw_event_type & ~VOIDED_EVENT_MASK in {1, 3}:
                        kind = "membership"
                    else:
                        continue
                else:
                    shifted_membership_pair = any(
                        position != 5
                        and position + 1 < len(window)
                        and window[position + 1].group(0).isascii()
                        and window[position + 1].group(0).isdigit()
                        and int(window[position + 1].group(0), 10)
                        <= 0xFFFF
                        and int(window[position + 1].group(0), 10)
                        & ~VOIDED_EVENT_MASK
                        in {1, 3}
                        for position in target_positions
                    )
                    if not shifted_membership_pair:
                        continue

            candidates.append(
                _TargetCandidate(
                    segment_start + window[0].start(),
                    pipe_match.end(),
                    kind,
                )
            )
        segment_start = pipe_match.end()

    # Deduplicate the canonical and window scans by byte span.  Ambiguity wins
    # over a membership label for the same span.
    candidates_by_span: dict[tuple[int, int], _TargetCandidate] = {}
    for candidate in candidates:
        key = (candidate.start, candidate.end)
        previous = candidates_by_span.get(key)
        if previous is None or candidate.kind == "ambiguous":
            candidates_by_span[key] = candidate
    return tuple(candidates_by_span.values())


def _safe_short_fragment(
    raw_line: bytes,
    allowlist: frozenset[str],
) -> bool:
    if (
        not 1 <= len(raw_line) <= 15
        or b"|" in raw_line
        or _CANONICAL_TIMESTAMP_START_RE.search(raw_line)
        or any(value < 0x20 or value > 0x7E for value in raw_line)
        or len(raw_line.split()) > 5
    ):
        return False
    for digit_run in re.findall(rb"[0-9]+", raw_line):
        if _normalized_target_member(digit_run, allowlist):
            return False
    return True


def _proven_outside_target_membership(
    raw_line: bytes,
    allowlist: frozenset[str],
) -> bool:
    header = raw_line.split(b"|", 1)[0]
    fields = _strict_header_fields(header)
    if fields is None:
        # A malformed legacy row may be ignored only when its fixed member
        # field remains a bounded non-target member and no token anywhere in
        # the malformed header can normalize to one of the selected members.
        # This keeps known non-target legacy noise out of scope without letting
        # a shifted target member field hide behind a malformed header.
        if any(
            _normalized_target_member(token, allowlist)
            for token in header.split()
        ):
            return False
        attributable = archive_probe._attributable_member_number(raw_line)
        return attributable is not None and attributable not in allowlist
    member_number, raw_event_type = fields
    event_type = raw_event_type & ~VOIDED_EVENT_MASK
    if member_number == 0:
        return event_type not in {1, 3}
    if str(member_number) not in allowlist:
        return True
    return event_type not in {1, 3}


def _candidate_crosses_boundary(
    data: bytes,
    boundary_positions: tuple[int, ...],
    allowlist: frozenset[str],
) -> int:
    count = 0
    for candidate in _target_candidates(data, allowlist):
        if any(
            candidate.start < boundary < candidate.end
            for boundary in boundary_positions
        ):
            count += 1
    return count


def _fragment_cluster_boundary_candidate_count(
    records: list[bytes],
    fragment_indices: list[int],
    allowlist: frozenset[str],
) -> int:
    """Detect target headers spanning one or more tiny fragment records."""

    count = 0
    if not fragment_indices:
        return count
    clusters: list[tuple[int, int]] = []
    start = previous = fragment_indices[0]
    for index in fragment_indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        clusters.append((start, previous))
        start = previous = index
    clusters.append((start, previous))

    for fragment_start, fragment_end in clusters:
        window_start = max(0, fragment_start - 1)
        window_end = min(len(records) - 1, fragment_end + 1)
        components = []
        for index in range(window_start, window_end + 1):
            value = records[index]
            if index == window_start and index < fragment_start:
                value = value[-MAX_BOUNDARY_SCAN_BYTES:]
            if index == window_end and index > fragment_end:
                value = value[:MAX_BOUNDARY_SCAN_BYTES]
            components.append(value)

        boundary_count = len(components) - 1
        if boundary_count > MAX_IGNORED_SHORT_FRAGMENTS + 1:
            raise archive_probe.ProbeBlocked("target_event_limit_exceeded")
        for mask in range(1 << boundary_count):
            joined = bytearray(components[0])
            boundaries: list[int] = []
            for offset, component in enumerate(components[1:]):
                boundaries.append(len(joined))
                if mask & (1 << offset):
                    joined.extend(b" ")
                joined.extend(component)
            count += _candidate_crosses_boundary(
                bytes(joined),
                tuple(boundaries),
                allowlist,
            )
    return count


def _scan_target_events(
    journal_bytes: bytes,
    allowlist: frozenset[str],
) -> _TargetScan:
    records: list[bytes] = []
    events: list = []
    target_candidates = 0
    ambiguous_candidates = 0
    embedded_candidates = 0
    target_issues = 0
    ignored_short_fragments = 0
    short_fragment_indices: list[int] = []

    for _line_number, raw_line in archive_probe._iter_cr_lf_lines(
        journal_bytes
    ):
        if not raw_line.strip(b" \t"):
            continue
        records.append(raw_line)
        if len(records) > MAX_JOURNAL_RECORDS:
            raise archive_probe.ProbeBlocked("target_event_limit_exceeded")
        if len(raw_line) > archive_probe.MAX_JOURNAL_RECORD_BYTES:
            target_issues += 1
            continue
        if archive_probe._contains_non_crlf_line_separator(raw_line):
            target_issues += 1
            continue

        candidates = _target_candidates(
            raw_line,
            allowlist,
        )
        membership_count = sum(
            candidate.kind == "membership" for candidate in candidates
        )
        ambiguous_count = sum(
            candidate.kind == "ambiguous" for candidate in candidates
        )
        first_nonspace = len(raw_line) - len(raw_line.lstrip(b" \t"))
        embedded_count = sum(
            candidate.start != first_nonspace for candidate in candidates
        )
        target_candidates += membership_count
        ambiguous_candidates += ambiguous_count
        embedded_candidates += embedded_count

        try:
            decoded = raw_line.decode("latin-1", errors="strict")
            event = parse_membership_journal_line(decoded)
        except (TypeError, UnicodeDecodeError, ValueError):
            event = None

        if event is not None and event.member_id in allowlist:
            if membership_count == 1 and not ambiguous_count and not embedded_count:
                events.append(event)
            else:
                target_issues += 1
        elif membership_count or ambiguous_count or embedded_count:
            target_issues += 1
        elif _safe_short_fragment(raw_line, allowlist):
            # A tiny standalone fragment cannot contain the complete 11-field
            # header.  Boundary reconstruction below still blocks if it can be
            # part of a target header split across CR/LF.
            ignored_short_fragments += 1
            if ignored_short_fragments > MAX_IGNORED_SHORT_FRAGMENTS:
                raise archive_probe.ProbeBlocked(
                    "target_event_limit_exceeded"
                )
            short_fragment_indices.append(len(records) - 1)
        elif not _proven_outside_target_membership(raw_line, allowlist):
            target_issues += 1

        if len(events) > archive_probe.MAX_TARGET_MEMBERSHIP_EVENTS:
            raise archive_probe.ProbeBlocked("target_event_limit_exceeded")

    cross_boundary = _fragment_cluster_boundary_candidate_count(
        records,
        short_fragment_indices,
        allowlist,
    )
    target_issues += cross_boundary
    if target_candidates != len(events):
        target_issues += 1

    return _TargetScan(
        events=tuple(events),
        target_candidate_count=target_candidates,
        ambiguous_target_candidate_count=ambiguous_candidates,
        embedded_target_candidate_count=embedded_candidates,
        cross_boundary_target_candidate_count=cross_boundary,
        target_issue_count=target_issues,
        ignored_short_fragment_count=ignored_short_fragments,
    )


def scan_target_invoice_membership_events(
    journal_bytes: bytes,
    member_ids: Iterable[str],
) -> _TargetScan:
    """Run the exact target scanner used by readiness and production sync."""

    if not isinstance(journal_bytes, bytes):
        raise ValueError("journal_bytes must be bytes")
    canonical_ids: list[str] = []
    for raw_member_id in member_ids:
        member_id = str(raw_member_id).strip()
        if (
            not re.fullmatch(r"[1-9][0-9]*", member_id)
            or int(member_id, 10) > 0xFFFFFFFF
        ):
            raise ValueError("invoice member ID is invalid")
        canonical_ids.append(member_id)
    if not canonical_ids or len(canonical_ids) != len(set(canonical_ids)):
        raise ValueError("invoice member IDs must be non-empty and unique")
    try:
        return _scan_target_events(
            journal_bytes,
            frozenset(canonical_ids),
        )
    except archive_probe.ProbeBlocked as exc:
        raise ValueError(
            f"target invoice scan blocked: {exc.reason_code}"
        ) from None


def _analyze_snapshot(
    snapshot: archive_probe._ArchiveSnapshot,
    allowlist: frozenset[str],
) -> tuple[dict, list[str]]:
    if not snapshot.journal_bytes.strip():
        raise archive_probe.ProbeBlocked("journal_empty")
    try:
        target_scan = _scan_target_events(snapshot.journal_bytes, allowlist)
        members_text = snapshot.members_bytes.decode("latin-1", errors="replace")
        archive_probe._validate_catalog_resource_bounds(members_text)
        options, _addons = parse_gymassistant_billing_catalog_text(members_text)
    except archive_probe.ProbeBlocked:
        raise
    except Exception:
        raise archive_probe.ProbeBlocked("probe_internal_error")

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
        if option_counts[(option.membership_type_id, option.billing_option_code)]
        == 1
    }

    events = list(target_scan.events)
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
        "target_header_candidate_count": (
            target_scan.target_candidate_count
        ),
        "ambiguous_target_candidate_count": (
            target_scan.ambiguous_target_candidate_count
        ),
        "embedded_target_candidate_count": (
            target_scan.embedded_target_candidate_count
        ),
        "cross_boundary_target_candidate_count": (
            target_scan.cross_boundary_target_candidate_count
        ),
        "target_issue_count": target_scan.target_issue_count,
        "ignored_short_fragment_count": (
            target_scan.ignored_short_fragment_count
        ),
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
        "renewal_event_count": sum(event.event_type == 3 for event in events),
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
    if target_scan.target_issue_count:
        reasons.append("target_membership_parse_issue")
    if not options:
        reasons.append("catalog_empty")
    if duplicate_key_count:
        reasons.append("catalog_ambiguous")
    return analysis, reasons


def probe_target_invoice_readiness(
    backup_path: Path,
    *,
    expected_length: int,
    expected_sha256: str,
    allowlist_path: Path,
    expected_allowlist_count: int,
    expected_allowlist_set_sha256: str,
    expected_target_event_count: int,
    expected_short_fragment_count: int,
) -> dict:
    result = _empty_result()
    try:
        if (
            not archive_probe._lexically_local_absolute_windows_path(backup_path)
            or not archive_probe._lexically_local_absolute_windows_path(
                allowlist_path
            )
        ):
            raise archive_probe.ProbeBlocked("backup_path_invalid")
        if (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or expected_length < 1
            or expected_length > archive_probe.MAX_ARCHIVE_BYTES
        ):
            raise archive_probe.ProbeBlocked("invalid_arguments")
        expected_source_hash = str(expected_sha256 or "").strip().casefold()
        expected_allowlist_hash = str(
            expected_allowlist_set_sha256 or ""
        ).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_source_hash):
            raise archive_probe.ProbeBlocked("invalid_arguments")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_allowlist_hash):
            raise archive_probe.ProbeBlocked("invalid_arguments")
        if (
            not isinstance(expected_allowlist_count, int)
            or isinstance(expected_allowlist_count, bool)
            or expected_allowlist_count < 1
            or expected_allowlist_count > 100
        ):
            raise archive_probe.ProbeBlocked("invalid_arguments")
        if (
            not isinstance(expected_target_event_count, int)
            or isinstance(expected_target_event_count, bool)
            or expected_target_event_count < 1
            or expected_target_event_count
            > archive_probe.MAX_TARGET_MEMBERSHIP_EVENTS
            or not isinstance(expected_short_fragment_count, int)
            or isinstance(expected_short_fragment_count, bool)
            or expected_short_fragment_count < 0
            or expected_short_fragment_count > MAX_IGNORED_SHORT_FRAGMENTS
        ):
            raise archive_probe.ProbeBlocked("invalid_arguments")

        allowlist_read = archive_probe._read_stable_file_twice(
            Path(allowlist_path),
            maximum_bytes=archive_probe.MAX_ALLOWLIST_BYTES,
        )
        allowlist, allowlist_hash = archive_probe._canonical_allowlist(
            allowlist_read.data
        )
        if (
            len(allowlist) != expected_allowlist_count
            or allowlist_hash != expected_allowlist_hash
        ):
            raise archive_probe.ProbeBlocked("allowlist_binding_mismatch")
        result["allowlist"] = {
            "count": len(allowlist),
            "set_sha256": allowlist_hash,
        }

        stable = archive_probe._read_stable_file_twice(
            Path(backup_path),
            maximum_bytes=archive_probe.MAX_ARCHIVE_BYTES,
            expected_length=expected_length,
        )
        if stable.source_sha256 != expected_source_hash:
            raise archive_probe.ProbeBlocked("source_hash_mismatch")
        result["source"] = {
            "kind": archive_probe.SOURCE_KIND,
            "byte_length": stable.byte_length,
            "source_sha256": stable.source_sha256,
            "file_identity_sha256": stable.file_identity_sha256,
            "stable_read_count": stable.stable_read_count,
        }

        snapshot = archive_probe._inspect_archive(stable.data)
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
        analysis["expected_target_membership_event_count"] = (
            expected_target_event_count
        )
        analysis["expected_short_fragment_count"] = (
            expected_short_fragment_count
        )
        if (
            analysis["target_membership_event_count"]
            != expected_target_event_count
        ):
            reasons.append("target_event_count_mismatch")
        if (
            analysis["ignored_short_fragment_count"]
            != expected_short_fragment_count
        ):
            reasons.append("short_fragment_count_mismatch")
        if analysis["target_member_with_event_count"] != len(allowlist):
            reasons.append("target_member_coverage_incomplete")
        result["analysis"] = analysis
        result["reason_codes"] = sorted(set(reasons))
        target_scope_proven = (
            analysis["target_issue_count"] == 0
            and analysis["target_membership_event_count"]
            == expected_target_event_count
            and analysis["ignored_short_fragment_count"]
            == expected_short_fragment_count
            and analysis["target_member_with_event_count"] == len(allowlist)
        )
        result["target_scope_proven"] = target_scope_proven
        if target_scope_proven and not reasons:
            result["status"] = "passed"
            result["ready_for_target_sync"] = True
        return result
    except archive_probe.ProbeBlocked as exc:
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
    parser.add_argument("--expected-target-event-count", required=True, type=int)
    parser.add_argument(
        "--expected-short-fragment-count", required=True, type=int
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        result = probe_target_invoice_readiness(
            args.backup_path,
            expected_length=args.expected_length,
            expected_sha256=args.expected_sha256,
            allowlist_path=args.allowlist_path,
            expected_allowlist_count=args.expected_allowlist_count,
            expected_allowlist_set_sha256=(
                args.expected_allowlist_set_sha256
            ),
            expected_target_event_count=args.expected_target_event_count,
            expected_short_fragment_count=(
                args.expected_short_fragment_count
            ),
        )
    except archive_probe.ProbeBlocked as exc:
        result = _empty_result()
        result["reason_codes"] = [exc.reason_code]
    except Exception:
        result = _empty_result()
        result["reason_codes"] = ["probe_internal_error"]
    sys.stdout.write(
        json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n"
    )
    return 0 if result.get("ready_for_target_sync") is True else 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
