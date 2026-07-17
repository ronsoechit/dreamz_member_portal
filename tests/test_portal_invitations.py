import importlib.util
import os
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import (  # noqa: E402
    EmailLog,
    Member,
    PortalInvitation,
    app,
    build_portal_activation_email,
    db,
)


class PortalInvitationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["EMAIL_DELIVERY_MODE"] = "log"
        app.config["MEMBER_PORTAL_PUBLIC_URL"] = "https://dreamzfitness.app"
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = "signup-portal-test-token-0123456789"
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
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = None
        app.config["SYNC_API_TOKEN"] = None
        app.config["_RUNTIME_SCHEMA_READY"] = False

    @staticmethod
    def invitation_headers(token="signup-portal-test-token-0123456789"):
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def invitation_payload(**overrides):
        payload = {
            "reference": "DF-20260716-1234",
            "member_number": "42001",
            "expected_email": "new.member@example.com",
            "language": "nl",
            "signup_plan": "month",
        }
        payload.update(overrides)
        return payload

    def post_invitation(self, **overrides):
        return self.client.post(
            "/api/integrations/signup/portal-invitations",
            json=self.invitation_payload(**overrides),
            headers=self.invitation_headers(),
        )

    def test_integration_requires_dedicated_token(self):
        response = self.client.post(
            "/api/integrations/signup/portal-invitations",
            json=self.invitation_payload(),
        )
        self.assertEqual(response.status_code, 403)

    def test_invitation_waits_for_exact_synced_member_then_sends_once(self):
        waiting = self.post_invitation()
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json["status"], "waiting_for_member")
        self.assertEqual(EmailLog.query.count(), 0)

        sync_response = self.client.post(
            "/api/sync/members",
            headers={"X-Sync-Token": "sync-test-token"},
            json={
                "source": "portal-invitation-test",
                "members": [{
                    "member_id": "42001",
                    "name": "Member, New",
                    "email": "new.member@example.com",
                    "plan_type": "no contract 1 month Dreamz",
                }],
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(sync_response.json["portal_invitations"]["sent"], 1)
        record = PortalInvitation.query.one()
        self.assertEqual(record.status, "sent")
        self.assertIsNotNone(record.ready_at)
        self.assertIsNotNone(record.sent_at)
        self.assertEqual(record.attempts, 1)
        self.assertEqual(EmailLog.query.count(), 1)

        duplicate = self.post_invitation()
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertEqual(duplicate.json["status"], "sent")
        self.assertEqual(EmailLog.query.count(), 1)

    def test_same_reference_with_changed_payload_is_a_conflict(self):
        self.post_invitation()
        conflict = self.post_invitation(member_number="42002")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(PortalInvitation.query.count(), 1)

    def test_ineligible_signup_plan_is_rejected(self):
        response = self.post_invitation(signup_plan="day")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(PortalInvitation.query.count(), 0)

    def test_gym_assistant_email_mismatch_requires_manual_review(self):
        db.session.add(Member(member_id="42001", email="different@example.com", plan_type="Contract 6 months 2024"))
        db.session.commit()
        response = self.post_invitation()
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_duplicate_gym_assistant_email_fails_closed(self):
        db.session.add_all([
            Member(member_id="42001", email="new.member@example.com", plan_type="Under 18 Dreamz"),
            Member(member_id="42002", email="NEW.MEMBER@example.com", plan_type="Under 18 Dreamz"),
        ])
        db.session.commit()
        response = self.post_invitation(signup_plan="under18")
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_excluded_gym_assistant_plan_fails_closed(self):
        db.session.add(Member(member_id="42001", email="new.member@example.com", plan_type="Week Pass Dreamz"))
        db.session.commit()
        response = self.post_invitation()
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_email_failure_is_not_automatically_retried(self):
        db.session.add(Member(member_id="42001", email="new.member@example.com", plan_type="Contract 12 months 2024"))
        db.session.commit()
        with patch("dreamz_portal.deliver_email", side_effect=RuntimeError("smtp unavailable")) as delivery:
            failed = self.post_invitation(signup_plan="twelve")
            duplicate = self.post_invitation(signup_plan="twelve")
        self.assertEqual(failed.json["status"], "manual_review")
        self.assertEqual(duplicate.json["status"], "manual_review")
        self.assertEqual(delivery.call_count, 1)

    def test_activation_email_is_localized_and_uses_fixed_login_url(self):
        member = Member(member_id="42001", name="Member, New", email="new.member@example.com")
        subjects = []
        for language in ("en", "nl", "pap", "es"):
            subject, body, html_body = build_portal_activation_email(member, language)
            subjects.append(subject)
            self.assertIn("https://dreamzfitness.app/login", body)
            self.assertIn("https://dreamzfitness.app/login", html_body)
            self.assertNotIn("new.member@example.com", html_body)
        self.assertEqual(len(set(subjects)), 4)


if __name__ == "__main__":
    unittest.main()
