from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
import zipfile

from ga_journal import (
    billing_option_code,
    parse_gymassistant_backup_billing_catalog,
    parse_gymassistant_backup_journal,
    parse_gymassistant_billing_catalog_text,
    parse_gymassistant_journal_text,
    parse_membership_journal_line,
    service_period_matches_catalog_interval,
)


ACTIVE_RENEWAL = (
    "c20260201!1214 4930 1769948040 0 990001 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 6000 0 0 6000 0 0 0"
)
VOIDED_RENEWAL = (
    "c20260202!0530 7D77 1770010200 0 990002 90002 32771 0 0 0 29"
    "|900000102 256 20260202 20260302 5500 0 0 5500 0 0 0"
)
ZERO_VALUE_RENEWAL = (
    "c20260203!0741 413B 1770104460 0 990003 90003 3 0 3 0 29"
    "|900000103 3072 20260201 20270201 0 0 0 0 0 0 0"
)
ACCOUNT_CREDIT_RENEWAL = (
    "c20260201!1215 4931 1769948100 0 990004 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 8000 0 0 0 0 0 -8000"
)
ACCOUNT_PAYMENT = (
    "c20260215!0921 749D 1771147260 0 0 90003 43 0 0 0 25"
    "|43210 2 4294967295"
)
PROSHOP_PURCHASE = (
    "c20260215!2029 29B1 1771187340 0 0 90003 38 0 0 0 34"
    "|850 ProShop purchase"
)


class GymAssistantJournalTests(unittest.TestCase):
    def test_parses_membership_renewal_without_using_account_or_proshop_entries(self):
        result = parse_gymassistant_journal_text(
            "\n".join([ACTIVE_RENEWAL, ACCOUNT_PAYMENT, PROSHOP_PURCHASE])
        )

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.member_id, "90001")
        self.assertEqual(event.event_name, "membership_renewal")
        self.assertEqual(event.occurred_at, datetime(2026, 2, 1, 12, 14))
        self.assertEqual(event.service_period_start, date(2026, 2, 1))
        self.assertEqual(event.service_period_end_exclusive, date(2026, 3, 1))
        self.assertEqual(event.service_period_end, date(2026, 2, 28))
        self.assertEqual(event.dues_cents, 6000)
        self.assertEqual(event.tender_total_cents, 6000)
        self.assertTrue(event.is_positive_membership_payment)
        self.assertTrue(event.source_reference.startswith("ga-journal:"))

    def test_high_bit_marks_voided_membership_event(self):
        event = parse_membership_journal_line(VOIDED_RENEWAL)

        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, 3)
        self.assertTrue(event.is_voided)
        self.assertFalse(event.is_positive_membership_payment)

    def test_source_identity_stays_stable_when_event_is_voided(self):
        active = VOIDED_RENEWAL.replace(" 32771 ", " 3 ")

        active_event = parse_membership_journal_line(active)
        voided_event = parse_membership_journal_line(VOIDED_RENEWAL)

        # The source reference is a snapshot fingerprint whose stability assumes
        # GymAssistant changes only the event-type void bit in a later snapshot.
        self.assertEqual(active_event.source_reference, voided_event.source_reference)
        self.assertNotEqual(active_event.source_payload_hash, voided_event.source_payload_hash)

    def test_account_credit_renewal_is_retained_for_manual_review(self):
        result = parse_gymassistant_journal_text(ACCOUNT_CREDIT_RENEWAL)

        self.assertEqual(result.issues, [])
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.dues_cents, 8000)
        self.assertEqual(event.balance_payment_cents, -8000)
        self.assertEqual(event.tender_total_cents, 0)
        self.assertTrue(event.requires_manual_review)
        self.assertEqual(
            event.manual_review_reason,
            "membership_payment_uses_account_credit",
        )
        self.assertFalse(event.is_positive_membership_payment)
        self.assertEqual(
            event.as_sync_record()["manual_review_reason"],
            "membership_payment_uses_account_credit",
        )

    def test_non_reconciling_payment_components_are_reported(self):
        invalid = ACTIVE_RENEWAL.replace(
            "6000 0 0 6000 0 0 0",
            "6000 0 0 5999 0 0 0",
        )

        result = parse_gymassistant_journal_text(invalid)

        self.assertEqual(result.events, [])
        self.assertEqual(len(result.issues), 1)
        self.assertIn("components do not reconcile", result.issues[0].message)

    def test_zero_value_membership_event_is_not_invoice_eligible(self):
        event = parse_membership_journal_line(ZERO_VALUE_RENEWAL)

        self.assertIsNotNone(event)
        self.assertEqual(event.member_id, "90003")
        self.assertEqual(event.tender_total_cents, 0)
        self.assertFalse(event.is_positive_membership_payment)

    def test_member_allowlist_limits_sensitive_financial_events(self):
        result = parse_gymassistant_journal_text(
            "\n".join([ACTIVE_RENEWAL, ZERO_VALUE_RENEWAL]),
            member_ids={"90003"},
        )

        self.assertEqual([event.member_id for event in result.events], ["90003"])

    def test_non_allowlisted_parse_error_is_not_attributed_to_pilot_member(self):
        malformed_other_member = (
            "c20260201!1216 4932 1769948160 0 990005 99999 3 0 0 0 29"
            "|900000101 257 broken payload"
        )

        result = parse_gymassistant_journal_text(
            "\n".join([ACTIVE_RENEWAL, malformed_other_member]),
            member_ids={"90001"},
        )

        self.assertEqual([event.member_id for event in result.events], ["90001"])
        self.assertEqual(result.issues, [])

    def test_torn_allowlisted_membership_header_is_reported(self):
        torn_pilot_line = (
            "bad-timestamp 4932 1769948160 0 990006 90001 3 0 0 0 29"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )

        result = parse_gymassistant_journal_text(
            "\n".join([ACTIVE_RENEWAL, torn_pilot_line]),
            member_ids={"90001"},
        )

        self.assertEqual([event.member_id for event in result.events], ["90001"])
        self.assertEqual(len(result.issues), 1)
        self.assertIn("invalid header or payload", result.issues[0].message)

    def test_malformed_membership_event_is_reported_but_unrelated_rows_are_ignored(self):
        result = parse_gymassistant_journal_text(
            "\n".join(
                [
                    ACCOUNT_PAYMENT,
                    "c20260201!1214 4930 1769948040 0 990001 90001 3 0 0 0 29|bad",
                ]
            )
        )

        self.assertEqual(result.events, [])
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].line_number, 2)

    def test_reads_journal_from_gymassistant_backup(self):
        with tempfile.NamedTemporaryFile(suffix=".gbu", delete=False) as handle:
            path = Path(handle.name)
        with zipfile.ZipFile(path, "w") as backup:
            backup.writestr("Data/Journal.jtx", ACTIVE_RENEWAL)

        result = parse_gymassistant_backup_journal(path, member_ids={"90001"})

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].member_id, "90001")

    def test_parses_plan_options_and_monthly_addons(self):
        text = """CLASS=contract Dreamz 12 months
MEMBERTYPE_ID=900000201
OPTION=1 MONTHS INV 5500 1
OPTION=1 MONTHS EFT 5500 1
-
MONTH_ADDON=9001,0,8500|Group PT
"""
        options, addons = parse_gymassistant_billing_catalog_text(text)

        self.assertEqual(len(options), 2)
        self.assertEqual(options[0].billing_option_code, 256)
        self.assertEqual(options[1].billing_option_code, 257)
        self.assertEqual(options[0].base_amount_cents, 5500)
        self.assertEqual(addons[0].addon_id, 9001)
        self.assertEqual(addons[0].name, "Group PT")
        self.assertEqual(addons[0].amount_cents, 8500)

    def test_plan_name_storage_is_shared_across_catalog_options(self):
        plan_name = "X" * 4096
        text = "\n".join(
            [
                f"CLASS= {plan_name}",
                "MEMBERTYPE_ID=900000201",
                *("OPTION=1 MONTHS INV 5500 1" for _ in range(1000)),
                "-",
            ]
        )

        options, addons = parse_gymassistant_billing_catalog_text(text)

        self.assertEqual(addons, [])
        self.assertEqual(len(options), 1000)
        self.assertEqual(options[0].plan_name, plan_name)
        self.assertEqual(len({id(option.plan_name) for option in options}), 1)

    def test_reads_billing_catalog_from_gymassistant_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "GABackup.gbu"
            with zipfile.ZipFile(path, "w") as backup:
                backup.writestr(
                    "Members.btx",
                    "\n".join([
                        "CLASS=contract Dreamz 6 months",
                        "MEMBERTYPE_ID=900000101",
                        "OPTION=1 MONTHS EFT 6500 0",
                        "-",
                    ]),
                )

            options, addons = parse_gymassistant_backup_billing_catalog(path)

        self.assertEqual(addons, [])
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].membership_type_id, 900000101)
        self.assertEqual(options[0].billing_option_code, 257)
        self.assertEqual(options[0].base_amount_cents, 6500)

    def test_billing_option_codes_match_observed_gymassistant_values(self):
        self.assertEqual(billing_option_code(1, "MONTHS", "INV"), 256)
        self.assertEqual(billing_option_code(1, "MONTHS", "EFT"), 257)
        self.assertEqual(billing_option_code(1, "MONTHS", "CC"), 258)
        self.assertEqual(billing_option_code(1, "WEEKS", "INV"), 288)
        self.assertEqual(billing_option_code(12, "MONTHS", "INV"), 3072)

    def test_service_period_matches_exact_calendar_month_interval(self):
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2026, 4, 1),
                date(2026, 8, 1),
                4,
                "MONTHS",
            )
        )
        self.assertFalse(
            service_period_matches_catalog_interval(
                date(2026, 4, 1),
                date(2026, 7, 31),
                4,
                "MONTHS",
            )
        )

    def test_calendar_month_interval_clamps_month_end_and_handles_leap_year(self):
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2023, 1, 31),
                date(2023, 2, 28),
                1,
                "MONTH",
            )
        )
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2024, 1, 31),
                date(2024, 2, 29),
                1,
                "MONTHS",
            )
        )
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2024, 2, 29),
                date(2025, 2, 28),
                12,
                "MONTHS",
            )
        )

    def test_multi_month_interval_shifts_from_original_date_once(self):
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2023, 1, 31),
                date(2023, 3, 31),
                2,
                "MONTHS",
            )
        )
        self.assertFalse(
            service_period_matches_catalog_interval(
                date(2023, 1, 31),
                date(2023, 3, 28),
                2,
                "MONTHS",
            )
        )

    def test_week_interval_requires_exact_multiple_of_seven_days(self):
        self.assertTrue(
            service_period_matches_catalog_interval(
                date(2026, 12, 29),
                date(2027, 1, 12),
                2,
                "WEEKS",
            )
        )
        self.assertFalse(
            service_period_matches_catalog_interval(
                date(2026, 12, 29),
                date(2027, 1, 11),
                2,
                "WEEK",
            )
        )

    def test_service_period_rejects_invalid_catalog_intervals(self):
        with self.assertRaisesRegex(ValueError, "must be positive"):
            service_period_matches_catalog_interval(
                date(2026, 1, 1),
                date(2026, 2, 1),
                0,
                "MONTHS",
            )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            service_period_matches_catalog_interval(
                date(2026, 1, 1),
                date(2026, 2, 1),
                1,
                "DAYS",
            )


if __name__ == "__main__":
    unittest.main()
