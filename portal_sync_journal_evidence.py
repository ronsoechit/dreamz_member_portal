from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
from typing import Callable, Mapping
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:  # pragma: no cover - exercised by an explicit dependency gate.
    serialization = None
    Ed25519PrivateKey = None


EVIDENCE_REQUEST_SCHEMA = "dreamz.existing-member-journal-evidence.request.v1"
EVIDENCE_AGENT_SCHEMA = "dreamz.portal-sync.member-journal-evidence.v1"
EVIDENCE_CORE_SCHEMA = "dreamz.portal-sync.member-journal-evidence-core.v1"
EVIDENCE_RECEIPT_SCHEMA = "dreamz.portal.member-journal-evidence-receipt.v1"
EVIDENCE_SIGNATURE_DOMAIN = b"dreamz.member-journal-evidence.v1\x00"
EVIDENCE_CLASSIFIER_VERSION = "dreamz.ga.journal.member-lines.v1"
EVIDENCE_SOURCE_KIND = "gym_assistant_live_journal"
EVIDENCE_TARGET_AGENT = "frontdesk_dreamz"
EVIDENCE_SOURCE_FLOW = "existing_member_reverification"
EVIDENCE_OPERATION = "update_existing_documents"

FEATURE_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"
SOURCE_COVERAGE_ENV = (
    "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE"
)
AGENT_ID_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_ID"
KEY_ID_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_KEY_ID"
PRIVATE_KEY_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_PRIVATE_KEY"
TIMEOUT_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_TIMEOUT_SECONDS"
LIMIT_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_LIMIT"
RETRIES_ENV = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_RETRIES"

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_RESPONSE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 32 * 1024
MAX_REQUEST_SECONDS = 600
MAX_CLOCK_SKEW_SECONDS = 60

_TIMESTAMP_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)
_OPAQUE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~-]{15,127}\Z")
_KEY_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SHA256_RE = re.compile(r"\A[a-f0-9]{64}\Z")
_REFERENCE_RE = re.compile(r"\ADF-\d{8}-\d{4,6}\Z")
_MEMBER_NUMBER_RE = re.compile(r"\A\d{1,12}\Z")

_REQUEST_HASH_FIELDS = frozenset({
    "baseline_sha256",
    "proposal_sha256",
    "signed_pdf_sha256",
    "review_bundle_sha256",
    "approved_mutation_core_sha256",
})
_PRE_REQUEST_FIELDS = frozenset({
    "schema",
    "request_id",
    "phase",
    "nonce",
    "requested_at",
    "expires_at",
    "reference",
    "source_flow",
    "member_number",
    "operation",
    *_REQUEST_HASH_FIELDS,
})
_POST_REQUEST_FIELDS = frozenset({
    *_PRE_REQUEST_FIELDS,
    "evidence_bound_job_sha256",
    "pre_receipt_sha256",
    "local_readback_sha256",
    "local_readback_completed_at",
})
_SOURCE_FIELDS = frozenset({
    "kind",
    "locator_fingerprint_sha256",
    "data_path_fingerprint_sha256",
    "file_identity_sha256",
    "byte_length",
    "source_sha256",
    "stable_read_count",
})
_SCOPE_FIELDS = frozenset({
    "classifier_version",
    "complete",
    "member_record_count",
    "member_record_multiset_sha256",
    "issue_count",
})
_AGENT_PAYLOAD_FIELDS = frozenset({
    "schema",
    "request_id",
    "request_payload_sha256",
    "phase",
    "reference",
    "member_number",
    "agent_id",
    "observed_at",
    "source",
    "scope",
})
_AGENT_ENVELOPE_FIELDS = frozenset({
    "payload",
    "payload_sha256",
    "key_id",
    "signature",
})
_CLAIM_FIELDS = frozenset({
    "request_id",
    "request_payload_sha256",
    "request",
    "claim_id",
    "claim_expires_at",
})
_QUEUE_RESPONSE_FIELDS = frozenset({"ok", "agent_id", "requests"})
_RESULT_BASE_FIELDS = frozenset({
    "ok",
    "duplicate",
    "request_id",
    "request_payload_sha256",
    "idempotency_key",
    "reference",
    "member_number",
    "phase",
    "status",
    "expires_at",
    "expired",
    "receipt",
})
_RESULT_MANUAL_FIELDS = frozenset({"requires_manual_review", "failure_code"})
_RECEIPT_PRE_FIELDS = frozenset({
    "schema",
    "receipt_id",
    "request_id",
    "request_payload_sha256",
    "agent_result_payload_sha256",
    "phase",
    "verdict",
    "reference",
    "source_flow",
    "member_number",
    "operation",
    *_REQUEST_HASH_FIELDS,
    "issued_at",
    "expires_at",
    "source",
    "scope",
})
_RECEIPT_POST_FIELDS = frozenset({
    *_RECEIPT_PRE_FIELDS,
    "evidence_bound_job_sha256",
    "pre_receipt_sha256",
    "local_readback_sha256",
    "local_readback_completed_at",
    "comparison",
})
_COMPARISON_FIELDS = frozenset({
    "source_locator_match",
    "data_path_match",
    "file_identity_match",
    "classifier_match",
    "member_record_count_match",
    "member_record_multiset_match",
    "verified_unchanged",
})


class JournalEvidenceClientError(RuntimeError):
    """A deliberately sanitized protocol or transport failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class JournalEvidenceConfigError(JournalEvidenceClientError):
    pass


class JournalEvidenceProtocolError(JournalEvidenceClientError):
    pass


class JournalEvidenceTransportError(JournalEvidenceClientError):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.retryable = retryable


@dataclass(frozen=True)
class JournalEvidenceClientConfig:
    portal_url: str
    sync_token: str = field(repr=False)
    agent_id: str = EVIDENCE_TARGET_AGENT
    key_id: str = ""
    private_key: object = field(default=None, repr=False)
    timeout_seconds: int = 15
    limit: int = 1
    retries: int = 1


def _exact_fields(value: object, expected: frozenset[str], code: str) -> dict:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise JournalEvidenceProtocolError(code)
    return value


def _valid_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise JournalEvidenceProtocolError(code)
    return value


def _valid_nonnegative_int(value: object, code: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or value > MAX_SAFE_INTEGER
    ):
        raise JournalEvidenceProtocolError(code)
    return value


def _validate_jcs_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise JournalEvidenceProtocolError("invalid_canonical_json")


def _jcs_sort_key(value: str) -> bytes:
    _validate_jcs_string(value)
    return value.encode("utf-16-be")


def journal_evidence_jcs(value: object) -> str:
    """RFC 8785 canonical JSON for this protocol's integer-only model."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_SAFE_INTEGER:
            raise JournalEvidenceProtocolError("invalid_canonical_json")
        return str(value)
    if isinstance(value, float):
        raise JournalEvidenceProtocolError("invalid_canonical_json")
    if isinstance(value, str):
        _validate_jcs_string(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(journal_evidence_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise JournalEvidenceProtocolError("invalid_canonical_json")
        return "{" + ",".join(
            journal_evidence_jcs(key) + ":" + journal_evidence_jcs(value[key])
            for key in sorted(value, key=_jcs_sort_key)
        ) + "}"
    raise JournalEvidenceProtocolError("invalid_canonical_json")


def journal_evidence_sha256(value: object) -> str:
    return hashlib.sha256(journal_evidence_jcs(value).encode("utf-8")).hexdigest()


def _base64url_decode(value: object, code: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or "=" in value
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise JournalEvidenceProtocolError(code)
    try:
        return base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError, binascii.Error) as exc:
        raise JournalEvidenceProtocolError(code) from exc


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _parse_timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise JournalEvidenceProtocolError(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise JournalEvidenceProtocolError(code) from exc
    return parsed.astimezone(timezone.utc)


def _utc_now_text(now: datetime) -> str:
    if not isinstance(now, datetime):
        raise JournalEvidenceProtocolError("invalid_local_clock")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    milliseconds = now.microsecond // 1000
    return now.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def _load_private_key(value: str):
    if Ed25519PrivateKey is None or serialization is None:
        raise JournalEvidenceConfigError("crypto_unavailable")
    if not isinstance(value, str) or not value.strip():
        raise JournalEvidenceConfigError("private_key_missing")
    encoded = value.strip()
    try:
        if encoded.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(
                encoded.encode("ascii"),
                password=None,
            )
        else:
            raw = _base64url_decode(encoded, "private_key_invalid")
            if len(raw) != 32:
                raise ValueError("wrong private-key length")
            key = Ed25519PrivateKey.from_private_bytes(raw)
    except JournalEvidenceProtocolError as exc:
        raise JournalEvidenceConfigError("private_key_invalid") from exc
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise JournalEvidenceConfigError("private_key_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise JournalEvidenceConfigError("private_key_not_ed25519")
    return key


def _configured_int(
    environ: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(environ.get(name, str(default))).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise JournalEvidenceConfigError("numeric_config_invalid") from exc
    if not minimum <= value <= maximum:
        raise JournalEvidenceConfigError("numeric_config_out_of_range")
    return value


def _normalize_portal_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JournalEvidenceConfigError("portal_url_missing")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise JournalEvidenceConfigError("portal_url_not_secure")
    return value.strip().rstrip("/")


def load_journal_evidence_client_config(
    *,
    portal_url: str | None,
    sync_token: str | None,
    agent_id: str | None,
    environ: Mapping[str, str] | None = None,
) -> JournalEvidenceClientConfig | None:
    environ = os.environ if environ is None else environ
    if environ.get(FEATURE_ENV, "") != "true":
        return None
    if environ.get(SOURCE_COVERAGE_ENV, "") != EVIDENCE_CLASSIFIER_VERSION:
        raise JournalEvidenceConfigError("source_coverage_unproven")
    configured_agent = environ.get(AGENT_ID_ENV, "")
    if (
        configured_agent != EVIDENCE_TARGET_AGENT
        or agent_id != EVIDENCE_TARGET_AGENT
    ):
        raise JournalEvidenceConfigError("wrong_agent")
    key_id = environ.get(KEY_ID_ENV, "")
    if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
        raise JournalEvidenceConfigError("key_id_invalid")
    if not isinstance(sync_token, str) or not sync_token:
        raise JournalEvidenceConfigError("sync_token_missing")
    return JournalEvidenceClientConfig(
        portal_url=_normalize_portal_url(portal_url or ""),
        sync_token=sync_token,
        agent_id=EVIDENCE_TARGET_AGENT,
        key_id=key_id,
        private_key=_load_private_key(environ.get(PRIVATE_KEY_ENV, "")),
        timeout_seconds=_configured_int(environ, TIMEOUT_ENV, 15, 2, 30),
        limit=_configured_int(environ, LIMIT_ENV, 1, 1, 10),
        retries=_configured_int(environ, RETRIES_ENV, 1, 0, 2),
    )


def _read_json_response(response) -> dict:
    content_type = str(response.headers.get("Content-Type", ""))
    if not content_type.lower().startswith("application/json"):
        raise JournalEvidenceTransportError("response_not_json")
    content_length = response.headers.get("Content-Length")
    declared_length = None
    if content_length:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise ValueError("negative content length")
            if declared_length > MAX_RESPONSE_BYTES:
                raise JournalEvidenceTransportError("response_too_large")
        except ValueError as exc:
            raise JournalEvidenceTransportError("content_length_invalid") from exc
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise JournalEvidenceTransportError(
            "network_unavailable",
            retryable=True,
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JournalEvidenceTransportError("response_too_large")
    if declared_length is not None and len(raw) != declared_length:
        raise JournalEvidenceTransportError(
            "response_truncated",
            retryable=True,
        )

    def reject_duplicate_keys(pairs):
        parsed = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate JSON object key")
            parsed[key] = value
        return parsed

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise JournalEvidenceTransportError("response_json_invalid") from exc
    if not isinstance(payload, dict):
        raise JournalEvidenceTransportError("response_contract_invalid")
    return payload


def _json_http(
    *,
    method: str,
    url: str,
    token: str,
    agent_id: str,
    timeout: int,
    retries: int,
    body: bytes | None = None,
    claim_id: str | None = None,
    opener: Callable = urlrequest.urlopen,
) -> dict:
    if body is not None and len(body) > MAX_REQUEST_BYTES:
        raise JournalEvidenceProtocolError("request_too_large")
    headers = {
        "Accept": "application/json",
        "X-Sync-Token": token,
        "X-Sync-Agent": agent_id,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if claim_id is not None:
        headers["X-Evidence-Claim-Id"] = claim_id
    retry_statuses = {500, 502, 503, 504}
    last_error: JournalEvidenceTransportError | None = None
    for attempt in range(retries + 1):
        http_request = urlrequest.Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with opener(http_request, timeout=timeout) as response:
                status_value = getattr(response, "status", None)
                if status_value is None:
                    status_value = response.getcode()
                status = int(status_value)
                if status not in {200, 201}:
                    raise JournalEvidenceTransportError(
                        f"http_{status}",
                        retryable=status in retry_statuses,
                    )
                return _read_json_response(response)
        except HTTPError as exc:
            last_error = JournalEvidenceTransportError(
                f"http_{exc.code}",
                retryable=exc.code in retry_statuses,
            )
        except (URLError, TimeoutError, socket.timeout, OSError):
            last_error = JournalEvidenceTransportError(
                "network_unavailable",
                retryable=True,
            )
        except JournalEvidenceTransportError as exc:
            last_error = exc
        if not last_error.retryable or attempt >= retries:
            raise last_error
    raise last_error or JournalEvidenceTransportError("network_unavailable")


def _validate_request_payload(payload: object, now: datetime) -> dict:
    if not isinstance(payload, dict):
        raise JournalEvidenceProtocolError("request_contract_invalid")
    phase = payload.get("phase")
    expected = _PRE_REQUEST_FIELDS if phase == "pre" else _POST_REQUEST_FIELDS
    if phase not in {"pre", "post"} or frozenset(payload) != expected:
        raise JournalEvidenceProtocolError("request_contract_invalid")
    if payload["schema"] != EVIDENCE_REQUEST_SCHEMA:
        raise JournalEvidenceProtocolError("request_schema_invalid")
    if not isinstance(payload["request_id"], str) or not _OPAQUE_RE.fullmatch(
        payload["request_id"]
    ):
        raise JournalEvidenceProtocolError("request_id_invalid")
    if not isinstance(payload["nonce"], str) or not _OPAQUE_RE.fullmatch(
        payload["nonce"]
    ):
        raise JournalEvidenceProtocolError("request_nonce_invalid")
    if not isinstance(payload["reference"], str) or not _REFERENCE_RE.fullmatch(
        payload["reference"]
    ):
        raise JournalEvidenceProtocolError("request_reference_invalid")
    member_number = payload["member_number"]
    if (
        not isinstance(member_number, str)
        or not _MEMBER_NUMBER_RE.fullmatch(member_number)
        or int(member_number) <= 0
    ):
        raise JournalEvidenceProtocolError("request_member_invalid")
    if payload["source_flow"] != EVIDENCE_SOURCE_FLOW:
        raise JournalEvidenceProtocolError("request_source_flow_invalid")
    if payload["operation"] != EVIDENCE_OPERATION:
        raise JournalEvidenceProtocolError("request_operation_invalid")
    for field_name in _REQUEST_HASH_FIELDS:
        _valid_sha256(payload[field_name], "request_hash_invalid")
    requested_at = _parse_timestamp(payload["requested_at"], "request_time_invalid")
    expires_at = _parse_timestamp(payload["expires_at"], "request_time_invalid")
    if requested_at > now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise JournalEvidenceProtocolError("request_time_invalid")
    lifetime = (expires_at - requested_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_REQUEST_SECONDS or expires_at <= now:
        raise JournalEvidenceProtocolError("request_expired")
    if phase == "post":
        for field_name in (
            "evidence_bound_job_sha256",
            "pre_receipt_sha256",
            "local_readback_sha256",
        ):
            _valid_sha256(payload[field_name], "request_hash_invalid")
        local_readback_at = _parse_timestamp(
            payload["local_readback_completed_at"],
            "request_time_invalid",
        )
        if local_readback_at > requested_at:
            raise JournalEvidenceProtocolError("request_time_invalid")
    return dict(payload)


def _validate_claim(value: object, now: datetime) -> dict:
    claim = _exact_fields(value, _CLAIM_FIELDS, "claim_contract_invalid")
    request_payload = _validate_request_payload(claim["request"], now)
    request_id = claim["request_id"]
    claim_id = claim["claim_id"]
    if request_id != request_payload["request_id"]:
        raise JournalEvidenceProtocolError("claim_request_binding_mismatch")
    if not isinstance(claim_id, str) or not _OPAQUE_RE.fullmatch(claim_id):
        raise JournalEvidenceProtocolError("claim_id_invalid")
    request_hash = _valid_sha256(
        claim["request_payload_sha256"],
        "claim_request_hash_invalid",
    )
    if request_hash != journal_evidence_sha256(request_payload):
        raise JournalEvidenceProtocolError("claim_request_hash_mismatch")
    claim_expires_at = _parse_timestamp(
        claim["claim_expires_at"],
        "claim_time_invalid",
    )
    request_expires_at = _parse_timestamp(
        request_payload["expires_at"],
        "request_time_invalid",
    )
    if claim_expires_at <= now or claim_expires_at > request_expires_at:
        raise JournalEvidenceProtocolError("claim_expired")
    return {
        **claim,
        "request": request_payload,
        "claim_expires_at_parsed": claim_expires_at,
    }


def get_journal_evidence_claims(
    config: JournalEvidenceClientConfig,
    *,
    opener: Callable = urlrequest.urlopen,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[dict]:
    endpoint = (
        config.portal_url
        + "/api/sync/existing-member-journal-evidence/requests"
        + f"?agent_id={quote(config.agent_id)}&limit={config.limit}"
    )
    payload = _json_http(
        method="GET",
        url=endpoint,
        token=config.sync_token,
        agent_id=config.agent_id,
        timeout=config.timeout_seconds,
        retries=config.retries,
        opener=opener,
    )
    _exact_fields(payload, _QUEUE_RESPONSE_FIELDS, "queue_contract_invalid")
    if payload["ok"] is not True or payload["agent_id"] != config.agent_id:
        raise JournalEvidenceProtocolError("queue_agent_binding_mismatch")
    claims = payload["requests"]
    if not isinstance(claims, list) or len(claims) > config.limit:
        raise JournalEvidenceProtocolError("queue_contract_invalid")
    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return [_validate_claim(claim, now.astimezone(timezone.utc)) for claim in claims]


def _validate_source(value: object) -> dict:
    source = _exact_fields(value, _SOURCE_FIELDS, "source_contract_invalid")
    if source["kind"] != EVIDENCE_SOURCE_KIND:
        raise JournalEvidenceProtocolError("source_kind_invalid")
    for field_name in (
        "locator_fingerprint_sha256",
        "data_path_fingerprint_sha256",
        "file_identity_sha256",
        "source_sha256",
    ):
        _valid_sha256(source[field_name], "source_hash_invalid")
    _valid_nonnegative_int(source["byte_length"], "source_size_invalid")
    if source["stable_read_count"] != 2:
        raise JournalEvidenceProtocolError("source_unstable")
    return dict(source)


def _validate_scope(value: object) -> dict:
    scope = _exact_fields(value, _SCOPE_FIELDS, "scope_contract_invalid")
    if scope["classifier_version"] != EVIDENCE_CLASSIFIER_VERSION:
        raise JournalEvidenceProtocolError("classifier_mismatch")
    if not isinstance(scope["complete"], bool):
        raise JournalEvidenceProtocolError("scope_complete_invalid")
    _valid_nonnegative_int(scope["member_record_count"], "scope_count_invalid")
    _valid_sha256(
        scope["member_record_multiset_sha256"],
        "scope_hash_invalid",
    )
    issue_count = _valid_nonnegative_int(
        scope["issue_count"],
        "scope_issue_count_invalid",
    )
    if scope["complete"] != (issue_count == 0):
        raise JournalEvidenceProtocolError("scope_completeness_inconsistent")
    return dict(scope)


def build_signed_journal_evidence_envelope(
    *,
    config: JournalEvidenceClientConfig,
    claim: dict,
    source_root: Path,
    snapshot_builder: Callable,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    request_payload = claim["request"]
    core = snapshot_builder(
        Path(source_root),
        member_number=request_payload["member_number"],
        source_coverage_proven=True,
    )
    if not isinstance(core, dict) or frozenset(core) != {
        "schema",
        "member_number",
        "observed_at",
        "source",
        "scope",
    }:
        raise JournalEvidenceProtocolError("snapshot_contract_invalid")
    if (
        core["schema"] != EVIDENCE_CORE_SCHEMA
        or str(core["member_number"]) != request_payload["member_number"]
    ):
        raise JournalEvidenceProtocolError("snapshot_binding_mismatch")
    source = _validate_source(core["source"])
    scope = _validate_scope(core["scope"])
    now = now_fn()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now >= claim["claim_expires_at_parsed"]:
        raise JournalEvidenceProtocolError("claim_expired_after_snapshot")
    observed_at = _utc_now_text(now)
    request_expires_at = _parse_timestamp(
        request_payload["expires_at"],
        "request_time_invalid",
    )
    if now > request_expires_at:
        raise JournalEvidenceProtocolError("request_expired_after_snapshot")
    payload = {
        "schema": EVIDENCE_AGENT_SCHEMA,
        "request_id": request_payload["request_id"],
        "request_payload_sha256": claim["request_payload_sha256"],
        "phase": request_payload["phase"],
        "reference": request_payload["reference"],
        "member_number": request_payload["member_number"],
        "agent_id": config.agent_id,
        "observed_at": observed_at,
        "source": source,
        "scope": scope,
    }
    if frozenset(payload) != _AGENT_PAYLOAD_FIELDS:
        raise JournalEvidenceProtocolError("agent_payload_contract_invalid")
    payload_sha256 = journal_evidence_sha256(payload)
    try:
        signature = config.private_key.sign(
            EVIDENCE_SIGNATURE_DOMAIN + payload_sha256.encode("ascii")
        )
    except Exception as exc:
        raise JournalEvidenceConfigError("signing_failed") from exc
    if len(signature) != 64:
        raise JournalEvidenceConfigError("signature_length_invalid")
    envelope = {
        "payload": payload,
        "payload_sha256": payload_sha256,
        "key_id": config.key_id,
        "signature": _base64url_encode(signature),
    }
    if frozenset(envelope) != _AGENT_ENVELOPE_FIELDS:
        raise JournalEvidenceProtocolError("agent_envelope_contract_invalid")
    return envelope


def _validate_receipt(
    value: object,
    claim: dict,
    agent_result_payload_sha256: str,
) -> None:
    receipt = _exact_fields(
        value,
        frozenset({"receipt_sha256", "verdict", "envelope"}),
        "receipt_contract_invalid",
    )
    _valid_sha256(receipt["receipt_sha256"], "receipt_hash_invalid")
    request_payload = claim["request"]
    phase = request_payload["phase"]
    allowed_verdict = "pre_verified" if phase == "pre" else "verified_unchanged"
    if receipt["verdict"] not in {allowed_verdict, "manual_review"}:
        raise JournalEvidenceProtocolError("receipt_verdict_invalid")
    envelope = _exact_fields(
        receipt["envelope"],
        _AGENT_ENVELOPE_FIELDS,
        "receipt_envelope_invalid",
    )
    payload_sha256 = _valid_sha256(
        envelope["payload_sha256"],
        "receipt_envelope_invalid",
    )
    if payload_sha256 != journal_evidence_sha256(envelope["payload"]):
        raise JournalEvidenceProtocolError("receipt_envelope_invalid")
    if receipt["receipt_sha256"] != payload_sha256:
        raise JournalEvidenceProtocolError("receipt_envelope_invalid")
    if not isinstance(envelope["key_id"], str) or not _KEY_ID_RE.fullmatch(
        envelope["key_id"]
    ):
        raise JournalEvidenceProtocolError("receipt_envelope_invalid")
    if len(_base64url_decode(envelope["signature"], "receipt_envelope_invalid")) != 64:
        raise JournalEvidenceProtocolError("receipt_envelope_invalid")

    receipt_payload = envelope["payload"]
    expected_fields = (
        _RECEIPT_PRE_FIELDS if phase == "pre" else _RECEIPT_POST_FIELDS
    )
    _exact_fields(
        receipt_payload,
        expected_fields,
        "receipt_payload_invalid",
    )
    if (
        receipt_payload["schema"] != EVIDENCE_RECEIPT_SCHEMA
        or not isinstance(receipt_payload["receipt_id"], str)
        or not _OPAQUE_RE.fullmatch(receipt_payload["receipt_id"])
        or receipt_payload["request_id"] != request_payload["request_id"]
        or receipt_payload["request_payload_sha256"]
        != claim["request_payload_sha256"]
        or receipt_payload["agent_result_payload_sha256"]
        != agent_result_payload_sha256
        or receipt_payload["phase"] != phase
        or receipt_payload["verdict"] != receipt["verdict"]
        or receipt_payload["reference"] != request_payload["reference"]
        or receipt_payload["source_flow"] != request_payload["source_flow"]
        or str(receipt_payload["member_number"])
        != request_payload["member_number"]
        or receipt_payload["operation"] != request_payload["operation"]
        or receipt_payload["expires_at"] != request_payload["expires_at"]
    ):
        raise JournalEvidenceProtocolError("receipt_binding_mismatch")
    _parse_timestamp(receipt_payload["issued_at"], "receipt_time_invalid")
    for field_name in _REQUEST_HASH_FIELDS:
        if receipt_payload[field_name] != request_payload[field_name]:
            raise JournalEvidenceProtocolError("receipt_binding_mismatch")
    _validate_source(receipt_payload["source"])
    _validate_scope(receipt_payload["scope"])
    if phase == "post":
        for field_name in (
            "evidence_bound_job_sha256",
            "pre_receipt_sha256",
            "local_readback_sha256",
            "local_readback_completed_at",
        ):
            if receipt_payload[field_name] != request_payload[field_name]:
                raise JournalEvidenceProtocolError("receipt_binding_mismatch")
        comparison = _exact_fields(
            receipt_payload["comparison"],
            _COMPARISON_FIELDS,
            "receipt_comparison_invalid",
        )
        if not all(isinstance(value, bool) for value in comparison.values()):
            raise JournalEvidenceProtocolError("receipt_comparison_invalid")


def _validate_result_response(
    value: object,
    claim: dict,
    agent_result_payload_sha256: str,
) -> str:
    if not isinstance(value, dict):
        raise JournalEvidenceProtocolError("result_contract_invalid")
    keys = frozenset(value)
    if keys not in {_RESULT_BASE_FIELDS, _RESULT_BASE_FIELDS | _RESULT_MANUAL_FIELDS}:
        raise JournalEvidenceProtocolError("result_contract_invalid")
    request_payload = claim["request"]
    if (
        value["ok"] is not True
        or not isinstance(value["duplicate"], bool)
        or value["request_id"] != request_payload["request_id"]
        or value["request_payload_sha256"] != claim["request_payload_sha256"]
        or value["idempotency_key"] != claim["request_payload_sha256"]
        or value["reference"] != request_payload["reference"]
        or str(value["member_number"]) != request_payload["member_number"]
        or value["phase"] != request_payload["phase"]
        or value["expires_at"] != request_payload["expires_at"]
        or not isinstance(value["expired"], bool)
    ):
        raise JournalEvidenceProtocolError("result_binding_mismatch")
    if value["status"] not in {"completed", "manual_review"}:
        raise JournalEvidenceProtocolError("result_status_invalid")
    _validate_receipt(
        value["receipt"],
        claim,
        agent_result_payload_sha256,
    )
    if value["status"] == "manual_review":
        if keys != _RESULT_BASE_FIELDS | _RESULT_MANUAL_FIELDS:
            raise JournalEvidenceProtocolError("result_contract_invalid")
        if value["requires_manual_review"] is not True or not isinstance(
            value["failure_code"], str
        ):
            raise JournalEvidenceProtocolError("result_contract_invalid")
    elif keys != _RESULT_BASE_FIELDS:
        raise JournalEvidenceProtocolError("result_contract_invalid")
    return value["status"]


def post_journal_evidence_result(
    config: JournalEvidenceClientConfig,
    claim: dict,
    envelope: dict,
    *,
    opener: Callable = urlrequest.urlopen,
) -> str:
    request_id = claim["request_id"]
    endpoint = (
        config.portal_url
        + "/api/sync/existing-member-journal-evidence/requests/"
        + quote(request_id, safe="")
        + f"/result?agent_id={quote(config.agent_id)}"
    )
    body = journal_evidence_jcs(envelope).encode("utf-8")
    result = _json_http(
        method="POST",
        url=endpoint,
        token=config.sync_token,
        agent_id=config.agent_id,
        timeout=config.timeout_seconds,
        retries=config.retries,
        body=body,
        claim_id=claim["claim_id"],
        opener=opener,
    )
    return _validate_result_response(
        result,
        claim,
        envelope["payload_sha256"],
    )


def process_journal_evidence_queue(
    *,
    config: JournalEvidenceClientConfig,
    source_root: Path,
    snapshot_builder: Callable,
    opener: Callable = urlrequest.urlopen,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    summary = {
        "enabled": True,
        "status": "idle",
        "claimed": 0,
        "completed": 0,
        "manual_review": 0,
        "failed": 0,
        "failure_codes": [],
    }
    try:
        claims = get_journal_evidence_claims(
            config,
            opener=opener,
            now_fn=now_fn,
        )
    except JournalEvidenceClientError as exc:
        summary["status"] = "blocked"
        summary["failed"] = 1
        summary["failure_codes"].append(exc.code)
        return summary
    except Exception:
        summary["status"] = "blocked"
        summary["failed"] = 1
        summary["failure_codes"].append("queue_unavailable")
        return summary
    summary["claimed"] = len(claims)
    for claim in claims:
        try:
            envelope = build_signed_journal_evidence_envelope(
                config=config,
                claim=claim,
                source_root=source_root,
                snapshot_builder=snapshot_builder,
                now_fn=now_fn,
            )
            status = post_journal_evidence_result(
                config,
                claim,
                envelope,
                opener=opener,
            )
            summary[status] += 1
        except JournalEvidenceClientError as exc:
            summary["failed"] += 1
            summary["failure_codes"].append(exc.code)
        except Exception:
            # Do not serialize exception details: source paths and member data
            # may be present in filesystem/parser failures.
            summary["failed"] += 1
            summary["failure_codes"].append("snapshot_unavailable")
    if summary["failed"]:
        summary["status"] = "blocked"
    elif summary["manual_review"]:
        summary["status"] = "manual_review"
    elif summary["completed"]:
        summary["status"] = "completed"
    return summary


def process_journal_evidence_queue_if_configured(
    *,
    source_root: Path,
    portal_url: str | None,
    sync_token: str | None,
    agent_id: str | None,
    snapshot_builder: Callable,
    environ: Mapping[str, str] | None = None,
    opener: Callable = urlrequest.urlopen,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict:
    try:
        config = load_journal_evidence_client_config(
            portal_url=portal_url,
            sync_token=sync_token,
            agent_id=agent_id,
            environ=environ,
        )
    except JournalEvidenceClientError as exc:
        return {
            "enabled": True,
            "status": "blocked",
            "claimed": 0,
            "completed": 0,
            "manual_review": 0,
            "failed": 1,
            "failure_codes": [exc.code],
        }
    except Exception:
        return {
            "enabled": True,
            "status": "blocked",
            "claimed": 0,
            "completed": 0,
            "manual_review": 0,
            "failed": 1,
            "failure_codes": ["configuration_unavailable"],
        }
    if config is None:
        return {
            "enabled": False,
            "status": "disabled",
            "claimed": 0,
            "completed": 0,
            "manual_review": 0,
            "failed": 0,
            "failure_codes": [],
        }
    return process_journal_evidence_queue(
        config=config,
        source_root=source_root,
        snapshot_builder=snapshot_builder,
        opener=opener,
        now_fn=now_fn,
    )
