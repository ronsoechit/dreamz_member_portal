from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import portal_sync_journal_evidence as client
import sync_agent


BASE = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
AGENT_SEED = bytes(range(1, 33))
PORTAL_SEED = bytes(range(33, 65))


def js_time(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{value.microsecond // 1000:03d}Z"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def request_payload(phase: str = "pre") -> dict:
    suffix = "pre" if phase == "pre" else "post"
    payload = {
        "schema": client.EVIDENCE_REQUEST_SCHEMA,
        "request_id": f"journal-request-{suffix}-000001",
        "phase": phase,
        "nonce": f"journal-nonce-{suffix}-00000001",
        "requested_at": js_time(BASE),
        "expires_at": js_time(BASE + timedelta(minutes=5)),
        "reference": "DF-20260801-900001",
        "source_flow": client.EVIDENCE_SOURCE_FLOW,
        "member_number": "42001",
        "operation": client.EVIDENCE_OPERATION,
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
            "local_readback_completed_at": js_time(BASE + timedelta(seconds=2)),
        })
        payload["requested_at"] = js_time(BASE + timedelta(seconds=3))
    return payload


def claim_payload(phase: str = "pre") -> dict:
    payload = request_payload(phase)
    return {
        "request_id": payload["request_id"],
        "request_payload_sha256": client.journal_evidence_sha256(payload),
        "request": payload,
        "claim_id": "journal-claim-000000000001",
        "claim_expires_at": js_time(BASE + timedelta(minutes=2)),
    }


def snapshot_core(member_number: str = "42001") -> dict:
    return {
        "schema": client.EVIDENCE_CORE_SCHEMA,
        "member_number": member_number,
        "observed_at": "ignored-by-client",
        "source": {
            "kind": client.EVIDENCE_SOURCE_KIND,
            "locator_fingerprint_sha256": "a" * 64,
            "data_path_fingerprint_sha256": "b" * 64,
            "file_identity_sha256": "c" * 64,
            "byte_length": 123456,
            "source_sha256": "d" * 64,
            "stable_read_count": 2,
        },
        "scope": {
            "classifier_version": client.EVIDENCE_CLASSIFIER_VERSION,
            "complete": True,
            "member_record_count": 9,
            "member_record_multiset_sha256": "e" * 64,
            "issue_count": 0,
        },
    }


def config() -> client.JournalEvidenceClientConfig:
    return client.JournalEvidenceClientConfig(
        portal_url="https://portal.example",
        sync_token="test-sync-token",
        agent_id=client.EVIDENCE_TARGET_AGENT,
        key_id="frontdesk-current-2026",
        private_key=Ed25519PrivateKey.from_private_bytes(AGENT_SEED),
        timeout_seconds=5,
        limit=1,
        retries=1,
    )


def validated_claim(phase: str = "pre") -> dict:
    return client._validate_claim(
        claim_payload(phase),
        BASE + timedelta(seconds=1),
    )


def result_response(
    claim: dict,
    agent_payload_sha256: str,
    *,
    duplicate=False,
    manual_review=False,
    scope=None,
) -> dict:
    request = claim["request"]
    phase = request["phase"]
    verdict = (
        "manual_review"
        if manual_review
        else ("pre_verified" if phase == "pre" else "verified_unchanged")
    )
    receipt_payload = {
        "schema": client.EVIDENCE_RECEIPT_SCHEMA,
        "receipt_id": "journal-receipt-0000000001",
        "request_id": request["request_id"],
        "request_payload_sha256": claim["request_payload_sha256"],
        "agent_result_payload_sha256": agent_payload_sha256,
        "phase": phase,
        "verdict": verdict,
        "reference": request["reference"],
        "source_flow": request["source_flow"],
        "member_number": request["member_number"],
        "operation": request["operation"],
        "baseline_sha256": request["baseline_sha256"],
        "proposal_sha256": request["proposal_sha256"],
        "signed_pdf_sha256": request["signed_pdf_sha256"],
        "review_bundle_sha256": request["review_bundle_sha256"],
        "approved_mutation_core_sha256": request[
            "approved_mutation_core_sha256"
        ],
        "issued_at": js_time(BASE + timedelta(seconds=2)),
        "expires_at": request["expires_at"],
        "source": snapshot_core()["source"],
        "scope": dict(scope or snapshot_core()["scope"]),
    }
    if phase == "post":
        receipt_payload.update({
            "evidence_bound_job_sha256": request["evidence_bound_job_sha256"],
            "pre_receipt_sha256": request["pre_receipt_sha256"],
            "local_readback_sha256": request["local_readback_sha256"],
            "local_readback_completed_at": request[
                "local_readback_completed_at"
            ],
            "comparison": {
                "source_locator_match": True,
                "data_path_match": True,
                "file_identity_match": True,
                "classifier_match": True,
                "member_record_count_match": True,
                "member_record_multiset_match": True,
                "verified_unchanged": True,
            },
        })
    receipt_hash = client.journal_evidence_sha256(receipt_payload)
    portal_signature = Ed25519PrivateKey.from_private_bytes(PORTAL_SEED).sign(
        client.EVIDENCE_SIGNATURE_DOMAIN + receipt_hash.encode("ascii")
    )
    response = {
        "ok": True,
        "duplicate": duplicate,
        "request_id": request["request_id"],
        "request_payload_sha256": claim["request_payload_sha256"],
        "idempotency_key": claim["request_payload_sha256"],
        "reference": request["reference"],
        "member_number": request["member_number"],
        "phase": phase,
        "status": "manual_review" if manual_review else "completed",
        "expires_at": request["expires_at"],
        "expired": False,
        "receipt": {
            "receipt_sha256": receipt_hash,
            "verdict": verdict,
            "envelope": {
                "payload": receipt_payload,
                "payload_sha256": receipt_hash,
                "key_id": "portal-current-2026",
                "signature": b64url(portal_signature),
            },
        },
    }
    if manual_review:
        response.update({
            "requires_manual_review": True,
            "failure_code": "evidence_not_verified",
        })
    return response


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200, extra_headers=None):
        self.status = status
        self.body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.body)),
            **(extra_headers or {}),
        }

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class PortalSyncJournalEvidenceClientTests(unittest.TestCase):
    def test_feature_is_off_by_default_without_io_or_snapshot(self):
        opener = Mock(side_effect=AssertionError("network must remain unused"))
        builder = Mock(side_effect=AssertionError("journal must remain unread"))
        result = client.process_journal_evidence_queue_if_configured(
            source_root=Path("unused"),
            portal_url=None,
            sync_token=None,
            agent_id=client.EVIDENCE_TARGET_AGENT,
            snapshot_builder=builder,
            environ={},
            opener=opener,
        )
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["enabled"])
        opener.assert_not_called()
        builder.assert_not_called()

    def test_unproven_source_coverage_blocks_before_key_network_or_snapshot(self):
        environ = {
            client.FEATURE_ENV: "true",
            client.AGENT_ID_ENV: client.EVIDENCE_TARGET_AGENT,
        }
        opener = Mock(side_effect=AssertionError("network must remain unused"))
        builder = Mock(side_effect=AssertionError("journal must remain unread"))
        result = client.process_journal_evidence_queue_if_configured(
            source_root=Path("unused"),
            portal_url="https://portal.example",
            sync_token="token",
            agent_id=client.EVIDENCE_TARGET_AGENT,
            snapshot_builder=builder,
            environ=environ,
            opener=opener,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_codes"], ["source_coverage_unproven"])
        opener.assert_not_called()
        builder.assert_not_called()

    def test_feature_requires_explicit_exact_frontdesk_agent_binding(self):
        environ = {
            client.FEATURE_ENV: "true",
            client.SOURCE_COVERAGE_ENV: client.EVIDENCE_CLASSIFIER_VERSION,
            client.AGENT_ID_ENV: client.EVIDENCE_TARGET_AGENT,
        }
        result = client.process_journal_evidence_queue_if_configured(
            source_root=Path("unused"),
            portal_url="https://portal.example",
            sync_token="token",
            agent_id="ron_laptop",
            snapshot_builder=Mock(),
            environ=environ,
        )
        self.assertEqual(result["failure_codes"], ["wrong_agent"])

    def test_claim_hash_binding_fails_before_snapshot(self):
        broken_claim = claim_payload()
        broken_claim["request_payload_sha256"] = "f" * 64
        queue = {
            "ok": True,
            "agent_id": client.EVIDENCE_TARGET_AGENT,
            "requests": [broken_claim],
        }
        opener = SequenceOpener([FakeResponse(queue)])
        builder = Mock()
        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("unused"),
            snapshot_builder=builder,
            opener=opener,
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_codes"], ["claim_request_hash_mismatch"])
        builder.assert_not_called()

    def test_expired_claim_is_rejected_before_snapshot(self):
        expired_claim = claim_payload()
        expired_claim["claim_expires_at"] = js_time(BASE)
        queue = {
            "ok": True,
            "agent_id": client.EVIDENCE_TARGET_AGENT,
            "requests": [expired_claim],
        }
        builder = Mock()
        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("unused"),
            snapshot_builder=builder,
            opener=SequenceOpener([FakeResponse(queue)]),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["failure_codes"], ["claim_expired"])
        builder.assert_not_called()

    def test_oversized_response_is_rejected_before_json_parsing(self):
        response = FakeResponse({"ok": True})
        response.headers["Content-Length"] = str(client.MAX_RESPONSE_BYTES + 1)
        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("unused"),
            snapshot_builder=Mock(),
            opener=SequenceOpener([response, response]),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["failure_codes"], ["response_too_large"])

    def test_agent_signature_is_portal_contract_compatible_fixture(self):
        claim = validated_claim()
        builder = Mock(return_value=snapshot_core())
        envelope = client.build_signed_journal_evidence_envelope(
            config=config(),
            claim=claim,
            source_root=Path("fixture-root"),
            snapshot_builder=builder,
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(
            envelope["payload_sha256"],
            "fcaeb8d02a4706b63704010ba151200fb6f899e6302e0ea8bf947a1121972d0c",
        )
        self.assertEqual(
            envelope["signature"],
            "spVG4IFgCj1jPpeNmQQCdF-wG5B-6efxQYpIAJwNgz-zP8OlAukxCZBTOLQ5Br7DaySQ2LvIvaOswFaxkSLcAg",
        )
        public_key = Ed25519PrivateKey.from_private_bytes(AGENT_SEED).public_key()
        public_key.verify(
            client._base64url_decode(envelope["signature"], "fixture"),
            client.EVIDENCE_SIGNATURE_DOMAIN
            + envelope["payload_sha256"].encode("ascii"),
        )
        builder.assert_called_once_with(
            Path("fixture-root"),
            member_number="42001",
            source_coverage_proven=True,
        )

    def test_agent_fixture_verifies_with_the_portal_contract_implementation(self):
        from cryptography.hazmat.primitives import serialization
        from dreamz_portal import (
            app,
            verify_portal_sync_evidence_signature,
        )

        envelope = client.build_signed_journal_evidence_envelope(
            config=config(),
            claim=validated_claim(),
            source_root=Path("fixture-root"),
            snapshot_builder=lambda *args, **kwargs: snapshot_core(),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        public_bytes = (
            Ed25519PrivateKey.from_private_bytes(AGENT_SEED)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        config_name = "EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_PUBLIC_KEYS_JSON"
        previous = app.config.get(config_name)
        app.config[config_name] = {"frontdesk-current-2026": b64url(public_bytes)}
        try:
            normalized, observed_at = verify_portal_sync_evidence_signature(
                envelope
            )
        finally:
            app.config[config_name] = previous
        self.assertEqual(normalized, envelope)
        self.assertEqual(observed_at, (BASE + timedelta(seconds=1)).replace(tzinfo=None))

    def test_post_phase_is_bound_and_accepts_verified_unchanged_receipt(self):
        claim = validated_claim("post")
        envelope = client.build_signed_journal_evidence_envelope(
            config=config(),
            claim=claim,
            source_root=Path("fixture-root"),
            snapshot_builder=lambda *args, **kwargs: snapshot_core(),
            now_fn=lambda: BASE + timedelta(seconds=4),
        )
        opener = SequenceOpener([
            FakeResponse(
                result_response(claim, envelope["payload_sha256"]),
                status=201,
            )
        ])
        status = client.post_journal_evidence_result(
            config(),
            claim,
            envelope,
            opener=opener,
        )
        self.assertEqual(status, "completed")
        self.assertEqual(envelope["payload"]["phase"], "post")

    def test_incomplete_sanitized_snapshot_is_posted_for_immediate_manual_review(self):
        incomplete = snapshot_core()
        incomplete["scope"] = {
            **incomplete["scope"],
            "complete": False,
            "issue_count": 1,
        }
        raw_claim = claim_payload()
        queue = {
            "ok": True,
            "agent_id": client.EVIDENCE_TARGET_AGENT,
            "requests": [raw_claim],
        }
        class ManualReviewOpener:
            def __init__(self):
                self.calls = []

            def __call__(self, request, timeout):
                self.calls.append(request)
                if request.method == "GET":
                    return FakeResponse(queue)
                envelope = json.loads(request.data.decode("utf-8"))
                return FakeResponse(
                    result_response(
                        raw_claim,
                        envelope["payload_sha256"],
                        manual_review=True,
                        scope=incomplete["scope"],
                    ),
                    status=201,
                )

        opener = ManualReviewOpener()
        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("fixture-root"),
            snapshot_builder=lambda *args, **kwargs: incomplete,
            opener=opener,
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["status"], "manual_review")
        self.assertEqual(result["manual_review"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(opener.calls), 2)

    def test_duplicate_json_keys_in_queue_response_are_rejected(self):
        raw = (
            b'{"ok":true,"ok":true,"agent_id":"frontdesk_dreamz",'
            b'"requests":[]}'
        )

        class RawResponse(FakeResponse):
            def __init__(self):
                self.status = 200
                self.body = raw
                self.headers = {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                }

        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("fixture-root"),
            snapshot_builder=Mock(),
            opener=SequenceOpener([RawResponse()]),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["failure_codes"], ["response_json_invalid"])

    def test_snapshot_failure_does_not_leak_member_data_or_source_path(self):
        raw_claim = claim_payload()
        queue = {
            "ok": True,
            "agent_id": client.EVIDENCE_TARGET_AGENT,
            "requests": [raw_claim],
        }
        opener = SequenceOpener([FakeResponse(queue)])

        def fail_snapshot(*args, **kwargs):
            raise FileNotFoundError(
                r"C:\Gym Assistant 2.6\Data\Journal.jtx for Example Member"
            )

        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("fixture-root"),
            snapshot_builder=fail_snapshot,
            opener=opener,
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        serialized = json.dumps(result)
        self.assertEqual(result["failure_codes"], ["snapshot_unavailable"])
        self.assertNotIn("Journal.jtx", serialized)
        self.assertNotIn("Example Member", serialized)

    def test_config_repr_never_contains_sync_token_or_private_key(self):
        rendered = repr(config())
        self.assertNotIn("test-sync-token", rendered)
        self.assertNotIn(str(AGENT_SEED), rendered)

    def test_jcs_rejects_floats_and_non_interoperable_integers(self):
        with self.assertRaises(client.JournalEvidenceProtocolError):
            client.journal_evidence_jcs({"amount": 1.5})
        with self.assertRaises(client.JournalEvidenceProtocolError):
            client.journal_evidence_jcs({"count": client.MAX_SAFE_INTEGER + 1})

    def test_envelope_contains_no_raw_journal_values_or_paths(self):
        envelope = client.build_signed_journal_evidence_envelope(
            config=config(),
            claim=validated_claim(),
            source_root=Path("fixture-root"),
            snapshot_builder=lambda *args, **kwargs: snapshot_core(),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        serialized = client.journal_evidence_jcs(envelope)
        for forbidden in (
            "Example Member",
            "6500",
            r"C:\Gym Assistant 2.6\Data\Journal.jtx",
            "fixture-root",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_post_retries_the_exact_same_signed_body_and_claim(self):
        claim = validated_claim()
        envelope = client.build_signed_journal_evidence_envelope(
            config=config(),
            claim=claim,
            source_root=Path("fixture-root"),
            snapshot_builder=lambda *args, **kwargs: snapshot_core(),
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        response = result_response(claim, envelope["payload_sha256"], duplicate=True)
        opener = SequenceOpener([
            URLError("response lost after server commit"),
            FakeResponse(response, status=200),
        ])
        status = client.post_journal_evidence_result(
            config(),
            claim,
            envelope,
            opener=opener,
        )
        self.assertEqual(status, "completed")
        self.assertEqual(len(opener.calls), 2)
        first_request, first_timeout = opener.calls[0]
        second_request, second_timeout = opener.calls[1]
        self.assertEqual(first_request.data, second_request.data)
        self.assertEqual(first_timeout, second_timeout)
        self.assertEqual(
            first_request.get_header("X-evidence-claim-id"),
            claim["claim_id"],
        )
        self.assertEqual(
            second_request.get_header("X-evidence-claim-id"),
            claim["claim_id"],
        )

    def test_full_queue_flow_posts_only_sanitized_completed_evidence(self):
        raw_claim = claim_payload()
        queue = {
            "ok": True,
            "agent_id": client.EVIDENCE_TARGET_AGENT,
            "requests": [raw_claim],
        }

        class FlowOpener:
            def __init__(self):
                self.calls = []

            def __call__(self, request, timeout):
                self.calls.append(request)
                if request.method == "GET":
                    return FakeResponse(queue)
                envelope = json.loads(request.data.decode("utf-8"))
                response = result_response(
                    raw_claim,
                    envelope["payload_sha256"],
                )
                return FakeResponse(response, status=201)

        opener = FlowOpener()
        builder = Mock(return_value=snapshot_core())
        result = client.process_journal_evidence_queue(
            config=config(),
            source_root=Path("fixture-root"),
            snapshot_builder=builder,
            opener=opener,
            now_fn=lambda: BASE + timedelta(seconds=1),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 0)
        post_request = opener.calls[1]
        self.assertEqual(post_request.method, "POST")
        self.assertEqual(
            post_request.get_header("X-evidence-claim-id"),
            raw_claim["claim_id"],
        )
        serialized = post_request.data.decode("utf-8")
        self.assertNotIn("fixture-root", serialized)
        self.assertNotIn("Journal.jtx", serialized)

    def test_normal_sync_main_still_scans_and_pushes_when_feature_off(self):
        diff = type("Diff", (), {"added": [], "changed": [], "removed": []})()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "sys.argv",
                [
                    "sync_agent.py",
                    "--source-root",
                    r"C:\Gym Assistant 2.6",
                    "--portal-url",
                    "https://portal.example",
                    "--sync-token",
                    "sync-token",
                    "--push-members",
                ],
            ),
            patch("sync_agent.scan_source", return_value=object()) as scan,
            patch("sync_agent.load_manifest", return_value=None),
            patch("sync_agent.diff_manifest", return_value=diff),
            patch("sync_agent.print_scan_report"),
            patch("sync_agent.get_invoice_monitor_member_ids", return_value=set()),
            patch("sync_agent.build_sync_payload", return_value={"members": []}),
            patch("sync_agent.post_json", return_value={"status": "success"}) as post,
            patch(
                "portal_sync_journal_evidence.urlrequest.urlopen",
                side_effect=AssertionError("evidence network must remain unused"),
            ),
            patch("builtins.print"),
        ):
            sync_agent.main()
        scan.assert_called_once()
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
