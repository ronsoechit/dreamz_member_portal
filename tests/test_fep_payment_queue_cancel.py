from datetime import datetime, timedelta
import importlib.util
import json
import os
import unittest
from unittest.mock import patch


if (
    importlib.util.find_spec("flask") is None
    or importlib.util.find_spec("flask_sqlalchemy") is None
):
    raise unittest.SkipTest(
        "Flask app dependencies are not installed in this Python runtime"
    )

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import (  # noqa: E402
    FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
    FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
    FEP_PAYMENT_COMMAND_STATUS_COMPLETED,
    FEP_PAYMENT_COMMAND_STATUS_FAILED,
    FEP_PAYMENT_QUEUE_CANCEL_ERROR,
    FEP_PAYMENT_QUEUE_CANCEL_SOURCE,
    FEP_PAYMENT_STATUS_FAILED,
    FEP_PAYMENT_STATUS_PENDING,
    FEP_PAYMENT_STATUS_PROCESSING,
    FepPaymentProcessCommand,
    FepPaymentUpdate,
    app,
    create_fep_payment_queue_cancel,
    db,
    stable_fep_payment_queue_cancel_request_hash,
    stable_json_hash,
)


class FepPaymentQueueCancelTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["FEP_API_TOKEN"] = "fep-test-token"
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
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
        target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        status=FEP_PAYMENT_STATUS_PENDING,
        **overrides,
    ):
        record = FepPaymentUpdate(
            idempotency_key=f"fep-bank-upload:{upload_id}:reserve:{suffix}",
            source="fep_manager_bank_upload_auto",
            status=status,
            member_id=f"9{upload_id}{suffix}",
            member_name=f"Reserve Member {suffix}",
            membership_period="2026-08",
            upload_id=upload_id,
            upload_filename="202608directdebits.txt",
            record_id=int(suffix) if str(suffix).isdigit() else 1,
            request_payload_hash=(str(suffix)[-1] or "a") * 64,
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
        command_id=None,
        upload_id=523,
        status=FEP_PAYMENT_COMMAND_STATUS_FAILED,
        target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        completed=True,
        claimed=False,
    ):
        now = datetime.now()
        command = FepPaymentProcessCommand(
            id=command_id,
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
            claim_expires_at=(
                now + timedelta(minutes=10) if claimed else None
            ),
            error=(
                "manual_reconciliation_required"
                if status == FEP_PAYMENT_COMMAND_STATUS_FAILED
                else None
            ),
            started_at=now - timedelta(minutes=1),
            completed_at=now if completed else None,
            created_at=now - timedelta(minutes=2),
            updated_at=now,
        )
        db.session.add(command)
        db.session.commit()
        return command

    def cancel_payload(self, command, records, **overrides):
        payload = {
            "schema": 1,
            "request_id": f"reserve-cancel:{command.upload_id}:{command.id}",
            "source": FEP_PAYMENT_QUEUE_CANCEL_SOURCE,
            "requested_by": "ron",
            "upload_id": command.upload_id,
            "source_command_id": command.id,
            "target_agent": FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
            "idempotency_keys": sorted(
                record.idempotency_key for record in records
            ),
        }
        payload.update(overrides)
        payload["request_payload_hash"] = (
            stable_fep_payment_queue_cancel_request_hash(payload)
        )
        return payload

    def post_cancel(self, payload, headers=None):
        return self.client.post(
            "/api/fep/payment-queue-cancel",
            json=payload,
            headers=self.fep_headers() if headers is None else headers,
        )

    def test_cancel_marks_only_exact_pristine_reserve_queue_failed(self):
        first = self.add_payment(suffix="1")
        second = self.add_payment(suffix="2")
        already_failed = self.add_payment(
            suffix="9",
            status=FEP_PAYMENT_STATUS_FAILED,
            apply_attempts=1,
            error="manual_reconciliation_required",
            result_json=json.dumps({"status": "failed"}),
        )
        source = self.add_source_command()
        payload = self.cancel_payload(source, [first, second])

        response = self.post_cancel(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json,
            {
                "ok": True,
                "duplicate": False,
                "request_id": payload["request_id"],
                "request_payload_hash": payload["request_payload_hash"],
                "cancelled_count": 2,
                "target_agent": FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
                "upload_id": 523,
                "source_command_id": source.id,
                "audit_command_id": response.json["audit_command_id"],
                "status": FEP_PAYMENT_COMMAND_STATUS_COMPLETED,
                "payment_status": FEP_PAYMENT_STATUS_FAILED,
            },
        )

        for record in (first, second):
            db.session.refresh(record)
            self.assertEqual(record.status, FEP_PAYMENT_STATUS_FAILED)
            self.assertEqual(record.error, FEP_PAYMENT_QUEUE_CANCEL_ERROR)
            self.assertEqual(record.apply_attempts, 0)
            self.assertIsNone(record.claimed_by)
            self.assertIsNone(record.applied_at)
            self.assertIsNone(record.confirmed_at)
            self.assertIsNone(record.result_json)

        db.session.refresh(already_failed)
        self.assertEqual(already_failed.apply_attempts, 1)
        self.assertEqual(
            already_failed.error,
            "manual_reconciliation_required",
        )

        audit = db.session.get(
            FepPaymentProcessCommand,
            response.json["audit_command_id"],
        )
        self.assertEqual(audit.source, FEP_PAYMENT_QUEUE_CANCEL_SOURCE)
        self.assertEqual(audit.status, FEP_PAYMENT_COMMAND_STATUS_COMPLETED)
        self.assertEqual(audit.requested_limit, 2)
        metadata = json.loads(audit.request_payload_json)
        self.assertEqual(metadata["payment_count"], 2)
        self.assertEqual(
            metadata["idempotency_keys_sha256"],
            stable_json_hash(payload["idempotency_keys"]),
        )
        self.assertNotIn("idempotency_keys", metadata)
        for key in payload["idempotency_keys"]:
            self.assertNotIn(key, audit.request_payload_json)
            self.assertNotIn(key, audit.result_json)

    def test_cancel_handles_current_183_row_reserve_queue_as_one_exact_set(self):
        records = []
        for index in range(1, 184):
            record = FepPaymentUpdate(
                idempotency_key=(
                    f"fep-bank-upload:523:reserve:bulk:{index:03d}"
                ),
                source="fep_manager_bank_upload_auto",
                status=FEP_PAYMENT_STATUS_PENDING,
                member_id=f"bulk-{index:03d}",
                member_name="Reserve cancellation test",
                membership_period="2026-08",
                upload_id=523,
                upload_filename="202608directdebits.txt",
                record_id=index,
                request_payload_hash=f"{index:064x}"[-64:],
                request_payload_json="{}",
                old_values_json="{}",
                new_values_json="{}",
                target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
                apply_attempts=0,
            )
            db.session.add(record)
            records.append(record)
        db.session.commit()
        source = self.add_source_command()

        response = self.post_cancel(
            self.cancel_payload(source, records)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["cancelled_count"], 183)
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                upload_id=523,
                target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
                status=FEP_PAYMENT_STATUS_PENDING,
            ).count(),
            0,
        )
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                upload_id=523,
                target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
                status=FEP_PAYMENT_STATUS_FAILED,
            ).count(),
            183,
        )
        self.assertEqual(
            FepPaymentUpdate.query.filter(
                FepPaymentUpdate.apply_attempts != 0
            ).count(),
            0,
        )

    def test_cancel_handles_dreamz_office_command_12_with_13_pending(self):
        office_records = [
            self.add_payment(
                suffix=str(index),
                target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
            )
            for index in range(1, 14)
        ]
        reserve_record = self.add_payment(
            suffix="reserve-sentinel",
            target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        )
        source = self.add_source_command(
            command_id=12,
            target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )
        payload = self.cancel_payload(
            source,
            office_records,
            target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )

        response = self.post_cancel(payload)

        self.assertEqual(source.id, 12)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["cancelled_count"], 13)
        self.assertEqual(
            response.json["target_agent"],
            FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )
        self.assertEqual(response.json["source_command_id"], 12)
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                upload_id=523,
                target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
                status=FEP_PAYMENT_STATUS_PENDING,
            ).count(),
            0,
        )
        self.assertEqual(
            FepPaymentUpdate.query.filter_by(
                upload_id=523,
                target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
                status=FEP_PAYMENT_STATUS_FAILED,
            ).count(),
            13,
        )
        db.session.refresh(reserve_record)
        self.assertEqual(
            reserve_record.status,
            FEP_PAYMENT_STATUS_PENDING,
        )
        self.assertIsNone(reserve_record.error)

    def test_exact_replay_is_idempotent(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.cancel_payload(source, [record])

        first = self.post_cancel(payload)
        replay = self.post_cancel(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])
        self.assertEqual(
            replay.json["audit_command_id"],
            first.json["audit_command_id"],
        )
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

    def test_replay_identity_is_scoped_by_request_id_and_target_agent(self):
        shared_request_id = "queue-cancel:shared-request-id"
        reserve_record = self.add_payment(
            upload_id=523,
            suffix="reserve",
            target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        )
        office_record = self.add_payment(
            upload_id=524,
            suffix="office",
            target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )
        reserve_source = self.add_source_command(
            upload_id=523,
            target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        )
        office_source = self.add_source_command(
            upload_id=524,
            target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )
        reserve_payload = self.cancel_payload(
            reserve_source,
            [reserve_record],
            request_id=shared_request_id,
            target_agent=FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
        )
        office_payload = self.cancel_payload(
            office_source,
            [office_record],
            request_id=shared_request_id,
            target_agent=FEP_PAYMENT_AGENT_DREAMZ_OFFICE,
        )

        reserve_first = self.post_cancel(reserve_payload)
        office_first = self.post_cancel(office_payload)
        reserve_replay = self.post_cancel(reserve_payload)
        office_replay = self.post_cancel(office_payload)

        self.assertEqual(reserve_first.status_code, 200)
        self.assertEqual(office_first.status_code, 200)
        self.assertFalse(reserve_first.json["duplicate"])
        self.assertFalse(office_first.json["duplicate"])
        self.assertNotEqual(
            reserve_first.json["audit_command_id"],
            office_first.json["audit_command_id"],
        )
        self.assertTrue(reserve_replay.json["duplicate"])
        self.assertTrue(office_replay.json["duplicate"])
        self.assertEqual(
            reserve_replay.json["audit_command_id"],
            reserve_first.json["audit_command_id"],
        )
        self.assertEqual(
            office_replay.json["audit_command_id"],
            office_first.json["audit_command_id"],
        )

    def test_request_id_reuse_with_changed_payload_is_rejected(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.cancel_payload(source, [record])
        first = self.post_cancel(payload)
        changed = self.cancel_payload(
            source,
            [record],
            request_id=payload["request_id"],
            requested_by="other-admin",
        )

        response = self.post_cancel(changed)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(response.status_code, 409)
        self.assertIn("different payload", response.json["error"])
        self.assertEqual(FepPaymentProcessCommand.query.count(), 2)

    def test_fep_auth_and_exact_payload_are_required(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.cancel_payload(source, [record])

        unauthenticated = self.post_cancel(payload, headers={})
        self.assertEqual(unauthenticated.status_code, 403)

        cases = (
            ("unknown field", {"unexpected": True}, 400),
            ("unknown target", {"target_agent": "unknown_agent"}, 400),
            ("target alias", {"target_agent": "office"}, 400),
            ("wrong source", {"source": "generic_cancel"}, 400),
        )
        for label, changes, expected_status in cases:
            with self.subTest(label=label):
                changed = self.cancel_payload(source, [record], **changes)
                response = self.post_cancel(changed)
                self.assertEqual(response.status_code, expected_status)

        db.session.refresh(record)
        self.assertEqual(record.status, FEP_PAYMENT_STATUS_PENDING)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)

    def test_source_command_must_be_failed_terminal_unclaimed_and_exact(self):
        cases = (
            (
                "not failed",
                {"status": FEP_PAYMENT_COMMAND_STATUS_COMPLETED},
                {},
                "terminal failed",
            ),
            (
                "not completed",
                {"completed": False},
                {},
                "durably terminal",
            ),
            (
                "claimed",
                {"claimed": True},
                {},
                "still claimed",
            ),
            (
                "wrong upload",
                {},
                {"upload_id": 999},
                "does not match upload_id",
            ),
            (
                "wrong agent",
                {"target_agent": "dreamz_office"},
                {},
                "does not match target_agent",
            ),
        )
        for label, command_args, payload_changes, error_text in cases:
            with self.subTest(label=label):
                self.reset_database()
                record = self.add_payment()
                source = self.add_source_command(**command_args)
                payload = self.cancel_payload(
                    source,
                    [record],
                    **payload_changes,
                )

                response = self.post_cancel(payload)

                self.assertEqual(response.status_code, 409)
                self.assertIn(error_text, response.json["error"])
                db.session.refresh(record)
                self.assertEqual(
                    record.status,
                    FEP_PAYMENT_STATUS_PENDING,
                )

    def test_source_command_must_be_latest(self):
        record = self.add_payment()
        stale = self.add_source_command()
        self.add_source_command()

        response = self.post_cancel(self.cancel_payload(stale, [record]))

        self.assertEqual(response.status_code, 409)
        self.assertIn("not the latest command", response.json["error"])
        db.session.refresh(record)
        self.assertEqual(record.status, FEP_PAYMENT_STATUS_PENDING)

    def test_active_command_or_processing_update_blocks_cancellation(self):
        for state in ("active command", "processing update"):
            with self.subTest(state=state):
                self.reset_database()
                record = self.add_payment()
                source = self.add_source_command()
                if state == "active command":
                    now = datetime.now()
                    db.session.add(
                        FepPaymentProcessCommand(
                            source="other",
                            status="pending",
                            target_agent="ron_laptop",
                            requested_limit=1,
                            requested_by="other",
                            upload_id=999,
                            request_payload_json="{}",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    self.add_payment(
                        upload_id=999,
                        suffix="8",
                        target_agent="ron_laptop",
                        status=FEP_PAYMENT_STATUS_PROCESSING,
                        claimed_by="ron_laptop",
                        claimed_at=datetime.now(),
                        claim_expires_at=(
                            datetime.now() + timedelta(minutes=5)
                        ),
                    )
                db.session.commit()

                response = self.post_cancel(
                    self.cancel_payload(source, [record])
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn(
                    "active payment process command"
                    if state == "active command"
                    else "is processing",
                    response.json["error"],
                )
                db.session.refresh(record)
                self.assertEqual(
                    record.status,
                    FEP_PAYMENT_STATUS_PENDING,
                )

    def test_allowlist_must_exactly_equal_all_pending_reserve_rows(self):
        first = self.add_payment(suffix="1")
        second = self.add_payment(suffix="2")
        source = self.add_source_command()

        response = self.post_cancel(
            self.cancel_payload(source, [first])
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("exactly equal", response.json["error"])
        for record in (first, second):
            db.session.refresh(record)
            self.assertEqual(record.status, FEP_PAYMENT_STATUS_PENDING)

    def test_each_source_row_must_be_pristine_and_unclaimed(self):
        now = datetime.now()
        cases = (
            (
                "claimed",
                {
                    "claimed_by": FEP_PAYMENT_AGENT_RESERVE_8KM7V7D,
                    "claimed_at": now,
                    "claim_expires_at": now + timedelta(minutes=5),
                },
                "is claimed",
            ),
            ("attempted", {"apply_attempts": 1}, "apply attempt"),
            ("errored", {"error": "old error"}, "has an error"),
            ("has result", {"result_json": "{}"}, "writer result"),
            ("applied timestamp", {"applied_at": now}, "write confirmation"),
            (
                "confirmed timestamp",
                {"confirmed_at": now},
                "write confirmation",
            ),
        )
        for label, record_changes, error_text in cases:
            with self.subTest(label=label):
                self.reset_database()
                record = self.add_payment(**record_changes)
                source = self.add_source_command()

                response = self.post_cancel(
                    self.cancel_payload(source, [record])
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn(error_text, response.json["error"])
                db.session.refresh(record)
                self.assertEqual(
                    record.status,
                    FEP_PAYMENT_STATUS_PENDING,
                )

    def test_commit_failure_rolls_back_queue_and_audit(self):
        record = self.add_payment()
        source = self.add_source_command()
        payload = self.cancel_payload(source, [record])
        with patch.object(
            db.session,
            "commit",
            side_effect=RuntimeError("simulated durable audit failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "simulated durable audit failure",
            ):
                create_fep_payment_queue_cancel(payload)

        db.session.rollback()
        db.session.expire_all()
        persisted = db.session.get(FepPaymentUpdate, record.id)
        self.assertEqual(persisted.status, FEP_PAYMENT_STATUS_PENDING)
        self.assertIsNone(persisted.error)
        self.assertEqual(FepPaymentProcessCommand.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
