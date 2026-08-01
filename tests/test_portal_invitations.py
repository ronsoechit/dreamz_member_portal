import importlib.util
import hashlib
import json
import os
import unittest
from unittest.mock import patch

if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from sqlalchemy import inspect, text  # noqa: E402

from dreamz_portal import (  # noqa: E402
    EmailLog,
    Member,
    PortalInvitation,
    app,
    build_portal_activation_email,
    db,
    ensure_runtime_schema,
    portal_invitation_member_plan_matches,
    portal_member_signup_plan,
)


class PortalInvitationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["EMAIL_DELIVERY_MODE"] = "log"
        app.config["MEMBER_PORTAL_PUBLIC_URL"] = "https://dreamzfitness.app"
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = "signup-portal-test-token-0123456789"
        app.config["SYNC_API_TOKEN"] = "sync-test-token"
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED"] = False
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = set()
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = set()
        app.config["_RUNTIME_SCHEMA_READY"] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        self.smtp_ssl_patcher = patch("dreamz_portal.smtplib.SMTP_SSL")
        self.smtp_ssl = self.smtp_ssl_patcher.start()

    def tearDown(self):
        self.smtp_ssl_patcher.stop()
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = None
        app.config["SYNC_API_TOKEN"] = None
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED"] = False
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = set()
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = set()
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

    @staticmethod
    def existing_member_payload(**overrides):
        canonical = {
            "request_type": "existing_member_reverification",
            "reference": "DF-20260726-900001",
            "member_number": "42001",
            "expected_email": "existing.member@example.com",
            "language": "nl",
            "signup_plan": "six",
        }
        canonical.update(overrides)
        idempotency_key = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {**canonical, "idempotency_key": idempotency_key}

    def post_existing_member(self, payload=None, authorization=True, idempotency_key=None):
        payload = payload or self.existing_member_payload()
        headers = self.invitation_headers() if authorization else {}
        headers["Idempotency-Key"] = (
            payload.get("idempotency_key")
            if idempotency_key is None
            else idempotency_key
        )
        return self.client.post(
            "/api/integrations/signup/portal-invitations",
            json=payload,
            headers=headers,
        )

    def enable_existing_member_pilot(
        self,
        member_id="42001",
        email="existing.member@example.com",
    ):
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED"] = True
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = {member_id}
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = {email}
        app.config["EMAIL_DELIVERY_MODE"] = "smtp"

    @staticmethod
    def add_existing_member(
        member_id="42001",
        email="existing.member@example.com",
        plan_type="Contract 6 months 2024",
        name="Member, Existing",
    ):
        member = Member(
            member_id=member_id,
            email=email,
            plan_type=plan_type,
            name=name,
        )
        db.session.add(member)
        db.session.commit()
        return member

    def test_integration_requires_dedicated_token(self):
        response = self.client.post(
            "/api/integrations/signup/portal-invitations",
            json=self.invitation_payload(),
        )
        self.assertEqual(response.status_code, 403)

    def test_existing_member_integration_requires_dedicated_token(self):
        response = self.post_existing_member(authorization=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PortalInvitation.query.count(), 0)

    def test_existing_member_flow_is_disabled_by_default_and_echo_is_bound(self):
        self.add_existing_member()
        payload = self.existing_member_payload()
        response = self.post_existing_member(payload=payload)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["request_type"], "existing_member_reverification")
        self.assertEqual(response.json["member_number"], payload["member_number"])
        self.assertEqual(response.json["idempotency_key"], payload["idempotency_key"])
        self.assertEqual(response.json["status"], "waiting_for_feature_enablement")
        self.assertNotIn("expected_email", response.json)
        self.assertEqual(EmailLog.query.count(), 0)
        record = PortalInvitation.query.one()
        self.assertEqual(record.request_type, "existing_member_reverification")

    def test_existing_member_pilot_requires_exactly_one_member_and_email(self):
        self.add_existing_member()
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED"] = True
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = {"42001"}
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = set()

        missing_email = self.post_existing_member()
        self.assertEqual(missing_email.status_code, 202)
        self.assertEqual(missing_email.json["status"], "waiting_for_pilot_allowlist")
        self.assertEqual(EmailLog.query.count(), 0)

        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = {
            "existing.member@example.com",
            "second.member@example.com",
        }
        multiple_emails = self.client.get(
            "/api/integrations/signup/portal-invitations/DF-20260726-900001",
            headers=self.invitation_headers(),
        )
        self.assertEqual(multiple_emails.status_code, 200)
        self.assertEqual(multiple_emails.json["status"], "waiting_for_pilot_allowlist")
        self.assertEqual(EmailLog.query.count(), 0)

        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS"] = {
            "EXISTING.MEMBER@example.com"
        }
        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = {
            "42001",
            "42002",
        }
        multiple_members = self.client.get(
            "/api/integrations/signup/portal-invitations/DF-20260726-900001",
            headers=self.invitation_headers(),
        )
        self.assertEqual(multiple_members.status_code, 200)
        self.assertEqual(multiple_members.json["status"], "waiting_for_pilot_allowlist")
        self.assertEqual(EmailLog.query.count(), 0)

        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = [
            "42001",
            "42001",
        ]
        duplicate_member_values = self.client.get(
            "/api/integrations/signup/portal-invitations/DF-20260726-900001",
            headers=self.invitation_headers(),
        )
        self.assertEqual(duplicate_member_values.status_code, 200)
        self.assertEqual(
            duplicate_member_values.json["status"],
            "waiting_for_pilot_allowlist",
        )
        self.assertEqual(EmailLog.query.count(), 0)

        app.config["PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS"] = {"42001"}
        app.config["EMAIL_DELIVERY_MODE"] = "smtp"
        released = self.client.get(
            "/api/integrations/signup/portal-invitations/DF-20260726-900001",
            headers=self.invitation_headers(),
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.json["status"], "sent")
        self.assertEqual(EmailLog.query.count(), 1)

    def test_existing_member_log_delivery_never_becomes_sent(self):
        self.enable_existing_member_pilot()
        app.config["EMAIL_DELIVERY_MODE"] = "log"
        self.add_existing_member()

        response = self.post_existing_member()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "waiting_for_smtp_configuration")
        record = PortalInvitation.query.one()
        self.assertEqual(record.status, "waiting_for_smtp_configuration")
        self.assertEqual(record.attempts, 0)
        self.assertIsNone(record.sent_at)
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_smtp_mode_must_be_exact(self):
        self.enable_existing_member_pilot()
        app.config["EMAIL_DELIVERY_MODE"] = "smtp "
        self.add_existing_member()

        response = self.post_existing_member()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "waiting_for_smtp_configuration")
        record = PortalInvitation.query.one()
        self.assertEqual(record.attempts, 0)
        self.assertIsNone(record.sent_at)
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_requires_exact_sent_delivery_result(self):
        self.enable_existing_member_pilot()
        self.add_existing_member()

        with patch("dreamz_portal.deliver_email", return_value="logged") as delivery:
            response = self.post_existing_member()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        record = PortalInvitation.query.one()
        self.assertEqual(record.last_error, "email_delivery_not_confirmed")
        self.assertEqual(record.attempts, 1)
        self.assertIsNone(record.sent_at)
        self.assertEqual(delivery.call_count, 1)

    def test_existing_member_with_unchanged_synced_email_sends_exactly_once(self):
        self.enable_existing_member_pilot()
        self.add_existing_member()

        payload = self.existing_member_payload()
        sent = self.post_existing_member(payload=payload)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json["status"], "sent")
        self.assertFalse(sent.json["duplicate"])
        self.assertEqual(sent.json["request_type"], payload["request_type"])
        self.assertEqual(sent.json["member_number"], payload["member_number"])
        self.assertEqual(sent.json["idempotency_key"], payload["idempotency_key"])
        record = PortalInvitation.query.one()
        self.assertEqual(record.attempts, 1)
        self.assertIsNotNone(record.sent_at)
        self.assertEqual(EmailLog.query.count(), 1)

        duplicate = self.post_existing_member(payload=payload)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertEqual(duplicate.json["status"], "sent")
        self.assertEqual(duplicate.json["idempotency_key"], payload["idempotency_key"])
        db.session.refresh(record)
        self.assertEqual(record.attempts, 1)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_existing_member_changed_email_waits_for_sync_then_sends(self):
        self.enable_existing_member_pilot()
        self.add_existing_member(email="old.member@example.com")

        waiting = self.post_existing_member()
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json["status"], "waiting_for_gym_assistant_email")
        self.assertEqual(EmailLog.query.count(), 0)

        sync_response = self.client.post(
            "/api/sync/members",
            headers={"X-Sync-Token": "sync-test-token"},
            json={
                "source": "existing-member-reverification-test",
                "members": [{
                    "member_id": "42001",
                    "name": "Member, Existing",
                    "email": "existing.member@example.com",
                    "plan_type": "Contract 6 months 2024",
                }],
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(sync_response.json["portal_invitations"]["sent"], 1)
        record = PortalInvitation.query.one()
        self.assertEqual(record.status, "sent")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(EmailLog.query.count(), 1)

    def test_existing_member_missing_email_waits_for_gym_assistant_sync(self):
        self.enable_existing_member_pilot()
        self.add_existing_member(email="")
        response = self.post_existing_member()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "waiting_for_gym_assistant_email")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_duplicate_email_requires_manual_review(self):
        self.enable_existing_member_pilot()
        db.session.add_all([
            Member(
                member_id="42001",
                name="Member, Existing",
                email="existing.member@example.com",
                plan_type="Contract 6 months 2024",
            ),
            Member(
                member_id="42002",
                name="Member, Duplicate",
                email="EXISTING.MEMBER@example.com",
                plan_type="Contract 6 months 2024",
            ),
        ])
        db.session.commit()
        response = self.post_existing_member()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        self.assertTrue(response.json["requires_manual_review"])
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_ineligible_live_plan_requires_manual_review(self):
        self.enable_existing_member_pilot()
        self.add_existing_member(plan_type="Week Pass Dreamz")
        response = self.post_existing_member()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_live_plan_must_exactly_match_signup_plan(self):
        self.enable_existing_member_pilot()
        self.add_existing_member(plan_type="Contract 12 months 2024")
        response = self.post_existing_member()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        record = PortalInvitation.query.one()
        self.assertEqual(
            record.last_error,
            "gym_assistant_plan_does_not_match_signup",
        )
        self.assertEqual(EmailLog.query.count(), 0)

    def test_existing_member_idempotency_key_binds_header_body_and_payload(self):
        valid_payload = self.existing_member_payload()

        header_mismatch = self.post_existing_member(
            payload=valid_payload,
            idempotency_key="0" * 64,
        )
        self.assertEqual(header_mismatch.status_code, 422)
        self.assertEqual(PortalInvitation.query.count(), 0)

        tampered_payload = dict(valid_payload)
        tampered_payload["member_number"] = "42002"
        tampered = self.post_existing_member(payload=tampered_payload)
        self.assertEqual(tampered.status_code, 422)
        self.assertEqual(PortalInvitation.query.count(), 0)

        accepted = self.post_existing_member(payload=valid_payload)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(PortalInvitation.query.count(), 1)

        changed = self.existing_member_payload(language="es")
        conflict = self.post_existing_member(payload=changed)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["status"], "conflict")
        self.assertEqual(
            conflict.json["request_type"],
            "existing_member_reverification",
        )
        self.assertEqual(conflict.json["member_number"], valid_payload["member_number"])
        self.assertEqual(
            conflict.json["idempotency_key"],
            valid_payload["idempotency_key"],
        )
        self.assertTrue(conflict.json["requires_manual_review"])
        self.assertEqual(PortalInvitation.query.count(), 1)

    def test_request_type_is_validated_and_bound_to_reference(self):
        invalid = self.existing_member_payload(request_type="unexpected_flow")
        rejected = self.post_existing_member(payload=invalid)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(PortalInvitation.query.count(), 0)

        existing = self.post_existing_member()
        self.assertEqual(existing.status_code, 202)
        legacy_payload = self.invitation_payload(reference="DF-20260726-900001")
        conflict = self.client.post(
            "/api/integrations/signup/portal-invitations",
            json=legacy_payload,
            headers=self.invitation_headers(),
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json["request_type"],
            "existing_member_reverification",
        )

    def test_existing_member_activation_uses_requested_language(self):
        self.enable_existing_member_pilot()
        member = self.add_existing_member()
        payload = self.existing_member_payload(language="es")
        response = self.post_existing_member(payload=payload)
        self.assertEqual(response.status_code, 200)
        expected_subject, _, _ = build_portal_activation_email(member, "es")
        self.assertEqual(EmailLog.query.one().subject, expected_subject)

    def test_invitation_waits_for_exact_synced_member_then_sends_once(self):
        waiting = self.post_invitation()
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json["request_type"], "new_member")
        self.assertEqual(waiting.json["status"], "waiting_for_member")
        self.assertNotIn("member_number", waiting.json)
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

    def test_legacy_new_member_hash_remains_idempotent_after_schema_upgrade(self):
        legacy_canonical = self.invitation_payload()
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_canonical,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        db.session.add(PortalInvitation(
            source_reference=legacy_canonical["reference"],
            request_type="new_member",
            member_id=legacy_canonical["member_number"],
            expected_email_hash=hashlib.sha256(
                legacy_canonical["expected_email"].encode("utf-8")
            ).hexdigest(),
            language=legacy_canonical["language"],
            signup_plan=legacy_canonical["signup_plan"],
            request_payload_hash=legacy_hash,
        ))
        db.session.commit()

        duplicate = self.post_invitation()
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json["duplicate"])
        self.assertEqual(duplicate.json["request_type"], "new_member")
        self.assertEqual(duplicate.json["status"], "waiting_for_member")
        self.assertEqual(PortalInvitation.query.count(), 1)

    def test_runtime_schema_adds_and_backfills_request_type(self):
        db.session.execute(text("DROP TABLE portal_invitation"))
        db.session.execute(text("""
            CREATE TABLE portal_invitation (
                id INTEGER PRIMARY KEY,
                source_reference VARCHAR(64) UNIQUE NOT NULL,
                member_id VARCHAR NOT NULL,
                expected_email_hash VARCHAR(64) NOT NULL,
                language VARCHAR(8) NOT NULL,
                signup_plan VARCHAR(32) NOT NULL,
                request_payload_hash VARCHAR(64) NOT NULL,
                status VARCHAR(40) NOT NULL,
                attempts INTEGER NOT NULL,
                manual_review_required BOOLEAN NOT NULL,
                last_error TEXT,
                email_log_id INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                ready_at DATETIME,
                send_started_at DATETIME,
                sent_at DATETIME
            )
        """))
        db.session.execute(text("""
            INSERT INTO portal_invitation (
                source_reference,
                member_id,
                expected_email_hash,
                language,
                signup_plan,
                request_payload_hash,
                status,
                attempts,
                manual_review_required,
                created_at,
                updated_at
            ) VALUES (
                'DF-20260716-1234',
                '42001',
                'email-hash',
                'nl',
                'month',
                'payload-hash',
                'waiting_for_member',
                0,
                0,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
        """))
        db.session.commit()
        app.config["_RUNTIME_SCHEMA_READY"] = False

        ensure_runtime_schema()

        columns = {column["name"] for column in inspect(db.engine).get_columns("portal_invitation")}
        self.assertIn("request_type", columns)
        record = PortalInvitation.query.one()
        self.assertEqual(record.request_type, "new_member")

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

    def test_kmar_invitation_accepts_current_internal_gym_assistant_plan(self):
        db.session.add(Member(
            member_id="42001",
            name="KMAR, Member",
            email="new.member@example.com",
            plan_type="KMAR medewerker",
        ))
        db.session.commit()

        response = self.post_invitation(signup_plan="kmar_2026")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "sent")
        self.assertEqual(PortalInvitation.query.one().signup_plan, "kmar_2026")
        self.assertEqual(EmailLog.query.count(), 1)

    def test_kmar_current_plan_from_sync_unlocks_waiting_invitation(self):
        waiting = self.post_invitation(signup_plan="kmar_2026")
        self.assertEqual(waiting.status_code, 202)
        self.assertEqual(waiting.json["status"], "waiting_for_member")
        self.assertEqual(EmailLog.query.count(), 0)

        synced = self.client.post(
            "/api/sync/members",
            headers={"X-Sync-Token": "sync-test-token"},
            json={
                "source": "kmar-current-plan-test",
                "members": [{
                    "member_id": "42001",
                    "name": "KMAR, Member",
                    "email": "new.member@example.com",
                    "plan_type": "KMAR medewerker",
                }],
            },
        )

        self.assertEqual(synced.status_code, 200)
        self.assertEqual(synced.json["members_new"], 1)
        member = Member.query.filter_by(member_id="42001").one()
        self.assertEqual(member.plan_type, "KMAR medewerker")
        invitation = PortalInvitation.query.one()
        self.assertEqual(invitation.status, "sent")
        self.assertEqual(invitation.signup_plan, "kmar_2026")
        self.assertEqual(EmailLog.query.count(), 1)

    def test_kmar_invitation_preserves_legacy_internal_gym_assistant_plan(self):
        db.session.add(Member(
            member_id="42001",
            name="KMAR, Member",
            email="new.member@example.com",
            plan_type="KMAR medewerker 2018",
        ))
        db.session.commit()

        response = self.post_invitation(signup_plan="kmar_2026")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "sent")
        self.assertEqual(PortalInvitation.query.one().signup_plan, "kmar_2026")
        self.assertEqual(EmailLog.query.count(), 1)

    def test_kmar_plan_mapping_allows_only_current_and_legacy_normalized_names(self):
        for plan_type in (
            "KMAR medewerker",
            " kmar   MEDEWERKER ",
            "KMAR medewerker 2018",
        ):
            with self.subTest(plan_type=plan_type):
                self.assertEqual(portal_member_signup_plan(plan_type), "kmar_2026")
                self.assertTrue(
                    portal_invitation_member_plan_matches("kmar_2026", plan_type)
                )
                self.assertFalse(
                    portal_invitation_member_plan_matches("month", plan_type)
                )

        for plan_type in (
            "KMAR",
            "KMAR medewerker 2018 extra",
            "KMAR medewerker 2026",
            "KMAR medewerkers",
        ):
            with self.subTest(plan_type=plan_type):
                self.assertIsNone(portal_member_signup_plan(plan_type))
                self.assertFalse(
                    portal_invitation_member_plan_matches("kmar_2026", plan_type)
                )

        self.assertEqual(
            portal_member_signup_plan("Contract 12 months 2024"),
            "twelve",
        )
        self.assertFalse(
            portal_invitation_member_plan_matches(
                "kmar_2026",
                "Contract 12 months 2024",
            )
        )

    def test_kmar_invitation_fails_closed_for_other_gym_assistant_plan(self):
        db.session.add(Member(
            member_id="42001",
            name="KMAR, Member",
            email="new.member@example.com",
            plan_type="Contract 12 months 2024",
        ))
        db.session.commit()

        response = self.post_invitation(signup_plan="kmar_2026")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(PortalInvitation.query.one().last_error, "gym_assistant_plan_not_eligible")
        self.assertEqual(EmailLog.query.count(), 0)

    def test_kmar_internal_plan_cannot_satisfy_a_regular_signup_invitation(self):
        db.session.add(Member(
            member_id="42001",
            name="KMAR, Member",
            email="new.member@example.com",
            plan_type="KMAR medewerker 2018",
        ))
        db.session.commit()

        response = self.post_invitation(signup_plan="month")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "manual_review")
        self.assertEqual(PortalInvitation.query.one().last_error, "gym_assistant_plan_not_eligible")
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
