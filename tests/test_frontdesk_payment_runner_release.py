from __future__ import annotations

import hashlib
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from frontdesk_payment_runner import preflight


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "frontdesk_payment_runner"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def canonical_sha256(path: Path) -> str:
    payload = (
        path.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest().upper()


def powershell_hashes(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {
        key: value
        for key, value in re.findall(
            r'^\s*"([^"]+)"\s*=\s*"([0-9A-F]{64})"\s*$',
            text,
            flags=re.MULTILINE,
        )
    }


class FrontdeskPreflightTests(unittest.TestCase):
    def test_preflight_requires_exact_host_and_user(self):
        with patch.object(
            preflight.scoped_runner,
            "native_computer_name",
            return_value="NOT-THE-FRONTDESK",
        ):
            result = preflight.run_preflight()
        self.assertEqual(result, {"ok": False, "reason": "wrong_computer"})

    def test_preflight_is_ready_only_after_all_read_only_checks(self):
        with (
            patch.object(
                preflight.scoped_runner,
                "native_computer_name",
                return_value="DREAMZ-FRNTDSK",
            ),
            patch.object(
                preflight.scoped_runner,
                "native_windows_user",
                return_value="Dreamz Fitness",
            ),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(
                preflight,
                "_windows_desktop_state",
                return_value=(True, "ready"),
            ),
            patch.object(
                preflight.payment_writer,
                "desktop_idle_seconds",
                return_value=600,
            ),
            patch.object(
                preflight,
                "_eligible_gymassistant_window_count",
                return_value=1,
            ),
            patch.object(preflight, "_payment_dialog_is_open", return_value=False),
        ):
            result = preflight.run_preflight()
        self.assertEqual(
            result,
            {
                "ok": True,
                "reason": "ready",
                "required_idle_seconds": 300,
                "eligible_window_count": 1,
            },
        )

    def test_preflight_never_allows_idle_floor_below_five_minutes(self):
        with (
            patch.object(
                preflight.scoped_runner,
                "native_computer_name",
                return_value="DREAMZ-FRNTDSK",
            ),
            patch.object(
                preflight.scoped_runner,
                "native_windows_user",
                return_value="Dreamz Fitness",
            ),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(
                preflight,
                "_windows_desktop_state",
                return_value=(True, "ready"),
            ),
            patch.object(
                preflight.payment_writer,
                "desktop_idle_seconds",
                return_value=299,
            ),
        ):
            result = preflight.run_preflight(1)
        self.assertEqual(result["reason"], "desktop_not_idle")
        self.assertEqual(result["required_idle_seconds"], 300)


class FrontdeskReleaseStaticTests(unittest.TestCase):
    @staticmethod
    def expected_files() -> dict[str, Path]:
        return {
            "sync_agent.py": ROOT / "sync_agent.py",
            "gymassistant_payment_writer.py": ROOT
            / "gymassistant_payment_writer.py",
            "process_fep_now.ps1": ROOT / "process_fep_now.ps1",
            "ga_import.py": ROOT / "ga_import.py",
            "ga_journal.py": ROOT / "ga_journal.py",
            "ga_documents.py": ROOT / "ga_documents.py",
            "storage_backend.py": ROOT / "storage_backend.py",
            r"frontdesk_payment_runner\__init__.py": ROOT
            / "frontdesk_payment_runner"
            / "__init__.py",
            r"frontdesk_payment_runner\preflight.py": ROOT
            / "frontdesk_payment_runner"
            / "preflight.py",
            r"frontdesk_payment_runner\runner.py": ROOT
            / "frontdesk_payment_runner"
            / "runner.py",
            r"frontdesk_payment_runner\README.md": ROOT
            / "frontdesk_payment_runner"
            / "README.md",
        }

    def test_manual_script_uses_only_robust_one_shot_runner(self):
        text = (ROOT / "process_fep_now.ps1").read_text(encoding="utf-8")
        lowered = text.casefold()
        self.assertIn('"dreamz-frntdsk"', lowered)
        self.assertIn('"dreamz fitness"', lowered)
        self.assertIn('"frontdesk_dreamz"', lowered)
        self.assertIn('"c:\\gym assistant 2.6"', lowered)
        self.assertIn(
            "from frontdesk_payment_runner.runner import main",
            lowered,
        )
        self.assertIn("--once", lowered)
        self.assertIn("--max-command-limit", lowered)
        self.assertIn("-b -i -c $runnerprogram", lowered)
        self.assertNotIn("--process-fep-command", lowered)
        self.assertNotIn("--payment-command-only", lowered)
        self.assertNotIn("--push-members", lowered)
        self.assertNotIn("--upload-files", lowered)
        self.assertNotIn("start-scheduledtask", lowered)
        self.assertNotIn("register-scheduledtask", lowered)
        self.assertIn("[environment]::machinename", lowered)
        self.assertNotIn("$env:computername", lowered)
        self.assertIn("windowsidentity]::getcurrent()", lowered)

    def test_all_lifecycle_scripts_use_native_machine_and_token_identity(self):
        script_paths = (
            ROOT / "process_fep_now.ps1",
            SCRIPTS / "Install-DreamzFrontdeskPaymentRunner.ps1",
            SCRIPTS / "Restore-DreamzFrontdeskPaymentRunner.ps1",
            SCRIPTS / "Get-DreamzFrontdeskPaymentRunnerInventory.ps1",
        )
        for path in script_paths:
            with self.subTest(script=path.name):
                lowered = path.read_text(encoding="utf-8").casefold()
                self.assertIn("[environment]::machinename", lowered)
                self.assertNotIn("$env:computername", lowered)
                self.assertIn("windowsidentity]::getcurrent()", lowered)

    def test_sync_agent_has_no_frontdesk_only_cli_patch(self):
        text = (ROOT / "sync_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("--payment-command-only", text)

    def test_installer_never_starts_or_registers_runtime(self):
        text = (
            SCRIPTS / "Install-DreamzFrontdeskPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        for forbidden in (
            "start-process",
            "start-scheduledtask",
            "register-scheduledtask",
            "disable-scheduledtask",
            "enable-scheduledtask",
            "new-service",
            "setenvironmentvariable",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("starts_during_install = $false", text)
        self.assertIn("changes_scheduled_sync = $false", text)

    def test_install_and_restore_keep_receipt_ledger_monotonic(self):
        install = (
            SCRIPTS / "Install-DreamzFrontdeskPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        restore = (
            SCRIPTS / "Restore-DreamzFrontdeskPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn("priorhistory.count -gt 0", install)
        self.assertIn("current-install-carried-forward", install)
        self.assertIn("copy-item -literalpath $existingledger", install)
        self.assertIn('{"schema":2,"receipts":{}}', install)
        self.assertIn("current authoritative ledger", restore)
        self.assertIn("copy-item -literalpath $currentledger", restore)
        self.assertNotIn(
            "move-item -literalpath $snapshotinstall -destination $installroot",
            restore,
        )

    def test_inventory_contains_no_mutating_runtime_commands(self):
        text = (
            SCRIPTS / "Get-DreamzFrontdeskPaymentRunnerInventory.ps1"
        ).read_text(encoding="utf-8").casefold()
        for forbidden in (
            "new-item",
            "copy-item",
            "move-item",
            "remove-item",
            "start-process",
            "start-scheduledtask",
            "stop-scheduledtask",
            "register-scheduledtask",
            "set-acl",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn('credential_check = "not_performed"', text)
        self.assertIn("receipt_ledger = $ledgerhealth", text)

    def test_latest_writer_safety_fixes_are_included(self):
        text = (ROOT / "gymassistant_payment_writer.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("decline_credit_balance_prompt", text)
        self.assertIn("payment_on_current_balance", text)
        self.assertIn("verify_member_payment_readback", text)
        self.assertIn('"payment_dialog_did_not_open"', text)
        self.assertNotIn("click_window_center", text)

    def test_frontdesk_runner_has_durable_receipts_and_fail_stop(self):
        text = (
            ROOT / "frontdesk_payment_runner" / "runner.py"
        ).read_text(encoding="utf-8")
        self.assertIn("receipts.record_writer_started", text)
        self.assertIn("receipts.mark_applied", text)
        self.assertIn("receipts.mark_ambiguous", text)
        self.assertIn('result.get("applied") is not True', text)
        self.assertIn("return reason, summary", text)
        self.assertIn("require_existing=True", text)
        self.assertIn("CreateMutexW(None, True", text)
        self.assertIn("ReleaseMutex(self.handle)", text)
        self.assertNotIn('os.getenv("COMPUTERNAME")', text)
        self.assertNotIn("getpass.getuser", text)

    def test_inventory_hashes_match_canonical_runtime_files(self):
        expected_files = self.expected_files()
        embedded = powershell_hashes(
            SCRIPTS / "Get-DreamzFrontdeskPaymentRunnerInventory.ps1"
        )
        self.assertEqual(set(embedded), set(expected_files))
        for relative_path, source in expected_files.items():
            self.assertEqual(
                embedded[relative_path],
                canonical_sha256(source),
            )

    def test_process_script_pins_every_other_runtime_file(self):
        expected_files = self.expected_files()
        expected_files.pop("process_fep_now.ps1")
        embedded = powershell_hashes(ROOT / "process_fep_now.ps1")
        self.assertEqual(set(embedded), set(expected_files))
        for relative_path, source in expected_files.items():
            self.assertEqual(
                embedded[relative_path],
                canonical_sha256(source),
            )

    def test_release_manifest_drives_install_and_current_restore_hashes(self):
        install = (
            SCRIPTS / "Install-DreamzFrontdeskPaymentRunner.ps1"
        ).read_text(encoding="utf-8")
        restore = (
            SCRIPTS / "Restore-DreamzFrontdeskPaymentRunner.ps1"
        ).read_text(encoding="utf-8")
        validator = (
            SCRIPTS / "Test-DreamzFrontdeskPaymentRunnerRelease.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "$expectedHashes[$relativePath.Replace(\"/\", \"\\\")]",
            install,
        )
        self.assertIn(
            "$expectedRuntimeHashes[$relativePath.Replace(\"/\", \"\\\")]",
            restore,
        )
        for text in (install, restore, validator):
            self.assertIn(
                "scripts/frontdesk_payment_runner/"
                "Install-DreamzFrontdeskPaymentRunner.ps1",
                text,
            )
            self.assertIn(
                "scripts/frontdesk_payment_runner/"
                "Restore-DreamzFrontdeskPaymentRunner.ps1",
                text,
            )
            self.assertIn(
                "scripts/frontdesk_payment_runner/"
                "Test-DreamzFrontdeskPaymentRunnerRelease.ps1",
                text,
            )

    def test_lifecycle_mutex_is_acquired_before_runner_mutex(self):
        for script_name in (
            "Install-DreamzFrontdeskPaymentRunner.ps1",
            "Restore-DreamzFrontdeskPaymentRunner.ps1",
        ):
            text = (SCRIPTS / script_name).read_text(
                encoding="utf-8"
            ).casefold()
            lifecycle_wait = text.index(
                "$lifecyclemutex.waitone(0)"
            )
            runner_wait = text.index("$runnermutex.waitone(0)")
            self.assertLess(lifecycle_wait, runner_wait)

    def test_writer_regressions_use_unmistakably_synthetic_members(self):
        paths = (
            ROOT / "tests" / "test_gymassistant_payment_writer.py",
            ROOT
            / "tests"
            / "test_gymassistant_payment_writer_credit_balance.py",
        )
        combined = "\n".join(
            path.read_text(encoding="utf-8").casefold()
            for path in paths
        )
        self.assertIn("voorbeeld lid", combined)
        self.assertIn("990001", combined)
        self.assertIn("990002", combined)


if __name__ == "__main__":
    unittest.main()
