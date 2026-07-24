from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader

from invoice_pdf import InvoicePdfData, InvoicePdfLineData, build_paid_invoice_pdf


def labels(**overrides: str) -> dict[str, str]:
    values = {
        "title": "FACTUUR",
        "paid": "BETAALD",
        "issuer": "Uitgever",
        "crib": "CRIB",
        "invoice_number": "Factuurnummer",
        "issue_date": "Factuurdatum",
        "payment_date": "Betaaldatum",
        "member": "Lid",
        "member_id": "Klantnummer",
        "membership": "Lidmaatschap",
        "service_period": "Serviceperiode",
        "description": "Omschrijving",
        "amount": "Bedrag",
        "subtotal": "Subtotaal excl. ABB",
        "abb": "ABB ({rate}%, inbegrepen)",
        "total": "Totaal",
        "payment_details": "Betalingsgegevens",
        "payment_method": "Betaalmethode",
        "payment_reference": "Betalingsreferentie",
        "confirmation_note": "Deze factuur bevestigt uitsluitend de vermelde fitnessdiensten.",
        "generated_note": "Digitaal gegenereerd door Dreamz Fitness Bonaire",
        "page": "Pagina",
    }
    values.update(overrides)
    return values


def invoice_data(*, lines: tuple[InvoicePdfLineData, ...] | None = None) -> InvoicePdfData:
    return InvoicePdfData(
        invoice_number="DF-PILOT-0001",
        issue_date=date(2026, 2, 15),
        member_id="90001",
        member_name="Pilot Member",
        member_email="pilot.member@example.com",
        membership_name="Dreamz 6 months",
        lines=lines
        or (
            InvoicePdfLineData("Lidmaatschap Dreamz 6 maanden", Decimal("50.00")),
            InvoicePdfLineData("Group PT", Decimal("15.00")),
        ),
        service_period_start=date(2026, 2, 1),
        service_period_end=date(2026, 5, 31),
        payment_date=date(2026, 2, 15),
        payment_method="CC-MANUAL",
        payment_reference="GA-990792",
        currency="USD",
        subtotal=Decimal("61.32"),
        abb_rate=Decimal("0.06"),
        abb_amount=Decimal("3.68"),
        total=Decimal("65.00"),
        legal_name="ABC Fitness & Health Bonaire N.V.",
        trade_name="Dreamz Fitness Bonaire",
        address_lines=("Example Address 1", "Kralendijk, Bonaire"),
        crib_number="303065217",
        contact_email="info@dreamzfitness.app",
        contact_phone="+599 777 0000",
    )


def extracted_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extracted_lines(pdf_bytes: bytes) -> list[str]:
    return [line.strip() for line in extracted_text(pdf_bytes).splitlines() if line.strip()]


class InvoicePdfTests(unittest.TestCase):
    def test_renders_multiple_line_items_with_individual_amounts(self):
        pdf_bytes = build_paid_invoice_pdf(invoice_data(), labels())

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        text = extracted_text(pdf_bytes)
        self.assertIn("Lidmaatschap Dreamz 6 maanden", text)
        self.assertIn("Group PT", text)
        self.assertIn("$50.00", text)
        self.assertIn("$15.00", text)
        self.assertIn("$65.00", text)
        self.assertIn("01-02-2026 - 31-05-2026", text)
        self.assertIn("303065217", text)

    def test_paid_badge_is_present_only_when_caller_supplies_paid_label(self):
        draft_labels = labels()
        draft_labels.pop("paid")

        draft_lines = extracted_lines(build_paid_invoice_pdf(invoice_data(), draft_labels))
        paid_lines = extracted_lines(build_paid_invoice_pdf(invoice_data(), labels()))

        self.assertNotIn("BETAALD", draft_lines)
        self.assertIn("BETAALD", paid_lines)

    def test_four_language_label_sets_render_without_hardcoded_invoice_language(self):
        language_labels = {
            "en": labels(
                title="INVOICE",
                paid="PAID",
                member="Member",
                description="Description",
                total="Total",
            ),
            "nl": labels(title="FACTUUR", paid="BETAALD"),
            "pap": labels(
                title="FAKTURA",
                paid="PAGÁ",
                member="Miembro",
                description="Deskripshon",
                total="Total",
            ),
            "es": labels(
                title="FACTURA",
                paid="PAGADA",
                member="Socio",
                description="Descripción",
                total="Total",
            ),
        }

        for language, localized_labels in language_labels.items():
            with self.subTest(language=language):
                text = extracted_text(build_paid_invoice_pdf(invoice_data(), localized_labels))
                self.assertIn(localized_labels["title"], text)
                self.assertIn(localized_labels["paid"], text)
                self.assertIn(localized_labels["description"].upper(), text)


if __name__ == "__main__":
    unittest.main()
