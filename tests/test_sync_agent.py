from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import tempfile
import unittest
import os
import subprocess
import zipfile
from unittest.mock import patch
from urllib.error import URLError

from sync_agent import build_sync_payload, diff_manifest, get_invoice_monitor_member_ids, load_manifest, main, parse_gymassistant_export, process_fep_payment_command, process_fep_payment_updates, read_stable_file_snapshot, run_fep_payment_writer, save_manifest, scan_source


PHOTO_VERSIONED_KEY = "portal/Data/Pictures/0000100-55c64d0fcd6f9d5f.jpg"


def write_backup(path: Path, member_id: str = "100") -> None:
    members_text = "\n".join([
        f"MN={member_id}",
        "LN=Tester",
        "FN=Sync",
        "MTN=contract Dreamz 12 m",
        "EM=sync@example.com",
        "-",
        "",
    ])
    with zipfile.ZipFile(path, "w") as backup:
        backup.writestr("Members.btx", members_text)


def write_members_btx(path: Path, member_id: str = "100") -> None:
    path.write_text("\n".join([
        "CLASS=contract Dreamz 6 months",
        "MEMBERTYPE_ID=900000101",
        "OPTION=1 MONTHS EFT 6500 0",
        "-",
        f"MN={member_id}",
        "LN=Tester",
        "FN=Live",
        "MTN=week pass",
        "EM=live@example.com",
        "-",
        "",
    ]), encoding="latin-1")


def write_members_btx_members(path: Path, member_ids: list[str]) -> None:
    lines = [
        "CLASS=contract Dreamz 6 months",
        "MEMBERTYPE_ID=900000101",
        "OPTION=1 MONTHS EFT 6500 0",
        "-",
    ]
    for member_id in member_ids:
        lines.extend([
            f"MN={member_id}",
            f"LN=Tester {member_id}",
            "FN=Snapshot",
            "MTN=contract Dreamz 6 months",
            "EM=snapshot@example.com",
            "-",
        ])
    lines.append("")
    path.write_text("\n".join(lines), encoding="latin-1")


def write_member_log(path: Path, member_id: str = "101") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        (
            "2026/05/25 15:30:00|FRONTDESK|Gym Assistant|"
            f"MN={member_id}\tLN=Live\tFN=Added\tMTN=week pass\t"
            "EM=added@example.com\tCB=20260525\tSU=20260525\t"
            "LP=20260525\tPU=20260625\tR$=4500\tN$=4500\t$B=0\tBT=1 MONTHS INV\tST=0\t-\t\n"
        ),
        encoding="latin-1",
    )


class SyncAgentTests(unittest.TestCase):
    def test_build_sync_payload_marks_full_member_snapshot_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            write_members_btx_members(
                data_dir / "Members.btx",
                ["200", "100"],
            )

            payload = build_sync_payload(root)

        snapshot = payload["member_snapshot"]
        self.assertTrue(snapshot["scope_complete"])
        self.assertTrue(snapshot["source_stable_during_read"])
        self.assertTrue(snapshot["authoritative_for_absence"])
        self.assertEqual(snapshot["source_count"], 2)
        self.assertEqual(snapshot["sent_count"], 2)
        self.assertEqual(snapshot["unique_count"], 2)
        self.assertEqual(
            snapshot["member_ids_sha256"],
            hashlib.sha256(b"100\n200").hexdigest(),
        )

    def test_build_sync_payload_rejects_source_changed_during_member_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            members_path = data_dir / "Members.btx"
            write_members_btx_members(members_path, ["100", "200"])
            original_parser = parse_gymassistant_export

            def parse_then_change(path):
                result = original_parser(path)
                members_path.write_bytes(members_path.read_bytes() + b"\n")
                return result

            with patch(
                "sync_agent.parse_gymassistant_export",
                side_effect=parse_then_change,
            ):
                payload = build_sync_payload(root)

        snapshot = payload["member_snapshot"]
        self.assertFalse(snapshot["source_stable_during_read"])
        self.assertFalse(snapshot["authoritative_for_absence"])

    def test_build_sync_payload_marks_member_limited_snapshot_non_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            write_members_btx_members(
                data_dir / "Members.btx",
                ["200", "100"],
            )

            payload = build_sync_payload(root, member_limit=1)

        snapshot = payload["member_snapshot"]
        self.assertFalse(snapshot["scope_complete"])
        self.assertFalse(snapshot["authoritative_for_absence"])
        self.assertEqual(snapshot["source_count"], 2)
        self.assertEqual(snapshot["sent_count"], 1)
        self.assertEqual(snapshot["unique_count"], 1)
        self.assertEqual(
            snapshot["member_ids_sha256"],
            hashlib.sha256(b"200").hexdigest(),
        )

    def test_invoice_monitor_discovery_fails_closed(self):
        with patch(
            "sync_agent.urlrequest.urlopen",
            side_effect=URLError("offline"),
        ):
            with self.assertRaisesRegex(RuntimeError, "reversal monitor"):
                get_invoice_monitor_member_ids(
                    "https://portal.example",
                    "sync-token",
                )

    def test_stable_snapshot_read_rejects_source_changed_during_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Journal.jtx"
            path.write_bytes(b"first")
            original_read_bytes = Path.read_bytes

            def mutate_after_read(target):
                data = original_read_bytes(target)
                target.write_bytes(data + b"-changed")
                return data

            with patch.object(Path, "read_bytes", mutate_after_read):
                with self.assertRaisesRegex(ValueError, "changed while"):
                    read_stable_file_snapshot(path)

    def test_build_sync_payload_supports_install_level_backup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Backup"
            backup_dir.mkdir(parents=True)
            write_members_btx(backup_dir / "Members.btx", member_id="90001")
            backup_path = backup_dir / "GABackup.gbu"
            with zipfile.ZipFile(backup_path, "w") as backup:
                backup.writestr(
                    "Members.btx",
                    "\n".join([
                        "CLASS=contract Dreamz 6 months",
                        "MEMBERTYPE_ID=900000101",
                        "OPTION=1 MONTHS EFT 6500 0",
                        "-",
                        "MN=90001",
                        "LN=Member",
                        "FN=Pilot",
                        "MTN=contract Dreamz 6 months",
                        "-",
                        "",
                    ]),
                )
                backup.writestr(
                    "Journal.jtx",
                    (
                        "c20260215!1558 7001 1771185480 0 990792 90001 3 0 0 0 29"
                        "|900000101 257 20260201 20260601 6500 0 0 6500 2 -1 0"
                    ),
                )

            with patch.dict(os.environ, {}, clear=True):
                payload = build_sync_payload(root, invoice_member_ids={"90001"})
                scan = scan_source(root)

        self.assertEqual(payload["invoice_membership_events"][0]["dues_cents"], 6500)
        self.assertEqual(Path(payload["invoice_journal_source"]).name, "GABackup.gbu")
        self.assertEqual(
            payload["invoice_membership_events"][0]["catalog_period_match_status"],
            "mismatch",
        )
        self.assertRegex(payload["invoice_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["invoice_catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertIsNotNone(payload["invoice_source_snapshot_at"])
        self.assertEqual(Path(scan.latest_backup).parent.name, "Backup")
        self.assertEqual(Path(scan.member_source).name, "Members.btx")

    def test_build_sync_payload_only_extracts_allowlisted_membership_journal_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            write_members_btx(data_dir / "Members.btx", member_id="90001")
            (data_dir / "Journal.jtx").write_text(
                "\n".join(
                    [
                        (
                            "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
                            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
                        ),
                        (
                            "c20260215!1559 7002 1771185540 0 0 90001 38 0 0 0 29"
                            "|200 ProShop purchase"
                        ),
                        (
                            "c20260723!1600 7003 1784841600 0 61002 99999 3 0 0 0 29"
                            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
                        ),
                    ]
                ),
                encoding="latin-1",
            )

            payload = build_sync_payload(root, invoice_member_ids={"90001"})

        self.assertEqual(len(payload["invoice_membership_events"]), 1)
        self.assertEqual(payload["invoice_pilot_member_ids"], ["90001"])
        event = payload["invoice_membership_events"][0]
        self.assertEqual(event["member_id"], "90001")
        self.assertEqual(event["event_name"], "membership_renewal")
        self.assertEqual(event["dues_cents"], 6500)
        self.assertEqual(event["tender_total_cents"], 6500)
        self.assertEqual(event["catalog_base_amount_cents"], 6500)
        self.assertEqual(event["catalog_interval_count"], 1)
        self.assertEqual(event["catalog_interval_unit"], "MONTHS")
        self.assertEqual(event["catalog_match_status"], "match")
        self.assertEqual(event["catalog_period_match_status"], "match")
        self.assertTrue(event["is_positive_membership_payment"])
        self.assertEqual(payload["invoice_journal_issue_count"], 0)
        self.assertTrue(payload["invoice_journal_source"].endswith("Journal.jtx"))
        self.assertTrue(payload["invoice_catalog_source"].endswith("Members.btx"))
        self.assertRegex(payload["invoice_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["invoice_catalog_sha256"], r"^[0-9a-f]{64}$")

    def test_invoice_snapshot_timestamp_comes_from_source_files_not_send_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            members_path = data_dir / "Members.btx"
            journal_path = data_dir / "Journal.jtx"
            write_members_btx(members_path, member_id="90001")
            journal_path.write_text(
                (
                    "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
                    "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
                ),
                encoding="latin-1",
            )
            old_time = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).timestamp()
            os.utime(members_path, (old_time, old_time))
            os.utime(journal_path, (old_time, old_time))

            payload = build_sync_payload(root, invoice_member_ids={"90001"})

        snapshot_at = datetime.fromisoformat(payload["invoice_source_snapshot_at"])
        self.assertLess(
            snapshot_at,
            datetime.now(timezone.utc) - timedelta(minutes=90),
        )

    def test_build_sync_payload_does_not_send_financial_events_without_pilot_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            data_dir.mkdir(parents=True)
            write_members_btx(data_dir / "Members.btx", member_id="90001")
            (data_dir / "Journal.jtx").write_text(
                (
                    "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
                    "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
                ),
                encoding="latin-1",
            )

            with patch.dict(os.environ, {}, clear=True):
                payload = build_sync_payload(root)

        self.assertEqual(payload["invoice_membership_events"], [])
        self.assertEqual(payload["invoice_pilot_member_ids"], [])
        self.assertIsNone(payload["invoice_journal_source"])

    def test_scan_source_summarizes_relevant_gymassistant_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            scan = scan_source(root)

            self.assertEqual(scan.member_count, 1)
            self.assertEqual(scan.relevant_file_count, 3)
            self.assertEqual({item.kind for item in scan.files}, {"backup", "attachment_pdf", "photo"})

    def test_scan_source_prefers_live_members_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu", member_id="100")
            live_members = data_dir / "Members.btx"
            write_members_btx(live_members, member_id="101")

            scan = scan_source(root)
            payload = build_sync_payload(root)

        self.assertTrue(scan.member_source.endswith("Members.btx"))
        self.assertEqual(scan.member_count, 1)
        self.assertIn("Data/Members.btx", [item.path for item in scan.files])
        self.assertEqual(payload["members"][0]["member_id"], "101")
        self.assertEqual(payload["members"][0]["name"], "Tester, Live")

    def test_scan_source_does_not_warn_for_recent_live_members_dat_touch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            write_backup(backup, member_id="100")
            live_dat = data_dir / "Members.dat"
            live_dat.write_bytes(b"live binary member data")
            os.utime(backup, (1000, 1000))
            os.utime(live_dat, (2000, 2000))

            scan = scan_source(root)
            payload = build_sync_payload(root)

        self.assertTrue(scan.member_source.endswith("GABackup-test.gbu"))
        self.assertIsNone(scan.warning)
        self.assertIsNone(payload["warning"])
        self.assertFalse(payload["member_snapshot"]["authoritative_for_absence"])
        self.assertIn("Data/Members.dat", [item.path for item in scan.files])

    def test_scan_source_warns_when_live_members_dat_is_more_than_24h_newer_than_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            write_backup(backup, member_id="100")
            live_dat = data_dir / "Members.dat"
            live_dat.write_bytes(b"live binary member data")
            os.utime(backup, (1000, 1000))
            os.utime(live_dat, (1000 + 25 * 60 * 60, 1000 + 25 * 60 * 60))

            scan = scan_source(root)
            payload = build_sync_payload(root)

        self.assertTrue(scan.member_source.endswith("GABackup-test.gbu"))
        self.assertIn("more than 24 hours newer", scan.warning)
        self.assertIn("more than 24 hours newer", payload["warning"])
        self.assertFalse(payload["member_snapshot"]["authoritative_for_absence"])

    def test_build_sync_payload_applies_live_member_logs_newer_than_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            write_backup(backup, member_id="100")
            live_dat = data_dir / "Members.dat"
            live_dat.write_bytes(b"live binary member data")
            log_file = data_dir / "Temp Files" / "Member Updates" / "EditMembers 2026-05-25.txt"
            write_member_log(log_file, member_id="101")
            os.utime(backup, (1000, 1000))
            os.utime(live_dat, (2000, 2000))
            os.utime(log_file, (2000, 2000))

            scan = scan_source(root)
            payload = build_sync_payload(root)

        member_ids = {member["member_id"] for member in payload["members"]}
        self.assertEqual(scan.warning, None)
        self.assertEqual(scan.member_count, 2)
        self.assertIn("101", member_ids)
        self.assertIn("Data/Temp Files/Member Updates/EditMembers 2026-05-25.txt", [item.path for item in scan.files])

    def test_added_members_file_does_not_overwrite_existing_backup_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            members_text = "\n".join([
                "MN=34933",
                "LN=Wissenmansen",
                "FN=Dennis",
                "MTN=1 WEEK PASS",
                "EM=dennis@example.com",
                "-",
                "",
            ])
            with zipfile.ZipFile(backup, "w") as backup_file:
                backup_file.writestr("Members.btx", members_text)
            added_file = data_dir / "Temp Files" / "AddedMembers.btx"
            added_file.parent.mkdir(parents=True, exist_ok=True)
            added_file.write_text(
                "\n".join([
                    "MN=34933",
                    "LN=A",
                    "FN=Bn",
                    "MTN=1 WEEK PASS",
                    "-",
                    "MN=34934",
                    "LN=New",
                    "FN=Member",
                    "MTN=1 WEEK PASS",
                    "-",
                    "",
                ]),
                encoding="latin-1",
            )
            os.utime(backup, (1000, 1000))
            os.utime(added_file, (2000, 2000))

            payload = build_sync_payload(root)

        by_id = {member["member_id"]: member for member in payload["members"]}
        self.assertEqual(by_id["34933"]["name"], "Wissenmansen, Dennis")
        self.assertEqual(by_id["34933"]["email"], "dennis@example.com")
        self.assertEqual(by_id["34934"]["name"], "New, Member")

    def test_partial_live_member_log_preserves_existing_contact_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            members_text = "\n".join([
                "MN=34848",
                "LN=Alvarez Sanchez",
                "FN=Claudia",
                "MTN=no contract 1 month Dreamz",
                "EM=claudia03752011@hotmail.com",
                "PM=795-6175",
                "$B=0",
                "-",
                "",
            ])
            with zipfile.ZipFile(backup, "w") as backup_file:
                backup_file.writestr("Members.btx", members_text)
            log_file = data_dir / "Temp Files" / "Member Updates" / "EditMembers 2026-05-26.txt"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                "2026/05/26 10:20:00|FRONTDESK|Gym Assistant|MN=34848\t$B=500\t-\t\n",
                encoding="latin-1",
            )
            os.utime(backup, (1000, 1000))
            os.utime(log_file, (2000, 2000))

            payload = build_sync_payload(root)

        member = next(member for member in payload["members"] if member["member_id"] == "34848")
        self.assertEqual(member["email"], "claudia03752011@hotmail.com")
        self.assertEqual(member["mobile"], "795-6175")
        self.assertEqual(member["balance"], 5.0)

    def test_payment_log_updates_last_paid_without_overwriting_billing_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            data_dir = root / "Data"
            backup_dir = data_dir / "Backup"
            backup_dir.mkdir(parents=True)
            backup = backup_dir / "GABackup-test.gbu"
            members_text = "\n".join([
                "MN=18659",
                "LN=De Aquino",
                "FN=Derick",
                "MTN=contract Dreamz 6 months",
                "R$=6500",
                "N$=6500",
                "$B=0",
                "-",
                "",
            ])
            with zipfile.ZipFile(backup, "w") as backup_file:
                backup_file.writestr("Members.btx", members_text)
            log_file = data_dir / "Temp Files" / "Member Updates" / "EditMembers 2026-05-26.txt"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                "2026/05/26 10:20:00|FRONTDESK|Gym Assistant|MN=18659\tLP=20260526\tR$=400\tN$=6500\t$B=0\t-\t\n",
                encoding="latin-1",
            )
            os.utime(backup, (1000, 1000))
            os.utime(log_file, (2000, 2000))

            payload = build_sync_payload(root)

        member = next(member for member in payload["members"] if member["member_id"] == "18659")
        self.assertEqual(member["billing_amount"], 65.0)
        self.assertEqual(member["last_payment_amount"], 4.0)

    def test_manifest_diff_detects_added_changed_and_removed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            contract = attachment_dir / "contract.pdf"
            contract.write_bytes(b"%PDF contract")

            first_scan = scan_source(root)
            manifest = Path(tmp) / "manifest.json"
            save_manifest(first_scan, manifest)
            loaded = load_manifest(manifest)

            contract.write_bytes(b"%PDF changed contract")
            (attachment_dir / "mandate.pdf").write_bytes(b"%PDF mandate")
            second_scan = scan_source(root)
            diff = diff_manifest(second_scan, loaded)

            self.assertIn("Data/Attachments/0000100/contract.pdf", diff.changed)
            self.assertIn("Data/Attachments/0000100/mandate.pdf", diff.added)
            self.assertEqual(diff.removed, [])

    def test_build_sync_payload_serializes_members_and_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")

            payload = build_sync_payload(root)

            self.assertEqual(payload["members"][0]["member_id"], "100")
            self.assertIn("generated_at", payload)
            self.assertIn("100", payload["documents"])
            self.assertEqual(payload["documents"]["100"][0]["document_type"], "contract")

    def test_build_sync_payload_can_upload_documents_and_photos(self):
        uploaded = []

        def fake_upload(path, key, bucket=None, client=None):
            uploaded.append((Path(path).name, key))
            return f"s3://dreamz-test/{key}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            with patch("sync_agent.s3_client", return_value=object()), patch("sync_agent.upload_file_to_s3", side_effect=fake_upload):
                payload = build_sync_payload(root, upload_files=True, storage_prefix="portal")

        self.assertIn(("contract.pdf", "portal/Data/Attachments/0000100/contract.pdf"), uploaded)
        self.assertIn(("0000100.jpg", PHOTO_VERSIONED_KEY), uploaded)
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")
        self.assertEqual(payload["members"][0]["photo_path"], f"s3://dreamz-test/{PHOTO_VERSIONED_KEY}")

    def test_build_sync_payload_can_upload_files_through_portal(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((url, token, Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertIn(("https://portal.example/api/sync/files", "sync-token", "contract.pdf", "portal/Data/Attachments/0000100/contract.pdf"), uploaded)
        self.assertIn(("https://portal.example/api/sync/files", "sync-token", "0000100.jpg", PHOTO_VERSIONED_KEY), uploaded)
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")
        self.assertEqual(payload["members"][0]["photo_path"], f"s3://dreamz-test/{PHOTO_VERSIONED_KEY}")

    def test_build_sync_payload_can_upload_only_changed_files_through_portal(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    upload_changed_only=True,
                    changed_file_paths={"Data/Pictures/0000100.jpg"},
                    existing_member_ids={"100"},
                    storage_bucket="dreamz-test",
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertEqual(uploaded, [("0000100.jpg", PHOTO_VERSIONED_KEY)])
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")
        self.assertEqual(payload["members"][0]["photo_path"], f"s3://dreamz-test/{PHOTO_VERSIONED_KEY}")

    def test_build_sync_payload_omits_unchanged_existing_photo_path(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    upload_changed_only=True,
                    changed_file_paths=set(),
                    existing_member_ids={"100"},
                    storage_bucket="dreamz-test",
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertEqual(uploaded, [])
        self.assertNotIn("photo_path", payload["members"][0])

    def test_build_sync_payload_uploads_photo_without_attachments_directory(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            photo_dir = root / "Data" / "Pictures"
            backup_dir.mkdir(parents=True)
            photo_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu")
            (photo_dir / "0000100.jpg").write_bytes(b"photo")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertEqual(uploaded, [("0000100.jpg", PHOTO_VERSIONED_KEY)])
        self.assertEqual(payload["members"][0]["photo_path"], f"s3://dreamz-test/{PHOTO_VERSIONED_KEY}")

    def test_build_sync_payload_uploads_new_member_files_even_when_unchanged_only(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000101"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu", member_id="101")
            (attachment_dir / "inscrip form 2026-05-25.pdf").write_bytes(b"%PDF signup")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    upload_changed_only=True,
                    changed_file_paths=set(),
                    existing_member_ids={"100"},
                    storage_bucket="dreamz-test",
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertEqual(uploaded, [("inscrip form 2026-05-25.pdf", "portal/Data/Attachments/0000101/inscrip form 2026-05-25.pdf")])
        self.assertEqual(payload["documents"]["101"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000101/inscrip form 2026-05-25.pdf")

    def test_build_sync_payload_uploads_missing_storage_files_even_for_existing_members(self):
        uploaded = []

        def fake_post_file(url, token, path, key):
            uploaded.append((Path(path).name, key))
            return {"uri": f"s3://dreamz-test/{key}"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            backup_dir = root / "Data" / "Backup"
            attachment_dir = root / "Data" / "Attachments" / "0000100"
            backup_dir.mkdir(parents=True)
            attachment_dir.mkdir(parents=True)
            write_backup(backup_dir / "GABackup-test.gbu", member_id="100")
            (attachment_dir / "contract.pdf").write_bytes(b"%PDF contract")

            with patch("sync_agent.post_file", side_effect=fake_post_file):
                payload = build_sync_payload(
                    root,
                    upload_files=True,
                    upload_via_portal=True,
                    upload_changed_only=True,
                    changed_file_paths=set(),
                    existing_member_ids={"100"},
                    missing_file_keys={"portal/Data/Attachments/0000100/contract.pdf"},
                    storage_bucket="dreamz-test",
                    portal_url="https://portal.example",
                    sync_token="sync-token",
                    storage_prefix="portal",
                )

        self.assertEqual(uploaded, [("contract.pdf", "portal/Data/Attachments/0000100/contract.pdf")])
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")

    def test_process_fep_payment_updates_posts_writer_success(self):
        update = {"id": 7, "member_id": "34203", "target_values": {"next_payment": "2026-08-01"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_updates", return_value=[update]) as get_updates,
                patch("sync_agent.run_fep_payment_writer", return_value={"status": "applied", "writer": "unit-test"}) as writer,
                patch("sync_agent.post_fep_payment_update_result", return_value={"ok": True}) as post_result,
            ):
                result = process_fep_payment_updates(root, "https://portal.example", "sync-token", "writer-cmd", agent_id="ron_laptop")

        self.assertEqual(result, {"received": 1, "applied": 1, "failed": 0, "deferred": 0})
        get_updates.assert_called_once_with("https://portal.example", "sync-token", limit=50, agent_id="ron_laptop")
        writer.assert_called_once_with("writer-cmd", root, update)
        post_result.assert_called_once_with(
            "https://portal.example",
            "sync-token",
            7,
            {"status": "applied", "writer": "unit-test"},
            agent_id="ron_laptop",
        )

    def test_process_fep_payment_updates_posts_writer_failure(self):
        update = {"id": 8, "member_id": "34203", "target_values": {"next_payment": "2026-08-01"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_updates", return_value=[update]),
                patch("sync_agent.run_fep_payment_writer", side_effect=RuntimeError("writer not configured")),
                patch("sync_agent.post_fep_payment_update_result", return_value={"ok": True}) as post_result,
            ):
                result = process_fep_payment_updates(root, "https://portal.example", "sync-token", "writer-cmd")

        self.assertEqual(result, {"received": 1, "applied": 0, "failed": 1, "deferred": 0})
        post_result.assert_called_once_with(
            "https://portal.example",
            "sync-token",
            8,
            {"status": "failed", "error": "writer not configured"},
            agent_id="frontdesk_dreamz",
        )

    def test_process_fep_payment_updates_reports_deferred_to_release_claim(self):
        update = {"id": 9, "member_id": "34203", "target_values": {"next_payment": "2026-08-01"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_updates", return_value=[update]),
                patch("sync_agent.run_fep_payment_writer", return_value={"status": "deferred", "reason": "desktop_not_idle"}),
                patch("sync_agent.post_fep_payment_update_result", return_value={"ok": True}) as post_result,
            ):
                result = process_fep_payment_updates(root, "https://portal.example", "sync-token", "writer-cmd")

        self.assertEqual(result, {"received": 1, "applied": 0, "failed": 0, "deferred": 1})
        post_result.assert_called_once_with(
            "https://portal.example",
            "sync-token",
            9,
            {"status": "deferred", "reason": "desktop_not_idle"},
            agent_id="frontdesk_dreamz",
        )

    def test_process_fep_payment_command_claims_and_reports_summary(self):
        command = {"id": 42, "requested_limit": 2, "target_agent": "frontdesk_dreamz"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_process_command", return_value=command) as get_command,
                patch("sync_agent.process_fep_payment_updates", return_value={"received": 2, "applied": 2, "failed": 0, "deferred": 0}) as process_updates,
                patch("sync_agent.post_fep_payment_process_command_result", return_value={"ok": True}) as post_command,
            ):
                result = process_fep_payment_command(root, "https://portal.example", "sync-token", "writer-cmd")

        self.assertEqual(result, {"claimed": True, "command_id": 42, "received": 2, "applied": 2, "failed": 0, "deferred": 0})
        get_command.assert_called_once_with("https://portal.example", "sync-token", agent_id="frontdesk_dreamz")
        process_updates.assert_called_once_with(
            root,
            "https://portal.example",
            "sync-token",
            "writer-cmd",
                limit=2,
                agent_id="frontdesk_dreamz",
            )
        post_command.assert_called_once()
        self.assertEqual(post_command.call_args.args[:3], ("https://portal.example", "sync-token", 42))
        self.assertEqual(post_command.call_args.args[3]["status"], "completed")

    def test_process_fep_payment_command_processes_large_command_in_chunks(self):
        command = {"id": 43, "requested_limit": 150, "target_agent": "frontdesk_dreamz"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_process_command", return_value=command),
                patch(
                    "sync_agent.process_fep_payment_updates",
                    side_effect=[
                        {"received": 100, "applied": 98, "failed": 1, "deferred": 1},
                        {"received": 20, "applied": 20, "failed": 0, "deferred": 0},
                        {"received": 0, "applied": 0, "failed": 0, "deferred": 0},
                    ],
                ) as process_updates,
                patch("sync_agent.post_fep_payment_process_command_result", return_value={"ok": True}) as post_command,
            ):
                result = process_fep_payment_command(root, "https://portal.example", "sync-token", "writer-cmd")

        self.assertEqual(result, {"claimed": True, "command_id": 43, "received": 120, "applied": 118, "failed": 1, "deferred": 1})
        self.assertEqual([call.kwargs["limit"] for call in process_updates.call_args_list], [100, 50, 30])
        self.assertEqual(post_command.call_args.args[3]["summary"]["received"], 120)

    def test_process_fep_payment_command_noops_without_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with (
                patch("sync_agent.get_fep_payment_process_command", return_value=None),
                patch("sync_agent.process_fep_payment_updates") as process_updates,
            ):
                result = process_fep_payment_command(root, "https://portal.example", "sync-token", "writer-cmd")

        self.assertEqual(result["status"], "idle")
        self.assertFalse(result["claimed"])
        process_updates.assert_not_called()

    def test_main_processes_fep_payments_before_scanning_source(self):
        calls = []

        def fake_process(*args, **kwargs):
            calls.append("payments")
            return {"received": 0, "applied": 0, "failed": 0, "deferred": 0}

        def fake_scan(source_root):
            calls.append("scan")
            return object()

        with (
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
                    "--agent-id",
                    "ron_laptop",
                    "--process-fep-payments",
                    "--fep-payment-writer",
                    "writer-cmd",
                ],
            ),
            patch("sync_agent.process_fep_payment_updates", side_effect=fake_process),
            patch("sync_agent.scan_source", side_effect=fake_scan),
            patch("sync_agent.load_manifest", return_value=None),
            patch("sync_agent.diff_manifest", return_value=object()),
            patch("sync_agent.print_scan_report"),
            patch("builtins.print"),
        ):
            main()

        self.assertEqual(calls[:2], ["payments", "scan"])

    def test_main_processes_fep_command_before_scanning_source(self):
        calls = []

        def fake_process(*args, **kwargs):
            calls.append("command")
            return {"claimed": False, "status": "idle", "received": 0, "applied": 0, "failed": 0, "deferred": 0}

        def fake_scan(source_root):
            calls.append("scan")
            return object()

        with (
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
                    "--agent-id",
                    "frontdesk_dreamz",
                    "--process-fep-command",
                    "--fep-payment-writer",
                    "writer-cmd",
                ],
            ),
            patch("sync_agent.process_fep_payment_command", side_effect=fake_process),
            patch("sync_agent.scan_source", side_effect=fake_scan),
            patch("sync_agent.load_manifest", return_value=None),
            patch("sync_agent.diff_manifest", return_value=object()),
            patch("sync_agent.print_scan_report"),
            patch("builtins.print"),
        ):
            main()

        self.assertEqual(calls[:2], ["command", "scan"])

    def test_main_continues_member_sync_when_fep_command_check_fails(self):
        calls = []

        def fake_scan(source_root):
            calls.append("scan")
            return object()

        with (
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
                    "--agent-id",
                    "frontdesk_dreamz",
                    "--process-fep-command",
                    "--fep-payment-writer",
                    "writer-cmd",
                ],
            ),
            patch("sync_agent.process_fep_payment_command", side_effect=RuntimeError("portal timeout")),
            patch("sync_agent.scan_source", side_effect=fake_scan),
            patch("sync_agent.load_manifest", return_value=None),
            patch("sync_agent.diff_manifest", return_value=object()),
            patch("sync_agent.print_scan_report"),
            patch("builtins.print") as printed,
        ):
            main()

        self.assertEqual(calls, ["scan"])
        printed_text = "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("FEP payment command response:", printed_text)
        self.assertIn("skipped", printed_text)

    def test_run_fep_payment_writer_uses_json_error_message(self):
        completed = subprocess.CompletedProcess(
            args=["writer"],
            returncode=1,
            stdout="",
            stderr='{"status":"failed","error":"manual payment review required"}',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            root.mkdir()
            with patch("sync_agent.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "manual payment review required"):
                    run_fep_payment_writer("writer", root, {"member_id": "34203"})


if __name__ == "__main__":
    unittest.main()
