from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch
import zipfile


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

import invoice_event_batch as batch_module  # noqa: E402
from dreamz_portal import (  # noqa: E402
    GymAssistantInvoiceEventBatch,
    GymAssistantInvoiceSyncState,
    GymAssistantJournalEvent,
    Member,
    MemberInvoice,
    SyncRun,
    app,
    db,
    fep_latest_sync_meta,
    invoice_event_batch_lock_key,
    invoice_event_batch_sha256,
    invoice_event_freshness_issues,
    invoice_event_member_ids_sha256,
    invoice_event_set_sha256,
    latest_member_sync_context,
    legacy_member_snapshot_promotion,
    sync_run_is_latest,
)


COHORT = ("91001", "91002")


class InvoiceEventBatchApiTests(unittest.TestCase):
    def setUp(self):
        self.original_config = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "SYNC_API_TOKEN",
                "INVOICE_GA_PILOT_ENABLED",
                "INVOICE_GA_PILOT_MEMBER_IDS",
                "INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES",
                "_RUNTIME_SCHEMA_READY",
            )
        }
        app.config.update(
            TESTING=True,
            SYNC_API_TOKEN="invoice-batch-token",
            INVOICE_GA_PILOT_ENABLED=True,
            INVOICE_GA_PILOT_MEMBER_IDS=set(COHORT),
            INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES=30,
            _RUNTIME_SCHEMA_READY=False,
        )
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        db.session.add_all([
            Member(
                member_id=member_id,
                name=f"Batch, {member_id}",
                billing_amount=65.0,
            )
            for member_id in COHORT
        ])
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        for key, value in self.original_config.items():
            app.config[key] = value

    @staticmethod
    def event(member_id, suffix="one", **overrides):
        source_reference = "ga-journal:" + hashlib.sha256(
            f"reference:{member_id}:{suffix}".encode("ascii")
        ).hexdigest()
        source_payload_hash = hashlib.sha256(
            f"payload:{member_id}:{suffix}".encode("ascii")
        ).hexdigest()
        values = {
            "source_reference": source_reference,
            "source_payload_hash": source_payload_hash,
            "member_id": member_id,
            "event_name": "membership_renewal",
            "event_type": 3,
            "is_voided": False,
            "occurred_at": "2026-07-31T13:45:00",
            "journal_sequence": "A1",
            "journal_transaction_id": 700000 + int(member_id),
            "membership_type_id": 1144074289,
            "billing_option_code": 1024,
            "service_period_start": "2026-07-01",
            "service_period_end_exclusive": "2026-08-01",
            "service_period_end": "2026-07-31",
            "dues_cents": 6500,
            "other_contract_fee_cents": 0,
            "source_tax_cents": 0,
            "tender_total_cents": 6500,
            "remittance_type": 2,
            "remittance_reference": None,
            "balance_payment_cents": 0,
            "is_positive_membership_payment": True,
            "requires_manual_review": False,
            "manual_review_reason": None,
            "catalog_plan_name": "Contract Dreamz",
            "catalog_base_amount_cents": 6500,
            "catalog_interval_count": 1,
            "catalog_interval_unit": "MONTHS",
            "catalog_match_status": "match",
            "catalog_period_match_status": "match",
        }
        values.update(overrides)
        return values

    def payload(self, batch_id="invoice-batch-1", events=None, **overrides):
        events = events or [self.event(member_id) for member_id in COHORT]
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        values = {
            "schema": "dreamz.ga.invoice-event-batch.v1",
            "batch_id": batch_id,
            "generated_at": now,
            "invoice_member_count": len(COHORT),
            "invoice_member_ids_sha256": invoice_event_member_ids_sha256(set(COHORT)),
            "invoice_event_count": len(events),
            "invoice_event_set_sha256": invoice_event_set_sha256(events),
            "invoice_short_fragment_count": 3,
            "invoice_scanner_schema": "dreamz.ga.invoice-target-readiness.v1",
            "invoice_pilot_member_ids": list(COHORT),
            "invoice_membership_events": events,
            "invoice_journal_issue_count": 0,
            "invoice_journal_source": "C:/Gym Assistant 2.6/Data/Backup/test.gbu::Journal.jtx",
            "invoice_catalog_source": "C:/Gym Assistant 2.6/Data/Backup/test.gbu::Members.btx",
            "invoice_source_snapshot_at": now,
            "invoice_source_sha256": "d" * 64,
            "invoice_catalog_sha256": "e" * 64,
        }
        values.update(overrides)
        values["batch_sha256"] = invoice_event_batch_sha256(values)
        return values

    @staticmethod
    def headers(batch_id, token="invoice-batch-token"):
        return {
            "X-Sync-Token": token,
            "Idempotency-Key": batch_id,
        }

    def post(self, payload, *, headers=None):
        return self.client.post(
            "/api/sync/invoice-event-batches",
            json=payload,
            headers=headers or self.headers(payload["batch_id"]),
        )

    def test_requires_sync_token_and_matching_idempotency_key(self):
        payload = self.payload()

        unauthorized = self.post(payload, headers={"Idempotency-Key": payload["batch_id"]})
        missing_key = self.post(
            payload,
            headers={"X-Sync-Token": "invoice-batch-token"},
        )
        wrong_key = self.post(
            payload,
            headers=self.headers("another-batch"),
        )

        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json["code"], "idempotency_key_missing")
        self.assertEqual(wrong_key.status_code, 409)
        self.assertEqual(wrong_key.json["code"], "idempotency_key_mismatch")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 0)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)

    def test_success_is_invoice_only_atomic_and_freshness_linked(self):
        payload = self.payload()
        before_member_count = Member.query.count()

        with (
            patch("dreamz_portal.reconcile_pending_portal_invitations") as invitations,
            patch("dreamz_portal.acquire_invoice_event_batch_transaction_lock") as batch_lock,
            patch("dreamz_portal.acquire_invoice_member_transaction_lock") as member_lock,
        ):
            response = self.post(payload)

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.json["schema"], "dreamz.ga.invoice-event-batch-receipt.v1")
        self.assertEqual(response.json["status"], "success")
        self.assertEqual(response.json["batch_id"], payload["batch_id"])
        self.assertEqual(response.json["batch_sha256"], payload["batch_sha256"])
        self.assertEqual(response.json["received"], 2)
        self.assertEqual(response.json["member_count"], 2)
        self.assertEqual(response.json["rejected"], 0)
        self.assertEqual(response.json["conflicts"], 0)
        self.assertEqual(response.json["inserted"], 2)
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 1)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(GymAssistantInvoiceSyncState.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), 1)
        self.assertEqual(Member.query.count(), before_member_count)
        self.assertEqual(MemberInvoice.query.count(), 0)
        invitations.assert_not_called()
        batch_lock.assert_called_once_with()
        self.assertEqual(
            member_lock.call_args_list,
            [call(member_id) for member_id in COHORT],
        )

        sync_run = SyncRun.query.one()
        self.assertEqual(sync_run.source, "invoice_event_batch")
        self.assertEqual(sync_run.status, "success")
        self.assertEqual(sync_run.members_received, 0)
        self.assertEqual(sync_run.documents_received, 0)
        for source_event in GymAssistantJournalEvent.query.all():
            state = db.session.get(GymAssistantInvoiceSyncState, source_event.member_id)
            self.assertEqual(source_event.last_sync_run_id, sync_run.id)
            self.assertEqual(state.last_sync_run_id, sync_run.id)
            self.assertNotIn("source_sync_not_successful", invoice_event_freshness_issues(source_event))

    def test_exact_duplicate_returns_receipt_without_writes(self):
        payload = self.payload()
        first = self.post(payload)
        state_times = {
            state.member_id: state.last_synced_at
            for state in GymAssistantInvoiceSyncState.query.all()
        }
        event_times = {
            event.source_reference: event.last_seen_at
            for event in GymAssistantJournalEvent.query.all()
        }

        second = self.post(payload)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json["status"], "duplicate")
        self.assertEqual(second.json["batch_sha256"], first.json["batch_sha256"])
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 1)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(GymAssistantInvoiceSyncState.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), 1)
        self.assertEqual(
            state_times,
            {state.member_id: state.last_synced_at for state in GymAssistantInvoiceSyncState.query.all()},
        )
        self.assertEqual(
            event_times,
            {event.source_reference: event.last_seen_at for event in GymAssistantJournalEvent.query.all()},
        )

    def test_same_batch_id_with_different_digest_is_conflict_and_no_write(self):
        first_payload = self.payload()
        self.assertEqual(self.post(first_payload).status_code, 201)
        second_payload = self.payload(invoice_short_fragment_count=2)

        response = self.post(second_payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "batch_id_conflict")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 1)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), 1)

    def test_one_invalid_or_duplicate_event_rolls_back_whole_batch(self):
        invalid_events = [
            self.event(COHORT[0]),
            self.event(COHORT[1], service_period_end="2026-07-30"),
        ]
        invalid_response = self.post(self.payload(events=invalid_events))

        self.assertEqual(invalid_response.status_code, 422)
        self.assertEqual(invalid_response.json["code"], "invalid_event")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 0)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)
        self.assertEqual(GymAssistantInvoiceSyncState.query.count(), 0)
        self.assertEqual(SyncRun.query.count(), 0)

        duplicate = self.event(COHORT[0])
        duplicate_events = [duplicate, {**duplicate, "member_id": COHORT[1]}]
        duplicate_response = self.post(
            self.payload(batch_id="invoice-batch-duplicate", events=duplicate_events)
        )
        self.assertEqual(duplicate_response.status_code, 422)
        self.assertEqual(duplicate_response.json["code"], "duplicate_event")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 0)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)
        self.assertEqual(SyncRun.query.count(), 0)

    def test_requires_zero_issues_exact_cohort_and_full_coverage(self):
        issue_payload = self.payload(invoice_journal_issue_count=1)
        issue_response = self.post(issue_payload)
        self.assertEqual(issue_response.status_code, 422)
        self.assertEqual(issue_response.json["code"], "journal_issues_present")

        one_event = [self.event(COHORT[0])]
        coverage_payload = self.payload(
            batch_id="invoice-batch-coverage",
            events=one_event,
        )
        coverage_response = self.post(coverage_payload)
        self.assertEqual(coverage_response.status_code, 422)
        self.assertEqual(coverage_response.json["code"], "cohort_coverage_incomplete")

        wrong_cohort_payload = self.payload(batch_id="invoice-batch-cohort")
        wrong_cohort_payload["invoice_pilot_member_ids"] = [COHORT[0]]
        wrong_cohort_payload["invoice_member_count"] = 1
        wrong_cohort_payload["invoice_member_ids_sha256"] = invoice_event_member_ids_sha256({COHORT[0]})
        wrong_cohort_payload["batch_sha256"] = invoice_event_batch_sha256({
            key: value
            for key, value in wrong_cohort_payload.items()
            if key != "batch_sha256"
        })
        wrong_cohort_response = self.post(wrong_cohort_payload)
        self.assertEqual(wrong_cohort_response.status_code, 422)
        self.assertEqual(wrong_cohort_response.json["code"], "cohort_mismatch")

        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 0)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)
        self.assertEqual(SyncRun.query.count(), 0)

    def test_existing_event_conflict_rolls_back_everything(self):
        first_payload = self.payload()
        self.assertEqual(self.post(first_payload).status_code, 201)
        original_batch_count = GymAssistantInvoiceEventBatch.query.count()
        original_run_count = SyncRun.query.count()
        events = json.loads(json.dumps(first_payload["invoice_membership_events"]))
        events[0]["source_payload_hash"] = "f" * 64
        conflict_payload = self.payload(
            batch_id="invoice-batch-conflict",
            events=events,
        )

        response = self.post(conflict_payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "event_conflict")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), original_batch_count)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), original_run_count)
        self.assertNotEqual(
            GymAssistantJournalEvent.query.filter_by(
                source_reference=events[0]["source_reference"]
            ).one().source_payload_hash,
            "f" * 64,
        )

    def test_later_snapshot_cannot_silently_shrink_nonvoid_history(self):
        first_payload = self.payload()
        self.assertEqual(self.post(first_payload).status_code, 201)
        replacement_events = [
            self.event(member_id, suffix="replacement")
            for member_id in COHORT
        ]
        replacement_payload = self.payload(
            batch_id="invoice-batch-shrink",
            events=replacement_events,
        )

        response = self.post(replacement_payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "snapshot_regression")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 1)
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), 1)

    def test_controlled_false_to_true_void_transition_voids_linked_invoice(self):
        first_payload = self.payload()
        self.assertEqual(self.post(first_payload).status_code, 201)
        source_event = GymAssistantJournalEvent.query.filter_by(
            member_id=COHORT[0]
        ).one()
        invoice = MemberInvoice(
            source_event_id=source_event.id,
            source_payload_hash=source_event.source_payload_hash,
            member_id=COHORT[0],
            member_name="Batch Member",
            membership_name="Contract Dreamz",
            language="en",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
            payment_date=date(2026, 7, 31),
            payment_method="cc_manual",
            currency="USD",
            paid_amount=Decimal("65.00"),
            status="ready_for_review",
        )
        db.session.add(invoice)
        db.session.commit()

        events = json.loads(json.dumps(first_payload["invoice_membership_events"]))
        events[0]["source_payload_hash"] = "f" * 64
        events[0]["is_voided"] = True
        events[0]["is_positive_membership_payment"] = False
        void_payload = self.payload(
            batch_id="invoice-batch-void",
            events=events,
        )
        response = self.post(void_payload)

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.json["voided"], 1)
        db.session.refresh(source_event)
        db.session.refresh(invoice)
        self.assertTrue(source_event.is_voided)
        self.assertEqual(source_event.source_payload_hash, "f" * 64)
        self.assertEqual(source_event.eligibility_status, "voided")
        self.assertEqual(invoice.status, "void")
        self.assertEqual(invoice.void_reason, "source_event_voided")
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 2)
        self.assertEqual(SyncRun.query.count(), 2)

    def test_global_batch_lock_key_is_stable(self):
        self.assertEqual(invoice_event_batch_lock_key(), invoice_event_batch_lock_key())
        self.assertIsInstance(invoice_event_batch_lock_key(), int)

    def test_invoice_run_does_not_displace_latest_normal_member_sync(self):
        normal_run = SyncRun(
            source="normal-member-sync",
            status="success",
            started_at=datetime.now() - timedelta(minutes=6),
            completed_at=datetime.now() - timedelta(minutes=5),
            members_received=2,
        )
        db.session.add(normal_run)
        db.session.commit()

        response = self.post(self.payload())

        self.assertEqual(response.status_code, 201)
        self.assertEqual(SyncRun.query.count(), 2)
        self.assertTrue(sync_run_is_latest(normal_run))
        self.assertEqual(fep_latest_sync_meta()["id"], normal_run.id)

    def test_invoice_run_does_not_interrupt_legacy_three_run_promotion(self):
        member_ids = list(COHORT)
        member_ids_sha256 = hashlib.sha256(
            "\n".join(sorted(member_ids)).encode("utf-8")
        ).hexdigest()
        base_time = datetime.now() - timedelta(minutes=6)
        legacy_runs = []
        for index in range(3):
            run = SyncRun(
                source="legacy-member-sync",
                status="success",
                started_at=base_time + timedelta(minutes=index * 2),
                completed_at=base_time + timedelta(minutes=index * 2),
                members_received=len(member_ids),
                member_source="legacy-members.btx",
                member_unique_count=len(member_ids),
                member_ids_sha256=member_ids_sha256,
                member_ids_json=json.dumps(member_ids),
                member_snapshot_protocol="legacy_candidate",
            )
            db.session.add(run)
            legacy_runs.append(run)
        db.session.commit()

        response = self.post(self.payload())
        promoted, reason = legacy_member_snapshot_promotion(legacy_runs[-1])

        self.assertEqual(response.status_code, 201)
        self.assertTrue(promoted, reason)
        self.assertIn("Three highly overlapping legacy snapshots", reason)

    def test_invoice_run_does_not_displace_staff_or_member_context_health(self):
        normal_run = SyncRun(
            source="stale-normal-member-sync",
            status="success",
            started_at=datetime.now() - timedelta(minutes=46),
            completed_at=datetime.now() - timedelta(minutes=45),
            members_received=2,
            change_summary=json.dumps({
                "new_members": [{
                    "member_id": COHORT[0],
                    "name": "Batch Member",
                    "plan_type": "Contract Dreamz",
                }],
                "changed_members": [],
                "document_changes": [],
                "suspicious_name_changes": [],
            }),
        )
        db.session.add(normal_run)
        db.session.commit()
        self.assertEqual(self.post(self.payload()).status_code, 201)

        with app.test_request_context("/staff/member-context"):
            context = latest_member_sync_context(COHORT[0])
        with self.client.session_transaction() as session_data:
            session_data["staff_role"] = "admin"
            session_data["staff_username"] = "ron"
        response = self.client.get("/staff/sync")

        self.assertEqual(context["latest"]["source"], "stale-normal-member-sync")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("stale-normal-member-sync", body)
        self.assertNotIn("invoice_event_batch", body)
        self.assertIn("Gym Assistant sync is not checking in", body)
        self.assertIn("Showing sync runs 1-1 of 1", body)

    def test_exact_backup_builder_payload_matches_endpoint_contract(self):
        journal = "\n".join((
            (
                "c20260215!1558 7001 1771185480 0 990001 91001 3 0 0 0 29"
                "|1144074289 257 20260201 20260301 6500 0 0 6500 0 0 0"
            ),
            (
                "c20260215!1559 7002 1771185540 0 990002 91002 3 0 0 0 29"
                "|1144074289 257 20260201 20260301 6500 0 0 6500 0 0 0"
            ),
        ))
        catalog = "\n".join((
            "CLASS=Contract Dreamz",
            "MEMBERTYPE_ID=1144074289",
            "OPTION=1 MONTHS EFT 6500 0",
            "-",
        ))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup = root / "GABackup-e2e.gbu"
            allowlist = root / "allowlist.txt"
            allowlist.write_text("91001\n91002\n", encoding="ascii")
            with zipfile.ZipFile(backup, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Journal.jtx", journal)
                archive.writestr("Members.btx", catalog)
            backup_bytes = backup.read_bytes()
            payload = batch_module.build_exact_invoice_event_batch(
                backup,
                expected_length=len(backup_bytes),
                expected_sha256=hashlib.sha256(backup_bytes).hexdigest(),
                allowlist_path=allowlist,
                expected_allowlist_count=2,
                expected_allowlist_set_sha256=invoice_event_member_ids_sha256(set(COHORT)),
                expected_target_event_count=2,
                expected_short_fragment_count=0,
                generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            )

        response = self.post(payload)

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        verified = batch_module.verify_invoice_batch_receipt(payload, response.json)
        self.assertEqual(verified["status"], "success")
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        self.assertEqual(GymAssistantInvoiceSyncState.query.count(), 2)
        self.assertEqual(GymAssistantInvoiceEventBatch.query.count(), 1)
        self.assertEqual(SyncRun.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
