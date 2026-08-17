import json
import unittest

from ga_journal_snapshot import (
    CLASSIFIER_VERSION,
    EXPORT_VERSION_RECORD,
    VOIDED_EVENT_MASK,
    canonical_member_record_multiset_sha256,
    parse_member_scoped_journal_snapshot_bytes,
    validate_official_journal_export_bytes,
)


TARGET_MEMBER = "90001"


def journal_line(
    *,
    member_number: str = TARGET_MEMBER,
    event_type: int = 3,
    sequence: str = "7001",
    payload: bytes | None = b"opaque-payload",
    field_7: int = 0,
) -> bytes:
    header = (
        b"c20260215!1558 "
        + sequence.encode("ascii")
        + b" 1771185480 0 990001 "
        + member_number.encode("ascii")
        + b" "
        + str(event_type).encode("ascii")
        + b" "
        + str(field_7).encode("ascii")
        + b" 0 0 29"
    )
    return header if payload is None else header + b"|" + payload


class MemberScopedJournalSnapshotTests(unittest.TestCase):
    def parse(
        self,
        rows: list[bytes],
        *,
        coverage: bool = True,
        include_version: bool = True,
    ):
        records = list(rows)
        if include_version:
            records.insert(0, EXPORT_VERSION_RECORD)
        return parse_member_scoped_journal_snapshot_bytes(
            b"\r\n".join(records),
            member_number=TARGET_MEMBER,
            source_coverage_proven=coverage,
        )

    def test_includes_all_valid_target_event_types_and_ignores_other_member(self):
        target_rows = [
            journal_line(event_type=1, sequence="7001", payload=b"new"),
            journal_line(event_type=3, sequence="7002", payload=b"renew"),
            journal_line(event_type=38, sequence="7003", payload=b"proshop"),
            journal_line(event_type=43, sequence="7004", payload=b"account"),
            journal_line(event_type=77, sequence="7005", payload=b"unknown"),
        ]
        snapshot = self.parse(
            target_rows
            + [
                journal_line(
                    member_number="99999",
                    event_type=38,
                    sequence="7006",
                    payload=b"unrelated",
                )
            ]
        )

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.classifier_version, CLASSIFIER_VERSION)
        self.assertEqual(snapshot.member_record_count, 5)
        self.assertEqual(len(snapshot.member_record_hashes), 5)
        self.assertEqual(snapshot.issues, ())

    def test_duplicate_rows_are_retained_in_multiset(self):
        row = journal_line(event_type=38, payload=b"duplicate")
        single = self.parse([row])
        duplicate = self.parse([row, row])

        self.assertEqual(single.member_record_count, 1)
        self.assertEqual(duplicate.member_record_count, 2)
        self.assertEqual(
            duplicate.member_record_hashes,
            (single.member_record_hashes[0], single.member_record_hashes[0]),
        )
        self.assertNotEqual(
            single.member_record_multiset_sha256,
            duplicate.member_record_multiset_sha256,
        )

    def test_append_delete_edit_and_void_each_change_scoped_evidence(self):
        first = journal_line(event_type=3, sequence="7001", payload=b"first")
        second = journal_line(event_type=43, sequence="7002", payload=b"second")
        base = self.parse([first, second])

        variants = {
            "append": self.parse(
                [first, second, journal_line(event_type=38, sequence="7003")]
            ),
            "delete": self.parse([first]),
            "edit": self.parse(
                [first, journal_line(event_type=43, sequence="7002", payload=b"changed")]
            ),
            "void": self.parse(
                [
                    journal_line(
                        event_type=3 | VOIDED_EVENT_MASK,
                        sequence="7001",
                        payload=b"first",
                    ),
                    second,
                ]
            ),
        }

        for label, variant in variants.items():
            with self.subTest(label=label):
                self.assertNotEqual(
                    base.member_record_multiset_sha256,
                    variant.member_record_multiset_sha256,
                )

    def test_order_does_not_change_multiset_hash(self):
        first = journal_line(event_type=1, sequence="7001", payload=b"first")
        second = journal_line(event_type=77, sequence="7002", payload=b"second")
        self.assertEqual(
            self.parse([first, second]).member_record_multiset_sha256,
            self.parse([second, first]).member_record_multiset_sha256,
        )

    def test_malformed_target_and_ambiguous_rows_make_scope_incomplete(self):
        malformed_target = (
            b"not-a-timestamp 7001 1771185480 0 990001 90001 3 0 0 0 29|x"
        )
        ambiguous = b"c20260215!1558 malformed"
        snapshot = self.parse([malformed_target, ambiguous])

        self.assertFalse(snapshot.complete)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            [
                "source_coverage_invalid",
                "malformed_target_record",
                "malformed_record_scope_ambiguous",
            ],
        )

    def test_malformed_other_member_row_still_fails_full_export_coverage(self):
        malformed_other = (
            b"not-a-timestamp 7001 1771185480 0 990001 99999 38 0 0 0 29|x"
        )
        snapshot = self.parse(
            [malformed_other, journal_line(event_type=38, payload=b"target")]
        )

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)
        self.assertIn("source_coverage_invalid", [issue.code for issue in snapshot.issues])

    def test_shifted_member_token_cannot_be_attributed_away(self):
        shifted_target = (
            b"c20260215!1558 7001 1771185480 0 990001 99999 "
            b"90001 38 0 0 0 29|shifted-target"
        )
        snapshot = self.parse([shifted_target])

        self.assertFalse(snapshot.complete)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            ["source_coverage_invalid", "malformed_record_scope_ambiguous"],
        )

    def test_source_coverage_is_fail_closed_by_default(self):
        snapshot = parse_member_scoped_journal_snapshot_bytes(
            b"\r\n".join([EXPORT_VERSION_RECORD, journal_line()]),
            member_number=TARGET_MEMBER,
        )

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            ["source_coverage_unproven"],
        )

    def test_only_crlf_terminates_records_and_payload_lf_is_hashed(self):
        row = journal_line(payload=b"opaque\npayload\x85value")
        snapshot = self.parse([row])

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)

    def test_official_export_record_variants_are_scoped_without_payload_parsing(self):
        snapshot = self.parse(
            [
                journal_line(member_number="0", event_type=38, payload=b"global"),
                journal_line(sequence="7002", payload=None),
                journal_line(sequence="7003", payload=b""),
                journal_line(sequence="7004", field_7=-1, payload=b"sentinel"),
            ]
        )

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 3)
        self.assertEqual(snapshot.issues, ())

    def test_duplicate_or_misplaced_export_version_record_fails_closed(self):
        snapshot = self.parse(
            [journal_line(), EXPORT_VERSION_RECORD, EXPORT_VERSION_RECORD],
            include_version=False,
        )

        self.assertFalse(snapshot.complete)
        self.assertIn("source_coverage_invalid", [issue.code for issue in snapshot.issues])

    def test_lone_cr_is_not_an_export_record_delimiter(self):
        snapshot = parse_member_scoped_journal_snapshot_bytes(
            EXPORT_VERSION_RECORD
            + b"\r\n"
            + journal_line()
            + b"\r"
            + journal_line(sequence="7002"),
            member_number=TARGET_MEMBER,
            source_coverage_proven=True,
        )

        self.assertFalse(snapshot.complete)
        self.assertIn("source_coverage_invalid", [issue.code for issue in snapshot.issues])

    def test_complete_export_coverage_reports_only_sanitized_counts(self):
        data = b"\r\n".join(
            [
                EXPORT_VERSION_RECORD,
                journal_line(member_number="0", payload=b"private-global-canary"),
                journal_line(sequence="7002", payload=None),
                journal_line(sequence="7003", payload=b""),
                journal_line(sequence="7004", payload=b"embedded\nline"),
            ]
        )

        coverage = validate_official_journal_export_bytes(data)

        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.record_count, 5)
        self.assertEqual(coverage.supported_record_count, 4)
        self.assertEqual(coverage.member_record_count, 3)
        self.assertEqual(coverage.global_record_count, 1)
        self.assertEqual(coverage.header_only_record_count, 1)
        self.assertEqual(coverage.empty_payload_record_count, 1)
        self.assertEqual(coverage.embedded_lf_record_count, 1)
        serialized = json.dumps(coverage.as_sanitized_dict())
        self.assertNotIn("embedded\nline", serialized)
        self.assertNotIn("private-global-canary", serialized)

    def test_coverage_requires_exact_leading_version_and_all_supported_records(self):
        missing = validate_official_journal_export_bytes(journal_line())
        malformed = validate_official_journal_export_bytes(
            b"\r\n".join([EXPORT_VERSION_RECORD, journal_line(), b"not-a-record"])
        )

        self.assertFalse(missing.complete)
        self.assertIn("export_version_missing", {issue.code for issue in missing.issues})
        self.assertFalse(malformed.complete)
        self.assertIn(
            "unsupported_export_record",
            {issue.code for issue in malformed.issues},
        )

    def test_version_only_export_fails_closed(self):
        coverage = validate_official_journal_export_bytes(EXPORT_VERSION_RECORD + b"\r\n")

        self.assertFalse(coverage.complete)
        self.assertIn(
            "journal_export_no_data_records",
            {issue.code for issue in coverage.issues},
        )

    def test_whitespace_only_record_fails_full_export_coverage(self):
        row = journal_line().replace(b" 1771185480", b"\t1771185480", 1)
        snapshot = self.parse([b"   \t", row, b"\t"])

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)
        self.assertIn("source_coverage_invalid", [issue.code for issue in snapshot.issues])

    def test_serialized_scope_contains_no_raw_record_data(self):
        secret_values = [
            "Member Name",
            "6500",
            r"C:\Gym Assistant 2.6\Data\Journal.dat",
            "raw-transaction-value",
        ]
        row = journal_line(payload="|".join(secret_values).encode("latin-1"))
        snapshot = self.parse([row])
        serialized = json.dumps(snapshot.as_evidence_scope(), sort_keys=True)

        for value in secret_values:
            self.assertNotIn(value, serialized)
        self.assertEqual(
            set(snapshot.as_evidence_scope()),
            {
                "classifier_version",
                "complete",
                "member_record_count",
                "member_record_multiset_sha256",
                "issue_count",
            },
        )

    def test_canonical_multiset_rejects_non_hash_values(self):
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            canonical_member_record_multiset_sha256(TARGET_MEMBER, ["not-a-hash"])

    def test_zero_target_records_can_be_complete_only_with_proven_coverage(self):
        unrelated = journal_line(member_number="99999", event_type=43)
        complete = self.parse([unrelated], coverage=True)
        unproven = self.parse([unrelated], coverage=False)

        self.assertTrue(complete.complete)
        self.assertEqual(complete.member_record_count, 0)
        self.assertFalse(unproven.complete)


if __name__ == "__main__":
    unittest.main()
