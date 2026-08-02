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


if __name__ == "__main__":
    unittest.main()
