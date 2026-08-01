import json
import unittest

from ga_journal_snapshot import (
    CLASSIFIER_VERSION,
    VOIDED_EVENT_MASK,
    canonical_member_record_multiset_sha256,
    parse_member_scoped_journal_snapshot_bytes,
)


TARGET_MEMBER = "90001"


def journal_line(
    *,
    member_number: str = TARGET_MEMBER,
    event_type: int = 3,
    sequence: str = "7001",
    payload: bytes = b"opaque-payload",
) -> bytes:
    return (
        b"c20260215!1558 "
        + sequence.encode("ascii")
        + b" 1771185480 0 990001 "
        + member_number.encode("ascii")
        + b" "
        + str(event_type).encode("ascii")
        + b" 0 0 0 29|"
        + payload
    )


class MemberScopedJournalSnapshotTests(unittest.TestCase):
    def parse(self, rows: list[bytes], *, coverage: bool = True):
        return parse_member_scoped_journal_snapshot_bytes(
            b"\r\n".join(rows),
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
            b"c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29|"
        )
        ambiguous = b"c20260215!1558 malformed"
        snapshot = self.parse([malformed_target, ambiguous])

        self.assertFalse(snapshot.complete)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            [
                "malformed_target_record",
                "malformed_record_scope_ambiguous",
            ],
        )

    def test_malformed_row_safely_attributed_to_other_member_is_ignored(self):
        malformed_other = (
            b"not-a-timestamp 7001 1771185480 0 990001 99999 38 0 0 0 29|x"
        )
        snapshot = self.parse(
            [malformed_other, journal_line(event_type=38, payload=b"target")]
        )

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)
        self.assertEqual(snapshot.issues, ())

    def test_shifted_member_token_cannot_be_attributed_away(self):
        shifted_target = (
            b"c20260215!1558 7001 1771185480 0 990001 99999 "
            b"90001 38 0 0 0 29|shifted-target"
        )
        snapshot = self.parse([shifted_target])

        self.assertFalse(snapshot.complete)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            ["malformed_record_scope_ambiguous"],
        )

    def test_source_coverage_is_fail_closed_by_default(self):
        snapshot = parse_member_scoped_journal_snapshot_bytes(
            journal_line(),
            member_number=TARGET_MEMBER,
        )

        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)
        self.assertEqual(
            [issue.code for issue in snapshot.issues],
            ["source_coverage_unproven"],
        )

    def test_only_cr_lf_terminators_split_records(self):
        row = journal_line(payload=b"opaque\x85payload")
        snapshot = self.parse([row])

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)

    def test_blank_whitespace_lines_are_ignored_and_header_whitespace_is_valid(self):
        row = journal_line().replace(b" 1771185480", b"\t1771185480", 1)
        snapshot = self.parse([b"   \t", row, b"\t"])

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.member_record_count, 1)

    def test_serialized_scope_contains_no_raw_record_data(self):
        secret_values = [
            "Member Name",
            "6500",
            r"C:\Gym Assistant 2.6\Data\Journal.jtx",
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
