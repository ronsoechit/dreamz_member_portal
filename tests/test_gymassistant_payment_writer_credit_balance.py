import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from gymassistant_payment_writer import (
    CreditBalancePrompt,
    PaymentSafetyError,
    Rect,
    WindowInfo,
    apply_payment,
    credit_balance_decline_authorized,
    decimal_money,
    decline_credit_balance_prompt,
    find_credit_balance_prompt,
    find_open_payment_dialog,
    find_transaction_payment_dialog,
    inspect_payment_dialog,
    open_payment_dialog,
    verify_member_payment_readback,
    wait_for_payment_dialog,
)


def window(
    hwnd,
    *,
    text="",
    class_name="#32770",
    enabled=True,
    visible=True,
    parent=0,
):
    return WindowInfo(
        hwnd=hwnd,
        parent=parent,
        control_id=0,
        class_name=class_name,
        text=text,
        enabled=enabled,
        visible=visible,
        rect=Rect(0, 0, 400, 200),
    )


def button(hwnd, text, *, enabled=True, visible=True):
    return window(
        hwnd,
        text=text,
        class_name="Button",
        enabled=enabled,
        visible=visible,
        parent=10,
    )


def valid_update(**overrides):
    payload = {
        "source": "fep_manager_bank_upload_auto",
        "member_id": "990002",
        "membership_period": "2026-08",
        "bank_amount": "55.00",
        "gym_billing_amount": "55.00",
        "target_values": {
            "last_payment": "2026-07-28",
            "next_payment": "2026-09-01",
        },
    }
    payload.update(overrides)
    return payload


class CreditBalancePromptTests(unittest.TestCase):
    def test_exact_known_prompt_matches_same_gym_process(self):
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[window(10)]),
            patch("gymassistant_payment_writer.window_process_id", side_effect=[77, 77]),
            patch(
                "gymassistant_payment_writer.dialog_texts",
                return_value=[
                    "Member has a credit balance of 6.00 (CR)",
                    "Apply this balance now?",
                    "&Yes",
                    "&No",
                ],
            ),
        ):
            prompt = find_credit_balance_prompt(1)

        self.assertIsNotNone(prompt)
        self.assertEqual(prompt.hwnd, 10)
        self.assertEqual(prompt.display_amount, "6.00 (CR)")

    def test_matching_text_from_other_process_is_ignored(self):
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[window(10)]),
            patch("gymassistant_payment_writer.window_process_id", side_effect=[77, 88]),
            patch(
                "gymassistant_payment_writer.dialog_texts",
                return_value=[
                    "Member has a credit balance of 6.00 (CR)",
                    "Apply this balance now?",
                ],
            ),
        ):
            prompt = find_credit_balance_prompt(1)

        self.assertIsNone(prompt)

    def test_near_match_is_not_recognized(self):
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[window(10)]),
            patch("gymassistant_payment_writer.window_process_id", side_effect=[77, 77]),
            patch(
                "gymassistant_payment_writer.dialog_texts",
                return_value=[
                    "Member has a debit balance of 6.00",
                    "Apply this balance now?",
                ],
            ),
        ):
            prompt = find_credit_balance_prompt(1)

        self.assertIsNone(prompt)

    def test_decline_requires_unique_enabled_yes_and_no(self):
        prompt = CreditBalancePrompt(10, "6.00 (CR)", [])
        unsafe_sets = [
            [button(1, "&Yes")],
            [button(1, "&Yes"), button(2, "&No", enabled=False)],
            [button(1, "&Yes"), button(2, "&No"), button(3, "No")],
            [button(1, "&Yes"), button(2, "&No"), button(3, "Cancel")],
        ]
        for controls in unsafe_sets:
            with self.subTest(controls=[control.text for control in controls]):
                with (
                    patch(
                        "gymassistant_payment_writer.dialog_enabled_buttons",
                        return_value=[control for control in controls if control.enabled],
                    ),
                    patch("gymassistant_payment_writer.click_button") as click,
                ):
                    with self.assertRaises(PaymentSafetyError):
                        decline_credit_balance_prompt(prompt, 1)
                    click.assert_not_called()

    def test_decline_clicks_only_no_and_waits_until_closed(self):
        prompt = CreditBalancePrompt(10, "6.00 (CR)", [])
        with (
            patch(
                "gymassistant_payment_writer.dialog_enabled_buttons",
                return_value=[button(1, "&Yes"), button(2, "&No")],
            ),
            patch("gymassistant_payment_writer.click_button") as click,
            patch("gymassistant_payment_writer.window_is_visible", return_value=False),
        ):
            decline_credit_balance_prompt(prompt, 1)

        click.assert_called_once_with(2)

    def test_only_valid_fep_auto_payment_authorizes_decline(self):
        self.assertTrue(credit_balance_decline_authorized(valid_update()))
        self.assertFalse(
            credit_balance_decline_authorized(
                valid_update(source="manual", member_id="990002")
            )
        )
        self.assertFalse(
            credit_balance_decline_authorized(
                valid_update(target_values={"next_payment": "2026-10-01"})
            )
        )
        self.assertFalse(
            credit_balance_decline_authorized(valid_update(bank_amount="50.00"))
        )

    def test_wait_declines_exact_prompt_once_then_returns_payment_form(self):
        prompt = CreditBalancePrompt(10, "6.00 (CR)", [])
        events = []
        with (
            patch(
                "gymassistant_payment_writer.find_open_payment_dialog",
                side_effect=[None, 99],
            ),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_member_activation_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=prompt),
            patch("gymassistant_payment_writer.decline_credit_balance_prompt") as decline,
            patch("gymassistant_payment_writer.window_process_id", return_value=77),
            patch("gymassistant_payment_writer.time.sleep"),
        ):
            result = wait_for_payment_dialog(
                "990002",
                2,
                main_hwnd=1,
                allow_credit_balance_decline=True,
                expected_credit_balance=Decimal("-6.00"),
                credit_balance_events=events,
            )

        self.assertEqual(result, 99)
        decline.assert_called_once_with(prompt, unittest.mock.ANY)
        self.assertEqual(
            events,
            [
                {
                    "action": "declined_apply_credit_balance",
                    "display_amount": "6.00 (CR)",
                    "selected_member_credit": "-6.00",
                }
            ],
        )

    def test_wait_does_not_click_credit_prompt_for_other_source(self):
        prompt = CreditBalancePrompt(10, "6.00 (CR)", [])
        with (
            patch("gymassistant_payment_writer.find_open_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_member_activation_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=prompt),
            patch("gymassistant_payment_writer.decline_credit_balance_prompt") as decline,
            patch("gymassistant_payment_writer.window_process_id", return_value=77),
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "not an authorized"):
                wait_for_payment_dialog(
                    "990002",
                    1,
                    main_hwnd=1,
                    allow_credit_balance_decline=False,
                    expected_credit_balance=Decimal("-6.00"),
                    credit_balance_events=[],
                )

        decline.assert_not_called()

    def test_wait_does_not_click_when_prompt_amount_differs_from_selected_member(self):
        prompt = CreditBalancePrompt(10, "5.00 (CR)", [])
        with (
            patch("gymassistant_payment_writer.find_open_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_member_activation_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=prompt),
            patch("gymassistant_payment_writer.decline_credit_balance_prompt") as decline,
            patch("gymassistant_payment_writer.window_process_id", return_value=77),
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "does not match"):
                wait_for_payment_dialog(
                    "990002",
                    1,
                    main_hwnd=1,
                    allow_credit_balance_decline=True,
                    expected_credit_balance=Decimal("-6.00"),
                    credit_balance_events=[],
                )

        decline.assert_not_called()

    def test_stale_credit_prompt_is_rejected_before_record_payment_click(self):
        prompt = CreditBalancePrompt(10, "6.00 (CR)", [])
        record_button = button(20, "Record a Payment")
        user32 = Mock()
        with (
            patch("gymassistant_payment_writer.user32", user32),
            patch("gymassistant_payment_writer.find_record_payment_button", return_value=record_button),
            patch(
                "gymassistant_payment_writer.read_selected_member_balance",
                return_value=Decimal("-6.00"),
            ),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=prompt),
            patch("gymassistant_payment_writer.click_button") as click,
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "already visible"):
                open_payment_dialog(1, "990002", 1, update=valid_update())

        click.assert_not_called()

    def test_payment_dialog_finder_uses_expected_process(self):
        other = window(10, text="Member Payment for #990002, Other")
        expected = window(11, text="Member Payment for #990002, Expected")
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[other, expected]),
            patch("gymassistant_payment_writer.window_process_id", side_effect=[88, 77]),
        ):
            result = find_open_payment_dialog("990002", process_id=77)

        self.assertEqual(result, 11)

    def test_transaction_dialog_finder_uses_expected_process(self):
        other = window(10, text="Transaction - Member Payment Other")
        expected = window(11, text="Transaction - Member Payment Expected")
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[other, expected]),
            patch("gymassistant_payment_writer.window_process_id", side_effect=[88, 77]),
        ):
            result = find_transaction_payment_dialog(process_id=77)

        self.assertEqual(result, 11)

    def test_unknown_safety_error_is_not_retried(self):
        record_button = button(20, "Record a Payment")
        user32 = Mock()
        with (
            patch("gymassistant_payment_writer.user32", user32),
            patch("gymassistant_payment_writer.find_record_payment_button", return_value=record_button),
            patch(
                "gymassistant_payment_writer.read_selected_member_balance",
                return_value=Decimal("0.00"),
            ),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=None),
            patch("gymassistant_payment_writer.click_button"),
            patch(
                "gymassistant_payment_writer.wait_for_payment_dialog",
                side_effect=PaymentSafetyError("unknown prompt"),
            ),
            patch("gymassistant_payment_writer.post_command") as post_command,
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "unknown prompt"):
                open_payment_dialog(
                    1,
                    "990003",
                    1,
                    update=valid_update(member_id="990003"),
                )

        post_command.assert_not_called()


class CreditBalancePaymentValidationTests(unittest.TestCase):
    def payment_controls(
        self,
        *,
        current_balance="6.00 (CR)",
        payment_on_balance="0.00",
        total="55.00",
    ):
        values = {
            "Billing Plan:": "contract Dreamz 12 months",
            "Billing Option:": "ACH (Invalid)",
            "Billing Amount:": "55.00",
            "Current Balance:": current_balance,
            "Last Paid Date:": "01/07/2026",
            "Current Due Date:": "01/08/2026",
            "Membership Fees:": "55.00",
            "Other Fees:": "0.00",
            "Payment on Current Balance:": payment_on_balance,
            "Total Payment Due:": total,
            "Next Payment Due:": "01/09/2026",
        }
        controls = {
            label: window(index + 100, text=value, class_name="Static")
            for index, (label, value) in enumerate(values.items())
        }
        controls["Billing Periods"] = window(500, text="1", class_name="ComboBox")
        return controls

    def inspect(self, controls, event):
        def right_of(_children, label, classes=("Static", "Edit", "ComboBox")):
            del classes
            return controls[label]

        with (
            patch("gymassistant_payment_writer.enum_children", return_value=[]),
            patch(
                "gymassistant_payment_writer.window_text",
                return_value="Member Payment for #990002, Voorbeeld Lid",
            ),
            patch("gymassistant_payment_writer.value_right_of", side_effect=right_of),
            patch(
                "gymassistant_payment_writer.value_right_of_label_prefix",
                return_value=controls["Billing Periods"],
            ),
            patch(
                "gymassistant_payment_writer.get_text",
                side_effect=lambda hwnd: next(
                    control.text for control in controls.values() if control.hwnd == hwnd
                ),
            ),
        ):
            return inspect_payment_dialog(1, valid_update(), event)

    def test_credit_is_preserved_and_not_applied_to_membership(self):
        observed = self.inspect(
            self.payment_controls(),
            {
                "action": "declined_apply_credit_balance",
                "display_amount": "6.00 (CR)",
            },
        )
        self.assertEqual(observed["current_balance"], "6.00 (CR)")
        self.assertEqual(observed["payment_on_current_balance"], "0.00")

    def test_ordinary_zero_balance_payment_remains_valid(self):
        observed = self.inspect(
            self.payment_controls(current_balance="0.00"),
            None,
        )
        self.assertEqual(observed["current_balance"], "0.00")
        self.assertEqual(observed["payment_on_current_balance"], "0.00")

    def test_credit_without_declined_prompt_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Current balance"):
            self.inspect(self.payment_controls(), None)

    def test_nonzero_payment_on_balance_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Payment on current balance"):
            self.inspect(
                self.payment_controls(payment_on_balance="6.00", total="55.00"),
                {
                    "action": "declined_apply_credit_balance",
                    "display_amount": "6.00 (CR)",
                },
            )

    def test_prompt_and_form_credit_must_match(self):
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            self.inspect(
                self.payment_controls(),
                {
                    "action": "declined_apply_credit_balance",
                    "display_amount": "5.00 (CR)",
                },
            )

    def test_credit_money_format_is_negative(self):
        self.assertEqual(decimal_money("6.00 (CR)"), Decimal("-6.00"))
        self.assertEqual(decimal_money("$6.00 CR"), Decimal("-6.00"))


class PaymentCompletionTests(unittest.TestCase):
    def test_disappearing_form_without_transaction_is_not_success(self):
        record = button(50, "&Record Payment")
        with (
            patch("gymassistant_payment_writer.enum_children", return_value=[record]),
            patch("gymassistant_payment_writer.click_button"),
            patch("gymassistant_payment_writer.find_transaction_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.find_open_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.window_process_id", return_value=77),
            patch("gymassistant_payment_writer.time.sleep"),
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "without an observed transaction"):
                apply_payment(1, 0.01, member_id="990002")

    def test_refreshed_member_state_must_confirm_payment_and_preserve_credit(self):
        today = date.today()
        update = valid_update(
            target_values={
                "last_payment": (today - timedelta(days=1)).isoformat(),
                "next_payment": "2026-09-01",
            }
        )
        observed = {
            "member_heading": "#990002 Voorbeeld Lid",
            "due_date": "01/09/2026",
            "last_paid_date": today.strftime("%d/%m/%Y"),
            "last_paid_amount": "55.00",
            "current_balance": "6.00 (CR)",
        }
        with (
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.read_member_payment_state", return_value=observed),
        ):
            result = verify_member_payment_readback(
                1,
                update,
                1,
                expected_current_balance=Decimal("-6.00"),
            )

        self.assertEqual(result, observed)

    def test_membership_period_last_paid_date_is_also_valid_readback(self):
        today = date.today()
        update = valid_update(
            target_values={
                "last_payment": (today - timedelta(days=1)).isoformat(),
                "next_payment": "2026-09-01",
            }
        )
        observed = {
            "member_heading": "#990002 Voorbeeld Lid",
            "due_date": "01/09/2026",
            "last_paid_date": "01/08/2026",
            "last_paid_amount": "55.00",
            "current_balance": "6.00 (CR)",
        }
        with (
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.read_member_payment_state", return_value=observed),
        ):
            result = verify_member_payment_readback(
                1,
                update,
                1,
                expected_current_balance=Decimal("-6.00"),
            )

        self.assertEqual(result, observed)

    def test_zero_balance_payment_rejects_nonzero_readback_balance(self):
        today = date.today()
        update = valid_update(
            target_values={
                "last_payment": (today - timedelta(days=1)).isoformat(),
                "next_payment": "2026-09-01",
            }
        )
        observed = {
            "member_heading": "#990002 Voorbeeld Lid",
            "due_date": "01/09/2026",
            "last_paid_date": today.strftime("%d/%m/%Y"),
            "last_paid_amount": "55.00",
            "current_balance": "1.00",
        }
        with (
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.read_member_payment_state", return_value=observed),
            patch("gymassistant_payment_writer.time.sleep"),
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "Current balance changed"):
                verify_member_payment_readback(
                    1,
                    update,
                    0.01,
                    expected_current_balance=Decimal("0.00"),
                )

    def test_unchanged_due_date_cannot_be_reported_applied(self):
        today = date.today()
        update = valid_update(
            target_values={
                "last_payment": (today - timedelta(days=1)).isoformat(),
                "next_payment": "2026-09-01",
            }
        )
        observed = {
            "member_heading": "#990002 Voorbeeld Lid",
            "due_date": "01/08/2026",
            "last_paid_date": today.strftime("%d/%m/%Y"),
            "last_paid_amount": "55.00",
            "current_balance": "6.00 (CR)",
        }
        with (
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.read_member_payment_state", return_value=observed),
            patch("gymassistant_payment_writer.time.sleep"),
        ):
            with self.assertRaisesRegex(PaymentSafetyError, "Due date remained"):
                verify_member_payment_readback(
                    1,
                    update,
                    0.01,
                    expected_current_balance=Decimal("-6.00"),
                )


if __name__ == "__main__":
    unittest.main()
