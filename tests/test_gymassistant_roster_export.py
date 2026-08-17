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
    RosterExportError,
    ValidationPolicy,
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


class GymAssistantRosterExportTests(unittest.TestCase):
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
