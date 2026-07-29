from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" / "frontdesk_payment_runner"
BUILD_SCRIPT = SCRIPTS / "build_release.py"
VALIDATOR = SCRIPTS / "Test-DreamzFrontdeskPaymentRunnerRelease.ps1"
INVENTORY = SCRIPTS / "Get-DreamzFrontdeskPaymentRunnerInventory.ps1"
RESTORE = SCRIPTS / "Restore-DreamzFrontdeskPaymentRunner.ps1"
PROCESS = ROOT / "process_fep_now.ps1"
# Exercise the declared production baseline first. PowerShell 7 is only a
# fallback on hosts where Windows PowerShell is unavailable.
POWERSHELL = (
    shutil.which("powershell.exe")
    or shutil.which("powershell")
    or shutil.which("pwsh.exe")
    or shutil.which("pwsh")
)
GIT = shutil.which("git.exe") or shutil.which("git")


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "frontdesk_release_builder_hardening_test",
        BUILD_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the Frontdesk release builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()
PAYLOAD_FILES = tuple(BUILDER.PAYLOAD_FILES)
RUNTIME_FILES = (
    "sync_agent.py",
    "gymassistant_payment_writer.py",
    "process_fep_now.ps1",
    "ga_import.py",
    "ga_journal.py",
    "ga_documents.py",
    "storage_backend.py",
    r"frontdesk_payment_runner\__init__.py",
    r"frontdesk_payment_runner\preflight.py",
    r"frontdesk_payment_runner\runner.py",
    r"frontdesk_payment_runner\README.md",
)
PINNED_LEGACY_SOURCE_COMMIT = (
    "f2678f6699d7e36c85946116461817131c7af525"
)
PINNED_LEGACY_RUNTIME_HASHES = {
    "sync_agent.py": (
        "4C7B9DA7B5CC651A727D0AA04C9FABDBA3AB05329461C328986FE7461D1D42D7"
    ),
    "gymassistant_payment_writer.py": (
        "13E8B3FFDC35C9675DDB3427EC1A35FB839F5060CDF8792A9589FF311F695201"
    ),
    "process_fep_now.ps1": (
        "64BF577872CF33FBC63D6CEF09BFC44BE0A4636B693C458A8D8CCA4799D9A3EB"
    ),
    "ga_import.py": (
        "14F82F2E8E4AE684659A8F400EABFADF4B32CB554D2314F8FA391E53645D788F"
    ),
    "ga_journal.py": (
        "5AF7346196AE785AED6C0BE25937F25ACB7DE6B8A8A3356C1D56D32CF1388712"
    ),
    "ga_documents.py": (
        "57A9F1D814CA4C8B70CE803AD13E6B02D4560077FD9491B204D58C6CC35858EC"
    ),
    "storage_backend.py": (
        "8E77D83176D06A37D1A7EE462A26F24131D491BF28846FE42BCBC1343B957694"
    ),
    r"frontdesk_payment_runner\__init__.py": (
        "64B37E6A63625504B5529C497AB69BA6A91AE2AE33FBDB294AF20E1E11C125AC"
    ),
    r"frontdesk_payment_runner\preflight.py": (
        "A60364D19AB937CFB63B49B2270D525046166DB2CAEE20015C98362547CA0685"
    ),
    r"frontdesk_payment_runner\runner.py": (
        "95E38FF552B5CA07C034D3D58D68C39F6DE701EB5FEFA7796E8EC5E791901069"
    ),
    r"frontdesk_payment_runner\README.md": (
        "04A0080DABC193417C3593674F6956991C7B61C1A992BE8E52E54D607A24EC20"
    ),
}
PINNED_LEGACY_CRLF_FILES = {
    "gymassistant_payment_writer.py",
    "ga_import.py",
    "ga_journal.py",
    "ga_documents.py",
    "storage_backend.py",
}
PINNED_LEGACY_SYNC_LF_ONLY_LINES = {
    1605,
    1617,
    1618,
    1619,
    1624,
    1625,
    1626,
    1631,
    1649,
    1650,
    1651,
    1652,
}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _canonical_source(relative_path: str) -> bytes:
    path = ROOT / Path(relative_path)
    return BUILDER.canonical_text_bytes(path)


def _copy_current_release_fixture(package_root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative_path in PAYLOAD_FILES:
        payload = _canonical_source(relative_path)
        destination = package_root / Path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        hashes[relative_path] = _sha256_bytes(payload)
    manifest = BUILDER.manifest_bytes("a" * 40, hashes)
    (package_root / BUILDER.MANIFEST_NAME).write_bytes(manifest)
    return hashes


def _run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _run_powershell(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        raise unittest.SkipTest("Windows PowerShell is not available")
    return _run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        env=env,
        timeout=timeout,
    )


def _json_from_output(output: str) -> dict:
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise AssertionError(f"No JSON object in PowerShell output:\n{output}")
    return json.loads(output[start : end + 1])


def _marker_command(path: Path) -> Path:
    marker_command = path / "marker-python.cmd"
    marker_command.write_text(
        "@echo off\r\n"
        'if not "%FR_HARDENING_MARKER%"=="" '
        'echo invoked>"%FR_HARDENING_MARKER%"\r\n'
        "exit /b 91\r\n",
        encoding="ascii",
        newline="",
    )
    return marker_command


def _powershell_identity() -> tuple[str, str]:
    if not POWERSHELL:
        raise unittest.SkipTest("Windows PowerShell is not available")
    result = _run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "[Environment]::MachineName; "
                "([Security.Principal.WindowsIdentity]::GetCurrent().Name "
                "-split '\\\\')[-1]"
            ),
        ],
    )
    if result.returncode:
        raise AssertionError(result.stdout)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise AssertionError(f"Could not read Windows identity: {result.stdout}")
    return lines[-2], lines[-1]


def _replace_assignment(text: str, variable: str, value: str) -> str:
    pattern = rf'(?m)^\${re.escape(variable)}\s*=\s*"[^"]*"\s*$'
    replacement = f'${variable} = "{value}"'
    changed, count = re.subn(
        pattern,
        lambda _match: replacement,
        text,
        count=1,
    )
    if count != 1:
        raise AssertionError(f"Could not patch ${variable}")
    return changed


def _runtime_path(root: Path, relative_path: str) -> Path:
    return root / Path(relative_path.replace("\\", "/"))


def _pinned_legacy_runtime_payloads() -> dict[str, bytes]:
    if not GIT:
        raise unittest.SkipTest(
            "Git is required to reconstruct the pinned legacy artifact"
        )
    payloads: dict[str, bytes] = {}
    for relative_path in RUNTIME_FILES:
        git_path = relative_path.replace("\\", "/")
        result = subprocess.run(
            [
                GIT,
                "show",
                f"{PINNED_LEGACY_SOURCE_COMMIT}:{git_path}",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise AssertionError(
                "Could not read pinned legacy Git blob "
                f"{git_path}: {result.stderr.decode(errors='replace')}"
            )
        blob = result.stdout
        if b"\r" in blob:
            raise AssertionError(
                f"Pinned Git blob unexpectedly contains CR bytes: {git_path}"
            )
        if relative_path == "sync_agent.py":
            lines = blob.splitlines(keepends=True)
            payload = b"".join(
                (
                    line
                    if line_number in PINNED_LEGACY_SYNC_LF_ONLY_LINES
                    else line.replace(b"\n", b"\r\n")
                )
                for line_number, line in enumerate(lines, 1)
            )
        elif relative_path in PINNED_LEGACY_CRLF_FILES:
            payload = blob.replace(b"\n", b"\r\n")
        else:
            payload = blob
        expected_hash = PINNED_LEGACY_RUNTIME_HASHES[relative_path]
        if _sha256_bytes(payload) != expected_hash:
            raise AssertionError(
                "Pinned legacy artifact reconstruction mismatch for "
                f"{relative_path}"
            )
        payloads[relative_path] = payload
    return payloads


def _installed_manifest(
    file_hashes: dict[str, str],
    *,
    source_commit: str = "b" * 40,
    release_version: str = "1.0.0",
    computer: str = "DREAMZ-FRNTDSK",
    user: str = "Dreamz Fitness",
) -> dict:
    return {
        "schema": 2,
        "release_version": release_version,
        "base_commit": "4fbfff17d0758282e106cbeab8368787e7c34ca8",
        "source_commit": source_commit,
        "expected_computer": computer,
        "expected_windows_user": user,
        "agent_id": "frontdesk_dreamz",
        "source_root": r"C:\Gym Assistant 2.6",
        "receipt_ledger_schema": 2,
        "receipt_ledger_policy": "current-install-carried-forward",
        "starts_during_install": False,
        "changes_scheduled_sync": False,
        "file_hashes": file_hashes,
    }


class DeterministicReleaseBuilderTests(unittest.TestCase):
    @unittest.skipUnless(GIT, "Git is required for deterministic build tests")
    def test_build_is_deterministic_exact_and_lf_independent_of_autocrlf(self):
        self.assertEqual(len(PAYLOAD_FILES), 17)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            for index, relative_path in enumerate(PAYLOAD_FILES):
                _write_lf(
                    source / Path(relative_path),
                    f"synthetic payload {index}\nsecond line\n",
                )

            for arguments in (
                ["init"],
                ["config", "user.email", "release-test@example.invalid"],
                ["config", "user.name", "Release Test"],
                ["config", "core.autocrlf", "false"],
                ["add", "--", *PAYLOAD_FILES],
                ["commit", "-m", "synthetic release source"],
            ):
                result = _run([GIT, "-C", str(source), *arguments])
                self.assertEqual(result.returncode, 0, result.stdout)
            commit = _run(
                [GIT, "-C", str(source), "rev-parse", "HEAD"]
            ).stdout.strip()
            self.assertRegex(commit, r"^[0-9a-f]{40}$")

            first = BUILDER.build_release(source, base / "out-a", commit)

            # Exercise the canonicalization promise against a CRLF worktree.
            result = _run(
                [GIT, "-C", str(source), "config", "core.autocrlf", "true"]
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            for relative_path in PAYLOAD_FILES:
                path = source / Path(relative_path)
                text = path.read_text(encoding="utf-8")
                path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

            second = BUILDER.build_release(source, base / "out-b", commit)
            first_zip = Path(first["zip"])
            second_zip = Path(second["zip"])

            self.assertEqual(first["payload_file_count"], 17)
            self.assertEqual(first["archive_file_count"], 18)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first_zip.read_bytes(), second_zip.read_bytes())

            with zipfile.ZipFile(first_zip) as archive:
                names = archive.namelist()
                self.assertEqual(
                    set(names),
                    set(PAYLOAD_FILES) | {BUILDER.MANIFEST_NAME},
                )
                self.assertEqual(len(names), 18)
                manifest = json.loads(
                    archive.read(BUILDER.MANIFEST_NAME).decode("utf-8")
                )
                self.assertEqual(manifest["payload_file_count"], 17)
                self.assertEqual(set(manifest["files"]), set(PAYLOAD_FILES))
                self.assertEqual(manifest["eol_policy"], "lf")
                for relative_path in PAYLOAD_FILES:
                    payload = archive.read(relative_path)
                    self.assertNotIn(b"\r", payload)
                    self.assertEqual(
                        manifest["files"][relative_path],
                        _sha256_bytes(payload),
                    )


class ReleaseValidatorFailClosedTests(unittest.TestCase):
    def test_lifecycle_scope_failures_never_execute_python(self):
        variants = ("missing", "extra", "tampered")
        for variant in variants:
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory() as temporary:
                    base = Path(temporary)
                    package = base / "package"
                    _copy_current_release_fixture(package)
                    if variant == "missing":
                        (
                            package
                            / "scripts"
                            / "frontdesk_payment_runner"
                            / "Install-DreamzFrontdeskPaymentRunner.ps1"
                        ).unlink()
                    elif variant == "extra":
                        _write_lf(
                            package
                            / "scripts"
                            / "frontdesk_payment_runner"
                            / "Unexpected-LifecycleScript.ps1",
                            "throw 'must not be present'\n",
                        )
                    else:
                        restore = (
                            package
                            / "scripts"
                            / "frontdesk_payment_runner"
                            / "Restore-DreamzFrontdeskPaymentRunner.ps1"
                        )
                        restore.write_bytes(
                            restore.read_bytes() + b"# unauthorized change\n"
                        )

                    marker = base / "python-invoked.txt"
                    marker_python = _marker_command(base)
                    env = os.environ.copy()
                    env["FR_HARDENING_MARKER"] = str(marker)
                    result = _run_powershell(
                        package
                        / "scripts"
                        / "frontdesk_payment_runner"
                        / "Test-DreamzFrontdeskPaymentRunnerRelease.ps1",
                        "-PythonPath",
                        str(marker_python),
                        env=env,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertFalse(
                        marker.exists(),
                        f"Python ran before {variant} validation failed",
                    )
                    payload = _json_from_output(result.stdout)
                    self.assertFalse(payload["valid"])
                    if variant == "tampered":
                        self.assertFalse(payload["hashes_ok"])
                    else:
                        self.assertFalse(payload["exact_allowlist_ok"])


class InventoryFailClosedTests(unittest.TestCase):
    def test_corrupt_installed_runner_executes_no_python(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            install = base / "install"
            install.mkdir()
            inventory_text = INVENTORY.read_text(encoding="utf-8")
            expected = {
                key: value
                for key, value in re.findall(
                    r'(?m)^\s*"([^"]+)"\s*=\s*"([0-9A-F]{64})"\s*$',
                    inventory_text,
                )
            }
            self.assertEqual(set(expected), set(RUNTIME_FILES))
            for relative_path in RUNTIME_FILES:
                source_key = relative_path.replace("\\", "/")
                if source_key == "process_fep_now.ps1":
                    payload = _canonical_source(source_key)
                else:
                    payload = _canonical_source(source_key)
                destination = _runtime_path(install, relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)

            corrupt_runner = _runtime_path(
                install, r"frontdesk_payment_runner\runner.py"
            )
            corrupt_runner.write_bytes(
                b"raise RuntimeError('corrupt runner must never execute')\n"
            )
            marker = base / "python-invoked.txt"
            marker_python = _marker_command(base)
            _write_lf(install / "python-path.txt", str(marker_python) + "\n")
            _write_lf(install / "runtime.json", "{}\n")
            _write_lf(
                install / "state" / "payment-receipts.json",
                '{"schema":2,"receipts":{}}\n',
            )
            manifest = _installed_manifest(expected)
            _write_lf(
                install / "installed-manifest.json",
                json.dumps(manifest, sort_keys=True) + "\n",
            )

            env = os.environ.copy()
            env["FR_HARDENING_MARKER"] = str(marker)
            result = _run_powershell(
                INVENTORY,
                "-InstallRoot",
                str(install),
                "-TrustedPythonPath",
                str(marker_python),
                "-SkipDesktopPreflight",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(marker.exists(), result.stdout)
            payload = _json_from_output(result.stdout)
            self.assertTrue(payload["install_exists"])
            self.assertFalse(payload["static_files_valid"])
            self.assertFalse(payload["trusted_install"])
            self.assertTrue(payload["python"]["available"])
            self.assertFalse(payload["python"]["supported"])
            self.assertIsNone(payload["python"]["version"])
            self.assertEqual(
                payload["receipt_ledger"]["reason"],
                "untrusted_install",
            )


class ProcessLauncherHardeningTests(unittest.TestCase):
    @staticmethod
    def _make_process_fixture(
        root: Path,
        *,
        python_path: Path,
        child_sleep_seconds: int = 3,
    ) -> Path:
        computer, user = _powershell_identity()
        contents: dict[str, bytes] = {}
        for relative_path in RUNTIME_FILES:
            if relative_path == "process_fep_now.ps1":
                continue
            if relative_path == r"frontdesk_payment_runner\runner.py":
                contents[relative_path] = (
                    "import os\n"
                    "from pathlib import Path\n"
                    "import time\n"
                    "def main(arguments=None):\n"
                    "    Path(os.environ['FR_CHILD_MARKER']).write_text("
                    "'started', encoding='utf-8')\n"
                    f"    time.sleep({int(child_sleep_seconds)})\n"
                    "    return 0\n"
                ).encode("utf-8")
            elif relative_path.endswith(".py"):
                contents[relative_path] = b"# synthetic runtime dependency\n"
            else:
                contents[relative_path] = b"synthetic runtime documentation\n"
            destination = _runtime_path(root, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents[relative_path])

        process_text = PROCESS.read_text(encoding="utf-8")
        process_text = _replace_assignment(
            process_text, "expectedComputer", computer
        )
        process_text = _replace_assignment(process_text, "expectedUser", user)
        for relative_path, payload in contents.items():
            digest = _sha256_bytes(payload)
            pattern = (
                rf'(?m)^(\s*"{re.escape(relative_path)}"\s*=\s*")'
                r"[0-9A-F]{64}"
                r'("\s*)$'
            )
            process_text, count = re.subn(
                pattern,
                rf"\g<1>{digest}\g<2>",
                process_text,
                count=1,
            )
            if count != 1:
                raise AssertionError(
                    f"Could not patch process hash for {relative_path}"
                )
        _write_lf(root / "process_fep_now.ps1", process_text)

        hashes = {
            relative_path: _sha256_path(_runtime_path(root, relative_path))
            for relative_path in RUNTIME_FILES
        }
        manifest = _installed_manifest(
            hashes,
            computer=computer,
            user=user,
        )
        _write_lf(
            root / "installed-manifest.json",
            json.dumps(manifest, sort_keys=True) + "\n",
        )
        _write_lf(
            root / "runtime.json",
            json.dumps(
                {
                    "source_root": r"C:\Gym Assistant 2.6",
                    "idle_seconds": 300,
                }
            )
            + "\n",
        )
        _write_lf(
            root / "state" / "payment-receipts.json",
            '{"schema":2,"receipts":{}}\n',
        )
        _write_lf(root / "python-path.txt", str(python_path) + "\n")
        return root / "process_fep_now.ps1"

    @staticmethod
    def _mutex_state() -> str:
        if not POWERSHELL:
            raise unittest.SkipTest("Windows PowerShell is not available")
        command = (
            "$m=[Threading.Mutex]::new("
            "$false,'Local\\DreamzFrontdeskPaymentCodeChange');"
            "try{$g=$m.WaitOne(0);"
            "if($g){'FREE';$m.ReleaseMutex()|Out-Null}else{'LOCKED'}}"
            "finally{$m.Dispose()}"
        )
        result = _run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ]
        )
        if result.returncode:
            raise AssertionError(result.stdout)
        return result.stdout.strip().splitlines()[-1]

    @staticmethod
    def _make_lifecycle_release(package: Path) -> tuple[Path, Path]:
        computer, user = _powershell_identity()
        _copy_current_release_fixture(package)
        for script_name in (
            "Install-DreamzFrontdeskPaymentRunner.ps1",
            "Restore-DreamzFrontdeskPaymentRunner.ps1",
        ):
            script = (
                package
                / "scripts"
                / "frontdesk_payment_runner"
                / script_name
            )
            text = script.read_text(encoding="utf-8")
            text = _replace_assignment(text, "expectedComputer", computer)
            text = _replace_assignment(text, "expectedUser", user)
            _write_lf(script, text)
        hashes = {
            relative_path: _sha256_path(package / Path(relative_path))
            for relative_path in PAYLOAD_FILES
        }
        (package / BUILDER.MANIFEST_NAME).write_bytes(
            BUILDER.manifest_bytes("c" * 40, hashes)
        )
        return (
            package
            / "scripts"
            / "frontdesk_payment_runner"
            / "Install-DreamzFrontdeskPaymentRunner.ps1",
            package
            / "scripts"
            / "frontdesk_payment_runner"
            / "Restore-DreamzFrontdeskPaymentRunner.ps1",
        )

    def test_launcher_holds_lifecycle_mutex_until_child_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            child_marker = base / "child-started.txt"
            script = self._make_process_fixture(
                base / "install",
                python_path=Path(sys.executable),
            )
            env = os.environ.copy()
            env["SYNC_API_TOKEN"] = "synthetic-test-credential"
            env["FR_CHILD_MARKER"] = str(child_marker)
            process = subprocess.Popen(
                [
                    POWERSHELL,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-ConfirmApply",
                    "-Limit",
                    "1",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not child_marker.exists():
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                if not child_marker.exists():
                    output, _ = process.communicate(timeout=5)
                    self.fail(f"Child never started:\n{output}")
                self.assertEqual(self._mutex_state(), "LOCKED")
                output, _ = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, output)
                self.assertEqual(self._mutex_state(), "FREE")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_launcher_rejects_tamper_before_python_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            marker = base / "python-invoked.txt"
            marker_python = _marker_command(base)
            script = self._make_process_fixture(
                base / "install",
                python_path=marker_python,
            )
            runner = (
                script.parent / "frontdesk_payment_runner" / "runner.py"
            )
            runner.write_bytes(runner.read_bytes() + b"# tampered\n")
            env = os.environ.copy()
            env["SYNC_API_TOKEN"] = "synthetic-test-credential"
            env["FR_HARDENING_MARKER"] = str(marker)
            result = _run_powershell(
                script,
                "-ConfirmApply",
                "-Limit",
                "1",
                env=env,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("Installed release hash failed", result.stdout)
            self.assertFalse(marker.exists(), result.stdout)

    def test_install_and_restore_block_while_launcher_child_is_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            child_marker = base / "child-started.txt"
            script = self._make_process_fixture(
                base / "install",
                python_path=Path(sys.executable),
                child_sleep_seconds=12,
            )
            install_script, restore_script = self._make_lifecycle_release(
                base / "release"
            )
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(base / "isolated-local-app-data")
            env["SYNC_API_TOKEN"] = "synthetic-test-credential"
            env["FR_CHILD_MARKER"] = str(child_marker)
            process = subprocess.Popen(
                [
                    POWERSHELL,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-ConfirmApply",
                    "-Limit",
                    "1",
                ],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not child_marker.exists():
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                if not child_marker.exists():
                    output, _ = process.communicate(timeout=5)
                    self.fail(f"Launcher child never started:\n{output}")

                for lifecycle_script, arguments in (
                    (
                        install_script,
                        ("-PythonPath", str(Path(sys.executable))),
                    ),
                    (
                        restore_script,
                        (
                            "-RollbackPath",
                            str(base / "not-read-while-locked"),
                            "-PythonPath",
                            str(Path(sys.executable)),
                        ),
                    ),
                ):
                    result = _run_powershell(
                        lifecycle_script,
                        *arguments,
                        env=env,
                        timeout=20,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        result.stdout,
                    )
                    self.assertIn(
                        "Another Frontdesk launcher or lifecycle operation "
                        "is active",
                        result.stdout,
                    )
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()


class RestoreTrustBoundaryTests(unittest.TestCase):
    def test_pinned_legacy_fixture_matches_exact_artifact_eol_shapes(self):
        payloads = _pinned_legacy_runtime_payloads()
        sync_payload = payloads["sync_agent.py"]
        self.assertEqual(sync_payload.count(b"\r\n"), 1713)
        self.assertEqual(sync_payload.count(b"\n"), 1725)
        for relative_path in PINNED_LEGACY_CRLF_FILES:
            payload = payloads[relative_path]
            self.assertGreater(payload.count(b"\r\n"), 0, relative_path)
            self.assertEqual(
                payload.count(b"\r\n"),
                payload.count(b"\n"),
                relative_path,
            )
        lf_only_files = (
            set(RUNTIME_FILES)
            - PINNED_LEGACY_CRLF_FILES
            - {"sync_agent.py"}
        )
        self.assertEqual(len(lf_only_files), 5)
        for relative_path in lf_only_files:
            self.assertNotIn(b"\r", payloads[relative_path], relative_path)

    @staticmethod
    def _make_restore_package(package: Path) -> tuple[Path, dict[str, str]]:
        computer, user = _powershell_identity()
        trusted_runner = (
            "import json\n"
            "def run_health_check(runtime_path):\n"
            "    print(json.dumps({'ok': True, 'receipt_count': 0}))\n"
            "    return 0\n"
        ).encode("utf-8")
        for relative_path in PAYLOAD_FILES:
            destination = package / Path(relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative_path == (
                "scripts/frontdesk_payment_runner/"
                "Restore-DreamzFrontdeskPaymentRunner.ps1"
            ):
                text = RESTORE.read_text(encoding="utf-8")
                text = _replace_assignment(text, "expectedComputer", computer)
                text = _replace_assignment(text, "expectedUser", user)
                mutex_suffix = hashlib.sha256(
                    str(package).encode("utf-8")
                ).hexdigest()[:16]
                text = _replace_assignment(
                    text,
                    "runnerMutexName",
                    rf"Local\DreamzFrontdeskRestoreTestRunner{mutex_suffix}",
                )
                text = _replace_assignment(
                    text,
                    "lifecycleMutexName",
                    rf"Local\DreamzFrontdeskRestoreTestLifecycle{mutex_suffix}",
                )
                destination.write_text(text, encoding="utf-8", newline="\n")
            elif relative_path == "frontdesk_payment_runner/runner.py":
                destination.write_bytes(trusted_runner)
            elif relative_path == "process_fep_now.ps1":
                destination.write_bytes(_canonical_source(relative_path))
            elif relative_path.endswith(".py"):
                destination.write_bytes(b"# synthetic trusted payload\n")
            elif relative_path.endswith(".ps1"):
                destination.write_bytes(b"# synthetic lifecycle payload\n")
            else:
                destination.write_bytes(b"synthetic trusted payload\n")
        runtime_hashes = {
            relative_path: _sha256_path(
                _runtime_path(package, relative_path)
            )
            for relative_path in RUNTIME_FILES
        }
        hashes = {
            relative_path: _sha256_path(package / Path(relative_path))
            for relative_path in PAYLOAD_FILES
        }
        (package / "release-manifest.json").write_bytes(
            BUILDER.manifest_bytes("b" * 40, hashes)
        )
        return (
            package
            / "scripts"
            / "frontdesk_payment_runner"
            / "Restore-DreamzFrontdeskPaymentRunner.ps1",
            hashes,
        )

    @staticmethod
    def _make_restore_state(
        base: Path,
        package: Path,
        hashes: dict[str, str],
        *,
        release_version: str,
        legacy_manifest: bool = False,
    ) -> tuple[Path, Path, bytes]:
        computer, user = _powershell_identity()
        local_app_data = base / "local-app-data"
        install = (
            local_app_data / "Dreamz" / "FrontdeskPaymentRunner"
        )
        rollback = (
            local_app_data
            / "Dreamz"
            / "FrontdeskPaymentRunnerRollback"
            / "upgrade-20200101-000000-000"
            / "install"
        )
        _write_lf(install / "runtime.json", "{}\n")
        current_ledger = (
            json.dumps(
                {
                    "schema": 2,
                    "receipts": {
                        "123": {
                            "idempotency_sha256": "a" * 64,
                            "state": "applied",
                            "writer_started_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                            "applied_at": "2026-01-01T00:00:00+00:00",
                        }
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        (install / "state").mkdir(parents=True, exist_ok=True)
        (install / "state" / "payment-receipts.json").write_bytes(
            current_ledger
        )
        _write_lf(
            install / "frontdesk_payment_runner" / "runner.py",
            (
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['FR_CURRENT_IMPORT_MARKER']).write_text("
                "'imported', encoding='utf-8')\n"
                "raise RuntimeError('current corrupt install imported')\n"
            ),
        )

        legacy_payloads = (
            _pinned_legacy_runtime_payloads()
            if legacy_manifest
            else None
        )
        runtime_hashes: dict[str, str] = {}
        for relative_path in RUNTIME_FILES:
            release_relative = relative_path.replace("\\", "/")
            destination = _runtime_path(rollback, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if legacy_payloads is not None:
                payload = legacy_payloads[relative_path]
            else:
                payload = (package / Path(release_relative)).read_bytes()
            destination.write_bytes(payload)
            runtime_hashes[relative_path] = _sha256_bytes(payload)
        snapshot_manifest = _installed_manifest(
            runtime_hashes,
            release_version=release_version,
            computer=computer,
            user=user,
        )
        if legacy_manifest:
            snapshot_manifest.pop("source_commit")
        _write_lf(
            rollback / "installed-manifest.json",
            json.dumps(snapshot_manifest, sort_keys=True) + "\n",
        )
        _write_lf(
            rollback / "runtime.json",
            json.dumps(
                {
                    "source_root": r"C:\Gym Assistant 2.6",
                    "idle_seconds": 300,
                }
            )
            + "\n",
        )
        _write_lf(
            rollback / "state" / "payment-receipts.json",
            '{"schema":2,"receipts":{}}\n',
        )
        _write_lf(
            rollback / "state" / "status.json",
            '{"state":"stale-snapshot-status"}\n',
        )
        _write_lf(
            rollback / "logs" / "runner.log",
            "stale snapshot log must not be restored\n",
        )
        _write_lf(rollback / "python-path.txt", str(sys.executable) + "\n")
        return local_app_data, rollback.parent, current_ledger

    def test_supported_restore_never_imports_current_corrupt_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package = base / "package"
            restore, hashes = self._make_restore_package(package)
            marker = base / "current-imported.txt"
            local_app_data, rollback_path, current_ledger = (
                self._make_restore_state(
                base,
                package,
                hashes,
                release_version="1.0.0",
                )
            )
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(local_app_data)
            env["FR_CURRENT_IMPORT_MARKER"] = str(marker)
            result = _run_powershell(
                restore,
                "-RollbackPath",
                str(rollback_path),
                "-PythonPath",
                str(Path(sys.executable)),
                env=env,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(marker.exists(), result.stdout)
            installed_runner = (
                local_app_data
                / "Dreamz"
                / "FrontdeskPaymentRunner"
                / "frontdesk_payment_runner"
                / "runner.py"
            )
            self.assertIn(
                "def run_health_check",
                installed_runner.read_text(encoding="utf-8"),
            )
            installed_root = (
                local_app_data / "Dreamz" / "FrontdeskPaymentRunner"
            )
            self.assertEqual(
                (
                    installed_root
                    / "state"
                    / "payment-receipts.json"
                ).read_bytes(),
                current_ledger,
            )
            self.assertFalse((installed_root / "state" / "status.json").exists())
            self.assertFalse((installed_root / "logs").exists())

    def test_pinned_legacy_snapshot_restores_with_current_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package = base / "package"
            restore, hashes = self._make_restore_package(package)
            marker = base / "current-imported.txt"
            local_app_data, rollback_path, current_ledger = (
                self._make_restore_state(
                    base,
                    package,
                    hashes,
                    release_version="1.0.0",
                    legacy_manifest=True,
                )
            )
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(local_app_data)
            env["FR_CURRENT_IMPORT_MARKER"] = str(marker)
            result = _run_powershell(
                restore,
                "-RollbackPath",
                str(rollback_path),
                "-PythonPath",
                str(Path(sys.executable)),
                env=env,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse(marker.exists(), result.stdout)
            installed_root = (
                local_app_data / "Dreamz" / "FrontdeskPaymentRunner"
            )
            self.assertEqual(
                (
                    installed_root
                    / "state"
                    / "payment-receipts.json"
                ).read_bytes(),
                current_ledger,
            )
            installed_manifest = json.loads(
                (
                    installed_root / "installed-manifest.json"
                ).read_text(encoding="utf-8")
            )
            expected_current_hashes = {
                relative_path: hashes[relative_path.replace("\\", "/")]
                for relative_path in RUNTIME_FILES
            }
            self.assertEqual(installed_manifest["source_commit"], "b" * 40)
            self.assertEqual(
                installed_manifest["file_hashes"],
                expected_current_hashes,
            )
            legacy_payloads = _pinned_legacy_runtime_payloads()
            for relative_path in RUNTIME_FILES:
                installed_payload = _runtime_path(
                    installed_root,
                    relative_path,
                ).read_bytes()
                current_payload = (
                    package / Path(relative_path.replace("\\", "/"))
                ).read_bytes()
                self.assertEqual(
                    installed_payload,
                    current_payload,
                    relative_path,
                )
            self.assertNotEqual(
                (installed_root / "process_fep_now.ps1").read_bytes(),
                legacy_payloads["process_fep_now.ps1"],
            )
            self.assertNotEqual(
                (
                    installed_root
                    / "frontdesk_payment_runner"
                    / "runner.py"
                ).read_bytes(),
                legacy_payloads[
                    r"frontdesk_payment_runner\runner.py"
                ],
            )
            installed_launcher = (
                installed_root / "process_fep_now.ps1"
            ).read_text(encoding="utf-8")
            self.assertIn("lifecycleMutexName", installed_launcher)
            self.assertIn("-B -I", installed_launcher)
            self.assertFalse((installed_root / "state" / "status.json").exists())
            self.assertFalse((installed_root / "logs").exists())

    def test_unsupported_older_snapshot_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            package = base / "package"
            restore, hashes = self._make_restore_package(package)
            marker = base / "current-imported.txt"
            local_app_data, rollback_path, _ = self._make_restore_state(
                base,
                package,
                hashes,
                release_version="0.0.0",
            )
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(local_app_data)
            env["FR_CURRENT_IMPORT_MARKER"] = str(marker)
            result = _run_powershell(
                restore,
                "-RollbackPath",
                str(rollback_path),
                "-PythonPath",
                str(Path(sys.executable)),
                env=env,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(
                "Unsupported rollback release manifest",
                result.stdout,
            )
            self.assertFalse(marker.exists(), result.stdout)

    def test_restore_static_trust_boundary_uses_release_code_not_current(self):
        text = RESTORE.read_text(encoding="utf-8")
        lowered = text.casefold()
        self.assertIn("function invoke-trustedhealthcheck", lowered)
        self.assertIn("$packageroot $runtimepath", lowered)
        self.assertIn("join-path $packageroot $relativepath", lowered)
        self.assertIn("source_commit = [string]$releasemanifest", lowered)
        self.assertNotIn("push-location $installroot", lowered)
        self.assertNotIn(
            "copy-item -literalpath (\n"
            "        join-path $snapshotinstall $relativepath",
            lowered,
        )
        self.assertNotRegex(
            lowered,
            r"-m\s+frontdesk_payment_runner\.runner",
        )


if __name__ == "__main__":
    unittest.main()
