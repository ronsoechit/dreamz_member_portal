from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ga_documents import classify_document_types
from selected_document_uploader import (
    bind_payload,
    build_selected_document_plan,
    parse_member_ids,
    upload_selected_documents,
)


def write_members(path: Path, member_ids: list[str]) -> None:
    records = []
    for member_id in member_ids:
        records.extend(
            [
                f"MN={member_id}",
                "LN=Tester",
                "FN=Selected",
                "MTN=contract Dreamz 12 m",
                f"EM=member-{member_id}@example.com",
                "-",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="latin-1")


def add_pdf(root: Path, member_id: str, filename: str, contents: bytes = b"%PDF test") -> Path:
    path = root / "Data" / "Attachments" / member_id.zfill(7) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


class SelectedDocumentUploaderTests(unittest.TestCase):
    def test_operational_manifest_has_exact_14_members_and_24_classified_documents(self):
        manifest_path = (
            Path(__file__).resolve().parents[1]
            / "operations"
            / "selected-member-documents-20260725.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(len(manifest["expected_member_ids"]), 14)
        self.assertEqual(manifest["expected_document_count"], 24)
        self.assertEqual(len(manifest["documents"]), 24)
        identities = {
            (
                document["member_id"],
                document["document_type"],
                document["source_filename"],
            )
            for document in manifest["documents"]
        }
        self.assertEqual(len(identities), 24)
        self.assertEqual(
            {document["member_id"] for document in manifest["documents"]},
            set(manifest["expected_member_ids"]),
        )
        for document in manifest["documents"]:
            inferred_types = classify_document_types(
                Path(document["source_filename"])
            )
            inferred_type = (
                "combined_contract_mandate"
                if {"contract", "direct_debit_mandate"}.issubset(inferred_types)
                else next(iter(inferred_types))
            )
            self.assertEqual(document["document_type"], inferred_type)

    def test_parse_member_ids_accepts_comma_and_whitespace_but_rejects_duplicates(self):
        self.assertEqual(
            parse_member_ids(["00100, 101", "102\n103"]),
            ("100", "101", "102", "103"),
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            parse_member_ids("100, 0000100")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            parse_member_ids("100, nope")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            parse_member_ids("0")

    def test_plan_contains_only_exact_selected_members_and_their_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100", "101", "999"])
            add_pdf(root, "100", "inscrip form.pdf")
            add_pdf(root, "100", "contract.pdf")
            add_pdf(root, "101", "inscrip form.pdf")
            unselected_pdf = add_pdf(root, "999", "private contract.pdf")
            photo = root / "Data" / "Pictures" / "0000100.jpg"
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b"photo")

            plan = build_selected_document_plan(
                root,
                "100, 101",
                expected_document_count=3,
            )

        self.assertEqual(plan.member_ids, ("100", "101"))
        self.assertEqual(len(plan.documents), 3)
        self.assertEqual({document.member_id for document in plan.documents}, {"100", "101"})
        self.assertNotIn(unselected_pdf.name, {document.source_filename for document in plan.documents})
        self.assertTrue(all(document.source_path.suffix == ".pdf" for document in plan.documents))
        self.assertTrue(
            all(
                f"Data/Attachments/{document.member_id.zfill(7)}/"
                in document.storage_key.replace("\\", "/")
                for document in plan.documents
            )
        )

    def test_plan_fails_closed_when_member_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")

            with self.assertRaisesRegex(ValueError, "absent"):
                build_selected_document_plan(
                    root,
                    "100, 101",
                    expected_document_count=1,
                )

    def test_plan_fails_closed_when_any_selected_member_has_no_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100", "101"])
            add_pdf(root, "100", "contract.pdf")

            with self.assertRaisesRegex(ValueError, "101 has no PDF"):
                build_selected_document_plan(
                    root,
                    "100, 101",
                    expected_document_count=1,
                )

    def test_plan_fails_closed_instead_of_silently_omitting_non_pdf_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")
            image = (
                root
                / "Data"
                / "Attachments"
                / "0000100"
                / "scanned-mandate.jpg"
            )
            image.write_bytes(b"image")

            with self.assertRaisesRegex(ValueError, "non-PDF attachment"):
                build_selected_document_plan(
                    root,
                    "100",
                    expected_document_count=1,
                )

    def test_plan_requires_exact_document_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")
            add_pdf(root, "100", "inscrip form.pdf")

            with self.assertRaisesRegex(ValueError, "expected 1, found 2"):
                build_selected_document_plan(
                    root,
                    "100",
                    expected_document_count=1,
                )

    def test_plan_can_require_exact_manifest_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")
            manifest = Path(tmp) / "expected.json"
            manifest.write_text(
                json.dumps(
                    {
                        "expected_member_ids": ["100"],
                        "expected_document_count": 1,
                        "documents": [
                            {
                                "member_id": "100",
                                "document_type": "signup_form",
                                "source_filename": "inscrip form.pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "do not exactly match"):
                build_selected_document_plan(
                    root,
                    "100",
                    expected_document_count=1,
                    expected_manifest=manifest,
                )

    def test_upload_uses_only_file_and_member_documents_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100", "999"])
            add_pdf(root, "100", "contract.pdf", b"%PDF selected")
            add_pdf(root, "999", "private.pdf", b"%PDF unselected")
            plan = build_selected_document_plan(
                root,
                "100",
                expected_document_count=1,
            )

            uploaded_tasks = []

            def fake_upload(tasks, portal_url, token, workers):
                uploaded_tasks.extend(tasks)
                return {
                    key: f"s3://dreamz-test/{key}"
                    for key, _ in tasks
                }

            with (
                patch(
                    "selected_document_uploader.upload_files_parallel",
                    side_effect=fake_upload,
                ),
                patch(
                    "selected_document_uploader.post_json",
                    return_value={"ok": True, "updated": 1},
                ) as post,
            ):
                response = upload_selected_documents(
                    plan,
                    "https://portal.example",
                    "sync-token",
                    upload_workers=2,
                )

        self.assertEqual(response, {"ok": True, "updated": 1})
        self.assertEqual(len(uploaded_tasks), 1)
        self.assertEqual(uploaded_tasks[0][1].name, "contract.pdf")
        post.assert_called_once()
        endpoint, token, payload = post.call_args.args
        self.assertEqual(endpoint, "https://portal.example/api/sync/member-documents")
        self.assertEqual(token, "sync-token")
        self.assertEqual(payload["expected_member_ids"], ["100"])
        self.assertEqual(payload["expected_document_count"], 1)
        self.assertEqual(
            set(payload["documents"][0]),
            {
                "member_id",
                "document_type",
                "source_filename",
                "storage_uri",
                "sha256",
                "size_bytes",
            },
        )
        self.assertNotIn("members", payload)
        self.assertNotIn("invoices", payload)
        self.assertNotIn("photo_path", payload["documents"][0])

    def test_missing_uploaded_uri_stops_before_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")
            plan = build_selected_document_plan(
                root,
                "100",
                expected_document_count=1,
            )

            with (
                patch(
                    "selected_document_uploader.upload_files_parallel",
                    return_value={},
                ),
                patch("selected_document_uploader.post_json") as post,
            ):
                with self.assertRaisesRegex(RuntimeError, "did not return an S3 URI"):
                    upload_selected_documents(
                        plan,
                        "https://portal.example",
                        "sync-token",
                    )

        post.assert_not_called()

    def test_bind_payload_contains_no_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Gym Assistant 2.6"
            write_members(root / "Data" / "Members.btx", ["100"])
            add_pdf(root, "100", "contract.pdf")
            plan = build_selected_document_plan(
                root,
                "100",
                expected_document_count=1,
            )
            uploaded_uris = {
                plan.documents[0].storage_key: (
                    f"s3://dreamz-test/{plan.documents[0].storage_key}"
                )
            }

            payload = bind_payload(plan, uploaded_uris)

        serialized = json.dumps(payload)
        self.assertNotIn(str(root), serialized)
        self.assertNotIn("source_path", serialized)


if __name__ == "__main__":
    unittest.main()
