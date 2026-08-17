import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from ga_journal_snapshot import EXPORT_VERSION_RECORD
from gymassistant_journal_export import (
    GymAssistantJournalExporter,
    JournalExportError,
    export_official_journal_snapshot,
)


def journal_record(member_number: str = "90001") -> bytes:
    return (
        b"c20260817!1545 7001 1786995900 0 990001 "
        + member_number.encode("ascii")
        + b" 3 0 0 0 29|opaque"
    )


class OfficialJournalExportTests(unittest.TestCase):
    def paths(self, root: Path):
        source_root = root / "Gym Assistant 2.6"
        data_root = source_root / "Data"
        backup_root = source_root / "Backup"
        data_root.mkdir(parents=True)
        backup_root.mkdir()
        (data_root / "Journal.dat").write_bytes(b"current-binary-journal")
        (data_root / "Journal.tmp").write_bytes(b"historical-temp")
        (data_root / "Journal_backup_1.dat").write_bytes(b"historical-segment")
        (backup_root / "Journal.jtx").write_bytes(b"stale-export")
        executable = source_root / "GymAssistant.exe"
        executable.write_bytes(b"test-executable")
        credential = root / "master-access.dpapi"
        credential.write_bytes(b"test-credential")
        bridge = root / "bridge"
        bridge.mkdir()
        candidate = root / "journal-evidence" / "Journal.jtx"
        return source_root, executable, credential, bridge, candidate

    def run_snapshot(self, root: Path, *, exported_data: bytes):
        source_root, executable, credential, bridge, candidate = self.paths(root)

        def fake_export(exporter):
            exporter.candidate_path.parent.mkdir(parents=True, exist_ok=True)
            exporter.candidate_path.write_bytes(exported_data)
            return {"status": "exported"}

        with patch(
            "gymassistant_journal_export.GymAssistantJournalExporter.run",
            autospec=True,
            side_effect=fake_export,
        ):
            snapshot = export_official_journal_snapshot(
                source_root=source_root,
                executable=executable,
                expected_data_path=str(source_root / "Data"),
                candidate_path=candidate,
                credential_path=credential,
                bridge_work_root=bridge,
                source_coverage_proven=True,
                ui_timeout_seconds=1,
                bridge_timeout_seconds=1,
            )
        return snapshot, candidate

    def test_valid_export_is_returned_sanitized_and_temporary_file_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = b"\r\n".join([EXPORT_VERSION_RECORD, journal_record()])
            snapshot, candidate = self.run_snapshot(Path(temporary), exported_data=data)

        self.assertEqual(snapshot.data, data)
        self.assertTrue(snapshot.coverage.complete)
        self.assertEqual(snapshot.coverage.record_count, 2)
        self.assertFalse(candidate.exists())
        for value in (
            snapshot.locator_fingerprint_sha256,
            snapshot.data_path_fingerprint_sha256,
            snapshot.source_binding_sha256,
        ):
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_temporary_file_is_removed_before_shared_mutex_is_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, candidate = self.paths(root)
            events = []

            class RecordingMutex:
                def __enter__(self):
                    events.append(("enter", candidate.exists()))
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    events.append(("exit", candidate.exists()))

            def fake_export(exporter):
                exporter.candidate_path.write_bytes(
                    b"\r\n".join([EXPORT_VERSION_RECORD, journal_record()])
                )
                return {"status": "exported"}

            with (
                patch(
                    "gymassistant_journal_export.NamedMutex",
                    RecordingMutex,
                ),
                patch(
                    "gymassistant_journal_export.pause_signup_bridge",
                    return_value=nullcontext(),
                ),
                patch(
                    "gymassistant_journal_export.GymAssistantJournalExporter.run",
                    autospec=True,
                    side_effect=fake_export,
                ),
            ):
                export_official_journal_snapshot(
                    source_root=source_root,
                    executable=executable,
                    expected_data_path=str(source_root / "Data"),
                    candidate_path=candidate,
                    credential_path=credential,
                    bridge_work_root=bridge,
                    source_coverage_proven=True,
                    ui_timeout_seconds=1,
                    bridge_timeout_seconds=1,
                )

            self.assertEqual(events, [("enter", False), ("exit", False)])

    def test_invalid_export_fails_closed_and_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, candidate = self.paths(root)

            def fake_export(exporter):
                exporter.candidate_path.parent.mkdir(parents=True, exist_ok=True)
                exporter.candidate_path.write_bytes(b"not-an-official-export")
                return {"status": "exported"}

            with patch(
                "gymassistant_journal_export.GymAssistantJournalExporter.run",
                autospec=True,
                side_effect=fake_export,
            ):
                with self.assertRaisesRegex(
                    JournalExportError,
                    "journal_export_coverage_invalid",
                ):
                    export_official_journal_snapshot(
                        source_root=source_root,
                        executable=executable,
                        expected_data_path=str(source_root / "Data"),
                        candidate_path=candidate,
                        credential_path=credential,
                        bridge_work_root=bridge,
                        source_coverage_proven=True,
                        ui_timeout_seconds=1,
                        bridge_timeout_seconds=1,
                    )
            self.assertFalse(candidate.exists())

    def test_live_journal_append_during_export_keeps_official_snapshot_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, candidate = self.paths(root)

            def fake_export(exporter):
                exporter.candidate_path.parent.mkdir(parents=True, exist_ok=True)
                exporter.candidate_path.write_bytes(
                    b"\r\n".join([EXPORT_VERSION_RECORD, journal_record()])
                )
                with (source_root / "Data" / "Journal.dat").open("ab") as handle:
                    handle.write(b"-appended-during-export")
                return {"status": "exported"}

            with patch(
                "gymassistant_journal_export.GymAssistantJournalExporter.run",
                autospec=True,
                side_effect=fake_export,
            ):
                snapshot = export_official_journal_snapshot(
                    source_root=source_root,
                    executable=executable,
                    expected_data_path=str(source_root / "Data"),
                    candidate_path=candidate,
                    credential_path=credential,
                    bridge_work_root=bridge,
                    source_coverage_proven=True,
                    ui_timeout_seconds=1,
                    bridge_timeout_seconds=1,
                )

            self.assertTrue(snapshot.coverage.complete)

    def test_active_source_replacement_during_export_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, candidate = self.paths(root)

            def fake_export(exporter):
                exporter.candidate_path.parent.mkdir(parents=True, exist_ok=True)
                exporter.candidate_path.write_bytes(
                    b"\r\n".join([EXPORT_VERSION_RECORD, journal_record()])
                )
                active = source_root / "Data" / "Journal.dat"
                active.unlink()
                active.write_bytes(b"replacement-source")
                return {"status": "exported"}

            with patch(
                "gymassistant_journal_export.GymAssistantJournalExporter.run",
                autospec=True,
                side_effect=fake_export,
            ):
                with self.assertRaisesRegex(
                    JournalExportError,
                    "journal_source_binding_changed_during_export",
                ):
                    export_official_journal_snapshot(
                        source_root=source_root,
                        executable=executable,
                        expected_data_path=str(source_root / "Data"),
                        candidate_path=candidate,
                        credential_path=credential,
                        bridge_work_root=bridge,
                        source_coverage_proven=True,
                        ui_timeout_seconds=1,
                        bridge_timeout_seconds=1,
                    )

    def test_operator_coverage_gate_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_root, executable, credential, bridge, candidate = self.paths(
                Path(temporary)
            )
            with self.assertRaisesRegex(
                JournalExportError,
                "journal_source_coverage_unproven",
            ):
                export_official_journal_snapshot(
                    source_root=source_root,
                    executable=executable,
                    expected_data_path=str(source_root / "Data"),
                    candidate_path=candidate,
                    credential_path=credential,
                    bridge_work_root=bridge,
                    source_coverage_proven=False,
                )

    def test_journal_export_never_starts_gymassistant(self):
        exporter = object.__new__(GymAssistantJournalExporter)
        exporter.ui = object()
        exporter.expected_data_path = r"C:\Gym Assistant 2.6\Data"

        with patch(
            "gymassistant_journal_export._find_gymassistant_main",
            return_value=None,
        ) as finder:
            with self.assertRaisesRegex(
                JournalExportError,
                "journal_export_gymassistant_not_running",
            ):
                exporter._main_window()

        finder.assert_called_once_with(exporter.ui, exporter.expected_data_path)

    def test_candidate_inside_source_root_is_rejected_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, _candidate = self.paths(root)
            unsafe_candidate = source_root / "Data" / "Journal.pending.jtx"
            unsafe_candidate.write_bytes(b"must-not-be-deleted")
            original = unsafe_candidate.read_bytes()

            with self.assertRaisesRegex(
                JournalExportError,
                "journal_export_candidate_inside_source_root",
            ):
                export_official_journal_snapshot(
                    source_root=source_root,
                    executable=executable,
                    expected_data_path=str(source_root / "Data"),
                    candidate_path=unsafe_candidate,
                    credential_path=credential,
                    bridge_work_root=bridge,
                    source_coverage_proven=True,
                )

            self.assertEqual(unsafe_candidate.read_bytes(), original)

    def test_in_place_live_journal_rewrite_during_export_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, executable, credential, bridge, candidate = self.paths(root)

            def fake_export(exporter):
                exporter.candidate_path.write_bytes(
                    b"\r\n".join([EXPORT_VERSION_RECORD, journal_record()])
                )
                active = source_root / "Data" / "Journal.dat"
                active.write_bytes(b"rewritten-binary-journal")
                return {"status": "exported"}

            with patch(
                "gymassistant_journal_export.GymAssistantJournalExporter.run",
                autospec=True,
                side_effect=fake_export,
            ):
                with self.assertRaisesRegex(
                    JournalExportError,
                    "journal_source_binding_changed_during_export",
                ):
                    export_official_journal_snapshot(
                        source_root=source_root,
                        executable=executable,
                        expected_data_path=str(source_root / "Data"),
                        candidate_path=candidate,
                        credential_path=credential,
                        bridge_work_root=bridge,
                        source_coverage_proven=True,
                        ui_timeout_seconds=1,
                        bridge_timeout_seconds=1,
                    )


if __name__ == "__main__":
    unittest.main()
