from pathlib import Path
import tempfile
import unittest
import zipfile

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


if __name__ == "__main__":
    unittest.main()
