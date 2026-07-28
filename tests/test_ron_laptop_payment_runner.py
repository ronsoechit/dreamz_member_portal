from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ron_laptop_payment_runner import runner


class FakePortalClient:
    def __init__(self, command=None, update_batches=None):
        self.command = command
        self.update_batches = list(update_batches or [])
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

    def claim_updates(self, _limit):
        if not self.update_batches:
            return []
        return self.update_batches.pop(0)

    def post_update_result(self, update_id, payload):
        self.update_results.append((update_id, payload))
        return {"ok": True}


def ready(_idle_seconds):
    return runner.Readiness(True, "ready")


def fresh_command(command_id=42, limit=1):
    return {
        "id": command_id,
        "status": "running",
        "target_agent": runner.AGENT_ID,
        "requested_limit": limit,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def queued_update(update_id=7, idempotency_key="test-idempotency-key"):
    return {
        "id": update_id,
        "idempotency_key": idempotency_key,
        "target_agent": runner.AGENT_ID,
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
        self.assertEqual(runner.AGENT_ID, "ron_laptop")
        self.assertEqual(str(runner.SOURCE_ROOT), "Z:\\")
        self.assertEqual(runner.EXPECTED_DATA_ROOT, r"Z:\Data")
        self.assertEqual(
            runner.PORTAL_URL,
            "https://dreamzmemberportal-production.up.railway.app",
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

    def test_runtime_config_rejects_scope_and_secret_fields(self):
        for field in ("agent_id", "source_root", "portal_url", "sync_token", "token"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "runtime.json"
                path.write_text(json.dumps({field: "forbidden"}), encoding="utf-8")
                with self.assertRaisesRegex(
                    runner.RunnerError,
                    "runtime_config_contains_fixed_or_secret_field",
                ):
                    runner.load_runtime_config(path)

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
                        "target_agent": "ron_laptop",
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
            "/api/sync/fep-payment-process-commands?agent_id=ron_laptop",
            request.full_url,
        )
        self.assertNotIn("unit-test-token", request.full_url)
        self.assertEqual(request.get_header("X-sync-agent"), "ron_laptop")
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
            "/api/sync/fep-payment-updates/7/result?agent_id=ron_laptop",
            request.full_url,
        )
        self.assertNotIn("unit-test-token", request.full_url)
        self.assertNotIn(b"unit-test-token", request.data)
        self.assertEqual(json.loads(request.data)["agent_id"], "ron_laptop")


class PreflightTests(unittest.TestCase):
    def test_exact_title_parser_accepts_only_z_data(self):
        self.assertEqual(
            runner.gymassistant_title_data_path(
                r"Gym Assistant 2.6 [Path=Z:\Data]"
            ),
            runner.normalize_windows_path(r"Z:\Data"),
        )
        self.assertNotEqual(
            runner.gymassistant_title_data_path(
                r"Gym Assistant 2.6 [Path=C:\Gym Assistant 2.6\Data]"
            ),
            runner.normalize_windows_path(r"Z:\Data"),
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
                runner.RuntimeConfig(),
                readiness_check=lambda _idle: runner.Readiness(
                    False,
                    "desktop_locked",
                ),
            )

        self.assertEqual(state, "desktop_locked")
        self.assertEqual(summary.received, 0)
        self.assertEqual(client.command_claims, 0)
        self.assertEqual(client.command_results, [])

    def test_missing_exact_window_does_not_claim_command(self):
        client = FakePortalClient(command=fresh_command())
        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, _summary = runner.process_available_command(
                client,
                receipts,
                runner.RuntimeConfig(),
                readiness_check=lambda _idle: runner.Readiness(
                    False,
                    "expected_gymassistant_window_missing",
                ),
            )

        self.assertEqual(state, "expected_gymassistant_window_missing")
        self.assertEqual(client.command_claims, 0)

    def test_workstation_preflight_checks_exactly_one_window(self):
        with (
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
            result = runner.workstation_readiness(2)

        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "expected_gymassistant_window_ambiguous")


class CommandProcessingTests(unittest.TestCase):
    def test_stale_command_is_failed_before_update_claim(self):
        command = fresh_command()
        command["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        client = FakePortalClient(command=command, update_batches=[[queued_update()]])

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            state, summary = runner.process_available_command(
                client,
                receipts,
                runner.RuntimeConfig(command_ttl_seconds=900),
                readiness_check=ready,
            )

        self.assertEqual(state, "command_expired")
        self.assertEqual(summary.received, 0)
        self.assertEqual(len(client.update_batches), 1)
        self.assertEqual(client.command_results[0][1]["status"], "failed")
        self.assertEqual(
            client.command_results[0][1]["error"],
            "command_expired",
        )

    def test_fresh_command_uses_official_writer_and_reports_counts(self):
        update = queued_update()
        client = FakePortalClient(
            command=fresh_command(limit=1),
            update_batches=[[update]],
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
                runner.RuntimeConfig(),
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
        writer.assert_called_once_with(update, 30)
        self.assertEqual(client.update_results[0][1]["status"], "applied")
        self.assertNotIn("member_id", client.update_results[0][1])
        self.assertNotIn("observed", client.update_results[0][1])
        self.assertEqual(client.command_results[-1][1]["status"], "completed")
        self.assertEqual(receipts.count(), 0)

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
                        runner.RuntimeConfig(),
                        readiness_check=ready,
                        writer_call=writer,
                    )

            self.assertEqual(receipts.count(), 1)
            writer.assert_called_once()

            second_client = FakePortalClient()
            second_writer = Mock(
                side_effect=AssertionError("writer must not run twice")
            )
            outcome = runner.process_one_update(
                second_client,
                update,
                receipts,
                runner.RuntimeConfig(),
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

    def test_writer_exception_is_reduced_to_safe_category(self):
        update = queued_update()
        client = FakePortalClient()
        raw_error = (
            "Member #34203 Sensitive Name amount $61.00 does not match expected."
        )

        with tempfile.TemporaryDirectory() as temporary:
            receipts = runner.ReceiptStore(Path(temporary) / "receipts.json")
            outcome = runner.process_one_update(
                client,
                update,
                receipts,
                runner.RuntimeConfig(),
                readiness_check=ready,
                writer_call=Mock(side_effect=RuntimeError(raw_error)),
            )

        self.assertEqual(outcome, "failed")
        payload = client.update_results[0][1]
        self.assertEqual(
            payload["error"],
            "payment_details_mismatch_requires_manual_review",
        )
        serialized = json.dumps(payload)
        self.assertNotIn("34203", serialized)
        self.assertNotIn("Sensitive Name", serialized)
        self.assertNotIn("61.00", serialized)


class SanitizedStateTests(unittest.TestCase):
    def test_status_contains_only_operational_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.json"
            status = runner.StatusStore(path)
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

        self.assertEqual(payload["agent_id"], "ron_laptop")
        self.assertEqual(payload["last_summary"]["applied"], 18)
        for forbidden in ("member_id", "member_name", "amount", "sync_token"):
            self.assertNotIn(forbidden, raw.casefold())

    def test_receipt_contains_hash_not_idempotency_key_or_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipts.json"
            receipts = runner.ReceiptStore(path)
            receipts.record_applied(7, "secret-idempotency-value")
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
                    {"SYNC_API_TOKEN": "unit-test-token"},
                    clear=False,
                ),
            ):
                exit_code = runner.run_loop(config_path, once=True)

            self.assertEqual(exit_code, 0)
            self.assertFalse(
                (Path(temporary) / "state" / "status.json").exists()
            )
            mutex.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
