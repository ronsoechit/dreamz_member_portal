from __future__ import annotations

import sys

# Set this before importing any source-backed module.  The package runner also
# uses ``python -B``, but the module is independently file-write-free.
sys.dont_write_bytecode = True

import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Sequence

import ga_invoice_backup_probe as probe
from ga_journal import VOIDED_EVENT_MASK, parse_membership_journal_line


DIAGNOSTIC_SCHEMA = "dreamz.ga.invoice-backup-parse-diagnostic.v4"
CLASSIFICATION_KEYS = (
    "record_over_limit",
    "hidden_header_separator",
    "target_header_invalid",
    "target_payload_field_count",
    "target_payload_non_decimal",
    "target_period_invalid",
    "target_component_reconciliation_failed",
    "target_parser_unexpected_none",
    "ambiguous_member_zero",
    "ambiguous_member_missing_or_invalid",
    "ambiguous_header_invalid",
)
INVALID_SCOPE_KEYS = (
    "member_unproven_header_shape",
    "member_zero",
    "member_allowlisted",
    "member_positive_nonallowlisted",
    "member_missing",
    "member_nondecimal",
    "member_out_of_uint32",
)
INVALID_FAILURE_KEYS = (
    "pipe_missing",
    "payload_empty",
    "header_token_count_not_11",
    "timestamp_lexical_invalid",
    "timestamp_calendar_invalid",
    "sequence_nonhex",
    "sequence_uint32_overflow",
    "numeric_header_token_nondecimal",
    "numeric_header_uint32_overflow",
    "event_type_uint16_overflow",
    "ascii_edge_whitespace_only",
    "noncanonical_header_separator",
    "residual_unclassified",
)
INVALID_EVENT_SCOPE_KEYS = (
    "event_unproven_header_shape",
    "membership_1_or_3",
    "voided_membership_1_or_3",
    "nonmembership",
    "event_missing",
    "event_nondecimal",
    "event_negative",
    "event_uint16_overflow",
)
INVALID_TARGET_PAYLOAD_KEYS = (
    "payload_field_count_not_11",
    "payload_nondecimal",
    "period_invalid",
    "component_reconciliation_failed",
    "payload_structurally_valid",
)
STRICT_ZERO_EVENT_KEYS = (
    "membership_1_or_3",
    "voided_membership_1_or_3",
    "nonmembership",
)
FOCUS_FRAMING_KEYS = (
    "payload_empty",
    "pipe_missing",
)
CORE_HEADER_KEYS = (
    "strict_valid",
    "header_token_count_not_11",
    "timestamp_lexical_invalid",
    "timestamp_calendar_invalid",
    "sequence_nonhex",
    "sequence_uint32_overflow",
    "numeric_header_token_nondecimal",
    "numeric_header_uint32_overflow",
    "event_type_uint16_overflow",
    "ascii_edge_whitespace_only",
    "noncanonical_header_separator",
)
CORE_MEMBER_SCOPE_KEYS = (
    "zero_canonical",
    "zero_noncanonical",
    "allowlisted",
    "positive_nonallowlisted",
)
CORE_EVENT_SCOPE_KEYS = (
    "membership_1_or_3",
    "voided_membership_1_or_3",
    "nonmembership",
)
PIPE_MISSING_TOKEN_BUCKET_KEYS = (
    "fewer_than_6",
    "6_to_10",
    "exactly_11",
    "more_than_11",
)
PIPE_MISSING_PREFIX_KEYS = (
    "canonical_timestamp_prefix",
    "other_c_prefix",
    "non_journal_prefix",
)
SYSTEM_ZERO_FRAMING_KEYS = (
    "pipe_nonempty",
    "payload_empty",
    "pipe_missing_exact_core",
)
SYSTEM_ZERO_TOKEN_KEYS = (
    "canonical_zero",
    "noncanonical_zero",
)
SHORT_FRAGMENT_TOKEN_KEYS = (
    "zero_tokens",
    "one_token",
    "two_to_five_tokens",
)
SHORT_FRAGMENT_LENGTH_KEYS = (
    "1_to_15",
    "16_to_31",
    "32_to_63",
    "64_to_127",
    "128_or_more",
)
SHORT_FRAGMENT_BYTECLASS_KEYS = (
    "printable_ascii",
    "ascii_with_tab",
    "ascii_control",
    "nonascii_or_binary",
)
SHORT_FRAGMENT_CHARACTER_KEYS = (
    "alpha_only",
    "digit_only",
    "punctuation_or_control_only",
    "alphanumeric",
    "mixed",
)
SHORT_FRAGMENT_MARKER_KEYS = (
    "dos_eof_only",
    "bom_only",
    "nul_only",
    "unrecognized",
)
OTHER_FRAMING_KEYS = (
    "pipe_nonempty",
    "payload_empty",
    "pipe_missing",
)
SYSTEM_ZERO_PIPE_NONEMPTY_CAP = 20_000
SYSTEM_ZERO_HEADER_ONLY_CAP = 64
_CANONICAL_HEADER_ONLY_RE = re.compile(
    rb"c\d{8}!\d{4}[ \t]+[0-9A-Fa-f]+(?:[ \t]+[0-9]+){9}"
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - text is private
        raise probe.ProbeBlocked("invalid_arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise probe.ProbeBlocked("invalid_arguments")


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


def _strict_header_allowing_zero(line: bytes) -> tuple[int, int] | None:
    """Validate the production header grammar while permitting member zero."""
    match = probe._JOURNAL_HEADER_RE.fullmatch(line)
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
    if raw_event_type > 0xFFFF:
        return None
    return member_number, raw_event_type


def _loose_member_number(line: bytes) -> int | None:
    header = line.split(b"|", 1)[0]
    tokens = header.split()
    if len(tokens) != 11 or not tokens[5].isascii() or not tokens[5].isdigit():
        return None
    try:
        member_number = int(tokens[5], 10)
    except ValueError:
        return None
    if member_number < 0 or member_number > 0xFFFFFFFF:
        return None
    return member_number


def _bounded_ascii_uint(
    token: bytes,
    maximum: int,
    *,
    base: int,
) -> tuple[int | None, str]:
    if not token.isascii():
        return None, "lexical"
    pattern = rb"[0-9]+" if base == 10 else rb"[0-9A-Fa-f]+"
    if not re.fullmatch(pattern, token):
        return None, "lexical"
    significant = token.lstrip(b"0") or b"0"
    limit = (
        str(maximum).encode("ascii")
        if base == 10
        else format(maximum, "X").encode("ascii")
    )
    comparable = significant if base == 10 else significant.upper()
    if len(comparable) > len(limit) or (
        len(comparable) == len(limit) and comparable > limit
    ):
        return None, "overflow"
    return int(comparable, base), "ok"


def _invalid_member_scope(
    raw_line: bytes,
    allowlist: frozenset[str],
) -> str:
    header, separator, _payload = raw_line.partition(b"|")
    tokens = header.split()
    if not separator or len(tokens) != 11:
        return "member_unproven_header_shape"
    if len(tokens) <= 5:
        return "member_missing"
    token = tokens[5]
    value, state = _bounded_ascii_uint(token, 0xFFFFFFFF, base=10)
    if state == "lexical":
        return "member_nondecimal"
    if state == "overflow":
        return "member_out_of_uint32"
    assert value is not None
    if value == 0:
        return "member_zero"
    if str(value) in allowlist:
        return "member_allowlisted"
    return "member_positive_nonallowlisted"


def _invalid_header_first_failure(raw_line: bytes) -> str:
    header, separator, payload = raw_line.partition(b"|")
    if not separator:
        return "pipe_missing"
    if not payload:
        return "payload_empty"
    tokens = header.split()
    if len(tokens) != 11:
        return "header_token_count_not_11"
    if not re.fullmatch(rb"c\d{8}!\d{4}", tokens[0]):
        return "timestamp_lexical_invalid"
    try:
        datetime.strptime(tokens[0].decode("ascii"), "c%Y%m%d!%H%M")
    except (UnicodeDecodeError, ValueError):
        return "timestamp_calendar_invalid"
    sequence, sequence_state = _bounded_ascii_uint(
        tokens[1], 0xFFFFFFFF, base=16
    )
    if sequence_state == "lexical":
        return "sequence_nonhex"
    if sequence_state == "overflow":
        return "sequence_uint32_overflow"
    numeric_states = [
        _bounded_ascii_uint(token, 0xFFFFFFFF, base=10)[1]
        for token in tokens[2:]
    ]
    if "lexical" in numeric_states:
        return "numeric_header_token_nondecimal"
    if "overflow" in numeric_states:
        return "numeric_header_uint32_overflow"
    _event_value, event_state = _bounded_ascii_uint(tokens[6], 0xFFFF, base=10)
    if event_state == "overflow":
        return "event_type_uint16_overflow"
    if header != header.strip(b" \t"):
        if _CANONICAL_HEADER_ONLY_RE.fullmatch(header.strip(b" \t")):
            return "ascii_edge_whitespace_only"
        return "noncanonical_header_separator"
    if not _CANONICAL_HEADER_ONLY_RE.fullmatch(header):
        return "noncanonical_header_separator"
    return "residual_unclassified"


def _invalid_event_scope(raw_line: bytes) -> str:
    header, separator, _payload = raw_line.partition(b"|")
    tokens = header.split()
    if not separator or len(tokens) != 11:
        return "event_unproven_header_shape"
    if len(tokens) <= 6:
        return "event_missing"
    token = tokens[6]
    if token.startswith(b"-") and token[1:].isdigit():
        return "event_negative"
    raw_event_type, state = _bounded_ascii_uint(token, 0xFFFF, base=10)
    if state == "lexical":
        return "event_nondecimal"
    if state == "overflow":
        return "event_uint16_overflow"
    assert raw_event_type is not None
    event_type = raw_event_type & ~VOIDED_EVENT_MASK
    if event_type in {1, 3}:
        return (
            "voided_membership_1_or_3"
            if raw_event_type & VOIDED_EVENT_MASK
            else "membership_1_or_3"
        )
    return "nonmembership"


def _classify_core_header(
    header: bytes,
) -> tuple[str, int | None, int | None, bytes | None]:
    """Validate the complete header core without trusting framing or payload."""
    tokens = header.split()
    if len(tokens) != 11:
        return "header_token_count_not_11", None, None, None
    if not re.fullmatch(rb"c\d{8}!\d{4}", tokens[0]):
        return "timestamp_lexical_invalid", None, None, None
    try:
        datetime.strptime(tokens[0].decode("ascii"), "c%Y%m%d!%H%M")
    except (UnicodeDecodeError, ValueError):
        return "timestamp_calendar_invalid", None, None, None
    _sequence, sequence_state = _bounded_ascii_uint(
        tokens[1], 0xFFFFFFFF, base=16
    )
    if sequence_state == "lexical":
        return "sequence_nonhex", None, None, None
    if sequence_state == "overflow":
        return "sequence_uint32_overflow", None, None, None
    numeric_values: list[int] = []
    for token in tokens[2:]:
        value, state = _bounded_ascii_uint(token, 0xFFFFFFFF, base=10)
        if state == "lexical":
            return "numeric_header_token_nondecimal", None, None, None
        if state == "overflow":
            return "numeric_header_uint32_overflow", None, None, None
        assert value is not None
        numeric_values.append(value)
    raw_event_type = numeric_values[4]
    if raw_event_type > 0xFFFF:
        return "event_type_uint16_overflow", None, None, None
    if header != header.strip(b" \t"):
        if _CANONICAL_HEADER_ONLY_RE.fullmatch(header.strip(b" \t")):
            return "ascii_edge_whitespace_only", None, None, None
        return "noncanonical_header_separator", None, None, None
    if not _CANONICAL_HEADER_ONLY_RE.fullmatch(header):
        return "noncanonical_header_separator", None, None, None
    return "strict_valid", numeric_values[3], raw_event_type, tokens[5]


def _strict_core_member_scope(
    member_number: int,
    raw_member_token: bytes,
    allowlist: frozenset[str],
) -> str:
    if member_number == 0:
        return "zero_canonical" if raw_member_token == b"0" else "zero_noncanonical"
    if str(member_number) in allowlist:
        return "allowlisted"
    return "positive_nonallowlisted"


def _strict_core_event_scope(raw_event_type: int) -> str:
    event_type = raw_event_type & ~VOIDED_EVENT_MASK
    if event_type in {1, 3}:
        return (
            "voided_membership_1_or_3"
            if raw_event_type & VOIDED_EVENT_MASK
            else "membership_1_or_3"
        )
    return "nonmembership"


def _pipe_missing_token_bucket(header: bytes) -> str:
    count = len(header.split())
    if count < 6:
        return "fewer_than_6"
    if count < 11:
        return "6_to_10"
    if count == 11:
        return "exactly_11"
    return "more_than_11"


def _pipe_missing_prefix_scope(header: bytes) -> str:
    if re.match(rb"^c\d{8}!\d{4}(?:[ \t]|$)", header):
        return "canonical_timestamp_prefix"
    if header.startswith(b"c"):
        return "other_c_prefix"
    return "non_journal_prefix"


def _system_zero_token_scope(raw_member_token: bytes) -> str:
    return (
        "canonical_zero"
        if raw_member_token == b"0"
        else "noncanonical_zero"
    )


def _short_fragment_token_scope(raw_line: bytes) -> str:
    token_count = len(raw_line.split())
    if token_count == 0:
        return "zero_tokens"
    if token_count == 1:
        return "one_token"
    return "two_to_five_tokens"


def _short_fragment_length_scope(raw_line: bytes) -> str:
    length = len(raw_line)
    if length <= 15:
        return "1_to_15"
    if length <= 31:
        return "16_to_31"
    if length <= 63:
        return "32_to_63"
    if length <= 127:
        return "64_to_127"
    return "128_or_more"


def _short_fragment_byteclass(raw_line: bytes) -> str:
    if all(0x20 <= value <= 0x7E for value in raw_line):
        return "printable_ascii"
    if all(value == 0x09 or 0x20 <= value <= 0x7E for value in raw_line):
        return "ascii_with_tab"
    if all(value < 0x80 for value in raw_line):
        return "ascii_control"
    return "nonascii_or_binary"


def _short_fragment_character_scope(raw_line: bytes) -> str:
    ascii_letters = sum(
        (0x41 <= value <= 0x5A) or (0x61 <= value <= 0x7A)
        for value in raw_line
    )
    ascii_digits = sum(0x30 <= value <= 0x39 for value in raw_line)
    other = len(raw_line) - ascii_letters - ascii_digits
    if ascii_letters and not ascii_digits and not other:
        return "alpha_only"
    if ascii_digits and not ascii_letters and not other:
        return "digit_only"
    if other and not ascii_letters and not ascii_digits:
        return "punctuation_or_control_only"
    if ascii_letters and ascii_digits and not other:
        return "alphanumeric"
    return "mixed"


def _short_fragment_marker_scope(raw_line: bytes) -> str:
    # These are diagnostic labels only.  None authorizes a production skip.
    if raw_line == b"\x1a":
        return "dos_eof_only"
    if raw_line in {b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"}:
        return "bom_only"
    if raw_line and not raw_line.strip(b"\x00"):
        return "nul_only"
    return "unrecognized"


def _other_framing_scope(raw_line: bytes) -> str:
    _header, separator, payload = raw_line.partition(b"|")
    if not separator:
        return "pipe_missing"
    return "pipe_nonempty" if payload else "payload_empty"


def _invalid_target_payload_structure(raw_line: bytes) -> str:
    _header, separator, payload = raw_line.partition(b"|")
    if not separator:
        return "payload_field_count_not_11"
    parts = payload.split()
    if len(parts) != 11:
        return "payload_field_count_not_11"
    try:
        values = [int(value) for value in parts]
    except ValueError:
        return "payload_nondecimal"
    try:
        datetime.strptime(str(values[2]), "%Y%m%d")
        datetime.strptime(str(values[3]), "%Y%m%d")
    except ValueError:
        return "period_invalid"
    if values[7] != values[4] + values[5] + values[6] + values[10]:
        return "component_reconciliation_failed"
    return "payload_structurally_valid"


def _has_hidden_header(raw_line: bytes) -> bool:
    # Keep diagnostic framing identical to the production preflight and the
    # shared Latin-1 ``str.splitlines`` journal parser.
    return probe._contains_non_crlf_line_separator(raw_line)


def _classify_target_payload(raw_line: bytes, member_id: str) -> str | None:
    decoded = raw_line.decode("latin-1", errors="strict")
    normalized = decoded.strip()
    if "|" not in normalized:
        return "target_parser_unexpected_none"
    _header, payload = normalized.split("|", 1)
    payload_parts = payload.split()
    if len(payload_parts) != 11:
        return "target_payload_field_count"
    try:
        values = [int(value) for value in payload_parts]
    except ValueError:
        return "target_payload_non_decimal"
    try:
        datetime.strptime(str(values[2]), "%Y%m%d")
        datetime.strptime(str(values[3]), "%Y%m%d")
    except ValueError:
        return "target_period_invalid"
    if values[7] != values[4] + values[5] + values[6] + values[10]:
        return "target_component_reconciliation_failed"
    try:
        event = parse_membership_journal_line(decoded)
    except (TypeError, UnicodeDecodeError, ValueError):
        return "target_parser_unexpected_none"
    if event is None or event.member_id != member_id:
        return "target_parser_unexpected_none"
    return None


def diagnose_journal_structure(
    journal_bytes: bytes,
    allowlist: frozenset[str],
) -> dict:
    """Return only bounded aggregate structural facts; never retain source rows."""
    classifications: Counter[str] = Counter({key: 0 for key in CLASSIFICATION_KEYS})
    confirmed_target_header_count = 0
    target_membership_candidate_count = 0
    target_parse_success_count = 0
    target_parse_failure_count = 0
    ambiguous_scope_record_count = 0
    invalid_header_record_count = 0
    target_invalid_header_record_count = 0
    ambiguous_invalid_header_record_count = 0
    invalid_scope_counts: Counter[str] = Counter(
        {key: 0 for key in INVALID_SCOPE_KEYS}
    )
    invalid_failure_counts = {
        scope: Counter({key: 0 for key in INVALID_FAILURE_KEYS})
        for scope in INVALID_SCOPE_KEYS
    }
    invalid_event_scope_counts = {
        scope: Counter({key: 0 for key in INVALID_EVENT_SCOPE_KEYS})
        for scope in INVALID_SCOPE_KEYS
    }
    invalid_allowlisted_membership_payload_counts: Counter[str] = Counter(
        {key: 0 for key in INVALID_TARGET_PAYLOAD_KEYS}
    )
    strict_zero_event_counts: Counter[str] = Counter(
        {key: 0 for key in STRICT_ZERO_EVENT_KEYS}
    )
    strict_zero_record_count = 0
    focus_framing_record_counts: Counter[str] = Counter(
        {key: 0 for key in FOCUS_FRAMING_KEYS}
    )
    focus_core_counts = {
        framing: Counter({key: 0 for key in CORE_HEADER_KEYS})
        for framing in FOCUS_FRAMING_KEYS
    }
    focus_strict_scope_counts = {
        framing: {
            member_scope: Counter({key: 0 for key in CORE_EVENT_SCOPE_KEYS})
            for member_scope in CORE_MEMBER_SCOPE_KEYS
        }
        for framing in FOCUS_FRAMING_KEYS
    }
    pipe_missing_token_bucket_counts: Counter[str] = Counter(
        {key: 0 for key in PIPE_MISSING_TOKEN_BUCKET_KEYS}
    )
    pipe_missing_prefix_counts: Counter[str] = Counter(
        {key: 0 for key in PIPE_MISSING_PREFIX_KEYS}
    )
    system_zero_scope_counts = {
        framing: {
            token_scope: Counter({key: 0 for key in CORE_EVENT_SCOPE_KEYS})
            for token_scope in SYSTEM_ZERO_TOKEN_KEYS
        }
        for framing in SYSTEM_ZERO_FRAMING_KEYS
    }
    short_fragment_token_counts: Counter[str] = Counter(
        {key: 0 for key in SHORT_FRAGMENT_TOKEN_KEYS}
    )
    short_fragment_length_counts: Counter[str] = Counter(
        {key: 0 for key in SHORT_FRAGMENT_LENGTH_KEYS}
    )
    short_fragment_byteclass_counts: Counter[str] = Counter(
        {key: 0 for key in SHORT_FRAGMENT_BYTECLASS_KEYS}
    )
    short_fragment_character_counts: Counter[str] = Counter(
        {key: 0 for key in SHORT_FRAGMENT_CHARACTER_KEYS}
    )
    short_fragment_marker_counts: Counter[str] = Counter(
        {key: 0 for key in SHORT_FRAGMENT_MARKER_KEYS}
    )
    short_fragment_header_prefix_present_count = 0
    non_crlf_separator_record_count = 0
    short_fragment_count = 0
    other_failure_counts: Counter[str] = Counter(
        {key: 0 for key in INVALID_FAILURE_KEYS}
    )
    other_core_counts: Counter[str] = Counter(
        {key: 0 for key in CORE_HEADER_KEYS}
    )
    other_member_scope_counts: Counter[str] = Counter(
        {key: 0 for key in INVALID_SCOPE_KEYS}
    )
    other_event_scope_counts: Counter[str] = Counter(
        {key: 0 for key in INVALID_EVENT_SCOPE_KEYS}
    )
    other_framing_counts: Counter[str] = Counter(
        {key: 0 for key in OTHER_FRAMING_KEYS}
    )
    other_joint_counts: Counter[tuple[str, str, str, str, str]] = Counter()
    strict_header_rejected_record_count = 0
    focus_other_rejected_header_count = 0

    for _record_number, raw_line in probe._iter_cr_lf_lines(journal_bytes):
        if len(raw_line) > probe.MAX_JOURNAL_RECORD_BYTES:
            classifications["record_over_limit"] += 1
            ambiguous_scope_record_count += 1
            continue
        if not raw_line.strip(b" \t"):
            continue
        if _has_hidden_header(raw_line):
            classifications["hidden_header_separator"] += 1
            ambiguous_scope_record_count += 1
            non_crlf_separator_record_count += 1
            continue

        strict_header = _strict_header_allowing_zero(raw_line)
        if strict_header is None:
            strict_header_rejected_record_count += 1
            global_failure = _invalid_header_first_failure(raw_line)
            if global_failure in FOCUS_FRAMING_KEYS:
                framing = global_failure
                focus_framing_record_counts[framing] += 1
                header = raw_line.partition(b"|")[0]
                (
                    core_status,
                    core_member_number,
                    core_raw_event_type,
                    core_member_token,
                ) = _classify_core_header(header)
                focus_core_counts[framing][core_status] += 1
                if core_status == "strict_valid":
                    assert core_member_number is not None
                    assert core_raw_event_type is not None
                    assert core_member_token is not None
                    core_member_scope = _strict_core_member_scope(
                        core_member_number,
                        core_member_token,
                        allowlist,
                    )
                    core_event_scope = _strict_core_event_scope(
                        core_raw_event_type
                    )
                    focus_strict_scope_counts[framing][core_member_scope][
                        core_event_scope
                    ] += 1
                    if core_member_number == 0:
                        zero_framing = (
                            "payload_empty"
                            if framing == "payload_empty"
                            else "pipe_missing_exact_core"
                        )
                        zero_token_scope = _system_zero_token_scope(
                            core_member_token
                        )
                        system_zero_scope_counts[zero_framing][
                            zero_token_scope
                        ][core_event_scope] += 1
                if framing == "pipe_missing":
                    pipe_missing_token_bucket_counts[
                        _pipe_missing_token_bucket(header)
                    ] += 1
                    pipe_missing_prefix_counts[
                        _pipe_missing_prefix_scope(header)
                    ] += 1
                    if (
                        core_status == "header_token_count_not_11"
                        and _pipe_missing_token_bucket(header) == "fewer_than_6"
                        and _pipe_missing_prefix_scope(header)
                        == "non_journal_prefix"
                    ):
                        short_fragment_count += 1
                        short_fragment_token_counts[
                            _short_fragment_token_scope(raw_line)
                        ] += 1
                        short_fragment_length_counts[
                            _short_fragment_length_scope(raw_line)
                        ] += 1
                        short_fragment_byteclass_counts[
                            _short_fragment_byteclass(raw_line)
                        ] += 1
                        short_fragment_character_counts[
                            _short_fragment_character_scope(raw_line)
                        ] += 1
                        short_fragment_marker_counts[
                            _short_fragment_marker_scope(raw_line)
                        ] += 1
                        if re.search(rb"c\d{8}!\d{4}", raw_line):
                            short_fragment_header_prefix_present_count += 1
            else:
                focus_other_rejected_header_count += 1
                header = raw_line.partition(b"|")[0]
                other_core_status, _member, _event, _token = (
                    _classify_core_header(header)
                )
                other_member_scope = _invalid_member_scope(raw_line, allowlist)
                other_event_scope = _invalid_event_scope(raw_line)
                other_framing_scope = _other_framing_scope(raw_line)
                other_failure_counts[global_failure] += 1
                other_core_counts[other_core_status] += 1
                other_member_scope_counts[other_member_scope] += 1
                other_event_scope_counts[other_event_scope] += 1
                other_framing_counts[other_framing_scope] += 1
                other_joint_counts[
                    (
                        global_failure,
                        other_core_status,
                        other_member_scope,
                        other_event_scope,
                        other_framing_scope,
                    )
                ] += 1
            loose_member = _loose_member_number(raw_line)
            include_invalid_detail = False
            if loose_member is not None and loose_member > 0:
                if str(loose_member) in allowlist:
                    classifications["target_header_invalid"] += 1
                    target_parse_failure_count += 1
                    target_invalid_header_record_count += 1
                    include_invalid_detail = True
                else:
                    continue
            elif loose_member is None:
                header_tokens = raw_line.split(b"|", 1)[0].split()
                key = (
                    "ambiguous_member_missing_or_invalid"
                    if len(header_tokens) == 11
                    else "ambiguous_header_invalid"
                )
                classifications[key] += 1
                ambiguous_scope_record_count += 1
                ambiguous_invalid_header_record_count += 1
                include_invalid_detail = True
            else:
                # A zero member on an otherwise invalid row is not enough to
                # prove a legitimate global/system record.
                key = "ambiguous_header_invalid"
                classifications[key] += 1
                ambiguous_scope_record_count += 1
                ambiguous_invalid_header_record_count += 1
                include_invalid_detail = True

            if include_invalid_detail:
                invalid_header_record_count += 1
                invalid_scope = _invalid_member_scope(raw_line, allowlist)
                invalid_failure = _invalid_header_first_failure(raw_line)
                invalid_event_scope = _invalid_event_scope(raw_line)
                invalid_scope_counts[invalid_scope] += 1
                invalid_failure_counts[invalid_scope][invalid_failure] += 1
                invalid_event_scope_counts[invalid_scope][invalid_event_scope] += 1
                if (
                    invalid_scope == "member_allowlisted"
                    and invalid_event_scope
                    in {"membership_1_or_3", "voided_membership_1_or_3"}
                ):
                    payload_scope = _invalid_target_payload_structure(raw_line)
                    invalid_allowlisted_membership_payload_counts[payload_scope] += 1
            continue

        member_number, raw_event_type = strict_header
        if member_number == 0:
            classifications["ambiguous_member_zero"] += 1
            ambiguous_scope_record_count += 1
            strict_zero_record_count += 1
            zero_event_scope = _invalid_event_scope(raw_line)
            if zero_event_scope not in STRICT_ZERO_EVENT_KEYS:
                raise ValueError("strict zero header produced invalid event scope")
            strict_zero_event_counts[zero_event_scope] += 1
            core_status, _member, _event, raw_member_token = (
                _classify_core_header(raw_line.partition(b"|")[0])
            )
            if core_status != "strict_valid" or raw_member_token is None:
                raise ValueError("strict zero row did not retain strict core")
            zero_token_scope = _system_zero_token_scope(raw_member_token)
            system_zero_scope_counts["pipe_nonempty"][zero_token_scope][
                zero_event_scope
            ] += 1
            continue
        member_id = str(member_number)
        if member_id not in allowlist:
            continue
        confirmed_target_header_count += 1
        event_type = raw_event_type & ~VOIDED_EVENT_MASK
        if event_type not in {1, 3}:
            continue
        target_membership_candidate_count += 1
        failure = _classify_target_payload(raw_line, member_id)
        if failure is None:
            target_parse_success_count += 1
        else:
            classifications[failure] += 1
            target_parse_failure_count += 1

    invalid_scope_total = sum(invalid_scope_counts.values())
    invalid_failure_total = sum(
        sum(counts.values()) for counts in invalid_failure_counts.values()
    )
    invalid_event_total = sum(
        sum(counts.values()) for counts in invalid_event_scope_counts.values()
    )
    residual_unclassified_count = sum(
        counts["residual_unclassified"] for counts in invalid_failure_counts.values()
    )
    invalid_origin_total = (
        target_invalid_header_record_count + ambiguous_invalid_header_record_count
    )
    invalid_allowlisted_membership_event_count = (
        invalid_event_scope_counts["member_allowlisted"]["membership_1_or_3"]
        + invalid_event_scope_counts["member_allowlisted"][
            "voided_membership_1_or_3"
        ]
    )
    invalid_allowlisted_membership_payload_total = sum(
        invalid_allowlisted_membership_payload_counts.values()
    )
    classified_invalid_header_count = (
        invalid_header_record_count - residual_unclassified_count
    )
    invalid_scope_counts_output = {
        key: int(invalid_scope_counts[key]) for key in INVALID_SCOPE_KEYS
    }
    invalid_failure_counts_output = {
        scope: {
            key: int(invalid_failure_counts[scope][key])
            for key in INVALID_FAILURE_KEYS
        }
        for scope in INVALID_SCOPE_KEYS
    }
    invalid_event_scope_counts_output = {
        scope: {
            key: int(invalid_event_scope_counts[scope][key])
            for key in INVALID_EVENT_SCOPE_KEYS
        }
        for scope in INVALID_SCOPE_KEYS
    }
    strict_zero_event_counts_output = {
        key: int(strict_zero_event_counts[key]) for key in STRICT_ZERO_EVENT_KEYS
    }
    focus_framing_record_counts_output = {
        key: int(focus_framing_record_counts[key]) for key in FOCUS_FRAMING_KEYS
    }
    focus_core_counts_output = {
        framing: {
            key: int(focus_core_counts[framing][key]) for key in CORE_HEADER_KEYS
        }
        for framing in FOCUS_FRAMING_KEYS
    }
    focus_strict_scope_counts_output = {
        framing: {
            member_scope: {
                event_scope: int(
                    focus_strict_scope_counts[framing][member_scope][event_scope]
                )
                for event_scope in CORE_EVENT_SCOPE_KEYS
            }
            for member_scope in CORE_MEMBER_SCOPE_KEYS
        }
        for framing in FOCUS_FRAMING_KEYS
    }
    focus_core_counts_match_framing = {
        framing: (
            sum(focus_core_counts[framing].values())
            == focus_framing_record_counts[framing]
        )
        for framing in FOCUS_FRAMING_KEYS
    }
    focus_strict_scope_counts_match_core = {
        framing: (
            sum(
                sum(event_counts.values())
                for event_counts in focus_strict_scope_counts[framing].values()
            )
            == focus_core_counts[framing]["strict_valid"]
        )
        for framing in FOCUS_FRAMING_KEYS
    }
    pipe_missing_token_bucket_counts_output = {
        key: int(pipe_missing_token_bucket_counts[key])
        for key in PIPE_MISSING_TOKEN_BUCKET_KEYS
    }
    pipe_missing_prefix_counts_output = {
        key: int(pipe_missing_prefix_counts[key])
        for key in PIPE_MISSING_PREFIX_KEYS
    }
    focus_framing_total = sum(focus_framing_record_counts.values())
    focus_accounting_complete = (
        focus_framing_total + focus_other_rejected_header_count
        == strict_header_rejected_record_count
        and all(focus_core_counts_match_framing.values())
        and all(focus_strict_scope_counts_match_core.values())
        and sum(pipe_missing_token_bucket_counts.values())
        == focus_framing_record_counts["pipe_missing"]
        and sum(pipe_missing_prefix_counts.values())
        == focus_framing_record_counts["pipe_missing"]
    )
    system_zero_scope_counts_output = {
        framing: {
            token_scope: {
                event_scope: int(
                    system_zero_scope_counts[framing][token_scope][event_scope]
                )
                for event_scope in CORE_EVENT_SCOPE_KEYS
            }
            for token_scope in SYSTEM_ZERO_TOKEN_KEYS
        }
        for framing in SYSTEM_ZERO_FRAMING_KEYS
    }
    system_zero_matrix_total = sum(
        sum(sum(event_counts.values()) for event_counts in token_counts.values())
        for token_counts in system_zero_scope_counts.values()
    )
    focused_zero_total = sum(
        sum(
            focus_strict_scope_counts[framing][member_scope].values()
        )
        for framing in FOCUS_FRAMING_KEYS
        for member_scope in ("zero_canonical", "zero_noncanonical")
    )
    system_zero_observed_total = strict_zero_record_count + focused_zero_total
    proposed_system_skip_pipe_nonempty = system_zero_scope_counts[
        "pipe_nonempty"
    ]["canonical_zero"]["nonmembership"]
    proposed_system_skip_header_only = system_zero_scope_counts[
        "pipe_missing_exact_core"
    ]["canonical_zero"]["nonmembership"]
    proposed_system_skip_total = (
        proposed_system_skip_pipe_nonempty
        + proposed_system_skip_header_only
    )
    forbidden_system_zero_total = (
        system_zero_matrix_total - proposed_system_skip_total
    )
    system_zero_policy_caps_ok = (
        proposed_system_skip_pipe_nonempty <= SYSTEM_ZERO_PIPE_NONEMPTY_CAP
        and proposed_system_skip_header_only <= SYSTEM_ZERO_HEADER_ONLY_CAP
    )
    short_fragment_token_counts_output = {
        key: int(short_fragment_token_counts[key])
        for key in SHORT_FRAGMENT_TOKEN_KEYS
    }
    short_fragment_length_counts_output = {
        key: int(short_fragment_length_counts[key])
        for key in SHORT_FRAGMENT_LENGTH_KEYS
    }
    short_fragment_byteclass_counts_output = {
        key: int(short_fragment_byteclass_counts[key])
        for key in SHORT_FRAGMENT_BYTECLASS_KEYS
    }
    short_fragment_character_counts_output = {
        key: int(short_fragment_character_counts[key])
        for key in SHORT_FRAGMENT_CHARACTER_KEYS
    }
    short_fragment_marker_counts_output = {
        key: int(short_fragment_marker_counts[key])
        for key in SHORT_FRAGMENT_MARKER_KEYS
    }
    short_fragment_axes_reconcile = all(
        sum(counts.values()) == short_fragment_count
        for counts in (
            short_fragment_token_counts,
            short_fragment_length_counts,
            short_fragment_byteclass_counts,
            short_fragment_character_counts,
            short_fragment_marker_counts,
        )
    )
    other_failure_counts_output = {
        key: int(other_failure_counts[key]) for key in INVALID_FAILURE_KEYS
    }
    other_core_counts_output = {
        key: int(other_core_counts[key]) for key in CORE_HEADER_KEYS
    }
    other_member_scope_counts_output = {
        key: int(other_member_scope_counts[key]) for key in INVALID_SCOPE_KEYS
    }
    other_event_scope_counts_output = {
        key: int(other_event_scope_counts[key])
        for key in INVALID_EVENT_SCOPE_KEYS
    }
    other_framing_counts_output = {
        key: int(other_framing_counts[key]) for key in OTHER_FRAMING_KEYS
    }
    other_joint_counts_output = [
        {
            "failure": failure,
            "core": core,
            "member_scope": member_scope,
            "event_scope": event_scope,
            "framing": framing,
            "count": int(count),
        }
        for (
            failure,
            core,
            member_scope,
            event_scope,
            framing,
        ), count in sorted(other_joint_counts.items())
    ]
    other_joint_total = sum(other_joint_counts.values())
    joint_failure_counts: Counter[str] = Counter()
    joint_core_counts: Counter[str] = Counter()
    joint_member_counts: Counter[str] = Counter()
    joint_event_counts: Counter[str] = Counter()
    joint_framing_counts: Counter[str] = Counter()
    for joint_key, count in other_joint_counts.items():
        failure, core, member_scope, event_scope, framing = joint_key
        joint_failure_counts[failure] += count
        joint_core_counts[core] += count
        joint_member_counts[member_scope] += count
        joint_event_counts[event_scope] += count
        joint_framing_counts[framing] += count
    other_joint_marginals_match = (
        all(
            joint_failure_counts[key] == other_failure_counts[key]
            for key in INVALID_FAILURE_KEYS
        )
        and all(
            joint_core_counts[key] == other_core_counts[key]
            for key in CORE_HEADER_KEYS
        )
        and all(
            joint_member_counts[key] == other_member_scope_counts[key]
            for key in INVALID_SCOPE_KEYS
        )
        and all(
            joint_event_counts[key] == other_event_scope_counts[key]
            for key in INVALID_EVENT_SCOPE_KEYS
        )
        and all(
            joint_framing_counts[key] == other_framing_counts[key]
            for key in OTHER_FRAMING_KEYS
        )
    )
    other_axes_reconcile = all(
        sum(counts.values()) == focus_other_rejected_header_count
        for counts in (
            other_failure_counts,
            other_core_counts,
            other_member_scope_counts,
            other_event_scope_counts,
            other_framing_counts,
        )
    ) and (
        other_joint_total == focus_other_rejected_header_count
        and other_joint_marginals_match
    )
    focus_non_strict_core_total = sum(
        count
        for framing_counts in focus_core_counts.values()
        for key, count in framing_counts.items()
        if key != "strict_valid"
    )
    focus_non_short_remainder_count = (
        focus_non_strict_core_total - short_fragment_count
    )
    remainder_record_count = (
        short_fragment_count
        + focus_non_short_remainder_count
        + focus_other_rejected_header_count
    )
    remainder_accounting_complete = (
        focus_non_short_remainder_count >= 0
        and remainder_record_count
        == focus_non_strict_core_total + focus_other_rejected_header_count
        and short_fragment_axes_reconcile
        and other_axes_reconcile
    )
    existing_empty_allowlisted_nonmembership_count = (
        focus_strict_scope_counts["payload_empty"]["allowlisted"][
            "nonmembership"
        ]
    )
    existing_empty_nonallowlisted_nonmembership_count = (
        focus_strict_scope_counts["payload_empty"][
            "positive_nonallowlisted"
        ]["nonmembership"]
    )
    existing_empty_nonmembership_exception_count = (
        existing_empty_allowlisted_nonmembership_count
        + existing_empty_nonallowlisted_nonmembership_count
    )
    zero_policy_prerequisites_ok = (
        system_zero_matrix_total == system_zero_observed_total
        and system_zero_policy_caps_ok
    )
    effective_system_zero_skip_count = (
        proposed_system_skip_total if zero_policy_prerequisites_ok else 0
    )
    empty_exception_prerequisites_ok = focus_accounting_complete
    effective_empty_allowlisted_exception_count = (
        existing_empty_allowlisted_nonmembership_count
        if empty_exception_prerequisites_ok
        else 0
    )
    simulated_target_issue_count = (
        target_parse_failure_count
        - effective_empty_allowlisted_exception_count
    )
    simulated_ambiguous_issue_count = (
        ambiguous_scope_record_count - effective_system_zero_skip_count
    )
    simulated_total_unresolved_issue_count = (
        simulated_target_issue_count + simulated_ambiguous_issue_count
    )
    policy_simulation_accounting_complete = (
        zero_policy_prerequisites_ok
        and empty_exception_prerequisites_ok
        and effective_empty_allowlisted_exception_count
        <= target_parse_failure_count
        and effective_system_zero_skip_count <= ambiguous_scope_record_count
        and simulated_target_issue_count >= 0
        and simulated_ambiguous_issue_count >= 0
        and remainder_accounting_complete
    )
    post_policy_simulation = {
        "existing_empty_nonmembership_exception_count": (
            existing_empty_nonmembership_exception_count
        ),
        "existing_empty_allowlisted_nonmembership_exception_count": (
            existing_empty_allowlisted_nonmembership_count
        ),
        "existing_empty_nonallowlisted_nonmembership_exception_count": (
            existing_empty_nonallowlisted_nonmembership_count
        ),
        "proposed_system_zero_skip_count": proposed_system_skip_total,
        "effective_system_zero_skip_count": effective_system_zero_skip_count,
        "zero_policy_prerequisites_ok": zero_policy_prerequisites_ok,
        "effective_empty_allowlisted_nonmembership_exception_count": (
            effective_empty_allowlisted_exception_count
        ),
        "empty_exception_prerequisites_ok": (
            empty_exception_prerequisites_ok
        ),
        "target_membership_event_count": target_parse_success_count,
        "target_issue_count": simulated_target_issue_count,
        "ambiguous_issue_count": simulated_ambiguous_issue_count,
        "total_unresolved_issue_count": simulated_total_unresolved_issue_count,
        "short_fragment_blocker_count": short_fragment_count,
        "non_crlf_separator_blocker_count": non_crlf_separator_record_count,
        "target_scope_clear": (
            policy_simulation_accounting_complete
            and simulated_target_issue_count == 0
        ),
        "global_scope_clear": (
            policy_simulation_accounting_complete
            and simulated_total_unresolved_issue_count == 0
        ),
        "accounting_complete": policy_simulation_accounting_complete,
        "authorizes_invoice_processing": False,
    }
    safe_summary = {
        "confirmed_target_header_count": confirmed_target_header_count,
        "target_membership_candidate_count": target_membership_candidate_count,
        "target_parse_success_count": target_parse_success_count,
        "target_parse_failure_count": target_parse_failure_count,
        "ambiguous_scope_record_count": ambiguous_scope_record_count,
        "classification_counts": {
            key: int(classifications[key]) for key in CLASSIFICATION_KEYS
        },
        "strict_zero_record_count": strict_zero_record_count,
        "strict_zero_event_scope_counts": strict_zero_event_counts_output,
        "strict_zero_counts_sum_matches_total": (
            sum(strict_zero_event_counts.values()) == strict_zero_record_count
        ),
        "system_zero_scope_counts": system_zero_scope_counts_output,
        "system_zero_matrix_total": system_zero_matrix_total,
        "system_zero_observed_total": system_zero_observed_total,
        "system_zero_matrix_matches_observed_total": (
            system_zero_matrix_total == system_zero_observed_total
        ),
        "proposed_system_skip_pipe_nonempty_count": (
            proposed_system_skip_pipe_nonempty
        ),
        "proposed_system_skip_header_only_count": (
            proposed_system_skip_header_only
        ),
        "proposed_system_skip_total": proposed_system_skip_total,
        "forbidden_system_zero_total": forbidden_system_zero_total,
        "system_zero_policy_caps_ok": system_zero_policy_caps_ok,
        "invalid_header_record_count": invalid_header_record_count,
        "target_invalid_header_record_count": target_invalid_header_record_count,
        "ambiguous_invalid_header_record_count": (
            ambiguous_invalid_header_record_count
        ),
        "classified_invalid_header_count": classified_invalid_header_count,
        "residual_unclassified_count": residual_unclassified_count,
        "invalid_header_scope_counts": invalid_scope_counts_output,
        "invalid_header_failure_counts": invalid_failure_counts_output,
        "invalid_header_event_scope_counts": invalid_event_scope_counts_output,
        "invalid_allowlisted_membership_payload_counts": {
            key: int(invalid_allowlisted_membership_payload_counts[key])
            for key in INVALID_TARGET_PAYLOAD_KEYS
        },
        "focus_framing_record_counts": focus_framing_record_counts_output,
        "strict_header_rejected_record_count": (
            strict_header_rejected_record_count
        ),
        "focus_other_rejected_header_count": focus_other_rejected_header_count,
        "focus_core_counts": focus_core_counts_output,
        "focus_strict_core_scope_counts": focus_strict_scope_counts_output,
        "focus_core_counts_match_framing": focus_core_counts_match_framing,
        "focus_strict_scope_counts_match_core": (
            focus_strict_scope_counts_match_core
        ),
        "pipe_missing_token_bucket_counts": (
            pipe_missing_token_bucket_counts_output
        ),
        "pipe_missing_prefix_counts": pipe_missing_prefix_counts_output,
        "pipe_missing_token_counts_match_total": (
            sum(pipe_missing_token_bucket_counts.values())
            == focus_framing_record_counts["pipe_missing"]
        ),
        "pipe_missing_prefix_counts_match_total": (
            sum(pipe_missing_prefix_counts.values())
            == focus_framing_record_counts["pipe_missing"]
        ),
        "short_fragment_count": short_fragment_count,
        "short_fragment_token_counts": short_fragment_token_counts_output,
        "short_fragment_length_counts": short_fragment_length_counts_output,
        "short_fragment_byteclass_counts": (
            short_fragment_byteclass_counts_output
        ),
        "short_fragment_character_counts": (
            short_fragment_character_counts_output
        ),
        "short_fragment_marker_counts": short_fragment_marker_counts_output,
        "short_fragment_header_prefix_present_count": (
            short_fragment_header_prefix_present_count
        ),
        "non_crlf_separator_record_count": non_crlf_separator_record_count,
        "short_fragment_axes_reconcile": short_fragment_axes_reconcile,
        "short_fragments_authorized_to_skip": False,
        "other_rejected_failure_counts": other_failure_counts_output,
        "other_rejected_core_counts": other_core_counts_output,
        "other_rejected_member_scope_counts": (
            other_member_scope_counts_output
        ),
        "other_rejected_event_scope_counts": other_event_scope_counts_output,
        "other_rejected_framing_counts": other_framing_counts_output,
        "other_rejected_joint_counts": other_joint_counts_output,
        "other_rejected_joint_total": other_joint_total,
        "other_rejected_joint_marginals_match": (
            other_joint_marginals_match
        ),
        "other_rejected_axes_reconcile": other_axes_reconcile,
        "focus_non_strict_core_total": focus_non_strict_core_total,
        "focus_non_short_remainder_count": focus_non_short_remainder_count,
        "remainder_record_count": remainder_record_count,
        "remainder_accounting_complete": remainder_accounting_complete,
        "post_policy_simulation": post_policy_simulation,
        "focus_counts_plus_other_match_rejected_total": (
            focus_framing_total + focus_other_rejected_header_count
            == strict_header_rejected_record_count
        ),
        "focus_accounting_complete": focus_accounting_complete,
        "scope_counts_sum_matches_invalid_total": (
            invalid_scope_total == invalid_header_record_count
        ),
        "reason_counts_sum_matches_invalid_total": (
            invalid_failure_total == invalid_header_record_count
        ),
        "event_counts_sum_matches_invalid_total": (
            invalid_event_total == invalid_header_record_count
        ),
        "origin_counts_sum_matches_invalid_total": (
            invalid_origin_total == invalid_header_record_count
        ),
        "invalid_allowlisted_membership_event_count": (
            invalid_allowlisted_membership_event_count
        ),
        "invalid_allowlisted_membership_payload_total": (
            invalid_allowlisted_membership_payload_total
        ),
        "payload_counts_sum_matches_allowlisted_membership_events": (
            invalid_allowlisted_membership_payload_total
            == invalid_allowlisted_membership_event_count
        ),
        "classification_complete": (
            invalid_scope_total == invalid_header_record_count
            and invalid_failure_total == invalid_header_record_count
            and invalid_event_total == invalid_header_record_count
            and invalid_origin_total == invalid_header_record_count
            and invalid_allowlisted_membership_payload_total
            == invalid_allowlisted_membership_event_count
            and residual_unclassified_count == 0
            and focus_accounting_complete
            and system_zero_matrix_total == system_zero_observed_total
            and system_zero_policy_caps_ok
            and remainder_accounting_complete
            and policy_simulation_accounting_complete
        ),
    }
    return {
        **safe_summary,
        "structural_summary_sha256": _canonical_json_sha256(safe_summary),
    }


def _empty_result() -> dict:
    return {
        "schema": DIAGNOSTIC_SCHEMA,
        "mode": "read_only",
        "status": "blocked",
        # ``completed`` means the structural scan finished.  This deliberately
        # remains false because diagnostics never authorize invoice processing.
        "diagnostic_conclusive": False,
        "authorizes_invoice_processing": False,
        "reason_codes": [],
        "source": None,
        "allowlist": None,
        "analysis": None,
        "privacy": {
            "raw_rows_returned": False,
            "member_ids_returned": False,
            "names_returned": False,
            "emails_returned": False,
            "phones_returned": False,
            "addresses_returned": False,
            "bank_data_returned": False,
            "payment_values_returned": False,
            "dates_returned": False,
            "transaction_ids_returned": False,
            "paths_returned": False,
            "per_record_hashes_returned": False,
            "files_written": False,
            "network_used": False,
        },
    }


def diagnose_invoice_backup(
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
            not probe._lexically_local_absolute_windows_path(backup_path)
            or not probe._lexically_local_absolute_windows_path(allowlist_path)
        ):
            raise probe.ProbeBlocked("backup_path_invalid")
        expected_source_hash = str(expected_sha256 or "").strip().casefold()
        expected_allowlist_hash = str(
            expected_allowlist_set_sha256 or ""
        ).strip().casefold()
        if (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or expected_length < 1
            or expected_length > probe.MAX_ARCHIVE_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", expected_source_hash)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_allowlist_hash)
            or not isinstance(expected_allowlist_count, int)
            or isinstance(expected_allowlist_count, bool)
            or expected_allowlist_count < 1
            or expected_allowlist_count > 100
        ):
            raise probe.ProbeBlocked("invalid_arguments")

        allowlist_read = probe._read_stable_file_twice(
            Path(allowlist_path), maximum_bytes=probe.MAX_ALLOWLIST_BYTES
        )
        allowlist, allowlist_hash = probe._canonical_allowlist(allowlist_read.data)
        if (
            len(allowlist) != expected_allowlist_count
            or allowlist_hash != expected_allowlist_hash
        ):
            raise probe.ProbeBlocked("allowlist_binding_mismatch")
        result["allowlist"] = {
            "count": len(allowlist),
            "set_sha256": allowlist_hash,
        }

        stable = probe._read_stable_file_twice(
            Path(backup_path),
            maximum_bytes=probe.MAX_ARCHIVE_BYTES,
            expected_length=expected_length,
        )
        if stable.source_sha256 != expected_source_hash:
            raise probe.ProbeBlocked("source_hash_mismatch")
        snapshot = probe._inspect_archive(stable.data)
        result["source"] = {
            "kind": probe.SOURCE_KIND,
            "byte_length": stable.byte_length,
            "source_sha256": stable.source_sha256,
            "file_identity_sha256": stable.file_identity_sha256,
            "stable_read_count": stable.stable_read_count,
            "journal_byte_length": len(snapshot.journal_bytes),
            "journal_sha256": sha256(snapshot.journal_bytes).hexdigest(),
            "crc_verified": True,
        }
        result["analysis"] = diagnose_journal_structure(
            snapshot.journal_bytes, allowlist
        )
        result["status"] = "completed"
        return result
    except probe.ProbeBlocked as exc:
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
        result = diagnose_invoice_backup(
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
            exc.reason_code
            if isinstance(exc, probe.ProbeBlocked)
            else "probe_internal_error"
        ]
    print(
        json.dumps(
            result,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
