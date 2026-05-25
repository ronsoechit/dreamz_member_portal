from datetime import date
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


if importlib.util.find_spec("flask") is None or importlib.util.find_spec("flask_sqlalchemy") is None:
    raise unittest.SkipTest("Flask app dependencies are not installed in this Python runtime")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from dreamz_portal import Member, app, db  # noqa: E402
from import_members import infer_document_paths, infer_member_photo, load_member_overrides, sync_members  # noqa: E402


class MemberSyncTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_sync_members_inserts_gymassistant_fields(self):
        new, updated = sync_members(
            [
                {
                    "member_id": "1206",
                    "name": "Example, Member",
                    "plan_type": "contract Dreamz 12 m",
                    "contract_type": "12-months",
                    "billing_status": "ACTIVE",
                    "billing_option": "ACH",
                    "billing_type": "ACH",
                    "billing_amount": 55.0,
                    "due_date": date(2025, 5, 1),
                    "due_date_raw": "* 01/05/2025",
                    "due_date_marker": "*",
                    "next_payment": date(2025, 5, 1),
                    "start_date": date(2021, 10, 5),
                    "end_date": date(2022, 10, 5),
                    "signup_date": date(2021, 10, 5),
                    "last_payment": date(2025, 3, 29),
                    "last_payment_amount": 55.0,
                    "email": "member@example.com",
                    "balance": 0.0,
                }
            ],
            db,
            Member,
        )

        self.assertEqual((new, updated), (1, 0))
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.contract_type, "12-months")
        self.assertEqual(member.billing_status, "ACTIVE")
        self.assertEqual(member.billing_option, "ACH")
        self.assertEqual(member.billing_type, "ACH")
        self.assertEqual(member.due_date, date(2025, 5, 1))
        self.assertEqual(member.due_date_raw, "* 01/05/2025")
        self.assertEqual(member.due_date_marker, "*")

    def test_sync_members_updates_existing_member(self):
        db.session.add(Member(member_id="1206", name="Old Name", balance=0.0))
        db.session.commit()

        new, updated = sync_members(
            [{"member_id": "1206", "name": "New Name", "balance": 12.5}],
            db,
            Member,
        )

        self.assertEqual((new, updated), (0, 1))
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.name, "New Name")
        self.assertEqual(member.balance, 12.5)

    def test_infer_document_paths_finds_available_member_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            static_root = Path(tmp)
            (static_root / "forms").mkdir()
            (static_root / "contracts").mkdir()
            (static_root / "mandates").mkdir()
            (static_root / "forms" / "1206_signup_form.pdf").write_bytes(b"%PDF-1.4")
            (static_root / "contracts" / "1206_contract.pdf").write_bytes(b"%PDF-1.4")
            (static_root / "mandates" / "1206_mandate.pdf").write_bytes(b"%PDF-1.4")

            paths = infer_document_paths("1206", static_root=static_root)

        self.assertEqual(paths["form_path"], "forms/1206_signup_form.pdf")
        self.assertEqual(paths["contract_path"], "contracts/1206_contract.pdf")
        self.assertEqual(paths["mandate_path"], "mandates/1206_mandate.pdf")

    def test_infer_document_paths_finds_gymassistant_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            attachments_root = Path(tmp)
            member_dir = attachments_root / "0001206"
            member_dir.mkdir()
            pdf = member_dir / "CNTR + DD 2022-09-19.pdf"
            pdf.write_bytes(b"%PDF-1.4")

            paths = infer_document_paths(
                "1206",
                static_root=Path(tmp) / "empty-static",
                attachments_root=attachments_root,
            )

        self.assertEqual(paths["contract_path"], str(pdf))
        self.assertEqual(paths["mandate_path"], str(pdf))

    def test_sync_members_attaches_inferred_documents_without_overwriting_existing(self):
        db.session.add(
            Member(
                member_id="1206",
                name="Old Name",
                form_path="forms/custom_signup.pdf",
            )
        )
        db.session.commit()

        with patch(
            "import_members.infer_document_paths",
            return_value={
                "form_path": "forms/1206_signup_form.pdf",
                "contract_path": "contracts/1206_contract.pdf",
                "mandate_path": "mandates/1206_mandate.pdf",
            },
        ):
            new, updated = sync_members(
                [{"member_id": "1206", "name": "New Name"}],
                db,
                Member,
            )

        self.assertEqual((new, updated), (0, 1))
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.form_path, "forms/custom_signup.pdf")
        self.assertEqual(member.contract_path, "contracts/1206_contract.pdf")
        self.assertEqual(member.mandate_path, "mandates/1206_mandate.pdf")

    def test_sync_members_can_refresh_existing_document_paths(self):
        db.session.add(
            Member(
                member_id="1206",
                name="Old Name",
                contract_path="contracts/old_contract.pdf",
            )
        )
        db.session.commit()

        with patch(
            "import_members.infer_document_paths",
            return_value={"contract_path": r"D:\Data\Attachments\0001206\CNTR + DD.pdf"},
        ):
            sync_members(
                [{"member_id": "1206", "name": "New Name"}],
                db,
                Member,
                refresh_documents=True,
            )

        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.contract_path, r"D:\Data\Attachments\0001206\CNTR + DD.pdf")

    def test_sync_members_attaches_gymassistant_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            attachments_root = Path(tmp)
            member_dir = attachments_root / "0001206"
            member_dir.mkdir()
            pdf = member_dir / "CNTR + DD 2022-09-19.pdf"
            pdf.write_bytes(b"%PDF-1.4")

            new, updated = sync_members(
                [{"member_id": "1206", "name": "Example Member"}],
                db,
                Member,
                attachments_root=attachments_root,
            )

        self.assertEqual((new, updated), (1, 0))
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.contract_path, str(pdf))
        self.assertEqual(member.mandate_path, str(pdf))

    def test_infer_member_photo_uses_gymassistant_picture_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos_root = Path(tmp)
            photo = photos_root / "0001206.jpg"
            photo.write_bytes(b"fake-jpg")

            result = infer_member_photo("1206", photos_root)

        self.assertEqual(result, str(photo))

    def test_sync_members_attaches_gymassistant_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos_root = Path(tmp)
            photo = photos_root / "0001206.jpg"
            photo.write_bytes(b"fake-jpg")

            sync_members(
                [{"member_id": "1206", "name": "Example Member"}],
                db,
                Member,
                photos_root=photos_root,
            )

        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.photo_path, str(photo))

    def test_sync_members_can_split_combined_gymassistant_document(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attachments_root = root / "attachments"
            split_root = root / "cache"
            member_dir = attachments_root / "0001206"
            member_dir.mkdir(parents=True)
            pdf = member_dir / "CNTR + DD 2022-09-19.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.add_blank_page(width=72, height=72)
            with pdf.open("wb") as pdf_file:
                writer.write(pdf_file)

            sync_members(
                [{"member_id": "1206", "name": "Example Member"}],
                db,
                Member,
                attachments_root=attachments_root,
                split_root=split_root,
            )

            member = Member.query.filter_by(member_id="1206").one()
            self.assertEqual(member.contract_path, str(split_root / "0001206" / "contract.pdf"))
            self.assertEqual(member.mandate_path, str(split_root / "0001206" / "direct-debit-mandate.pdf"))
            self.assertTrue(Path(member.contract_path).exists())
            self.assertTrue(Path(member.mandate_path).exists())

    def test_sync_members_applies_member_date_overrides(self):
        new, updated = sync_members(
            [
                {
                    "member_id": "1206",
                    "name": "Example, Member",
                    "start_date": date(2021, 10, 5),
                    "end_date": date(2022, 10, 5),
                    "signup_date": date(2021, 10, 5),
                }
            ],
            db,
            Member,
            overrides={
                "1206": {
                    "start_date": date(2022, 9, 14),
                    "end_date": date(2023, 9, 14),
                    "signup_date": date(2022, 9, 14),
                }
            },
        )

        self.assertEqual((new, updated), (1, 0))
        member = Member.query.filter_by(member_id="1206").one()
        self.assertEqual(member.start_date, date(2022, 9, 14))
        self.assertEqual(member.end_date, date(2023, 9, 14))
        self.assertEqual(member.signup_date, date(2022, 9, 14))

    def test_load_member_overrides_parses_iso_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "member_overrides.json"
            path.write_text(
                '{"1206":{"start_date":"2022-09-14","start_date_raw":"14/09/2022"}}',
                encoding="utf-8",
            )

            overrides = load_member_overrides(path)

        self.assertEqual(overrides["1206"]["start_date"], date(2022, 9, 14))
        self.assertEqual(overrides["1206"]["start_date_raw"], "14/09/2022")


if __name__ == "__main__":
    unittest.main()
