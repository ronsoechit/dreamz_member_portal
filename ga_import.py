from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET
import re
import zipfile


SPREADSHEET_NS = "urn:schemas-microsoft-com:office:spreadsheet"
SS_INDEX = f"{{{SPREADSHEET_NS}}}Index"

GA_COLUMNS = [
    "member_id",
    "name",
    "plan_type",
    "billing_status",
    "billing_option",
    "billing_amount",
    "due_date",
    "start_date",
    "end_date",
    "signup_date",
    "last_payment",
    "last_payment_amount",
    "email",
    "balance",
]

TEXT_SPANS = [
    ("member_id", 0, 10),
    ("name", 10, 34),
    ("plan_type", 34, 56),
    ("billing_status", 56, 71),
    ("billing_option", 71, 88),
    ("billing_amount", 88, 101),
    ("due_date", 101, 114),
    ("start_date", 114, 126),
    ("end_date", 126, 138),
    ("signup_date", 138, 153),
    ("last_payment", 153, 170),
    ("last_payment_amount", 170, 180),
    ("email", 180, 219),
    ("balance", 219, None),
]

DATE_FIELDS = {"due_date", "start_date", "end_date", "signup_date", "last_payment"}
MONEY_FIELDS = {"billing_amount", "last_payment_amount", "balance"}
BACKUP_DATE_FIELDS = {
    "CB": "start_date",
    "CE": "end_date",
    "SU": "signup_date",
    "LP": "last_payment",
    "PU": "due_date",
}


@dataclass(frozen=True)
class ParsedDate:
    value: date | None
    marker: str | None
    raw: str


@dataclass(frozen=True)
class ImportIssue:
    row_number: int
    field: str
    value: str
    message: str


@dataclass(frozen=True)
class ImportResult:
    members: list[dict]
    issues: list[ImportIssue]


def parse_money(raw: str | None) -> float:
    if raw is None:
        return 0.0
    value = raw.strip().replace(",", "")
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_ga_date(raw: str | None) -> ParsedDate:
    original = "" if raw is None else str(raw).strip()
    if not original:
        return ParsedDate(None, None, original)

    marker = None
    match = re.match(r"^([*a-zA-Z]+)\s+(.+)$", original)
    value_text = original
    if match:
        marker = match.group(1)
        value_text = match.group(2).strip()

    if value_text in {"00/00/0000", "0/0/0000"}:
        return ParsedDate(None, marker, original)

    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return ParsedDate(datetime.strptime(value_text, fmt).date(), marker, original)
        except ValueError:
            pass
    return ParsedDate(None, marker, original)


def parse_backup_date(raw: str | None) -> ParsedDate:
    original = "" if raw is None else str(raw).strip()
    if not original or original == "00000000":
        return ParsedDate(None, None, original)

    for fmt in ("%Y%m%d", "%Y%m%d!%H%M"):
        try:
            return ParsedDate(datetime.strptime(original, fmt).date(), None, original)
        except ValueError:
            pass
    return ParsedDate(None, None, original)


def parse_backup_money(raw: str | None) -> float:
    if raw is None:
        return 0.0
    value = raw.strip()
    if not value:
        return 0.0
    try:
        return int(value) / 100
    except ValueError:
        return parse_money(value)


def derive_contract_type(plan_type: str | None) -> str:
    plan = re.sub(r"\s+", " ", (plan_type or "").strip().lower())
    if "contract" in plan and re.search(r"\b6\b|6\s*mo|6\s*month", plan):
        return "6-months"
    if "contract" in plan and re.search(r"\b12\b|12\s*m|12\s*month", plan):
        return "12-months"
    return "No-Contract"


def normalize_member_row(row: list[str], row_number: int) -> tuple[dict | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    values = list(row[: len(GA_COLUMNS)])
    values.extend([""] * (len(GA_COLUMNS) - len(values)))

    member_id = values[0].strip()
    if not member_id.isdigit():
        return None, [
            ImportIssue(row_number, "member_id", values[0], "Skipped row without numeric member id")
        ]

    record: dict = {}
    for key, raw in zip(GA_COLUMNS, values):
        raw = "" if raw is None else str(raw).strip()
        if key in DATE_FIELDS:
            parsed = parse_ga_date(raw)
            record[key] = parsed.value
            record[f"{key}_raw"] = parsed.raw
            if parsed.marker:
                record[f"{key}_marker"] = parsed.marker
            if parsed.value is None and raw and raw not in {"00/00/0000", "0/0/0000"}:
                issues.append(ImportIssue(row_number, key, raw, "Date could not be parsed"))
        elif key in MONEY_FIELDS:
            record[key] = parse_money(raw)
        else:
            record[key] = raw or None

    record["member_id"] = member_id
    record["billing_type"] = record.get("billing_option")
    record["contract_type"] = derive_contract_type(record.get("plan_type"))
    record["next_payment"] = record.get("due_date")
    return record, issues


def normalize_backup_record(fields: dict[str, str], record_number: int) -> tuple[dict | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    member_id = (fields.get("MN") or "").strip()
    if not member_id.isdigit():
        return None, [
            ImportIssue(record_number, "member_id", member_id, "Skipped record without numeric member id")
        ]

    last_name = (fields.get("LN") or "").strip()
    first_name = (fields.get("FN") or "").strip()
    if last_name and first_name:
        name = f"{last_name}, {first_name}"
    else:
        name = last_name or first_name or None

    plan_type = (fields.get("MTN") or "").strip() or None
    billing_type = (fields.get("BT") or "").strip() or None
    billing_amount = parse_backup_money(fields.get("R$") or fields.get("N$"))
    last_payment_amount = parse_backup_money(fields.get("N$") or fields.get("R$"))
    balance = parse_backup_money(fields.get("$B"))

    record: dict = {
        "member_id": member_id,
        "name": name,
        "plan_type": plan_type,
        "contract_type": derive_contract_type(plan_type),
        "billing_option": billing_type,
        "billing_type": billing_type,
        "billing_status": "ACTIVE" if (fields.get("ST") or "0").strip() == "0" else "INACTIVE",
        "billing_amount": billing_amount,
        "last_payment_amount": last_payment_amount,
        "balance": balance,
        "email": (fields.get("EM") or "").strip() or None,
        "phone": (fields.get("PH") or "").strip() or None,
        "mobile": (fields.get("PM") or "").strip() or None,
        "is_active": (fields.get("ST") or "0").strip() == "0",
    }

    visits = (fields.get("TV") or "").strip()
    if visits.isdigit():
        record["visits"] = int(visits)

    birthdate = parse_backup_date(fields.get("BD"))
    record["birthdate"] = birthdate.value
    if birthdate.value is None and birthdate.raw and birthdate.raw != "00000000":
        issues.append(ImportIssue(record_number, "birthdate", birthdate.raw, "Date could not be parsed"))

    for backup_key, record_key in BACKUP_DATE_FIELDS.items():
        parsed = parse_backup_date(fields.get(backup_key))
        record[record_key] = parsed.value
        record[f"{record_key}_raw"] = parsed.raw
        if parsed.value is None and parsed.raw and parsed.raw != "00000000":
            issues.append(ImportIssue(record_number, record_key, parsed.raw, "Date could not be parsed"))

    record["next_payment"] = record.get("due_date")
    return record, issues


def _cell_text(cell: ET.Element) -> str:
    for child in cell:
        if child.tag.endswith("Data"):
            return (child.text or "").strip()
    return ""


def _row_values(row: ET.Element) -> list[str]:
    values: list[str] = []
    for cell in row:
        if not cell.tag.endswith("Cell"):
            continue
        ss_index = cell.attrib.get(SS_INDEX)
        if ss_index:
            target_index = int(ss_index) - 1
            while len(values) < target_index:
                values.append("")
        values.append(_cell_text(cell))
    return values


def parse_xml_spreadsheet(path: str | Path) -> ImportResult:
    root = ET.parse(path).getroot()
    issues: list[ImportIssue] = []
    members: list[dict] = []
    row_number = 0

    for row in root.iter():
        if not row.tag.endswith("Row"):
            continue
        row_number += 1
        values = _row_values(row)
        if not values or not any(values):
            continue
        member, row_issues = normalize_member_row(values, row_number)
        issues.extend(row_issues)
        if member:
            members.append(member)

    return ImportResult(members, issues)


def parse_report_txt(path: str | Path) -> ImportResult:
    issues: list[ImportIssue] = []
    members: list[dict] = []

    for row_number, line in enumerate(Path(path).read_text(encoding="latin-1").splitlines(), start=1):
        if not re.match(r"^\s*\d+\s+", line):
            continue
        values = [
            line[start:end].strip() if end is not None else line[start:].strip()
            for _, start, end in TEXT_SPANS
        ]
        member, row_issues = normalize_member_row(values, row_number)
        issues.extend(row_issues)
        if member:
            members.append(member)

    return ImportResult(members, issues)


def _parse_backup_text(text: str) -> ImportResult:
    issues: list[ImportIssue] = []
    members: list[dict] = []
    fields: dict[str, str] = {}
    record_number = 0
    in_member_record = False

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == "-":
            if in_member_record:
                record_number += 1
                member, row_issues = normalize_backup_record(fields, record_number)
                issues.extend(row_issues)
                if member:
                    members.append(member)
            fields = {}
            in_member_record = False
            continue

        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "MN":
            fields = {}
            in_member_record = True
        if in_member_record:
            fields[key] = value

    if in_member_record and fields:
        record_number += 1
        member, row_issues = normalize_backup_record(fields, record_number)
        issues.extend(row_issues)
        if member:
            members.append(member)

    return ImportResult(members, issues)


def parse_members_btx(path: str | Path) -> ImportResult:
    text = Path(path).read_text(encoding="latin-1")
    return _parse_backup_text(text)


def parse_gymassistant_backup(path: str | Path) -> ImportResult:
    path = Path(path)
    with zipfile.ZipFile(path) as backup:
        member_entry = next(
            (entry for entry in backup.namelist() if Path(entry).name.lower() == "members.btx"),
            None,
        )
        if not member_entry:
            raise ValueError(f"GymAssistant backup does not contain Members.btx: {path}")
        text = backup.read(member_entry).decode("latin-1", errors="replace")
    return _parse_backup_text(text)


def parse_gymassistant_export(path: str | Path) -> ImportResult:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".xls", ".xlsx", ".xml"}:
        return parse_xml_spreadsheet(path)
    if suffix in {".txt", ".prn"}:
        return parse_report_txt(path)
    if suffix == ".btx":
        return parse_members_btx(path)
    if suffix == ".gbu":
        return parse_gymassistant_backup(path)
    raise ValueError(f"Unsupported GymAssistant export type: {path.suffix}")
