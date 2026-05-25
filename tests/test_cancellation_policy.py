from datetime import date
import unittest

from cancellation_policy import (
    add_months,
    detect_term_months,
    evaluate_cancellation_policy,
    is_short_pass,
)


class CancellationPolicyTests(unittest.TestCase):
    def test_detects_only_dreamz_fixed_contracts(self):
        self.assertEqual(detect_term_months("contract Dreamz 6 mo"), 6)
        self.assertEqual(detect_term_months("Contract 6 months 20"), 6)
        self.assertEqual(detect_term_months("contract Dreamz 12 m"), 12)
        self.assertEqual(detect_term_months("Contract 12 months 2"), 12)
        self.assertIsNone(detect_term_months("no contract 1 month"))
        self.assertIsNone(detect_term_months("JICN Year Plan"))

    def test_add_months_handles_end_of_month(self):
        self.assertEqual(add_months(date(2025, 1, 31), 1), date(2025, 2, 28))
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))

    def test_12_month_contract_window_is_open_for_ten_days(self):
        result = evaluate_cancellation_policy(
            today=date(2025, 9, 5),
            plan_type="contract Dreamz 12 m",
            contract_begin=date(2021, 10, 5),
            contract_end=date(2022, 10, 5),
        )

        self.assertTrue(result.can_request)
        self.assertEqual(result.status, "allowed_in_window")
        self.assertEqual(result.current_term_end, date(2025, 10, 5))
        self.assertEqual(result.window_open, date(2025, 9, 5))
        self.assertEqual(result.last_request_date, date(2025, 9, 14))
        self.assertEqual(result.window_close_exclusive, date(2025, 9, 15))

    def test_day_before_window_is_blocked_as_too_early(self):
        result = evaluate_cancellation_policy(
            today=date(2025, 9, 4),
            plan_type="contract Dreamz 12 m",
            contract_begin=date(2021, 10, 5),
            contract_end=date(2022, 10, 5),
        )

        self.assertFalse(result.can_request)
        self.assertEqual(result.status, "blocked_too_early")
        self.assertEqual(result.next_window_open, date(2025, 9, 5))
        self.assertEqual(result.next_window_last_request_date, date(2025, 9, 14))

    def test_day_after_ten_day_window_is_closed_until_next_term(self):
        result = evaluate_cancellation_policy(
            today=date(2025, 9, 15),
            plan_type="contract Dreamz 12 m",
            contract_begin=date(2021, 10, 5),
            contract_end=date(2022, 10, 5),
        )

        self.assertFalse(result.can_request)
        self.assertEqual(result.status, "blocked_window_closed")
        self.assertEqual(result.current_term_end, date(2025, 10, 5))
        self.assertEqual(result.next_window_open, date(2026, 9, 5))
        self.assertEqual(result.next_window_last_request_date, date(2026, 9, 14))

    def test_6_month_contract_renews_in_six_month_steps(self):
        result = evaluate_cancellation_policy(
            today=date(2025, 7, 2),
            plan_type="contract Dreamz 6 mo",
            contract_begin=date(2022, 2, 1),
            contract_end=date(2022, 8, 1),
        )

        self.assertTrue(result.can_request)
        self.assertEqual(result.current_term_end, date(2025, 8, 1))
        self.assertEqual(result.window_open, date(2025, 7, 2))
        self.assertEqual(result.last_request_date, date(2025, 7, 11))

    def test_no_contract_can_request_without_fixed_term_window(self):
        result = evaluate_cancellation_policy(
            today=date(2025, 5, 1),
            plan_type="no contract 1 month",
            contract_begin=date(2022, 2, 1),
            contract_end=None,
        )

        self.assertTrue(result.can_request)
        self.assertEqual(result.status, "allowed_no_fixed_term")
        self.assertIsNone(result.term_months)

    def test_short_pass_does_not_allow_cancellation_request(self):
        result = evaluate_cancellation_policy(
            today=date(2026, 5, 25),
            plan_type="2 WEEKS PASS",
            contract_type="No-Contract",
            contract_begin=date(2026, 5, 25),
        )

        self.assertFalse(result.can_request)
        self.assertEqual(result.status, "not_applicable_short_pass")
        self.assertTrue(is_short_pass("2 WEEKS PASS", "No-Contract"))


if __name__ == "__main__":
    unittest.main()
