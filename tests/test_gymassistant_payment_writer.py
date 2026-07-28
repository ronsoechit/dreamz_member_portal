import unittest
from pathlib import Path
from unittest.mock import patch

from gymassistant_payment_writer import (
    BlockingDialog,
    Rect,
    WindowInfo,
    find_main_window,
    find_member_activation_prompt,
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


if __name__ == "__main__":
    unittest.main()
