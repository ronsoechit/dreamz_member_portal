import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
import unittest
from unittest.mock import patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "journal-evidence-test-secret"

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from dreamz_portal import (  # noqa: E402
    EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_SCHEMA,
    EXISTING_MEMBER_JOURNAL_EVIDENCE_RECEIPT_SCHEMA,
    EXISTING_MEMBER_JOURNAL_EVIDENCE_REQUEST_SCHEMA,
    EXISTING_MEMBER_JOURNAL_EVIDENCE_SIGNATURE_DOMAIN,
    ExistingMemberJournalEvidenceReceipt,
    ExistingMemberJournalEvidenceRequest,
    app,
    db,
    existing_member_journal_evidence_jcs,
    existing_member_journal_evidence_sha256,
)


JS_ISO_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")


def js_iso(value):
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="milliseconds") + "Z"


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def raw_public_key(private_key):
    return b64url(private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ))


class ExistingMemberJournalEvidenceApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = (
            "signup-portal-journal-evidence-test-token"
        )
        app.config["SYNC_API_TOKEN"] = "portal-sync-journal-evidence-test-token"
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"] = True
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE_PROVEN"
        ] = True
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_CLAIM_SECONDS"] = 120
        self.agent_private_key = Ed25519PrivateKey.from_private_bytes(b"\x11" * 32)
        self.previous_agent_private_key = Ed25519PrivateKey.from_private_bytes(
            b"\x12" * 32
        )
        self.portal_private_key = Ed25519PrivateKey.from_private_bytes(b"\x21" * 32)
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_PUBLIC_KEYS_JSON"] = {
            "frontdesk-current-2026": raw_public_key(self.agent_private_key),
            "frontdesk-previous-2026": raw_public_key(
                self.previous_agent_private_key
            ),
        }
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_SIGNING_KEY_ID"
        ] = "portal-current-2026"
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_PRIVATE_KEY"] = (
            b64url(self.portal_private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        )
        app.config["_RUNTIME_SCHEMA_READY"] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        self.client = app.test_client()
        self.base = datetime.now(timezone.utc).replace(
            tzinfo=None,
            microsecond=123000,
        ) - timedelta(seconds=10)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        app.config["SIGNUP_PORTAL_INTEGRATION_TOKEN"] = None
        app.config["SYNC_API_TOKEN"] = None
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"] = False
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE_PROVEN"
        ] = False
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_PUBLIC_KEYS_JSON"
        ] = ""
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_SIGNING_KEY_ID"
        ] = ""
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_PRIVATE_KEY"] = ""
        app.config["_RUNTIME_SCHEMA_READY"] = False

    @staticmethod
    def signup_headers(payload=None):
        headers = {
            "Authorization": (
                "Bearer signup-portal-journal-evidence-test-token"
            ),
        }
        if payload is not None:
            headers["Idempotency-Key"] = existing_member_journal_evidence_sha256(
                payload
            )
        return headers

    @staticmethod
    def sync_headers(claim_id=None):
        headers = {
            "X-Sync-Token": "portal-sync-journal-evidence-test-token",
        }
        if claim_id:
            headers["X-Evidence-Claim-Id"] = claim_id
        return headers

    def request_payload(self, phase="pre", **overrides):
        phase_suffix = "pre" if phase == "pre" else "post"
        payload = {
            "schema": EXISTING_MEMBER_JOURNAL_EVIDENCE_REQUEST_SCHEMA,
            "request_id": f"journal-request-{phase_suffix}-000001",
            "phase": phase,
            "nonce": f"journal-nonce-{phase_suffix}-00000001",
            "requested_at": js_iso(self.base),
            "expires_at": js_iso(self.base + timedelta(minutes=5)),
            "reference": "DF-20260801-900001",
            "source_flow": "existing_member_reverification",
            "member_number": "42001",
            "operation": "update_existing_documents",
            "baseline_sha256": "1" * 64,
            "proposal_sha256": "2" * 64,
            "signed_pdf_sha256": "3" * 64,
            "review_bundle_sha256": "4" * 64,
            "approved_mutation_core_sha256": "5" * 64,
        }
        if phase == "post":
            payload.update({
                "evidence_bound_job_sha256": "6" * 64,
                "pre_receipt_sha256": "7" * 64,
                "local_readback_sha256": "8" * 64,
                "local_readback_completed_at": js_iso(
                    self.base + timedelta(seconds=2)
                ),
            })
            payload["requested_at"] = js_iso(self.base + timedelta(seconds=3))
        payload.update(overrides)
        return payload

    def post_signup(self, payload, idempotency_key=None):
        headers = self.signup_headers()
        headers["Idempotency-Key"] = (
            existing_member_journal_evidence_sha256(payload)
            if idempotency_key is None
            else idempotency_key
        )
        return self.client.post(
            "/api/integrations/signup/existing-member-journal-evidence",
            json=payload,
            headers=headers,
        )

    def claim(self):
        response = self.client.get(
            "/api/sync/existing-member-journal-evidence/requests"
            "?agent_id=frontdesk_dreamz&limit=1",
            headers=self.sync_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(response.json["requests"]), 1)
        return response.json["requests"][0]

    def agent_envelope(
        self,
        claimed,
        *,
        observed_at=None,
        private_key=None,
        key_id="frontdesk-current-2026",
        source_overrides=None,
        scope_overrides=None,
        payload_overrides=None,
    ):
        request_payload = claimed["request"]
        source = {
            "kind": "gym_assistant_live_journal",
            "locator_fingerprint_sha256": "a" * 64,
            "data_path_fingerprint_sha256": "b" * 64,
            "file_identity_sha256": "c" * 64,
            "byte_length": 123456,
            "source_sha256": "d" * 64,
            "stable_read_count": 2,
        }
        source.update(source_overrides or {})
        scope = {
            "classifier_version": "dreamz.ga.journal.member-lines.v1",
            "complete": True,
            "member_record_count": 9,
            "member_record_multiset_sha256": "e" * 64,
            "issue_count": 0,
        }
        scope.update(scope_overrides or {})
        payload = {
            "schema": EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_SCHEMA,
            "request_id": request_payload["request_id"],
            "request_payload_sha256": claimed["request_payload_sha256"],
            "phase": request_payload["phase"],
            "reference": request_payload["reference"],
            "member_number": request_payload["member_number"],
            "agent_id": "frontdesk_dreamz",
            "observed_at": js_iso(
                observed_at or (self.base + timedelta(seconds=1))
            ),
            "source": source,
            "scope": scope,
        }
        payload.update(payload_overrides or {})
        payload_sha256 = existing_member_journal_evidence_sha256(payload)
        signer = private_key or self.agent_private_key
        signature = signer.sign(
            EXISTING_MEMBER_JOURNAL_EVIDENCE_SIGNATURE_DOMAIN
            + payload_sha256.encode("ascii")
        )
        return {
            "payload": payload,
            "payload_sha256": payload_sha256,
            "key_id": key_id,
            "signature": b64url(signature),
        }

    def post_result(self, claimed, envelope, claim_id=None):
        return self.client.post(
            "/api/sync/existing-member-journal-evidence/requests/"
            f"{claimed['request_id']}/result?agent_id=frontdesk_dreamz",
            json=envelope,
            headers=self.sync_headers(claim_id or claimed["claim_id"]),
        )

    def complete_pre(self, *, scope_overrides=None, key_id="frontdesk-current-2026", private_key=None):
        payload = self.request_payload("pre")
        created = self.post_signup(payload)
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        claimed = self.claim()
        envelope = self.agent_envelope(
            claimed,
            scope_overrides=scope_overrides,
            key_id=key_id,
            private_key=private_key,
        )
        completed = self.post_result(claimed, envelope)
        return payload, claimed, envelope, completed

    def test_feature_and_source_coverage_are_fail_closed(self):
        payload = self.request_payload()
        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"] = False
        disabled = self.post_signup(payload)
        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(disabled.json["code"], "feature_disabled")
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 0)

        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"] = True
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE_PROVEN"
        ] = False
        unproven = self.post_signup(payload)
        self.assertEqual(unproven.status_code, 503)
        self.assertEqual(unproven.json["code"], "source_coverage_unproven")
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 0)

        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE_PROVEN"
        ] = True
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_PRIVATE_KEY"
        ] = b64url(self.agent_private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        shared_key = self.post_signup(payload)
        self.assertEqual(shared_key.status_code, 503)
        self.assertEqual(shared_key.json["code"], "invalid_key_configuration")
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 0)

        app.config["EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_PRIVATE_KEY"] = (
            b64url(self.portal_private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        )
        same_agent_key = raw_public_key(self.agent_private_key)
        app.config[
            "EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_PUBLIC_KEYS_JSON"
        ] = {
            "frontdesk-current-2026": same_agent_key,
            "frontdesk-previous-2026": same_agent_key,
        }
        duplicate_rotation_key = self.post_signup(payload)
        self.assertEqual(duplicate_rotation_key.status_code, 503)
        self.assertEqual(
            duplicate_rotation_key.json["code"],
            "invalid_key_configuration",
        )

    def test_both_signup_and_sync_routes_require_their_own_transport_auth(self):
        payload = self.request_payload()
        no_signup_auth = self.client.post(
            "/api/integrations/signup/existing-member-journal-evidence",
            json=payload,
            headers={
                "Idempotency-Key": existing_member_journal_evidence_sha256(payload),
            },
        )
        self.assertEqual(no_signup_auth.status_code, 403)
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 0)

        self.assertEqual(self.post_signup(payload).status_code, 202)
        no_sync_auth = self.client.get(
            "/api/sync/existing-member-journal-evidence/requests"
            "?agent_id=frontdesk_dreamz",
        )
        self.assertEqual(no_sync_auth.status_code, 403)
        self.assertEqual(
            ExistingMemberJournalEvidenceRequest.query.one().status,
            "pending",
        )

    def test_pre_happy_path_is_signed_immutable_and_idempotent(self):
        payload, claimed, envelope, completed = self.complete_pre()
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        self.assertEqual(completed.json["status"], "completed")
        self.assertEqual(
            completed.json["receipt"]["verdict"],
            "pre_verified",
        )
        self.assertEqual(completed.json["idempotency_key"], claimed["request_payload_sha256"])
        receipt_envelope = completed.json["receipt"]["envelope"]
        receipt_payload = receipt_envelope["payload"]
        self.assertEqual(receipt_payload["schema"], EXISTING_MEMBER_JOURNAL_EVIDENCE_RECEIPT_SCHEMA)
        self.assertEqual(receipt_payload["phase"], "pre")
        self.assertTrue(JS_ISO_RE.fullmatch(receipt_payload["issued_at"]))
        self.assertTrue(JS_ISO_RE.fullmatch(receipt_payload["expires_at"]))
        self.assertEqual(
            set(receipt_payload),
            {
                "schema", "receipt_id", "request_id", "request_payload_sha256",
                "agent_result_payload_sha256", "phase", "verdict", "reference",
                "source_flow", "member_number", "operation", "baseline_sha256",
                "proposal_sha256", "signed_pdf_sha256", "review_bundle_sha256",
                "approved_mutation_core_sha256", "issued_at", "expires_at",
                "source", "scope",
            },
        )
        self.assertEqual(
            existing_member_journal_evidence_sha256(receipt_payload),
            receipt_envelope["payload_sha256"],
        )
        self.portal_private_key.public_key().verify(
            base64.urlsafe_b64decode(receipt_envelope["signature"] + "=="),
            EXISTING_MEMBER_JOURNAL_EVIDENCE_SIGNATURE_DOMAIN
            + receipt_envelope["payload_sha256"].encode("ascii"),
        )
        duplicate_result = self.post_result(claimed, envelope)
        self.assertEqual(duplicate_result.status_code, 200)
        self.assertTrue(duplicate_result.json["duplicate"])
        self.assertEqual(ExistingMemberJournalEvidenceReceipt.query.count(), 1)
        duplicate_request = self.post_signup(payload)
        self.assertEqual(duplicate_request.status_code, 200)
        self.assertTrue(duplicate_request.json["duplicate"])
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 1)

    def test_request_contract_and_idempotency_conflicts_fail_closed(self):
        payload = self.request_payload()
        wrong_key = self.post_signup(payload, idempotency_key="0" * 64)
        self.assertEqual(wrong_key.status_code, 422)
        self.assertEqual(wrong_key.json["code"], "invalid_idempotency_key")

        created = self.post_signup(payload)
        self.assertEqual(created.status_code, 202)
        conflict_payload = dict(payload)
        conflict_payload["proposal_sha256"] = "9" * 64
        conflict = self.post_signup(conflict_payload)
        self.assertEqual(conflict.status_code, 409)
        self.assertTrue(conflict.json["requires_manual_review"])
        persisted = ExistingMemberJournalEvidenceRequest.query.one()
        self.assertEqual(persisted.status, "manual_review")

    def test_duplicate_json_keys_are_rejected_before_idempotency(self):
        raw = (
            '{"schema":"dreamz.existing-member-journal-evidence.request.v1",'
            '"phase":"pre","phase":"post"}'
        )
        rejected = self.client.post(
            "/api/integrations/signup/existing-member-journal-evidence",
            data=raw.encode("utf-8"),
            content_type="application/json",
            headers={
                "Authorization": (
                    "Bearer signup-portal-journal-evidence-test-token"
                ),
                "Idempotency-Key": "0" * 64,
            },
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "invalid_json")
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.count(), 0)

    def test_claim_ownership_expiry_and_agent_binding_are_enforced(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        denied = self.client.get(
            "/api/sync/existing-member-journal-evidence/requests?agent_id=ron_laptop",
            headers=self.sync_headers(),
        )
        self.assertEqual(denied.status_code, 403)

        claimed = self.claim()
        envelope = self.agent_envelope(claimed)
        wrong_claim = self.post_result(claimed, envelope, claim_id="wrong-claim-id-000000")
        self.assertEqual(wrong_claim.status_code, 409)
        record = ExistingMemberJournalEvidenceRequest.query.one()
        record.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        reclaimed = self.claim()
        self.assertNotEqual(reclaimed["claim_id"], claimed["claim_id"])
        old_claim = self.post_result(reclaimed, self.agent_envelope(reclaimed), claim_id=claimed["claim_id"])
        self.assertEqual(old_claim.status_code, 409)
        accepted = self.post_result(reclaimed, self.agent_envelope(reclaimed))
        self.assertEqual(accepted.status_code, 201)

    def test_invalid_result_cannot_poison_request_without_exact_claim(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        invalid_envelope = {"not": "the signed envelope"}
        rejected = self.post_result(
            claimed,
            invalid_envelope,
            claim_id="wrong-claim-id-000000",
        )
        self.assertEqual(rejected.status_code, 409)
        record = ExistingMemberJournalEvidenceRequest.query.one()
        self.assertEqual(record.status, "claimed")
        self.assertIsNone(record.failure_code)

    def test_future_observation_is_rejected_after_valid_claim(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        future_envelope = self.agent_envelope(
            claimed,
            observed_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(minutes=5),
        )
        rejected = self.post_result(claimed, future_envelope)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "invalid_timeline")
        self.assertEqual(
            ExistingMemberJournalEvidenceRequest.query.one().status,
            "manual_review",
        )

    def test_wrong_key_and_phase_swap_require_manual_review(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        unknown_key = Ed25519PrivateKey.from_private_bytes(b"\x44" * 32)
        envelope = self.agent_envelope(
            claimed,
            private_key=unknown_key,
            key_id="not-configured-2026",
        )
        rejected = self.post_result(claimed, envelope)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "unknown_agent_key")
        self.assertEqual(ExistingMemberJournalEvidenceRequest.query.one().status, "manual_review")

        db.session.delete(ExistingMemberJournalEvidenceRequest.query.one())
        db.session.commit()
        payload = self.request_payload(
            request_id="journal-request-pre-000002",
            nonce="journal-nonce-pre-00000002",
            reference="DF-20260801-900002",
        )
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        swapped = self.agent_envelope(claimed, payload_overrides={"phase": "post"})
        rejected = self.post_result(claimed, swapped)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "agent_request_binding_mismatch")

    def test_known_key_id_with_wrong_signature_is_terminal(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        wrong_signature = self.agent_envelope(
            claimed,
            private_key=self.previous_agent_private_key,
            key_id="frontdesk-current-2026",
        )
        rejected = self.post_result(claimed, wrong_signature)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "invalid_agent_signature")
        self.assertEqual(
            ExistingMemberJournalEvidenceRequest.query.one().status,
            "manual_review",
        )

    def test_explicit_previous_agent_key_is_accepted(self):
        _payload, _claimed, _envelope, completed = self.complete_pre(
            key_id="frontdesk-previous-2026",
            private_key=self.previous_agent_private_key,
        )
        self.assertEqual(completed.status_code, 201)
        self.assertEqual(completed.json["receipt"]["verdict"], "pre_verified")

    def test_incomplete_pre_snapshot_is_signed_for_manual_review(self):
        _payload, _claimed, _envelope, completed = self.complete_pre(
            scope_overrides={"complete": False, "issue_count": 1},
        )
        self.assertEqual(completed.status_code, 201)
        self.assertEqual(completed.json["status"], "manual_review")
        self.assertEqual(completed.json["receipt"]["verdict"], "manual_review")
        self.assertTrue(completed.json["requires_manual_review"])

    def test_request_bindings_and_receipts_are_immutable(self):
        _payload, _claimed, _envelope, completed = self.complete_pre()
        self.assertEqual(completed.status_code, 201)
        record = ExistingMemberJournalEvidenceRequest.query.one()
        record.member_number = "42002"
        with self.assertRaisesRegex(RuntimeError, "request bindings are immutable"):
            db.session.commit()
        db.session.rollback()

        receipt = ExistingMemberJournalEvidenceReceipt.query.one()
        receipt.verdict = "manual_review"
        with self.assertRaisesRegex(RuntimeError, "receipts are immutable"):
            db.session.commit()
        db.session.rollback()

    def test_exact_post_equality_ignores_unrelated_global_journal_changes(self):
        _pre_payload, _claimed, _envelope, pre_completed = self.complete_pre()
        pre_receipt_sha256 = pre_completed.json["receipt"]["receipt_sha256"]
        post_payload = self.request_payload(
            "post",
            pre_receipt_sha256=pre_receipt_sha256,
        )
        created = self.post_signup(post_payload)
        self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
        claimed = self.claim()
        post_envelope = self.agent_envelope(
            claimed,
            observed_at=self.base + timedelta(seconds=4),
            source_overrides={
                "byte_length": 999999,
                "source_sha256": "f" * 64,
            },
        )
        completed = self.post_result(claimed, post_envelope)
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        self.assertEqual(completed.json["status"], "completed")
        self.assertEqual(completed.json["receipt"]["verdict"], "verified_unchanged")
        receipt_payload = completed.json["receipt"]["envelope"]["payload"]
        self.assertTrue(receipt_payload["comparison"]["verified_unchanged"])
        self.assertEqual(
            set(receipt_payload["comparison"]),
            {
                "source_locator_match", "data_path_match", "file_identity_match",
                "classifier_match", "member_record_count_match",
                "member_record_multiset_match", "verified_unchanged",
            },
        )

    def test_post_result_may_finish_after_pre_expiry_when_post_was_bound_in_time(self):
        _pre_payload, _claimed, _envelope, pre_completed = self.complete_pre()
        pre_expires_at = self.base + timedelta(minutes=5)
        post_requested_at = pre_expires_at - timedelta(seconds=10)
        post_payload = self.request_payload(
            "post",
            requested_at=js_iso(post_requested_at),
            expires_at=js_iso(post_requested_at + timedelta(minutes=5)),
            local_readback_completed_at=js_iso(
                post_requested_at - timedelta(seconds=1)
            ),
            pre_receipt_sha256=pre_completed.json["receipt"]["receipt_sha256"],
        )
        with patch(
            "dreamz_portal.existing_member_journal_evidence_utc_now",
            return_value=post_requested_at,
        ):
            created = self.post_signup(post_payload)
            self.assertEqual(created.status_code, 202, created.get_data(as_text=True))
            claimed = self.claim()

        result_time = pre_expires_at + timedelta(seconds=5)
        post_envelope = self.agent_envelope(
            claimed,
            observed_at=result_time - timedelta(seconds=1),
        )
        with patch(
            "dreamz_portal.existing_member_journal_evidence_utc_now",
            return_value=result_time,
        ):
            completed = self.post_result(claimed, post_envelope)
        self.assertEqual(completed.status_code, 201, completed.get_data(as_text=True))
        self.assertEqual(completed.json["status"], "completed")
        self.assertEqual(
            completed.json["receipt"]["verdict"],
            "verified_unchanged",
        )

    def test_post_member_scope_change_is_terminal_manual_review(self):
        _pre_payload, _claimed, _envelope, pre_completed = self.complete_pre()
        post_payload = self.request_payload(
            "post",
            pre_receipt_sha256=pre_completed.json["receipt"]["receipt_sha256"],
        )
        self.assertEqual(self.post_signup(post_payload).status_code, 202)
        claimed = self.claim()
        changed = self.agent_envelope(
            claimed,
            observed_at=self.base + timedelta(seconds=4),
            scope_overrides={
                "member_record_count": 10,
                "member_record_multiset_sha256": "9" * 64,
            },
        )
        completed = self.post_result(claimed, changed)
        self.assertEqual(completed.status_code, 201)
        self.assertEqual(completed.json["status"], "manual_review")
        receipt_payload = completed.json["receipt"]["envelope"]["payload"]
        self.assertFalse(receipt_payload["comparison"]["member_record_count_match"])
        self.assertFalse(receipt_payload["comparison"]["member_record_multiset_match"])
        self.assertFalse(receipt_payload["comparison"]["verified_unchanged"])

    def test_sanitized_contract_rejects_raw_rows_paths_names_and_amounts(self):
        payload = self.request_payload()
        self.assertEqual(self.post_signup(payload).status_code, 202)
        claimed = self.claim()
        envelope = self.agent_envelope(claimed)
        envelope["payload"]["source"]["local_path"] = "C:/Gym Assistant/Journal.jtx"
        envelope["payload_sha256"] = existing_member_journal_evidence_sha256(
            envelope["payload"]
        )
        envelope["signature"] = b64url(self.agent_private_key.sign(
            EXISTING_MEMBER_JOURNAL_EVIDENCE_SIGNATURE_DOMAIN
            + envelope["payload_sha256"].encode("ascii")
        ))
        rejected = self.post_result(claimed, envelope)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "invalid_contract")
        serialized = "\n".join(
            value or ""
            for value in db.session.query(
                ExistingMemberJournalEvidenceRequest.request_payload_json,
                ExistingMemberJournalEvidenceRequest.result_envelope_json,
            ).first()
        )
        for forbidden in (
            "Journal.jtx", "C:/", "member_name", "amount", "raw_line",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_jcs_and_timestamp_contract_are_exact(self):
        fixture = {
            "z": 1,
            "a": "é",
            "list": [True, False, None, 0],
        }
        self.assertEqual(
            existing_member_journal_evidence_jcs(fixture),
            '{"a":"é","list":[true,false,null,0],"z":1}',
        )
        payload = self.request_payload(
            requested_at="2026-08-01T12:00:00Z",
        )
        rejected = self.post_signup(payload)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json["code"], "invalid_timestamp")


if __name__ == "__main__":
    unittest.main()
