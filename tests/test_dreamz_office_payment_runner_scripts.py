from pathlib import Path
import json
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "dreamz_office_payment_runner"


class RunnerScriptSafetyTests(unittest.TestCase):
    def test_expected_lifecycle_scripts_exist(self):
        expected = {
            "Get-DreamzOfficePaymentRunnerInventory.ps1",
            "Install-DreamzOfficePaymentRunner.ps1",
            "Start-DreamzOfficePaymentRunner.ps1",
            "Stop-DreamzOfficePaymentRunner.ps1",
            "Uninstall-DreamzOfficePaymentRunner.ps1",
            "Restore-DreamzOfficePaymentRunner.ps1",
        }
        self.assertEqual(
            {path.name for path in SCRIPT_ROOT.glob("*.ps1")},
            expected,
        )
        self.assertTrue((SCRIPT_ROOT / "START-INVENTARISATIE.cmd").is_file())

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

    def test_inventory_is_read_only_and_share_safe(self):
        inventory = (
            SCRIPT_ROOT / "Get-DreamzOfficePaymentRunnerInventory.ps1"
        ).read_text(encoding="utf-8").casefold()
        for forbidden in (
            "win32_process",
            ".commandline",
            "get-ciminstance win32_process",
            "member_id",
            "member_name",
            "members.dat",
            "journal.dat",
            "x-sync-token",
            "fep_payment_runner_token",
            "sync_api_token",
            "payment-token.dpapi",
            "new-item",
            "set-content",
            "add-content",
            "out-file",
            "copy-item",
            "move-item",
            "remove-item",
            "start-process",
            "stop-process",
            "register-scheduledtask",
            "set-itemproperty",
            "new-itemproperty",
        ):
            self.assertNotIn(forbidden, inventory)
        self.assertNotIn("current_identity = $currentidentity", inventory)
        self.assertNotIn("interactive_user = $interactiveuser", inventory)
        self.assertNotIn("current_process_session_id =", inventory)
        self.assertNotIn("active_console_session_id =", inventory)
        self.assertNotIn("main_window_title =", inventory)
        self.assertNotIn("window_title =", inventory)
        self.assertIn("data_path = $datapath", inventory)
        self.assertIn("source_root = $sourceroot", inventory)
        self.assertIn('expectedcomputername = "dreamz-office-p"', inventory)
        self.assertIn("read_only = $true", inventory)
        self.assertIn("installation_prerequisites_met", inventory)
        self.assertIn("unattended_remote_prerequisites_met", inventory)
        self.assertIn("test-fullyqualifiedwindowspath", inventory)
        self.assertNotIn("ispathfullyqualified", inventory)
        self.assertIn("[string]$outputpath", inventory)
        self.assertIn("[io.file]::writealltext(", inventory)
        self.assertIn("[io.file]::replace(", inventory)
        self.assertIn("[io.file]::move(", inventory)
        self.assertIn("[io.file]::delete(", inventory)
        self.assertIn("$json", inventory)

    def test_easy_inventory_launcher_is_local_quoted_and_nonprivileged(self):
        launcher = (
            SCRIPT_ROOT / "START-INVENTARISATIE.cmd"
        ).read_text(encoding="utf-8").casefold()
        for required in (
            "%~dp0",
            'set "inventory_script=',
            'set "inventory_output=',
            '-noprofile',
            '-noninteractive',
            '-executionpolicy bypass',
            '-file "%inventory_script%"',
            '-outputpath "%inventory_output%"',
            "dreamz-office-pc-inventarisatie.json",
        ):
            self.assertIn(required, launcher)
        for forbidden in (
            "runas",
            "install-dreamzofficepaymentrunner",
            "paymenttoken",
            "sync-token",
            "set-executionpolicy",
            "x:\\",
        ):
            self.assertNotIn(forbidden, launcher)

    def test_inventory_atomically_writes_json_to_path_with_spaces(self):
        inventory_script = (
            SCRIPT_ROOT / "Get-DreamzOfficePaymentRunnerInventory.ps1"
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "transfer folder with spaces"
            output_dir.mkdir()
            output_path = output_dir / "Dreamz Office inventarisatie.json"
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(inventory_script),
                    "-OutputPath",
                    str(output_path),
                    "-AllowNonOfficeForTesting",
                    "-SkipPortalHttpsCheck",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            stdout_payload = json.loads(result.stdout)
            self.assertEqual(payload, stdout_payload)
            self.assertTrue(payload["read_only"])
            self.assertEqual(
                list(output_dir.glob(".*.tmp")),
                [],
            )

    def test_inventory_checks_session_python_ga_drive_power_and_startup(self):
        inventory = (
            SCRIPT_ROOT / "Get-DreamzOfficePaymentRunnerInventory.ps1"
        ).read_text(encoding="utf-8").casefold()
        for required in (
            "wtsgetactiveconsolesessionid",
            "openinputdesktop",
            "getlastinputinfo",
            "get-command python.exe",
            'processname -like "gym assistant*"',
            "mainwindowhandle",
            "path=",
            "test-path -literalpath $datapath",
            "get-psdrive",
            "powercfg.exe /getactivescheme",
            'getfolderpath("startup")',
            "portal_https",
            "x_drive_is_transfer_only",
        ):
            self.assertIn(required, inventory)

    def test_all_office_powershell_scripts_parse(self):
        for script in sorted(SCRIPT_ROOT.glob("*.ps1")):
            script_path = str(script).replace("'", "''")
            command = (
                "$errors = $null; "
                "[void][Management.Automation.Language.Parser]::ParseFile("
                f"'{script_path}', "
                "[ref]$null, [ref]$errors); "
                "if ($errors.Count) { "
                "$errors | ForEach-Object { Write-Error $_.Message }; exit 1 "
                "}"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"{script.name}: {result.stderr}",
            )

    def test_installer_is_current_user_not_service_or_scheduled_task(self):
        installer = (
            SCRIPT_ROOT / "Install-DreamzOfficePaymentRunner.ps1"
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

    def test_upgrade_preserves_and_validates_receipt_ledger_before_start(self):
        installer = (
            SCRIPT_ROOT / "Install-DreamzOfficePaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        copy_position = installer.index(
            "copy-item -literalpath $oldreceiptledger"
        )
        health_position = installer.index("--health-check")
        start_position = installer.index("start-process")
        self.assertLess(copy_position, health_position)
        self.assertLess(health_position, start_position)
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
        self.assertIn("idle_seconds = 300", installer)
        self.assertIn("[string]$sourceroot", installer)
        self.assertIn("source_root = $resolvedsourceroot", installer)
        self.assertIn("sourceroot must contain an existing data directory", installer)
        self.assertNotIn("ispathfullyqualified", installer)
        self.assertIn("installation is restricted", installer)
        self.assertIn("dreamz-office-p", installer)
        self.assertIn("x:\\ is reserved", installer)
        self.assertIn("shared operations transfer location", installer)
        self.assertIn("sourceroot differs from the installed runner", installer)
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
            SCRIPT_ROOT / "Stop-DreamzOfficePaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertNotIn("stop-process", stop_script)
        self.assertNotIn("taskkill", stop_script)
        self.assertIn("stop.request", stop_script)
        self.assertIn(
            '[threading.mutex]::openexisting($name)',
            stop_script,
        )
        self.assertIn("dreamzofficepaymentrunner", stop_script)
        self.assertIn("dreamzofficepaymentlauncher", stop_script)
        self.assertIn("$quietmutexchecks -ge 5", stop_script)
        self.assertIn("nothing was killed", stop_script)

        start_script = (
            SCRIPT_ROOT / "Start-DreamzOfficePaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertIn("dreamzofficepaymentlauncher", start_script)
        self.assertIn("[threading.mutex]::new(", start_script)
        self.assertIn("$launchermutex.releasemutex()", start_script)

    def test_restore_refuses_stale_upgrade_snapshot_and_health_checks_uninstall(self):
        restore = (
            SCRIPT_ROOT / "Restore-DreamzOfficePaymentRunner.ps1"
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


if __name__ == "__main__":
    unittest.main()
