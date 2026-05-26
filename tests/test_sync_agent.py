from pathlib import Path
import tempfile
import unittest
import os
import zipfile
from unittest.mock import patch

from sync_agent import build_sync_payload, diff_manifest, load_manifest, save_manifest, scan_source


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
        f"MN={member_id}",
        "LN=Tester",
        "FN=Live",
        "MTN=week pass",
        "EM=live@example.com",
        "-",
        "",
    ]), encoding="latin-1")


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

    def test_scan_source_warns_when_live_members_dat_is_newer_than_backup(self):
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
        self.assertIn("Members.dat is newer", scan.warning)
        self.assertIn("Members.dat is newer", payload["warning"])
        self.assertIn("Data/Members.dat", [item.path for item in scan.files])

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
        self.assertIn(("0000100.jpg", "portal/Data/Pictures/0000100.jpg"), uploaded)
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")
        self.assertEqual(payload["members"][0]["photo_path"], "s3://dreamz-test/portal/Data/Pictures/0000100.jpg")

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
        self.assertIn(("https://portal.example/api/sync/files", "sync-token", "0000100.jpg", "portal/Data/Pictures/0000100.jpg"), uploaded)
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")

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

        self.assertEqual(uploaded, [("0000100.jpg", "portal/Data/Pictures/0000100.jpg")])
        self.assertEqual(payload["documents"]["100"][0]["path"], "s3://dreamz-test/portal/Data/Attachments/0000100/contract.pdf")
        self.assertEqual(payload["members"][0]["photo_path"], "s3://dreamz-test/portal/Data/Pictures/0000100.jpg")

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


if __name__ == "__main__":
    unittest.main()
