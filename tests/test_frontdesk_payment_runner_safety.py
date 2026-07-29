from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from dreamz_office_payment_runner import runner as office_runner
from frontdesk_payment_runner import runner


TEST_SOURCE_ROOT = r"C:\Gym Assistant 2.6"


def runtime_config(**overrides):
    return runner.RuntimeConfig(source_root=TEST_SOURCE_ROOT, **overrides)


def write_runtime(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "source_root": TEST_SOURCE_ROOT,
                "idle_seconds": 300,
            }
        ),
        encoding="utf-8",
    )


def ready(_config):
    return runner.Readiness(True, "ready")


def fresh_command(limit=1, upload_id=523):
    return {
        "id": 42,
        "status": "running",
        "target_agent": runner.AGENT_ID,
        "requested_limit": limit,
        "upload_id": upload_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def queued_update(update_id=7, key="synthetic-idempotency", upload_id=523):
    return {
        "id": update_id,
        "idempotency_key": key,
        "target_agent": runner.AGENT_ID,
        "upload_id": upload_id,
        "member_id": "90001",
        "member_name": "Synthetic Member",
        "gym_billing_amount": 55.00,
        "membership_period": "2026-08",
        "target_values": {"next_payment": "2026-09-01"},
    }


class FakePortalClient:
    def __init__(self, command=None, updates=None):
        self.command = command
        self.pending_updates = list(updates or [])
        self.command_claims = 0
        self.command_results = []
        self.update_results = []

    def claim_command(self):
        self.command_claims += 1
        command, self.command = self.command, None
        return command

    def post_command_result(self, command_id, payload):
        self.command_results.append((command_id, payload))
        return {"ok": True}

    def peek_updates(self, limit=1):
        return self.pending_updates[:limit]

    def claim_updates(self, limit):
        updates = self.pending_updates[:limit]
        del self.pending_updates[:limit]
        return updates

    def post_update_result(self, update_id, payload):
        self.update_results.append((update_id, payload))
        return {"ok": True}


class FrontdeskFixedScopeTests(unittest.TestCase):
    def test_scope_and_cli_are_fixed_to_manual_one_shot(self):
        self.assertEqual(runner.AGENT_ID, "frontdesk_dreamz")
        self.assertEqual(runner.EXPECTED_COMPUTER_NAME, "DREAMZ-FRNTDSK")
        self.assertEqual(runner.EXPECTED_WINDOWS_USER, "Dreamz Fitness")
        self.assertEqual(runner.EXPECTED_SOURCE_ROOT, TEST_SOURCE_ROOT)
        parser = runner.build_parser()
        args = parser.parse_args(["--once"])
        self.assertTrue(args.once)
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_runtime_rejects_any_other_gymassistant_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "runtime.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_root": r"Z:\Gym Assistant 2.6",
                        "idle_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runner.RunnerError,
                "frontdesk_source_root_mismatch",
            ):
                runner.load_runtime_config(config_path)

    def test_wrong_windows_user_is_rejected_before_desktop_or_claim(self):
        client = FakePortalClient(command=fresh_command())
        with (
            patch.dict(
                os.environ,
                {"USERNAME": "Dreamz Fitness"},
                clear=False,
            ),
            patch.object(
                runner,
                "native_computer_name",
                return_value="DREAMZ-FRNTDSK",
            ),
            patch.object(runner, "native_windows_user", return_value="Other User"),
            patch.object(
                runner,
                "desktop_availability",
                side_effect=AssertionError("desktop must not be inspected"),
            ),
        ):
            state, summary = runner.process_available_command(
                client,
                runner.ReceiptStore(Path("unused-receipts.json")),
                runtime_config(),
            )
        self.assertEqual(state, "unexpected_windows_user")
        self.assertEqual(summary.received, 0)
        self.assertEqual(client.command_claims, 0)

    def test_environment_cannot_spoof_native_host_identity(self):
        client = FakePortalClient(command=fresh_command())
        with (
            patch.dict(
                os.environ,
                {"COMPUTERNAME": "DREAMZ-FRNTDSK"},
                clear=False,
            ),
            patch.object(
                runner,
                "native_computer_name",
                return_value="NOT-THE-FRONTDESK",
            ),
            patch.object(
                runner,
                "desktop_availability",
                side_effect=AssertionError("desktop must not be inspected"),
            ),
        ):
            state, summary = runner.process_available_command(
                client,
                runner.ReceiptStore(Path("unused-receipts.json")),
                runtime_config(),
            )
        self.assertEqual(state, "unexpected_computer")
        self.assertEqual(summary.received, 0)
        self.assertEqual(client.command_claims, 0)


@unittest.skipUnless(os.name == "nt", "Windows named-mutex semantics required")
class FrontdeskMutexInteropTests(unittest.TestCase):
    @staticmethod
    def dotnet_mutex_can_acquire(name: str) -> bool:
        escaped_name = name.replace("'", "''")
        script = (
            "$created = $false; "
            f"$mutex = [Threading.Mutex]::new($false, '{escaped_name}', [ref]$created); "
            "$taken = $false; "
            "try { "
            "  $taken = $mutex.WaitOne(0); "
            "  if ($taken) { exit 0 } else { exit 5 } "
            "} finally { "
            "  if ($taken) { $mutex.ReleaseMutex() | Out-Null }; "
            "  $mutex.Dispose() "
            "}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode not in (0, 5):
            raise AssertionError(
                f"PowerShell mutex probe failed: {result.returncode} "
                f"{result.stderr.strip()}"
            )
        return result.returncode == 0

    def test_runner_owns_mutex_and_blocks_dotnet_install_restore_lock(self):
        for runner_module in (runner, office_runner):
            with self.subTest(runner=runner_module.AGENT_ID):
                name = rf"Local\DreamzPaymentTest-{uuid.uuid4()}"
                mutex = runner_module.NamedMutex(name)
                self.assertTrue(mutex.acquire())
                try:
                    self.assertFalse(self.dotnet_mutex_can_acquire(name))
                finally:
                    mutex.close()
                self.assertTrue(self.dotnet_mutex_can_acquire(name))

    def test_environment_spoofing_does_not_change_native_identity(self):
        native_host = runner.native_computer_name()
        native_user = runner.native_windows_user()
        self.assertTrue(native_host)
        self.assertTrue(native_user)
        with patch.dict(
            os.environ,
            {
                "COMPUTERNAME": "DREAMZ-FRNTDSK",
                "USERNAME": "Dreamz Fitness",
                "USER": "Dreamz Fitness",
                "LOGNAME": "Dreamz Fitness",
            },
            clear=False,
        ):
            self.assertEqual(runner.native_computer_name(), native_host)
            self.assertEqual(runner.native_windows_user(), native_user)


class FrontdeskCrashSafetyTests(unittest.TestCase):
    def test_empty_malformed_or_non_boolean_writer_success_is_ambiguous(self):
        unsafe_results = (
            None,
            "",
            [],
            {},
            {"status": "applied"},
            {"status": "applied", "applied": 1},
            {"status": "applied", "applied": "true"},
            {"applied": True},
        )
        for index, unsafe_result in enumerate(unsafe_results, start=1):
            with self.subTest(result=repr(unsafe_result)):
                update = queued_update(index, f"key-{index}")
                client = FakePortalClient()
                with tempfile.TemporaryDirectory() as temporary:
                    ledger_path = Path(temporary) / "receipts.json"
                    receipts = runner.ReceiptStore(ledger_path)
                    outcome = runner.process_one_update(
                        client,
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=Mock(return_value=unsafe_result),
                    )
                    receipts = runner.ReceiptStore(ledger_path)
                    self.assertEqual(
                        receipts.state(index, f"key-{index}"),
                        "ambiguous",
                    )
                    retry_writer = Mock(
                        side_effect=AssertionError("must not click twice")
                    )
                    retry = runner.process_one_update(
                        FakePortalClient(),
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=retry_writer,
                    )
                self.assertEqual(outcome, "ambiguous")
                self.assertEqual(retry, "ambiguous")
                retry_writer.assert_not_called()
                self.assertEqual(
                    client.update_results[-1][1]["error"],
                    "manual_reconciliation_required",
                )

    def test_crash_receipt_writer_started_never_clicks_again(self):
        update = queued_update()
        client = FakePortalClient()
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            receipts.record_writer_started(7, "synthetic-idempotency")
            writer = Mock(side_effect=AssertionError("must reconcile"))
            outcome = runner.process_one_update(
                client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=writer,
            )
        self.assertEqual(outcome, "ambiguous")
        writer.assert_not_called()
        self.assertEqual(
            client.update_results[-1][1]["error"],
            "manual_reconciliation_required",
        )

    def test_ack_loss_retries_ack_only_and_never_writer(self):
        update = queued_update()

        class LostAckClient(FakePortalClient):
            def post_update_result(self, update_id, payload):
                raise runner.PortalApiError("portal_unreachable")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(path)
            writer = Mock(return_value={"status": "applied", "applied": True})
            with patch.object(runner.time, "sleep"):
                with self.assertRaises(runner.AcknowledgementPending):
                    runner.process_one_update(
                        LostAckClient(),
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=writer,
                    )
            writer.assert_called_once()
            receipts = runner.ReceiptStore(path)
            self.assertEqual(
                receipts.state(7, "synthetic-idempotency"),
                "applied",
            )
            retry_writer = Mock(side_effect=AssertionError("must not click"))
            retry_client = FakePortalClient()
            outcome = runner.process_one_update(
                retry_client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=retry_writer,
            )
        self.assertEqual(outcome, "applied")
        retry_writer.assert_not_called()
        self.assertEqual(
            retry_client.update_results[-1][1]["verification"],
            "recovered_local_receipt",
        )

    def test_first_retry_safe_defer_stops_command_before_second_update(self):
        updates = [
            queued_update(7, "key-7"),
            queued_update(8, "key-8"),
        ]
        client = FakePortalClient(
            command=fresh_command(limit=2),
            updates=updates,
        )
        writer = Mock(
            return_value={
                "status": "deferred",
                "applied": False,
                "reason": "payment_dialog_did_not_open",
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
            )
        self.assertEqual(state, "payment_deferred")
        self.assertEqual(summary.received, 1)
        self.assertEqual(summary.deferred, 1)
        self.assertEqual(len(client.pending_updates), 1)
        writer.assert_called_once()
        self.assertEqual(client.command_results[-1][1]["status"], "failed")
        self.assertNotIn(
            "completed",
            [payload["status"] for _command_id, payload in client.command_results],
        )

    def test_first_malformed_result_stops_command_before_second_update(self):
        updates = [
            queued_update(7, "key-7"),
            queued_update(8, "key-8"),
        ]
        client = FakePortalClient(
            command=fresh_command(limit=2),
            updates=updates,
        )
        writer = Mock(return_value={})
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
            )
        self.assertEqual(state, "manual_reconciliation_required")
        self.assertEqual(summary.received, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(len(client.pending_updates), 1)
        writer.assert_called_once()
        self.assertEqual(client.command_results[-1][1]["status"], "failed")


class FrontdeskOneShotLifecycleTests(unittest.TestCase):
    def test_one_shot_maps_every_unsafe_state_to_nonzero_and_clears_token(self):
        cases = {
            "completed": 0,
            "idle": 0,
            "payment_result_ack_pending": 4,
            "payment_deferred": 3,
            "manual_reconciliation_required": 3,
            "portal_unreachable_during_command": 3,
            "stop_requested": 3,
        }
        for state, expected_exit in cases.items():
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    (root / "state").mkdir()
                    (root / "state" / "payment-receipts.json").write_text(
                        '{"schema":2,"receipts":{}}',
                        encoding="utf-8",
                    )
                    config_path = root / "runtime.json"
                    write_runtime(config_path)
                    mutex = Mock()
                    mutex.acquire.return_value = True
                    with (
                        patch.object(runner, "NamedMutex", return_value=mutex),
                        patch.object(
                            runner,
                            "process_available_command",
                            return_value=(state, runner.CommandSummary()),
                        ) as process,
                        patch.dict(
                            os.environ,
                            {"FEP_PAYMENT_RUNNER_TOKEN": "synthetic-token"},
                            clear=False,
                        ),
                    ):
                        exit_code = runner.run_once(
                            config_path,
                            client_factory=lambda _token, _timeout: FakePortalClient(),
                        )
                        self.assertNotIn(
                            "FEP_PAYMENT_RUNNER_TOKEN",
                            os.environ,
                        )
                self.assertEqual(exit_code, expected_exit)
                process.assert_called_once()
                mutex.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
