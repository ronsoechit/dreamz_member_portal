from datetime import date, datetime, timedelta
import importlib.util
import os
import re
import unittest
from unittest import mock
from urllib.parse import urlsplit

from werkzeug.security import generate_password_hash


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

import dreamz_portal as portal  # noqa: E402
from dreamz_portal import (  # noqa: E402
    AppSetting,
    EmailLog,
    Member,
    MemberEmailCorrectionAttempt,
    MemberEmailCorrectionRequest,
    MemberLoginCode,
    app,
    db,
    reconcile_pending_member_email_corrections,
)


class MemberEmailCorrectionTests(unittest.TestCase):
    """Public-flow regressions for existing members whose GA email is stale."""

    CSRF_TOKEN = "email-correction-csrf"
    MEMBER_ID = "42001"
    MEMBER_NAME = "Existing Member"
    OLD_EMAIL = "old.member@example.com"
    REQUESTED_EMAIL = "new.member@example.com"
    MEMBER_BIRTHDATE = date(1990, 4, 23)

    def setUp(self):
        self.original_config = {
            key: app.config.get(key)
            for key in [
                "TESTING",
                "EMAIL_DELIVERY_MODE",
                "MEMBER_PORTAL_PUBLIC_URL",
                "MEMBER_EMAIL_CORRECTION_MAX_PER_IP_HOUR",
                "MEMBER_EMAIL_CORRECTION_MAX_PER_MEMBER_DAY",
                "MEMBER_EMAIL_CORRECTION_SYNC_TTL_HOURS",
                "MEMBER_EMAIL_CORRECTION_DELIVERY_STALE_MINUTES",
                "MEMBER_EMAIL_CORRECTION_TRUSTED_PROXY_HOPS",
                "MEMBER_EMAIL_CORRECTION_TRUST_RAILWAY_X_REAL_IP",
                "STAFF_TOKEN",
                "_RUNTIME_SCHEMA_READY",
            ]
        }
        app.config["TESTING"] = True
        app.config["EMAIL_DELIVERY_MODE"] = "log"
        app.config["MEMBER_PORTAL_PUBLIC_URL"] = "https://dreamzfitness.app"
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_IP_HOUR"] = 1
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_MEMBER_DAY"] = 1
        app.config["MEMBER_EMAIL_CORRECTION_SYNC_TTL_HOURS"] = 168
        app.config["MEMBER_EMAIL_CORRECTION_DELIVERY_STALE_MINUTES"] = 15
        app.config["MEMBER_EMAIL_CORRECTION_TRUSTED_PROXY_HOPS"] = 0
        app.config["MEMBER_EMAIL_CORRECTION_TRUST_RAILWAY_X_REAL_IP"] = False
        app.config["STAFF_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        db.session.add_all(
            [
                AppSetting(key="admin_email", value="ron@dreamzfitness.com"),
                AppSetting(key="notification_to", value="frontdesk@dreamzfitness.com"),
                AppSetting(key="notification_cc", value=""),
                AppSetting(key="always_cc_admin", value="1"),
            ]
        )
        db.session.commit()
        self.client = app.test_client()
        self.client.get("/language?lang=en&next=/access-help")
        self.sent_messages = []
        self.delivery_patch = mock.patch.object(
            portal,
            "deliver_email",
            side_effect=self.deliver_as_sent,
        )
        self.delivery_mock = self.delivery_patch.start()

    def tearDown(self):
        self.delivery_patch.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        for key, value in self.original_config.items():
            app.config[key] = value

    def add_member(self, **overrides):
        values = {
            "member_id": self.MEMBER_ID,
            "name": self.MEMBER_NAME,
            "email": self.OLD_EMAIL,
            "plan_type": "Contract 12 months 2024",
            "is_active": True,
            "birthdate": self.MEMBER_BIRTHDATE,
        }
        values.update(overrides)
        member = Member(**values)
        db.session.add(member)
        db.session.commit()
        return member

    def csrf_data(self, client=None, **values):
        client = client or self.client
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = self.CSRF_TOKEN
        return {"csrf_token": self.CSRF_TOKEN, **values}

    def request_access(
        self,
        client=None,
        remote_addr="198.51.100.10",
        headers=None,
        process_queue=True,
        **overrides,
    ):
        client = client or self.client
        values = {
            "member_number": self.MEMBER_ID,
            "full_name": self.MEMBER_NAME,
            "birthdate": self.MEMBER_BIRTHDATE.isoformat(),
            "requested_email": self.REQUESTED_EMAIL,
        }
        values.update(overrides)
        response = client.post(
            "/access-help",
            data=self.csrf_data(client=client, **values),
            environ_overrides={"REMOTE_ADDR": remote_addr},
            headers=headers or {},
            follow_redirects=True,
        )
        if process_queue:
            reconcile_pending_member_email_corrections()
        return response

    def deliver_as_sent(
        self,
        to_addresses,
        subject,
        body,
        cc_addresses=None,
        bcc_addresses=None,
        html_body=None,
        redact_log_content=False,
    ):
        self.sent_messages.append({
            "to_addresses": list(to_addresses or []),
            "cc_addresses": list(cc_addresses or []),
            "bcc_addresses": list(bcc_addresses or []),
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "redact_log_content": redact_log_content,
        })
        log = EmailLog(
            delivery_mode="smtp",
            status="sent",
            to_addresses=", ".join(to_addresses or []),
            cc_addresses=", ".join(cc_addresses or []),
            bcc_addresses=", ".join(bcc_addresses or []),
            subject=subject,
            body=("[Sensitive e-mail content redacted]" if redact_log_content else body),
            html_body=(None if redact_log_content else html_body),
        )
        db.session.add(log)
        db.session.commit()
        return "sent"

    @staticmethod
    def confirmation_parts(message):
        combined = "\n".join([message.get("body") or "", message.get("html_body") or ""])
        match = re.search(
            r"https?://[^\s<\"']+(/access-help/confirm/[^\s<\"']+)",
            combined,
        )
        if not match:
            raise AssertionError("The verification email did not contain an access-help confirmation link")
        parsed = urlsplit(match.group(0))
        if not parsed.fragment:
            raise AssertionError("The verification token was not isolated in the URL fragment")
        return parsed.path, parsed.fragment

    @classmethod
    def confirmation_path(cls, message):
        return cls.confirmation_parts(message)[0]

    @classmethod
    def confirmation_token(cls, message):
        return cls.confirmation_parts(message)[1]

    def verification_message(self):
        return next(
            message
            for message in self.sent_messages
            if message["to_addresses"] == [self.REQUESTED_EMAIL]
            and "/access-help/confirm/" in (message["body"] or "")
        )

    def create_and_confirm_request(self):
        response = self.request_access()
        self.assertEqual(response.status_code, 200)
        record = MemberEmailCorrectionRequest.query.one()
        verification_log = EmailLog.query.filter_by(to_addresses=self.REQUESTED_EMAIL).one()
        verification_message = self.verification_message()
        confirmation_path = self.confirmation_path(verification_message)
        confirmation_token = self.confirmation_token(verification_message)

        status_before_get = record.status
        scanner_get = self.client.get(confirmation_path)
        self.assertEqual(scanner_get.status_code, 200)
        db.session.refresh(record)
        self.assertEqual(record.status, status_before_get)

        confirmed = self.client.post(
            confirmation_path,
            data=self.csrf_data(confirmation_token=confirmation_token),
            follow_redirects=True,
        )
        self.assertEqual(confirmed.status_code, 200)
        db.session.refresh(record)
        self.assertEqual(record.status, "waiting_for_gym_assistant_email")
        return record, confirmation_path

    def test_access_help_get_renders_required_fields_and_post_requires_csrf(self):
        response = self.client.get("/access-help")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('name="csrf_token"', body)
        self.assertIn('name="member_number"', body)
        self.assertIn('name="full_name"', body)
        self.assertIn('name="birthdate"', body)
        self.assertIn('name="requested_email"', body)

        rejected = self.client.post(
            "/access-help",
            data={
                "member_number": self.MEMBER_ID,
                "full_name": self.MEMBER_NAME,
                "birthdate": self.MEMBER_BIRTHDATE.isoformat(),
                "requested_email": self.REQUESTED_EMAIL,
            },
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 0)
        self.assertEqual(EmailLog.query.count(), 0)

    def test_schema_default_and_open_member_index_match_safe_state_machine(self):
        portal.ensure_runtime_schema()
        now = datetime.now()
        open_key = portal.member_email_correction_open_member_key(self.MEMBER_ID)
        record = MemberEmailCorrectionRequest(
            reference="EC-TEST-DEFAULT-1",
            member_id=self.MEMBER_ID,
            claimed_name=self.MEMBER_NAME,
            requested_email=self.REQUESTED_EMAIL,
            requested_email_hash=portal.member_email_correction_email_hash(self.REQUESTED_EMAIL),
            open_member_key=open_key,
            language="en",
            confirmation_token_hash="a" * 64,
            confirmation_token_ciphertext="encrypted",
            confirmation_expires_at=now + timedelta(hours=24),
            requester_ip_hash="b" * 64,
            created_at=now,
            updated_at=now,
        )
        db.session.add(record)
        db.session.commit()

        self.assertEqual(record.status, "verification_queued")
        indexes = {
            item["name"]: item
            for item in portal.inspect(db.engine).get_indexes(
                MemberEmailCorrectionRequest.__tablename__
            )
        }
        open_index = indexes["uq_member_email_correction_open_member_key"]
        self.assertTrue(open_index["unique"])
        self.assertEqual(open_index["column_names"], ["open_member_key"])

        duplicate = MemberEmailCorrectionRequest(
            reference="EC-TEST-DEFAULT-2",
            member_id=self.MEMBER_ID,
            claimed_name=self.MEMBER_NAME,
            requested_email="second.member@example.com",
            requested_email_hash=portal.member_email_correction_email_hash("second.member@example.com"),
            open_member_key=open_key,
            language="en",
            confirmation_token_hash="c" * 64,
            confirmation_token_ciphertext="encrypted",
            confirmation_expires_at=now + timedelta(hours=24),
            requester_ip_hash="d" * 64,
            created_at=now,
            updated_at=now,
        )
        db.session.add(duplicate)
        with self.assertRaises(portal.IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_unknown_member_and_wrong_name_have_same_neutral_public_result(self):
        unknown = self.request_access(member_number="99999")
        unknown_body = unknown.get_data(as_text=True)

        self.add_member()
        wrong_name = self.request_access(full_name="Someone Else")
        wrong_name_body = wrong_name.get_data(as_text=True)

        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(wrong_name.status_code, 200)
        self.assertEqual(unknown_body, wrong_name_body)
        self.assertNotIn(self.MEMBER_ID, wrong_name_body)
        self.assertNotIn(self.OLD_EMAIL, wrong_name_body)
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 0)
        self.assertEqual(EmailLog.query.count(), 0)

    def test_access_help_uses_no_third_party_executable_and_is_not_cached(self):
        response = self.client.get("/access-help")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cdn.tailwindcss.com", body)
        self.assertNotRegex(body, r'<script[^>]+src=["\']https?://')
        self.assertIn("private", response.headers.get("Cache-Control", ""))
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("script-src 'none'", response.headers.get("Content-Security-Policy", ""))

    def test_valid_request_queues_without_synchronous_delivery_then_sends_scanner_safe_link(self):
        self.add_member()

        response = self.request_access(process_queue=False)

        self.assertEqual(response.status_code, 200)
        record = MemberEmailCorrectionRequest.query.one()
        self.assertEqual(record.status, "verification_queued")
        self.assertEqual(self.delivery_mock.call_count, 0)
        self.assertEqual(EmailLog.query.count(), 0)

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "awaiting_email_confirmation")
        verification_log = EmailLog.query.one()
        self.assertEqual(verification_log.to_addresses, self.REQUESTED_EMAIL)
        self.assertEqual(verification_log.cc_addresses, "")
        self.assertNotIn("frontdesk@dreamzfitness.com", verification_log.to_addresses)
        self.assertEqual(verification_log.body, "[Sensitive e-mail content redacted]")
        self.assertIsNone(verification_log.html_body)
        confirmation_path = self.confirmation_path(self.verification_message())
        confirmation_token = self.confirmation_token(self.verification_message())
        self.assertNotIn(confirmation_token, confirmation_path)

        status_before_get = record.status
        first_get = self.client.get(confirmation_path)
        second_get = self.client.get(confirmation_path)
        db.session.refresh(record)

        self.assertEqual(first_get.status_code, 200)
        self.assertEqual(second_get.status_code, 200)
        self.assertIn("no-store", first_get.headers["Cache-Control"])
        self.assertEqual(first_get.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("noindex", first_get.headers["X-Robots-Tag"])
        self.assertIn("default-src 'self'", first_get.headers["Content-Security-Policy"])
        self.assertNotIn("cdn.tailwindcss.com", first_get.get_data(as_text=True))
        self.assertIn('name="confirmation_token"', first_get.get_data(as_text=True))
        self.assertEqual(record.status, status_before_get)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_post_confirmation_notifies_frontdesk_with_admin_cc_and_then_waits_for_ga(self):
        self.add_member()

        record, _ = self.create_and_confirm_request()

        self.assertEqual(record.status, "waiting_for_gym_assistant_email")
        self.assertEqual(EmailLog.query.count(), 2)
        staff_log = EmailLog.query.filter_by(
            to_addresses="frontdesk@dreamzfitness.com"
        ).one()
        self.assertEqual(staff_log.cc_addresses, "ron@dreamzfitness.com")
        self.assertIn(self.MEMBER_ID, staff_log.body)
        self.assertIn(self.MEMBER_NAME, staff_log.body)
        self.assertNotIn(self.REQUESTED_EMAIL, staff_log.body)
        self.assertIn("/staff/email-corrections", staff_log.body)
        self.assertIn("uitsluitend een melding", staff_log.body)

    def test_exact_unique_ga_email_reconcile_sends_activation_once_and_completes(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        before_reconcile = EmailLog.query.count()

        member.email = self.REQUESTED_EMAIL.upper()
        db.session.commit()
        first_summary = reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "completed")
        self.assertIsNotNone(record.completed_at)
        self.assertEqual(EmailLog.query.count(), before_reconcile + 1)
        activation_log = (
            EmailLog.query.filter_by(to_addresses=self.REQUESTED_EMAIL)
            .order_by(EmailLog.id.desc())
            .first()
        )
        self.assertIsNotNone(activation_log)
        self.assertIn("portal", (activation_log.subject or "").lower())

        second_summary = reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "completed")
        self.assertEqual(EmailLog.query.count(), before_reconcile + 1)
        self.assertIsNotNone(first_summary)
        self.assertIsNotNone(second_summary)

    def test_mismatched_ga_email_remains_waiting_without_activation(self):
        self.add_member()
        record, _ = self.create_and_confirm_request()
        before_reconcile = EmailLog.query.count()

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "waiting_for_gym_assistant_email")
        self.assertIsNone(record.completed_at)
        self.assertEqual(EmailLog.query.count(), before_reconcile)

    def test_duplicate_synced_email_moves_request_to_manual_review(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        db.session.add(
            Member(
                member_id="42002",
                name="Duplicate Member",
                email=self.REQUESTED_EMAIL,
                plan_type="Contract 12 months 2024",
                is_active=True,
            )
        )
        member.email = self.REQUESTED_EMAIL.upper()
        db.session.commit()
        before_reconcile = EmailLog.query.count()

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "manual_review")
        self.assertEqual(record.last_error, "gym_assistant_email_not_unique")
        self.assertIsNone(record.completed_at)
        self.assertEqual(EmailLog.query.count(), before_reconcile)

    def test_duplicate_and_changed_requests_are_idempotently_rate_limited(self):
        self.add_member()

        first = self.request_access()
        duplicate = self.request_access()
        changed = self.request_access(requested_email="another.member@example.com")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 1)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_invalid_confirmation_token_never_changes_request_or_notifies_staff(self):
        self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        confirmation_path = self.confirmation_path(self.verification_message())
        status_before = record.status

        response = self.client.post(
            confirmation_path,
            data=self.csrf_data(confirmation_token="invalid-token"),
            follow_redirects=True,
        )
        db.session.refresh(record)

        self.assertIn(response.status_code, {400, 404})
        self.assertEqual(record.status, status_before)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_completed_email_change_revokes_old_code_and_old_code_cannot_login(self):
        member = self.add_member()
        member.password_hash = generate_password_hash("OldPortalPassword1!")
        member.password_set_at = datetime.now()
        original_auth_version = member.auth_version or 0
        old_code = "123456"
        login_code = MemberLoginCode(
            member_id=self.MEMBER_ID,
            email=self.OLD_EMAIL,
            code_hash=generate_password_hash(old_code),
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(minutes=15),
        )
        db.session.add(login_code)
        db.session.commit()
        record, _ = self.create_and_confirm_request()

        with self.client.session_transaction() as browser_session:
            browser_session["member_id"] = self.MEMBER_ID
            browser_session["member_auth_version"] = original_auth_version
            browser_session["member_password_verified"] = True

        member.email = self.REQUESTED_EMAIL
        db.session.commit()
        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        db.session.refresh(login_code)

        self.assertEqual(record.status, "completed")
        self.assertIsNotNone(login_code.used_at)
        self.assertIsNone(member.password_hash)
        self.assertIsNone(member.password_set_at)
        self.assertEqual(member.auth_version, original_auth_version + 1)
        self.assertIsNone(record.open_member_key)

        self.client.get("/")
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("member_id", browser_session)
            self.assertNotIn("member_auth_version", browser_session)
            self.assertNotIn("member_password_verified", browser_session)

        with self.client.session_transaction() as browser_session:
            browser_session["pending_login_email"] = self.OLD_EMAIL
            browser_session["_csrf_token"] = self.CSRF_TOKEN
            browser_session.pop("member_id", None)
        response = self.client.post(
            "/login",
            data={
                "step": "code",
                "code": old_code,
                "csrf_token": self.CSRF_TOKEN,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("member_id", browser_session)

        reconcile_pending_member_email_corrections()
        db.session.refresh(member)
        self.assertEqual(member.auth_version, original_auth_version + 1)

    def test_sync_auth_invalidation_is_not_repeated_by_correction_completion(self):
        member = self.add_member(password_hash=generate_password_hash("OldPortalPassword1!"))
        original_auth_version = int(member.auth_version or 0)
        record, _ = self.create_and_confirm_request()

        # This is the auth effect apply_sync_payload performs in the same
        # transaction as importing the GA e-mail change.
        portal.secure_member_auth_after_email_change(member)
        member.email = self.REQUESTED_EMAIL
        db.session.commit()
        self.assertEqual(member.auth_version, original_auth_version + 1)

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        db.session.refresh(member)

        self.assertEqual(record.status, "completed")
        self.assertIsNotNone(record.auth_secured_at)
        self.assertEqual(record.auth_version_after_secure, original_auth_version + 1)
        self.assertEqual(member.auth_version, original_auth_version + 1)

    def test_correction_completion_clears_password_written_after_sync_invalidation(self):
        member = self.add_member(password_hash=generate_password_hash("OldPortalPassword1!"))
        original_auth_version = int(member.auth_version or 0)
        record, _ = self.create_and_confirm_request()

        portal.secure_member_auth_after_email_change(member)
        member.email = self.REQUESTED_EMAIL
        db.session.commit()

        # Simulate a password request that started before the sync transaction
        # and committed its write afterwards.
        member.password_hash = generate_password_hash("LatePasswordWrite1!")
        member.password_set_at = datetime.now()
        late_code = MemberLoginCode(
            member_id=self.MEMBER_ID,
            email=self.REQUESTED_EMAIL,
            code_hash=generate_password_hash("654321"),
            created_at=datetime.now(),
            expires_at=datetime.now() + timedelta(minutes=15),
        )
        db.session.add(late_code)
        db.session.commit()

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        db.session.refresh(member)
        db.session.refresh(late_code)

        self.assertEqual(record.status, "completed")
        self.assertIsNone(member.password_hash)
        self.assertIsNone(member.password_set_at)
        self.assertIsNotNone(late_code.used_at)
        self.assertEqual(member.auth_version, original_auth_version + 1)
        self.assertEqual(record.auth_version_after_secure, original_auth_version + 1)

    def test_inflight_set_password_cannot_commit_after_auth_version_changes(self):
        member = self.add_member()
        original_auth_version = int(member.auth_version or 0)
        with self.client.session_transaction() as browser_session:
            browser_session["member_id"] = self.MEMBER_ID
            browser_session["member_auth_version"] = original_auth_version
            browser_session["member_password_verified"] = True

        def advance_auth_version(*_args, **_kwargs):
            current = Member.query.filter_by(member_id=self.MEMBER_ID).one()
            current.auth_version = original_auth_version + 1
            db.session.commit()
            return None

        with mock.patch.object(
            portal,
            "validate_member_password",
            side_effect=advance_auth_version,
        ):
            response = self.client.post(
                "/set-password",
                data=self.csrf_data(
                    password="LatePasswordWrite1!",
                    password_confirm="LatePasswordWrite1!",
                ),
                follow_redirects=False,
            )

        db.session.refresh(member)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))
        self.assertEqual(member.auth_version, original_auth_version + 1)
        self.assertIsNone(member.password_hash)
        self.assertIsNone(member.password_set_at)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("member_id", browser_session)
            self.assertNotIn("member_auth_version", browser_session)
            self.assertNotIn("member_password_verified", browser_session)

    def test_email_change_during_activation_never_completes_and_recipient_stays_bound(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        db.session.commit()

        def deliver_then_change_member(*args, **kwargs):
            result = self.deliver_as_sent(*args, **kwargs)
            changed_member = Member.query.filter_by(member_id=self.MEMBER_ID).one()
            changed_member.email = "changed.again@example.com"
            db.session.commit()
            return result

        with mock.patch.object(portal, "deliver_email", side_effect=deliver_then_change_member):
            reconcile_pending_member_email_corrections()

        db.session.refresh(record)
        self.assertEqual(record.status, "manual_review")
        self.assertEqual(
            record.last_error,
            "gym_assistant_email_changed_or_not_unique_during_completion",
        )
        self.assertIsNone(record.completed_at)
        self.assertIsNotNone(record.open_member_key)
        activation_message = self.sent_messages[-1]
        self.assertEqual(activation_message["to_addresses"], [self.REQUESTED_EMAIL])

    def test_wrong_or_missing_birthdate_returns_the_same_neutral_result(self):
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_IP_HOUR"] = 10
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_MEMBER_DAY"] = 10
        self.add_member()

        wrong = self.request_access(
            birthdate="1990-04-22",
            remote_addr="198.51.100.21",
        )
        missing = self.request_access(
            birthdate="",
            remote_addr="198.51.100.22",
        )
        unknown = self.request_access(
            member_number="99999",
            remote_addr="198.51.100.23",
        )

        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(wrong.get_data(), missing.get_data())
        self.assertEqual(wrong.get_data(), unknown.get_data())
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 0)
        self.assertEqual(EmailLog.query.count(), 0)
        self.assertEqual(MemberEmailCorrectionAttempt.query.count(), 3)

    def test_invalid_probe_consumes_ip_rate_limit_before_member_lookup(self):
        self.add_member()

        first = self.request_access(
            member_number="99999",
            remote_addr="198.51.100.30",
        )
        second = self.request_access(remote_addr="198.51.100.30")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(), second.get_data())
        self.assertEqual(MemberEmailCorrectionAttempt.query.count(), 1)
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 0)
        self.assertEqual(EmailLog.query.count(), 0)

    def test_invalid_identity_proofs_do_not_consume_verified_member_limit(self):
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_IP_HOUR"] = 10
        app.config["MEMBER_EMAIL_CORRECTION_MAX_PER_MEMBER_DAY"] = 1
        self.add_member()

        for index in range(3):
            response = self.request_access(
                birthdate="1990-04-22",
                remote_addr=f"198.51.100.{40 + index}",
            )
            self.assertEqual(response.status_code, 200)

        accepted = self.request_access(remote_addr="198.51.100.50")

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(MemberEmailCorrectionRequest.query.count(), 1)
        self.assertEqual(
            MemberEmailCorrectionAttempt.query.filter_by(proof_verified=True).count(),
            1,
        )
        self.assertEqual(
            MemberEmailCorrectionAttempt.query.filter_by(proof_verified=False).count(),
            3,
        )
        self.assertEqual(EmailLog.query.count(), 1)

    def test_forwarded_for_is_ignored_without_explicit_hops_and_uses_rightmost_with_one_hop(self):
        spoofed_headers = {"X-Forwarded-For": "192.0.2.9, 198.51.100.77"}
        app.config["MEMBER_EMAIL_CORRECTION_TRUSTED_PROXY_HOPS"] = 0
        self.request_access(
            member_number="99999",
            remote_addr="203.0.113.8",
            headers=spoofed_headers,
            process_queue=False,
        )
        first = MemberEmailCorrectionAttempt.query.one()
        self.assertEqual(
            first.requester_ip_hash,
            portal.member_email_correction_ip_hash("203.0.113.8"),
        )

        MemberEmailCorrectionAttempt.query.delete()
        db.session.commit()
        app.config["MEMBER_EMAIL_CORRECTION_TRUSTED_PROXY_HOPS"] = 1
        self.request_access(
            member_number="99999",
            remote_addr="203.0.113.8",
            headers=spoofed_headers,
            process_queue=False,
        )
        second = MemberEmailCorrectionAttempt.query.one()
        self.assertEqual(
            second.requester_ip_hash,
            portal.member_email_correction_ip_hash("198.51.100.77"),
        )

    def test_railway_runtime_uses_valid_edge_real_ip_for_rate_limit(self):
        app.config["MEMBER_EMAIL_CORRECTION_TRUST_RAILWAY_X_REAL_IP"] = True
        self.request_access(
            member_number="99999",
            remote_addr="203.0.113.8",
            headers={
                "X-Real-IP": "198.51.100.88",
                "X-Forwarded-For": "192.0.2.9",
            },
            process_queue=False,
        )

        attempt = MemberEmailCorrectionAttempt.query.one()
        self.assertEqual(
            attempt.requester_ip_hash,
            portal.member_email_correction_ip_hash("198.51.100.88"),
        )

    def test_logged_verification_delivery_fails_closed_and_retains_open_key(self):
        self.add_member()

        with mock.patch.object(portal, "deliver_email", return_value="logged") as delivery:
            response = self.request_access()

        self.assertEqual(response.status_code, 200)
        record = MemberEmailCorrectionRequest.query.one()
        self.assertEqual(record.status, "manual_review")
        self.assertIsNotNone(record.open_member_key)
        self.assertEqual(
            record.last_error,
            "verification_email_delivery_failed_or_uncertain",
        )
        self.assertEqual(delivery.call_count, 1)

    def test_logged_staff_delivery_fails_closed_without_waiting(self):
        self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        verification_message = self.verification_message()
        confirmation_path = self.confirmation_path(verification_message)
        confirmation_token = self.confirmation_token(verification_message)

        with mock.patch.object(portal, "deliver_email", return_value="logged") as delivery:
            response = self.client.post(
                confirmation_path,
                data=self.csrf_data(confirmation_token=confirmation_token),
                follow_redirects=True,
            )

        db.session.refresh(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(record.status, "manual_review")
        self.assertIsNone(record.staff_notified_at)
        self.assertIsNotNone(record.open_member_key)
        self.assertEqual(delivery.call_count, 1)

    def test_logged_activation_delivery_fails_closed_without_completion(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        db.session.commit()

        with mock.patch.object(portal, "deliver_email", return_value="logged") as delivery:
            reconcile_pending_member_email_corrections()

        db.session.refresh(record)
        self.assertEqual(record.status, "manual_review")
        self.assertIsNone(record.completed_at)
        self.assertIsNone(record.member_notified_at)
        self.assertIsNotNone(record.open_member_key)
        self.assertEqual(delivery.call_count, 1)

    def test_preexisting_duplicate_email_never_sends_actionable_staff_mail(self):
        self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        verification_message = self.verification_message()
        confirmation_path = self.confirmation_path(verification_message)
        confirmation_token = self.confirmation_token(verification_message)
        db.session.add(Member(
            member_id="42002",
            name="Other Member",
            email=self.REQUESTED_EMAIL,
            birthdate=date(1984, 2, 10),
            is_active=True,
        ))
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        response = self.client.post(
            confirmation_path,
            data=self.csrf_data(confirmation_token=confirmation_token),
            follow_redirects=True,
        )

        db.session.refresh(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(record.status, "manual_review")
        self.assertEqual(record.last_error, "requested_email_already_in_use")
        self.assertIsNone(record.staff_notified_at)
        self.assertIsNone(record.staff_email_log_id)
        self.assertIsNotNone(record.open_member_key)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_waiting_request_expires_and_never_reactivates(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        record.email_confirmed_at = datetime.now() - timedelta(hours=169)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "expired")
        self.assertIsNone(record.open_member_key)

        member.email = self.REQUESTED_EMAIL
        db.session.commit()
        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "expired")
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

    def test_stale_sending_without_log_retries_once_and_completes(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        record.status = "sending_member_notification"
        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "completed")
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count + 1)

        reconcile_pending_member_email_corrections()
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count + 1)

    def test_sent_activation_log_recovers_without_resending(self):
        member = self.add_member(password_hash=generate_password_hash("OldPassword1!"))
        original_auth_version = int(member.auth_version or 0)
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        record.status = "sending_member_notification"
        record.updated_at = datetime.now() - timedelta(minutes=16)
        subject, body, html_body = portal.member_email_correction_activation_message(record, member)
        sent_log = EmailLog(
            delivery_mode="smtp",
            status="sent",
            to_addresses=self.REQUESTED_EMAIL,
            cc_addresses="",
            bcc_addresses="",
            subject=subject,
            body=body,
            html_body=html_body,
        )
        db.session.add(sent_log)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        db.session.refresh(member)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.member_email_log_id, sent_log.id)
        self.assertIsNone(member.password_hash)
        self.assertEqual(member.auth_version, original_auth_version + 1)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

        reconcile_pending_member_email_corrections()
        db.session.refresh(member)
        self.assertEqual(member.auth_version, original_auth_version + 1)

    def test_fresh_pending_delivery_waits_but_stale_pending_fails_closed(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        record.status = "sending_member_notification"
        record.updated_at = datetime.now()
        subject, body, html_body = portal.member_email_correction_activation_message(record, member)
        db.session.add(EmailLog(
            delivery_mode="smtp",
            status="pending",
            to_addresses=self.REQUESTED_EMAIL,
            cc_addresses="",
            bcc_addresses="",
            subject=subject,
            body=body,
            html_body=html_body,
        ))
        db.session.commit()

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "sending_member_notification")

        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "manual_review")
        self.assertIsNotNone(record.open_member_key)

    def test_sent_verification_log_recovers_to_awaiting_without_resending(self):
        self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        verification_log = EmailLog.query.one()
        record.status = "sending_verification"
        record.verification_email_log_id = None
        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "awaiting_email_confirmation")
        self.assertEqual(record.verification_email_log_id, verification_log.id)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

    def test_sent_verification_log_past_token_deadline_expires_immediately(self):
        self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        verification_log = EmailLog.query.one()
        record.status = "sending_verification"
        record.verification_email_log_id = None
        record.confirmation_expires_at = datetime.now() - timedelta(seconds=1)
        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "expired")
        self.assertEqual(record.verification_email_log_id, verification_log.id)
        self.assertIsNone(record.open_member_key)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

    def test_stale_verification_without_log_retries_safely_and_sends_once(self):
        member = self.add_member()
        record, token = portal.create_member_email_correction_request(
            member,
            self.MEMBER_NAME,
            self.REQUESTED_EMAIL,
            "en",
            "198.51.100.44",
        )
        record.status = "sending_verification"
        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        confirmation_path = f"/access-help/confirm/{record.reference}"
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "awaiting_email_confirmation")
        self.assertIsNotNone(record.open_member_key)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count + 1)

        confirmed = self.client.post(
            confirmation_path,
            data=self.csrf_data(confirmation_token=token),
        )
        self.assertEqual(confirmed.status_code, 200)

    def test_fresh_pending_verification_waits_then_stale_becomes_manual_review(self):
        member = self.add_member()
        record, token = portal.create_member_email_correction_request(
            member,
            self.MEMBER_NAME,
            self.REQUESTED_EMAIL,
            "en",
            "198.51.100.45",
        )
        subject = portal.member_email_correction_verification_subject(record)
        db.session.add(EmailLog(
            delivery_mode="smtp",
            status="pending",
            to_addresses=self.REQUESTED_EMAIL,
            cc_addresses="",
            bcc_addresses="",
            subject=subject,
            body="[Sensitive e-mail content redacted]",
            html_body=None,
        ))
        record.status = "sending_verification"
        db.session.commit()

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "sending_verification")

        record.updated_at = datetime.now() - timedelta(minutes=16)
        db.session.commit()
        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "manual_review")

    def test_sent_staff_log_recovers_without_duplicate_action_email(self):
        member = self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        record.status = "sending_staff_notification"
        record.email_confirmed_at = datetime.now()
        record.updated_at = datetime.now() - timedelta(minutes=16)
        subject, body, html_body = portal.build_staff_member_email_correction_email(record, member)
        staff_log = EmailLog(
            delivery_mode="smtp",
            status="sent",
            to_addresses="frontdesk@dreamzfitness.com",
            cc_addresses="ron@dreamzfitness.com",
            bcc_addresses="",
            subject=subject,
            body=body,
            html_body=html_body,
        )
        db.session.add(staff_log)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "waiting_for_gym_assistant_email")
        self.assertEqual(record.staff_email_log_id, staff_log.id)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

    def test_sent_staff_log_past_queue_deadline_expires_without_resending(self):
        member = self.add_member()
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        record.status = "sending_staff_notification"
        record.email_confirmed_at = datetime.now() - timedelta(hours=169)
        record.updated_at = datetime.now() - timedelta(minutes=16)
        subject, body, html_body = portal.build_staff_member_email_correction_email(record, member)
        staff_log = EmailLog(
            delivery_mode="smtp",
            status="sent",
            to_addresses="frontdesk@dreamzfitness.com",
            cc_addresses="ron@dreamzfitness.com",
            bcc_addresses="",
            subject=subject,
            body=body,
            html_body=html_body,
        )
        db.session.add(staff_log)
        db.session.commit()
        before_delivery_count = self.delivery_mock.call_count

        reconcile_pending_member_email_corrections()
        db.session.refresh(record)

        self.assertEqual(record.status, "expired")
        self.assertEqual(record.staff_email_log_id, staff_log.id)
        self.assertIsNone(record.open_member_key)
        self.assertEqual(self.delivery_mock.call_count, before_delivery_count)

    def test_admin_can_close_durable_manual_review_queue_item(self):
        self.add_member()
        with mock.patch.object(portal, "deliver_email", return_value="logged"):
            self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        self.assertEqual(record.status, "manual_review")
        self.assertIsNotNone(record.open_member_key)
        with self.client.session_transaction() as browser_session:
            browser_session["staff_username"] = "ron"
            browser_session["staff_role"] = "admin"

        queue_page = self.client.get("/staff/email-corrections")
        self.assertEqual(queue_page.status_code, 200)
        self.assertIn(record.reference, queue_page.get_data(as_text=True))
        response = self.client.post(
            f"/staff/email-corrections/{record.id}/close",
            data=self.csrf_data(review_note="Conflict checked; member may resubmit."),
            follow_redirects=True,
        )

        db.session.refresh(record)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(record.status, "review_closed")
        self.assertIsNone(record.open_member_key)
        self.assertEqual(record.reviewed_by, "ron")
        self.assertIsNotNone(record.reviewed_at)
        self.assertIn("member may resubmit", record.review_note)

        reviewed_at = record.reviewed_at
        reviewed_by = record.reviewed_by
        review_note = record.review_note
        second = self.client.post(
            f"/staff/email-corrections/{record.id}/close",
            data=self.csrf_data(review_note="Must not overwrite the first review."),
        )
        db.session.refresh(record)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(record.reviewed_at, reviewed_at)
        self.assertEqual(record.reviewed_by, reviewed_by)
        self.assertEqual(record.review_note, review_note)

    def test_manager_can_read_active_protected_queue_but_cannot_close_records(self):
        self.add_member()
        record, _ = self.create_and_confirm_request()
        with self.client.session_transaction() as browser_session:
            browser_session["staff_username"] = "frontdesk"
            browser_session["staff_role"] = "manager"

        queue_page = self.client.get("/staff/email-corrections")

        self.assertEqual(queue_page.status_code, 200)
        self.assertIn("private", queue_page.headers.get("Cache-Control", ""))
        self.assertIn("no-store", queue_page.headers.get("Cache-Control", ""))
        self.assertEqual(queue_page.headers.get("Pragma"), "no-cache")
        self.assertEqual(queue_page.headers.get("Expires"), "0")
        queue_body = queue_page.get_data(as_text=True)
        self.assertIn(record.reference, queue_body)
        self.assertIn(self.REQUESTED_EMAIL, queue_body)
        expected_deadline = (
            portal.local_datetime(
                record.email_confirmed_at + timedelta(hours=168)
            )
        ).strftime("%d/%m/%Y %H:%M")
        self.assertIn(expected_deadline, queue_body)

        blocked = self.client.post(
            f"/staff/email-corrections/{record.id}/close",
            data=self.csrf_data(review_note="Must remain read-only."),
        )
        self.assertEqual(blocked.status_code, 403)
        db.session.refresh(record)
        self.assertEqual(record.status, "waiting_for_gym_assistant_email")

    def test_staff_token_cannot_read_the_session_only_correction_queue(self):
        app.config["STAFF_TOKEN"] = "legacy-staff-token"

        response = self.client.get(
            "/staff/email-corrections?token=legacy-staff-token",
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/staff/login", response.headers["Location"])
        self.assertIn("next=", response.headers["Location"])

    def test_manager_login_returns_to_queue_and_navigation_links_to_it(self):
        db.session.add(portal.StaffUser(
            username="queue-manager",
            role="manager",
            email="queue-manager@example.com",
            password_hash=generate_password_hash("QueuePassword1!"),
            is_active=True,
        ))
        db.session.commit()

        response = self.client.post(
            "/staff/login?next=/staff/email-corrections",
            data=self.csrf_data(
                username="queue-manager",
                password="QueuePassword1!",
                next="/staff/email-corrections",
            ),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/staff/email-corrections"))

        queue_page = self.client.get(response.headers["Location"])
        self.assertEqual(queue_page.status_code, 200)
        self.assertIn('href="/staff/email-corrections"', queue_page.get_data(as_text=True))

    def test_queue_shows_more_than_one_hundred_open_items(self):
        now = datetime.now()
        records = []
        for index in range(105):
            member_id = f"Q{index:04d}"
            records.append(MemberEmailCorrectionRequest(
                reference=f"EC-QUEUE-{index:04d}",
                member_id=member_id,
                claimed_name=f"Queue Member {index}",
                requested_email=f"queue{index}@example.com",
                requested_email_hash=portal.member_email_correction_email_hash(
                    f"queue{index}@example.com"
                ),
                open_member_key=portal.member_email_correction_open_member_key(member_id),
                language="en",
                status="manual_review",
                confirmation_token_hash="a" * 64,
                requester_ip_hash="b" * 64,
                confirmation_expires_at=now + timedelta(hours=1),
                email_confirmed_at=now,
                last_error="test_manual_review",
                created_at=now,
                updated_at=now,
            ))
        db.session.add_all(records)
        db.session.commit()
        with self.client.session_transaction() as browser_session:
            browser_session["staff_username"] = "frontdesk"
            browser_session["staff_role"] = "manager"

        response = self.client.get("/staff/email-corrections")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("EC-QUEUE-0000", body)
        self.assertIn("EC-QUEUE-0104", body)
        self.assertIn(">105<", body)

    def test_unconfirmed_manual_review_never_labels_input_as_confirmed(self):
        member = self.add_member()
        record, _ = portal.create_member_email_correction_request(
            member,
            self.MEMBER_NAME,
            self.REQUESTED_EMAIL,
            "en",
            "198.51.100.61",
        )
        record.status = "manual_review"
        record.last_error = "verification_email_delivery_failed_or_uncertain"
        record.confirmation_token_ciphertext = None
        db.session.commit()
        with self.client.session_transaction() as browser_session:
            browser_session["staff_username"] = "frontdesk"
            browser_session["staff_role"] = "manager"

        response = self.client.get("/staff/email-corrections")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(record.reference, body)
        self.assertNotIn(self.REQUESTED_EMAIL, body)
        self.assertNotIn(self.MEMBER_NAME, body)

    def test_terminal_state_cannot_be_overwritten_by_stale_failure_or_expiry(self):
        member = self.add_member()
        record, _ = self.create_and_confirm_request()
        member.email = self.REQUESTED_EMAIL
        db.session.commit()
        reconcile_pending_member_email_corrections()
        db.session.refresh(record)
        self.assertEqual(record.status, "completed")
        completed_at = record.completed_at
        auth_version = member.auth_version

        portal.mark_member_email_correction_for_review(record, "stale_worker_failure")
        portal.expire_member_email_correction(record, reason="stale_worker_expiry")
        portal.recover_sending_member_email_correction(record)

        db.session.refresh(record)
        db.session.refresh(member)
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.completed_at, completed_at)
        self.assertIsNone(record.open_member_key)
        self.assertEqual(member.auth_version, auth_version)

    def test_stale_finish_cannot_change_expired_or_review_closed_requests(self):
        member = self.add_member(email=self.REQUESTED_EMAIL)
        original_auth_version = int(member.auth_version or 0)
        sent_log = EmailLog(
            delivery_mode="smtp",
            status="sent",
            to_addresses=self.REQUESTED_EMAIL,
            cc_addresses="",
            bcc_addresses="",
            subject="Terminal state test",
            body="sent",
        )
        db.session.add(sent_log)
        db.session.flush()
        now = datetime.now()
        records = []
        for index, status in enumerate(("expired", "review_closed"), start=1):
            record = MemberEmailCorrectionRequest(
                reference=f"EC-TERMINAL-{index}",
                member_id=self.MEMBER_ID,
                claimed_name=self.MEMBER_NAME,
                requested_email=self.REQUESTED_EMAIL,
                requested_email_hash=portal.member_email_correction_email_hash(
                    self.REQUESTED_EMAIL
                ),
                open_member_key=None,
                language="en",
                status=status,
                confirmation_token_hash="c" * 64,
                requester_ip_hash="d" * 64,
                confirmation_expires_at=now + timedelta(hours=1),
                email_confirmed_at=now,
                created_at=now,
                updated_at=now,
            )
            db.session.add(record)
            records.append(record)
        db.session.commit()

        for record in records:
            with self.subTest(status=record.status):
                original_status = record.status
                portal.finish_member_email_correction(record, member, sent_log)
                db.session.refresh(record)
                self.assertEqual(record.status, original_status)
                self.assertIsNone(record.completed_at)

        db.session.refresh(member)
        self.assertEqual(member.auth_version, original_auth_version)

    def test_startup_schema_migration_adds_auth_version_before_traffic(self):
        db.drop_all()
        db.session.execute(portal.text(
            "CREATE TABLE member (id INTEGER PRIMARY KEY, member_id VARCHAR UNIQUE NOT NULL, "
            "name VARCHAR, email VARCHAR)"
        ))
        db.session.commit()
        app.config["_RUNTIME_SCHEMA_READY"] = False

        portal.ensure_runtime_schema_before_traffic()

        member_columns = {
            column["name"] for column in portal.inspect(db.engine).get_columns("member")
        }
        correction_indexes = {
            index["name"]: index
            for index in portal.inspect(db.engine).get_indexes(
                MemberEmailCorrectionRequest.__tablename__
            )
        }
        self.assertIn("auth_version", member_columns)
        self.assertTrue(
            correction_indexes["uq_member_email_correction_open_member_key"]["unique"]
        )

    def test_production_scheduler_never_runs_smtp_reconciliation_inline(self):
        app.config["TESTING"] = False
        with (
            mock.patch.object(portal.threading, "Thread") as thread_class,
            mock.patch.object(portal, "post_sync_reconciliation_summaries") as reconcile,
        ):
            result = portal.schedule_post_sync_reconciliation()

        try:
            self.assertEqual(result["portal_invitations"]["status"], "scheduled")
            self.assertEqual(
                result["member_email_corrections"]["status"],
                "scheduled",
            )
            thread_class.assert_called_once()
            thread_class.return_value.start.assert_called_once()
            reconcile.assert_not_called()
        finally:
            if portal._POST_SYNC_RECONCILIATION_THREAD_LOCK.locked():
                portal._POST_SYNC_RECONCILIATION_THREAD_LOCK.release()

    def test_queue_get_expires_stale_active_request_without_waiting_for_sync(self):
        self.add_member()
        record, _ = self.create_and_confirm_request()
        record.email_confirmed_at = datetime.now() - timedelta(hours=169)
        db.session.commit()
        with self.client.session_transaction() as browser_session:
            browser_session["staff_username"] = "frontdesk"
            browser_session["staff_role"] = "manager"

        queue_page = self.client.get("/staff/email-corrections")
        db.session.refresh(record)

        self.assertEqual(queue_page.status_code, 200)
        self.assertEqual(record.status, "expired")
        self.assertIsNone(record.open_member_key)

    def test_request_language_survives_a_fresh_confirmation_session(self):
        self.add_member()
        self.client.get("/language?lang=pap&next=/access-help")
        self.request_access()
        record = MemberEmailCorrectionRequest.query.one()
        verification_message = self.verification_message()
        confirmation_path = self.confirmation_path(verification_message)
        confirmation_token = self.confirmation_token(verification_message)
        self.assertEqual(record.language, "pap")

        fresh_client = app.test_client()
        page = fresh_client.get(confirmation_path)
        self.assertEqual(page.status_code, 200)
        self.assertIn('<html lang="pap">', page.get_data(as_text=True))
        self.assertIn("Konfirmá bo adrès di e-mail", page.get_data(as_text=True))
        confirmed = fresh_client.post(
            confirmation_path,
            data=self.csrf_data(
                client=fresh_client,
                confirmation_token=confirmation_token,
            ),
            follow_redirects=True,
        )
        self.assertEqual(confirmed.status_code, 200)

        member = Member.query.filter_by(member_id=self.MEMBER_ID).one()
        member.email = self.REQUESTED_EMAIL
        db.session.commit()
        reconcile_pending_member_email_corrections()
        activation_message = self.sent_messages[-1]
        self.assertTrue(activation_message["subject"].startswith("Bo portal di miembro"))


if __name__ == "__main__":
    unittest.main()
