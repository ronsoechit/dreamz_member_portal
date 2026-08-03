from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import os
import tempfile
import unittest

from flask import session
from pypdf import PdfReader

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from dreamz_portal import (
    GymAssistantInvoiceSyncState,
    GymAssistantJournalEvent,
    Member,
    MemberInvoice,
    MemberInvoiceLine,
    MemberPortalPreference,
    SyncRun,
    app,
    create_ga_membership_invoice_draft,
    db,
    invoice_event_freshness_issues,
    issue_member_invoice,
    member_invoice_integrity_issues,
    reconcile_ga_membership_invoice_drafts,
    start_member_session,
)

PILOT_MEMBER_ID = "90001"
PILOT_TRANSACTION_ID = 990792


class MemberInvoicePilotTests(unittest.TestCase):
    def setUp(self):
        self.original_config = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "SYNC_API_TOKEN",
                "INVOICE_ISSUING_ENABLED",
                "INVOICE_NUMBER_SERIES_APPROVED",
                "INVOICE_GA_PILOT_ENABLED",
                "INVOICE_GA_PILOT_MEMBER_IDS",
                "INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES",
                "INVOICE_FORCE_LOCAL_STORAGE",
                "INVOICE_STORAGE_ROOT",
                "_RUNTIME_SCHEMA_READY",
            )
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            SYNC_API_TOKEN="invoice-sync-token",
            INVOICE_ISSUING_ENABLED=False,
            INVOICE_NUMBER_SERIES_APPROVED=True,
            INVOICE_GA_PILOT_ENABLED=True,
            INVOICE_GA_PILOT_MEMBER_IDS={PILOT_MEMBER_ID},
            INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES=30,
            INVOICE_FORCE_LOCAL_STORAGE=True,
            INVOICE_STORAGE_ROOT=self.temp_dir.name,
            _RUNTIME_SCHEMA_READY=False,
        )
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp_dir.cleanup()
        for key, value in self.original_config.items():
            app.config[key] = value

    def add_pilot_member(self, **overrides):
        values = {
            "member_id": PILOT_MEMBER_ID,
            "name": "Member, Pilot",
            "email": "pilot.member@example.com",
            "plan_type": "contract Dreamz 6 months",
            "billing_amount": 65.0,
            "last_payment_amount": 432.10,
            "balance": 178.25,
            "last_payment": date(2026, 2, 15),
            "due_date": date(2026, 6, 1),
            "next_payment": date(2026, 6, 1),
        }
        values.update(overrides)
        member = Member(**values)
        db.session.add(member)
        db.session.commit()
        return member

    def event_payload(self, **overrides):
        values = {
            "source_reference": "ga-journal:" + ("a" * 64),
            "source_payload_hash": "b" * 64,
            "member_id": PILOT_MEMBER_ID,
            "event_name": "membership_renewal",
            "event_type": 3,
            "is_voided": False,
            "occurred_at": "2026-02-15T19:56:00+00:00",
            "journal_sequence": "1",
            "journal_transaction_id": PILOT_TRANSACTION_ID,
            "membership_type_id": 900000101,
            "billing_option_code": 1024,
            "service_period_start": "2026-02-01",
            "service_period_end_exclusive": "2026-06-01",
            "dues_cents": 6500,
            "other_contract_fee_cents": 0,
            "source_tax_cents": 0,
            "tender_total_cents": 6500,
            "remittance_type": 2,
            "remittance_reference": -1,
            "balance_payment_cents": 0,
            "catalog_plan_name": "contract Dreamz 6 months",
            "catalog_base_amount_cents": 6500,
            "catalog_interval_count": 4,
            "catalog_interval_unit": "MONTHS",
            "catalog_match_status": "match",
            "catalog_period_match_status": "match",
        }
        values.update(overrides)
        return values

    def sync_event(
        self,
        event=None,
        *,
        generated_at=None,
        source_snapshot_at=None,
        member_ids=None,
        journal_issue_count=0,
        valid_snapshot=True,
        payload_overrides=None,
    ):
        generated_at = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        source_snapshot_at = source_snapshot_at or generated_at
        events = event if isinstance(event, list) else [event or self.event_payload()]
        payload = {
            "source": "frontdesk-invoice-test",
            "generated_at": generated_at,
            "members": [{"member_id": PILOT_MEMBER_ID}],
            "documents": {},
            "invoice_pilot_member_ids": member_ids or [PILOT_MEMBER_ID],
            "invoice_membership_events": events,
            "invoice_journal_issue_count": journal_issue_count,
            "invoice_journal_source": "C:/GymAssistant/Data/Journal.jtx",
            "invoice_catalog_source": "C:/GymAssistant/Data/Members.btx",
            "invoice_source_snapshot_at": (
                source_snapshot_at if valid_snapshot else None
            ),
            "invoice_source_sha256": "d" * 64 if valid_snapshot else None,
            "invoice_catalog_sha256": "e" * 64 if valid_snapshot else None,
        }
        payload.update(payload_overrides or {})
        response = self.client.post(
            "/api/sync/members",
            json=payload,
            headers={"X-Sync-Token": "invoice-sync-token"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def prepare_draft(self, event=None):
        self.sync_event(event)
        source_event = GymAssistantJournalEvent.query.one()
        invoice, created = create_ga_membership_invoice_draft(source_event, actor="ron")
        self.assertTrue(created)
        db.session.commit()
        return invoice

    def enable_issuing(self, language="en"):
        app.config["INVOICE_ISSUING_ENABLED"] = True
        if (
            language
            and Member.query.filter_by(member_id=PILOT_MEMBER_ID).first()
            and not db.session.get(MemberPortalPreference, PILOT_MEMBER_ID)
        ):
            db.session.add(MemberPortalPreference(
                member_id=PILOT_MEMBER_ID,
                invoice_language=language,
            ))
            db.session.commit()

    def test_sync_and_draft_use_membership_dues_only(self):
        self.add_pilot_member(plan_type="new current plan")
        event = self.event_payload(
            tender_total_cents=24325,
            balance_payment_cents=17825,
        )

        self.sync_event(event)

        source_event = GymAssistantJournalEvent.query.one()
        self.assertEqual(source_event.eligibility_status, "eligible")
        self.assertEqual(source_event.eligibility_reason, "account_component_excluded")
        invoice, created = create_ga_membership_invoice_draft(source_event, actor="ron")
        db.session.commit()

        self.assertTrue(created)
        self.assertEqual(invoice.paid_amount, Decimal("65.00"))
        self.assertEqual(invoice.membership_name, "contract Dreamz 6 months")
        self.assertEqual(invoice.service_period_start, date(2026, 2, 1))
        self.assertEqual(invoice.service_period_end, date(2026, 5, 31))
        self.assertEqual(invoice.payment_method, "cc_manual")
        self.assertEqual(invoice.payment_reference, f"GA {PILOT_TRANSACTION_ID}")
        self.assertEqual(len(invoice.lines), 1)
        self.assertEqual(invoice.lines[0].category, "membership")
        self.assertEqual(invoice.lines[0].amount, Decimal("65.00"))
        self.assertNotEqual(invoice.paid_amount, Decimal("178.25"))
        self.assertNotEqual(invoice.paid_amount, Decimal("432.10"))

    def test_disabled_pilot_imports_no_financial_events(self):
        self.add_pilot_member()
        app.config["INVOICE_GA_PILOT_ENABLED"] = False

        self.sync_event()

        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)
        summary = json.loads(SyncRun.query.one().change_summary)
        self.assertEqual(summary["invoice_membership_events"]["status"], "disabled")

    def test_other_amount_and_dues_mismatch_are_blocked(self):
        self.add_pilot_member()
        self.sync_event(self.event_payload(
            dues_cents=7500,
            other_contract_fee_cents=1000,
            tender_total_cents=8500,
        ))

        event = GymAssistantJournalEvent.query.one()
        self.assertEqual(event.eligibility_status, "blocked")
        self.assertEqual(event.eligibility_reason, "non_membership_components_require_review")
        with self.assertRaises(ValueError):
            create_ga_membership_invoice_draft(event)

    def test_catalog_price_mismatch_blocks_possible_addon_bundle(self):
        self.add_pilot_member(billing_amount=75.0)
        self.sync_event(self.event_payload(
            dues_cents=7500,
            tender_total_cents=7500,
            catalog_base_amount_cents=6500,
            catalog_match_status="mismatch",
        ))

        event = GymAssistantJournalEvent.query.one()

        self.assertEqual(event.eligibility_status, "blocked")
        self.assertEqual(event.eligibility_reason, "catalog_price_mismatch")

    def test_membership_payment_using_account_credit_is_blocked(self):
        self.add_pilot_member()
        self.sync_event(self.event_payload(
            tender_total_cents=6000,
            balance_payment_cents=-500,
        ))

        event = GymAssistantJournalEvent.query.one()

        self.assertEqual(event.eligibility_status, "blocked")
        self.assertEqual(
            event.eligibility_reason,
            "membership_payment_uses_account_credit",
        )

    def test_stale_sync_blocks_draft_and_issue(self):
        self.add_pilot_member()
        self.sync_event()
        event = GymAssistantJournalEvent.query.one()
        state = db.session.get(GymAssistantInvoiceSyncState, PILOT_MEMBER_ID)
        state.source_snapshot_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        db.session.commit()

        self.assertIn("source_snapshot_is_stale", invoice_event_freshness_issues(event))
        with self.assertRaises(ValueError):
            create_ga_membership_invoice_draft(event)

    def test_source_parse_issue_blocks_draft(self):
        self.add_pilot_member()
        self.sync_event(journal_issue_count=1)
        event = GymAssistantJournalEvent.query.one()

        self.assertIn("source_parse_issues", invoice_event_freshness_issues(event))
        with self.assertRaises(ValueError):
            create_ga_membership_invoice_draft(event)

    def test_reconcile_only_considers_latest_event_for_member(self):
        self.add_pilot_member()
        older = self.event_payload(
            source_reference="ga-journal:" + ("1" * 64),
            source_payload_hash="2" * 64,
            journal_transaction_id=990700,
            occurred_at="2026-01-15T19:56:00+00:00",
            service_period_start="2025-09-01",
            service_period_end_exclusive="2026-01-01",
        )
        latest = self.event_payload(
            source_reference="ga-journal:" + ("3" * 64),
            source_payload_hash="4" * 64,
            journal_transaction_id=990701,
            occurred_at="2026-02-15T19:56:00+00:00",
        )
        self.sync_event([older, latest])

        created, blocked = reconcile_ga_membership_invoice_drafts(actor="staff")
        db.session.commit()

        self.assertEqual(blocked, [])
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].source_event.journal_transaction_id, 990701)
        self.assertEqual(MemberInvoice.query.count(), 1)

    def test_reconcile_does_not_fall_back_when_latest_event_is_blocked(self):
        self.add_pilot_member()
        older = self.event_payload(
            source_reference="ga-journal:" + ("5" * 64),
            source_payload_hash="6" * 64,
            journal_transaction_id=990702,
            occurred_at="2026-01-15T19:56:00+00:00",
            service_period_start="2025-09-01",
            service_period_end_exclusive="2026-01-01",
        )
        latest = self.event_payload(
            source_reference="ga-journal:" + ("7" * 64),
            source_payload_hash="8" * 64,
            journal_transaction_id=990703,
            occurred_at="2026-02-15T19:56:00+00:00",
            dues_cents=7500,
            tender_total_cents=7500,
            catalog_base_amount_cents=6500,
            catalog_match_status="mismatch",
        )
        self.sync_event([older, latest])

        created, blocked = reconcile_ga_membership_invoice_drafts(actor="staff")

        self.assertEqual(created, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(MemberInvoice.query.count(), 0)

    def test_existing_older_draft_cannot_be_issued_after_newer_event_arrives(self):
        self.add_pilot_member()
        older_payload = self.event_payload(
            source_reference="ga-journal:" + ("9" * 64),
            source_payload_hash="a" * 64,
            journal_transaction_id=990704,
            occurred_at="2026-01-15T19:56:00+00:00",
            service_period_start="2025-09-01",
            service_period_end_exclusive="2026-01-01",
        )
        self.sync_event(older_payload)
        older_event = GymAssistantJournalEvent.query.one()
        older_invoice, _ = create_ga_membership_invoice_draft(
            older_event,
            actor="staff",
        )
        db.session.commit()
        latest_payload = self.event_payload(
            source_reference="ga-journal:" + ("b" * 64),
            source_payload_hash="c" * 64,
            journal_transaction_id=990705,
            occurred_at="2026-02-15T19:56:00+00:00",
        )
        self.sync_event([older_payload, latest_payload])
        self.enable_issuing()

        with self.assertRaises(ValueError):
            issue_member_invoice(older_invoice, "staff")

    def test_unreadable_new_snapshot_immediately_invalidates_old_draft(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()

        self.sync_event([], valid_snapshot=False)
        db.session.refresh(invoice)

        issues = invoice_event_freshness_issues(invoice.source_event)
        self.assertIn("source_event_not_in_latest_sync", issues)
        self.assertIn("source_parse_issues", issues)
        self.enable_issuing()
        with self.assertRaises(ValueError):
            issue_member_invoice(invoice, "staff")

    def test_invalid_sync_envelope_immediately_invalidates_old_draft(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()

        self.sync_event(
            [],
            payload_overrides={
                "generated_at": "not-a-date",
                "invoice_journal_issue_count": "not-an-integer",
                "invoice_membership_events": {"not": "a-list"},
            },
        )
        db.session.refresh(invoice)

        issues = invoice_event_freshness_issues(invoice.source_event)
        self.assertIn("source_event_not_in_latest_sync", issues)
        self.assertIn("source_parse_issues", issues)

    def test_missing_expected_member_coverage_invalidates_old_draft(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()

        self.sync_event(
            [],
            payload_overrides={"invoice_pilot_member_ids": []},
        )
        db.session.refresh(invoice)

        issues = invoice_event_freshness_issues(invoice.source_event)
        self.assertIn("source_event_not_in_latest_sync", issues)
        self.assertIn("source_parse_issues", issues)

    def test_voided_source_blocks_prepared_draft(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        void_event = self.event_payload(
            source_payload_hash="c" * 64,
            is_voided=True,
        )

        self.sync_event(void_event)
        db.session.refresh(invoice)

        self.assertTrue(invoice.source_event.is_voided)
        self.assertEqual(invoice.status, "void")
        self.assertEqual(invoice.void_reason, "source_event_voided")
        self.assertIn("source_event_voided", member_invoice_integrity_issues(invoice))
        self.enable_issuing()
        with self.assertRaises(ValueError):
            issue_member_invoice(invoice, "ron")

    def test_voided_source_removes_issued_invoice_from_member_access(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.enable_issuing()
        issue_member_invoice(invoice, "ron")
        db.session.commit()
        app.config["INVOICE_GA_PILOT_ENABLED"] = False
        app.config["INVOICE_GA_PILOT_MEMBER_IDS"] = set()
        self.sync_event(self.event_payload(
            source_payload_hash="f" * 64,
            is_voided=True,
        ))
        with self.client.session_transaction() as session:
            session["member_id"] = PILOT_MEMBER_ID

        updated = db.session.get(MemberInvoice, invoice.id)
        self.assertEqual(updated.status, "void")
        self.assertEqual(
            self.client.get(f"/account/invoices/{invoice.id}/download").status_code,
            404,
        )

        monitor_response = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "invoice-sync-token"},
        )
        self.assertEqual(monitor_response.status_code, 200)
        self.assertIn(
            PILOT_MEMBER_ID,
            monitor_response.get_json()["invoice_monitor_member_ids"],
        )

    def test_disallowed_retail_line_cannot_be_issued(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        invoice.lines[0].category = "drinks"
        db.session.commit()
        self.enable_issuing()

        issues = member_invoice_integrity_issues(invoice)

        self.assertIn("invoice_line_category_not_allowed", issues)
        with self.assertRaises(ValueError):
            issue_member_invoice(invoice, "ron")

    def test_issue_creates_dutch_paid_pdf_without_account_amounts(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.assertEqual(invoice.language, "en")
        db.session.add(MemberPortalPreference(member_id=PILOT_MEMBER_ID, invoice_language="nl"))
        db.session.commit()
        self.enable_issuing()

        issue_member_invoice(invoice, "ron")
        db.session.commit()

        self.assertEqual(invoice.status, "issued")
        self.assertRegex(invoice.invoice_number, r"^DF-\d{4}-000001$")
        self.assertEqual(invoice.subtotal, Decimal("61.32"))
        self.assertEqual(invoice.abb_amount, Decimal("3.68"))
        self.assertEqual(invoice.total_amount, Decimal("65.00"))
        self.assertEqual(invoice.language, "nl")
        pdf_path = Path(invoice.storage_uri)
        self.assertTrue(pdf_path.is_file())
        reader = PdfReader(str(pdf_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("BETAALD", text)
        self.assertIn("Pilot Member", text)
        self.assertIn("303065217", text)
        self.assertIn("$65.00", text)
        self.assertNotIn("$178.25", text)
        self.assertNotIn("$432.10", text)

    def test_missing_language_preference_blocks_before_number_and_pdf(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.enable_issuing(language=None)

        with self.assertRaisesRegex(ValueError, "language preference is not confirmed"):
            issue_member_invoice(invoice, "ron")

        self.assertIsNone(invoice.invoice_number)
        self.assertIsNone(invoice.storage_uri)
        self.assertIsNone(invoice.pdf_sha256)
        self.assertEqual(invoice.status, "ready_for_review")
        self.assertEqual(list(Path(self.temp_dir.name).rglob("*.pdf")), [])

    def test_invalid_language_preference_blocks_instead_of_falling_back_to_english(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        db.session.add(MemberPortalPreference(
            member_id=PILOT_MEMBER_ID,
            invoice_language="invalid",
        ))
        db.session.commit()
        self.enable_issuing(language=None)

        with self.assertRaisesRegex(ValueError, "language preference is not confirmed"):
            issue_member_invoice(invoice, "ron")

        self.assertIsNone(invoice.invoice_number)
        self.assertIsNone(invoice.storage_uri)

    def test_issued_pdf_and_language_remain_immutable_after_preference_change(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        preference = MemberPortalPreference(
            member_id=PILOT_MEMBER_ID,
            invoice_language="nl",
        )
        db.session.add(preference)
        db.session.commit()
        self.enable_issuing()
        issue_member_invoice(invoice, "ron")
        db.session.commit()
        original_number = invoice.invoice_number
        original_hash = invoice.pdf_sha256
        original_bytes = Path(invoice.storage_uri).read_bytes()

        preference.invoice_language = "es"
        preference.updated_at = datetime.now()
        db.session.commit()
        with self.client.session_transaction() as member_session:
            member_session["member_id"] = PILOT_MEMBER_ID

        download = self.client.get(f"/account/invoices/{invoice.id}/download")
        with self.client.session_transaction() as staff_session:
            staff_session.pop("member_id", None)
            staff_session["staff_role"] = "admin"
            staff_session["staff_username"] = "ron"
        staff_page = self.client.get("/staff/invoices")

        db.session.refresh(invoice)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, original_bytes)
        self.assertEqual(staff_page.status_code, 200)
        self.assertIn(">NL</dd>", staff_page.get_data(as_text=True))
        self.assertNotIn(">ES</dd>", staff_page.get_data(as_text=True))
        self.assertEqual(invoice.invoice_number, original_number)
        self.assertEqual(invoice.language, "nl")
        self.assertEqual(invoice.pdf_sha256, original_hash)

    def test_staff_review_requires_all_three_checks(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.enable_issuing()
        with self.client.session_transaction() as session:
            session["staff_role"] = "admin"
            session["staff_username"] = "ron"
            session["_csrf_token"] = "csrf-test"

        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={"csrf_token": "csrf-test", "review_source_confirmed": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(MemberInvoice, invoice.id).status, "ready_for_review")

        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-test",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(MemberInvoice, invoice.id).status, "issued")

    def test_nonstandard_source_period_requires_explicit_confirmation(self):
        self.add_pilot_member()
        invoice = self.prepare_draft(self.event_payload(
            catalog_interval_count=1,
            catalog_period_match_status="mismatch",
        ))
        self.enable_issuing()
        with self.client.session_transaction() as session:
            session["staff_role"] = "admin"
            session["staff_username"] = "ron"
            session["_csrf_token"] = "csrf-test"

        review = {
            "csrf_token": "csrf-test",
            "review_source_confirmed": "1",
            "review_scope_confirmed": "1",
            "review_details_confirmed": "1",
        }
        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data=review,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.get(MemberInvoice, invoice.id).status,
            "ready_for_review",
        )

        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={**review, "review_period_confirmed": "1"},
        )
        self.assertEqual(response.status_code, 302)
        issued = db.session.get(MemberInvoice, invoice.id)
        self.assertEqual(issued.status, "issued")
        self.assertIsNotNone(issued.period_reviewed_at)

    def test_staff_pilot_screen_renders_source_period_and_safe_amount(self):
        self.add_pilot_member()
        self.prepare_draft(self.event_payload(
            tender_total_cents=24325,
            balance_payment_cents=17825,
        ))
        with self.client.session_transaction() as session:
            session["staff_role"] = "admin"
            session["staff_username"] = "ron"

        response = self.client.get("/staff/invoices")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Pilot Member", body)
        self.assertIn("$65.00", body)
        self.assertIn(str(PILOT_TRANSACTION_ID), body)
        self.assertIn("Not selected", body)
        self.assertRegex(body, r'<button type="submit" disabled[^>]*>')
        self.assertIn("$243.25", body)
        self.assertIn("$178.25", body)
        self.assertNotIn("$432.10", body)

    def test_staff_pilot_screen_uses_live_draft_preference(self):
        self.add_pilot_member()
        self.prepare_draft()
        db.session.add(MemberPortalPreference(
            member_id=PILOT_MEMBER_ID,
            invoice_language="nl",
        ))
        db.session.commit()
        self.enable_issuing()
        with self.client.session_transaction() as staff_session:
            staff_session["staff_role"] = "admin"
            staff_session["staff_username"] = "ron"

        response = self.client.get("/staff/invoices")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(">NL</dd>", body)
        self.assertNotIn("Not selected", body)
        self.assertNotRegex(body, r'<button type="submit" disabled[^>]*>')

    def test_member_can_only_download_own_issued_invoice(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.enable_issuing()
        issue_member_invoice(invoice, "ron")
        db.session.commit()
        with self.client.session_transaction() as session:
            session["member_id"] = PILOT_MEMBER_ID
            session["language"] = "nl"

        overview = self.client.get("/account/invoices")
        download = self.client.get(f"/account/invoices/{invoice.id}/download")

        self.assertEqual(overview.status_code, 200)
        self.assertIn(invoice.invoice_number, overview.get_data(as_text=True))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/pdf")
        self.assertIn("private, no-store", download.headers["Cache-Control"])

        db.session.add(Member(member_id="99999", name="Other, Member"))
        db.session.commit()
        with self.client.session_transaction() as session:
            session["member_id"] = "99999"
        self.assertEqual(self.client.get(f"/account/invoices/{invoice.id}/download").status_code, 404)

    def test_issued_invoice_remains_available_after_pilot_is_disabled(self):
        self.add_pilot_member()
        invoice = self.prepare_draft()
        self.enable_issuing()
        issue_member_invoice(invoice, "ron")
        db.session.commit()
        app.config["INVOICE_GA_PILOT_ENABLED"] = False
        app.config["INVOICE_GA_PILOT_MEMBER_IDS"] = set()
        with self.client.session_transaction() as session:
            session["member_id"] = PILOT_MEMBER_ID
            session["language"] = "nl"

        overview = self.client.get("/account/invoices")
        download = self.client.get(f"/account/invoices/{invoice.id}/download")

        self.assertEqual(overview.status_code, 200)
        self.assertIn(invoice.invoice_number, overview.get_data(as_text=True))
        self.assertEqual(download.status_code, 200)

    def test_member_language_choice_is_persisted_for_future_invoice(self):
        self.add_pilot_member()
        with self.client.session_transaction() as session:
            session["member_id"] = PILOT_MEMBER_ID

        response = self.client.get("/language?lang=es&next=/account")

        self.assertEqual(response.status_code, 302)
        preference = db.session.get(MemberPortalPreference, PILOT_MEMBER_ID)
        self.assertEqual(preference.invoice_language, "es")

    def test_first_member_session_initializes_invoice_language(self):
        member = self.add_pilot_member()

        with app.test_request_context("/login"):
            session["language"] = "pap"
            start_member_session(member, password_verified=True)

        preference = db.session.get(MemberPortalPreference, PILOT_MEMBER_ID)
        self.assertEqual(preference.invoice_language, "pap")


if __name__ == "__main__":
    unittest.main()
