from __future__ import annotations

"""Build and submit an exact, invoice-only Gym Assistant event batch.

This module never writes Gym Assistant data.  The first bootstrap is bound to
one stable backup, one canonical member allowlist and exact scanner counts.  It
builds the upload from the very same in-memory journal scan that satisfies the
gate, avoiding a preflight/upload time-of-check/time-of-use split.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from typing import Sequence
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

sys.dont_write_bytecode = True

import ga_invoice_backup_probe as archive_probe
from ga_invoice_target_probe import (
    TARGET_PROBE_SCHEMA,
    scan_target_invoice_membership_events,
)
from ga_journal import (
    parse_gymassistant_billing_catalog_text,
    service_period_matches_catalog_interval,
)


INVOICE_BATCH_SCHEMA = "dreamz.ga.invoice-event-batch.v1"
INVOICE_BATCH_RECEIPT_SCHEMA = "dreamz.ga.invoice-event-batch-receipt.v1"
MAX_BATCH_EVENTS = archive_probe.MAX_TARGET_MEMBERSHIP_EVENTS
DEFAULT_PORTAL_URL = "https://dreamzmemberportal-production.up.railway.app"


class InvoiceBatchBlocked(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Refuse redirects before an authenticated request can be replayed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InvoiceBatchBlocked("portal_redirect_refused")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def invoice_event_set_sha256(records: list[dict]) -> str:
    bindings = sorted(
        "|".join(
            (
                str(record.get("source_reference") or ""),
                str(record.get("source_payload_hash") or ""),
            )
        )
        for record in records
    )
    return sha256("\n".join(bindings).encode("ascii")).hexdigest()


def _validated_hash(value: object, reason_code: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise InvoiceBatchBlocked(reason_code)
    return normalized


def _source_snapshot_at(mtime_ns: int) -> str:
    if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool) or mtime_ns < 0:
        raise InvoiceBatchBlocked("source_unstable")
    return datetime.fromtimestamp(
        mtime_ns / 1_000_000_000,
        timezone.utc,
    ).replace(microsecond=0).isoformat()


def build_exact_invoice_event_batch(
    backup_path: Path,
    *,
    expected_length: int,
    expected_sha256: str,
    allowlist_path: Path,
    expected_allowlist_count: int,
    expected_allowlist_set_sha256: str,
    expected_target_event_count: int,
    expected_short_fragment_count: int,
    generated_at: str | None = None,
) -> dict:
    """Return a portal batch only when every exact bootstrap gate passes."""

    try:
        if (
            not archive_probe._lexically_local_absolute_windows_path(backup_path)
            or not archive_probe._lexically_local_absolute_windows_path(
                allowlist_path
            )
        ):
            raise InvoiceBatchBlocked("path_invalid")
        if (
            not isinstance(expected_length, int)
            or isinstance(expected_length, bool)
            or expected_length < 1
            or expected_length > archive_probe.MAX_ARCHIVE_BYTES
            or not isinstance(expected_allowlist_count, int)
            or isinstance(expected_allowlist_count, bool)
            or not 1 <= expected_allowlist_count <= 100
            or not isinstance(expected_target_event_count, int)
            or isinstance(expected_target_event_count, bool)
            or not 1 <= expected_target_event_count <= MAX_BATCH_EVENTS
            or not isinstance(expected_short_fragment_count, int)
            or isinstance(expected_short_fragment_count, bool)
            or not 0 <= expected_short_fragment_count <= 8
        ):
            raise InvoiceBatchBlocked("invalid_binding")
        expected_source_hash = _validated_hash(
            expected_sha256,
            "source_hash_invalid",
        )
        expected_allowlist_hash = _validated_hash(
            expected_allowlist_set_sha256,
            "allowlist_hash_invalid",
        )

        allowlist_read = archive_probe._read_stable_file_twice(
            Path(allowlist_path),
            maximum_bytes=archive_probe.MAX_ALLOWLIST_BYTES,
        )
        member_ids, member_set_hash = archive_probe._canonical_allowlist(
            allowlist_read.data
        )
        if (
            len(member_ids) != expected_allowlist_count
            or member_set_hash != expected_allowlist_hash
        ):
            raise InvoiceBatchBlocked("allowlist_binding_mismatch")

        stable = archive_probe._read_stable_file_twice(
            Path(backup_path),
            maximum_bytes=archive_probe.MAX_ARCHIVE_BYTES,
            expected_length=expected_length,
        )
        if stable.source_sha256 != expected_source_hash:
            raise InvoiceBatchBlocked("source_hash_mismatch")
        snapshot_at = _source_snapshot_at(stable.mtime_ns)
        snapshot = archive_probe._inspect_archive(stable.data)
        target_scan = scan_target_invoice_membership_events(
            snapshot.journal_bytes,
            member_ids,
        )
        if target_scan.target_issue_count:
            raise InvoiceBatchBlocked("target_membership_parse_issue")
        if (
            len(target_scan.events) != expected_target_event_count
            or target_scan.target_candidate_count != expected_target_event_count
        ):
            raise InvoiceBatchBlocked("target_event_count_mismatch")
        if (
            target_scan.ignored_short_fragment_count
            != expected_short_fragment_count
        ):
            raise InvoiceBatchBlocked("short_fragment_count_mismatch")
        if (
            target_scan.ambiguous_target_candidate_count
            or target_scan.embedded_target_candidate_count
            or target_scan.cross_boundary_target_candidate_count
        ):
            raise InvoiceBatchBlocked("target_scope_ambiguous")

        covered_ids = {event.member_id for event in target_scan.events}
        positive_ids = {
            event.member_id
            for event in target_scan.events
            if event.is_positive_membership_payment
        }
        if covered_ids != member_ids:
            raise InvoiceBatchBlocked("target_member_coverage_incomplete")
        if positive_ids != member_ids:
            raise InvoiceBatchBlocked("target_positive_coverage_incomplete")

        options, _issues = parse_gymassistant_billing_catalog_text(
            snapshot.members_bytes.decode("latin-1", errors="replace")
        )
        options_by_key = {}
        for option in options:
            key = (option.membership_type_id, option.billing_option_code)
            if key in options_by_key:
                raise InvoiceBatchBlocked("catalog_ambiguous")
            options_by_key[key] = option
        if not options_by_key:
            raise InvoiceBatchBlocked("catalog_empty")

        records: list[dict] = []
        for event in target_scan.events:
            record = event.as_sync_record()
            option = options_by_key.get(
                (event.membership_type_id, event.billing_option_code)
            )
            if option:
                record.update(
                    catalog_plan_name=option.plan_name,
                    catalog_base_amount_cents=option.base_amount_cents,
                    catalog_interval_count=option.interval_count,
                    catalog_interval_unit=option.interval_unit,
                    catalog_match_status=(
                        "match"
                        if event.dues_cents == option.base_amount_cents
                        else "mismatch"
                    ),
                    catalog_period_match_status=(
                        "match"
                        if service_period_matches_catalog_interval(
                            event.service_period_start,
                            event.service_period_end_exclusive,
                            option.interval_count,
                            option.interval_unit,
                        )
                        else "mismatch"
                    ),
                )
            else:
                record.update(
                    catalog_plan_name=None,
                    catalog_base_amount_cents=None,
                    catalog_interval_count=None,
                    catalog_interval_unit=None,
                    catalog_match_status="missing",
                    catalog_period_match_status="missing",
                )
            records.append(record)

        ordered_member_ids = sorted(member_ids, key=int)
        event_set_hash = invoice_event_set_sha256(records)
        batch_id = (
            "ga-invoice-"
            f"{stable.source_sha256[:20]}-{member_set_hash[:20]}"
        )
        batch = {
            "schema": INVOICE_BATCH_SCHEMA,
            "batch_id": batch_id,
            # Keep retries byte-identical for the same bound snapshot.  The
            # endpoint receipt time records the actual upload time.
            "generated_at": generated_at or snapshot_at,
            "invoice_scanner_schema": TARGET_PROBE_SCHEMA,
            "invoice_member_count": len(ordered_member_ids),
            "invoice_member_ids_sha256": member_set_hash,
            "invoice_event_count": len(records),
            "invoice_event_set_sha256": event_set_hash,
            "invoice_short_fragment_count": (
                target_scan.ignored_short_fragment_count
            ),
            "invoice_pilot_member_ids": ordered_member_ids,
            "invoice_membership_events": records,
            "invoice_journal_issue_count": 0,
            "invoice_journal_source": "gym_assistant_backup:Journal.jtx",
            "invoice_catalog_source": "gym_assistant_backup:Members.btx",
            "invoice_source_snapshot_at": snapshot_at,
            "invoice_source_sha256": stable.source_sha256,
            "invoice_catalog_sha256": sha256(snapshot.members_bytes).hexdigest(),
        }
        batch["batch_sha256"] = canonical_json_sha256(batch)
        return batch
    except InvoiceBatchBlocked:
        raise
    except archive_probe.ProbeBlocked as exc:
        raise InvoiceBatchBlocked(exc.reason_code) from None
    except Exception as exc:
        raise InvoiceBatchBlocked("batch_internal_error") from exc


def verify_invoice_batch_receipt(payload: dict, receipt: object) -> dict:
    if not isinstance(receipt, dict):
        raise InvoiceBatchBlocked("receipt_invalid")
    if receipt.get("schema") != INVOICE_BATCH_RECEIPT_SCHEMA:
        raise InvoiceBatchBlocked("receipt_schema_invalid")
    if receipt.get("status") not in {"success", "duplicate"}:
        raise InvoiceBatchBlocked("receipt_status_invalid")
    exact_fields = {
        "batch_id": payload["batch_id"],
        "batch_sha256": payload["batch_sha256"],
        "source_sha256": payload["invoice_source_sha256"],
        "event_set_sha256": payload["invoice_event_set_sha256"],
        "member_ids_sha256": payload["invoice_member_ids_sha256"],
        "received": payload["invoice_event_count"],
        "member_count": payload["invoice_member_count"],
        "rejected": 0,
        "conflicts": 0,
    }
    if any(receipt.get(key) != value for key, value in exact_fields.items()):
        raise InvoiceBatchBlocked("receipt_binding_mismatch")
    return {"schema": receipt["schema"], "status": receipt["status"], **exact_fields}


def post_invoice_event_batch(
    portal_url: str,
    sync_token: str,
    payload: dict,
    *,
    timeout: int = 120,
) -> dict:
    if not str(portal_url or "").startswith("https://"):
        raise InvoiceBatchBlocked("portal_url_invalid")
    if not str(sync_token or "").strip():
        raise InvoiceBatchBlocked("sync_token_missing")
    endpoint = portal_url.rstrip("/") + "/api/sync/invoice-event-batches"
    request = urlrequest.Request(
        endpoint,
        data=canonical_json_bytes(payload),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Sync-Token": sync_token,
            "Idempotency-Key": str(payload.get("batch_id") or ""),
        },
    )
    opener = urlrequest.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != endpoint:
                raise InvoiceBatchBlocked("portal_response_url_mismatch")
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise InvoiceBatchBlocked(f"portal_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise InvoiceBatchBlocked("portal_request_failed") from None
    return verify_invoice_batch_receipt(payload, result)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exact, invoice-only Dreamz portal bootstrap",
    )
    parser.add_argument("--backup-path", required=True, type=Path)
    parser.add_argument("--expected-length", required=True, type=int)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--allowlist-path", required=True, type=Path)
    parser.add_argument("--expected-allowlist-count", required=True, type=int)
    parser.add_argument("--expected-allowlist-set-sha256", required=True)
    parser.add_argument("--expected-target-event-count", required=True, type=int)
    parser.add_argument("--expected-short-fragment-count", required=True, type=int)
    parser.add_argument("--portal-url", default=DEFAULT_PORTAL_URL)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        payload = build_exact_invoice_event_batch(
            args.backup_path,
            expected_length=args.expected_length,
            expected_sha256=args.expected_sha256,
            allowlist_path=args.allowlist_path,
            expected_allowlist_count=args.expected_allowlist_count,
            expected_allowlist_set_sha256=args.expected_allowlist_set_sha256,
            expected_target_event_count=args.expected_target_event_count,
            expected_short_fragment_count=args.expected_short_fragment_count,
        )
        if args.prepare_only:
            result = {
                "schema": "dreamz.ga.invoice-event-batch-preflight.v1",
                "status": "passed",
                "ready_for_upload": True,
                "member_count": payload["invoice_member_count"],
                "event_count": payload["invoice_event_count"],
                "source_sha256": payload["invoice_source_sha256"],
                "event_set_sha256": payload["invoice_event_set_sha256"],
                "batch_sha256": payload["batch_sha256"],
                "authorizes_invoice_issuing": False,
            }
        else:
            result = post_invoice_event_batch(
                args.portal_url,
                os.getenv("SYNC_API_TOKEN", ""),
                payload,
            )
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        return 0
    except InvoiceBatchBlocked as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "schema": "dreamz.ga.invoice-event-batch-result.v1",
                    "status": "blocked",
                    "reason_code": exc.reason_code,
                    "authorizes_invoice_issuing": False,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
