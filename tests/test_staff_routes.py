from datetime import date, datetime
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

from dreamz_portal import AppSetting, CancellationRequest, EmailLog, Member, MemberDocument, StaffUser, SyncRun, app, db, deliver_email, payment_status_for_member  # noqa: E402


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
        self.assertEqual(response.headers["Location"], "/login")

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

    def test_manager_navigation_can_view_operational_pages_without_settings(self):
        self.add_member()
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"
            sess["staff_username"] = "manager"

        response = self.client.get("/staff/data-audit")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(">Audit</a>", body)
        self.assertIn(">Changes</a>", body)
        self.assertIn(">Sync</a>", body)
        self.assertIn("Cancellations", body)
        self.assertIn("Email Log", body)
        self.assertNotIn(">Settings</a>", body)
        self.assertIn("/staff/members/1206", body)

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

    def test_manager_can_view_cancellations_but_not_update(self):
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

        self.assertEqual(response.status_code, 302)
        self.assertIn("/staff/login", response.headers["Location"])

    def test_staff_home_shows_member_portal_and_fep_choices(self):
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "admin"
            sess["staff_username"] = "ron"

        response = self.client.get("/staff")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Dreamz Fitness Staff", body)
        self.assertIn("Member Info Staff/Admin", body)
        self.assertIn("/staff/data-audit", body)
        self.assertIn("FEP Manager", body)
        self.assertIn("https://dreamz-fep.onrender.com/login", body)
        self.assertNotIn(">Audit</a>", body)
        self.assertNotIn(">Sync</a>", body)
        self.assertIn("/staff/logout", body)

    def test_staff_login_username_is_case_insensitive(self):
        app.config["STAFF_MANAGER_USERNAME"] = "manager"
        app.config["STAFF_MANAGER_PASSWORD"] = "manager-pass"

        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
        response = self.client.post(
            "/staff/login",
            data={"username": "Manager", "password": "manager-pass", "csrf_token": "token"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dreamz Fitness Staff", response.get_data(as_text=True))

    def test_staff_login_shows_loading_state_script(self):
        response = self.client.get("/staff/login")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("data-loading-form", body)
        self.assertIn("Logging in...", body)
        self.assertIn("button.disabled = true", body)

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

    def test_staff_login_allows_manager_audit(self):
        app.config["STAFF_MANAGER_USERNAME"] = "manager"
        app.config["STAFF_MANAGER_PASSWORD"] = "manager-pass"
        self.add_member()

        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "token"
        response = self.client.post(
            "/staff/login",
            data={"username": "manager", "password": "manager-pass", "csrf_token": "token"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dreamz Fitness Staff", response.get_data(as_text=True))
        self.assertIn("Member Info Staff/Admin", response.get_data(as_text=True))

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
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AppSetting.query.filter_by(key="notification_to").one().value, "manager@dreamzfitness.com")
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
        status = deliver_email(["member@example.com"], "Test subject", "Test body", cc_addresses=["ron@dreamzfitness.com"])

        self.assertEqual(status, "logged")
        log_entry = EmailLog.query.one()
        self.assertEqual(log_entry.status, "logged")
        self.assertEqual(log_entry.delivery_mode, "log")
        self.assertEqual(log_entry.to_addresses, "member@example.com")
        self.assertEqual(log_entry.cc_addresses, "ron@dreamzfitness.com")
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

    def test_manager_can_view_email_log_but_not_review_or_open_settings(self):
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

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("cancellation request received", body)
        self.assertIn("View only", body)
        self.assertNotIn("Email settings", body)
        self.assertNotIn("Mark reviewed", body)

        review_response = self.client.post(
            "/staff/email-log/1/review",
            data={"csrf_token": "token"},
        )
        self.assertEqual(review_response.status_code, 403)

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
        self.assertIn("ron@dreamzfitness.com", updated.notification_cc)
        self.assertIn("manager@dreamzfitness.com", updated.notification_cc)

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
        with self.client.session_transaction() as sess:
            sess["staff_role"] = "manager"

        response = self.client.get("/staff/members/1206")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Staff member view", response.get_data(as_text=True))

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
