from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.gymassistant_roster_export.roster_export import (
    GymAssistantExporter,
    RosterExportError,
    ValidationPolicy,
    WindowInfo,
    _classify_export_dialog,
    _exported_record_count,
    invoke_accessible_dialog_button,
    pause_signup_bridge,
    promote_candidate,
    run_export,
    validate_candidate,
)


HEADERS = [
    "MemberNum",
    "LastName",
    "FirstName",
    "MemberType",
    "BillingOption",
    "DueDate",
    "BillingAmount",
    "LastPaidDate",
    "LastPaidAmount",
    "SignupDate",
    "ContractEnd",
    "ContractBegin",
    "BirthDate",
    "Email",
    "BillingStatus",
    "CurrentBalance",
    "IsDeleted",
]


def write_csv(path: Path, count: int, *, status: str = "ACTIVE", start: int = 1) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        for member_id in range(start, start + count):
            writer.writerow(
                {
                    "MemberNum": member_id,
                    "LastName": f"Last{member_id}",
                    "FirstName": f"First{member_id}",
                    "MemberType": "Delfins Fitness",
                    "BillingOption": "Monthly",
                    "DueDate": "01/09/2026",
                    "BillingAmount": "80.00",
                    "LastPaidDate": "01/08/2026",
                    "LastPaidAmount": "80.00",
                    "SignupDate": "01/01/2026",
                    "ContractEnd": "00/00/0000",
                    "ContractBegin": "01/01/2026",
                    "BirthDate": "01/01/1990",
                    "Email": f"member{member_id}@example.test",
                    "BillingStatus": status,
                    "CurrentBalance": "0.00",
                    "IsDeleted": "0",
                }
            )


def window_info(
    handle: int,
    *,
    text: str = "",
    class_name: str = "#32770",
    control_id: int = 0,
    parent: int = 0,
) -> WindowInfo:
    return WindowInfo(
        handle=handle,
        parent=parent,
        process_id=42,
        control_id=control_id,
        class_name=class_name,
        text=text,
        left=0,
        top=0,
        right=100,
        bottom=30,
        visible=True,
        enabled=True,
    )


class PromptFlowUI:
    def __init__(self) -> None:
        self.stage = 0
        self.clicked: list[str] = []
        self.filename: str | None = None

    def _controls(self) -> list[WindowInfo]:
        parent = 100 + self.stage
        definitions = (
            (
                ("Save file CSV or Tab-Delimited format?", "Static", 0),
                ("CSV", "Button", 10),
                ("Tab-Delimited", "Button", 11),
            ),
            (
                ("Include financial and bank data?", "Static", 0),
                ("Yes", "Button", 20),
                ("No", "Button", 21),
            ),
            (
                ("", "Edit", 1148),
                ("Save", "Button", 1),
                ("Cancel", "Button", 2),
            ),
            (
                ("MemberData.csv already exists. Do you want to replace it?", "Static", 0),
                ("Yes", "Button", 30),
                ("No", "Button", 31),
            ),
            (
                ("5,027 member records exported.", "Static", 0),
                ("Close", "Button", 40),
            ),
        )
        return [
            window_info(
                (self.stage + 1) * 1000 + index,
                text=text,
                class_name=class_name,
                control_id=control_id,
                parent=parent,
            )
            for index, (text, class_name, control_id) in enumerate(definitions[self.stage], start=1)
        ]

    def top_windows(self, process_id: int | None = None) -> list[WindowInfo]:
        if self.stage >= 5:
            return []
        title = "Save As" if self.stage == 2 else ""
        return [window_info(100 + self.stage, text=title)]

    def children(self, parent: int) -> list[WindowInfo]:
        return self._controls()

    def button(self, parent: int, text: str) -> WindowInfo:
        matches = [item for item in self._controls() if item.class_name == "Button" and item.text == text]
        if len(matches) != 1:
            raise RosterExportError(f"Button ontbreekt: {text}")
        return matches[0]

    def control_by_id(self, parent: int, control_id: int) -> WindowInfo:
        matches = [item for item in self._controls() if item.control_id == control_id]
        if len(matches) != 1:
            raise RosterExportError(f"Control ontbreekt: {control_id}")
        return matches[0]

    def set_text(self, handle: int, value: str) -> None:
        self.filename = value

    def click(self, handle: int) -> None:
        control = next(item for item in self._controls() if item.handle == handle)
        self.clicked.append(control.text)
        self.stage += 1

    def wait_not_visible(self, handle: int, *, timeout_seconds: float = 5.0) -> None:
        if self.stage < 5 and handle == 100 + self.stage:
            raise RosterExportError("Dialoog bleef zichtbaar.")


class CleanupFlowUI:
    def __init__(self) -> None:
        self.report_open = True
        self.phase = "special"
        self.clicked: list[str] = []
        self.closed: list[int] = []

    def top_windows(self, process_id: int | None = None) -> list[WindowInfo]:
        main = window_info(1, text="Gym Assistant", class_name="GymAssistant26Task")
        if self.phase == "special":
            return [main, window_info(2)]
        if self.phase == "about":
            return [main, window_info(3, text="About Gym Assistant")]
        return [main]

    def children(self, parent: int) -> list[WindowInfo]:
        if parent == 1:
            if not self.report_open:
                return []
            return [
                window_info(
                    10,
                    text="Membership List [Dreamz Fitness Bonaire]",
                    class_name="xGym Assistant1220doc9012",
                    parent=1,
                )
            ]
        if parent == 2:
            return [
                window_info(20, text="Special Commands", class_name="Static", parent=2),
                window_info(21, text="Cancel", class_name="Button", parent=2),
            ]
        if parent == 3:
            return [
                window_info(30, text="Gym Assistant", class_name="Static", parent=3),
                window_info(31, text="Deluxe 5,000 Edition / 5 workstations", class_name="Static", parent=3),
                window_info(32, text="Revision Info...", class_name="Button", parent=3),
                window_info(33, text="OK", class_name="Button", parent=3),
            ]
        return []

    def button(self, parent: int, text: str) -> WindowInfo:
        matches = [item for item in self.children(parent) if item.class_name == "Button" and item.text == text]
        if len(matches) != 1:
            raise RosterExportError(f"Button ontbreekt: {text}")
        return matches[0]

    def click(self, handle: int) -> None:
        if handle == 21:
            self.clicked.append("Cancel")
            self.phase = "about"
        elif handle == 33:
            self.clicked.append("OK")
            self.phase = "done"
        else:
            raise RosterExportError(f"Onverwachte klik: {handle}")

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        if handle == 10:
            self.report_open = False

    def wait_not_visible(self, handle: int, *, timeout_seconds: float = 5.0) -> None:
        visible_handles = {window.handle for window in self.top_windows()}
        if handle in visible_handles:
            raise RosterExportError("Dialoog bleef zichtbaar.")


class ModernConfirmUI:
    def __init__(self) -> None:
        self.visible = True
        self.legacy_clicks: list[int] = []

    def button(self, parent: int, text: str) -> WindowInfo:
        if parent != 70 or text not in {"Yes", "No"}:
            raise RosterExportError(f"Button ontbreekt: {text}")
        return window_info(
            71 if text == "Yes" else 72,
            text=f"&{text}",
            class_name="Button",
            parent=70,
        )

    def click(self, handle: int) -> None:
        self.legacy_clicks.append(handle)

    def wait_not_visible(self, handle: int, *, timeout_seconds: float = 5.0) -> None:
        if self.visible:
            raise RosterExportError("Dialoog bleef zichtbaar.")


class GymAssistantRosterExportTests(unittest.TestCase):
    def test_export_prompt_classifier_recognizes_observed_dialogs(self) -> None:
        candidate = Path(r"C:\DreamzPortalSync\exports\pending\MemberData.csv")
        overwrite = window_info(1)
        overwrite_children = [
            window_info(
                2,
                text="MemberData.csv already exists. Do you want to replace it?",
                class_name="Static",
                parent=1,
            )
        ]
        internal_overwrite = window_info(11)
        internal_overwrite_children = [
            window_info(
                12,
                text="MemData.dat bestaat al. Wilt u het overschrijven?",
                class_name="Static",
                parent=11,
            )
        ]
        confirm_save_as = window_info(13, text="Confirm Save As")
        confirm_save_as_children = [
            window_info(14, text="&Yes", class_name="Button", parent=13),
            window_info(15, text="&No", class_name="Button", parent=13),
        ]
        success = window_info(3)
        success_children = [
            window_info(
                4,
                text="5,027 member records exported.",
                class_name="Static",
                parent=3,
            )
        ]
        unrelated = window_info(5)
        unrelated_children = [
            window_info(
                6,
                text="unrelated.dat bestaat al. Wilt u het overschrijven?",
                class_name="Static",
                parent=5,
            )
        ]
        special = window_info(7)
        special_children = [
            window_info(8, text="Special Commands", class_name="Static", parent=7),
            window_info(9, text="BankInfo Editor", class_name="ListBox", parent=7),
            window_info(10, text="Cancel", class_name="Button", parent=7),
        ]

        self.assertEqual(_classify_export_dialog(overwrite, overwrite_children, candidate), "overwrite")
        self.assertEqual(
            _classify_export_dialog(internal_overwrite, internal_overwrite_children, candidate),
            "overwrite",
        )
        self.assertEqual(
            _classify_export_dialog(confirm_save_as, confirm_save_as_children, candidate),
            "overwrite",
        )
        self.assertEqual(_classify_export_dialog(success, success_children, candidate), "success")
        self.assertEqual(_classify_export_dialog(special, special_children, candidate), "special_commands")
        self.assertEqual(_exported_record_count("5,027 member records exported."), 5027)
        self.assertIsNone(_classify_export_dialog(unrelated, unrelated_children, candidate))

    def test_export_prompts_wait_for_overwrite_and_explicit_success(self) -> None:
        ui = PromptFlowUI()
        candidate = Path(r"C:\DreamzPortalSync\exports\pending\MemberData.csv")
        exporter = GymAssistantExporter(
            executable=Path(r"C:\Gym Assistant 2.6\Gym Assistant 26.exe"),
            expected_data_path=r"C:\Gym Assistant 2.6\Data",
            candidate_path=candidate,
            credential_path=Path(r"C:\state\master-access.dpapi"),
            ui_timeout_seconds=1.0,
            ui=ui,  # type: ignore[arg-type]
        )

        with patch("scripts.gymassistant_roster_export.roster_export.time.sleep"):
            count = exporter._answer_export_prompts(42)

        self.assertEqual(count, 5027)
        self.assertEqual(ui.filename, str(candidate))
        self.assertEqual(ui.clicked, ["CSV", "Yes", "Save", "Yes", "Close"])

    def test_confirm_save_as_uses_accessibility_without_legacy_click(self) -> None:
        ui = ModernConfirmUI()
        invocations: list[tuple[int, int, str]] = []

        def invoke(dialog: WindowInfo, button: WindowInfo, label: str) -> None:
            invocations.append((dialog.handle, button.handle, label))
            ui.visible = False

        exporter = GymAssistantExporter(
            executable=Path(r"C:\Gym Assistant 2.6\Gym Assistant 26.exe"),
            expected_data_path=r"C:\Gym Assistant 2.6\Data",
            candidate_path=Path(r"C:\DreamzPortalSync\exports\pending\MemberData.csv"),
            credential_path=Path(r"C:\state\master-access.dpapi"),
            ui_timeout_seconds=1.0,
            ui=ui,  # type: ignore[arg-type]
            accessible_button_invoker=invoke,
        )

        exporter._click_modal_button(window_info(70, text="Confirm Save As"), "Yes")

        self.assertEqual(invocations, [(70, 71, "Yes")])
        self.assertEqual(ui.legacy_clicks, [])

    def test_accessible_invoker_requires_auditable_success_response(self) -> None:
        dialog = window_info(70, text="Confirm Save As")
        button = window_info(72, text="&No", class_name="Button", parent=70)
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"status":"invoked","process_id":42,"window_handle":70,'
                '"button_handle":72,"button":"No","accessible_role":43,'
                '"method":"MSAA.accDoDefaultAction"}\n'
            ),
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "scripts.gymassistant_roster_export.roster_export.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            helper = Path(directory) / "Invoke-GymAssistantDialogButton.ps1"
            helper.write_text("# test helper", encoding="ascii")
            invoke_accessible_dialog_button(dialog, button, "No", helper_path=helper)

        command = run.call_args.args[0]
        self.assertIn("-ExpectedProcessId", command)
        self.assertIn("42", command)
        self.assertIn("-WindowHandle", command)
        self.assertIn("70", command)
        self.assertIn("-ButtonHandle", command)
        self.assertIn("72", command)
        self.assertIn("-ExpectedTitle", command)
        self.assertIn("Confirm Save As", command)
        self.assertIn("-ButtonLabel", command)
        self.assertIn("No", command)

    def test_accessible_invoker_rejects_missing_confirmation(self) -> None:
        dialog = window_info(70, text="Confirm Save As")
        button = window_info(72, text="&No", class_name="Button", parent=70)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "scripts.gymassistant_roster_export.roster_export.subprocess.run",
                return_value=completed,
            ),
        ):
            helper = Path(directory) / "Invoke-GymAssistantDialogButton.ps1"
            helper.write_text("# test helper", encoding="ascii")
            with self.assertRaisesRegex(RosterExportError, "geen controleerbaar resultaat"):
                invoke_accessible_dialog_button(dialog, button, "No", helper_path=helper)

    def test_accessible_invoker_rejects_wrong_button_handle_confirmation(self) -> None:
        dialog = window_info(70, text="Confirm Save As")
        button = window_info(72, text="&No", class_name="Button", parent=70)
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"status":"invoked","process_id":42,"window_handle":70,'
                '"button_handle":73,"button":"No","accessible_role":43,'
                '"method":"MSAA.accDoDefaultAction"}\n'
            ),
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "scripts.gymassistant_roster_export.roster_export.subprocess.run",
                return_value=completed,
            ),
        ):
            helper = Path(directory) / "Invoke-GymAssistantDialogButton.ps1"
            helper.write_text("# test helper", encoding="ascii")
            with self.assertRaisesRegex(RosterExportError, "bevestigde knop"):
                invoke_accessible_dialog_button(dialog, button, "No", helper_path=helper)

    def test_cleanup_returns_through_special_commands_and_about_dialog(self) -> None:
        ui = CleanupFlowUI()
        exporter = GymAssistantExporter(
            executable=Path(r"C:\Gym Assistant 2.6\Gym Assistant 26.exe"),
            expected_data_path=r"C:\Gym Assistant 2.6\Data",
            candidate_path=Path(r"C:\DreamzPortalSync\exports\pending\MemberData.csv"),
            credential_path=Path(r"C:\state\master-access.dpapi"),
            ui_timeout_seconds=1.0,
            ui=ui,  # type: ignore[arg-type]
        )
        clock = iter(range(100, 140))

        with (
            patch("scripts.gymassistant_roster_export.roster_export.time.monotonic", side_effect=clock),
            patch("scripts.gymassistant_roster_export.roster_export.time.sleep"),
        ):
            exporter._cleanup_export_windows(
                window_info(1, text="Gym Assistant", class_name="GymAssistant26Task")
            )

        self.assertIn(10, ui.closed)
        self.assertEqual(ui.clicked, ["Cancel", "OK"])
        self.assertEqual(ui.phase, "done")

    def test_valid_candidate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.csv"
            write_csv(path, 4)
            report = validate_candidate(path, policy=ValidationPolicy(minimum_members=4))
            self.assertTrue(report.ok)
            self.assertEqual(report.member_count, 4)
            self.assertEqual(report.critical_issue_count, 0)

    def test_missing_status_rejects_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.csv"
            write_csv(path, 4, status="")
            report = validate_candidate(path, policy=ValidationPolicy(minimum_members=4))
            self.assertFalse(report.ok)
            self.assertEqual(report.critical_issue_count, 4)

    def test_population_jump_rejects_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "MemberData.csv"
            candidate = root / "pending.csv"
            write_csv(previous, 10)
            write_csv(candidate, 6)
            report = validate_candidate(
                candidate,
                previous=previous,
                policy=ValidationPolicy(minimum_members=1, max_count_change_percent=15),
            )
            self.assertFalse(report.ok)
            self.assertAlmostEqual(report.count_change_percent or 0, 40.0)

    def test_valid_candidate_is_backed_up_and_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "MemberData.csv"
            candidate = root / "MemberData.pending.csv"
            state = root / "state" / "last-run.json"
            history = root / "history"
            write_csv(target, 4, start=1)
            write_csv(candidate, 4, start=10)

            result = promote_candidate(
                candidate,
                target,
                backup_dir=history,
                state_path=state,
                policy=ValidationPolicy(minimum_members=4),
            )

            self.assertFalse(candidate.exists())
            self.assertTrue(target.exists())
            self.assertIsNotNone(result.backup)
            self.assertTrue(Path(result.backup or "").is_file())
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "published")
            self.assertEqual(payload["validation"]["member_count"], 4)
            self.assertNotIn("First10", state.read_text(encoding="utf-8"))

    def test_rejected_candidate_does_not_replace_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "MemberData.csv"
            candidate = root / "MemberData.pending.csv"
            state = root / "last-run.json"
            write_csv(target, 4)
            original = target.read_bytes()
            write_csv(candidate, 1)

            with self.assertRaises(RosterExportError):
                promote_candidate(
                    candidate,
                    target,
                    backup_dir=root / "history",
                    state_path=state,
                    policy=ValidationPolicy(minimum_members=4),
                )

            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(candidate.exists())
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["status"], "rejected")

    def test_candidate_changed_after_validation_does_not_replace_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "MemberData.csv"
            candidate = root / "MemberData.pending.csv"
            state = root / "last-run.json"
            history = root / "history"
            write_csv(target, 4, start=1)
            write_csv(candidate, 4, start=10)
            original = target.read_bytes()

            original_copy2 = __import__("shutil").copy2
            mutated = False

            def mutate_after_backup(source, destination, *args, **kwargs):
                nonlocal mutated
                result = original_copy2(source, destination, *args, **kwargs)
                if not mutated:
                    with candidate.open("ab") as handle:
                        handle.write(b"changed-after-validation\n")
                    mutated = True
                return result

            with patch(
                "scripts.gymassistant_roster_export.roster_export.shutil.copy2",
                side_effect=mutate_after_backup,
            ):
                with self.assertRaisesRegex(RosterExportError, "wijzigde na validatie"):
                    promote_candidate(
                        candidate,
                        target,
                        backup_dir=history,
                        state_path=state,
                        policy=ValidationPolicy(minimum_members=4),
                    )

            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(candidate.exists())

    def test_bridge_pause_requires_fresh_confirmation_and_restores_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enabled = root / "enabled.flag"
            paused_marker = root / "enabled.flag.roster-export-paused"
            state = root / "agent-state.json"
            enabled.write_text("enabled\n", encoding="utf-8")
            state.write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )

            def acknowledge_pause() -> None:
                deadline = time.monotonic() + 2.0
                while enabled.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                time.sleep(0.1)
                temporary = state.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        {
                            "status": "paused",
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )
                temporary.replace(state)

            bridge = threading.Thread(target=acknowledge_pause, daemon=True)
            bridge.start()
            with pause_signup_bridge(root, timeout_seconds=3.0):
                self.assertFalse(enabled.exists())
                self.assertTrue(paused_marker.exists())
            bridge.join(timeout=2.0)

            self.assertFalse(bridge.is_alive())
            self.assertTrue(enabled.exists())
            self.assertFalse(paused_marker.exists())

    def test_bridge_pause_restores_enabled_marker_when_status_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enabled = root / "enabled.flag"
            enabled.write_text("enabled\n", encoding="utf-8")

            with self.assertRaises(RosterExportError):
                with pause_signup_bridge(root, timeout_seconds=0.1):
                    self.fail("Export mocht niet starten zonder bridge-status.")

            self.assertTrue(enabled.exists())
            self.assertFalse((root / "enabled.flag.roster-export-paused").exists())

    def test_export_failure_replaces_stale_success_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state" / "last-run.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"status": "published"}), encoding="utf-8")
            args = SimpleNamespace(
                candidate=str(root / "MemberData.pending.csv"),
                target=str(root / "MemberData.csv"),
                state_path=str(state),
                backup_dir=str(root / "history"),
                minimum_members=1,
                max_count_change_percent=15.0,
                bridge_work_root=None,
                bridge_timeout_seconds=0.1,
                gymassistant_exe=str(root / "Gym Assistant 26.exe"),
                expected_data_path=str(root / "Data"),
                credential_path=str(root / "credential.dpapi"),
                ui_timeout_seconds=0.1,
                manual_auth=False,
            )

            with patch(
                "scripts.gymassistant_roster_export.roster_export.GymAssistantExporter.run",
                side_effect=RosterExportError("UI-export testfout"),
            ):
                with self.assertRaises(RosterExportError):
                    run_export(args)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["error"], "UI-export testfout")
            self.assertNotIn("members", payload)

    def test_unexpected_published_target_change_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "MemberData.csv"
            candidate = root / "pending" / "MemberData.csv"
            state = root / "state" / "last-run.json"
            history = root / "history"
            write_csv(target, 4, start=1)
            original = target.read_bytes()
            args = SimpleNamespace(
                candidate=str(candidate),
                target=str(target),
                state_path=str(state),
                backup_dir=str(history),
                minimum_members=1,
                max_count_change_percent=15.0,
                bridge_work_root=None,
                bridge_timeout_seconds=0.1,
                gymassistant_exe=str(root / "Gym Assistant 26.exe"),
                expected_data_path=str(root / "Data"),
                credential_path=str(root / "credential.dpapi"),
                ui_timeout_seconds=0.1,
                manual_auth=False,
            )

            def mutate_published_target() -> dict:
                candidate.parent.mkdir(parents=True)
                write_csv(candidate, 4, start=10)
                write_csv(target, 4, start=100)
                return {"status": "exported", "candidate": str(candidate)}

            with patch(
                "scripts.gymassistant_roster_export.roster_export.GymAssistantExporter.run",
                side_effect=mutate_published_target,
            ):
                with self.assertRaisesRegex(RosterExportError, "onverwacht"):
                    run_export(args)

            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(candidate.exists())
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertIn("oorspronkelijke lijst is hersteld", payload["error"])
            self.assertEqual(list(history.glob("*.pre-export")), [])

    def test_run_export_publishes_candidate_with_target_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "MemberData.csv"
            candidate = root / "pending" / "MemberData.csv"
            state = root / "state" / "last-run.json"
            history = root / "history"
            write_csv(target, 4, start=1)
            args = SimpleNamespace(
                candidate=str(candidate),
                target=str(target),
                state_path=str(state),
                backup_dir=str(history),
                minimum_members=4,
                max_count_change_percent=15.0,
                bridge_work_root=None,
                bridge_timeout_seconds=0.1,
                gymassistant_exe=str(root / "Gym Assistant 26.exe"),
                expected_data_path=str(root / "Data"),
                credential_path=str(root / "credential.dpapi"),
                ui_timeout_seconds=0.1,
                manual_auth=False,
            )

            def create_candidate() -> dict:
                candidate.parent.mkdir(parents=True)
                write_csv(candidate, 4, start=10)
                return {"status": "exported", "candidate": str(candidate)}

            with patch(
                "scripts.gymassistant_roster_export.roster_export.GymAssistantExporter.run",
                side_effect=create_candidate,
            ):
                result = run_export(args)

            self.assertEqual(result["status"], "published")
            self.assertFalse(candidate.exists())
            self.assertIn(b"First10", target.read_bytes())
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "published")
            self.assertEqual(payload["validation"]["member_count"], 4)
            self.assertEqual(list(history.glob("*.pre-export")), [])

    def test_interrupted_export_records_state_and_removes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "MemberData.pending.csv"
            candidate.write_text("partial", encoding="utf-8")
            state = root / "last-run.json"
            args = SimpleNamespace(
                candidate=str(candidate),
                target=str(root / "MemberData.csv"),
                state_path=str(state),
                backup_dir=str(root / "history"),
                minimum_members=1,
                max_count_change_percent=15.0,
                bridge_work_root=None,
                bridge_timeout_seconds=0.1,
                gymassistant_exe=str(root / "Gym Assistant 26.exe"),
                expected_data_path=str(root / "Data"),
                credential_path=str(root / "credential.dpapi"),
                ui_timeout_seconds=0.1,
                manual_auth=False,
            )

            with patch(
                "scripts.gymassistant_roster_export.roster_export.GymAssistantExporter.run",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_export(args)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "interrupted")
            self.assertFalse(candidate.exists())


if __name__ == "__main__":
    unittest.main()
