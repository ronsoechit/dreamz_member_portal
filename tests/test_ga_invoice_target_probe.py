from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import ga_invoice_target_probe as target_probe


TARGET = (
    "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
)
OTHER = (
    "c20260215!1559 7002 1771185540 0 990002 99999 38 0 0 0 29"
    "|200 ProShop purchase"
)
CATALOG = "\n".join(
    [
        "CLASS=contract Dreamz test",
        "MEMBERTYPE_ID=900000101",
        "OPTION=1 MONTHS EFT 6500 0",
        "-",
    ]
)


def _allowlist_hash(*values: str) -> str:
    return sha256("\n".join(sorted(values, key=int)).encode("ascii")).hexdigest()


def _write_backup(path: Path, journal: str | bytes, catalog: str = CATALOG) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Journal.jtx", journal)
        archive.writestr("Members.btx", catalog)


class InvoiceTargetProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "GABackup-test.gbu"
        self.allowlist = self.root / "allowlist.txt"
        self.allowlist.write_text("90001\n", encoding="ascii")
        _write_backup(self.backup, TARGET)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_probe(self, **overrides) -> dict:
        values = {
            "backup_path": self.backup,
            "expected_length": self.backup.stat().st_size,
            "expected_sha256": sha256(self.backup.read_bytes()).hexdigest(),
            "allowlist_path": self.allowlist,
            "expected_allowlist_count": 1,
            "expected_allowlist_set_sha256": _allowlist_hash("90001"),
            "expected_target_event_count": 1,
            "expected_short_fragment_count": 0,
        }
        values.update(overrides)
        return target_probe.probe_target_invoice_readiness(**values)

    def test_target_scope_ignores_proven_non_target_legacy_noise(self):
        zero_full = (
            "c20260215!1600 7003 1771185600 0 0 0 43 0 0 0 29|system"
        )
        zero_header_only = (
            "c20260215!1601 7004 1771185660 0 0 0 43 0 0 0 29"
        )
        non_target_malformed = (
            "c20260215!1602 7005 NOTDECIMAL 0 990005 99999 38 0 0 0 29|x"
        )
        journal = "\n".join(
            [
                zero_full,
                zero_header_only,
                "A!",
                "B C",
                "D-E",
                non_target_malformed,
                TARGET,
            ]
        )
        _write_backup(self.backup, journal)

        result = self.run_probe(expected_short_fragment_count=3)

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["target_scope_proven"])
        self.assertTrue(result["ready_for_target_sync"])
        self.assertFalse(result["authorizes_invoice_issuing"])
        self.assertEqual(result["analysis"]["target_membership_event_count"], 1)
        self.assertEqual(result["analysis"]["target_issue_count"], 0)
        self.assertEqual(result["analysis"]["ignored_short_fragment_count"], 3)

    def test_target_nonmembership_empty_payload_is_out_of_invoice_scope(self):
        nonmembership = (
            "c20260215!1600 7003 1771185600 0 990003 90001 43 0 0 0 29|"
        )
        _write_backup(self.backup, "\n".join([nonmembership, TARGET]))

        result = self.run_probe()

        self.assertTrue(result["ready_for_target_sync"])
        self.assertEqual(result["analysis"]["target_membership_event_count"], 1)

    def test_embedded_target_after_non_target_prefix_blocks(self):
        _write_backup(self.backup, OTHER + TARGET)

        result = self.run_probe()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(result["analysis"]["embedded_target_candidate_count"], 0)
        self.assertIn("target_membership_parse_issue", result["reason_codes"])

    def test_malformed_embedded_target_after_non_target_prefix_blocks(self):
        malformed = TARGET.replace("c20260215!1558", "bad-timestamp")
        _write_backup(self.backup, "\r\n".join([TARGET, OTHER + malformed]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertFalse(result["ready_for_target_sync"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_shifted_target_member_event_pair_blocks(self):
        shifted = (
            "c20260215!1602 7005 1771185720 0 90001 3 0 0 0 29 0"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        _write_backup(self.backup, "\r\n".join([TARGET, shifted]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_embedded_target_with_trailing_header_tokens_blocks(self):
        malformed = TARGET.replace(
            "c20260215!1558",
            "bad-timestamp",
        ).replace(
            " 29|",
            " 29 10 11 12 13 14 15 16 17 18 19|",
        )
        _write_backup(self.backup, "\r\n".join([TARGET, OTHER + malformed]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_embedded_shifted_target_with_nonnumeric_event_blocks(self):
        shifted = (
            "bad-timestamp 7001 1771185480 0 90001 BAD 0 0 0 29 0"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        _write_backup(self.backup, "\r\n".join([TARGET, OTHER + shifted]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_embedded_truncated_target_header_blocks(self):
        truncated = "bad-ts 7001 0 0 90001 BAD|x"
        _write_backup(self.backup, "\r\n".join([TARGET, OTHER + truncated]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_every_short_split_of_target_event_blocks(self):
        raw = TARGET.encode("ascii")
        for offset in range(1, len(raw)):
            if min(offset, len(raw) - offset) > 15:
                continue
            for separator in (b"\n", b"\r", b"\r\n"):
                with self.subTest(offset=offset, separator=separator):
                    _write_backup(
                        self.backup,
                        raw[:offset] + separator + raw[offset:],
                    )
                    result = self.run_probe()
                    self.assertFalse(result["target_scope_proven"])
                    self.assertFalse(result["ready_for_target_sync"])

    def test_target_header_split_across_fragment_cluster_blocks(self):
        left = b"c20260215!1558 7001 1771185480 0"
        fragment = b"990001"
        right = (
            b"90001 3 0 0 0 29|900000101 257 20260201 20260301 "
            b"6500 0 0 6500 0 0 0"
        )
        _write_backup(self.backup, b"\r\n".join([left, fragment, right]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["cross_boundary_target_candidate_count"],
            0,
        )

    def test_short_fragment_cap_and_expected_count_are_bound(self):
        fragments = [f"X{index}" for index in range(9)]
        _write_backup(self.backup, "\n".join([*fragments, TARGET]))

        result = self.run_probe(expected_short_fragment_count=8)

        self.assertFalse(result["target_scope_proven"])
        self.assertIn("target_event_limit_exceeded", result["reason_codes"])

    def test_exact_target_event_count_is_required(self):
        result = self.run_probe(expected_target_event_count=2)

        self.assertFalse(result["target_scope_proven"])
        self.assertIn("target_event_count_mismatch", result["reason_codes"])

    def test_every_allowlisted_member_must_have_an_event(self):
        self.allowlist.write_text("90001\n90002\n", encoding="ascii")

        result = self.run_probe(
            expected_allowlist_count=2,
            expected_allowlist_set_sha256=_allowlist_hash("90001", "90002"),
        )

        self.assertFalse(result["target_scope_proven"])
        self.assertIn(
            "target_member_coverage_incomplete",
            result["reason_codes"],
        )

    def test_noncanonical_leading_zero_target_member_blocks(self):
        malformed = TARGET.replace(" 90001 3 ", " 090001 3 ")
        _write_backup(self.backup, malformed)

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(
            result["analysis"]["ambiguous_target_candidate_count"],
            0,
        )

    def test_malformed_target_header_blocks(self):
        malformed = (
            "bad-timestamp 7001 1771185480 0 990001 90001 3 0 0 0 29"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        _write_backup(self.backup, "\n".join([TARGET, malformed]))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])
        self.assertGreater(result["analysis"]["target_issue_count"], 0)

    def test_non_crlf_separator_blocks(self):
        header, payload = TARGET.encode("ascii").split(b"|", 1)
        _write_backup(self.backup, header + b"|" + payload.replace(b" ", b"\x0b", 1))

        result = self.run_probe()

        self.assertFalse(result["target_scope_proven"])

    def test_cli_output_is_one_private_json_line(self):
        stdout = StringIO()
        stderr = StringIO()
        argv = [
            "--backup-path",
            str(self.backup),
            "--expected-length",
            str(self.backup.stat().st_size),
            "--expected-sha256",
            sha256(self.backup.read_bytes()).hexdigest(),
            "--allowlist-path",
            str(self.allowlist),
            "--expected-allowlist-count",
            "1",
            "--expected-allowlist-set-sha256",
            _allowlist_hash("90001"),
            "--expected-target-event-count",
            "1",
            "--expected-short-fragment-count",
            "0",
        ]

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = target_probe.main(argv)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        output = stdout.getvalue()
        self.assertNotIn("90001", output)
        self.assertNotIn("6500", output)
        self.assertNotIn(str(self.backup), output)
        parsed = json.loads(output)
        self.assertTrue(parsed["ready_for_target_sync"])

    def test_direct_cli_does_not_create_bytecode(self):
        isolated = self.root / "isolated"
        isolated.mkdir()
        module_root = Path(target_probe.__file__).resolve().parent
        for name in (
            "ga_invoice_target_probe.py",
            "ga_invoice_backup_probe.py",
            "ga_journal.py",
        ):
            shutil.copyfile(module_root / name, isolated / name)
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)

        completed = subprocess.run(
            [sys.executable, "ga_invoice_target_probe.py", "--help"],
            cwd=isolated,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse((isolated / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
