import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ga_journal_source_coverage_probe as probe


class JournalExportPreflightProbeTests(unittest.TestCase):
    def make_fixture(
        self,
        temporary_root: str,
        *,
        active_data: bytes = b"opaque-proprietary-binary",
        executable_data: bytes = b"synthetic-executable",
    ) -> tuple[Path, Path, Path]:
        install_root = Path(temporary_root) / "Gym Assistant 2.6"
        data_root = install_root / "Data"
        data_root.mkdir(parents=True)
        journal = data_root / "Journal.dat"
        journal.write_bytes(active_data)
        executable = install_root / "Gym Assistant 26.exe"
        executable.write_bytes(executable_data)
        return data_root, journal, executable

    def run_fixture_probe(self, data_root: Path, **kwargs) -> dict:
        with (
            patch.object(
                probe,
                "_production_data_root_lexically_exact",
                return_value=True,
            ),
            patch.object(
                probe,
                "EXPECTED_DATA_ROOT_FINGERPRINT_SHA256",
                probe._path_fingerprint(data_root),
            ),
        ):
            return probe.probe_journal_source_coverage(data_root, **kwargs)

    def test_binary_source_preflight_is_ready_but_never_claims_record_coverage(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(temporary_root)
            result = self.run_fixture_probe(data_root)

        self.assertEqual(result["schema"], probe.PROBE_SCHEMA)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["export_preflight_ready"])
        self.assertFalse(result["coverage_proven"])
        self.assertTrue(result["official_export_required"])
        self.assertEqual(
            result["classifier_version"],
            "dreamz.ga.journal.export-records.v2",
        )
        self.assertEqual(
            result["source"]["kind"],
            "gym_assistant_official_journal_export",
        )
        self.assertEqual(result["source"]["active_stable_read_count"], 2)
        self.assertEqual(result["source"]["executable_stable_read_count"], 2)
        self.assertEqual(result["reason_codes"], [])

    def test_arbitrary_binary_bytes_are_not_parsed_as_export_records(self):
        private_binary = b"\x00\xffPRIVATE-MEMBER\r\nnot-a-jtx-record\x85"
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(
                temporary_root,
                active_data=private_binary,
            )
            result = self.run_fixture_probe(data_root)

        self.assertTrue(result["export_preflight_ready"])
        self.assertNotIn("PRIVATE-MEMBER", json.dumps(result))

    def test_known_historical_segments_are_bound_inventory_not_blockers(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(temporary_root)
            (data_root / "Journal.tmp").write_bytes(b"temporary-segment")
            (data_root / "Journal_backup_1.dat").write_bytes(b"old-segment")
            backup = data_root.parent / "Backup"
            backup.mkdir()
            (backup / "Journal.jtx").write_bytes(b"official-old-export")
            result = self.run_fixture_probe(data_root)

        self.assertTrue(result["export_preflight_ready"])
        self.assertEqual(result["inventory"]["historical_segment_count"], 3)
        self.assertEqual(result["inventory"]["tree_scan_stable_count"], 2)

    def test_missing_active_source_never_falls_back_to_backup_export(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, journal, _executable = self.make_fixture(temporary_root)
            journal.unlink()
            backup = data_root.parent / "Backup"
            backup.mkdir()
            (backup / "Journal.jtx").write_bytes(b"stale-export")
            result = self.run_fixture_probe(data_root)

        self.assertFalse(result["export_preflight_ready"])
        self.assertEqual(result["reason_codes"], ["source_missing"])
        self.assertIsNone(result["source"])

    def test_missing_executable_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, executable = self.make_fixture(temporary_root)
            executable.unlink()
            result = self.run_fixture_probe(data_root)

        self.assertEqual(result["reason_codes"], ["executable_missing"])
        self.assertFalse(result["export_preflight_ready"])

    def test_empty_active_source_and_executable_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(
                temporary_root,
                active_data=b"",
                executable_data=b"",
            )
            result = self.run_fixture_probe(data_root)

        self.assertIn("active_journal_empty", result["reason_codes"])
        self.assertIn("executable_empty", result["reason_codes"])
        self.assertFalse(result["export_preflight_ready"])

    def test_unstable_source_error_is_sanitized(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(temporary_root)
            with patch.object(
                probe,
                "_read_stable_file_twice",
                side_effect=probe.ProbeSourceUnstableError(
                    r"PRIVATE-MEMBER C:\private\Journal.dat"
                ),
            ):
                result = self.run_fixture_probe(data_root)

        rendered = json.dumps(result, sort_keys=True)
        self.assertEqual(result["reason_codes"], ["source_unstable"])
        self.assertNotIn("PRIVATE-MEMBER", rendered)
        self.assertNotIn("Journal.dat", rendered)

    def test_two_different_reads_fail_closed(self):
        first = probe._ExactRead(
            data=b"first",
            byte_length=5,
            mtime_ns=1,
            file_identity=(1, 2),
            file_identity_sha256="a" * 64,
        )
        second = probe._ExactRead(
            data=b"second",
            byte_length=6,
            mtime_ns=2,
            file_identity=(1, 2),
            file_identity_sha256="a" * 64,
        )
        with patch.object(
            probe,
            "_read_exact_file_once",
            side_effect=[first, second, first, second],
        ):
            with self.assertRaises(probe.ProbeSourceUnstableError):
                probe._read_stable_file_twice(Path("unused"), max_attempts=2)

    def test_wrong_or_network_path_blocks_before_filesystem_io(self):
        unsafe_values = (
            r"\\server\share\Gym Assistant 2.6\Data",
            r"Z:\Gym Assistant 2.6\Data",
            r"C:\Gym Assistant 2.6\Elsewhere\..\Data",
        )
        for unsafe in unsafe_values:
            with self.subTest(unsafe=unsafe):
                with patch.object(
                    probe,
                    "_path_is_symlink_or_reparse_point",
                    side_effect=AssertionError("filesystem must remain untouched"),
                ):
                    result = probe.probe_journal_source_coverage(unsafe)
                self.assertEqual(result["reason_codes"], ["wrong_data_root"])

    def test_output_contains_no_rows_names_or_absolute_paths(self):
        private_name = "VERY-PRIVATE-SYNTHETIC-PERSON"
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal, _executable = self.make_fixture(
                temporary_root,
                active_data=private_name.encode("ascii"),
            )
            (data_root / f"Journal-{private_name}.tmp").write_bytes(
                b"PRIVATE-PAYLOAD"
            )
            result = self.run_fixture_probe(data_root)
            rendered = json.dumps(result, sort_keys=True)

        for forbidden in (
            private_name,
            "PRIVATE-PAYLOAD",
            str(data_root),
            "Journal.dat",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_cli_is_stdout_only_and_source_has_no_network_or_write_calls(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = probe.main(
                ["--data-root", "C:/PRIVATE-MEMBER-COPY/Data"]
            )
        self.assertEqual(exit_code, 2)
        self.assertNotIn("PRIVATE-MEMBER-COPY", output.getvalue())

        source = Path(probe.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_import_roots = {
            "ftplib",
            "http",
            "requests",
            "socket",
            "subprocess",
            "urllib",
        }
        forbidden_write_methods = {
            "chmod",
            "mkdir",
            "remove",
            "rename",
            "rmdir",
            "touch",
            "unlink",
            "write_bytes",
            "write_text",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name.split(".", 1)[0],
                        forbidden_import_roots,
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(
                    node.module.split(".", 1)[0],
                    forbidden_import_roots,
                )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_write_methods)

    def test_plain_python_cli_creates_no_bytecode_or_other_output_file(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            isolated = Path(temporary_root)
            probe_copy = isolated / "ga_journal_source_coverage_probe.py"
            classifier_copy = isolated / "ga_journal_snapshot.py"
            shutil.copyfile(Path(probe.__file__), probe_copy)
            shutil.copyfile(
                Path(probe.__file__).with_name("ga_journal_snapshot.py"),
                classifier_copy,
            )
            before = {path.relative_to(isolated).as_posix() for path in isolated.rglob("*")}
            completed = subprocess.run(
                [
                    sys.executable,
                    str(probe_copy),
                    "--data-root",
                    str(isolated / "PRIVATE-SYNTHETIC" / "Data"),
                ],
                cwd=isolated,
                check=False,
                capture_output=True,
                text=True,
            )
            after = {path.relative_to(isolated).as_posix() for path in isolated.rglob("*")}

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(before, after)
        self.assertNotIn("PRIVATE-SYNTHETIC", completed.stdout)

    def test_documentation_lists_every_fixed_reason_code(self):
        documentation = Path(probe.__file__).with_name(
            "EXISTING_MEMBER_JOURNAL_SOURCE_COVERAGE_PROBE.md"
        ).read_text(encoding="utf-8")
        for reason_code in probe.KNOWN_REASON_CODES:
            self.assertIn(f"`{reason_code}`", documentation)


if __name__ == "__main__":
    unittest.main()
