from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import csv
from io import StringIO
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

OFFICIAL_CSV_REQUIRED_COLUMNS = {
    "MemberNum",
    "LastName",
    "FirstName",
    "MemberType",
    "BillingOption",
    "DueDate",
    "BillingAmount",
    "LastPaidDate",
    "LastPaidAmount",
    "SignupDate",
    "ContractEnd",
    "ContractBegin",
    "BirthDate",
    "Email",
    "BillingStatus",
    "CurrentBalance",
    "IsDeleted",
}

OFFICIAL_BILLING_OPTION_MAP = {
    "MONTHLY": "1 MONTHS INV",
    "ACH": "1 MONTHS EFT",
    "ANNUAL": "12 MONTHS INV",
    "SEMI-ANNUAL": "6 MONTHS INV",
    "1-WEEK INVOICE": "1 WEEKS INV",
    "2-WEEK INVOICE": "2 WEEKS INV",
    "3-WEEK INVOICE": "3 WEEKS INV",
    "5-MONTH INVOICE": "5 MONTHS INV",
    "QUARTERLY": "3 MONTHS INV",
    "10 VISITS": "10 VISITS INV",
}

SUPPORTED_BILLING_STATUSES = {
    "ACTIVE",
    "INACTIVE",
    "TERMINATED",
    "CANCELLED",
    "FREEZE",
    "HOLD",
    "PROSPECT",
    "ARCHIVE",
    "COLLECTIONS",
    "SUSPENDED",
    "DELETED",
    "INCOMPLETE",
}

BACKUP_BILLING_STATUS_CODES = {
    "0": "ACTIVE",
    "1": "INACTIVE",
    "2": "TERMINATED",
    "3": "FREEZE",
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
    critical: bool = False


@dataclass(frozen=True)
class ImportResult:
    members: list[dict]
    issues: list[ImportIssue]


@dataclass(frozen=True)
class MemberLogEvent:
    row_number: int
    occurred_at: datetime | None
    member: dict


@dataclass(frozen=True)
class MemberLogResult:
    events: list[MemberLogEvent]
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


def parse_backup_member_ids(raw: str | None) -> list[str]:
    return [member_id for member_id in re.findall(r"\d+", str(raw or "")) if int(member_id) > 0]


def derive_contract_type(plan_type: str | None) -> str:
    plan = re.sub(r"\s+", " ", (plan_type or "").strip().lower())
    if "contract" in plan and re.search(r"\b6\b|6\s*mo|6\s*month", plan):
        return "6-months"
    if "contract" in plan and re.search(r"\b12\b|12\s*m|12\s*month", plan):
        return "12-months"
    return "No-Contract"


def normalize_billing_status(
    raw: str | None,
    row_number: int,
    field: str = "billing_status",
) -> tuple[str, bool | None, list[ImportIssue]]:
    original = "" if raw is None else str(raw).strip()
    status = original.upper()
    if status in SUPPORTED_BILLING_STATUSES:
        return status, status == "ACTIVE", []
    message = (
        "Billing status is missing"
        if not status
        else "Billing status is not recognized"
    )
    return "UNKNOWN", None, [
        ImportIssue(row_number, field, original, message, critical=True)
    ]


def normalize_backup_billing_status(
    fields: dict[str, str],
    record_number: int,
    require_status: bool,
) -> tuple[str, bool | None, list[ImportIssue]]:
    if "ST" not in fields and not require_status:
        return "UNKNOWN", None, []
    raw_status = (fields.get("ST") or "").strip()
    status = BACKUP_BILLING_STATUS_CODES.get(raw_status)
    if status:
        return status, status == "ACTIVE", []
    message = (
        "GymAssistant ST status is missing"
        if not raw_status
        else "GymAssistant ST status code is not recognized"
    )
    return "UNKNOWN", None, [
        ImportIssue(record_number, "billing_status", raw_status, message, critical=True)
    ]


def normalize_official_billing_option(raw: str | None) -> str | None:
    value = re.sub(r"\s+", " ", str(raw or "").strip())
    if not value:
        return None
    return OFFICIAL_BILLING_OPTION_MAP.get(value.upper(), value)


def normalize_member_row(row: list[str], row_number: int) -> tuple[dict | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    values = list(row[: len(GA_COLUMNS)])
    values.extend([""] * (len(GA_COLUMNS) - len(values)))

    member_id = values[0].strip()
    if not member_id.isdigit() or int(member_id) <= 0:
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
    billing_status, is_active, status_issues = normalize_billing_status(
        record.get("billing_status"),
        row_number,
    )
    record["billing_status"] = billing_status
    record["is_active"] = is_active
    issues.extend(status_issues)
    record["billing_type"] = record.get("billing_option")
    record["contract_type"] = derive_contract_type(record.get("plan_type"))
    record["next_payment"] = record.get("due_date")
    return record, issues


def normalize_backup_record(
    fields: dict[str, str],
    record_number: int,
    require_status: bool = True,
) -> tuple[dict | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    member_id = (fields.get("MN") or "").strip()
    if not member_id.isdigit() or int(member_id) <= 0:
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
    billing_amount = parse_backup_money(fields.get("N$") or fields.get("R$"))
    last_payment_amount = parse_backup_money(fields.get("R$") or fields.get("N$"))
    balance = parse_backup_money(fields.get("$B"))
    billing_status, is_active, status_issues = normalize_backup_billing_status(
        fields,
        record_number,
        require_status=require_status,
    )
    issues.extend(status_issues)

    record: dict = {
        "member_id": member_id,
        "name": name,
        "plan_type": plan_type,
        "contract_type": derive_contract_type(plan_type),
        "billing_option": billing_type,
        "billing_type": billing_type,
        "billing_status": billing_status,
        "billing_amount": billing_amount,
        "last_payment_amount": last_payment_amount,
        "balance": balance,
        "email": (fields.get("EM") or "").strip() or None,
        "phone": (fields.get("PH") or "").strip() or None,
        "mobile": (fields.get("PM") or "").strip() or None,
        "is_active": is_active,
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
    dependent_member_ids = parse_backup_member_ids(fields.get("DPL"))
    if dependent_member_ids:
        record["dependent_member_ids"] = ",".join(dependent_member_ids)
    return record, issues


def normalize_backup_update_record(fields: dict[str, str], record_number: int) -> tuple[dict | None, list[ImportIssue]]:
    """Normalize a partial GymAssistant member update without inventing missing fields."""
    full_record, issues = normalize_backup_record(
        fields,
        record_number,
        require_status=False,
    )
    if not full_record:
        return None, issues

    record: dict = {"member_id": full_record["member_id"]}
    if "LN" in fields or "FN" in fields:
        record["name"] = full_record.get("name")
    if "MTN" in fields:
        record["plan_type"] = full_record.get("plan_type")
        record["contract_type"] = full_record.get("contract_type")
    if "BT" in fields:
        record["billing_option"] = full_record.get("billing_option")
        record["billing_type"] = full_record.get("billing_type")
    if "N$" in fields:
        record["billing_amount"] = parse_backup_money(fields.get("N$"))
    if "R$" in fields:
        record["last_payment_amount"] = parse_backup_money(fields.get("R$"))
    if "$B" in fields:
        record["balance"] = parse_backup_money(fields.get("$B"))
    if "EM" in fields:
        record["email"] = (fields.get("EM") or "").strip() or None
    if "PH" in fields:
        record["phone"] = (fields.get("PH") or "").strip() or None
    if "PM" in fields:
        record["mobile"] = (fields.get("PM") or "").strip() or None
    if "ST" in fields:
        record["billing_status"] = full_record.get("billing_status")
        record["is_active"] = full_record.get("is_active")
    if "TV" in fields and "visits" in full_record:
        record["visits"] = full_record.get("visits")
    if "BD" in fields:
        record["birthdate"] = full_record.get("birthdate")

    for backup_key, record_key in BACKUP_DATE_FIELDS.items():
        if backup_key not in fields:
            continue
        record[record_key] = full_record.get(record_key)
        record[f"{record_key}_raw"] = full_record.get(f"{record_key}_raw")
        if record_key == "due_date":
            record["next_payment"] = full_record.get("due_date")

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


def _decode_official_csv(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(
        f"GymAssistant CSV is not valid UTF-8 or Windows-1252 text: {path}"
    )


def _parse_csv_date(
    row: dict[str, str],
    source_field: str,
    target_field: str,
    row_number: int,
    record: dict,
    issues: list[ImportIssue],
) -> None:
    raw = str(row.get(source_field) or "").strip()
    parsed = parse_ga_date(raw)
    record[target_field] = parsed.value
    record[f"{target_field}_raw"] = parsed.raw
    if parsed.marker:
        record[f"{target_field}_marker"] = parsed.marker
    if parsed.value is None and raw and raw not in {"00/00/0000", "0/0/0000"}:
        issues.append(
            ImportIssue(
                row_number,
                target_field,
                raw,
                "Date could not be parsed",
            )
        )


def parse_official_member_csv(path: str | Path) -> ImportResult:
    path = Path(path)
    reader = csv.DictReader(StringIO(_decode_official_csv(path)), delimiter=",")
    fieldnames = [str(field or "").strip() for field in (reader.fieldnames or [])]
    missing_columns = sorted(OFFICIAL_CSV_REQUIRED_COLUMNS - set(fieldnames))
    if missing_columns:
        raise ValueError(
            "GymAssistant member CSV is missing required column(s): "
            + ", ".join(missing_columns)
        )

    members: list[dict] = []
    issues: list[ImportIssue] = []
    seen_member_ids: set[str] = set()
    for row_number, source_row in enumerate(reader, start=2):
        row = {
            str(key or "").strip(): "" if value is None else str(value).strip()
            for key, value in source_row.items()
        }
        if not any(row.values()):
            continue

        deleted_value = row.get("IsDeleted", "").strip().lower()
        if deleted_value in {"1", "true", "yes"}:
            continue
        if deleted_value not in {"0", "false", "no", ""}:
            issues.append(
                ImportIssue(
                    row_number,
                    "is_deleted",
                    row.get("IsDeleted", ""),
                    "IsDeleted value is not recognized",
                    critical=True,
                )
            )

        member_id = row.get("MemberNum", "").strip()
        if not member_id.isdigit() or int(member_id) <= 0:
            issues.append(
                ImportIssue(
                    row_number,
                    "member_id",
                    member_id,
                    "Skipped row without numeric member id",
                    critical=True,
                )
            )
            continue
        if member_id in seen_member_ids:
            issues.append(
                ImportIssue(
                    row_number,
                    "member_id",
                    member_id,
                    "Skipped duplicate member id",
                    critical=True,
                )
            )
            continue
        seen_member_ids.add(member_id)

        last_name = row.get("LastName", "").strip()
        first_name = row.get("FirstName", "").strip()
        name = (
            f"{last_name}, {first_name}"
            if last_name and first_name
            else last_name or first_name or None
        )
        plan_type = row.get("MemberType", "").strip() or None
        billing_option = normalize_official_billing_option(row.get("BillingOption"))
        billing_status, is_active, status_issues = normalize_billing_status(
            row.get("BillingStatus"),
            row_number,
        )
        issues.extend(status_issues)

        record: dict = {
            "member_id": member_id,
            "name": name,
            "plan_type": plan_type,
            "contract_type": derive_contract_type(plan_type),
            "billing_status": billing_status,
            "billing_option": billing_option,
            "billing_type": billing_option,
            "billing_amount": parse_money(row.get("BillingAmount")),
            "last_payment_amount": parse_money(row.get("LastPaidAmount")),
            "balance": parse_money(row.get("CurrentBalance")),
            "email": row.get("Email", "").strip() or None,
            "phone": row.get("HomePhone", "").strip() or None,
            "mobile": row.get("MobilePhone", "").strip() or None,
            "is_active": is_active,
        }

        for source_field, target_field in (
            ("DueDate", "due_date"),
            ("ContractBegin", "start_date"),
            ("ContractEnd", "end_date"),
            ("SignupDate", "signup_date"),
            ("LastPaidDate", "last_payment"),
            ("BirthDate", "birthdate"),
        ):
            _parse_csv_date(
                row,
                source_field,
                target_field,
                row_number,
                record,
                issues,
            )
        record["next_payment"] = record.get("due_date")

        visits = row.get("NUM_VISITS_TOTAL", "").strip()
        if visits:
            try:
                record["visits"] = int(float(visits))
            except ValueError:
                issues.append(
                    ImportIssue(
                        row_number,
                        "visits",
                        visits,
                        "Visit count could not be parsed",
                    )
                )

        responsible_id = row.get("ResponsibleMemberNum", "").strip()
        if responsible_id.isdigit() and int(responsible_id) > 0:
            record["responsible_member_id"] = responsible_id
        members.append(record)

    members_by_id = {
        str(member["member_id"]): member
        for member in members
        if member.get("member_id")
    }
    dependent_ids_by_responsible: dict[str, list[str]] = {}
    for member in members:
        responsible_id = str(member.get("responsible_member_id") or "")
        if responsible_id and responsible_id in members_by_id:
            dependent_ids_by_responsible.setdefault(responsible_id, []).append(
                str(member["member_id"])
            )
    for responsible_id, dependent_ids in dependent_ids_by_responsible.items():
        members_by_id[responsible_id]["dependent_member_ids"] = ",".join(
            sorted(set(dependent_ids), key=int)
        )

    return ImportResult(members, issues)


def attach_backup_member_relationships(members: list[dict], raw_records: list[dict[str, str]]) -> None:
    members_by_id = {str(member.get("member_id")): member for member in members if member.get("member_id")}
    for fields in raw_records:
        responsible_id = (fields.get("MN") or "").strip()
        if not responsible_id:
            continue
        dependent_ids = [
            dependent_id
            for dependent_id in parse_backup_member_ids(fields.get("DPL"))
            if dependent_id and dependent_id != responsible_id
        ]
        if dependent_ids and responsible_id in members_by_id:
            members_by_id[responsible_id]["dependent_member_ids"] = ",".join(dependent_ids)
        for dependent_id in dependent_ids:
            dependent = members_by_id.get(dependent_id)
            if dependent and not dependent.get("responsible_member_id"):
                dependent["responsible_member_id"] = responsible_id


def _parse_backup_text(text: str) -> ImportResult:
    issues: list[ImportIssue] = []
    members: list[dict] = []
    raw_records: list[dict[str, str]] = []
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
                    raw_records.append(dict(fields))
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
            raw_records.append(dict(fields))

    attach_backup_member_relationships(members, raw_records)
    return ImportResult(members, issues)


def parse_members_btx(path: str | Path) -> ImportResult:
    text = Path(path).read_text(encoding="latin-1")
    return _parse_backup_text(text)


def _parse_member_log_events_text(text: str) -> MemberLogResult:
    issues: list[ImportIssue] = []
    events: list[MemberLogEvent] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        payload = line
        occurred_at = None
        if "|" in line:
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            payload = parts[3]
            timestamp = parts[0].strip()
            try:
                occurred_at = datetime.strptime(timestamp, "%Y/%m/%d %H:%M:%S")
            except ValueError:
                issues.append(
                    ImportIssue(
                        line_number,
                        "occurred_at",
                        timestamp,
                        "Member log timestamp could not be parsed",
                        critical=True,
                    )
                )

        if "MN=" not in payload:
            continue

        fields: dict[str, str] = {}
        for item in payload.replace("\t", "\n").splitlines():
            item = item.strip()
            if not item or item == "-" or "=" not in item:
                continue
            key, value = item.split("=", 1)
            fields[key] = value

        if not fields:
            continue
        if (fields.get("MN") or "").strip() in {"", "0"}:
            continue

        member, row_issues = normalize_backup_update_record(fields, line_number)
        issues.extend(row_issues)
        if member:
            events.append(
                MemberLogEvent(
                    row_number=line_number,
                    occurred_at=occurred_at,
                    member=member,
                )
            )

    return MemberLogResult(events, issues)


def _parse_member_log_text(text: str) -> ImportResult:
    result = _parse_member_log_events_text(text)
    return ImportResult(
        [event.member for event in result.events],
        result.issues,
    )


def parse_member_log(path: str | Path) -> ImportResult:
    result = parse_member_log_events(path)
    return ImportResult(
        [event.member for event in result.events],
        result.issues,
    )


def parse_member_log_events(path: str | Path) -> MemberLogResult:
    path = Path(path)
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    if "\t" not in text and "|" not in text and re.search(r"(?m)^MN=", text):
        result = parse_members_btx(path)
        return MemberLogResult(
            [
                MemberLogEvent(
                    row_number=row_number,
                    occurred_at=None,
                    member=member,
                )
                for row_number, member in enumerate(result.members, start=1)
            ],
            result.issues,
        )
    return _parse_member_log_events_text(text)


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
    if suffix == ".csv":
        return parse_official_member_csv(path)
    raise ValueError(f"Unsupported GymAssistant export type: {path.suffix}")
