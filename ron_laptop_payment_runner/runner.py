from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import ntpath
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
import uuid

import gymassistant_payment_writer as payment_writer


PACKAGE_VERSION = "1.0.0"
AGENT_ID = "ron_laptop"
AGENT_LABEL = "Ron laptop"
SOURCE_ROOT = Path("Z:\\")
EXPECTED_DATA_ROOT = r"Z:\Data"
PORTAL_URL = "https://dreamzmemberportal-production.up.railway.app"
MUTEX_NAME = r"Local\DreamzRonLaptopPaymentRunner"

DEFAULT_POLL_SECONDS = 5
DEFAULT_IDLE_SECONDS = 2
DEFAULT_COMMAND_TTL_SECONDS = 15 * 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_WRITER_TIMEOUT_SECONDS = 30
MAX_COMMAND_LIMIT = 500
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024

COMMAND_ENDPOINT = "/api/sync/fep-payment-process-commands"
UPDATE_ENDPOINT = "/api/sync/fep-payment-updates"

ERROR_ALREADY_EXISTS = 183
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    _kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.CloseDesktop.restype = wintypes.BOOL
    _user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetUserObjectInformationW.restype = wintypes.BOOL
    _user32.GetForegroundWindow.restype = wintypes.HWND
else:
    _kernel32 = None
    _user32 = None


class RunnerError(RuntimeError):
    """Base error whose message is safe to use as an internal category."""


class PortalApiError(RunnerError):
    pass


class AcknowledgementPending(RunnerError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    poll_seconds: int = DEFAULT_POLL_SECONDS
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    command_ttl_seconds: int = DEFAULT_COMMAND_TTL_SECONDS
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    writer_timeout_seconds: int = DEFAULT_WRITER_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str


@dataclass
class CommandSummary:
    received: int = 0
    applied: int = 0
    failed: int = 0
    deferred: int = 0

    def public(self) -> dict[str, int]:
        return {
            "received": int(self.received),
            "applied": int(self.applied),
            "failed": int(self.failed),
            "deferred": int(self.deferred),
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def load_runtime_config(path: Path) -> RuntimeConfig:
    if not path.is_file():
        return RuntimeConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError("invalid_runtime_config") from exc
    if not isinstance(raw, dict):
        raise RunnerError("invalid_runtime_config")
    forbidden = {"agent_id", "source_root", "portal_url", "sync_token", "token"}
    if forbidden.intersection(raw):
        raise RunnerError("runtime_config_contains_fixed_or_secret_field")
    return RuntimeConfig(
        poll_seconds=_bounded_int(
            raw.get("poll_seconds"),
            default=DEFAULT_POLL_SECONDS,
            minimum=2,
            maximum=60,
        ),
        idle_seconds=_bounded_int(
            raw.get("idle_seconds"),
            default=DEFAULT_IDLE_SECONDS,
            minimum=1,
            maximum=300,
        ),
        command_ttl_seconds=_bounded_int(
            raw.get("command_ttl_seconds"),
            default=DEFAULT_COMMAND_TTL_SECONDS,
            minimum=60,
            maximum=3600,
        ),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"),
            default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
            minimum=5,
            maximum=60,
        ),
        writer_timeout_seconds=_bounded_int(
            raw.get("writer_timeout_seconds"),
            default=DEFAULT_WRITER_TIMEOUT_SECONDS,
            minimum=10,
            maximum=120,
        ),
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class StatusStore:
    def __init__(self, path: Path):
        self.path = path

    def update(
        self,
        state: str,
        *,
        reason: str | None = None,
        summary: CommandSummary | None = None,
        receipt_count: int = 0,
    ) -> None:
        payload: dict[str, object] = {
            "schema": 1,
            "version": PACKAGE_VERSION,
            "agent_id": AGENT_ID,
            "source_root": str(SOURCE_ROOT),
            "state": state,
            "updated_at": utc_iso(),
            "pid": os.getpid(),
            "pending_local_receipts": max(0, int(receipt_count)),
        }
        if reason:
            payload["reason"] = reason
        if summary:
            payload["last_summary"] = summary.public()
        atomic_write_json(self.path, payload)


class ReceiptStore:
    """Small local crash-safety ledger with no member or payment details."""

    def __init__(self, path: Path):
        self.path = path
        self._records: dict[str, dict[str, str]] = {}
        self._load()

    @staticmethod
    def _key(update_id: int) -> str:
        return str(int(update_id))

    @staticmethod
    def _digest(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._records = {}
            return
        records = raw.get("receipts") if isinstance(raw, dict) else None
        if not isinstance(records, dict):
            return
        for update_id, record in records.items():
            if not str(update_id).isdigit() or not isinstance(record, dict):
                continue
            digest = str(record.get("idempotency_sha256") or "")
            applied_at = str(record.get("applied_at") or "")
            if re.fullmatch(r"[0-9a-f]{64}", digest) and applied_at:
                self._records[str(update_id)] = {
                    "idempotency_sha256": digest,
                    "applied_at": applied_at,
                }

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema": 1,
                "receipts": self._records,
            },
        )

    def count(self) -> int:
        return len(self._records)

    def matches(self, update_id: int, idempotency_key: str) -> bool:
        record = self._records.get(self._key(update_id))
        return bool(
            record
            and record.get("idempotency_sha256") == self._digest(idempotency_key)
        )

    def has_conflict(self, update_id: int, idempotency_key: str) -> bool:
        record = self._records.get(self._key(update_id))
        return bool(
            record
            and record.get("idempotency_sha256") != self._digest(idempotency_key)
        )

    def record_applied(self, update_id: int, idempotency_key: str) -> None:
        self._records[self._key(update_id)] = {
            "idempotency_sha256": self._digest(idempotency_key),
            "applied_at": utc_iso(),
        }
        self._save()

    def acknowledge(self, update_id: int) -> None:
        if self._records.pop(self._key(update_id), None) is not None:
            self._save()


class NamedMutex:
    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            raise RunnerError("windows_required")
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise RunnerError("mutex_create_failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise RunnerError("runner_already_active")
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


def current_input_desktop_name() -> str | None:
    if os.name != "nt":
        return None
    desktop = _user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not desktop:
        return None
    try:
        required = wintypes.DWORD(0)
        _user32.GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0 or required.value > 4096:
            return None
        buffer = ctypes.create_unicode_buffer((required.value // ctypes.sizeof(ctypes.c_wchar)) + 1)
        if not _user32.GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(required),
        ):
            return None
        return buffer.value
    finally:
        _user32.CloseDesktop(desktop)


def desktop_availability() -> Readiness:
    if os.name != "nt":
        return Readiness(False, "windows_required")

    process_session = wintypes.DWORD(0)
    if not _kernel32.ProcessIdToSessionId(
        _kernel32.GetCurrentProcessId(),
        ctypes.byref(process_session),
    ):
        return Readiness(False, "session_unavailable")
    active_session = int(_kernel32.WTSGetActiveConsoleSessionId())
    if int(process_session.value) != active_session:
        return Readiness(False, "session_not_active")

    desktop_name = current_input_desktop_name()
    if not desktop_name:
        return Readiness(False, "desktop_unavailable")
    if desktop_name.casefold() != "default":
        return Readiness(False, "desktop_locked")
    if not _user32.GetForegroundWindow():
        return Readiness(False, "desktop_unavailable")
    return Readiness(True, "ready")


def normalize_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).strip().strip('"')))


def gymassistant_title_data_path(title: str) -> str | None:
    text = str(title or "").strip()
    if not text.startswith("Gym Assistant") or "Path=" not in text:
        return None
    value = text.rsplit("Path=", 1)[1].strip()
    value = value.rstrip("])}").strip()
    if not value:
        return None
    return normalize_windows_path(value)


def exact_gymassistant_window_count() -> int:
    expected = normalize_windows_path(EXPECTED_DATA_ROOT)
    matches = 0
    for window in payment_writer.enum_top_windows():
        if not window.visible:
            continue
        if gymassistant_title_data_path(window.text) == expected:
            matches += 1
    return matches


def source_is_available() -> bool:
    return SOURCE_ROOT.is_dir() and (SOURCE_ROOT / "Data").is_dir()


def payment_dialog_is_open() -> bool:
    return bool(
        payment_writer.find_open_payment_dialog()
        or payment_writer.find_transaction_payment_dialog()
        or payment_writer.find_credit_card_approval_dialog()
        or payment_writer.find_credit_card_result_dialog()
        or payment_writer.find_dependent_payment_prompt()
    )


def workstation_readiness(idle_seconds: int) -> Readiness:
    desktop = desktop_availability()
    if not desktop.ready:
        return desktop
    if not source_is_available():
        return Readiness(False, "z_data_unavailable")
    exact_windows = exact_gymassistant_window_count()
    if exact_windows == 0:
        return Readiness(False, "expected_gymassistant_window_missing")
    if exact_windows != 1:
        return Readiness(False, "expected_gymassistant_window_ambiguous")
    if payment_dialog_is_open():
        return Readiness(False, "gymassistant_dialog_open")
    try:
        current_idle = float(payment_writer.desktop_idle_seconds())
    except Exception:
        return Readiness(False, "desktop_idle_unavailable")
    if current_idle < max(1, int(idle_seconds)):
        return Readiness(False, "desktop_in_use")
    return Readiness(True, "ready")


class PortalClient:
    def __init__(self, token: str, timeout_seconds: int):
        token = str(token or "").strip()
        if not token:
            raise RunnerError("sync_token_missing")
        self._token = token
        self.timeout_seconds = timeout_seconds

    def _request(self, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Dreamz-RonLaptopPaymentRunner/{PACKAGE_VERSION}",
            "X-Sync-Agent": AGENT_ID,
            "X-Sync-Token": self._token,
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request(
            PORTAL_URL + path,
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise PortalApiError(f"portal_http_{int(exc.code)}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise PortalApiError("portal_unreachable") from exc
        if len(raw) > MAX_API_RESPONSE_BYTES:
            raise PortalApiError("portal_response_too_large")
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise PortalApiError("portal_invalid_json") from exc
        if not isinstance(decoded, dict):
            raise PortalApiError("portal_invalid_payload")
        return decoded

    def claim_command(self) -> dict | None:
        query = urlencode({"agent_id": AGENT_ID})
        payload = self._request(f"{COMMAND_ENDPOINT}?{query}")
        commands = payload.get("commands")
        if not isinstance(commands, list) or not commands:
            return None
        if len(commands) != 1 or not isinstance(commands[0], dict):
            raise PortalApiError("portal_invalid_command_batch")
        return commands[0]

    def post_command_result(self, command_id: int, payload: dict) -> dict:
        query = urlencode({"agent_id": AGENT_ID})
        return self._request(
            f"{COMMAND_ENDPOINT}/{int(command_id)}/result?{query}",
            method="POST",
            payload={**payload, "agent_id": AGENT_ID},
        )

    def claim_updates(self, limit: int) -> list[dict]:
        query = urlencode(
            {
                "limit": max(1, min(int(limit), 100)),
                "agent_id": AGENT_ID,
            }
        )
        payload = self._request(f"{UPDATE_ENDPOINT}?{query}")
        updates = payload.get("updates")
        if not isinstance(updates, list):
            raise PortalApiError("portal_invalid_update_batch")
        if any(not isinstance(update, dict) for update in updates):
            raise PortalApiError("portal_invalid_update_batch")
        return updates

    def post_update_result(self, update_id: int, payload: dict) -> dict:
        query = urlencode({"agent_id": AGENT_ID})
        return self._request(
            f"{UPDATE_ENDPOINT}/{int(update_id)}/result?{query}",
            method="POST",
            payload={**payload, "agent_id": AGENT_ID},
        )


def parse_portal_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def command_recency(command: dict, ttl_seconds: int, *, now: datetime | None = None) -> Readiness:
    created_at = parse_portal_timestamp(command.get("created_at"))
    if not created_at:
        return Readiness(False, "command_age_unverifiable")
    current = (now or utc_now()).astimezone(timezone.utc)
    age_seconds = (current - created_at).total_seconds()
    if age_seconds < -120:
        return Readiness(False, "command_timestamp_in_future")
    if age_seconds > ttl_seconds:
        return Readiness(False, "command_expired")
    return Readiness(True, "ready")


def safe_writer_error(exc: Exception) -> str:
    text = str(exc or "").casefold()
    if "dependent" in text or "linked membership" in text:
        return "linked_membership_requires_manual_review"
    if "balance" in text:
        return "nonzero_balance_requires_manual_review"
    if "amount" in text or "due date" in text or "billing period" in text:
        return "payment_details_mismatch_requires_manual_review"
    if "timed out" in text or "timeout" in text:
        return "gymassistant_ui_timeout_requires_manual_review"
    if "inactive" in text or "activate" in text:
        return "gymassistant_activation_requires_manual_review"
    return "gymassistant_write_failed_requires_manual_review"


def sanitized_deferred_reason(value: object) -> str:
    allowed = {
        "desktop_not_idle",
        "payment_dialog_already_open",
        "gym_assistant_not_running",
    }
    reason = str(value or "").strip().lower()
    return reason if reason in allowed else "workstation_not_ready"


def validate_command(command: dict) -> tuple[int, int]:
    try:
        command_id = int(command.get("id"))
    except (TypeError, ValueError) as exc:
        raise RunnerError("invalid_command_id") from exc
    if command_id <= 0:
        raise RunnerError("invalid_command_id")
    if str(command.get("target_agent") or "").strip() != AGENT_ID:
        raise RunnerError("command_target_mismatch")
    try:
        requested_limit = int(command.get("requested_limit") or 0)
    except (TypeError, ValueError) as exc:
        raise RunnerError("invalid_command_limit") from exc
    if requested_limit <= 0:
        raise RunnerError("invalid_command_limit")
    return command_id, min(requested_limit, MAX_COMMAND_LIMIT)


def validate_update(update: dict) -> tuple[int, str]:
    try:
        update_id = int(update.get("id"))
    except (TypeError, ValueError) as exc:
        raise RunnerError("invalid_update_id") from exc
    if update_id <= 0:
        raise RunnerError("invalid_update_id")
    if str(update.get("target_agent") or "").strip() != AGENT_ID:
        raise RunnerError("update_target_mismatch")
    idempotency_key = str(update.get("idempotency_key") or "").strip()
    if not idempotency_key:
        raise RunnerError("update_idempotency_key_missing")
    return update_id, idempotency_key


def apply_official_writer(update: dict, writer_timeout_seconds: int) -> dict:
    return payment_writer.run_writer(
        {
            "source_root": str(SOURCE_ROOT),
            "update": update,
        },
        apply=True,
        timeout=float(writer_timeout_seconds),
        foreground_ui=True,
        require_idle_seconds=0,
        payment_method="Credit Card",
    )


def _post_update_result_with_retry(
    client: PortalClient,
    update_id: int,
    payload: dict,
    *,
    attempts: int = 3,
) -> None:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            client.post_update_result(update_id, payload)
            return
        except PortalApiError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise AcknowledgementPending("payment_result_ack_pending") from last_error


def process_one_update(
    client: PortalClient,
    update: dict,
    receipts: ReceiptStore,
    config: RuntimeConfig,
    *,
    readiness_check: Callable[[int], Readiness] = workstation_readiness,
    writer_call: Callable[[dict, int], dict] = apply_official_writer,
) -> str:
    update_id, idempotency_key = validate_update(update)

    if receipts.has_conflict(update_id, idempotency_key):
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "failed",
                "error": "local_idempotency_receipt_conflict",
                "writer": "gymassistant_payment_writer",
            },
        )
        return "failed"

    if receipts.matches(update_id, idempotency_key):
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "applied",
                "writer": "gymassistant_payment_writer",
                "verification": "recovered_local_receipt",
            },
        )
        receipts.acknowledge(update_id)
        return "applied"

    ready = readiness_check(config.idle_seconds)
    if not ready.ready:
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "deferred",
                "reason": ready.reason,
                "writer": "gymassistant_payment_writer",
            },
        )
        return "deferred"

    try:
        result = writer_call(update, config.writer_timeout_seconds)
    except Exception as exc:
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "failed",
                "error": safe_writer_error(exc),
                "writer": "gymassistant_payment_writer",
            },
        )
        return "failed"

    if str(result.get("status") or "").strip().lower() == "deferred":
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "deferred",
                "reason": sanitized_deferred_reason(result.get("reason")),
                "writer": "gymassistant_payment_writer",
            },
        )
        return "deferred"
    if str(result.get("status") or "").strip().lower() != "applied" or not result.get("applied"):
        _post_update_result_with_retry(
            client,
            update_id,
            {
                "status": "failed",
                "error": "writer_did_not_confirm_applied",
                "writer": "gymassistant_payment_writer",
            },
        )
        return "failed"

    # This receipt is written before the network acknowledgement. If the API
    # response is lost, a later claim reports applied without clicking twice.
    receipts.record_applied(update_id, idempotency_key)
    _post_update_result_with_retry(
        client,
        update_id,
        {
            "status": "applied",
            "writer": "gymassistant_payment_writer",
            "verification": "guarded_gymassistant_ui",
        },
    )
    receipts.acknowledge(update_id)
    return "applied"


def post_command_failure(client: PortalClient, command_id: int, reason: str) -> None:
    client.post_command_result(
        command_id,
        {
            "status": "failed",
            "error": reason,
            "command": {
                "id": command_id,
                "target_agent": AGENT_ID,
            },
        },
    )


def process_available_command(
    client: PortalClient,
    receipts: ReceiptStore,
    config: RuntimeConfig,
    *,
    readiness_check: Callable[[int], Readiness] = workstation_readiness,
    writer_call: Callable[[dict, int], dict] = apply_official_writer,
    now: datetime | None = None,
) -> tuple[str, CommandSummary]:
    initial_ready = readiness_check(config.idle_seconds)
    if not initial_ready.ready:
        return initial_ready.reason, CommandSummary()

    # This is the first mutating queue operation. No command is claimed before
    # the local desktop, source mapping, and exact Gym Assistant window pass.
    command = client.claim_command()
    if not command:
        return "idle", CommandSummary()

    try:
        command_id, limit = validate_command(command)
    except RunnerError:
        # Without a trustworthy id/target, do not attempt any queue or UI work.
        return "invalid_command", CommandSummary()

    recent = command_recency(command, config.command_ttl_seconds, now=now)
    if not recent.ready:
        post_command_failure(client, command_id, recent.reason)
        return recent.reason, CommandSummary()

    after_claim_ready = readiness_check(config.idle_seconds)
    if not after_claim_ready.ready:
        post_command_failure(client, command_id, after_claim_ready.reason)
        return after_claim_ready.reason, CommandSummary()

    summary = CommandSummary()
    remaining = limit
    stop_claiming = False
    try:
        while remaining > 0 and not stop_claiming:
            ready = readiness_check(config.idle_seconds)
            if not ready.ready:
                if summary.received == 0:
                    post_command_failure(client, command_id, ready.reason)
                    return ready.reason, summary
                break

            updates = client.claim_updates(min(remaining, 100))
            if not updates:
                break
            summary.received += len(updates)
            remaining -= len(updates)

            for update in updates:
                try:
                    outcome = process_one_update(
                        client,
                        update,
                        receipts,
                        config,
                        readiness_check=readiness_check,
                        writer_call=writer_call,
                    )
                except AcknowledgementPending:
                    # Do not complete/fail the command. The server claim will
                    # expire, while the local receipt prevents a second click.
                    raise
                except RunnerError:
                    outcome = "failed"
                    try:
                        update_id = int(update.get("id"))
                    except (TypeError, ValueError):
                        update_id = 0
                    if update_id > 0:
                        _post_update_result_with_retry(
                            client,
                            update_id,
                            {
                                "status": "failed",
                                "error": "invalid_payment_update_payload",
                                "writer": "gymassistant_payment_writer",
                            },
                        )

                if outcome == "applied":
                    summary.applied += 1
                elif outcome == "deferred":
                    summary.deferred += 1
                    stop_claiming = True
                else:
                    summary.failed += 1

        client.post_command_result(
            command_id,
            {
                "status": "completed",
                "summary": summary.public(),
                "command": {
                    "id": command_id,
                    "requested_limit": limit,
                    "target_agent": AGENT_ID,
                },
            },
        )
        return "completed", summary
    except AcknowledgementPending:
        return "payment_result_ack_pending", summary
    except PortalApiError:
        # Leave the command claim to expire; never manufacture a completion.
        return "portal_unreachable_during_command", summary


def configure_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ron_laptop_payment_runner")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        path,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def wait_for_stop(stop_path: Path, seconds: int) -> bool:
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if stop_path.exists():
            return True
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    return stop_path.exists()


def run_loop(
    config_path: Path,
    *,
    once: bool = False,
    client_factory: Callable[[str, int], PortalClient] = PortalClient,
) -> int:
    home = config_path.parent
    state_dir = home / "state"
    status = StatusStore(state_dir / "status.json")
    receipts = ReceiptStore(state_dir / "payment-receipts.json")
    stop_path = state_dir / "stop.request"
    logger = configure_logger(home / "logs" / "runner.log")
    mutex = NamedMutex()

    try:
        if not mutex.acquire():
            # An already-running instance owns the shared status file. A
            # duplicate launcher exits without overwriting that live status.
            logger.info("state=duplicate_launcher_exit reason=runner_already_active")
            return 0

        config = load_runtime_config(config_path)
        token = str(os.getenv("SYNC_API_TOKEN") or "").strip()
        if not token:
            status.update(
                "configuration_error",
                reason="sync_token_missing",
                receipt_count=receipts.count(),
            )
            logger.error("state=configuration_error reason=sync_token_missing")
            return 2
        client = client_factory(token, config.request_timeout_seconds)
        token = ""
        os.environ.pop("SYNC_API_TOKEN", None)

        logger.info(
            "state=started version=%s agent=%s source=Z_drive_payment_only",
            PACKAGE_VERSION,
            AGENT_ID,
        )
        while True:
            if stop_path.exists():
                status.update(
                    "stopped",
                    reason="stop_requested",
                    receipt_count=receipts.count(),
                )
                logger.info("state=stopped reason=stop_requested")
                return 0
            try:
                state, summary = process_available_command(client, receipts, config)
                status.update(
                    "waiting" if state in {"idle", "desktop_in_use"} else state,
                    reason=None if state == "completed" else state,
                    summary=summary if state == "completed" else None,
                    receipt_count=receipts.count(),
                )
                if state == "completed":
                    logger.info(
                        "state=completed received=%d applied=%d failed=%d deferred=%d",
                        summary.received,
                        summary.applied,
                        summary.failed,
                        summary.deferred,
                    )
                elif state not in {
                    "idle",
                    "desktop_in_use",
                    "desktop_locked",
                    "session_not_active",
                    "expected_gymassistant_window_missing",
                    "z_data_unavailable",
                }:
                    logger.warning("state=waiting reason=%s", state)
            except PortalApiError as exc:
                status.update(
                    "waiting",
                    reason=str(exc),
                    receipt_count=receipts.count(),
                )
                logger.warning("state=waiting reason=%s", str(exc))
            except Exception:
                status.update(
                    "waiting",
                    reason="unexpected_runner_error",
                    receipt_count=receipts.count(),
                )
                logger.error("state=waiting reason=unexpected_runner_error")

            if once:
                return 0
            if wait_for_stop(stop_path, config.poll_seconds):
                continue
    except RunnerError as exc:
        reason = str(exc)
        status.update(
            "stopped",
            reason=reason,
            receipt_count=receipts.count(),
        )
        logger.error("state=stopped reason=%s", reason)
        return 3
    finally:
        mutex.close()
        os.environ.pop("SYNC_API_TOKEN", None)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dedicated outbound-only FEP payment runner. "
            "Agent ron_laptop and Gym Assistant source Z:\\ are fixed."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "runtime.json",
        help="Path to the non-secret runtime tuning file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one safe poll iteration; primarily for local diagnostics.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_loop(args.config.resolve(), once=bool(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
