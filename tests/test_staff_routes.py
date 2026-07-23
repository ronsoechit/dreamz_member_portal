from datetime import date, datetime, timedelta
import csv
import importlib.util
import io
import json
import os
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import AppSetting, CancellationRequest, CoachInteraction, DigitalSignatureRecord, EmailLog, GroupClassOccurrence, GroupClassType, LegalDocument, LegalDocumentVersion, LegalTranslation, Member, MemberClassAttendance, MemberClassPlan, MemberDocument, MemberSignedDocument, MembershipApplication, MembershipApplicationStatus, PricingChangeLog, PricingItem, RequiredAgreementRule, ScheduleChangeNotification, StaffUser, SyncRun, app, db, deliver_email, ensure_runtime_schema, payment_status_for_member, pricing_visibility_list, seed_group_class_schedule, seed_legal_documents, seed_pricing_catalog  # noqa: E402


class FakeS3Body:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunks(self):
        return iter(self._chunks)


class StaffRouteTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["STAFF_TOKEN"] = "staff-test-token"
        app.config["EMAIL_DELIVERY_MODE"] = "log"
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
        app.config["STAFF_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    def test_root_redirects_to_member_login(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/choose-language", response.headers["Location"])

    def add_request(self, **overrides):
        data = {
            "member_id": "1206",
            "requested_at": datetime(2026, 5, 24, 10, 15),
            "member_name": "Example Member",
            "member_email": "member@example.com",
            "status": "accepted",
            "policy_status": "allowed_in_window",
            "mail_status": "sent",
            "reason": "Moving away",
            "plan_type": "contract Dreamz 12 m",
            "contract_type": "12-months",
            "term_months": 12,
            "current_term_end": date(2026, 6, 23),
            "window_open": date(2026, 5, 24),
            "last_request_date": date(2026, 6, 2),
        }
        data.update(overrides)
        record = CancellationRequest(**data)
        db.session.add(record)
        db.session.commit()
        return record

    def add_member(self, **overrides):
        data = {
            "member_id": "1206",
            "name": "Damon, Norluze",
            "email": "member@example.com",
            "mobile": "701-0000",
            "plan_type": "contract Dreamz 12 m",
            "contract_type": "12-months",
            "start_date": date(2022, 9, 14),
            "end_date": date(2023, 9, 14),
            "signup_date": date(2022, 9, 14),
            "last_payment": date(2025, 3, 29),
            "next_payment": date(2025, 5, 1),
            "balance": 0.0,
            "photo_path": None,
        }
        data.update(overrides)
        member = Member(**data)
        db.session.add(member)
        db.session.commit()
        return member

    def add_document(self, **overrides):
        data = {
            "member_id": "1206",
            "document_type": "contract",
            "title": "Contract",
            "path": "contracts/1206_contract.pdf",
            "source_filename": "contract.pdf",
            "display_order": 0,
        }
        data.update(overrides)
        document = MemberDocument(**data)
        db.session.add(document)
        db.session.commit()
        return document

    def test_staff_cancellations_requires_token(self):
        self.add_request()

        response = self.client.get("/staff/cancellations")

        self.assertEqual(response.status_code, 403)

    def test_staff_cancellations_lists_requests_with_valid_token(self):
        self.add_request()

        response = self.client.get("/staff/cancellations?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Cancellation Requests", body)
        self.assertIn("Example Member", body)
        self.assertIn("allowed_in_window", body)
        self.assertIn("Moving away", body)
        self.assertIn("new", body)
        self.assertIn("Processed:", body)

    def test_staff_header_shows_open_cancellation_count(self):
        self.add_request(member_id="1206", admin_status="new")
        self.add_request(member_id="2204", admin_status="reviewed")
        self.add_request(member_id="3305", admin_status="processed")
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/cancellations")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Cancellations", body)
        self.assertIn(">2</span>", body)

    def test_manager_navigation_only_shows_allowed_pages(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/staff/changes")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('href="/staff/changes"', body)
        self.assertIn('href="/staff/group-classes"', body)
        self.assertIn('href="/staff/pricing-products"', body)
        self.assertIn('href="/staff/cancellations"', body)
        self.assertIn('href="/staff/logout"', body)
        self.assertIn("Manager Dashboard", body)
        self.assertNotIn("Admin Dashboard", body)
        self.assertNotIn('href="/admin"', body)
        self.assertNotIn('href="/staff"', body)
        self.assertNotIn('href="/staff/data-audit"', body)
        self.assertNotIn('href="/staff/sync"', body)
        self.assertNotIn('href="/staff/coach"', body)
        self.assertNotIn('href="/staff/equipment"', body)
        self.assertNotIn('href="/staff/terms-agreements"', body)
        self.assertNotIn('href="/staff/whatsapp-login"', body)
        self.assertNotIn('href="/staff/email-log"', body)
        self.assertNotIn('href="/staff/settings"', body)

    def test_manager_direct_access_to_other_staff_pages_redirects_to_changes(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        for route in (
            "/admin",
            "/staff",
            "/staff/data-audit",
            "/staff/sync",
            "/staff/coach",
            "/staff/equipment",
            "/staff/terms-agreements",
            "/staff/whatsapp-login",
            "/staff/email-log",
            "/staff/settings",
        ):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/staff/changes")

    def test_staff_terms_agreements_page_lists_seeded_documents_and_warnings(self):
        seed_legal_documents()
        db.session.add(MembershipApplication(
            applicant_first_name="Ron",
            applicant_last_name="Soechit",
            email="ron@example.com",
            selected_membership_type="6 months contract",
            selected_contract_term="6_months",
            selected_payment_method="frontdesk_payment",
            status="submitted",
            language="en",
        ))
        db.session.add(DigitalSignatureRecord(
            full_legal_name="Ron Soechit",
            email="ron@example.com",
            verification_method="email_link",
            audit_reference_number="DS-TEST-001",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/terms-agreements")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Terms and agreements management", body)
        self.assertIn("6-Month Membership Contract", body)
        self.assertIn("Legal review needed", body)
        self.assertIn("Legacy 6-month contract inconsistency", body)
        self.assertIn("Required agreement rules", body)
        self.assertIn("Membership applications", body)
        self.assertIn("Digital signatures", body)
        self.assertIn("Cancellation requests", body)
        self.assertIn("Ron Soechit", body)
        self.assertIn("DS-TEST-001", body)

    def test_staff_terms_admin_can_add_version_rule_signed_document_and_update_application(self):
        seed_legal_documents()
        document = LegalDocument.query.filter_by(document_type="gym_rules").one()
        application = MembershipApplication(
            applicant_first_name="Ana",
            applicant_last_name="Member",
            email="ana@example.com",
            selected_membership_type="No contract / 1 month",
            status="submitted",
            language="en",
        )
        db.session.add(application)
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/terms-agreements/versions",
            data={
                "csrf_token": "token",
                "document_id": str(document.id),
                "version": "2026-06-01-review",
                "effective_from": "2026-06-01",
                "legal_review_status": "reviewed",
                "short_summary": "Updated gym rules",
                "plain_language_summary": "Use the gym safely.",
                "full_legal_text": "Updated gym rules legal text.",
                "is_current": "1",
                "legal_review_needed": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        new_version = LegalDocumentVersion.query.filter_by(document_id=document.id, version="2026-06-01-review").one()
        self.assertTrue(new_version.is_current)
        self.assertEqual(new_version.legal_review_status, "reviewed")

        response = self.client.post(
            "/staff/terms-agreements/translations",
            data={
                "csrf_token": "token",
                "version_id": str(new_version.id),
                "language": "nl",
                "title": "Gymregels",
                "short_summary": "Samenvatting",
                "plain_language_summary": "Gebruik de gym veilig.",
                "full_legal_text": "Nederlandse concepttekst.",
                "translation_status": "reviewed",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(LegalTranslation.query.filter_by(version_id=new_version.id, language="nl").one().translation_status, "reviewed")

        response = self.client.post(
            "/staff/terms-agreements/required-rules",
            data={
                "csrf_token": "token",
                "applies_to_contract_term": "test_term",
                "required_legal_document_types": "gym_rules\nliability_waiver",
                "active": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(RequiredAgreementRule.query.filter_by(applies_to_contract_term="test_term").first())

        response = self.client.post(
            "/staff/terms-agreements/signed-documents",
            data={
                "csrf_token": "token",
                "member_id": "13659",
                "document_type": "gym_rules",
                "file_url": "/documents/signed/gym-rules.pdf",
                "pdf_hash": "hash-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MemberSignedDocument.query.filter_by(member_id="13659", document_type="gym_rules").count(), 1)

        response = self.client.post(
            f"/staff/terms-agreements/applications/{application.id}/status",
            data={"csrf_token": "token", "status": "pending_frontdesk_payment", "note": "Waiting for first payment."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.get(MembershipApplication, application.id).status, "pending_frontdesk_payment")
        self.assertEqual(MembershipApplicationStatus.query.filter_by(application_id=application.id).count(), 1)

    def test_staff_can_download_printable_agreement_pdf_and_generated_application_pdf(self):
        seed_legal_documents()
        application = MembershipApplication(
            applicant_first_name="Ana",
            applicant_last_name="Member",
            email="ana@example.com",
            selected_membership_type="6 months contract",
            selected_payment_method="frontdesk_payment",
            status="signed",
            language="en",
        )
        db.session.add(application)
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        printable = self.client.get("/staff/terms-agreements/templates/gym_rules/en.pdf")

        self.assertEqual(printable.status_code, 200)
        self.assertEqual(printable.mimetype, "application/pdf")
        self.assertTrue(printable.get_data().startswith(b"%PDF-1.4"))
        self.assertIn(b"Dreamz Fitness Bonaire", printable.get_data())
        self.assertIn(b"Version:", printable.get_data())

        html = self.client.get("/staff/terms-agreements/templates/gym_rules/en/print")
        self.assertEqual(html.status_code, 200)
        self.assertIn("Dreamz Fitness Bonaire", html.get_data(as_text=True))
        self.assertIn("Full legal text", html.get_data(as_text=True))

        generated = self.client.get(f"/applications/{application.id}/documents/gym_rules.pdf")
        self.assertEqual(generated.status_code, 200)
        self.assertTrue(generated.get_data().startswith(b"%PDF-1.4"))
        self.assertIn(b"Ana Member", generated.get_data())

    def test_staff_coach_activity_lists_coach_interactions(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        db.session.add(CoachInteraction(
            member_id="13659",
            actor="coach",
            category="answer",
            source="fallback",
            language="en",
            message="Keep the next session controlled.",
            context_summary="goal=build_muscle",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/coach")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Coach Activity", body)
        self.assertIn("Ron Soechit", body)
        self.assertIn("Keep the next session controlled.", body)

    def test_manager_cannot_view_staff_coach_activity(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/staff/coach")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/staff/changes")

    def test_staff_group_classes_requires_staff_access(self):
        response = self.client.get("/staff/group-classes")

        self.assertEqual(response.status_code, 403)

    def test_staff_group_classes_lists_seeded_schedule(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/staff/group-classes")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Group Class Management", body)
        self.assertIn("BODYPUMP", body)
        self.assertIn("RESERVED", body)
        self.assertIn("AEROBICS ROOM", body)

    def test_staff_pricing_requires_staff_access(self):
        response = self.client.get("/staff/pricing-products")

        self.assertEqual(response.status_code, 403)

    def test_staff_pricing_lists_seeded_catalog(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/staff/pricing-products")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Pricing &amp; Product Management", body)
        self.assertIn("No contract / 1 month", body)
        self.assertIn("External Personal Trainer Package", body)
        self.assertIn("All prices and fees are non-negotiable.", body)
        self.assertIn("Changing active prices can affect public and member information.", body)

    def test_staff_new_admin_sections_load_real_seeded_data(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        cases = {
            "/staff/pricing-products": "No contract / 1 month",
            "/staff/group-classes": "BODYPUMP",
        }
        for route, expected in cases.items():
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn(expected, body)
                self.assertNotIn("data is still being prepared", body)

    def test_runtime_schema_repairs_legacy_staff_settings_table(self):
        db.session.remove()
        db.drop_all()
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE staff_user ("
                "id INTEGER PRIMARY KEY, "
                "username VARCHAR UNIQUE NOT NULL, "
                "role VARCHAR NOT NULL, "
                "password_hash VARCHAR NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO staff_user (username, role, password_hash) "
                "VALUES ('legacy-admin', 'admin', 'hash')"
            )

        app.config["_RUNTIME_SCHEMA_READY"] = False
        ensure_runtime_schema()
        user = StaffUser.query.filter_by(username="legacy-admin").one()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.created_at)
        self.assertIsNotNone(user.updated_at)

        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Staff Settings", response.get_data(as_text=True))
        self.assertNotIn("data is still being prepared", response.get_data(as_text=True))

    def test_staff_settings_warns_instead_of_500_when_schema_unavailable(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        previous_testing = app.config["TESTING"]
        app.config["TESTING"] = False
        try:
            with patch("dreamz_portal.ensure_runtime_schema", side_effect=RuntimeError("schema unavailable")):
                response = self.client.get("/staff/settings")
        finally:
            app.config["TESTING"] = previous_testing

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("could not load all live data", body)
        self.assertNotIn("Internal Server Error", body)

    def test_staff_admin_sections_warn_instead_of_500_when_context_fails(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        cases = [
            ("/staff/group-classes", "dreamz_portal.group_class_admin_context"),
            ("/staff/pricing-products", "dreamz_portal.pricing_admin_context"),
            ("/staff/terms-agreements", "dreamz_portal.terms_admin_context"),
        ]
        for route, target in cases:
            with self.subTest(route=route), patch(target, side_effect=RuntimeError("schema unavailable")):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertIn("could not load all live data", body)
                self.assertNotIn("Internal Server Error", body)

    def test_staff_can_update_pricing_item_and_log_change(self):
        seed_pricing_catalog()
        item = PricingItem.query.filter_by(seed_key="membership-no-contract-1-month").one()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/pricing-products/items",
            data={
                "csrf_token": "token",
                "item_id": str(item.id),
                "name": item.name,
                "category_key": item.category_key,
                "description": item.description or "",
                "price_amount": "82.50",
                "currency": "USD",
                "billing_interval": item.billing_interval,
                "duration": item.duration or "",
                "visibility_public": "1",
                "visibility_members": "1",
                "is_active": "1",
                "requires_front_desk_handling": "1",
                "member_eligible": "1",
                "sort_order": str(item.sort_order),
                "terms": "\n".join(json.loads(item.terms)),
                "internal_notes": "Adjusted by staff",
                "change_note": "monthly price update",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        updated = db.session.get(PricingItem, item.id)
        self.assertEqual(updated.price_amount, 82.50)
        self.assertEqual(updated.internal_notes, "Adjusted by staff")
        self.assertEqual(PricingChangeLog.query.filter_by(pricing_item_id=item.id).count(), 1)

    def test_staff_pricing_enforces_external_trainer_not_member_upgrade(self):
        seed_pricing_catalog()
        item = PricingItem.query.filter_by(seed_key="b2b-external-personal-trainer-package").one()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/pricing-products/items",
            data={
                "csrf_token": "token",
                "item_id": str(item.id),
                "name": item.name,
                "category_key": "external_trainer_b2b",
                "description": item.description or "",
                "price_amount": "250",
                "currency": "USD",
                "billing_interval": "per_month",
                "visibility_members": "1",
                "is_active": "1",
                "requires_front_desk_handling": "1",
                "member_eligible": "1",
                "sort_order": str(item.sort_order),
                "terms": "\n".join(json.loads(item.terms)),
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        updated = db.session.get(PricingItem, item.id)
        self.assertFalse(updated.member_eligible)
        self.assertNotIn("members", pricing_visibility_list(updated))
        self.assertTrue({"public_business", "staff_only"} & set(pricing_visibility_list(updated)))

    def test_staff_pricing_preserves_mcb_required_terms(self):
        seed_pricing_catalog()
        item = PricingItem.query.filter_by(seed_key="mcb-direct-debit-6-month-contract").one()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/pricing-products/items",
            data={
                "csrf_token": "token",
                "item_id": str(item.id),
                "name": item.name,
                "category_key": item.category_key,
                "description": item.description or "",
                "price_amount": "70",
                "currency": "USD",
                "billing_interval": "per_month",
                "duration": item.duration or "",
                "visibility_public": "1",
                "visibility_members": "1",
                "is_active": "1",
                "requires_front_desk_handling": "1",
                "member_eligible": "1",
                "contract_only": "1",
                "sort_order": str(item.sort_order),
                "terms": "Custom MCB note",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        terms = " ".join(json.loads(db.session.get(PricingItem, item.id).terms))
        self.assertIn("Current MCB Bank Bonaire accounts only", terms)
        self.assertIn("Direct Debit only", terms)
        self.assertIn("No exceptions", terms)

    def test_staff_can_create_group_class_type(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/group-classes/class-types",
            data={
                "csrf_token": "token",
                "name": "CORE FLOW",
                "category": "core_mobility",
                "intensity": "low_medium",
                "muscle_focus": "core",
                "cardio_load": "low",
                "strength_load": "low_medium",
                "recovery_impact": "low",
                "impact_level": "low",
                "pregnancy_safety_level": "suitable_or_requires_modification",
                "default_bookable": "1",
                "default_publish": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(GroupClassType.query.filter_by(name="CORE FLOW").count(), 1)
        self.assertIn("CORE FLOW", response.get_data(as_text=True))

    def test_staff_schedule_update_notifies_future_member_plans_only(self):
        self.add_member(member_id="13659", name="Ron Soechit")
        seed_group_class_schedule()
        bodypump = (
            GroupClassOccurrence.query
            .join(GroupClassType)
            .filter(GroupClassType.name == "BODYPUMP", GroupClassOccurrence.day_of_week == 0)
            .order_by(GroupClassOccurrence.start_time.asc())
            .first()
        )
        future_plan = MemberClassPlan(
            member_id="13659",
            occurrence_id=bodypump.id,
            class_date=date.today() + timedelta(days=7),
            status="planned",
        )
        past_plan = MemberClassPlan(
            member_id="13659",
            occurrence_id=bodypump.id,
            class_date=date.today() - timedelta(days=7),
            status="planned",
        )
        db.session.add_all([future_plan, past_plan])
        db.session.commit()
        attendance = MemberClassAttendance(
            member_id="13659",
            plan_id=past_plan.id,
            occurrence_id=bodypump.id,
            class_date=past_plan.class_date,
        )
        db.session.add(attendance)
        db.session.commit()

        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/group-classes/occurrences",
            data={
                "csrf_token": "token",
                "occurrence_id": str(bodypump.id),
                "day_of_week": "0",
                "start_time": "20:00",
                "end_time": "21:00",
                "class_type_id": str(bodypump.class_type_id),
                "room": "AEROBICS ROOM",
                "instructor": "Christel",
                "capacity": "24",
                "status": "scheduled",
                "is_bookable": "1",
                "is_published": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        updated = db.session.get(GroupClassOccurrence, bodypump.id)
        self.assertEqual(updated.start_time.strftime("%H:%M"), "20:00")
        self.assertEqual(updated.instructor, "Christel")
        self.assertEqual(updated.capacity, 24)
        self.assertEqual(db.session.get(MemberClassPlan, future_plan.id).status, "adjusted")
        self.assertEqual(db.session.get(MemberClassPlan, past_plan.id).status, "planned")
        self.assertEqual(MemberClassAttendance.query.filter_by(plan_id=past_plan.id).count(), 1)
        notification = ScheduleChangeNotification.query.filter_by(member_id="13659").one()
        self.assertEqual(notification.change_type, "class_time_changed")
        self.assertIn("moved to 20:00", notification.message)

    def test_staff_cancellations_status_filter(self):
        self.add_request(member_id="1206", member_name="Accepted Member", status="accepted")
        self.add_request(
            member_id="9999",
            member_name="Blocked Member",
            status="blocked",
            policy_status="blocked_window_closed",
            mail_status="not_sent",
        )

        response = self.client.get("/staff/cancellations?token=staff-test-token&status=blocked")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Blocked Member", body)
        self.assertNotIn("Accepted Member", body)

    def test_manager_can_view_cancellations_but_not_update_unreviewed_request(self):
        record = self.add_request()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"
            sess["_csrf_token"] = "token"

        response = self.client.get("/staff/cancellations")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Example Member", body)
        self.assertIn("View only", body)
        self.assertNotIn("Export CSV", body)
        self.assertNotIn("name=\"admin_status\"", body)

        update_response = self.client.post(
            f"/staff/cancellations/{record.id}/status",
            data={"csrf_token": "token", "admin_status": "processed"},
        )
        self.assertEqual(update_response.status_code, 403)

    def test_manager_can_complete_reviewed_cancellation_and_send_confirmation(self):
        self.add_member(last_payment=date(2026, 5, 1), next_payment=date(2026, 6, 1))
        record = self.add_request(admin_status="reviewed", staff_note="Ready for manager")
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "christel"
            sess["_csrf_token"] = "token"

        response = self.client.get("/staff/cancellations")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Manager completion", body)
        self.assertIn("Mark completed & send confirmation", body)

        update_response = self.client.post(
            f"/staff/cancellations/{record.id}/status",
            data={"csrf_token": "token", "admin_status": "processed", "staff_note": "Updated in GymAssistant and FEP"},
            follow_redirects=True,
        )

        self.assertEqual(update_response.status_code, 200)
        updated = db.session.get(CancellationRequest, record.id)
        self.assertEqual(updated.admin_status, "processed")
        self.assertEqual(updated.handled_by, "christel")
        self.assertEqual(updated.staff_note, "Updated in GymAssistant and FEP")
        self.assertEqual(updated.notification_to, "member@example.com")
        self.assertEqual(updated.notification_bcc, "ron@dreamzfitness.com")
        self.assertEqual(updated.notification_cc, "")
        email = EmailLog.query.order_by(EmailLog.id.desc()).first()
        self.assertEqual(email.to_addresses, "member@example.com")
        self.assertEqual(email.bcc_addresses, "ron@dreamzfitness.com")

    def test_staff_cancellations_csv_export(self):
        self.add_request()

        response = self.client.get("/staff/cancellations.csv?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_id"], "1206")
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(rows[0]["policy_status"], "allowed_in_window")
        self.assertEqual(rows[0]["mail_status"], "sent")

    def test_payment_status_flags_stale_zero_balance_due_date(self):
        member = self.add_member()

        status = payment_status_for_member(member, today=date(2026, 5, 24))

        self.assertEqual(status["status"], "stale_payment_data")
        self.assertIn("before today", status["reason"])

    def test_staff_data_audit_requires_token(self):
        self.add_member()

        response = self.client.get("/staff/data-audit")

        self.assertEqual(response.status_code, 403)

    def test_staff_home_requires_login(self):
        response = self.client.get("/staff")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Staff Login", body)
        self.assertIn('action="/staff/login"', body)
        self.assertNotIn("FEP Manager", body)

    def test_public_staff_entry_pages_skip_runtime_schema_prepare(self):
        with patch("dreamz_portal.ensure_runtime_schema", side_effect=RuntimeError("should skip")):
            staff_login = self.client.get("/staff/login")
            staff_home = self.client.get("/staff")
            admin_home = self.client.get("/admin")

        self.assertEqual(staff_login.status_code, 200)
        self.assertEqual(staff_home.status_code, 200)
        self.assertEqual(admin_home.status_code, 200)
        self.assertIn("Staff Login", staff_login.get_data(as_text=True))
        self.assertIn("Staff Login", staff_home.get_data(as_text=True))
        self.assertIn("Staff Login", admin_home.get_data(as_text=True))

    def test_unauthorized_sync_api_skips_runtime_schema_prepare(self):
        with patch("dreamz_portal.ensure_runtime_schema", side_effect=RuntimeError("should skip")):
            response = self.client.post("/api/sync/members", json={})

        self.assertEqual(response.status_code, 403)

    def test_staff_home_trailing_slash_redirects(self):
        response = self.client.get("/staff/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/staff")

    def test_staff_home_shows_member_portal_and_fep_choices(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Dreamz Fitness Staff", body)
        self.assertIn("Member Portal Staff/Admin", body)
        self.assertIn("/staff/data-audit", body)
        self.assertIn("FEP GUI", body)
        self.assertIn("https://fep.dreamzfitness.app", body)
        self.assertIn("/admin", body)
        self.assertNotIn(">Audit</a>", body)
        self.assertNotIn(">Sync</a>", body)
        self.assertIn("/staff/logout", body)

    def test_admin_dashboard_is_private_owner_cockpit(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Dreamz Master Dashboard", body)
        self.assertIn("financial.dreamzfitness.app", body)
        self.assertIn("invoices.dreamzfitness.app", body)
        self.assertIn("cash.dreamzfitness.app", body)
        self.assertIn("dreamzfitness.app/staff", body)
        self.assertIn("fep.dreamzfitness.app", body)
        self.assertIn('href="/staff"', body)
        self.assertIn(">Admin</a>", body)

    def test_admin_dashboard_shows_login_when_not_authenticated(self):
        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Staff Login", body)
        self.assertIn('action="/staff/login?next=/admin"', body)
        self.assertIn('name="next" value="/admin"', body)

    def test_staff_login_can_redirect_to_admin_dashboard(self):
        app.config["STAFF_ADMIN_USERNAME"] = "ron"
        app.config["STAFF_ADMIN_PASSWORD"] = "admin-pass"

        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
            sess["language"] = "en"
        response = self.client.post(
            "/staff/login?next=/admin",
            data={"username": "ron", "password": "admin-pass", "csrf_token": "token", "next": "/admin"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dreamz Master Dashboard", response.get_data(as_text=True))

    def test_admin_dashboard_redirects_manager_to_changes(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/staff/changes")

    def test_legacy_staff_master_dashboard_redirects_to_admin(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/master-dashboard")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin")

    def test_staff_login_username_is_case_insensitive(self):
        app.config["STAFF_MANAGER_USERNAME"] = "manager"
        app.config["STAFF_MANAGER_PASSWORD"] = "manager-pass"

        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
            sess["language"] = "en"
        response = self.client.post(
            "/staff/login?next=/admin",
            data={"username": "Manager", "password": "manager-pass", "csrf_token": "token", "next": "/admin"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Daily Changes", body)
        self.assertIn("Manager access is limited", body)
        self.assertNotIn("Dreamz Master Dashboard", body)

    def test_staff_login_uses_configured_fallback_when_schema_unavailable(self):
        app.config["STAFF_MANAGER_USERNAME"] = "manager"
        app.config["STAFF_MANAGER_PASSWORD"] = "manager-pass"
        original_testing = app.config["TESTING"]
        app.config["TESTING"] = False
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
            sess["language"] = "en"
        try:
            with patch("dreamz_portal.ensure_runtime_schema", side_effect=RuntimeError("schema unavailable")):
                response = self.client.post(
                    "/staff/login",
                    data={"username": "Manager", "password": "manager-pass", "csrf_token": "token"},
                    follow_redirects=True,
                )
        finally:
            app.config["TESTING"] = original_testing

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Daily Changes", body)
        self.assertIn("Manager access is limited", body)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["staff_role"], "manager")

    def test_staff_login_shows_loading_state_script(self):
        response = self.client.get("/staff/login")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("data-loading-form", body)
        self.assertIn("Signing in...", body)
        self.assertIn("button.disabled = true", body)
        self.assertIn("staff-login-language", body)
        self.assertNotIn("language-switcher", body)

    def test_staff_data_audit_lists_member_issues(self):
        self.add_member(email="", mobile="", photo_path=None)
        self.add_document(document_type="contract", title="Contract")
        self.add_document(document_type="direct_debit_mandate", title="Direct Debit Mandate", display_order=1)

        response = self.client.get("/staff/data-audit?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Data Audit", body)
        self.assertIn("Norluze Damon", body)
        self.assertIn("Missing Email", body)
        self.assertIn("Missing Phone", body)
        self.assertIn("Stale Payment Data", body)

    def test_staff_daily_changes_groups_sync_changes_by_date(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 5, 25, 18, 5),
            completed_at=datetime(2026, 5, 25, 18, 6),
            members_received=2,
            members_new=1,
            members_updated=1,
            documents_received=1,
            change_summary=json.dumps({
                "new_members": [
                    {"member_id": "34838", "name": "Coffy, Etisienne", "plan_type": "Contract 12 months 2024"}
                ],
                "changed_members": [
                    {
                        "member_id": "1206",
                        "name": "Damon, Norluze",
                        "plan_type": "contract Dreamz 12 months",
                        "changes": [
                            {"label": "Balance", "old": 7.0, "new": 0.0}
                        ],
                    }
                ],
                "document_changes": [
                    {"member_id": "34838", "name": "Coffy, Etisienne", "old_count": 0, "new_count": 2}
                ],
            }),
        ))
        db.session.add(SyncRun(
            source="old-run",
            status="success",
            started_at=datetime(2026, 5, 24, 18, 5),
            completed_at=datetime(2026, 5, 24, 18, 6),
            change_summary=json.dumps({
                "new_members": [
                    {"member_id": "999", "name": "Old Member"}
                ],
                "changed_members": [],
                "document_changes": [],
            }),
        ))
        db.session.commit()

        response = self.client.get("/staff/changes?token=staff-test-token&date=2026-05-25")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Daily Changes", body)
        self.assertIn("New Members", body)
        self.assertIn("Coffy, Etisienne", body)
        self.assertIn("Changed Members", body)
        self.assertIn("Damon, Norluze", body)
        self.assertIn("Balance", body)
        self.assertIn("7.0", body)
        self.assertIn("Document Updates", body)
        self.assertIn("0 -> 2 documents", body)
        self.assertNotIn("Old Member", body)

    def test_staff_daily_changes_falls_back_to_current_member_name_for_new_members(self):
        self.add_member(
            member_id="35007",
            name="Kerkhof, Mike",
            plan_type="Delfins Fitness",
        )
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 6, 4, 13, 21),
            completed_at=datetime(2026, 6, 4, 13, 21),
            members_received=1,
            members_new=1,
            change_summary=json.dumps({
                "new_members": [
                    {"member_id": "35007", "name": "", "plan_type": ""}
                ],
                "changed_members": [],
                "document_changes": [],
            }),
        ))
        db.session.commit()

        response = self.client.get("/staff/changes?token=staff-test-token&date=2026-06-04")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Kerkhof, Mike", body)
        self.assertIn("Delfins Fitness", body)
        self.assertNotIn(">Unknown</span>", body)

    def test_staff_daily_changes_hides_duplicate_active_status_change(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 6, 4, 8, 6),
            completed_at=datetime(2026, 6, 4, 8, 6),
            members_received=1,
            members_updated=1,
            change_summary=json.dumps({
                "new_members": [],
                "changed_members": [
                    {
                        "member_id": "34886",
                        "name": "Castelijn, Bas",
                        "plan_type": "1 WEEK PASS",
                        "changes": [
                            {"field": "billing_status", "label": "Billing status", "old": "ACTIVE", "new": "INACTIVE"},
                            {"field": "is_active", "label": "Active", "old": True, "new": False},
                        ],
                    }
                ],
                "document_changes": [],
            }),
        ))
        db.session.commit()

        response = self.client.get("/staff/changes?token=staff-test-token&date=2026-06-04")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Billing status", body)
        self.assertIn("ACTIVE", body)
        self.assertIn("INACTIVE", body)
        self.assertNotIn(">Active</td>", body)
        self.assertNotIn(">True</td>", body)
        self.assertNotIn(">False</td>", body)

    def test_staff_daily_changes_lists_suspicious_name_changes(self):
        db.session.add(Member(member_id="34933", name="Wissenmansen, Dennis"))
        db.session.add(SyncRun(
            source="Z:\\Data\\Temp Files\\AddedMembers.btx",
            status="success",
            started_at=datetime(2026, 6, 1, 9, 31),
            completed_at=datetime(2026, 6, 1, 9, 31),
            change_summary=json.dumps({
                "new_members": [],
                "changed_members": [],
                "document_changes": [],
                "suspicious_name_changes": [
                    {
                        "member_id": "34933",
                        "name": "Wissenmansen, Dennis",
                        "source": "Z:\\Data\\Temp Files\\AddedMembers.btx",
                        "raw_old": "Wissenmansen, Dennis",
                        "raw_new": "A, Bn",
                        "parsed_old": "Wissenmansen, Dennis",
                        "parsed_new": "A, Bn",
                        "reason": "Blocked suspicious abbreviation-like GymAssistant name change.",
                    }
                ],
            }),
        ))
        db.session.commit()

        response = self.client.get("/staff/changes?token=staff-test-token&date=2026-06-01")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Suspicious Name Changes", body)
        self.assertIn("34933", body)
        self.assertIn("Wissenmansen, Dennis", body)
        self.assertIn("A, Bn", body)
        self.assertIn("AddedMembers.btx", body)

    def test_staff_sync_status_distinguishes_cleaned_warnings_from_backup_stale(self):
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 5, 28, 7, 41),
            completed_at=datetime(2026, 5, 28, 7, 41),
            members_received=1,
            members_new=0,
            members_updated=0,
            documents_received=0,
            error="Ignored 2 blank GymAssistant value(s) over existing member data for 1 member(s).",
            change_summary=json.dumps({"new_members": [], "changed_members": [], "document_changes": []}),
        ))
        db.session.commit()

        response = self.client.get("/staff/sync?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Cleaned", body)
        self.assertNotIn("Backup stale", body)
        self.assertNotIn("Backup may be out of date", body)

    def test_staff_data_audit_paginates_500_members(self):
        for index in range(1, 506):
            self.add_member(
                member_id=str(index),
                name=f"Member, {index:03d}",
                email=f"member{index}@example.com",
                mobile="701-0000",
                photo_path=__file__,
                next_payment=date(2026, 6, 1),
            )

        response = self.client.get("/staff/data-audit?token=staff-test-token&page=2&sort=member&dir=asc")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Showing 501-505 of 505", body)
        self.assertIn("Page 2 of 2", body)
        self.assertIn("505 Member", body)
        self.assertNotIn("001 Member", body)

    def test_staff_login_sends_manager_to_changes(self):
        app.config["STAFF_MANAGER_USERNAME"] = "manager"
        app.config["STAFF_MANAGER_PASSWORD"] = "manager-pass"

        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
        response = self.client.post(
            "/staff/login",
            data={"username": "manager", "password": "manager-pass", "csrf_token": "token"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Daily Changes", body)
        self.assertIn("Manager access is limited", body)
        self.assertNotIn("Member Portal Staff/Admin", body)

    def test_staff_settings_updates_notifications_and_staff_user(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/settings",
            data={
                "csrf_token": "token",
                "admin_email": "ron@dreamzfitness.com",
                "notification_to": "manager@dreamzfitness.com",
                "notification_cc": "ron@dreamzfitness.com",
                "always_cc_admin": "1",
                "notify_login_code_requests": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AppSetting.query.filter_by(key="notification_to").one().value, "manager@dreamzfitness.com")
        self.assertEqual(AppSetting.query.filter_by(key="notify_login_code_requests").one().value, "1")
        self.assertIn("Staff settings updated.", response.get_data(as_text=True))

    def test_staff_settings_can_create_new_staff_user(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            "/staff/settings",
            data={
                "csrf_token": "token",
                "admin_email": "ron@dreamzfitness.com",
                "notification_to": "ron@dreamzfitness.com",
                "notification_cc": "",
                "always_cc_admin": "1",
                "new_username": "frontdesk",
                "new_role": "manager",
                "new_email": "frontdesk@dreamzfitness.com",
                "new_password": "secret-pass",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        user = StaffUser.query.filter_by(username="frontdesk").one()
        self.assertEqual(user.role, "manager")
        self.assertEqual(user.email, "frontdesk@dreamzfitness.com")

    def test_staff_settings_can_send_test_email(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        with patch("dreamz_portal.send_staff_test_email", return_value="logged") as send_test:
            response = self.client.post(
                "/staff/settings",
                data={
                    "csrf_token": "token",
                    "action": "send_test_email",
                    "test_email": "ron@dreamzfitness.com",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        send_test.assert_called_once_with("ron@dreamzfitness.com")
        self.assertIn("Test email logged", response.get_data(as_text=True))

    def test_deliver_email_records_log_entry(self):
        status = deliver_email(
            ["member@example.com"],
            "Test subject",
            "Test body",
            cc_addresses=["manager@dreamzfitness.com"],
            bcc_addresses=["ron@dreamzfitness.com"],
        )

        self.assertEqual(status, "logged")
        log_entry = EmailLog.query.one()
        self.assertEqual(log_entry.status, "logged")
        self.assertEqual(log_entry.delivery_mode, "log")
        self.assertEqual(log_entry.to_addresses, "member@example.com")
        self.assertEqual(log_entry.cc_addresses, "manager@dreamzfitness.com")
        self.assertEqual(log_entry.bcc_addresses, "ron@dreamzfitness.com")
        self.assertEqual(log_entry.subject, "Test subject")
        self.assertEqual(log_entry.body, "Test body")

    def test_staff_email_log_lists_generated_emails(self):
        db.session.add(EmailLog(
            delivery_mode="log",
            status="logged",
            to_addresses="member@example.com",
            cc_addresses="ron@dreamzfitness.com",
            subject="Dreamz Fitness - cancellation request received",
            body="Email body",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/email-log")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Email Log", body)
        self.assertIn("cancellation request received", body)
        self.assertIn("member@example.com", body)

    def test_staff_email_log_review_count_ignores_login_code_logs(self):
        db.session.add(EmailLog(
            delivery_mode="log",
            status="logged",
            to_addresses="member@example.com",
            subject="Dreamz Fitness - member portal login code",
            body="Login code",
        ))
        db.session.add(EmailLog(
            delivery_mode="log",
            status="logged",
            to_addresses="member@example.com",
            subject="Dreamz Fitness - cancellation request received",
            body="Cancellation",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff/email-log?review=open")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("cancellation request received", body)
        self.assertNotIn("member portal login code", body)

    def test_manager_cannot_view_email_log_review_or_settings(self):
        db.session.add(EmailLog(
            delivery_mode="log",
            status="logged",
            to_addresses="member@example.com",
            subject="Dreamz Fitness - cancellation request received",
            body="Cancellation",
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"
            sess["_csrf_token"] = "token"

        response = self.client.get("/staff/email-log?review=open")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/staff/changes")

        review_response = self.client.post(
            "/staff/email-log/1/review",
            data={"csrf_token": "token"},
        )
        self.assertEqual(review_response.status_code, 403)

        settings_response = self.client.get("/staff/settings")
        self.assertEqual(settings_response.status_code, 302)
        self.assertEqual(settings_response.headers["Location"], "/staff/changes")

    def test_cancellation_status_update_to_processed_sends_confirmation(self):
        self.add_member(last_payment=date(2026, 5, 1), next_payment=date(2026, 6, 1))
        db.session.add(StaffUser(
            username="manager2",
            role="manager",
            email="manager@dreamzfitness.com",
            password_hash="test",
            is_active=True,
        ))
        db.session.commit()
        record = self.add_request()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            f"/staff/cancellations/{record.id}/status",
            data={"csrf_token": "token", "admin_status": "processed", "staff_note": "Handled in GymAssistant"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        updated = db.session.get(CancellationRequest, record.id)
        self.assertEqual(updated.admin_status, "processed")
        self.assertEqual(updated.handled_by, "ron")
        self.assertEqual(updated.staff_note, "Handled in GymAssistant")
        self.assertIsNotNone(updated.confirmed_at)
        self.assertEqual(updated.last_paid_date, date(2026, 5, 28))
        self.assertEqual(updated.access_until, date(2026, 6, 30))
        self.assertIn("Final payment date: 28 May 2026", updated.confirmation_body)
        self.assertIn("Access until: 30 June 2026", updated.confirmation_body)
        self.assertEqual(updated.notification_cc, "")
        self.assertEqual(updated.notification_bcc, "ron@dreamzfitness.com")
        email = EmailLog.query.order_by(EmailLog.id.desc()).first()
        self.assertEqual(email.to_addresses, "member@example.com")
        self.assertEqual(email.bcc_addresses, "ron@dreamzfitness.com")

    def test_processed_cancellation_is_locked_without_admin_override(self):
        self.add_member()
        record = self.add_request(admin_status="processed", confirmed_at=datetime(2026, 5, 24, 18, 3))
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            f"/staff/cancellations/{record.id}/status",
            data={"csrf_token": "token", "admin_status": "reviewed", "staff_note": "Accidental edit"},
        )

        self.assertEqual(response.status_code, 409)
        updated = db.session.get(CancellationRequest, record.id)
        self.assertEqual(updated.admin_status, "processed")
        self.assertNotEqual(updated.staff_note, "Accidental edit")

    def test_processed_cancellation_allows_explicit_admin_correction(self):
        self.add_member()
        record = self.add_request(
            admin_status="processed",
            confirmed_at=datetime(2026, 5, 24, 18, 3),
            mail_status="logged",
            staff_note="Original note",
        )
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"
            sess["_csrf_token"] = "token"

        response = self.client.post(
            f"/staff/cancellations/{record.id}/status",
            data={
                "csrf_token": "token",
                "admin_override": "1",
                "admin_status": "processed",
                "staff_note": "Corrected note",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        updated = db.session.get(CancellationRequest, record.id)
        self.assertEqual(updated.admin_status, "processed")
        self.assertEqual(updated.staff_note, "Corrected note")
        self.assertEqual(updated.mail_status, "logged")

    def test_manager_can_open_staff_member_detail(self):
        self.add_member()
        db.session.add(SyncRun(
            source="unit-test",
            status="success",
            started_at=datetime(2026, 6, 4, 8, 5),
            completed_at=datetime(2026, 6, 4, 8, 6),
            change_summary=json.dumps({"new_members": [], "changed_members": [], "document_changes": []}),
        ))
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"

        response = self.client.get("/staff/members/1206")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Staff member view", body)
        self.assertIn("Latest sync", body)
        self.assertIn("unit-test", body)
        self.assertNotIn("{&#39;status&#39;:", body)

    def test_staff_member_detail_explains_member_only_cancellation_button(self):
        today = date.today()
        self.add_member(
            plan_type="contract Dreamz 6 months",
            contract_type="6-months",
            start_date=today - timedelta(days=153),
            end_date=today + timedelta(days=30),
            signup_date=today - timedelta(days=153),
        )
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"

        response = self.client.get("/staff/members/1206")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Membership &amp; billing", body)
        self.assertIn("Contact &amp; identity", body)
        self.assertIn("Recent sync changes", body)
        self.assertIn("Payment data may be stale", body)
        self.assertNotIn("{&#39;status&#39;:", body)
        self.assertNotIn("I want to cancel my contract", body)

    def test_manager_can_view_staff_member_document(self):
        self.add_member()
        document = self.add_document(path="s3://dreamz-test/portal/Data/Attachments/0001206/contract.pdf")
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"

        viewer_response = self.client.get(f"/documents/item/{document.id}")
        self.assertEqual(viewer_response.status_code, 200)
        self.assertIn(f"/documents/item/{document.id}/file", viewer_response.get_data(as_text=True))

        with patch("dreamz_portal.open_s3_object", return_value=FakeS3Body([b"%PDF staff"])):
            file_response = self.client.get(f"/documents/item/{document.id}/file")

        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.mimetype, "application/pdf")
        self.assertEqual(file_response.get_data(), b"%PDF staff")

    def test_manager_can_view_staff_member_photo(self):
        self.add_member(member_id="1206", photo_path="s3://dreamz-test/portal/Data/Pictures/0001206.jpg")
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"

        with patch("dreamz_portal.open_s3_object", return_value=FakeS3Body([b"photo"])):
            response = self.client.get("/member-photo/1206")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b"photo")

    def test_admin_can_open_member_detail(self):
        self.add_member()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"

        response = self.client.get("/staff/members/1206")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Staff member view", response.get_data(as_text=True))

    def test_no_contract_member_does_not_require_contract_or_mandate(self):
        self.add_member(
            plan_type="no contract 1 month",
            contract_type="No-Contract",
            email="member@example.com",
            mobile="701-0000",
            photo_path=__file__,
            next_payment=date(2026, 6, 1),
        )

        response = self.client.get("/staff/data-audit?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("Missing Contract", body)
        self.assertNotIn("Missing Direct Debit Mandate", body)

    def test_staff_data_audit_issue_filter(self):
        self.add_member(member_id="1206", name="With, Photo", photo_path="missing.jpg")
        self.add_member(
            member_id="2204",
            name="Clean, Member",
            email="clean@example.com",
            mobile="701-0000",
            next_payment=date(2026, 6, 1),
            photo_path=__file__,
        )
        self.add_document(member_id="2204", document_type="signup_form", title="Signup Form")
        self.add_document(member_id="2204", document_type="contract", title="Contract", display_order=1)
        self.add_document(member_id="2204", document_type="direct_debit_mandate", title="Direct Debit Mandate", display_order=2)

        response = self.client.get("/staff/data-audit?token=staff-test-token&issue=missing_photo")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Photo With", body)
        self.assertNotIn("Member Clean", body)

    def test_staff_data_audit_plan_filter(self):
        self.add_member(member_id="1206", name="Contract, Member", plan_type="contract Dreamz 12 m")
        self.add_member(member_id="2204", name="Delfins, Member", plan_type="Delfins Fitness")

        response = self.client.get("/staff/data-audit?token=staff-test-token&plan=Delfins+Fitness")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("on Delfins Fitness", body)
        self.assertIn("Member Delfins", body)
        self.assertNotIn("Member Contract", body)

    def test_staff_data_audit_csv_export(self):
        self.add_member(email="", mobile="", photo_path=None)

        response = self.client.get("/staff/data-audit.csv?token=staff-test-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_id"], "1206")
        self.assertEqual(rows[0]["payment_status"], "stale_payment_data")
        self.assertIn("missing_email", rows[0]["issues"])


if __name__ == "__main__":
    unittest.main()
