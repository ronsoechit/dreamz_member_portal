from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from dreamz_portal import (
    GymAssistantInvoiceEventBatch,
    GymAssistantInvoiceSyncState,
    GymAssistantJournalEvent,
    Member,
    MemberInvoice,
    SyncRun,
    app,
    db,
    invoice_event_member_ids_sha256,
)


class InvoiceReconciliationReportTests(unittest.TestCase):
    COHORT = {str(91000 + index) for index in range(14)}

    def setUp(self):
        self.original_config = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "INVOICE_GA_PILOT_ENABLED",
                "INVOICE_GA_PILOT_MEMBER_IDS",
                "INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES",
                "INVOICE_STORAGE_ROOT",
                "_RUNTIME_SCHEMA_READY",
            )
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            INVOICE_GA_PILOT_ENABLED=True,
            INVOICE_GA_PILOT_MEMBER_IDS=set(self.COHORT),
            INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES=30,
            INVOICE_STORAGE_ROOT=self.temp_dir.name,
            _RUNTIME_SCHEMA_READY=True,
        )
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        self.seed_report_data()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp_dir.cleanup()
        for key, value in self.original_config.items():
            app.config[key] = value

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def source_event(self, member_id, sequence, *, amount=6500, occurred_at=None, **overrides):
        occurred_at = occurred_at or datetime.now() - timedelta(minutes=5)
        values = {
            "source_reference": f"ga-journal:{self.digest(f'{member_id}:{sequence}')}",
            "source_payload_hash": self.digest(f"payload:{member_id}:{sequence}"),
            "member_id": member_id,
            "event_name": "membership_renewal",
            "event_type": 3,
            "is_voided": False,
            "occurred_at": occurred_at,
            "journal_sequence": str(sequence),
            "journal_transaction_id": 800000 + sequence,
            "membership_type_id": 900000101,
            "billing_option_code": 1024,
            "service_period_start": date(2026, 7, 1),
            "service_period_end_exclusive": date(2026, 8, 1),
            "dues_cents": amount,
            "other_contract_fee_cents": 0,
            "source_tax_cents": 0,
            "tender_total_cents": amount,
            "remittance_type": 2,
            "remittance_reference": -1,
            "balance_payment_cents": 0,
            "catalog_plan_name": "Contract Dreamz",
            "catalog_base_amount_cents": amount,
            "catalog_interval_count": 1,
            "catalog_interval_unit": "MONTHS",
            "catalog_match_status": "match",
            "catalog_period_match_status": "match",
            "eligibility_status": "eligible",
            "eligibility_reason": None,
        }
        values.update(overrides)
        return GymAssistantJournalEvent(**values)

    def seed_report_data(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        sync_run = SyncRun(
            source="invoice_event_batch",
            started_at=now,
            completed_at=now,
            status="success",
        )
        db.session.add(sync_run)
        db.session.flush()
        latest_by_member = {}
        for sequence, member_id in enumerate(sorted(self.COHORT, key=int), start=1):
            db.session.add(Member(
                member_id=member_id,
                name=f"Pilot, Member {sequence}",
                email=f"pilot{sequence}@example.com",
                billing_amount=65.0,
            ))
            source_snapshot_at = now - timedelta(hours=2) if sequence == 1 else now
            db.session.add(GymAssistantInvoiceSyncState(
                member_id=member_id,
                source="verified-backup.gbu",
                catalog_source="Members.btx",
                source_snapshot_at=source_snapshot_at,
                source_sha256="a" * 64,
                catalog_sha256="b" * 64,
                events_received=1,
                journal_issue_count=0,
                last_sync_run_id=sync_run.id,
                last_synced_at=now,
            ))
            event_overrides = {}
            if sequence == 2:
                event_overrides = {
                    "catalog_match_status": "mismatch",
                    "catalog_base_amount_cents": 6000,
                    "eligibility_status": "blocked",
                    "eligibility_reason": "catalog_price_mismatch",
                }
            source_event = self.source_event(member_id, sequence, **event_overrides)
            source_event.first_sync_run_id = sync_run.id
            source_event.last_sync_run_id = sync_run.id
            db.session.add(source_event)
            latest_by_member[member_id] = source_event

        first_member = sorted(self.COHORT, key=int)[0]
        older_event = self.source_event(
            first_member,
            99,
            amount=5500,
            occurred_at=now - timedelta(days=40),
        )
        older_event.first_sync_run_id = sync_run.id
        older_event.last_sync_run_id = sync_run.id
        db.session.add(older_event)
        db.session.flush()
        from dreamz_portal import invoice_event_set_sha256
        batch_events = GymAssistantJournalEvent.query.filter_by(
            last_sync_run_id=sync_run.id,
        ).all()
        batch_event_set_sha256 = invoice_event_set_sha256([
            {
                "source_reference": event.source_reference,
                "source_payload_hash": event.source_payload_hash,
            }
            for event in batch_events
        ])
        db.session.add(GymAssistantInvoiceEventBatch(
            batch_id="invoice-reconciliation-test",
            batch_sha256="c" * 64,
            source_sha256="a" * 64,
            event_set_sha256=batch_event_set_sha256,
            member_ids_sha256=invoice_event_member_ids_sha256(self.COHORT),
            member_count=14,
            event_count=15,
            sync_run_id=sync_run.id,
            receipt_json="{}",
            created_at=now,
        ))
        first_event = latest_by_member[first_member]
        db.session.add(MemberInvoice(
            source_event_id=first_event.id,
            source_payload_hash=first_event.source_payload_hash,
            member_id=first_member,
            member_name="Member 1 Pilot",
            membership_name="Contract Dreamz",
            service_period_start=date(2026, 7, 1),
            service_period_end=date(2026, 7, 31),
            payment_date=date(2026, 7, 1),
            payment_method="gym_assistant",
            currency="USD",
            paid_amount=Decimal("65.00"),
            status="ready_for_review",
        ))
        db.session.commit()

    def login(self, role="admin"):
        with self.client.session_transaction() as session:
            session["staff_role"] = role
            session["staff_username"] = "ron"

    def test_report_is_exact_cohort_read_only_and_no_store(self):
        self.login()
        model_counts_before = {
            model: model.query.count()
            for model in (
                GymAssistantInvoiceEventBatch,
                GymAssistantInvoiceSyncState,
                GymAssistantJournalEvent,
                MemberInvoice,
                SyncRun,
            )
        }
        with (
            patch("dreamz_portal.ensure_runtime_schema", side_effect=AssertionError("no schema bootstrap")),
            patch("dreamz_portal.reconcile_ga_membership_invoice_drafts", side_effect=AssertionError("no drafts")),
            patch("dreamz_portal.next_member_invoice_number", side_effect=AssertionError("no number")),
            patch("dreamz_portal.build_paid_invoice_pdf", side_effect=AssertionError("no PDF")),
            patch("dreamz_portal.store_member_invoice_pdf", side_effect=AssertionError("no storage")),
            patch("dreamz_portal.deliver_email", side_effect=AssertionError("no email")),
        ):
            response = self.client.get("/staff/invoices/reconciliation")

        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Pragma"], "no-cache")
        body = response.get_data(as_text=True)
        self.assertEqual(body.count('data-member-id="'), 14)
        for member_id in self.COHORT:
            self.assertIn(f'data-member-id="{member_id}"', body)
        self.assertIn("$65.00", body)
        self.assertNotIn("$55.00", body)
        self.assertIn("Invoice source reconciliation", body)
        self.assertIn("The source contains an additional amount", body)
        self.assertIn("has not been synced recently enough", body)
        self.assertIn("Source snapshot", body)
        self.assertIn("Ready for review", body)
        self.assertIn("Observed event-set SHA-256", body)
        self.assertNotIn("/staff/invoices/reconcile", body)
        self.assertNotIn("/issue", body)
        self.assertEqual(list(self.temp_dir_path_files()), [])
        for model, count in model_counts_before.items():
            self.assertEqual(model.query.count(), count)

    def temp_dir_path_files(self):
        from pathlib import Path
        return Path(self.temp_dir.name).rglob("*")

    def test_report_requires_admin_and_keeps_missing_member_row(self):
        self.assertNotEqual(self.client.get("/staff/invoices/reconciliation").status_code, 200)
        self.login(role="manager")
        self.assertNotEqual(self.client.get("/staff/invoices/reconciliation").status_code, 200)

        missing_member_id = sorted(self.COHORT, key=int)[-1]
        GymAssistantJournalEvent.query.filter_by(member_id=missing_member_id).delete()
        db.session.commit()
        self.login()
        response = self.client.get("/staff/invoices/reconciliation")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body.count('data-member-id="'), 14)
        self.assertIn(f'data-member-id="{missing_member_id}"', body)
        self.assertIn("No imported membership event", body)

    def test_report_fails_batch_hash_closed_and_blocks_missing_member(self):
        self.login()
        initial = self.client.get("/staff/invoices/reconciliation")
        self.assertIn("Matches", initial.get_data(as_text=True))

        member_id = sorted(self.COHORT, key=int)[2]
        event = GymAssistantJournalEvent.query.filter_by(member_id=member_id).first()
        event.source_payload_hash = "f" * 64
        Member.query.filter_by(member_id=member_id).delete()
        db.session.commit()

        response = self.client.get("/staff/invoices/reconciliation")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Does not match", body)
        self.assertIn(
            "The imported event has no matching member record in the portal.",
            body,
        )


if __name__ == "__main__":
    unittest.main()
