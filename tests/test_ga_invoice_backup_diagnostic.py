from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from ga_invoice_backup_diagnostic import (
    CLASSIFICATION_KEYS,
    _empty_result,
    diagnose_journal_structure,
)


TARGET_ID = "90001"
ACTIVE_RENEWAL = (
    "c20260201!1214 4930 1769948040 0 990001 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 6000 0 0 6000 0 0 0"
)


class InvoiceBackupDiagnosticTests(unittest.TestCase):
    def diagnose(self, *rows: str) -> dict:
        return diagnose_journal_structure(
            "\r\n".join(rows).encode("latin-1"), frozenset({TARGET_ID})
        )

    def test_zero_member_is_counted_without_hiding_later_target_event(self):
        zero_member = ACTIVE_RENEWAL.replace(" 90001 3 ", " 0 3 ")

        result = self.diagnose(zero_member, ACTIVE_RENEWAL)

        self.assertEqual(result["ambiguous_scope_record_count"], 1)
        self.assertEqual(
            result["classification_counts"]["ambiguous_member_zero"], 1
        )
        self.assertEqual(result["confirmed_target_header_count"], 1)
        self.assertEqual(result["target_membership_candidate_count"], 1)
        self.assertEqual(result["target_parse_success_count"], 1)
        self.assertEqual(result["target_parse_failure_count"], 0)

    def test_strict_zero_event_scope_counts_reconcile(self):
        zero_membership = ACTIVE_RENEWAL.replace(" 90001 3 ", " 0 3 ")
        zero_voided = ACTIVE_RENEWAL.replace(" 90001 3 ", " 0 32771 ")
        zero_nonmembership = ACTIVE_RENEWAL.replace(" 90001 3 ", " 0 43 ")

        result = self.diagnose(
            zero_membership,
            zero_voided,
            zero_nonmembership,
        )

        self.assertEqual(result["strict_zero_record_count"], 3)
        self.assertEqual(
            result["strict_zero_event_scope_counts"],
            {
                "membership_1_or_3": 1,
                "voided_membership_1_or_3": 1,
                "nonmembership": 1,
            },
        )
        self.assertTrue(result["strict_zero_counts_sum_matches_total"])

    def test_invalid_headers_receive_fixed_reconciling_breakdown(self):
        header = ACTIVE_RENEWAL.split("|", 1)[0]
        target_empty_payload = header + "|"
        target_event_overflow = ACTIVE_RENEWAL.replace(
            " 90001 3 ", " 90001 65536 "
        )
        zero_empty_payload = target_empty_payload.replace(" 90001 3", " 0 3")
        metadata_without_header = "metadata"

        result = self.diagnose(
            target_empty_payload,
            target_event_overflow,
            zero_empty_payload,
            metadata_without_header,
        )

        self.assertEqual(result["invalid_header_record_count"], 4)
        self.assertEqual(result["target_invalid_header_record_count"], 2)
        self.assertEqual(result["ambiguous_invalid_header_record_count"], 2)
        self.assertEqual(
            result["invalid_header_scope_counts"]["member_allowlisted"], 2
        )
        self.assertEqual(result["invalid_header_scope_counts"]["member_zero"], 1)
        self.assertEqual(
            result["invalid_header_scope_counts"][
                "member_unproven_header_shape"
            ],
            1,
        )
        failures = result["invalid_header_failure_counts"]
        self.assertEqual(failures["member_allowlisted"]["payload_empty"], 1)
        self.assertEqual(
            failures["member_allowlisted"]["event_type_uint16_overflow"], 1
        )
        self.assertEqual(failures["member_zero"]["payload_empty"], 1)
        self.assertEqual(
            failures["member_unproven_header_shape"]["pipe_missing"], 1
        )
        event_scopes = result["invalid_header_event_scope_counts"]
        self.assertEqual(
            event_scopes["member_allowlisted"]["membership_1_or_3"], 1
        )
        self.assertEqual(
            event_scopes["member_allowlisted"]["event_uint16_overflow"], 1
        )
        self.assertEqual(event_scopes["member_zero"]["membership_1_or_3"], 1)
        self.assertEqual(
            event_scopes["member_unproven_header_shape"][
                "event_unproven_header_shape"
            ],
            1,
        )
        self.assertEqual(
            result["invalid_allowlisted_membership_payload_counts"][
                "payload_field_count_not_11"
            ],
            1,
        )
        self.assertEqual(result["residual_unclassified_count"], 0)
        self.assertEqual(result["classified_invalid_header_count"], 4)
        self.assertTrue(result["scope_counts_sum_matches_invalid_total"])
        self.assertTrue(result["reason_counts_sum_matches_invalid_total"])
        self.assertTrue(result["event_counts_sum_matches_invalid_total"])
        self.assertTrue(result["origin_counts_sum_matches_invalid_total"])
        self.assertTrue(
            result[
                "payload_counts_sum_matches_allowlisted_membership_events"
            ]
        )
        self.assertTrue(result["classification_complete"])

    def test_noncanonical_separator_and_edge_whitespace_are_distinguished(self):
        separator = ACTIVE_RENEWAL.replace("!1214 4930", "!1214\v4930")
        edge = " " + ACTIVE_RENEWAL

        result = self.diagnose(separator, edge)
        failures = result["invalid_header_failure_counts"]["member_allowlisted"]

        self.assertEqual(failures["noncanonical_header_separator"], 1)
        self.assertEqual(failures["ascii_edge_whitespace_only"], 1)
        self.assertEqual(result["residual_unclassified_count"], 0)
        self.assertTrue(result["classification_complete"])

    def test_combined_edge_and_separator_defects_are_not_labeled_edge_only(self):
        combined = " " + ACTIVE_RENEWAL.replace("!1214 4930", "!1214\v4930")

        result = self.diagnose(combined)
        failures = result["invalid_header_failure_counts"]["member_allowlisted"]

        self.assertEqual(failures["ascii_edge_whitespace_only"], 0)
        self.assertEqual(failures["noncanonical_header_separator"], 1)
        self.assertTrue(result["classification_complete"])

    def test_very_long_numeric_tokens_use_fixed_overflow_categories(self):
        header, payload = ACTIVE_RENEWAL.split("|", 1)
        tokens = header.split()
        huge_decimal = "9" * 5000

        member_tokens = tokens.copy()
        member_tokens[5] = huge_decimal
        event_tokens = tokens.copy()
        event_tokens[6] = huge_decimal
        other_tokens = tokens.copy()
        other_tokens[2] = huge_decimal
        rows = (
            " ".join(member_tokens) + "|" + payload,
            " ".join(event_tokens) + "|" + payload,
            " ".join(other_tokens) + "|" + payload,
        )

        result = self.diagnose(*rows)

        self.assertEqual(result["invalid_header_record_count"], 3)
        self.assertEqual(
            result["invalid_header_scope_counts"]["member_out_of_uint32"], 1
        )
        failures = result["invalid_header_failure_counts"]
        self.assertEqual(
            failures["member_out_of_uint32"][
                "numeric_header_uint32_overflow"
            ],
            1,
        )
        self.assertEqual(
            failures["member_allowlisted"]["numeric_header_uint32_overflow"],
            2,
        )
        events = result["invalid_header_event_scope_counts"]
        self.assertEqual(
            events["member_allowlisted"]["event_uint16_overflow"], 1
        )
        self.assertTrue(result["classification_complete"])

    def test_non_eleven_token_header_does_not_claim_member_or_event_scope(self):
        header, payload = ACTIVE_RENEWAL.split("|", 1)
        tokens = header.split()
        shifted = " ".join(tokens[:2] + tokens[3:]) + "|" + payload

        result = self.diagnose(shifted)

        self.assertEqual(
            result["invalid_header_scope_counts"][
                "member_unproven_header_shape"
            ],
            1,
        )
        self.assertEqual(
            result["invalid_header_event_scope_counts"][
                "member_unproven_header_shape"
            ]["event_unproven_header_shape"],
            1,
        )
        self.assertTrue(result["classification_complete"])

    def test_invalid_header_first_failure_categories_are_table_driven(self):
        header, payload = ACTIVE_RENEWAL.split("|", 1)
        base = header.split()

        def row_with(index: int, value: str) -> str:
            tokens = base.copy()
            tokens[index] = value
            return " ".join(tokens) + "|" + payload

        cases = {
            "pipe_missing": header,
            "payload_empty": header + "|",
            "header_token_count_not_11": " ".join(base[:-1]) + "|" + payload,
            "timestamp_lexical_invalid": row_with(0, "bad-timestamp"),
            "timestamp_calendar_invalid": row_with(0, "c20261301!1214"),
            "sequence_nonhex": row_with(1, "NOTHEX"),
            "sequence_uint32_overflow": row_with(1, "100000000"),
            "numeric_header_token_nondecimal": row_with(2, "not-decimal"),
            "numeric_header_uint32_overflow": row_with(2, "4294967296"),
            "event_type_uint16_overflow": row_with(6, "65536"),
            "ascii_edge_whitespace_only": " " + ACTIVE_RENEWAL,
            "noncanonical_header_separator": ACTIVE_RENEWAL.replace(
                "!1214 4930", "!1214\v4930"
            ),
        }

        for expected, row in cases.items():
            with self.subTest(expected=expected):
                result = self.diagnose(row)
                observed = sum(
                    scope_counts[expected]
                    for scope_counts in result[
                        "invalid_header_failure_counts"
                    ].values()
                )
                self.assertEqual(observed, 1)
                self.assertEqual(result["residual_unclassified_count"], 0)
                self.assertTrue(result["classification_complete"])

    def test_invalid_member_and_event_tokens_use_explicit_categories(self):
        header, payload = ACTIVE_RENEWAL.split("|", 1)
        base = header.split()

        member_nondecimal = base.copy()
        member_nondecimal[5] = "not-a-member"
        event_nondecimal = base.copy()
        event_nondecimal[6] = "not-an-event"
        event_negative = base.copy()
        event_negative[6] = "-1"

        result = self.diagnose(
            " ".join(member_nondecimal) + "|" + payload,
            " ".join(event_nondecimal) + "|" + payload,
            " ".join(event_negative) + "|" + payload,
        )

        self.assertEqual(
            result["invalid_header_scope_counts"]["member_nondecimal"], 1
        )
        events = result["invalid_header_event_scope_counts"]
        self.assertEqual(events["member_allowlisted"]["event_nondecimal"], 1)
        self.assertEqual(events["member_allowlisted"]["event_negative"], 1)
        self.assertTrue(result["classification_complete"])

    def test_target_payload_failures_use_fixed_categories_only(self):
        bad_field_count = ACTIVE_RENEWAL.rsplit(" ", 1)[0]
        bad_non_decimal = ACTIVE_RENEWAL.replace(" 6000 0 0 6000 ", " x 0 0 6000 ")
        bad_period = ACTIVE_RENEWAL.replace(" 20260201 20260301 ", " 20261301 20260301 ")
        bad_total = ACTIVE_RENEWAL.replace(" 6000 0 0 6000 ", " 6000 0 0 5999 ")

        result = self.diagnose(
            bad_field_count,
            bad_non_decimal,
            bad_period,
            bad_total,
        )

        self.assertEqual(result["target_membership_candidate_count"], 4)
        self.assertEqual(result["target_parse_success_count"], 0)
        self.assertEqual(result["target_parse_failure_count"], 4)
        counts = result["classification_counts"]
        self.assertEqual(counts["target_payload_field_count"], 1)
        self.assertEqual(counts["target_payload_non_decimal"], 1)
        self.assertEqual(counts["target_period_invalid"], 1)
        self.assertEqual(counts["target_component_reconciliation_failed"], 1)

    def test_non_target_malformed_row_is_ignored_when_member_is_attributable(self):
        malformed_non_target = ACTIVE_RENEWAL.replace(" 90001 3 ", " 81234 999999 ")

        result = self.diagnose(malformed_non_target, ACTIVE_RENEWAL)

        self.assertEqual(result["ambiguous_scope_record_count"], 0)
        self.assertTrue(
            all(result["classification_counts"][key] == 0 for key in CLASSIFICATION_KEYS)
        )
        self.assertEqual(result["target_parse_success_count"], 1)

    def test_hidden_header_separator_is_counted_without_returning_source_content(self):
        hidden = "metadata\x85" + ACTIVE_RENEWAL

        result = self.diagnose(hidden)
        encoded = json.dumps(result, sort_keys=True)

        self.assertEqual(
            result["classification_counts"]["hidden_header_separator"], 1
        )
        self.assertEqual(result["ambiguous_scope_record_count"], 1)
        self.assertNotIn(TARGET_ID, encoded)
        self.assertNotIn("metadata", encoded)
        self.assertNotIn("6000", encoded)

    def test_structural_summary_hash_is_deterministic_and_changes_with_counts(self):
        first = self.diagnose(ACTIVE_RENEWAL)
        second = self.diagnose(ACTIVE_RENEWAL)
        zero_member = ACTIVE_RENEWAL.replace(" 90001 3 ", " 0 3 ")
        changed = self.diagnose(zero_member, ACTIVE_RENEWAL)

        self.assertEqual(
            first["structural_summary_sha256"],
            second["structural_summary_sha256"],
        )
        self.assertNotEqual(
            first["structural_summary_sha256"],
            changed["structural_summary_sha256"],
        )

    def test_direct_cli_imports_create_no_bytecode_without_dash_b(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            isolated = Path(temporary) / "isolated"
            isolated.mkdir()
            for name in (
                "ga_invoice_backup_diagnostic.py",
                "ga_invoice_backup_probe.py",
                "ga_journal.py",
            ):
                shutil.copy2(repository / name, isolated / name)
            environment = os.environ.copy()
            environment.pop("PYTHONDONTWRITEBYTECODE", None)
            environment.pop("PYTHONPYCACHEPREFIX", None)

            completed = subprocess.run(
                [sys.executable, "-S", str(isolated / "ga_invoice_backup_diagnostic.py")],
                cwd=isolated,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stderr, "")
            self.assertFalse((isolated / "__pycache__").exists())

    def test_completed_diagnostic_is_explicitly_non_authorizing(self):
        result = _empty_result()

        # Structural completion is useful evidence, but it must never become
        # an approval signal for invoices or Gym Assistant mutations.
        self.assertFalse(result["diagnostic_conclusive"])
        self.assertFalse(result["authorizes_invoice_processing"])
        self.assertEqual(
            result["schema"], "dreamz.ga.invoice-backup-parse-diagnostic.v2"
        )


if __name__ == "__main__":
    unittest.main()
