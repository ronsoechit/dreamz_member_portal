from datetime import datetime, timedelta
import importlib.util
import json
import os
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import (  # noqa: E402
    FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
    FEP_PAYMENT_AGENT_FRONTDESK,
    FEP_PAYMENT_COMMAND_STATUS_FAILED,
    FEP_PAYMENT_COMMAND_STATUS_PENDING,
    FEP_PAYMENT_QUEUE_HANDOFF_SOURCE,
    FEP_PAYMENT_STATUS_PENDING,
    FEP_PAYMENT_STATUS_PROCESSING,
    FepPaymentProcessCommand,
    FepPaymentQueueHandoffAudit,
    FepPaymentUpdate,
    app,
    db,
    stable_fep_payment_queue_handoff_request_hash,
    stable_json_hash,
)


class FepPaymentQueueHandoffTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["FEP_API_TOKEN"] = "fep-test-token"
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
        app.config["FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP"] = None
        app.config["FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        app.config["FEP_API_TOKEN"] = None
        app.config["SYNC_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    def reset_database(self):
        db.session.remove()
        db.drop_all()
        db.create_all()

    @staticmethod
    def fep_headers():
        return {
            "X-FEP-Token": "fep-test-token",
            "Authorization": "Bearer fep-test-token",
        }

    def add_payment(
        self,
        *,
        upload_id=523,
        suffix="1",
        target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        status=FEP_PAYMENT_STATUS_PENDING,
        **overrides,
    ):
        record = FepPaymentUpdate(
            idempotency_key=f"fep-bank-upload:{upload_id}:handoff:{suffix}",
            source="fep_manager_bank_upload_auto",
            status=status,
            member_id=f"9{upload_id}{suffix}",
            member_name=f"Handoff Member {suffix}",
            membership_period="2026-08",
            upload_id=upload_id,
            upload_filename="202608directdebits.txt",
            record_id=int(suffix) if str(suffix).isdigit() else 1,
            request_payload_hash=(str(suffix)[-1] if str(suffix) else "a") * 64,
            request_payload_json="{}",
            old_values_json="{}",
            new_values_json="{}",
            target_agent=target_agent,
            apply_attempts=0,
        )
        for key, value in overrides.items():
            setattr(record, key, value)
        db.session.add(record)
        db.session.commit()
        return record

    def add_source_command(
        self,
        *,
        upload_id=523,
        status=FEP_PAYMENT_COMMAND_STATUS_FAILED,
        target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        completed=True,
        claimed=False,
    ):
        now = datetime.now()
        command = FepPaymentProcessCommand(
            source="fep_manager_payment_process_request",
            status=status,
            target_agent=target_agent,
            requested_limit=500,
            requested_by="ron",
            upload_id=upload_id,
            upload_filename="202608directdebits.txt",
            request_payload_json="{}",
            result_json=json.dumps({"status": status}),
            claimed_by=target_agent if claimed else None,
            claimed_at=now if claimed else None,
            claim_expires_at=now + timedelta(minutes=10) if claimed else None,
            error="manual_reconciliation_required" if status == FEP_PAYMENT_COMMAND_STATUS_FAILED else None,
            started_at=now - timedelta(minutes=1),
            completed_at=now if completed else None,
            created_at=now - timedelta(minutes=2),
            updated_at=now,
        )
        db.session.add(command)
        db.session.commit()
        return command

    def handoff_payload(self, command, records, **overrides):
        payload = {
            "schema": 1,
            "request_id": f"handoff-test:{command.upload_id}:{command.id}",
            "source": FEP_PAYMENT_QUEUE_HANDOFF_SOURCE,
            "requested_by": "ron",
            "upload_id": command.upload_id,
            "source_command_id": command.id,
            "from_agent": FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
            "to_agent": FEP_PAYMENT_AGENT_FRONTDESK,
            "idempotency_keys": sorted(record.idempotency_key for record in records),
        }
        payload.update(overrides)
        payload["request_payload_hash"] = stable_fep_payment_queue_handoff_request_hash(payload)
        return payload

    def post_handoff(self, payload, headers=None):
        return self.client.post(
            "/api/fep/payment-queue-handoff",
            json=payload,
            headers=self.fep_headers() if headers is None else headers,
        )

    def test_handoff_moves_exact_queue_and_creates_audited_frontdesk_command(self):
        first = self.add_payment(suffix="1")
        second = self.add_payment(suffix="2")
        failed_record = self.add_payment(
            suffix="9",
            status="failed",
            apply_attempts=1,
            error="manual reconciliation required",
        )
        command = self.add_source_command()
        payload = self.handoff_payload(command, [first, second])

        response = self.post_handoff(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json),
            {
                "ok",
                "duplicate",
                "request_id",
                "request_payload_hash",
                "moved_count",
                "from_agent",
                "to_agent",
                "upload_id",
                "source_command_id",
                "command",
            },
        )
        self.assertTrue(response.json["ok"])
        self.assertFalse(response.json["duplicate"])
        self.assertEqual(response.json["moved_count"], 2)
        self.assertEqual(response.json["from_agent"], FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
        self.assertEqual(response.json["to_agent"], FEP_PAYMENT_AGENT_FRONTDESK)
        self.assertEqual(response.json["source_command_id"], command.id)
        self.assertEqual(response.json["command"]["status"], FEP_PAYMENT_COMMAND_STATUS_PENDING)
        self.assertEqual(response.json["command"]["target_agent"], FEP_PAYMENT_AGENT_FRONTDESK)
        self.assertEqual(response.json["command"]["requested_limit"], 2)
        self.assertEqual(response.json["command"]["pending_payments"], 2)
        self.assertEqual(response.json["command"]["upload_id"], 523)

        db.session.refresh(first)
        db.session.refresh(second)
        db.session.refresh(failed_record)
        self.assertEqual(first.target_agent, FEP_PAYMENT_AGENT_FRONTDESK)
        self.assertEqual(second.target_agent, FEP_PAYMENT_AGENT_FRONTDESK)
        self.assertEqual(failed_record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)

        destination = db.session.get(
            FepPaymentProcessCommand,
            response.json["command"]["command_id"],
        )
        self.assertEqual(destination.status, FEP_PAYMENT_COMMAND_STATUS_PENDING)
        self.assertEqual(destination.target_agent, FEP_PAYMENT_AGENT_FRONTDESK)
        self.assertEqual(destination.upload_id, 523)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

        audit = FepPaymentQueueHandoffAudit.query.one()
        self.assertEqual(audit.request_id, payload["request_id"])
        self.assertEqual(audit.request_payload_hash, payload["request_payload_hash"])
        self.assertEqual(audit.source_command_id, command.id)
        self.assertEqual(audit.destination_command_id, destination.id)
        self.assertEqual(audit.payment_count, 2)
        self.assertEqual(
            audit.idempotency_keys_sha256,
            stable_json_hash(payload["idempotency_keys"]),
        )
        audit_metadata = json.loads(audit.request_payload_json)
        self.assertEqual(audit_metadata["payment_count"], 2)
        self.assertEqual(
            audit_metadata["idempotency_keys_sha256"],
            stable_json_hash(payload["idempotency_keys"]),
        )
        self.assertNotIn("idempotency_keys", audit_metadata)
        for key in payload["idempotency_keys"]:
            self.assertNotIn(key, audit.request_payload_json)
            self.assertNotIn(key, audit.result_json)

    def test_exact_replay_is_idempotent_even_after_destination_command_changes(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.handoff_payload(source, [record])
        first = self.post_handoff(payload)
        destination = db.session.get(
            FepPaymentProcessCommand,
            first.json["command"]["command_id"],
        )
        destination.status = "running"
        destination.claimed_by = FEP_PAYMENT_AGENT_FRONTDESK
        destination.claimed_at = datetime.now()
        destination.claim_expires_at = datetime.now() + timedelta(minutes=5)
        db.session.commit()

        replay = self.post_handoff(payload)

        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])
        self.assertEqual(replay.json["command"], first.json["command"])
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 1)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

    def test_request_id_reuse_with_different_exact_payload_is_rejected(self):
        record = self.add_payment()
        source = self.add_source_command()
        first_payload = self.handoff_payload(source, [record])
        first = self.post_handoff(first_payload)
        changed_payload = self.handoff_payload(
            source,
            [record],
            request_id=first_payload["request_id"],
            requested_by="other-admin",
        )

        response = self.post_handoff(changed_payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(response.status_code, 409)
        self.assertIn("different payload", response.json["error"])
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 1)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

    def test_replay_fails_closed_when_durable_command_snapshot_is_invalid(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.handoff_payload(source, [record])
        first = self.post_handoff(payload)
        audit = FepPaymentQueueHandoffAudit.query.one()
        audit.result_json = "{}"
        db.session.commit()

        replay = self.post_handoff(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 409)
        self.assertIn("audit is incomplete", replay.json["error"])
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 1)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

    def test_payload_hash_mismatch_is_rejected_without_mutation(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.handoff_payload(source, [record])
        payload["requested_by"] = "tampered"

        response = self.post_handoff(payload)

        self.assertEqual(response.status_code, 409)
        self.assertIn("does not match", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)

    def test_fep_auth_is_required(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.handoff_payload(source, [record])

        response = self.post_handoff(payload, headers={})

        self.assertEqual(response.status_code, 403)
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)

    def test_payload_fields_and_direction_are_exact(self):
        cases = (
            ("unknown field", {"unexpected": True}, 400),
            ("wrong from", {"from_agent": FEP_PAYMENT_AGENT_FRONTDESK}, 400),
            ("wrong to", {"to_agent": FEP_PAYMENT_AGENT_DREAMZ_OFFICE}, 400),
            ("wrong source", {"source": "generic_handoff"}, 400),
        )
        for label, changes, expected_status in cases:
            with self.subTest(label=label):
                self.reset_database()
                record = self.add_payment()
                source = self.add_source_command()
                payload = self.handoff_payload(source, [record], **changes)
                response = self.post_handoff(payload)
                self.assertEqual(response.status_code, expected_status)
                db.session.refresh(record)
                self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
                self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)

    def test_source_command_must_be_failed_terminal_unclaimed_and_exact(self):
        cases = (
            ("not failed", {"status": "completed"}, {}, "terminal failed"),
            ("not completed", {"completed": False}, {}, "durably terminal"),
            ("claimed", {"claimed": True}, {}, "still claimed"),
            (
                "wrong upload",
                {},
                {"upload_id": 999},
                "does not match upload_id",
            ),
            (
                "wrong agent",
                {"target_agent": FEP_PAYMENT_AGENT_FRONTDESK},
                {},
                "does not match from_agent",
            ),
        )
        for label, command_args, payload_changes, error_text in cases:
            with self.subTest(label=label):
                self.reset_database()
                record = self.add_payment()
                source = self.add_source_command(**command_args)
                payload = self.handoff_payload(source, [record], **payload_changes)
                response = self.post_handoff(payload)
                self.assertEqual(response.status_code, 409)
                self.assertIn(error_text, response.json["error"])
                db.session.refresh(record)
                self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
                self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)

    def test_source_command_must_be_latest_for_upload_and_agent(self):
        record = self.add_payment()
        stale_source = self.add_source_command()
        latest_source = self.add_source_command()

        response = self.post_handoff(
            self.handoff_payload(stale_source, [record])
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("not the latest command", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)
        self.assertGreater(latest_source.id, stale_source.id)

    def test_handoff_rejects_any_active_command(self):
        record = self.add_payment()
        source = self.add_source_command()
        active = FepPaymentProcessCommand(
            source="other",
            status="pending",
            target_agent="ron_laptop",
            requested_limit=1,
            requested_by="other",
            upload_id=999,
            request_payload_json="{}",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.session.add(active)
        db.session.commit()

        response = self.post_handoff(self.handoff_payload(source, [record]))

        self.assertEqual(response.status_code, 409)
        self.assertIn("active payment process command", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)

    def test_handoff_rejects_any_processing_update(self):
        record = self.add_payment()
        source = self.add_source_command()
        self.add_payment(
            upload_id=999,
            suffix="8",
            target_agent="ron_laptop",
            status=FEP_PAYMENT_STATUS_PROCESSING,
            claimed_by="ron_laptop",
            claimed_at=datetime.now(),
            claim_expires_at=datetime.now() + timedelta(minutes=5),
        )

        response = self.post_handoff(self.handoff_payload(source, [record]))

        self.assertEqual(response.status_code, 409)
        self.assertIn("is processing", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)

    def test_handoff_rejects_existing_destination_pending_queue(self):
        record = self.add_payment()
        source = self.add_source_command()
        self.add_payment(
            upload_id=999,
            suffix="8",
            target_agent=FEP_PAYMENT_AGENT_FRONTDESK,
        )

        response = self.post_handoff(self.handoff_payload(source, [record]))

        self.assertEqual(response.status_code, 409)
        self.assertIn("destination agent already has pending", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)

    def test_allowlist_must_exactly_equal_all_pending_source_rows(self):
        cases = ("missing", "extra", "duplicate", "unsorted")
        for label in cases:
            with self.subTest(label=label):
                self.reset_database()
                first = self.add_payment(suffix="1")
                second = self.add_payment(suffix="2")
                source = self.add_source_command()
                keys = sorted([first.idempotency_key, second.idempotency_key])
                if label == "missing":
                    keys = keys[:1]
                elif label == "extra":
                    keys.append("fep-bank-upload:523:handoff:999")
                elif label == "duplicate":
                    keys.append(keys[-1])
                else:
                    keys = list(reversed(keys))
                payload = self.handoff_payload(
                    source,
                    [first, second],
                    idempotency_keys=keys,
                )

                response = self.post_handoff(payload)

                self.assertIn(response.status_code, {400, 409})
                db.session.refresh(first)
                db.session.refresh(second)
                self.assertEqual(first.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
                self.assertEqual(second.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
                self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)

    def test_each_source_row_must_be_pristine_and_unclaimed(self):
        now = datetime.now()
        cases = (
            (
                "claimed",
                {
                    "claimed_by": FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
                    "claimed_at": now,
                    "claim_expires_at": now + timedelta(minutes=5),
                },
                "is claimed",
            ),
            ("attempted", {"apply_attempts": 1}, "apply attempt"),
            ("errored", {"error": "old error"}, "has an error"),
            ("has result", {"result_json": "{}"}, "writer result"),
            ("applied timestamp", {"applied_at": now}, "write confirmation"),
            ("confirmed timestamp", {"confirmed_at": now}, "write confirmation"),
        )
        for label, record_changes, error_text in cases:
            with self.subTest(label=label):
                self.reset_database()
                record = self.add_payment(**record_changes)
                source = self.add_source_command()

                response = self.post_handoff(self.handoff_payload(source, [record]))

                self.assertEqual(response.status_code, 409)
                self.assertIn(error_text, response.json["error"])
                db.session.refresh(record)
                self.assertEqual(record.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
                self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)

    def test_transaction_rolls_back_targets_command_and_audit_when_commit_fails(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.handoff_payload(source, [record])
        real_commit = db.session.commit

        def fail_final_commit():
            if any(
                isinstance(item, FepPaymentQueueHandoffAudit)
                for item in db.session.new
            ):
                raise RuntimeError("simulated durable audit failure")
            return real_commit()

        with patch.object(db.session, "commit", side_effect=fail_final_commit):
            response = self.post_handoff(payload)

        self.assertEqual(response.status_code, 500)
        db.session.expire_all()
        persisted = db.session.get(FepPaymentUpdate, record.id)
        self.assertEqual(persisted.target_agent, FEP_PAYMENT_AGENT_DREAMZ_OFFICE)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)
        self.assertEqual(FepPaymentQueueHandoffAudit.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
