from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Mapping, Sequence
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


BRAND_GOLD = colors.HexColor("#E5B547")
BRAND_INK = colors.HexColor("#18222D")
MUTED_INK = colors.HexColor("#5D6874")
SOFT_BACKGROUND = colors.HexColor("#F5F7F9")
SOFT_BORDER = colors.HexColor("#DDE3E8")
SUCCESS = colors.HexColor("#177245")
GOLD_SOFT = colors.HexColor("#FFF7E5")


def _invoice_font_names() -> tuple[str, str]:
    """Use ReportLab's bundled Vera font for a portable, modern sans-serif."""
    regular_name = "DreamzInvoiceSans"
    bold_name = "DreamzInvoiceSansBold"
    registered = set(pdfmetrics.getRegisteredFontNames())
    font_root = Path(reportlab.__file__).resolve().parent / "fonts"
    regular_path = font_root / "Vera.ttf"
    bold_path = font_root / "VeraBd.ttf"
    if not regular_path.is_file() or not bold_path.is_file():
        return "Helvetica", "Helvetica-Bold"
    if regular_name not in registered:
        pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
    if bold_name not in registered:
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
    pdfmetrics.registerFontFamily(
        regular_name,
        normal=regular_name,
        bold=bold_name,
        italic=regular_name,
        boldItalic=bold_name,
    )
    return regular_name, bold_name


FONT_REGULAR, FONT_BOLD = _invoice_font_names()


@dataclass(frozen=True)
class InvoicePdfLineData:
    description: str
    amount: Decimal


@dataclass(frozen=True)
class InvoicePdfData:
    invoice_number: str
    issue_date: date
    member_id: str
    member_name: str
    member_email: str | None
    membership_name: str
    lines: Sequence[InvoicePdfLineData]
    service_period_start: date
    service_period_end: date
    payment_date: date
    payment_method: str
    payment_reference: str | None
    currency: str
    subtotal: Decimal
    abb_rate: Decimal
    abb_amount: Decimal
    total: Decimal
    legal_name: str
    trade_name: str
    address_lines: Sequence[str]
    crib_number: str | None
    contact_email: str | None
    contact_phone: str | None


def _date(value: date) -> str:
    return value.strftime("%d-%m-%Y")


def _money(currency: str, value: Decimal) -> str:
    symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
    return f"{symbol}{Decimal(value):,.2f}"


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(value or "")), style)


def build_paid_invoice_pdf(
    data: InvoicePdfData,
    labels: Mapping[str, str],
    *,
    logo_path: str | Path | None = None,
) -> bytes:
    """Build a compact, searchable invoice PDF from caller-provided language labels.

    The status badge is deliberately opt-in. It is rendered only when the caller
    supplies a non-empty ``paid`` label, so a draft cannot accidentally be marked
    as paid by the PDF renderer itself.
    """
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=f"{labels['title']} {data.invoice_number}",
        author=data.trade_name,
        subject=labels["confirmation_note"],
        pageCompression=1,
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "InvoiceNormal",
        parent=styles["BodyText"],
        fontName=FONT_REGULAR,
        fontSize=9.2,
        leading=13.2,
        textColor=BRAND_INK,
        spaceAfter=0,
    )
    small = ParagraphStyle(
        "InvoiceSmall",
        parent=normal,
        fontSize=7.7,
        leading=10.5,
        textColor=MUTED_INK,
    )
    label = ParagraphStyle(
        "InvoiceLabel",
        parent=small,
        fontName=FONT_BOLD,
        fontSize=7.1,
        leading=9.5,
        textColor=MUTED_INK,
    )
    table_header = ParagraphStyle(
        "InvoiceTableHeader",
        parent=label,
        textColor=colors.white,
    )
    table_header_right = ParagraphStyle(
        "InvoiceTableHeaderRight",
        parent=table_header,
        alignment=TA_RIGHT,
    )
    title = ParagraphStyle(
        "InvoiceTitle",
        parent=normal,
        fontName=FONT_BOLD,
        fontSize=21,
        leading=24,
        alignment=TA_RIGHT,
        textColor=BRAND_INK,
    )
    status = ParagraphStyle(
        "InvoiceStatus",
        parent=normal,
        fontName=FONT_BOLD,
        fontSize=8.5,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.white,
    )
    section_title = ParagraphStyle(
        "InvoiceSectionTitle",
        parent=normal,
        fontName=FONT_BOLD,
        fontSize=10.2,
        leading=14,
        textColor=BRAND_INK,
    )
    right = ParagraphStyle(
        "InvoiceRight",
        parent=normal,
        alignment=TA_RIGHT,
    )
    right_bold = ParagraphStyle(
        "InvoiceRightBold",
        parent=right,
        fontName=FONT_BOLD,
        fontSize=11.2,
    )
    metadata_value = ParagraphStyle(
        "InvoiceMetadataValue",
        parent=right,
        fontSize=8.8,
    )
    metadata_number = ParagraphStyle(
        "InvoiceMetadataNumber",
        parent=right,
        fontName=FONT_BOLD,
        fontSize=10.5,
        textColor=BRAND_INK,
    )

    story = []
    logo = None
    if logo_path:
        candidate = Path(logo_path)
        if candidate.is_file():
            logo = Image(str(candidate), width=49 * mm, height=17 * mm, kind="proportional")

    title_block = [_paragraph(labels["title"], title)]
    paid_label = str(labels.get("paid") or "").strip()
    if paid_label:
        title_block.extend(
            [
                Spacer(1, 2.5 * mm),
                Table(
                    [[_paragraph(paid_label, status)]],
                    colWidths=[29 * mm],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), SUCCESS),
                            ("BOX", (0, 0), (-1, -1), 0.5, SUCCESS),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                        ]
                    ),
                    hAlign="RIGHT",
                ),
            ]
        )

    header = Table(
        [[logo or _paragraph(data.trade_name, section_title), title_block]],
        colWidths=[92 * mm, 86 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    brand_rule = Table(
        [[""]],
        colWidths=[178 * mm],
        rowHeights=[1.2 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BRAND_GOLD),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    story.extend([header, Spacer(1, 4 * mm), brand_rule, Spacer(1, 7 * mm)])

    issuer_lines = [
        f"<b>{escape(data.legal_name)}</b>",
        escape(data.trade_name),
        *[escape(line) for line in data.address_lines if line],
    ]
    if data.crib_number:
        issuer_lines.append(f"{escape(labels['crib'])}: {escape(data.crib_number)}")
    if data.contact_email:
        issuer_lines.append(escape(data.contact_email))
    if data.contact_phone:
        issuer_lines.append(escape(data.contact_phone))

    metadata = Table(
        [
            [_paragraph(labels["invoice_number"].upper(), label), _paragraph(data.invoice_number, metadata_number)],
            [_paragraph(labels["issue_date"].upper(), label), _paragraph(_date(data.issue_date), metadata_value)],
            [_paragraph(labels["payment_date"].upper(), label), _paragraph(_date(data.payment_date), metadata_value)],
        ],
        colWidths=[36 * mm, 48 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), GOLD_SOFT),
                ("BOX", (0, 0), (-1, -1), 0.6, BRAND_GOLD),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.35, colors.HexColor("#E9D7AA")),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        ),
    )
    issuer = [
        _paragraph(labels["issuer"].upper(), label),
        Spacer(1, 2 * mm),
        Paragraph("<br/>".join(issuer_lines), normal),
    ]
    overview = Table(
        [[issuer, metadata]],
        colWidths=[90 * mm, 88 * mm],
        style=TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 10),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        ),
    )
    story.extend([overview, Spacer(1, 7 * mm)])

    member_lines = [
        f"<b>{escape(data.member_name)}</b>",
        f"{escape(labels['member_id'])}: {escape(data.member_id)}",
        f"{escape(labels['membership'])}: {escape(data.membership_name)}",
    ]
    if data.member_email:
        member_lines.append(escape(data.member_email))
    member_box = Table(
        [
            [
                [
                    _paragraph(labels["member"].upper(), label),
                    Spacer(1, 2 * mm),
                    Paragraph("<br/>".join(member_lines), normal),
                ],
                [
                    _paragraph(labels["service_period"].upper(), label),
                    Spacer(1, 2 * mm),
                    _paragraph(f"{_date(data.service_period_start)} - {_date(data.service_period_end)}", normal),
                ],
            ]
        ],
        colWidths=[108 * mm, 70 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SOFT_BACKGROUND),
                ("BOX", (0, 0), (-1, -1), 0.5, SOFT_BORDER),
                ("LINEBEFORE", (0, 0), (0, 0), 2.2, BRAND_GOLD),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 11),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        ),
    )
    story.extend([member_box, Spacer(1, 7 * mm)])

    line_rows = [
        [
            _paragraph(labels["description"].upper(), table_header),
            _paragraph(labels["amount"].upper(), table_header_right),
        ]
    ]
    line_rows.extend(
        [_paragraph(line.description, normal), _paragraph(_money(data.currency, line.amount), right)]
        for line in data.lines
    )
    line_items = Table(
        line_rows,
        colWidths=[138 * mm, 40 * mm],
        repeatRows=1,
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_INK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT_BACKGROUND]),
                ("LINEBELOW", (0, 1), (-1, -1), 0.5, SOFT_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 7),
                ("TOPPADDING", (0, 1), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 9),
            ]
        ),
    )
    story.extend([line_items, Spacer(1, 3 * mm)])

    totals = Table(
        [
            [_paragraph(labels["subtotal"], right), _paragraph(_money(data.currency, data.subtotal), right)],
            [
                _paragraph(
                    labels["abb"].format(rate=f"{data.abb_rate * 100:.2f}".rstrip("0").rstrip(".")),
                    right,
                ),
                _paragraph(_money(data.currency, data.abb_amount), right),
            ],
            [_paragraph(labels["total"], right_bold), _paragraph(_money(data.currency, data.total), right_bold)],
        ],
        colWidths=[42 * mm, 38 * mm],
        style=TableStyle(
            [
                ("LINEABOVE", (0, 2), (-1, 2), 1, BRAND_GOLD),
                ("BACKGROUND", (0, 2), (-1, 2), GOLD_SOFT),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 2), (-1, 2), 7),
                ("BOTTOMPADDING", (0, 2), (-1, 2), 7),
            ]
        ),
        hAlign="RIGHT",
    )
    story.extend([totals, Spacer(1, 7 * mm)])

    payment_rows = [
        [_paragraph(labels["payment_method"].upper(), label), _paragraph(data.payment_method, normal)],
        [_paragraph(labels["payment_date"].upper(), label), _paragraph(_date(data.payment_date), normal)],
    ]
    if data.payment_reference:
        payment_rows.append(
            [
                _paragraph(labels["payment_reference"].upper(), label),
                _paragraph(data.payment_reference, normal),
            ]
        )
    payment_table = Table(
        payment_rows,
        colWidths=[45 * mm, 133 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SOFT_BACKGROUND),
                ("BOX", (0, 0), (-1, -1), 0.5, SOFT_BORDER),
                ("LINEBELOW", (0, 0), (-1, -2), 0.35, SOFT_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        ),
    )
    confirmation_box = Table(
        [[_paragraph(labels["confirmation_note"], small)]],
        colWidths=[178 * mm],
        style=TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), GOLD_SOFT),
                ("LINEBEFORE", (0, 0), (0, 0), 2.2, BRAND_GOLD),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#E9D7AA")),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        ),
    )
    story.extend(
        [
            _paragraph(labels["payment_details"].upper(), section_title),
            Spacer(1, 2 * mm),
            payment_table,
            Spacer(1, 6 * mm),
            confirmation_box,
        ]
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(SOFT_BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, 12 * mm, A4[0] - 16 * mm, 12 * mm)
        canvas.setFillColor(MUTED_INK)
        canvas.setFont(FONT_REGULAR, 7.2)
        canvas.drawString(16 * mm, 7.5 * mm, f"{data.invoice_number}  |  {labels['generated_note']}")
        canvas.drawRightString(A4[0] - 16 * mm, 7.5 * mm, f"{labels['page']} {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
