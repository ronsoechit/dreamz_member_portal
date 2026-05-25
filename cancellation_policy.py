from dataclasses import dataclass
from datetime import date, timedelta
import calendar
import re


WINDOW_DAYS_BEFORE_TERM_END = 30
WINDOW_LENGTH_DAYS = 10


@dataclass(frozen=True)
class CancellationPolicyResult:
    can_request: bool
    status: str
    reason: str
    term_months: int | None = None
    current_term_start: date | None = None
    current_term_end: date | None = None
    window_open: date | None = None
    window_close_exclusive: date | None = None
    last_request_date: date | None = None
    next_window_open: date | None = None
    next_window_last_request_date: date | None = None


def detect_term_months(plan_type: str | None = None, contract_type: str | None = None) -> int | None:
    """Return fixed contract term length for Dreamz contracts only."""
    normalized_contract = (contract_type or "").strip().lower()
    if normalized_contract in {"6-months", "6 months", "6 month"}:
        return 6
    if normalized_contract in {"12-months", "12 months", "12 month"}:
        return 12

    normalized_plan = re.sub(r"\s+", " ", (plan_type or "").strip().lower())
    if "contract" not in normalized_plan:
        return None
    if re.search(r"\b6\b|6\s*mo|6\s*month", normalized_plan):
        return 6
    if re.search(r"\b12\b|12\s*m|12\s*month", normalized_plan):
        return 12
    return None


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def first_term_end(start_date: date | None, contract_end: date | None, term_months: int) -> date | None:
    if contract_end and contract_end.year > 1900:
        return contract_end
    if start_date:
        return add_months(start_date, term_months)
    return None


def evaluate_cancellation_policy(
    *,
    today: date,
    plan_type: str | None = None,
    contract_type: str | None = None,
    contract_begin: date | None = None,
    contract_end: date | None = None,
    signup_date: date | None = None,
) -> CancellationPolicyResult:
    term_months = detect_term_months(plan_type=plan_type, contract_type=contract_type)
    if term_months is None:
        return CancellationPolicyResult(
            can_request=True,
            status="allowed_no_fixed_term",
            reason="This membership is not a 6- or 12-month fixed-term Dreamz contract.",
        )

    start = contract_begin or signup_date
    term_end = first_term_end(start, contract_end, term_months)
    if term_end is None:
        return CancellationPolicyResult(
            can_request=False,
            status="blocked_missing_contract_dates",
            reason="The contract term cannot be calculated because contract dates are missing.",
            term_months=term_months,
        )

    term_start = start or add_months(term_end, -term_months)
    while term_end <= today:
        term_start = term_end
        term_end = add_months(term_end, term_months)

    window_open = term_end - timedelta(days=WINDOW_DAYS_BEFORE_TERM_END)
    window_close_exclusive = window_open + timedelta(days=WINDOW_LENGTH_DAYS)
    last_request_date = window_close_exclusive - timedelta(days=1)

    if window_open <= today < window_close_exclusive:
        return CancellationPolicyResult(
            can_request=True,
            status="allowed_in_window",
            reason="The cancellation window is open.",
            term_months=term_months,
            current_term_start=term_start,
            current_term_end=term_end,
            window_open=window_open,
            window_close_exclusive=window_close_exclusive,
            last_request_date=last_request_date,
        )

    if today < window_open:
        return CancellationPolicyResult(
            can_request=False,
            status="blocked_too_early",
            reason="The cancellation window has not opened yet.",
            term_months=term_months,
            current_term_start=term_start,
            current_term_end=term_end,
            window_open=window_open,
            window_close_exclusive=window_close_exclusive,
            last_request_date=last_request_date,
            next_window_open=window_open,
            next_window_last_request_date=last_request_date,
        )

    next_term_end = add_months(term_end, term_months)
    next_window_open = next_term_end - timedelta(days=WINDOW_DAYS_BEFORE_TERM_END)
    next_window_last_request_date = next_window_open + timedelta(days=WINDOW_LENGTH_DAYS - 1)
    return CancellationPolicyResult(
        can_request=False,
        status="blocked_window_closed",
        reason="The cancellation window is closed and the contract renews under the same conditions.",
        term_months=term_months,
        current_term_start=term_start,
        current_term_end=term_end,
        window_open=window_open,
        window_close_exclusive=window_close_exclusive,
        last_request_date=last_request_date,
        next_window_open=next_window_open,
        next_window_last_request_date=next_window_last_request_date,
    )
