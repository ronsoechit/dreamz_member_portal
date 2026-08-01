from __future__ import annotations

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


def journal_row(
    *,
    member_number: str = "13659",
    payload: str = "synthetic-payload",
) -> bytes:
    return (
        "c20260731!1234 7001 1771185480 0 990001 "
        f"{member_number} 77 0 0 0 29|{payload}"
    ).encode("latin-1")


class JournalSourceCoverageProbeTests(unittest.TestCase):
    def make_fixture(self, temporary_root: str, data: bytes) -> tuple[Path, Path]:
        install_root = Path(temporary_root) / "Gym Assistant 2.6"
        data_root = install_root / "Data"
        data_root.mkdir(parents=True)
        journal = data_root / "Journal.jtx"
        journal.write_bytes(data)
        return data_root, journal

    def expected_fixture_fingerprint(self, data_root: Path) -> str:
        return probe._path_fingerprint(data_root)

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
                self.expected_fixture_fingerprint(data_root),
            ),
        ):
            return probe.probe_journal_source_coverage(data_root, **kwargs)

    def test_supported_live_source_passes_with_exact_classifier_and_binding(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(
                temporary_root,
                journal_row() + b"\r\n" + journal_row(member_number="22001"),
            )
            result = self.run_fixture_probe(data_root)

        self.assertEqual(result["schema"], probe.PROBE_SCHEMA)
        self.assertEqual(result["mode"], "read_only")
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["coverage_proven"])
        self.assertEqual(result["classifier_version"], probe.CLASSIFIER_VERSION)
        self.assertEqual(
            result["classifier_version"],
            "dreamz.ga.journal.member-lines.v1",
        )
        self.assertEqual(result["reason_codes"], [])
        self.assertEqual(result["source"]["stable_read_count"], 2)
        self.assertEqual(result["source"]["post_scan_confirmation_count"], 1)
        self.assertRegex(
            result["source"]["file_identity_sha256"],
            r"\A[a-f0-9]{64}\Z",
        )
        self.assertEqual(result["coverage"]["record_count"], 2)
        self.assertEqual(result["coverage"]["supported_record_count"], 2)
        self.assertEqual(result["coverage"]["malformed_record_count"], 0)
        self.assertEqual(result["coverage"]["invalid_member_field_count"], 0)
        self.assertEqual(result["coverage"]["tree_scan_stable_count"], 2)

    def test_malformed_record_and_member_field_fail_closed(self):
        malformed = (
            b"c20260731!1234 7001 1771185480 0 990001 NOT-A-MEMBER "
            b"77 0 0 0 29|synthetic"
        )
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(
                temporary_root,
                journal_row() + b"\n" + malformed,
            )
            result = self.run_fixture_probe(data_root)

        self.assertFalse(result["coverage_proven"])
        self.assertIn("unsupported_record_grammar", result["reason_codes"])
        self.assertIn("member_number_field_unproven", result["reason_codes"])
        self.assertEqual(result["coverage"]["record_count"], 2)
        self.assertEqual(result["coverage"]["supported_record_count"], 1)
        self.assertEqual(result["coverage"]["malformed_record_count"], 1)
        self.assertEqual(result["coverage"]["invalid_member_field_count"], 1)

    def test_possible_rotated_or_historical_segment_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(temporary_root, journal_row())
            archive = data_root / "Archive"
            archive.mkdir()
            (archive / "Journal-2025.jtx").write_bytes(journal_row())
            result = self.run_fixture_probe(data_root)

        self.assertFalse(result["coverage_proven"])
        self.assertEqual(
            result["coverage"]["historical_segment_candidate_count"],
            1,
        )
        self.assertIn(
            "possible_historical_journal_segments",
            result["reason_codes"],
        )
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("Journal-2025.jtx", rendered)
        self.assertNotIn("Archive", rendered)

    def test_wrong_data_root_and_copied_source_binding_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            wrong_root = Path(temporary_root) / "Copied Gym Assistant" / "Data"
            wrong_root.mkdir(parents=True)
            (wrong_root / "Journal.jtx").write_bytes(journal_row())
            wrong_shape = probe.probe_journal_source_coverage(wrong_root)

            data_root, _journal = self.make_fixture(temporary_root, journal_row())
            with (
                patch.object(
                    probe,
                    "_production_data_root_lexically_exact",
                    return_value=True,
                ),
                patch.object(
                    probe,
                    "_read_stable_file_twice",
                    side_effect=AssertionError("copied source must remain unread"),
                ),
            ):
                copied_binding = probe.probe_journal_source_coverage(data_root)

        self.assertEqual(wrong_shape["reason_codes"], ["wrong_data_root"])
        self.assertIsNone(wrong_shape["source"])
        self.assertIn(
            "data_root_binding_mismatch",
            copied_binding["reason_codes"],
        )
        self.assertFalse(copied_binding["coverage_proven"])
        self.assertIsNone(copied_binding["source"])

    def test_missing_exact_live_source_never_falls_back_to_backup(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root = Path(temporary_root) / "Gym Assistant 2.6" / "Data"
            backup = data_root / "Backup"
            backup.mkdir(parents=True)
            (backup / "Journal.jtx").write_bytes(journal_row())
            result = self.run_fixture_probe(data_root)

        self.assertFalse(result["coverage_proven"])
        self.assertEqual(result["reason_codes"], ["source_missing"])
        self.assertIsNone(result["source"])

    def test_unstable_source_is_sanitized_and_blocks(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(temporary_root, journal_row())
            with patch.object(
                probe,
                "_read_stable_file_twice",
                side_effect=probe.ProbeSourceUnstableError(
                    "PRIVATE-MEMBER at C:\\private\\Journal.jtx"
                ),
            ):
                result = self.run_fixture_probe(data_root)

        self.assertEqual(result["reason_codes"], ["source_unstable"])
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("PRIVATE-MEMBER", rendered)
        self.assertNotIn("Journal.jtx", rendered)
        self.assertNotIn("private", rendered.casefold())

    def test_exact_read_detects_different_second_read(self):
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

    def test_exact_read_rejects_file_identity_replacement(self):
        first = probe._ExactRead(
            data=b"same",
            byte_length=4,
            mtime_ns=1,
            file_identity=(1, 2),
            file_identity_sha256="a" * 64,
        )
        replacement = probe._ExactRead(
            data=b"same",
            byte_length=4,
            mtime_ns=1,
            file_identity=(1, 3),
            file_identity_sha256="b" * 64,
        )
        with patch.object(
            probe,
            "_read_exact_file_once",
            side_effect=[first, replacement],
        ):
            with self.assertRaises(probe.ProbeSourceIdentityError):
                probe._read_stable_file_twice(Path("unused"))

    def test_empty_journal_is_not_coverage_proof(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(temporary_root, b"")
            result = self.run_fixture_probe(data_root)

        self.assertIn("journal_empty", result["reason_codes"])
        self.assertFalse(result["coverage_proven"])

    def test_output_contains_no_rows_payloads_or_absolute_paths(self):
        private_name = "VERY-PRIVATE-SYNTHETIC-PERSON"
        private_payload = f"{private_name}|USD 77.37|BANK-SYNTHETIC-ONLY"
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, _journal = self.make_fixture(
                temporary_root,
                journal_row(payload=private_payload),
            )
            result = self.run_fixture_probe(data_root)
            rendered = json.dumps(result, sort_keys=True)

        for forbidden in (
            private_name,
            "USD 77.37",
            "BANK-SYNTHETIC-ONLY",
            str(data_root),
            "Journal.jtx",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_cli_stdout_is_machine_json_and_never_echoes_wrong_path(self):
        private_path = Path("C:/PRIVATE-MEMBER-COPY/Data")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = probe.main(["--data-root", str(private_path)])
        result = json.loads(output.getvalue())

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["mode"], "read_only")
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("PRIVATE-MEMBER-COPY", output.getvalue())

    def test_probe_source_contains_no_network_or_file_write_path(self):
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
                if node.func.attr == "open" and node.args:
                    mode = node.args[0]
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                        self.assertFalse(set(mode.value) & set("wax+"))

    def test_unc_mapped_and_traversal_paths_block_before_any_filesystem_io(self):
        unsafe_values = (
            r"\\server\share\Gym Assistant 2.6\Data",
            r"Z:\Gym Assistant 2.6\Data",
            r"C:\Gym Assistant 2.6\Elsewhere\..\Data",
        )
        for unsafe in unsafe_values:
            with self.subTest(unsafe=unsafe):
                with (
                    patch.object(
                        probe,
                        "_path_is_symlink_or_reparse_point",
                        side_effect=AssertionError(
                            "filesystem must remain untouched"
                        ),
                    ),
                    patch.object(
                        Path,
                        "resolve",
                        side_effect=AssertionError(
                            "filesystem must remain untouched"
                        ),
                    ),
                    patch.object(
                        probe.os,
                        "walk",
                        side_effect=AssertionError(
                            "filesystem must remain untouched"
                        ),
                    ),
                ):
                    result = probe.probe_journal_source_coverage(unsafe)
                self.assertEqual(result["reason_codes"], ["wrong_data_root"])
                self.assertIsNone(result["source"])

    def test_production_api_has_no_caller_controlled_source_binding(self):
        parameters = probe.probe_journal_source_coverage.__annotations__
        self.assertNotIn("expected_data_root_fingerprint_sha256", parameters)
        with self.assertRaises(TypeError):
            probe.probe_journal_source_coverage(
                Path("unused"),
                expected_data_root_fingerprint_sha256="a" * 64,
            )

    def test_change_during_second_tree_scan_is_detected_by_final_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            data_root, journal = self.make_fixture(temporary_root, journal_row())
            calls = 0

            def scan_then_change(_data_root, _live_journal):
                nonlocal calls
                calls += 1
                if calls == 2:
                    journal.write_bytes(journal.read_bytes() + b"\n" + journal_row())
                return probe._TreeScan(True, False, ())

            with patch.object(
                probe,
                "_scan_possible_historical_segments",
                side_effect=scan_then_change,
            ):
                result = self.run_fixture_probe(data_root)

        self.assertEqual(result["reason_codes"], ["source_unstable"])
        self.assertFalse(result["coverage_proven"])

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
            before = {
                path.relative_to(isolated).as_posix()
                for path in isolated.rglob("*")
            }
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
            after = {
                path.relative_to(isolated).as_posix()
                for path in isolated.rglob("*")
            }

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(before, after)
        self.assertNotIn("PRIVATE-SYNTHETIC", completed.stdout)
        self.assertEqual(json.loads(completed.stdout)["mode"], "read_only")

    def test_documentation_lists_every_fixed_reason_code(self):
        documentation = Path(probe.__file__).with_name(
            "EXISTING_MEMBER_JOURNAL_SOURCE_COVERAGE_PROBE.md"
        ).read_text(encoding="utf-8")
        for reason_code in probe.KNOWN_REASON_CODES:
            self.assertIn(f"`{reason_code}`", documentation)


if __name__ == "__main__":
    unittest.main()
