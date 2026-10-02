from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import unittest


os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from dreamz_portal import (  # noqa: E402
    GymAssistantJournalEvent,
    GymAssistantInvoiceSyncState,
    Member,
    MemberInvoice,
    MemberInvoiceRequest,
    MemberInvoiceRequestAudit,
    MemberPortalPreference,
    app,
    create_ga_membership_invoice_draft,
    db,
    invoice_event_issue_blockers,
    member_invoice_integrity_issues,
)


MEMBER_ID = "92001"


class InvoiceOnDemandTests(unittest.TestCase):
    def setUp(self):
        self.original_config = {
            key: app.config.get(key)
            for key in (
                "TESTING",
                "SYNC_API_TOKEN",
                "STAFF_TOKEN",
                "INVOICE_ISSUING_ENABLED",
                "INVOICE_NUMBER_SERIES_APPROVED",
                "INVOICE_GA_PILOT_ENABLED",
                "INVOICE_GA_PILOT_MEMBER_IDS",
                "INVOICE_ON_DEMAND_ENABLED",
                "INVOICE_ON_DEMAND_MEMBER_IDS",
                "INVOICE_ON_DEMAND_PLAN_TYPES",
                "INVOICE_ON_DEMAND_MAX_MEMBERS",
                "INVOICE_GA_PILOT_SYNC_MAX_AGE_MINUTES",
                "INVOICE_FORCE_LOCAL_STORAGE",
                "INVOICE_STORAGE_ROOT",
                "_RUNTIME_SCHEMA_READY",
            )
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            SYNC_API_TOKEN="invoice-on-demand-sync-token",
            STAFF_TOKEN="staff-token-test",
            INVOICE_ISSUING_ENABLED=False,
            INVOICE_NUMBER_SERIES_APPROVED=True,
            INVOICE_GA_PILOT_ENABLED=False,
            INVOICE_GA_PILOT_MEMBER_IDS=set(),
            INVOICE_ON_DEMAND_ENABLED=True,
            INVOICE_ON_DEMAND_MEMBER_IDS={MEMBER_ID},
            INVOICE_ON_DEMAND_PLAN_TYPES="",
            INVOICE_ON_DEMAND_MAX_MEMBERS=2,
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
        self.add_member()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.temp_dir.cleanup()
        for key, value in self.original_config.items():
            app.config[key] = value

    def add_member(self, member_id=MEMBER_ID, email=None):
        member = Member(
            member_id=member_id,
            name=f"Member, {member_id}",
            email=email or f"member-{member_id}@example.com",
            plan_type="contract Dreamz 6 months",
            billing_amount=65.0,
            last_payment=date(2026, 8, 30),
        )
        db.session.add(member)
        db.session.commit()
        return member

    def member_session(self, *, language="nl", csrf="csrf-member"):
        if language:
            preference = db.session.get(MemberPortalPreference, MEMBER_ID)
            if not preference:
                db.session.add(MemberPortalPreference(
                    member_id=MEMBER_ID,
                    invoice_language=language,
                ))
                db.session.commit()
        with self.client.session_transaction() as member_session:
            member_session.clear()
            member_session["member_id"] = MEMBER_ID
            member_session["member_auth_version"] = 0
            member_session["language"] = language or "en"
            member_session["_csrf_token"] = csrf

    def staff_session(self, *, csrf="csrf-staff", role="admin"):
        with self.client.session_transaction() as staff_session:
            staff_session.clear()
            staff_session["staff_role"] = role
            staff_session["staff_username"] = "ron"
            staff_session["_csrf_token"] = csrf

    def request_invoice(self, *, csrf="csrf-member"):
        self.member_session(csrf=csrf)
        return self.client.post(
            "/account/invoices/request",
            data={"csrf_token": csrf},
        )

    def event(self, *, suffix="a", occurred_at=None, transaction_id=990792, **overrides):
        values = {
            "source_reference": "ga-journal:" + (suffix * 64),
            "source_payload_hash": suffix * 64,
            "member_id": MEMBER_ID,
            "event_name": "membership_renewal",
            "event_type": 3,
            "is_voided": False,
            "occurred_at": occurred_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "journal_sequence": "1",
            "journal_transaction_id": transaction_id,
            "membership_type_id": 900000101,
            "billing_option_code": 1024,
            "service_period_start": "2026-08-01",
            "service_period_end_exclusive": "2026-09-01",
            "dues_cents": 6500,
            "other_contract_fee_cents": 0,
            "source_tax_cents": 0,
            "tender_total_cents": 6500,
            "remittance_type": 2,
            "remittance_reference": -1,
            "balance_payment_cents": 0,
            "catalog_plan_name": "contract Dreamz 6 months",
            "catalog_base_amount_cents": 6500,
            "catalog_interval_count": 1,
            "catalog_interval_unit": "MONTHS",
            "catalog_match_status": "match",
            "catalog_period_match_status": "match",
        }
        values.update(overrides)
        return values

    def sync(self, *, events=None, member_ids=None, source_snapshot_at=None):
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        response = self.client.post(
            "/api/sync/members",
            json={
                "source": "frontdesk-on-demand-test",
                "generated_at": now,
                "members": [{"member_id": MEMBER_ID}],
                "documents": {},
                "invoice_pilot_member_ids": [MEMBER_ID] if member_ids is None else member_ids,
                "invoice_membership_events": events or [],
                "invoice_journal_issue_count": 0,
                "invoice_journal_source": "C:/Gym Assistant 2.6/Data/Backup/test.gbu::Journal.jtx",
                "invoice_catalog_source": "C:/Gym Assistant 2.6/Data/Backup/test.gbu::Members.btx",
                "invoice_source_snapshot_at": source_snapshot_at or now,
                "invoice_source_sha256": "d" * 64,
                "invoice_catalog_sha256": "e" * 64,
            },
            headers={"X-Sync-Token": "invoice-on-demand-sync-token"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def test_admin_can_request_source_without_impersonating_member(self):
        self.member_session(language="nl")
        self.staff_session()
        self.assertEqual(self.client.post('/staff/invoice-requests').status_code, 400)
        for _ in range(2):
            response = self.client.post('/staff/invoice-requests', data={
                'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
            })
            self.assertEqual(response.status_code, 302)
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.requested_language, 'nl')
        self.assertEqual(invoice_request.status, 'pending_sync')
        audit = MemberInvoiceRequestAudit.query.one()
        self.assertEqual(audit.action, 'staff_request')
        self.assertEqual(audit.actor, 'ron')
        self.assertEqual(MemberInvoice.query.count(), 0)

    def test_admin_request_rejects_nonallowlisted_member_and_disabled_feature(self):
        self.member_session(language="nl")
        self.staff_session()
        app.config['INVOICE_ON_DEMAND_MEMBER_IDS'] = {'99999'}
        response = self.client.post('/staff/invoice-requests', data={
            'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
        })
        self.assertEqual(response.status_code, 403)
        app.config['INVOICE_ON_DEMAND_MEMBER_IDS'] = {MEMBER_ID}
        app.config['INVOICE_ON_DEMAND_ENABLED'] = False
        response = self.client.post('/staff/invoice-requests', data={
            'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MemberInvoiceRequest.query.count(), 0)

    def test_plan_rule_opens_requests_for_own_memberships_only(self):
        from dreamz_portal import invoice_configuration_issues

        app.config["INVOICE_ON_DEMAND_MEMBER_IDS"] = set()
        self.assertIn("on_demand_allowlist_empty", invoice_configuration_issues())
        self.assertEqual(self.request_invoice().status_code, 404)

        app.config["INVOICE_ON_DEMAND_PLAN_TYPES"] = (
            "Contract 12 months 2024; CONTRACT  dreamz 6 Months ;"
        )
        self.assertNotIn("on_demand_allowlist_empty", invoice_configuration_issues())
        self.member_session(language="nl")
        body = self.client.get("/account/invoices").get_data(as_text=True)
        self.assertEqual(self.client.get("/account/invoices").status_code, 200)
        self.assertIn("/account/invoices/request", body)
        self.assertEqual(self.request_invoice().status_code, 302)
        self.assertEqual(MemberInvoiceRequest.query.one().status, "pending_sync")

        for member_id, plan_type, is_active in (
            ("35871", "Delfins Fitness", True),
            ("35872", "Day Pass", True),
            ("35873", None, True),
            ("35874", "contract Dreamz 6 months", False),
        ):
            db.session.add(Member(
                member_id=member_id,
                name=f"Member, {member_id}",
                email=f"member-{member_id}@example.com",
                plan_type=plan_type,
                is_active=is_active,
            ))
            db.session.add(MemberPortalPreference(member_id=member_id, invoice_language="nl"))
            db.session.commit()
            with self.client.session_transaction() as member_session:
                member_session.clear()
                member_session["member_id"] = member_id
                member_session["member_auth_version"] = 0
                member_session["language"] = "nl"
                member_session["_csrf_token"] = "csrf-member"
            self.assertEqual(self.client.get("/account/invoices").status_code, 404, plan_type)
            response = self.client.post(
                "/account/invoices/request",
                data={"csrf_token": "csrf-member"},
            )
            self.assertEqual(response.status_code, 404, plan_type)
        self.assertEqual(MemberInvoiceRequest.query.count(), 1)

    def test_admin_request_requires_browser_admin_not_manager_or_staff_token(self):
        self.member_session(language="nl")
        self.staff_session(role='manager')
        response = self.client.post('/staff/invoice-requests', data={
            'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
        })
        self.assertEqual(response.status_code, 403)
        with self.client.session_transaction() as staff_session:
            staff_session.clear()
            staff_session['_csrf_token'] = 'csrf-staff'
        response = self.client.post('/staff/invoice-requests?token=staff-token-test', data={
            'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(MemberInvoiceRequest.query.count(), 0)

    def test_admin_request_does_not_invent_member_language(self):
        self.staff_session()
        response = self.client.post('/staff/invoice-requests', data={
            'csrf_token': 'csrf-staff', 'member_id': MEMBER_ID,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MemberInvoiceRequest.query.count(), 0)

    def test_member_request_is_csrf_protected_idempotent_and_monitored(self):
        self.member_session()
        self.assertEqual(self.client.post("/account/invoices/request").status_code, 400)

        first = self.client.post(
            "/account/invoices/request",
            data={"csrf_token": "csrf-member"},
        )
        second = self.client.post(
            "/account/invoices/request",
            data={"csrf_token": "csrf-member"},
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(MemberInvoiceRequest.query.count(), 1)
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.member_id, MEMBER_ID)
        self.assertEqual(invoice_request.status, "pending_sync")
        monitor = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "invoice-on-demand-sync-token"},
        )
        self.assertIn(MEMBER_ID, monitor.get_json()["invoice_monitor_member_ids"])

    def test_request_requires_confirmed_member_language(self):
        self.member_session(language=None)
        db.session.query(MemberPortalPreference).delete()
        db.session.commit()

        response = self.client.post(
            "/account/invoices/request",
            data={"csrf_token": "csrf-member"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/account/preferences", response.headers["Location"])
        self.assertEqual(MemberInvoiceRequest.query.count(), 0)

    def test_non_allowlisted_member_cannot_view_or_create_invoice_request(self):
        other_member_id = "92002"
        self.add_member(other_member_id)
        db.session.add(MemberPortalPreference(
            member_id=other_member_id,
            invoice_language="en",
        ))
        db.session.commit()
        with self.client.session_transaction() as member_session:
            member_session.clear()
            member_session["member_id"] = other_member_id
            member_session["member_auth_version"] = 0
            member_session["language"] = "en"
            member_session["_csrf_token"] = "csrf-other"

        page = self.client.get("/account/invoices")
        submitted = self.client.post(
            "/account/invoices/request",
            data={"csrf_token": "csrf-other"},
        )

        self.assertEqual(page.status_code, 404)
        self.assertEqual(submitted.status_code, 404)
        self.assertEqual(MemberInvoiceRequest.query.count(), 0)

    def test_new_event_outside_pilot_requires_request_and_binds_only_newest(self):
        older = self.event(
            suffix="a",
            occurred_at=(datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
            transaction_id=100,
        )
        newest = self.event(suffix="b", transaction_id=200)

        self.sync(events=[newest, older])
        self.assertEqual(GymAssistantJournalEvent.query.count(), 0)

        self.request_invoice()
        self.sync(events=[older, newest])

        self.assertEqual(GymAssistantJournalEvent.query.count(), 1)
        source_event = GymAssistantJournalEvent.query.one()
        self.assertEqual(source_event.journal_transaction_id, 200)
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_ready")
        self.assertEqual(invoice_request.source_event_id, source_event.id)
        self.assertEqual(invoice_request.source_payload_hash, source_event.source_payload_hash)

    def test_request_created_after_agent_target_read_waits_one_cycle(self):
        self.request_invoice()

        response = self.sync(events=[], member_ids=[])

        self.assertEqual(response.get_json()["status"], "success")
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "pending_sync")
        self.assertIsNone(invoice_request.source_event_id)

    def test_request_never_falls_back_from_newest_blocked_event(self):
        self.request_invoice()
        older = self.event(
            suffix="a",
            occurred_at=(datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
            transaction_id=100,
        )
        newest = self.event(
            suffix="b",
            transaction_id=200,
            other_contract_fee_cents=1000,
            tender_total_cents=7500,
        )

        self.sync(events=[older, newest])

        invoice_request = MemberInvoiceRequest.query.one()
        source_event = db.session.get(GymAssistantJournalEvent, invoice_request.source_event_id)
        self.assertEqual(source_event.journal_transaction_id, 200)
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(MemberInvoice.query.count(), 0)

    def test_kill_switch_removes_unbound_request_from_monitor(self):
        self.request_invoice()
        app.config["INVOICE_ON_DEMAND_ENABLED"] = False

        monitor = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "invoice-on-demand-sync-token"},
        )

        self.assertNotIn(MEMBER_ID, monitor.get_json()["invoice_monitor_member_ids"])
        self.member_session()
        self.assertEqual(
            self.client.post(
                "/account/invoices/request",
                data={"csrf_token": "csrf-member"},
            ).status_code,
            404,
        )

    def test_clean_scan_without_event_moves_to_manual_source_check_and_admin_can_retry(self):
        self.request_invoice()
        self.sync(events=[])
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(invoice_request.open_member_key, MEMBER_ID)
        prior_reason = invoice_request.status_reason
        self.assertTrue(prior_reason)

        self.staff_session()
        response = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/retry",
            data={"csrf_token": "csrf-staff"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "pending_sync")
        audit = MemberInvoiceRequestAudit.query.one()
        self.assertEqual(audit.request_id, invoice_request.id)
        self.assertEqual(audit.action, "retry")
        self.assertEqual(audit.from_status, "source_blocked")
        self.assertEqual(audit.to_status, "pending_sync")
        self.assertEqual(audit.prior_status_reason, prior_reason)
        self.assertEqual(audit.actor, "ron")

    def test_retry_preserves_bound_source_and_draft_preimage_once(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        db.session.refresh(invoice_request)
        invoice = db.session.get(MemberInvoice, invoice_request.invoice_id)
        invoice_request.status = "source_blocked"
        invoice_request.status_reason = "operator_source_review_required"
        db.session.commit()
        expected = {
            "source_event_id": invoice_request.source_event_id,
            "source_payload_hash": invoice_request.source_payload_hash,
            "source_sync_run_id": invoice_request.source_sync_run_id,
            "source_snapshot_sha256": invoice_request.source_snapshot_sha256,
            "source_bound_at": invoice_request.source_bound_at,
            "invoice_id": invoice_request.invoice_id,
            "invoice_binding_key": invoice_request.invoice_binding_key,
            "prepared_at": invoice_request.prepared_at,
            "prepared_by": invoice_request.prepared_by,
            "open_member_key": invoice_request.open_member_key,
        }

        first = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/retry",
            data={"csrf_token": "csrf-staff"},
        )
        second = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/retry",
            data={"csrf_token": "csrf-staff"},
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(MemberInvoiceRequestAudit.query.count(), 1)
        audit = MemberInvoiceRequestAudit.query.one()
        for field, value in expected.items():
            self.assertEqual(getattr(audit, field), value, field)
        self.assertEqual(audit.prior_status_reason, "operator_source_review_required")
        db.session.refresh(invoice_request)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.status, "pending_sync")
        self.assertIsNone(invoice_request.source_event_id)
        self.assertIsNone(invoice_request.invoice_id)
        self.assertIsNone(invoice_request.invoice_binding_key)
        self.assertEqual(invoice.status, "superseded")

    def test_exact_request_can_be_prepared_reviewed_issued_and_downloaded(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()

        self.staff_session()
        prepared = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        self.assertEqual(prepared.status_code, 302)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "draft_ready")
        invoice = db.session.get(MemberInvoice, invoice_request.invoice_id)
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.source_event_id, invoice_request.source_event_id)

        app.config["INVOICE_ISSUING_ENABLED"] = True
        issued = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.assertEqual(issued.status_code, 302)
        db.session.refresh(invoice_request)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.status, "issued")
        self.assertIsNone(invoice_request.open_member_key)
        self.assertEqual(invoice.status, "issued")
        self.assertEqual(invoice_event_issue_blockers(invoice.source_event), [])
        self.assertEqual(member_invoice_integrity_issues(invoice), [])
        app.config["INVOICE_ON_DEMAND_ENABLED"] = False
        state = db.session.get(GymAssistantInvoiceSyncState, MEMBER_ID)
        state.source_snapshot_at = datetime.now() - timedelta(days=1)
        db.session.commit()
        self.assertEqual(member_invoice_integrity_issues(invoice), [])
        self.assertTrue(Path(invoice.storage_uri).is_file())
        monitor = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "invoice-on-demand-sync-token"},
        )
        self.assertIn(MEMBER_ID, monitor.get_json()["invoice_monitor_member_ids"])

        self.member_session()
        overview = self.client.get("/account/invoices")
        download = self.client.get(f"/account/invoices/{invoice.id}/download")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("no-store", overview.headers.get("Cache-Control", ""))
        self.assertIn(invoice.invoice_number, overview.get_data(as_text=True))
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/pdf")

    def test_hex_sequence_selects_a_over_9_at_the_same_timestamp(self):
        self.request_invoice()
        occurred_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        sequence_9 = self.event(
            suffix="a",
            occurred_at=occurred_at,
            journal_sequence="9",
            transaction_id=900,
        )
        sequence_a = self.event(
            suffix="b",
            occurred_at=occurred_at,
            journal_sequence="A",
            transaction_id=100,
        )

        self.sync(events=[sequence_a, sequence_9])

        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_ready")
        self.assertEqual(invoice_request.source_event.source_reference, sequence_a["source_reference"])

    def test_pilot_and_request_use_the_same_hex_latest_ordering(self):
        app.config["INVOICE_GA_PILOT_ENABLED"] = True
        app.config["INVOICE_GA_PILOT_MEMBER_IDS"] = {MEMBER_ID}
        self.request_invoice()
        occurred_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        sequence_9 = self.event(
            suffix="a",
            occurred_at=occurred_at,
            journal_sequence="9",
            transaction_id=900,
        )
        sequence_a = self.event(
            suffix="b",
            occurred_at=occurred_at,
            journal_sequence="A",
            transaction_id=100,
        )

        self.sync(events=[sequence_9, sequence_a])

        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.source_event.source_reference, sequence_a["source_reference"])

    def test_older_issued_source_is_voided_while_new_request_binds_newest(self):
        old_event = self.event(
            suffix="a",
            occurred_at=(datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
            transaction_id=100,
        )
        self.request_invoice()
        self.sync(events=[old_event])
        first_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{first_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        app.config["INVOICE_ISSUING_ENABLED"] = True
        first_invoice = MemberInvoice.query.one()
        self.client.post(
            f"/staff/invoices/{first_invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.request_invoice()
        second_request = MemberInvoiceRequest.query.order_by(MemberInvoiceRequest.id.desc()).first()
        new_event = self.event(suffix="b", transaction_id=200)
        reversed_old = dict(old_event)
        reversed_old["source_payload_hash"] = "c" * 64
        reversed_old["is_voided"] = True

        self.sync(events=[new_event, reversed_old])

        db.session.refresh(first_request)
        db.session.refresh(second_request)
        db.session.refresh(first_invoice)
        self.assertEqual(first_request.status, "void")
        self.assertEqual(first_invoice.status, "void")
        self.assertEqual(second_request.status, "source_ready")
        self.assertEqual(second_request.source_event.source_reference, new_event["source_reference"])

    def test_newer_payment_after_source_binding_blocks_the_request(self):
        old_event = self.event(
            suffix="a",
            occurred_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
            transaction_id=100,
        )
        self.request_invoice()
        self.sync(events=[old_event])
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_ready")
        new_event = self.event(suffix="b", transaction_id=200)

        self.sync(events=[old_event, new_event])

        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(invoice_request.status_reason, "newer_membership_event_detected")
        self.assertEqual(GymAssistantJournalEvent.query.count(), 2)

    def test_newer_payment_after_draft_blocks_issue(self):
        old_event = self.event(
            suffix="a",
            occurred_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
            transaction_id=100,
        )
        self.request_invoice()
        self.sync(events=[old_event])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        new_event = self.event(suffix="b", transaction_id=200)
        self.sync(events=[old_event, new_event])
        db.session.refresh(invoice_request)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(invoice.status, "superseded")
        self.assertIsNone(invoice_request.invoice_id)
        self.assertIsNone(invoice_request.invoice_binding_key)

        app.config["INVOICE_ISSUING_ENABLED"] = True
        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(invoice)
        self.assertNotEqual(invoice.status, "issued")

        retry = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/retry",
            data={"csrf_token": "csrf-staff"},
        )
        self.assertEqual(retry.status_code, 302)
        self.sync(events=[old_event, new_event])
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "source_ready")
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "draft_ready")
        self.assertNotEqual(invoice_request.invoice_id, invoice.id)
        self.assertEqual(MemberInvoice.query.count(), 2)
        self.assertEqual(db.session.get(MemberInvoice, invoice.id).status, "superseded")

    def test_stale_snapshot_never_becomes_source_ready(self):
        self.request_invoice()
        stale_at = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).replace(microsecond=0).isoformat()

        self.sync(events=[self.event()], source_snapshot_at=stale_at)

        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(invoice_request.status_reason, "source_snapshot_is_stale")

    def test_existing_draft_cannot_bypass_current_source_blockers(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        invoice, created = create_ga_membership_invoice_draft(invoice_request.source_event)
        self.assertTrue(created)
        state = db.session.get(GymAssistantInvoiceSyncState, MEMBER_ID)
        state.source_snapshot_at = datetime.now() - timedelta(hours=1)
        db.session.commit()
        self.staff_session()

        response = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(invoice_request)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.status, "source_ready")
        self.assertIsNone(invoice_request.invoice_binding_key)
        self.assertEqual(invoice.status, "ready_for_review")

    def test_open_request_requires_exact_prepare_binding_before_issue(self):
        app.config["INVOICE_GA_PILOT_ENABLED"] = True
        app.config["INVOICE_GA_PILOT_MEMBER_IDS"] = {MEMBER_ID}
        event_payload = self.event()
        self.sync(events=[event_payload])
        source_event = GymAssistantJournalEvent.query.one()
        invoice, created = create_ga_membership_invoice_draft(
            source_event,
            actor="system:pilot",
        )
        self.assertTrue(created)
        db.session.commit()
        self.request_invoice()
        self.sync(events=[event_payload])
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "source_ready")
        self.assertIn(
            "invoice_request_prepare_required",
            member_invoice_integrity_issues(invoice),
        )
        app.config["INVOICE_ISSUING_ENABLED"] = True
        self.staff_session()

        blocked = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )

        self.assertEqual(blocked.status_code, 302)
        db.session.refresh(invoice)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice.status, "ready_for_review")
        self.assertEqual(invoice_request.status, "source_ready")
        self.assertIsNone(invoice_request.invoice_binding_key)
        self.assertIsNone(invoice.invoice_number)
        self.assertIsNone(invoice.storage_uri)
        self.assertIsNone(invoice.pdf_sha256)

        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "draft_ready")
        self.assertEqual(MemberInvoice.query.count(), 1)
        self.assertEqual(invoice_request.invoice_id, invoice.id)
        self.assertEqual(invoice_request.invoice_binding_key, str(invoice.id))
        self.assertNotIn(
            "invoice_request_prepare_required",
            member_invoice_integrity_issues(invoice),
        )
        issued = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.assertEqual(issued.status_code, 302)
        db.session.refresh(invoice)
        self.assertEqual(invoice.status, "issued")

    def test_request_language_is_immutable_through_review_and_issue(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        preference = db.session.get(MemberPortalPreference, MEMBER_ID)
        preference.invoice_language = "es"
        db.session.commit()

        overview = self.client.get("/staff/invoices")
        self.assertEqual(overview.status_code, 200)
        self.assertIn("no-store", overview.headers.get("Cache-Control", ""))
        self.assertIn(b">NL</dd>", overview.data)
        app.config["INVOICE_ISSUING_ENABLED"] = True
        issued = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        self.assertEqual(issued.status_code, 302)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.requested_language, "nl")
        self.assertEqual(invoice.language, "nl")
        self.assertEqual(member_invoice_integrity_issues(invoice), [])

    def test_repeat_request_for_same_issued_event_does_not_rebind_language(self):
        self.request_invoice()
        source_event = self.event()
        self.sync(events=[source_event])
        first_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{first_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        app.config["INVOICE_ISSUING_ENABLED"] = True
        self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        preference = db.session.get(MemberPortalPreference, MEMBER_ID)
        preference.invoice_language = "es"
        db.session.commit()
        self.request_invoice()
        second_request = MemberInvoiceRequest.query.order_by(MemberInvoiceRequest.id.desc()).first()
        self.sync(events=[source_event])
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{second_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )

        db.session.refresh(first_request)
        db.session.refresh(second_request)
        db.session.refresh(invoice)
        self.assertEqual(first_request.invoice_binding_key, str(invoice.id))
        self.assertIsNone(second_request.invoice_binding_key)
        self.assertEqual(second_request.status, "issued")
        self.assertEqual(second_request.status_reason, "invoice_already_issued")
        self.assertEqual(invoice.language, "nl")
        self.assertEqual(member_invoice_integrity_issues(invoice), [])

    def test_source_conflict_is_invalidated_not_reported_as_payment_void(self):
        original = self.event(suffix="a")
        self.request_invoice()
        self.sync(events=[original])
        invoice_request = MemberInvoiceRequest.query.one()
        changed = dict(original)
        changed["source_payload_hash"] = "b" * 64

        self.sync(events=[changed])

        db.session.refresh(invoice_request)
        state = db.session.get(GymAssistantInvoiceSyncState, MEMBER_ID)
        self.assertEqual(invoice_request.status, "invalidated")
        self.assertEqual(invoice_request.status_reason, "source_payload_changed")
        self.assertGreater(state.journal_issue_count, 0)

    def test_conflict_on_new_pending_request_becomes_admin_recoverable(self):
        app.config["INVOICE_GA_PILOT_ENABLED"] = True
        app.config["INVOICE_GA_PILOT_MEMBER_IDS"] = {MEMBER_ID}
        original = self.event(suffix="a")
        self.sync(events=[original])
        self.request_invoice()
        invoice_request = MemberInvoiceRequest.query.one()
        self.assertEqual(invoice_request.status, "pending_sync")
        changed = dict(original)
        changed["source_payload_hash"] = "b" * 64

        self.sync(events=[changed])

        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "source_blocked")
        self.assertEqual(invoice_request.status_reason, "source_payload_changed")
        self.assertEqual(invoice_request.open_member_key, MEMBER_ID)
        self.staff_session()
        retried = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/retry",
            data={"csrf_token": "csrf-staff"},
        )
        self.assertEqual(retried.status_code, 302)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "pending_sync")
        self.assertEqual(MemberInvoiceRequestAudit.query.count(), 1)

    def test_source_conflict_after_issue_preserves_legal_invoice_for_review(self):
        original = self.event(suffix="a")
        self.request_invoice()
        self.sync(events=[original])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        app.config["INVOICE_ISSUING_ENABLED"] = True
        self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )
        changed = dict(original)
        changed["source_payload_hash"] = "b" * 64

        self.sync(events=[changed])

        db.session.refresh(invoice_request)
        db.session.refresh(invoice)
        self.assertEqual(invoice_request.status, "issued")
        self.assertEqual(invoice_request.status_reason, "source_payload_changed")
        self.assertEqual(invoice.status, "issued")
        self.member_session()
        self.assertEqual(
            self.client.get(f"/account/invoices/{invoice.id}/download").status_code,
            200,
        )

    def test_conflict_for_tracked_member_does_not_block_clean_other_request(self):
        other_member_id = "92002"
        app.config["INVOICE_ON_DEMAND_MEMBER_IDS"] = {MEMBER_ID, other_member_id}
        self.add_member(other_member_id)
        db.session.add(MemberPortalPreference(
            member_id=other_member_id,
            invoice_language="en",
        ))
        db.session.commit()
        with app.test_request_context("/account/invoices/request"):
            from dreamz_portal import create_member_invoice_request

            other_request, _ = create_member_invoice_request(other_member_id)
            db.session.commit()
        other_event = self.event(
            suffix="a",
            member_id=other_member_id,
            occurred_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
        )
        self.sync(events=[other_event], member_ids=[other_member_id])
        db.session.refresh(other_request)
        self.assertEqual(other_request.status, "source_ready")
        self.request_invoice()
        clean_request = MemberInvoiceRequest.query.filter_by(member_id=MEMBER_ID).one()
        changed_other = dict(other_event)
        changed_other["source_payload_hash"] = "c" * 64
        clean_event = self.event(suffix="b")

        self.sync(
            events=[changed_other, clean_event],
            member_ids=[other_member_id, MEMBER_ID],
        )

        db.session.refresh(other_request)
        db.session.refresh(clean_request)
        self.assertEqual(other_request.status, "invalidated")
        self.assertEqual(clean_request.status, "source_ready")
        self.assertEqual(clean_request.source_event.source_reference, clean_event["source_reference"])

    def test_admin_can_close_blocked_request_and_member_can_request_again(self):
        self.request_invoice()
        self.sync(events=[])
        invoice_request = MemberInvoiceRequest.query.one()
        prior_reason = invoice_request.status_reason
        self.staff_session()

        missing_confirmation = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/close",
            data={"csrf_token": "csrf-staff"},
        )
        self.assertEqual(missing_confirmation.status_code, 302)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "source_blocked")

        response = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/close",
            data={"csrf_token": "csrf-staff", "confirm_close": "1"},
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "closed")
        self.assertEqual(invoice_request.closed_by, "ron")
        self.assertIsNone(invoice_request.open_member_key)
        audit = MemberInvoiceRequestAudit.query.one()
        self.assertEqual(audit.action, "close")
        self.assertEqual(audit.from_status, "source_blocked")
        self.assertEqual(audit.to_status, "closed")
        self.assertEqual(audit.prior_status_reason, prior_reason)
        self.assertEqual(audit.transition_reason, "closed_after_manual_source_review")
        monitor = self.client.get(
            "/api/sync/member-ids",
            headers={"X-Sync-Token": "invoice-on-demand-sync-token"},
        )
        self.assertNotIn(MEMBER_ID, monitor.get_json()["invoice_monitor_member_ids"])
        self.request_invoice()
        self.assertEqual(MemberInvoiceRequest.query.count(), 2)

    def test_issue_does_not_rewrite_historical_requests_for_same_source(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        active_request = MemberInvoiceRequest.query.one()
        historical_request = MemberInvoiceRequest(
            member_id=MEMBER_ID,
            status="closed",
            requested_language="en",
            source_event_id=active_request.source_event_id,
            source_payload_hash=active_request.source_payload_hash,
            status_reason="closed_after_manual_source_review",
            requested_at=datetime.now() - timedelta(days=1),
            completed_at=datetime.now() - timedelta(days=1),
            updated_at=datetime.now() - timedelta(days=1),
        )
        db.session.add(historical_request)
        db.session.commit()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{active_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        app.config["INVOICE_ISSUING_ENABLED"] = True

        response = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-staff",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        db.session.refresh(active_request)
        db.session.refresh(historical_request)
        self.assertEqual(active_request.status, "issued")
        self.assertEqual(historical_request.status, "closed")
        self.assertEqual(historical_request.status_reason, "closed_after_manual_source_review")

    def test_bound_source_hash_change_blocks_draft_preparation(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        invoice_request.source_payload_hash = "f" * 64
        db.session.commit()
        self.staff_session()

        response = self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(MemberInvoice.query.count(), 0)
        db.session.refresh(invoice_request)
        self.assertEqual(invoice_request.status, "source_ready")

    def test_staff_token_cannot_prepare_member_owned_request(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        request_id = MemberInvoiceRequest.query.one().id
        with self.client.session_transaction() as staff_session:
            staff_session.clear()
            staff_session["_csrf_token"] = "csrf-token-only"

        response = self.client.post(
            f"/staff/invoice-requests/{request_id}/prepare",
            data={"csrf_token": "csrf-token-only"},
            headers={"X-Staff-Token": "staff-token-test"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(MemberInvoice.query.count(), 0)

    def test_staff_token_cannot_view_or_issue_financial_documents(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        app.config["INVOICE_ISSUING_ENABLED"] = True
        with self.client.session_transaction() as staff_session:
            staff_session.clear()
            staff_session["_csrf_token"] = "csrf-token-only"

        overview = self.client.get(
            "/staff/invoices",
            headers={"X-Staff-Token": "staff-token-test"},
        )
        issued = self.client.post(
            f"/staff/invoices/{invoice.id}/issue",
            data={
                "csrf_token": "csrf-token-only",
                "review_source_confirmed": "1",
                "review_scope_confirmed": "1",
                "review_details_confirmed": "1",
            },
            headers={"X-Staff-Token": "staff-token-test"},
        )

        self.assertEqual(overview.status_code, 403)
        self.assertEqual(issued.status_code, 403)
        db.session.refresh(invoice)
        self.assertNotEqual(invoice.status, "issued")

    def test_manager_cannot_view_issue_or_download_financial_documents(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        self.staff_session(role="manager")

        self.assertIn(self.client.get("/staff/invoices").status_code, {302, 403})
        self.assertIn(
            self.client.post(
                f"/staff/invoices/{invoice.id}/issue",
                data={
                    "csrf_token": "csrf-staff",
                    "review_source_confirmed": "1",
                    "review_scope_confirmed": "1",
                    "review_details_confirmed": "1",
                },
            ).status_code,
            {302, 403},
        )
        self.assertIn(
            self.client.get(f"/staff/invoices/{invoice.id}/download").status_code,
            {302, 403},
        )

    def test_all_invoice_mutations_require_csrf(self):
        self.request_invoice()
        self.sync(events=[self.event()])
        invoice_request = MemberInvoiceRequest.query.one()
        self.staff_session()

        self.assertEqual(
            self.client.post(
                f"/staff/invoice-requests/{invoice_request.id}/prepare"
            ).status_code,
            400,
        )
        self.client.post(
            f"/staff/invoice-requests/{invoice_request.id}/prepare",
            data={"csrf_token": "csrf-staff"},
        )
        invoice = MemberInvoice.query.one()
        self.assertEqual(
            self.client.post(
                f"/staff/invoices/{invoice.id}/issue",
                data={
                    "review_source_confirmed": "1",
                    "review_scope_confirmed": "1",
                    "review_details_confirmed": "1",
                },
            ).status_code,
            400,
        )
        invoice_request.status = "source_blocked"
        db.session.commit()
        self.assertEqual(
            self.client.post(
                f"/staff/invoice-requests/{invoice_request.id}/retry"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/staff/invoice-requests/{invoice_request.id}/close",
                data={"confirm_close": "1"},
            ).status_code,
            400,
        )

    def test_rollout_member_cap_fails_closed(self):
        app.config["INVOICE_ON_DEMAND_MEMBER_IDS"] = {
            MEMBER_ID,
            "92002",
            "92003",
        }
        self.request_invoice()
        self.add_member("92002")
        db.session.add(MemberPortalPreference(member_id="92002", invoice_language="en"))
        db.session.commit()
        with app.test_request_context("/account/invoices/request"):
            from dreamz_portal import create_member_invoice_request

            create_member_invoice_request("92002")
            db.session.commit()

        self.add_member("92003")
        db.session.add(MemberPortalPreference(member_id="92003", invoice_language="en"))
        db.session.commit()
        with app.test_request_context("/account/invoices/request"):
            from dreamz_portal import create_member_invoice_request

            with self.assertRaisesRegex(ValueError, "rollout is full"):
                create_member_invoice_request("92003")


if __name__ == "__main__":
    unittest.main()
