from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import invoice_event_batch as batch_module


TARGET = (
    "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
)
CATALOG = "\n".join(
    [
        "CLASS=contract Dreamz test",
        "MEMBERTYPE_ID=900000101",
        "OPTION=1 MONTHS EFT 6500 0",
        "-",
    ]
)


def allowlist_hash(*values: str) -> str:
    return sha256("\n".join(sorted(values, key=int)).encode("ascii")).hexdigest()


class InvoiceEventBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "GABackup-test.gbu"
        self.allowlist = self.root / "allowlist.txt"
        self.allowlist.write_text("90001\n", encoding="ascii")
        self.write_backup(TARGET)

    def tearDown(self):
        self.temp.cleanup()

    def write_backup(self, journal: str, catalog: str = CATALOG):
        with zipfile.ZipFile(
            self.backup,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("Journal.jtx", journal)
            archive.writestr("Members.btx", catalog)

    def build(self, **overrides):
        values = {
            "backup_path": self.backup,
            "expected_length": self.backup.stat().st_size,
            "expected_sha256": sha256(self.backup.read_bytes()).hexdigest(),
            "allowlist_path": self.allowlist,
            "expected_allowlist_count": 1,
            "expected_allowlist_set_sha256": allowlist_hash("90001"),
            "expected_target_event_count": 1,
            "expected_short_fragment_count": 0,
            "generated_at": "2026-08-02T12:00:00+00:00",
        }
        values.update(overrides)
        return batch_module.build_exact_invoice_event_batch(**values)

    def test_builds_bound_batch_from_same_scan(self):
        payload = self.build()

        self.assertEqual(payload["schema"], batch_module.INVOICE_BATCH_SCHEMA)
        self.assertEqual(payload["invoice_member_count"], 1)
        self.assertEqual(payload["invoice_event_count"], 1)
        self.assertEqual(payload["invoice_journal_issue_count"], 0)
        self.assertEqual(len(payload["invoice_membership_events"]), 1)
        self.assertEqual(
            payload["batch_sha256"],
            batch_module.canonical_json_sha256(
                {key: value for key, value in payload.items() if key != "batch_sha256"}
            ),
        )

    def test_snapshot_time_uses_the_stable_read_mtime_without_restat(self):
        fixed_mtime_ns = 1_775_187_245_999_999_900
        os.utime(self.backup, ns=(fixed_mtime_ns, fixed_mtime_ns))
        changed_mtime_ns = fixed_mtime_ns + 7_200_000_000_000
        stable_reader = batch_module.archive_probe._read_stable_file_twice

        def read_then_touch(path, **kwargs):
            stable = stable_reader(path, **kwargs)
            if Path(path) == self.backup:
                os.utime(
                    self.backup,
                    ns=(changed_mtime_ns, changed_mtime_ns),
                )
            return stable

        with patch.object(
            batch_module.archive_probe,
            "_read_stable_file_twice",
            side_effect=read_then_touch,
        ):
            payload = self.build()

        self.assertEqual(
            payload["invoice_source_snapshot_at"],
            datetime.fromtimestamp(
                fixed_mtime_ns / 1_000_000_000,
                timezone.utc,
            ).replace(microsecond=0).isoformat(),
        )
        self.assertEqual(self.backup.stat().st_mtime_ns, changed_mtime_ns)

    def test_source_drift_blocks_before_payload(self):
        expected_hash = sha256(self.backup.read_bytes()).hexdigest()
        self.write_backup(TARGET + "\nA!")

        with self.assertRaisesRegex(
            batch_module.InvoiceBatchBlocked,
            "source_length_mismatch|source_hash_mismatch",
        ):
            self.build(expected_sha256=expected_hash)

    def test_target_defect_returns_no_partial_batch(self):
        other = (
            "c20260215!1559 7002 1771185540 0 990002 99999 38 0 0 0 29"
            "|200 ProShop purchase"
        )
        self.write_backup(TARGET + "\n" + other + "bad-ts 1 0 0 90001 BAD|x")

        with self.assertRaises(batch_module.InvoiceBatchBlocked):
            self.build()

    def test_duplicate_catalog_key_blocks_whole_batch(self):
        self.write_backup(TARGET, CATALOG + "\n" + CATALOG)

        with self.assertRaisesRegex(
            batch_module.InvoiceBatchBlocked,
            "catalog_ambiguous",
        ):
            self.build()

    def test_receipt_must_match_every_financial_binding(self):
        payload = self.build()
        receipt = {
            "schema": batch_module.INVOICE_BATCH_RECEIPT_SCHEMA,
            "status": "success",
            "batch_id": payload["batch_id"],
            "batch_sha256": payload["batch_sha256"],
            "source_sha256": payload["invoice_source_sha256"],
            "event_set_sha256": payload["invoice_event_set_sha256"],
            "member_ids_sha256": payload["invoice_member_ids_sha256"],
            "received": 1,
            "member_count": 1,
            "rejected": 0,
            "conflicts": 0,
        }
        self.assertEqual(
            batch_module.verify_invoice_batch_receipt(payload, receipt)["status"],
            "success",
        )
        receipt["received"] = 0
        with self.assertRaisesRegex(
            batch_module.InvoiceBatchBlocked,
            "receipt_binding_mismatch",
        ):
            batch_module.verify_invoice_batch_receipt(payload, receipt)

    def test_authenticated_post_uses_a_no_redirect_opener(self):
        payload = self.build()
        receipt = {
            "schema": batch_module.INVOICE_BATCH_RECEIPT_SCHEMA,
            "status": "success",
            "batch_id": payload["batch_id"],
            "batch_sha256": payload["batch_sha256"],
            "source_sha256": payload["invoice_source_sha256"],
            "event_set_sha256": payload["invoice_event_set_sha256"],
            "member_ids_sha256": payload["invoice_member_ids_sha256"],
            "received": 1,
            "member_count": 1,
            "rejected": 0,
            "conflicts": 0,
        }
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = (
            "https://portal.example/api/sync/invoice-event-batches"
        )
        response.read.return_value = json.dumps(receipt).encode("utf-8")
        opener = MagicMock()
        opener.open.return_value = response

        with patch.object(
            batch_module.urlrequest,
            "build_opener",
            return_value=opener,
        ) as build_opener:
            result = batch_module.post_invoice_event_batch(
                "https://portal.example",
                "test-token",
                payload,
            )

        self.assertEqual(result["status"], "success")
        self.assertIsInstance(
            build_opener.call_args.args[0],
            batch_module._NoRedirectHandler,
        )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-sync-token"), "test-token")

    def test_redirect_is_refused_before_authenticated_replay(self):
        handler = batch_module._NoRedirectHandler()

        with self.assertRaisesRegex(
            batch_module.InvoiceBatchBlocked,
            "portal_redirect_refused",
        ):
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://redirect.example/collect",
            )

    def test_redirect_target_never_receives_the_sync_token(self):
        for status_code in (301, 302, 303, 307, 308):
            with self.subTest(status_code=status_code):
                observed = []

                class RedirectServer(BaseHTTPRequestHandler):
                    def record(self):
                        observed.append(
                            (self.command, self.path, self.headers.get("X-Sync-Token"))
                        )

                    def do_POST(self):
                        self.record()
                        if self.path == "/start":
                            self.send_response(status_code)
                            self.send_header("Location", "/collector")
                            self.end_headers()
                            return
                        self.send_response(204)
                        self.end_headers()

                    def do_GET(self):
                        self.record()
                        self.send_response(204)
                        self.end_headers()

                    def log_message(self, _format, *_args):
                        return

                server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectServer)
                server_thread = threading.Thread(
                    target=server.serve_forever,
                    daemon=True,
                )
                server_thread.start()
                try:
                    request = batch_module.urlrequest.Request(
                        f"http://127.0.0.1:{server.server_port}/start",
                        data=b"{}",
                        method="POST",
                        headers={"X-Sync-Token": "test-token"},
                    )
                    opener = batch_module.urlrequest.build_opener(
                        batch_module._NoRedirectHandler()
                    )
                    with self.assertRaisesRegex(
                        batch_module.InvoiceBatchBlocked,
                        "portal_redirect_refused",
                    ):
                        opener.open(request, timeout=2)
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)

                self.assertEqual(
                    observed,
                    [("POST", "/start", "test-token")],
                )

    def test_response_from_a_different_url_is_rejected(self):
        payload = self.build()
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://redirect.example/collect"
        opener = MagicMock()
        opener.open.return_value = response

        with patch.object(
            batch_module.urlrequest,
            "build_opener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(
                batch_module.InvoiceBatchBlocked,
                "portal_response_url_mismatch",
            ):
                batch_module.post_invoice_event_batch(
                    "https://portal.example",
                    "test-token",
                    payload,
                )
        response.read.assert_not_called()

    def test_prepare_cli_output_contains_no_member_or_amount(self):
        args = [
            "--backup-path",
            str(self.backup),
            "--expected-length",
            str(self.backup.stat().st_size),
            "--expected-sha256",
            sha256(self.backup.read_bytes()).hexdigest(),
            "--allowlist-path",
            str(self.allowlist),
            "--expected-allowlist-count",
            "1",
            "--expected-allowlist-set-sha256",
            allowlist_hash("90001"),
            "--expected-target-event-count",
            "1",
            "--expected-short-fragment-count",
            "0",
            "--prepare-only",
        ]
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            exit_code = batch_module.main(args)
        self.assertEqual(exit_code, 0)
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ready_for_upload"])
        self.assertNotIn("90001", output.getvalue())
        self.assertNotIn("6500", output.getvalue())


if __name__ == "__main__":
    unittest.main()
