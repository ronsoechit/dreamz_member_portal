from pathlib import Path
import tempfile
import unittest

from ga_documents import (
    classify_pdf,
    index_attachments_root,
    infer_member_document_records,
    infer_member_documents,
    member_attachment_folder,
    split_combined_contract_mandate,
)


class GymAssistantDocumentTests(unittest.TestCase):
    def make_pdf(self, path, pages=2):
        from pypdf import PdfWriter

        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=72, height=72)
        with path.open("wb") as pdf_file:
            writer.write(pdf_file)

    def make_two_page_pdf(self, path):
        self.make_pdf(path, pages=2)

    def test_member_attachment_folder_uses_gymassistant_padding(self):
        self.assertEqual(member_attachment_folder("1206"), "0001206")
        self.assertEqual(member_attachment_folder("17291"), "0017291")

    def test_classify_contract_and_direct_debit_combined_pdf(self):
        fields = classify_pdf(Path("CNTR + DD 2022-09-19.pdf"))

        self.assertIn("contract_path", fields)
        self.assertIn("mandate_path", fields)

    def test_infer_member_documents_from_attachment_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            member_dir = root / "0001206"
            member_dir.mkdir()
            pdf = member_dir / "CNTR + DD 2022-09-19.pdf"
            pdf.write_bytes(b"%PDF-1.4")

            documents = infer_member_documents("1206", root)

        self.assertEqual(documents["contract_path"], str(pdf))
        self.assertEqual(documents["mandate_path"], str(pdf))

    def test_index_attachments_root_reports_members_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "0001206").mkdir()
            (root / "0001206" / "CNTR + DD 2022-09-19.pdf").write_bytes(b"%PDF-1.4")
            (root / "0017291").mkdir()
            (root / "0017291" / "inscrip form 2024-07-01.pdf").write_bytes(b"%PDF-1.4")

            result = index_attachments_root(root)

        self.assertEqual(result.scanned_members, 2)
        self.assertEqual(result.scanned_files, 2)
        self.assertIn("1206", result.documents)
        self.assertIn("17291", result.documents)
        self.assertIn("form_path", result.documents["17291"])

    def test_split_combined_contract_mandate_splits_two_page_scan_by_page_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "CNTR + DD 2022-09-19.pdf"
            cache_root = root / "cache"
            self.make_two_page_pdf(source)

            result = split_combined_contract_mandate(
                "1206",
                {
                    "contract_path": str(source),
                    "mandate_path": str(source),
                },
                cache_root,
            )

            contract_path = Path(result["contract_path"])
            mandate_path = Path(result["mandate_path"])
            self.assertNotEqual(contract_path, mandate_path)
            self.assertTrue(contract_path.exists())
            self.assertTrue(mandate_path.exists())
            self.assertEqual(contract_path.name, "contract.pdf")
            self.assertEqual(mandate_path.name, "direct-debit-mandate.pdf")

    def test_split_combined_contract_mandate_splits_even_page_scan_in_halves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "CNTR + DD 2023-10-02.pdf"
            cache_root = root / "cache"
            self.make_pdf(source, pages=4)

            result = split_combined_contract_mandate(
                "1206",
                {
                    "contract_path": str(source),
                    "mandate_path": str(source),
                },
                cache_root,
            )

            self.assertNotEqual(result["contract_path"], str(source))
            self.assertNotEqual(result["mandate_path"], str(source))

    def test_unsplittable_combined_document_is_listed_as_combined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            member_dir = root / "0001206"
            member_dir.mkdir()
            source = member_dir / "Ccontract + Mandate 2024-04-15.pdf"
            self.make_pdf(source, pages=1)

            records = infer_member_document_records("1206", root, split_root=root / "cache")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["document_type"], "combined_contract_mandate")


if __name__ == "__main__":
    unittest.main()
