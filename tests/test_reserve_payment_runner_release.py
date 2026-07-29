from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / "scripts" / "reserve_laptop_payment_runner"
BUILD_SCRIPT = SCRIPT_ROOT / "build_release.py"


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "reserve_payment_runner_release_builder",
        BUILD_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load reserve release builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()


class ReserveReleaseTests(unittest.TestCase):
    def test_release_payload_is_exact_and_contains_no_portal_or_data_source(self):
        expected = {
            "reserve_laptop_payment_runner/README.md",
            "reserve_laptop_payment_runner/__init__.py",
            "reserve_laptop_payment_runner/runner.py",
            "gymassistant_payment_writer.py",
            (
                "scripts/reserve_laptop_payment_runner/"
                "Install-ReserveLaptopPaymentRunner.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Restore-ReserveLaptopPaymentRunner.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Start-ReserveLaptopPaymentRunner.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Stop-ReserveLaptopPaymentRunner.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Test-DreamzReservePaymentRunnerRelease.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Test-ReserveLaptopPaymentRunnerPreflight.ps1"
            ),
            (
                "scripts/reserve_laptop_payment_runner/"
                "Uninstall-ReserveLaptopPaymentRunner.ps1"
            ),
        }
        self.assertEqual(set(BUILDER.PAYLOAD_FILES), expected)
        combined = "\n".join(BUILDER.PAYLOAD_FILES).casefold()
        for forbidden in (
            ".env",
            "sync_agent.py",
            "members.dat",
            "journal.dat",
            "database",
            "browser",
            "frontdesk_payment_runner/",
            "ron_laptop_payment_runner/",
            "dreamz_office_payment_runner/",
        ):
            self.assertNotIn(forbidden, combined)

    def test_manifest_pins_unique_target_and_safe_install_defaults(self):
        hashes = {
            relative_path: "A" * 64
            for relative_path in BUILDER.PAYLOAD_FILES
        }
        manifest = json.loads(
            BUILDER.manifest_bytes("b" * 40, hashes).decode("utf-8")
        )
        self.assertEqual(
            manifest["release"],
            "dreamz_reserve_payment_runner",
        )
        self.assertEqual(
            manifest["expected_computer"],
            "DESKTOP-8KM7V7D",
        )
        self.assertEqual(manifest["expected_windows_user"], "ron")
        self.assertEqual(manifest["agent_id"], "reserve_8km7v7d")
        self.assertEqual(
            manifest["source_root"],
            r"\\DREAMZ-FRNTDSK\Gym Assistant 2.6",
        )
        self.assertFalse(manifest["starts_during_install"])
        self.assertFalse(manifest["creates_startup_by_default"])
        self.assertTrue(manifest["requires_dedicated_payment_token"])

    def test_installer_validates_release_before_first_file_mutation(self):
        installer = (
            SCRIPT_ROOT / "Install-ReserveLaptopPaymentRunner.ps1"
        ).read_text(encoding="utf-8").casefold()
        validation = installer.index(
            "test-dreamzreservepaymentrunnerrelease.ps1"
        )
        first_mutation = installer.index(
            "new-item -itemtype directory"
        )
        self.assertLess(validation, first_mutation)
        self.assertIn("the runner was not started", installer)
        self.assertNotIn("start-process", installer)
        self.assertIn("createstartupshortcut", installer)
        self.assertIn("windowsapps", installer)

    def test_runner_and_lifecycle_never_reuse_an_existing_agent_identity(self):
        paths = [
            ROOT / "reserve_laptop_payment_runner" / "runner.py",
            *sorted(SCRIPT_ROOT.glob("*.ps1")),
        ]
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in paths
        ).casefold()
        self.assertIn("reserve_8km7v7d", combined)
        self.assertIn("desktop-8km7v7d", combined)
        for forbidden in (
            'agent_id = "frontdesk_dreamz"',
            'agent_id = "ron_laptop"',
            'agent_id = "dreamz_office"',
            "dreamzronlaptoppaymentrunner",
            "dreamzofficepaymentrunner",
            "dreamzfrontdeskpaymentrunner",
        ):
            self.assertNotIn(forbidden, combined)

    def test_release_validator_pins_target_manifest_and_exact_allowlist(self):
        validator = (
            SCRIPT_ROOT / "Test-DreamzReservePaymentRunnerRelease.ps1"
        ).read_text(encoding="utf-8")
        for expected in (
            "dreamz_reserve_payment_runner",
            "DESKTOP-8KM7V7D",
            "reserve_8km7v7d",
            "requires_dedicated_payment_token",
            "Compare-Object $archiveAllowlist $actualFiles",
        ):
            self.assertIn(expected, validator)


if __name__ == "__main__":
    unittest.main()
