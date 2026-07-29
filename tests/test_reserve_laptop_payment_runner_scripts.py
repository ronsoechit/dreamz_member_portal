from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "reserve_laptop_payment_runner"


class RunnerScriptSafetyTests(unittest.TestCase):
    def test_expected_lifecycle_scripts_exist(self):
        expected = {
            "Install-ReserveLaptopPaymentRunner.ps1",
            "Start-ReserveLaptopPaymentRunner.ps1",
            "Stop-ReserveLaptopPaymentRunner.ps1",
            "Uninstall-ReserveLaptopPaymentRunner.ps1",
            "Restore-ReserveLaptopPaymentRunner.ps1",
            "Test-ReserveLaptopPaymentRunnerPreflight.ps1",
            "Test-DreamzReservePaymentRunnerRelease.ps1",
        }
        self.assertEqual(
            {path.name for path in SCRIPT_ROOT.glob("*.ps1")},
            expected,
        )

    def test_scripts_do_not_run_member_sync_or_put_token_on_command_line(self):
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(SCRIPT_ROOT.glob("*.ps1"))
        ).casefold()
        for forbidden in (
            "sync_agent.py",
            "--push-members",
            "--upload-files",
            "--sync-token",
            "/api/sync/members",
            "members.btx",
            "journal.dat",
            "members.dat",
        ):
            self.assertNotIn(forbidden, combined)

    def test_installer_is_current_user_not_service_or_scheduled_task(self):
        installer = (
            SCRIPT_ROOT / "Install-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn('getfolderpath("startup")', installer)
        self.assertIn("localappdata", installer)
        for forbidden in (
            "new-scheduledtask",
            "register-scheduledtask",
            "new-service",
            "sc.exe",
            "netsh",
            "new-netfirewallrule",
        ):
            self.assertNotIn(forbidden, installer)

    def test_upgrade_preserves_and_validates_receipt_ledger_without_starting(self):
        installer = (
            SCRIPT_ROOT / "Install-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        copy_position = installer.index(
            "copy-item -literalpath $oldreceiptledger"
        )
        health_position = installer.index("--health-check")
        self.assertLess(copy_position, health_position)
        self.assertNotIn("start-process", installer)
        self.assertIn("the runner was not started", installer)
        self.assertIn("upgrade refused", installer)
        self.assertIn(
            "existing receipt ledger is missing",
            installer,
        )
        self.assertIn(
            "prior runner state exists",
            installer,
        )
        self.assertIn(
            "demonstrable first install",
            installer,
        )
        self.assertIn("idle_seconds = 5", installer)
        self.assertIn('{"schema":2,"receipts":{}}', installer)

        missing_guard = installer.index(
            "if ($existinginstall -and -not "
            "(test-path -literalpath $existingreceiptledger -pathtype leaf))"
        )
        history_guard = installer.index(
            "if (-not $existinginstall -and "
            "($existingstartuplink -or $priorrunnerhistory.count -gt 0))"
        )
        empty_ledger = installer.index('{"schema":2,"receipts":{}}')
        self.assertLess(missing_guard, empty_ledger)
        self.assertLess(history_guard, empty_ledger)

    def test_stop_script_never_forcibly_kills_a_process(self):
        stop_script = (
            SCRIPT_ROOT / "Stop-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertNotIn("stop-process", stop_script)
        self.assertNotIn("taskkill", stop_script)
        self.assertIn("stop.request", stop_script)
        self.assertIn(
            '[threading.mutex]::openexisting($name)',
            stop_script,
        )
        self.assertIn("dreamzreservelaptoppaymentrunner", stop_script)
        self.assertIn("dreamzreservelaptoppaymentlauncher", stop_script)
        self.assertIn("$quietmutexchecks -ge 5", stop_script)
        self.assertIn("nothing was killed", stop_script)

        start_script = (
            SCRIPT_ROOT / "Start-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn("dreamzreservelaptoppaymentlauncher", start_script)
        self.assertIn("[threading.mutex]::new(", start_script)
        self.assertIn("$launchermutex.releasemutex()", start_script)

    def test_restore_refuses_stale_upgrade_snapshot_and_health_checks_uninstall(self):
        restore = (
            SCRIPT_ROOT / "Restore-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn(
            '$rollbackname -notmatch "^uninstalled-',
            restore,
        )
        self.assertIn("pre-upgrade snapshots", restore)
        self.assertIn("--health-check", restore)
        self.assertIn("receipt ledger", restore)
        self.assertIn("newer or same-generation runner snapshot exists", restore)
        self.assertIn("unrecognized runner rollback history exists", restore)
        self.assertIn("startup shortcut already exists", restore)
        self.assertIn(
            "if ($historytimestamp -ge $selectedtimestamp)",
            restore,
        )

        newest_check = restore.index(
            "newer or same-generation runner snapshot exists"
        )
        move_position = restore.index(
            "move-item -literalpath $savedinstall"
        )
        self.assertLess(newest_check, move_position)

    def test_exact_target_identity_and_preflight_are_enforced(self):
        installer = (
            SCRIPT_ROOT / "Install-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        start_script = (
            SCRIPT_ROOT / "Start-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        preflight = (
            SCRIPT_ROOT / "Test-ReserveLaptopPaymentRunnerPreflight.ps1"
        ).read_text(encoding="utf-8").casefold()
        for content in (installer, start_script, preflight):
            self.assertIn("desktop-8km7v7d", content)
            self.assertIn('"ron"', content)
        self.assertIn("--preflight", preflight)
        self.assertNotIn("-i", preflight.split())
        self.assertNotIn("sync-token.dpapi", preflight)
        self.assertNotIn("sync_api_token", preflight)

        restore = (
            SCRIPT_ROOT / "Restore-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertNotIn("-i", restore.split())


if __name__ == "__main__":
    unittest.main()
