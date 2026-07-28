from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dreamz_office_payment_runner import runner


TEST_SOURCE_ROOT = r"Y:\Gym Assistant 2.6"


def runtime_config(**overrides):
    return runner.RuntimeConfig(source_root=TEST_SOURCE_ROOT, **overrides)


def write_runtime_config(path: Path, **overrides):
    payload = {
        "source_root": TEST_SOURCE_ROOT,
        "idle_seconds": 300,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


class FakePortalClient:
    def __init__(self, command=None, updates=None):
        self.command = command
        self.pending_updates = list(updates or [])
        self.command_claims = 0
        self.command_results = []
        self.update_results = []
        self.peek_limits = []
        self.claim_limits = []

    def claim_command(self):
        self.command_claims += 1
        command, self.command = self.command, None
        return command

    def post_command_result(self, command_id, payload):
        self.command_results.append((command_id, payload))
        return {"ok": True}

    def peek_updates(self, limit=1):
        self.peek_limits.append(limit)
        return self.pending_updates[:limit]

    def claim_updates(self, limit):
        self.claim_limits.append(limit)
        updates = self.pending_updates[:limit]
        del self.pending_updates[:limit]
        return updates

    def post_update_result(self, update_id, payload):
        self.update_results.append((update_id, payload))
        return {"ok": True}


def ready(_config):
    return runner.Readiness(True, "ready")


def fresh_command(command_id=42, limit=1, upload_id=523):
    return {
        "id": command_id,
        "status": "running",
        "target_agent": runner.AGENT_ID,
        "requested_limit": limit,
        "upload_id": upload_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def queued_update(
    update_id=7,
    idempotency_key="test-idempotency-key",
    upload_id=523,
):
    return {
        "id": update_id,
        "idempotency_key": idempotency_key,
        "target_agent": runner.AGENT_ID,
        "upload_id": upload_id,
        "member_id": "34203",
        "member_name": "Sensitive Name",
        "gym_billing_amount": 61.00,
        "membership_period": "2026-08",
        "target_values": {"next_payment": "2026-09-01"},
    }


class FakeHttpResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self, _limit):
        return self.raw


class FixedScopeTests(unittest.TestCase):
    def test_runner_scope_is_fixed(self):
        self.assertEqual(runner.AGENT_ID, "dreamz_office")
        self.assertEqual(runner.AGENT_LABEL, "Dreamz Office PC")
        self.assertEqual(runner.EXPECTED_COMPUTER_NAME, "DREAMZ-OFFICE-P")
        self.assertEqual(
            runner.PORTAL_URL,
            "https://dreamzmemberportal-production.up.railway.app",
        )
        config = runtime_config()
        self.assertEqual(config.idle_seconds, 300)
        self.assertEqual(
            config.expected_data_root,
            r"Y:\Gym Assistant 2.6\Data",
        )

    def test_cli_has_no_agent_source_or_token_override(self):
        parser = runner.build_parser()
        option_names = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("--agent-id", option_names)
        self.assertNotIn("--source-root", option_names)
        self.assertNotIn("--sync-token", option_names)
        self.assertNotIn("--portal-url", option_names)
        self.assertNotIn("--once", option_names)
        self.assertIn("--health-check", option_names)

    def test_runtime_config_rejects_scope_and_secret_fields(self):
        for field in (
            "agent_id",
            "expected_data_root",
            "portal_url",
            "sync_token",
            "payment_token",
            "token",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "runtime.json"
                path.write_text(
                    json.dumps(
                        {
                            "source_root": TEST_SOURCE_ROOT,
                            field: "forbidden",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    runner.RunnerError,
                    "runtime_config_contains_fixed_or_secret_field",
                ):
                    runner.load_runtime_config(path)

    def test_runtime_config_requires_exact_absolute_nontransfer_source(self):
        invalid = {
            "": "source_root_missing",
            r"relative\Gym Assistant": "source_root_must_be_absolute",
            "C:": "source_root_must_be_absolute",
            r"1:\Gym Assistant": "source_root_must_be_absolute",
            r"\\DREAMZ-FRNTDSK": "source_root_must_be_absolute",
            r"\\DREAMZ-FRNTDSK\.": "source_root_must_be_absolute",
            r"Y:\Gym Assistant\..\Other": "source_root_invalid",
            r"\\DREAMZ-FRNTDSK\Gym Assistant 2.6\..\Other": (
                "source_root_invalid"
            ),
            r"Y:\Gym Assistant:Other": "source_root_invalid",
            r"\\DREAMZ-FRNTDSK\Gym Assistant:Other": (
                "source_root_must_be_absolute"
            ),
            r"X:\Runtime": "source_root_reserved_for_transfer",
            r"\\DREAMZ-OFFICE-P\Shared Operations": (
                "source_root_reserved_for_transfer"
            ),
            r"\\DREAMZ-OFFICE-P\Shared Operations\Runtime": (
                "source_root_reserved_for_transfer"
            ),
        }
        for source_root, reason in invalid.items():
            with self.subTest(source_root=source_root):
                with self.assertRaisesRegex(runner.RunnerError, reason):
                    runner.normalize_configured_source_root(source_root)

    def test_runtime_config_accepts_exact_unc_share_root(self):
        source_root = r"\\DREAMZ-FRNTDSK\Gym Assistant 2.6"

        self.assertEqual(
            runner.normalize_configured_source_root(source_root),
            source_root,
        )

        config = runner.RuntimeConfig(source_root=source_root)
        self.assertEqual(
            config.expected_data_root,
            source_root + r"\Data",
        )

    def test_runtime_config_loads_installer_recorded_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "source_root": TEST_SOURCE_ROOT,
                        "idle_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            config = runner.load_runtime_config(path)

        self.assertEqual(config.source_root, TEST_SOURCE_ROOT)
        self.assertEqual(config.expected_data_root, TEST_SOURCE_ROOT + r"\Data")
        self.assertEqual(config.idle_seconds, 300)

    def test_runtime_config_cannot_lower_office_idle_safety_floor(self):
        for configured_idle in (1, 299, 301, 3600):
            with self.subTest(configured_idle=configured_idle):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "runtime.json"
                    path.write_text(
                        json.dumps(
                            {
                                "source_root": TEST_SOURCE_ROOT,
                                "idle_seconds": configured_idle,
                            }
                        ),
                        encoding="utf-8",
                    )
                    config = runner.load_runtime_config(path)

                self.assertEqual(config.idle_seconds, 300)

    def test_no_member_sync_or_scan_code_is_referenced(self):
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import sync_agent", source)
        self.assertNotIn("scan_source", source)
        self.assertNotIn("push_members", source)
        self.assertNotIn("/api/sync/members", source)
        self.assertNotIn("/api/sync/files", source)

    def test_portal_client_claims_only_fixed_agent_command_endpoint(self):
        response = FakeHttpResponse(
            {
                "commands": [
                    {
                        "id": 42,
                        "target_agent": "dreamz_office",
                        "requested_limit": 1,
                    }
                ]
            }
        )
        with patch.object(
            runner.urlrequest,
            "urlopen",
            return_value=response,
        ) as urlopen:
            client = runner.PortalClient("unit-test-token", 20)
            command = client.claim_command()

        self.assertEqual(command["id"], 42)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIn(
            "/api/sync/fep-payment-process-commands?agent_id=dreamz_office",
            request.full_url,
        )
        self.assertNotIn("unit-test-token", request.full_url)
        self.assertEqual(request.get_header("X-sync-agent"), "dreamz_office")
        self.assertEqual(request.get_header("X-sync-token"), "unit-test-token")

    def test_portal_client_posts_payment_result_without_secret_in_body_or_url(self):
        response = FakeHttpResponse({"ok": True})
        with patch.object(
            runner.urlrequest,
            "urlopen",
            return_value=response,
        ) as urlopen:
            client = runner.PortalClient("unit-test-token", 20)
            client.post_update_result(7, {"status": "applied"})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertIn(
            "/api/sync/fep-payment-updates/7/result?agent_id=dreamz_office",
            request.full_url,
        )
        self.assertNotIn("unit-test-token", request.full_url)
        self.assertNotIn(b"unit-test-token", request.data)
        self.assertEqual(json.loads(request.data)["agent_id"], "dreamz_office")

    def test_portal_client_peeks_one_update_without_claiming(self):
        response = FakeHttpResponse({"updates": [queued_update()]})
        with patch.object(
            runner.urlrequest,
            "urlopen",
            return_value=response,
        ) as urlopen:
            client = runner.PortalClient("unit-test-token", 20)
            updates = client.peek_updates(1)

        self.assertEqual(len(updates), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertIn("limit=1", request.full_url)
        self.assertIn("agent_id=dreamz_office", request.full_url)
        self.assertIn("peek=1", request.full_url)

    def test_official_writer_keeps_nonzero_idle_guard(self):
        with patch.object(
            runner.payment_writer,
            "run_writer",
            return_value={"status": "applied", "applied": True},
        ) as run_writer:
            runner.apply_official_writer(
                queued_update(),
                30,
                300,
                TEST_SOURCE_ROOT,
            )

        self.assertEqual(
            run_writer.call_args.kwargs["require_idle_seconds"],
            300.0,
        )
        self.assertEqual(
            run_writer.call_args.args[0]["source_root"],
            TEST_SOURCE_ROOT,
        )


class PreflightTests(unittest.TestCase):
    def test_exact_title_parser_supports_installer_selected_data_root(self):
        self.assertEqual(
            runner.gymassistant_title_data_path(
                r"Gym Assistant 2.6 [Path=Y:\Gym Assistant 2.6\Data]"
            ),
            runner.normalize_windows_path(
                r"Y:\Gym Assistant 2.6\Data"
            ),
        )
        self.assertNotEqual(
            runner.gymassistant_title_data_path(
                r"Gym Assistant 2.6 [Path=C:\Gym Assistant 2.6\Data]"
            ),
            runner.normalize_windows_path(
                r"Y:\Gym Assistant 2.6\Data"
            ),
        )
        self.assertIsNone(
            runner.gymassistant_title_data_path(r"Gym Assistant 2.6")
        )

    def test_locked_desktop_does_not_claim_command(self):
        client = FakePortalClient(command=fresh_command())
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=lambda _idle: runner.Readiness(
                    False,
                    "desktop_locked",
                ),
            )

        self.assertEqual(state, "desktop_locked")
        self.assertEqual(summary.received, 0)
        self.assertEqual(client.command_claims, 0)
        self.assertEqual(client.command_results, [])

    def test_unexpected_computer_is_rejected_before_desktop_inspection(self):
        with (
            patch.dict(
                runner.os.environ,
                {"COMPUTERNAME": "NOT-THE-OFFICE-PC"},
                clear=False,
            ),
            patch.object(
                runner,
                "desktop_availability",
                side_effect=AssertionError("desktop must not be inspected"),
            ),
        ):
            result = runner.workstation_readiness(runtime_config())

        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "unexpected_computer")

    def test_missing_exact_window_does_not_claim_command(self):
        client = FakePortalClient(command=fresh_command())
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, _summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=lambda _idle: runner.Readiness(
                    False,
                    "expected_gymassistant_window_missing",
                ),
            )

        self.assertEqual(state, "expected_gymassistant_window_missing")
        self.assertEqual(client.command_claims, 0)

    def test_workstation_preflight_checks_exactly_one_window(self):
        with (
            patch.dict(
                runner.os.environ,
                {"COMPUTERNAME": runner.EXPECTED_COMPUTER_NAME},
                clear=False,
            ),
            patch.object(
                runner,
                "desktop_availability",
                return_value=runner.Readiness(True, "ready"),
            ),
            patch.object(runner, "source_is_available", return_value=True),
            patch.object(runner, "payment_dialog_is_open", return_value=False),
            patch.object(
                runner.payment_writer,
                "desktop_idle_seconds",
                return_value=100,
            ),
            patch.object(runner, "exact_gymassistant_window_count", return_value=2),
        ):
            result = runner.workstation_readiness(runtime_config())

        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "expected_gymassistant_window_ambiguous")


class CommandProcessingTests(unittest.TestCase):
    def test_stale_command_is_failed_before_update_claim(self):
        command = fresh_command()
        command["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        client = FakePortalClient(command=command, updates=[queued_update()])

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(command_ttl_seconds=900),
                readiness_check=ready,
            )

        self.assertEqual(state, "command_expired")
        self.assertEqual(summary.received, 0)
        self.assertEqual(len(client.pending_updates), 1)
        self.assertEqual(client.command_results[0][1]["status"], "failed")
        self.assertEqual(
            client.command_results[0][1]["error"],
            "command_expired",
        )

    def test_fresh_command_uses_official_writer_and_reports_counts(self):
        update = queued_update()
        client = FakePortalClient(
            command=fresh_command(limit=1),
            updates=[update],
        )
        writer = Mock(
            return_value={
                "status": "applied",
                "applied": True,
                "member_id": update["member_id"],
                "observed": {"sensitive": "not forwarded"},
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

        self.assertEqual(state, "completed")
        self.assertEqual(summary.public(), {
            "received": 1,
            "applied": 1,
            "failed": 0,
            "deferred": 0,
        })
        writer.assert_called_once_with(
            update,
            30,
            300,
            TEST_SOURCE_ROOT,
        )
        self.assertEqual(client.peek_limits, [1])
        self.assertEqual(client.claim_limits, [1])
        self.assertEqual(client.update_results[0][1]["status"], "applied")
        self.assertNotIn("member_id", client.update_results[0][1])
        self.assertNotIn("observed", client.update_results[0][1])
        self.assertEqual(client.command_results[-1][1]["status"], "completed")
        self.assertEqual(receipts.count(), 0)

    def test_two_payments_are_peeked_and_claimed_one_at_a_time(self):
        updates = [
            queued_update(7, "key-7"),
            queued_update(8, "key-8"),
        ]
        client = FakePortalClient(
            command=fresh_command(limit=2),
            updates=updates,
        )
        writer = Mock(return_value={"status": "applied", "applied": True})

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
            )

        self.assertEqual(state, "completed")
        self.assertEqual(summary.applied, 2)
        self.assertEqual(client.peek_limits, [1, 1])
        self.assertEqual(client.claim_limits, [1, 1])
        self.assertEqual(writer.call_count, 2)

    def test_stop_between_payments_fails_command_with_partial_summary(self):
        stop = {"requested": False}
        updates = [
            queued_update(7, "key-7"),
            queued_update(8, "key-8"),
        ]
        client = FakePortalClient(
            command=fresh_command(limit=2),
            updates=updates,
        )

        def writer(_update, _timeout, _idle, _source_root):
            stop["requested"] = True
            return {"status": "applied", "applied": True}

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
                stop_check=lambda: stop["requested"],
            )

        self.assertEqual(state, "stop_requested")
        self.assertEqual(summary.public(), {
            "received": 1,
            "applied": 1,
            "failed": 0,
            "deferred": 0,
        })
        self.assertEqual(client.claim_limits, [1])
        self.assertEqual(len(client.pending_updates), 1)
        command_result = client.command_results[-1][1]
        self.assertEqual(command_result["status"], "failed")
        self.assertEqual(command_result["error"], "stop_requested")
        self.assertEqual(command_result["summary"]["applied"], 1)

    def test_peek_upload_mismatch_fails_before_claim_or_writer(self):
        update = queued_update(upload_id=999)
        client = FakePortalClient(
            command=fresh_command(limit=1, upload_id=523),
            updates=[update],
        )
        writer = Mock(side_effect=AssertionError("writer must remain closed"))

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
            )

        self.assertEqual(state, "command_upload_scope_mismatch")
        self.assertEqual(summary.received, 0)
        self.assertEqual(client.claim_limits, [])
        writer.assert_not_called()
        self.assertEqual(
            client.command_results[-1][1]["error"],
            "command_upload_scope_mismatch",
        )

    def test_claim_changed_after_matching_peek_is_deferred_without_writer(self):
        class ChangedClaimClient(FakePortalClient):
            def peek_updates(self, limit=1):
                self.peek_limits.append(limit)
                return [queued_update(7, "key-7", upload_id=523)]

            def claim_updates(self, limit):
                self.claim_limits.append(limit)
                return [queued_update(8, "key-8", upload_id=999)]

        client = ChangedClaimClient(command=fresh_command(upload_id=523))
        writer = Mock(side_effect=AssertionError("writer must remain closed"))

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, _summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
                writer_call=writer,
            )

        self.assertEqual(state, "command_upload_scope_mismatch")
        writer.assert_not_called()
        self.assertEqual(client.update_results[-1][1]["status"], "deferred")
        self.assertEqual(
            client.update_results[-1][1]["reason"],
            "command_upload_scope_mismatch",
        )

    def test_missing_command_upload_scope_is_failed_without_peek(self):
        command = fresh_command()
        command.pop("upload_id")
        client = FakePortalClient(command=command, updates=[queued_update()])

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, _summary = runner.process_available_command(
                client,
                receipts,
                runtime_config(),
                readiness_check=ready,
            )

        self.assertEqual(state, "command_upload_scope_missing")
        self.assertEqual(client.peek_limits, [])
        self.assertEqual(
            client.command_results[-1][1]["error"],
            "command_upload_scope_missing",
        )

    def test_local_receipt_prevents_second_ui_write_after_lost_ack(self):
        update = queued_update()

        class FailingAckClient(FakePortalClient):
            def post_update_result(self, update_id, payload):
                raise runner.PortalApiError("portal_unreachable")

        with tempfile.TemporaryDirectory() as temporary:
            receipt_path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(receipt_path)
            first_client = FailingAckClient()
            writer = Mock(return_value={"status": "applied", "applied": True})

            with patch.object(runner.time, "sleep"):
                with self.assertRaisesRegex(
                    runner.AcknowledgementPending,
                    "payment_result_ack_pending",
                ):
                    runner.process_one_update(
                        first_client,
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=writer,
                    )

            self.assertEqual(receipts.count(), 1)
            writer.assert_called_once()
            receipts = runner.ReceiptStore(receipt_path)
            self.assertEqual(
                receipts.state(7, "test-idempotency-key"),
                "applied",
            )

            second_client = FakePortalClient()
            second_writer = Mock(
                side_effect=AssertionError("writer must not run twice")
            )
            outcome = runner.process_one_update(
                second_client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=second_writer,
            )

            self.assertEqual(outcome, "applied")
            second_writer.assert_not_called()
            self.assertEqual(
                second_client.update_results[0][1]["verification"],
                "recovered_local_receipt",
            )
            self.assertEqual(receipts.count(), 0)

    def test_writer_exception_is_durable_ambiguous_and_never_retried(self):
        update = queued_update()
        client = FakePortalClient()
        raw_error = (
            "Member #34203 Sensitive Name amount $61.00 does not match expected."
        )

        with tempfile.TemporaryDirectory() as temporary:
            receipt_path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(receipt_path)
            outcome = runner.process_one_update(
                client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=Mock(side_effect=RuntimeError(raw_error)),
            )

            receipts = runner.ReceiptStore(receipt_path)
            self.assertEqual(
                receipts.state(7, "test-idempotency-key"),
                "ambiguous",
            )

            retry_writer = Mock(
                side_effect=AssertionError("ambiguous writer must not run twice")
            )
            retry_client = FakePortalClient()
            retry_outcome = runner.process_one_update(
                retry_client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=retry_writer,
            )

        self.assertEqual(outcome, "ambiguous")
        self.assertEqual(retry_outcome, "ambiguous")
        retry_writer.assert_not_called()
        payload = client.update_results[0][1]
        self.assertEqual(
            payload["error"],
            "manual_reconciliation_required",
        )
        serialized = json.dumps(payload)
        self.assertNotIn("34203", serialized)
        self.assertNotIn("Sensitive Name", serialized)
        self.assertNotIn("61.00", serialized)


class SanitizedStateTests(unittest.TestCase):
    def test_status_contains_only_operational_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.json"
            status = runner.StatusStore(path, TEST_SOURCE_ROOT)
            status.update(
                "completed",
                summary=runner.CommandSummary(
                    received=19,
                    applied=18,
                    failed=0,
                    deferred=1,
                ),
                receipt_count=0,
            )
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertEqual(payload["agent_id"], "dreamz_office")
        self.assertEqual(payload["source_root"], TEST_SOURCE_ROOT)
        self.assertEqual(payload["last_summary"]["applied"], 18)
        for forbidden in ("member_id", "member_name", "amount", "sync_token"):
            self.assertNotIn(forbidden, raw.casefold())

    def test_receipt_contains_hash_not_idempotency_key_or_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(path)
            receipts.record_writer_started(7, "secret-idempotency-value")
            receipts.mark_applied(7, "secret-idempotency-value")
            raw = path.read_text(encoding="utf-8")

        self.assertNotIn("secret-idempotency-value", raw)
        self.assertNotIn("member", raw.casefold())
        self.assertIn("idempotency_sha256", raw)

    def test_duplicate_launcher_does_not_overwrite_live_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "runtime.json"
            mutex = Mock()
            mutex.acquire.return_value = False
            with (
                patch.object(runner, "NamedMutex", return_value=mutex),
                patch.dict(
                    runner.os.environ,
                    {"FEP_PAYMENT_RUNNER_TOKEN": "unit-test-token"},
                    clear=False,
                ),
            ):
                exit_code = runner.run_loop(config_path)

            self.assertEqual(exit_code, 0)
            self.assertFalse(
                (Path(temporary) / "state" / "status.json").exists()
            )
            mutex.close.assert_called_once()

    def test_runner_publishes_starting_pid_before_loading_ledger_or_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "runtime.json"
            write_runtime_config(config_path)
            mutex = Mock()
            mutex.acquire.return_value = True
            status = Mock()
            client_factory = Mock(
                side_effect=AssertionError("client must not be created")
            )

            def fail_ledger_load(*_args, **_kwargs):
                self.assertEqual(
                    status.update.call_args_list[0].args[0],
                    "starting",
                )
                raise runner.ReceiptLedgerError("receipt_ledger_missing")

            with (
                patch.object(runner, "NamedMutex", return_value=mutex),
                patch.object(runner, "StatusStore", return_value=status),
                patch.object(
                    runner,
                    "ReceiptStore",
                    side_effect=fail_ledger_load,
                ),
                patch.dict(
                    runner.os.environ,
                    {"FEP_PAYMENT_RUNNER_TOKEN": "unit-test-token"},
                    clear=False,
                ),
            ):
                exit_code = runner.run_loop(
                    config_path,
                    client_factory=client_factory,
                )

        self.assertEqual(exit_code, 3)
        self.assertEqual(
            [call.args[0] for call in status.update.call_args_list],
            ["starting", "stopped"],
        )
        self.assertEqual(
            status.update.call_args_list[-1].kwargs["reason"],
            "receipt_ledger_missing",
        )
        client_factory.assert_not_called()
        mutex.close.assert_called_once()

    def test_health_check_is_local_and_nonmutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "runtime.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_root": TEST_SOURCE_ROOT,
                        "idle_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            ledger = Path(temporary) / "state" / "payment-receipts.json"
            runner.atomic_write_json(
                ledger,
                {"schema": 2, "receipts": {}},
                durable=True,
            )
            output = io.StringIO()
            with (
                patch("sys.stdout", output),
                patch.object(
                    runner.urlrequest,
                    "urlopen",
                    side_effect=AssertionError("health check must not use network"),
                ),
                patch.object(
                    runner,
                    "workstation_readiness",
                    side_effect=AssertionError("health check must not inspect UI"),
                ),
            ):
                exit_code = runner.run_health_check(config_path)

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["network_checked"])
        self.assertFalse(payload["ui_checked"])
        self.assertEqual(payload["idle_seconds"], 300)
        self.assertEqual(payload["source_root"], TEST_SOURCE_ROOT)
        self.assertEqual(
            payload["expected_data_root"],
            TEST_SOURCE_ROOT + r"\Data",
        )

    def test_health_check_accepts_exact_unc_share_root(self):
        source_root = r"\\DREAMZ-FRNTDSK\Gym Assistant 2.6"
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "runtime.json"
            config_path.write_text(
                json.dumps(
                    {
                        "source_root": source_root,
                        "idle_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            ledger = Path(temporary) / "state" / "payment-receipts.json"
            runner.atomic_write_json(
                ledger,
                {"schema": 2, "receipts": {}},
                durable=True,
            )
            output = io.StringIO()
            with (
                patch("sys.stdout", output),
                patch.object(
                    runner.urlrequest,
                    "urlopen",
                    side_effect=AssertionError("health check must not use network"),
                ),
                patch.object(
                    runner,
                    "workstation_readiness",
                    side_effect=AssertionError("health check must not inspect UI"),
                ),
            ):
                exit_code = runner.run_health_check(config_path)

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_root"], source_root)
        self.assertEqual(
            payload["expected_data_root"],
            source_root + r"\Data",
        )


class ReceiptLedgerFailClosedTests(unittest.TestCase):
    def test_corrupt_json_blocks_ledger_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            path.write_text('{"schema":2,"receipts":', encoding="utf-8")
            with self.assertRaisesRegex(
                runner.ReceiptLedgerError,
                "receipt_ledger_invalid",
            ):
                runner.ReceiptStore(path)

    def test_partially_invalid_ledger_blocks_instead_of_skipping_record(self):
        valid_digest = "a" * 64
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": 2,
            "receipts": {
                "7": {
                    "idempotency_sha256": valid_digest,
                    "state": "writer_started",
                    "writer_started_at": timestamp,
                    "updated_at": timestamp,
                },
                "8": {
                    "idempotency_sha256": "not-a-hash",
                    "state": "applied",
                    "writer_started_at": timestamp,
                    "updated_at": timestamp,
                    "applied_at": timestamp,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                runner.ReceiptLedgerError,
                "receipt_ledger_invalid",
            ):
                runner.ReceiptStore(path)

    def test_noncanonical_update_id_blocks_instead_of_hiding_receipt(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": 2,
            "receipts": {
                "07": {
                    "idempotency_sha256": "a" * 64,
                    "state": "writer_started",
                    "writer_started_at": timestamp,
                    "updated_at": timestamp,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                runner.ReceiptLedgerError,
                "receipt_ledger_invalid",
            ):
                runner.ReceiptStore(path)

    def test_state_specific_extra_fields_block_corrupt_ledger(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": 2,
            "receipts": {
                "7": {
                    "idempotency_sha256": "a" * 64,
                    "state": "writer_started",
                    "writer_started_at": timestamp,
                    "updated_at": timestamp,
                    "applied_at": timestamp,
                },
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                runner.ReceiptLedgerError,
                "receipt_ledger_invalid",
            ):
                runner.ReceiptStore(path)

    def test_v1_applied_receipt_is_preserved_as_safe_applied_state(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        key = "legacy-idempotency"
        payload = {
            "schema": 1,
            "receipts": {
                "7": {
                    "idempotency_sha256": runner.hashlib.sha256(
                        key.encode("utf-8")
                    ).hexdigest(),
                    "applied_at": timestamp,
                }
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            receipts = runner.ReceiptStore(path)

        self.assertEqual(receipts.state(7, key), "applied")

    def test_disk_failure_before_writer_blocks_ui_call(self):
        update = queued_update()
        client = FakePortalClient()
        writer = Mock(return_value={"status": "applied", "applied": True})
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(
                Path(temporary) / "receipts.json"
            )
            with patch.object(
                runner,
                "atomic_write_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(
                    runner.ReceiptLedgerError,
                    "receipt_ledger_write_failed",
                ):
                    runner.process_one_update(
                        client,
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=writer,
                    )

        writer.assert_not_called()
        self.assertEqual(client.update_results[-1][1]["status"], "deferred")
        self.assertEqual(
            client.update_results[-1][1]["reason"],
            "local_safety_ledger_unavailable",
        )

    def test_writer_started_is_durable_before_writer_invocation(self):
        update = queued_update()
        client = FakePortalClient()
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(
                Path(temporary) / "receipts.json"
            )

            def writer(_update, _timeout, _idle, _source_root):
                self.assertEqual(
                    receipts.state(7, "test-idempotency-key"),
                    "writer_started",
                )
                return {"status": "applied", "applied": True}

            outcome = runner.process_one_update(
                client,
                update,
                receipts,
                runtime_config(),
                523,
                readiness_check=ready,
                writer_call=writer,
            )

        self.assertEqual(outcome, "applied")

    def test_disk_failure_after_writer_success_leaves_manual_only_receipt(self):
        update = queued_update()
        client = FakePortalClient()
        writer = Mock(return_value={"status": "applied", "applied": True})
        real_atomic_write = runner.atomic_write_json
        writes = {"count": 0}

        def fail_second_write(*args, **kwargs):
            writes["count"] += 1
            if writes["count"] == 2:
                raise OSError("disk full after UI result")
            return real_atomic_write(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(path)
            with patch.object(
                runner,
                "atomic_write_json",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(
                    runner.ReceiptLedgerError,
                    "receipt_ledger_write_failed",
                ):
                    runner.process_one_update(
                        client,
                        update,
                        receipts,
                        runtime_config(),
                        523,
                        readiness_check=ready,
                        writer_call=writer,
                    )

            reloaded = runner.ReceiptStore(path)

        writer.assert_called_once()
        self.assertEqual(
            reloaded.state(7, "test-idempotency-key"),
            "writer_started",
        )
        self.assertEqual(
            client.update_results[-1][1]["error"],
            "manual_reconciliation_required",
        )

    def test_interrupted_writer_started_receipt_never_clicks_again(self):
        update = queued_update()
        client = FakePortalClient()
        writer = Mock(side_effect=AssertionError("must require reconciliation"))
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(
                Path(temporary) / "receipts.json"
            )
            receipts.record_writer_started(7, "test-idempotency-key")
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

    def test_invalid_ledger_blocks_runner_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "state" / "payment-receipts.json"
            ledger.parent.mkdir()
            ledger.write_text("{invalid", encoding="utf-8")
            config_path = root / "runtime.json"
            write_runtime_config(config_path)
            mutex = Mock()
            mutex.acquire.return_value = True
            with (
                patch.object(runner, "NamedMutex", return_value=mutex),
                patch.dict(
                    runner.os.environ,
                    {"FEP_PAYMENT_RUNNER_TOKEN": "unit-test-token"},
                    clear=False,
                ),
            ):
                exit_code = runner.run_loop(config_path)
            status = json.loads(
                (root / "state" / "status.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 3)
        self.assertEqual(status["reason"], "receipt_ledger_invalid")
        mutex.close.assert_called_once()

    def test_missing_ledger_blocks_installed_runner_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "runtime.json"
            write_runtime_config(config_path)
            mutex = Mock()
            mutex.acquire.return_value = True
            with (
                patch.object(runner, "NamedMutex", return_value=mutex),
                patch.dict(
                    runner.os.environ,
                    {"FEP_PAYMENT_RUNNER_TOKEN": "unit-test-token"},
                    clear=False,
                ),
            ):
                exit_code = runner.run_loop(config_path)
            status = json.loads(
                (root / "state" / "status.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 3)
        self.assertEqual(status["reason"], "receipt_ledger_missing")

    def test_health_check_rejects_corrupt_ledger_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "state" / "payment-receipts.json"
            ledger.parent.mkdir()
            corrupt = '{"schema":2,"receipts":'
            ledger.write_text(corrupt, encoding="utf-8")
            write_runtime_config(root / "runtime.json")
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = runner.run_health_check(root / "runtime.json")

            self.assertEqual(ledger.read_text(encoding="utf-8"), corrupt)

        self.assertEqual(exit_code, 3)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "receipt_ledger_invalid")


if __name__ == "__main__":
    unittest.main()
