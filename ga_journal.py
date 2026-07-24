from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Iterable
import re
import zipfile


VOIDED_EVENT_MASK = 0x8000
MEMBERSHIP_EVENT_NAMES = {
    1: "new_membership",
    3: "membership_renewal",
}


@dataclass(frozen=True)
class GymAssistantMembershipEvent:
    source_reference: str
    source_payload_hash: str
    member_id: str
    event_name: str
    event_type: int
    is_voided: bool
    occurred_at: datetime
    journal_sequence: str
    journal_transaction_id: int | None
    membership_type_id: int
    billing_option_code: int
    service_period_start: date
    service_period_end_exclusive: date
    dues_cents: int
    other_contract_fee_cents: int
    source_tax_cents: int
    tender_total_cents: int
    remittance_type: int
    remittance_reference: int | None
    balance_payment_cents: int

    @property
    def service_period_end(self) -> date:
        """The source stores the next due date, so the service end is exclusive."""
        from datetime import timedelta

        return self.service_period_end_exclusive - timedelta(days=1)

    @property
    def manual_review_reason(self) -> str | None:
        if (
            not self.is_voided
            and self.dues_cents > 0
            and self.balance_payment_cents < 0
        ):
            return "membership_payment_uses_account_credit"
        return None

    @property
    def requires_manual_review(self) -> bool:
        return self.manual_review_reason is not None

    @property
    def is_positive_membership_payment(self) -> bool:
        return (
            not self.is_voided
            and self.dues_cents > 0
            and self.tender_total_cents > 0
            and not self.requires_manual_review
            and self.service_period_end_exclusive > self.service_period_start
        )

    def as_sync_record(self) -> dict:
        record = asdict(self)
        for key in ("occurred_at", "service_period_start", "service_period_end_exclusive"):
            record[key] = record[key].isoformat()
        record["service_period_end"] = self.service_period_end.isoformat()
        record["is_positive_membership_payment"] = self.is_positive_membership_payment
        record["requires_manual_review"] = self.requires_manual_review
        record["manual_review_reason"] = self.manual_review_reason
        return record


@dataclass(frozen=True)
class JournalParseIssue:
    line_number: int
    message: str


@dataclass(frozen=True)
class JournalParseResult:
    events: list[GymAssistantMembershipEvent]
    issues: list[JournalParseIssue]


@dataclass(frozen=True)
class GymAssistantPlanOption:
    membership_type_id: int
    plan_name: str
    billing_option_code: int
    interval_count: int
    interval_unit: str
    billing_method: str
    base_amount_cents: int
    taxable: bool


@dataclass(frozen=True)
class GymAssistantMonthlyAddon:
    addon_id: int
    name: str
    amount_cents: int
    taxable: bool


def _parse_yyyymmdd(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_occurred_at(value: str) -> datetime:
    if not re.fullmatch(r"c\d{8}!\d{4}", value):
        raise ValueError("invalid journal timestamp")
    return datetime.strptime(value[1:], "%Y%m%d!%H%M")


def _optional_positive_int(value: str | int) -> int | None:
    parsed = int(value)
    return parsed if parsed > 0 and parsed != 0xFFFFFFFF else None


def _snapshot_source_reference(header_parts: list[str]) -> str:
    """Build a snapshot fingerprint, not a durable GymAssistant transaction ID.

    This assumes the timestamp, sequence, epoch, transaction ID, and member ID stay
    unchanged when GymAssistant later sets only the high-bit void marker. The raw
    line hash remains available separately to detect any changed source payload.
    """
    identity = "|".join(
        (
            header_parts[0],
            header_parts[1],
            header_parts[2],
            header_parts[4],
            str(int(header_parts[5])),
        )
    )
    return f"ga-journal:{sha256(identity.encode('ascii')).hexdigest()}"


def parse_membership_journal_line(
    line: str,
) -> GymAssistantMembershipEvent | None:
    normalized = line.strip()
    if not normalized or "|" not in normalized:
        return None

    header, payload = normalized.split("|", 1)
    if not re.fullmatch(
        r"c\d{8}!\d{4}\s+[0-9A-Fa-f]+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+",
        header,
    ):
        return None
    header_parts = header.split()
    if len(header_parts) != 11:
        raise ValueError("membership journal header must contain 11 fields")

    raw_event_type = int(header_parts[6])
    event_type = raw_event_type & ~VOIDED_EVENT_MASK
    event_name = MEMBERSHIP_EVENT_NAMES.get(event_type)
    if not event_name:
        return None

    payload_parts = payload.split()
    if len(payload_parts) != 11:
        raise ValueError("membership journal payload must contain 11 fields")
    values = [int(value) for value in payload_parts]
    component_total_cents = values[4] + values[5] + values[6] + values[10]
    if values[7] != component_total_cents:
        raise ValueError(
            "membership payment components do not reconcile: "
            "tender_total_cents must equal dues_cents + other_contract_fee_cents "
            "+ source_tax_cents + balance_payment_cents"
        )
    payload_digest = sha256(normalized.encode("latin-1", errors="replace")).hexdigest()

    return GymAssistantMembershipEvent(
        source_reference=_snapshot_source_reference(header_parts),
        source_payload_hash=payload_digest,
        member_id=str(int(header_parts[5])),
        event_name=event_name,
        event_type=event_type,
        is_voided=bool(raw_event_type & VOIDED_EVENT_MASK),
        occurred_at=_parse_occurred_at(header_parts[0]),
        journal_sequence=header_parts[1],
        journal_transaction_id=_optional_positive_int(header_parts[4]),
        membership_type_id=values[0],
        billing_option_code=values[1],
        service_period_start=_parse_yyyymmdd(str(values[2])),
        service_period_end_exclusive=_parse_yyyymmdd(str(values[3])),
        dues_cents=values[4],
        other_contract_fee_cents=values[5],
        source_tax_cents=values[6],
        tender_total_cents=values[7],
        remittance_type=values[8],
        remittance_reference=_optional_positive_int(values[9]),
        balance_payment_cents=values[10],
    )


def parse_gymassistant_journal_text(
    text: str,
    *,
    member_ids: Iterable[str] | None = None,
) -> JournalParseResult:
    allowed_member_ids = (
        {str(member_id).strip() for member_id in member_ids if str(member_id).strip()}
        if member_ids is not None
        else None
    )
    events: list[GymAssistantMembershipEvent] = []
    issues: list[JournalParseIssue] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            event = parse_membership_journal_line(line)
        except (TypeError, ValueError) as exc:
            header = line.split("|", 1)[0].split()
            if allowed_member_ids is not None and len(header) > 5:
                try:
                    if str(int(header[5])) not in allowed_member_ids:
                        continue
                except ValueError:
                    continue
            if len(header) > 6:
                try:
                    if int(header[6]) & ~VOIDED_EVENT_MASK not in MEMBERSHIP_EVENT_NAMES:
                        continue
                except ValueError:
                    continue
            issues.append(JournalParseIssue(line_number, str(exc)))
            continue
        if not event:
            header = line.split("|", 1)[0].split()
            if len(header) > 6:
                try:
                    header_member_id = str(int(header[5]))
                    header_event_type = int(header[6]) & ~VOIDED_EVENT_MASK
                except ValueError:
                    continue
                if (
                    header_event_type in MEMBERSHIP_EVENT_NAMES
                    and (
                        allowed_member_ids is None
                        or header_member_id in allowed_member_ids
                    )
                ):
                    issues.append(
                        JournalParseIssue(
                            line_number,
                            "membership journal line has an invalid header or payload",
                        )
                    )
            continue
        if allowed_member_ids is not None and event.member_id not in allowed_member_ids:
            continue
        events.append(event)

    return JournalParseResult(events, issues)


def parse_gymassistant_journal(
    path: str | Path,
    *,
    member_ids: Iterable[str] | None = None,
) -> JournalParseResult:
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    return parse_gymassistant_journal_text(text, member_ids=member_ids)


def parse_gymassistant_backup_journal(
    path: str | Path,
    *,
    member_ids: Iterable[str] | None = None,
) -> JournalParseResult:
    with zipfile.ZipFile(path) as backup:
        journal_entry = next(
            (
                entry
                for entry in backup.namelist()
                if Path(entry).name.lower() == "journal.jtx"
            ),
            None,
        )
        if not journal_entry:
            raise ValueError(f"GymAssistant backup does not contain Journal.jtx: {path}")
        text = backup.read(journal_entry).decode("latin-1", errors="replace")
    return parse_gymassistant_journal_text(text, member_ids=member_ids)


def billing_option_code(interval_count: int, interval_unit: str, billing_method: str) -> int:
    unit = str(interval_unit or "").strip().upper()
    method = str(billing_method or "").strip().upper()
    if interval_count <= 0:
        raise ValueError("billing interval must be positive")
    if unit in {"MONTH", "MONTHS"}:
        unit_offset = 0
    elif unit in {"WEEK", "WEEKS"}:
        unit_offset = 32
    else:
        raise ValueError(f"unsupported GymAssistant billing interval: {interval_unit}")
    method_offset = {"INV": 0, "EFT": 1, "CC": 2}.get(method)
    if method_offset is None:
        raise ValueError(f"unsupported GymAssistant billing method: {billing_method}")
    return interval_count * 256 + unit_offset + method_offset


def service_period_matches_catalog_interval(
    service_period_start: date,
    service_period_end_exclusive: date,
    interval_count: int,
    interval_unit: str,
) -> bool:
    """Return whether an exclusive service period exactly matches a catalog interval.

    Month intervals are shifted in one calendar operation and clamp the original
    day to the last valid day of the target month. This preserves Jan 31 -> Feb
    28/29 and Jan 31 + two months -> Mar 31 without approximating months as days.
    """
    if interval_count <= 0:
        raise ValueError("billing interval must be positive")

    unit = str(interval_unit or "").strip().upper()
    if unit in {"WEEK", "WEEKS"}:
        expected_end_exclusive = service_period_start + timedelta(
            weeks=interval_count
        )
    elif unit in {"MONTH", "MONTHS"}:
        target_month_index = (
            service_period_start.year * 12
            + service_period_start.month
            - 1
            + interval_count
        )
        target_year, zero_based_target_month = divmod(target_month_index, 12)
        target_month = zero_based_target_month + 1
        target_day = min(
            service_period_start.day,
            monthrange(target_year, target_month)[1],
        )
        expected_end_exclusive = date(target_year, target_month, target_day)
    else:
        raise ValueError(
            f"unsupported GymAssistant billing interval: {interval_unit}"
        )

    return service_period_end_exclusive == expected_end_exclusive


def parse_gymassistant_billing_catalog_text(
    text: str,
) -> tuple[list[GymAssistantPlanOption], list[GymAssistantMonthlyAddon]]:
    plan_options: list[GymAssistantPlanOption] = []
    addons: list[GymAssistantMonthlyAddon] = []
    fields: dict[str, str] = {}
    options: list[str] = []

    def flush_plan() -> None:
        if not fields.get("MEMBERTYPE_ID") or not fields.get("CLASS"):
            return
        try:
            membership_type_id = int(fields["MEMBERTYPE_ID"])
        except ValueError:
            return
        for option in options:
            parts = option.split()
            if len(parts) != 5:
                continue
            try:
                interval_count = int(parts[0])
                amount_cents = int(parts[3])
                tax_flag = int(parts[4])
                option_code = billing_option_code(interval_count, parts[1], parts[2])
            except ValueError:
                continue
            plan_options.append(
                GymAssistantPlanOption(
                    membership_type_id=membership_type_id,
                    plan_name=fields["CLASS"].strip(),
                    billing_option_code=option_code,
                    interval_count=interval_count,
                    interval_unit=parts[1].upper(),
                    billing_method=parts[2].upper(),
                    base_amount_cents=amount_cents,
                    taxable=bool(tax_flag),
                )
            )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "-":
            flush_plan()
            fields = {}
            options = []
            continue
        if line.startswith("MONTH_ADDON="):
            raw_addon = line.split("=", 1)[1]
            metadata, separator, name = raw_addon.partition("|")
            parts = metadata.split(",")
            if separator and len(parts) == 3:
                try:
                    addons.append(
                        GymAssistantMonthlyAddon(
                            addon_id=int(parts[0]),
                            name=name.strip(),
                            amount_cents=int(parts[2]),
                            taxable=bool(int(parts[1])),
                        )
                    )
                except ValueError:
                    pass
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "OPTION":
            options.append(value)
        else:
            fields[key] = value
    flush_plan()
    return plan_options, addons


def parse_gymassistant_billing_catalog(
    path: str | Path,
) -> tuple[list[GymAssistantPlanOption], list[GymAssistantMonthlyAddon]]:
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    return parse_gymassistant_billing_catalog_text(text)


def parse_gymassistant_backup_billing_catalog(
    path: str | Path,
) -> tuple[list[GymAssistantPlanOption], list[GymAssistantMonthlyAddon]]:
    path = Path(path)
    with zipfile.ZipFile(path) as backup:
        members_entry = next(
            (
                entry
                for entry in backup.namelist()
                if Path(entry).name.lower() == "members.btx"
            ),
            None,
        )
        if not members_entry:
            raise ValueError(f"GymAssistant backup does not contain Members.btx: {path}")
        text = backup.read(members_entry).decode("latin-1", errors="replace")
    return parse_gymassistant_billing_catalog_text(text)
