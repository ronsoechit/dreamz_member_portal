from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "ron_laptop_payment_runner"


class RunnerScriptSafetyTests(unittest.TestCase):
    def test_expected_lifecycle_scripts_exist(self):
        expected = {
            "Install-RonLaptopPaymentRunner.ps1",
            "Start-RonLaptopPaymentRunner.ps1",
            "Stop-RonLaptopPaymentRunner.ps1",
            "Uninstall-RonLaptopPaymentRunner.ps1",
            "Restore-RonLaptopPaymentRunner.ps1",
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
            SCRIPT_ROOT / "Install-RonLaptopPaymentRunner.ps1"
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

    def test_stop_script_never_forcibly_kills_a_process(self):
        stop_script = (
            SCRIPT_ROOT / "Stop-RonLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        self.assertNotIn("stop-process", stop_script)
        self.assertNotIn("taskkill", stop_script)
        self.assertIn("stop.request", stop_script)


if __name__ == "__main__":
    unittest.main()
