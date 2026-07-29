import unittest
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal

from gymassistant_payment_writer import (
    BlockingDialog,
    PaymentDialogTimeout,
    Rect,
    WindowInfo,
    find_main_window,
    find_member_activation_prompt,
    open_payment_dialog,
    run_writer,
    wait_for_payment_dialog,
)


def visible_window(hwnd=100):
    return WindowInfo(
        hwnd=hwnd,
        parent=0,
        control_id=0,
        class_name="#32770",
        text="",
        enabled=True,
        visible=True,
        rect=Rect(0, 0, 400, 200),
    )


def titled_window(title, hwnd=100):
    window = visible_window(hwnd)
    return WindowInfo(
        hwnd=window.hwnd,
        parent=window.parent,
        control_id=window.control_id,
        class_name=window.class_name,
        text=title,
        enabled=window.enabled,
        visible=window.visible,
        rect=window.rect,
    )


class GymAssistantPaymentWriterTests(unittest.TestCase):
    def test_find_main_window_uses_exact_path_not_prefix_match(self):
        source_root = Path.cwd()
        expected_data = str((source_root / "Data").resolve())
        windows = [
            titled_window(
                f"Gym Assistant 2.6 [Path={expected_data}Backup]",
                hwnd=20,
            ),
            titled_window(
                f"Gym Assistant 2.6 [Path={expected_data}]",
                hwnd=21,
            ),
        ]

        with patch(
            "gymassistant_payment_writer.enum_top_windows",
            return_value=windows,
        ):
            hwnd = find_main_window(source_root)

        self.assertEqual(hwnd, 21)

    def test_find_main_window_refuses_single_nonmatching_candidate(self):
        source_root = Path.cwd()
        expected_data = str((source_root / "Data").resolve())

        with patch(
            "gymassistant_payment_writer.enum_top_windows",
            return_value=[
                titled_window(
                    f"Gym Assistant 2.6 [Path={expected_data}Backup]",
                    hwnd=22,
                )
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "0 exact match"):
                find_main_window(source_root)

    def test_find_main_window_refuses_multiple_exact_matches(self):
        source_root = Path.cwd()
        expected_data = str((source_root / "Data").resolve())

        with patch(
            "gymassistant_payment_writer.enum_top_windows",
            return_value=[
                titled_window(
                    f"Gym Assistant 2.6 [Path={expected_data}]",
                    hwnd=23,
                ),
                titled_window(
                    f"Gym Assistant 2.6 [Path={expected_data}]",
                    hwnd=24,
                ),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "2 exact match"):
                find_main_window(source_root)

    def test_find_member_activation_prompt_matches_current_member(self):
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[visible_window(10)]),
            patch(
                "gymassistant_payment_writer.dialog_texts",
                return_value=[
                    "Member #24023 Benjamin Sanchez is currently Inactive.  Do you want to activate this member?",
                    "Cancel",
                    "No",
                    "Yes",
                ],
            ),
        ):
            dialog = find_member_activation_prompt("24023")

        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.hwnd, 10)
        self.assertIn("activation prompt was accepted", dialog.reason)

    def test_find_member_activation_prompt_ignores_other_member(self):
        with (
            patch("gymassistant_payment_writer.enum_top_windows", return_value=[visible_window(11)]),
            patch(
                "gymassistant_payment_writer.dialog_texts",
                return_value=[
                    "Member #24023 Benjamin Sanchez is currently Inactive.  Do you want to activate this member?",
                    "Cancel",
                    "No",
                    "Yes",
                ],
            ),
        ):
            dialog = find_member_activation_prompt("34203")

        self.assertIsNone(dialog)

    def test_wait_for_payment_dialog_accepts_activation_prompt_and_continues(self):
        activation_dialog = BlockingDialog(
            hwnd=12,
            reason="Gym Assistant member was inactive; activation prompt was accepted before recording payment.",
            texts=["Member #24023 is currently Inactive. Do you want to activate this member?"],
        )
        activation_events = []

        with (
            patch("gymassistant_payment_writer.find_open_payment_dialog", side_effect=[None, 99]),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.find_member_activation_prompt", return_value=activation_dialog),
            patch("gymassistant_payment_writer.accept_activation_dialog") as accept_activation,
            patch("gymassistant_payment_writer.time.sleep"),
        ):
            hwnd = wait_for_payment_dialog("24023", 2, activation_events)

        self.assertEqual(hwnd, 99)
        accept_activation.assert_called_once_with(activation_dialog)
        self.assertEqual(activation_events, [activation_dialog.reason])

    def test_run_writer_defers_when_payment_dialog_never_opens(self):
        payload = {
            "source_root": str(Path.cwd()),
            "update": {"member_id": "18951"},
        }

        with (
            patch("gymassistant_payment_writer.find_main_window", return_value=10),
            patch("gymassistant_payment_writer.window_process_id", return_value=20),
            patch("gymassistant_payment_writer.find_open_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.member_view_blocking_reason", return_value=None),
            patch(
                "gymassistant_payment_writer.open_payment_dialog",
                side_effect=PaymentDialogTimeout("payment form did not open"),
            ),
            patch("gymassistant_payment_writer.apply_payment") as apply_payment,
        ):
            result = run_writer(
                payload,
                apply=True,
                timeout=1,
                foreground_ui=True,
            )

        self.assertEqual(
            result,
            {
                "status": "deferred",
                "applied": False,
                "member_id": "18951",
                "reason": "payment_dialog_did_not_open",
            },
        )
        apply_payment.assert_not_called()

    def test_open_payment_dialog_retries_only_the_outer_button_handle(self):
        outer_button = WindowInfo(
            hwnd=42,
            parent=10,
            control_id=7,
            class_name="Button",
            text="Record a Payment",
            enabled=True,
            visible=True,
            rect=Rect(10, 10, 110, 40),
        )

        with (
            patch("gymassistant_payment_writer.user32") as user32,
            patch("gymassistant_payment_writer.wait_until", return_value=outer_button),
            patch("gymassistant_payment_writer.credit_balance_decline_authorized", return_value=False),
            patch("gymassistant_payment_writer.read_selected_member_balance", return_value=Decimal("0.00")),
            patch("gymassistant_payment_writer.find_credit_balance_prompt", return_value=None),
            patch(
                "gymassistant_payment_writer.wait_for_payment_dialog",
                side_effect=PaymentDialogTimeout("payment form did not open"),
            ) as wait_for_dialog,
            patch("gymassistant_payment_writer.click_button") as click_button,
            patch("gymassistant_payment_writer.post_command") as post_command,
        ):
            with self.assertRaises(PaymentDialogTimeout):
                open_payment_dialog(
                    10,
                    "18951",
                    1,
                    update={},
                )

        self.assertEqual(wait_for_dialog.call_count, 3)
        self.assertEqual(
            [call.args for call in click_button.call_args_list],
            [(42,), (42,)],
        )
        post_command.assert_called_once_with(10, 2004)
        self.assertEqual(user32.SetForegroundWindow.call_count, 2)

    def test_run_writer_keeps_post_commit_error_ambiguous(self):
        payload = {
            "source_root": str(Path.cwd()),
            "update": {"member_id": "18951"},
        }

        with (
            patch("gymassistant_payment_writer.find_main_window", return_value=10),
            patch("gymassistant_payment_writer.window_process_id", return_value=20),
            patch("gymassistant_payment_writer.find_open_payment_dialog", return_value=None),
            patch("gymassistant_payment_writer.find_dependent_payment_prompt", return_value=None),
            patch("gymassistant_payment_writer.select_member"),
            patch("gymassistant_payment_writer.member_view_blocking_reason", return_value=None),
            patch("gymassistant_payment_writer.open_payment_dialog", return_value=30),
            patch(
                "gymassistant_payment_writer.inspect_payment_dialog",
                return_value={"current_balance": "0.00"},
            ),
            patch(
                "gymassistant_payment_writer.apply_payment",
                side_effect=RuntimeError("transaction result not observed"),
            ),
            patch("gymassistant_payment_writer.verify_member_payment_readback") as readback,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "transaction result not observed",
            ):
                run_writer(
                    payload,
                    apply=True,
                    timeout=1,
                    foreground_ui=True,
                )

        readback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
