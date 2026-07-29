from __future__ import annotations

import argparse
import ctypes
import json
import ntpath
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
GA_COMMAND_RECORD_PAYMENT = 2004
FEP_AUTO_PAYMENT_SOURCE = "fep_manager_bank_upload_auto"


if os.name == "nt":
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumWindowsProc, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetProcessDPIAware.restype = wintypes.BOOL
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount.restype = wintypes.DWORD
else:
    wintypes = None
    user32 = None
    kernel32 = None
    EnumWindowsProc = None


class LastInputInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


@dataclass
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    def overlaps_y(self, other: "Rect", tolerance: int = 4) -> bool:
        return self.bottom + tolerance >= other.top and other.bottom + tolerance >= self.top


@dataclass
class WindowInfo:
    hwnd: int
    parent: int
    control_id: int
    class_name: str
    text: str
    enabled: bool
    visible: bool
    rect: Rect


@dataclass
class BlockingDialog:
    hwnd: int
    reason: str
    texts: list[str]


@dataclass
class CreditBalancePrompt:
    hwnd: int
    display_amount: str
    texts: list[str]


class PaymentDialogTimeout(RuntimeError):
    """The expected payment form did not appear before the bounded deadline."""


class PaymentSafetyError(RuntimeError):
    """Gym Assistant showed a state that the guarded writer cannot prove safe."""


def require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Gym Assistant payment writer only runs on Windows.")
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def desktop_idle_seconds() -> float:
    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise RuntimeError("Could not read Windows idle time.")
    elapsed_ms = (int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF
    return elapsed_ms / 1000


def window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(length + 1, 256))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def window_process_id(hwnd: int) -> int:
    process_id = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not thread_id or not process_id.value:
        raise RuntimeError("Could not determine the Gym Assistant window process.")
    return int(process_id.value)


def window_is_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def window_rect(hwnd: int) -> Rect:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def get_text(hwnd: int) -> str:
    length = int(user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0))
    buffer = ctypes.create_unicode_buffer(max(length + 1, 256))
    user32.SendMessageW(hwnd, WM_GETTEXT, len(buffer), buffer)
    return buffer.value


def set_text(hwnd: int, text: str) -> None:
    user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text))


def click_button(hwnd: int) -> None:
    user32.SendMessageW(hwnd, BM_CLICK, 0, 0)


def post_command(hwnd: int, command_id: int) -> None:
    user32.PostMessageW(hwnd, WM_COMMAND, command_id, 0)


def enum_top_windows() -> list[WindowInfo]:
    windows: list[WindowInfo] = []

    def callback(hwnd, _lparam):
        windows.append(window_info(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return windows


def enum_children(hwnd: int) -> list[WindowInfo]:
    children: list[WindowInfo] = []

    def callback(child_hwnd, _lparam):
        children.append(window_info(child_hwnd))
        return True

    user32.EnumChildWindows(hwnd, EnumWindowsProc(callback), 0)
    return children


def window_info(hwnd: int) -> WindowInfo:
    parent = user32.GetParent(hwnd)
    return WindowInfo(
        hwnd=int(hwnd),
        parent=int(parent or 0),
        control_id=int(user32.GetDlgCtrlID(hwnd)),
        class_name=class_name(hwnd),
        text=window_text(hwnd),
        enabled=bool(user32.IsWindowEnabled(hwnd)),
        visible=bool(user32.IsWindowVisible(hwnd)),
        rect=window_rect(hwnd),
    )


def normalize_path_text(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).strip().strip('"')))


def gymassistant_title_data_path(title: str) -> str | None:
    text = str(title or "").strip()
    if not text.startswith("Gym Assistant") or "Path=" not in text:
        return None
    value = text.rsplit("Path=", 1)[1].strip()
    value = value.rstrip("])}").strip()
    if not value:
        return None
    return normalize_path_text(value)


def find_main_window(source_root: Path) -> int:
    expected_data = normalize_path_text(str((source_root / "Data").resolve()))
    candidates = []
    exact_matches = []
    for info in enum_top_windows():
        if not info.visible:
            continue
        title = info.text or ""
        if title.startswith("Gym Assistant") and "Path=" in title:
            candidates.append(info)
            if gymassistant_title_data_path(title) == expected_data:
                exact_matches.append(info)
    if len(exact_matches) == 1:
        return exact_matches[0].hwnd
    raise RuntimeError(
        "Could not find exactly one Gym Assistant window for the configured "
        f"data path; found {len(exact_matches)} exact match(es) among "
        f"{len(candidates)} candidate(s)."
    )


def find_open_payment_dialog(
    member_id: str | None = None,
    process_id: int | None = None,
) -> int | None:
    prefix = f"Member Payment for #{member_id}," if member_id else "Member Payment for #"
    for info in enum_top_windows():
        if (
            info.visible
            and info.text.startswith(prefix)
            and (process_id is None or window_process_id(info.hwnd) == process_id)
        ):
            return info.hwnd
    return None


def find_transaction_payment_dialog(process_id: int | None = None) -> int | None:
    for info in enum_top_windows():
        if (
            info.visible
            and info.text.startswith("Transaction - Member Payment")
            and (process_id is None or window_process_id(info.hwnd) == process_id)
        ):
            return info.hwnd
    return None


def dialog_texts(hwnd: int) -> list[str]:
    texts = [window_text(hwnd)]
    for child in enum_children(hwnd):
        text = child.text or get_text(child.hwnd)
        if text:
            texts.append(text)
    return [text.strip() for text in texts if text and text.strip()]


def find_credit_card_approval_dialog(process_id: int | None = None) -> int | None:
    for info in enum_top_windows():
        if not info.visible:
            continue
        if process_id is not None and window_process_id(info.hwnd) != process_id:
            continue
        combined = " ".join(dialog_texts(info.hwnd)).casefold()
        if "was credit card charge approved" in combined and "approved" in combined:
            return info.hwnd
    return None


def find_credit_card_result_dialog(process_id: int | None = None) -> int | None:
    for info in enum_top_windows():
        if not info.visible:
            continue
        if process_id is not None and window_process_id(info.hwnd) != process_id:
            continue
        combined = " ".join(dialog_texts(info.hwnd)).casefold()
        if info.text.startswith("CC Charge Results") or ("result: approved" in combined and "amount:" in combined):
            return info.hwnd
    return None


def find_next_payment_due_dialog(
    member_id: str | None = None,
    process_id: int | None = None,
) -> int | None:
    member_fragment = f"for #{member_id}" if member_id else "for #"
    for info in enum_top_windows():
        if not info.visible:
            continue
        if process_id is not None and window_process_id(info.hwnd) != process_id:
            continue
        combined = " ".join(dialog_texts(info.hwnd)).casefold()
        if (
            "next payment" in combined
            and "will be due" in combined
            and member_fragment.casefold() in combined
        ):
            return info.hwnd
    return None


def find_dependent_payment_prompt(process_id: int | None = None) -> BlockingDialog | None:
    for info in enum_top_windows():
        if not info.visible:
            continue
        if process_id is not None and window_process_id(info.hwnd) != process_id:
            continue
        texts = dialog_texts(info.hwnd)
        combined = " ".join(texts)
        lower = combined.casefold()
        if "dependent of #" not in lower or "responsible member" not in lower:
            continue
        match = re.search(r"dependent of\s+#(\d+)\s*([^.]*)", combined, re.IGNORECASE)
        if match:
            responsible = " ".join(match.group(2).split()).strip()
            responsible_label = f"#{match.group(1)}"
            if responsible:
                responsible_label = f"{responsible_label} {responsible}"
            reason = f"member is a dependent of responsible member {responsible_label}; manual payment review required."
        else:
            reason = "member is a dependent of another member; manual payment review required."
        return BlockingDialog(info.hwnd, reason, texts)
    return None


def find_member_activation_prompt(
    member_id: str | None = None,
    process_id: int | None = None,
) -> BlockingDialog | None:
    member_fragment = f"#{member_id}" if member_id else "#"
    for info in enum_top_windows():
        if not info.visible:
            continue
        if process_id is not None and window_process_id(info.hwnd) != process_id:
            continue
        texts = dialog_texts(info.hwnd)
        combined = " ".join(texts)
        lower = combined.casefold()
        if "currently inactive" not in lower or "do you want to activate" not in lower:
            continue
        if member_fragment.casefold() not in lower:
            continue
        reason = "Gym Assistant member was inactive; activation prompt was accepted before recording payment."
        return BlockingDialog(info.hwnd, reason, texts)
    return None


_CREDIT_BALANCE_PROMPT_RE = re.compile(
    r"Member has a credit balance of\s+"
    r"(?P<amount>\$?\s*\d[\d,]*\.\d{2}\s*(?:\(CR\)|CR)?)"
    r"\s*\.?\s*Apply this balance now\?",
    re.IGNORECASE,
)


def find_credit_balance_prompt(main_hwnd: int) -> CreditBalancePrompt | None:
    main_process_id = window_process_id(main_hwnd)
    matches: list[CreditBalancePrompt] = []
    for info in enum_top_windows():
        if not info.visible or info.class_name != "#32770":
            continue
        if window_process_id(info.hwnd) != main_process_id:
            continue
        texts = dialog_texts(info.hwnd)
        combined = " ".join(" ".join(text.split()) for text in texts)
        match = _CREDIT_BALANCE_PROMPT_RE.search(combined)
        if not match:
            continue
        matches.append(
            CreditBalancePrompt(
                hwnd=info.hwnd,
                display_amount=" ".join(match.group("amount").split()),
                texts=texts,
            )
        )
    if len(matches) > 1:
        raise PaymentSafetyError(
            "Multiple matching Gym Assistant credit-balance prompts are visible; refusing to choose one."
        )
    return matches[0] if matches else None


def dialog_enabled_buttons(dialog_hwnd: int) -> list[WindowInfo]:
    return [
        child
        for child in enum_children(dialog_hwnd)
        if child.class_name == "Button" and child.enabled and child.visible
    ]


def decline_credit_balance_prompt(prompt: CreditBalancePrompt, timeout: float) -> None:
    buttons = dialog_enabled_buttons(prompt.hwnd)
    labels = [button.text.replace("&", "").strip().casefold() for button in buttons]
    no_buttons = [button for button, label in zip(buttons, labels) if label == "no"]
    yes_buttons = [button for button, label in zip(buttons, labels) if label == "yes"]
    if len(no_buttons) != 1 or len(yes_buttons) != 1 or len(buttons) != 2:
        raise PaymentSafetyError(
            "Gym Assistant credit-balance prompt did not contain exactly one enabled Yes and one enabled No button."
        )
    click_button(no_buttons[0].hwnd)
    wait_until(
        lambda: not window_is_visible(prompt.hwnd),
        timeout,
        "Gym Assistant credit-balance prompt did not close after choosing No.",
    )


def find_unknown_gym_yes_no_prompt(main_hwnd: int) -> BlockingDialog | None:
    main_process_id = window_process_id(main_hwnd)
    for info in enum_top_windows():
        if not info.visible or info.class_name != "#32770":
            continue
        if window_process_id(info.hwnd) != main_process_id:
            continue
        buttons = dialog_enabled_buttons(info.hwnd)
        labels = [button.text.replace("&", "").strip().casefold() for button in buttons]
        if labels.count("yes") == 1 and labels.count("no") == 1:
            texts = dialog_texts(info.hwnd)
            return BlockingDialog(
                info.hwnd,
                "unrecognized Gym Assistant Yes/No prompt; manual review required.",
                texts,
            )
    return None


def click_dialog_button(dialog_hwnd: int, button_names: set[str]) -> bool:
    normalized = {name.casefold() for name in button_names}
    for child in enum_children(dialog_hwnd):
        if child.class_name != "Button" or not child.enabled:
            continue
        text = child.text.replace("&", "").strip().casefold()
        if text in normalized:
            click_button(child.hwnd)
            return True
    return False


def cancel_blocking_dialog(dialog: BlockingDialog) -> None:
    click_dialog_button(dialog.hwnd, {"Cancel"})


def accept_activation_dialog(dialog: BlockingDialog) -> None:
    if not click_dialog_button(dialog.hwnd, {"Yes"}):
        raise RuntimeError("Could not find enabled Yes button on Gym Assistant inactive-member activation prompt.")


def member_view_blocking_reason(main_hwnd: int) -> str | None:
    texts = [child.text.strip() for child in enum_children(main_hwnd) if child.text and child.text.strip()]
    for text in texts:
        match = re.search(r"Dependent of\s+#(\d+)\s*(.*)", text, re.IGNORECASE)
        if match:
            responsible = " ".join(match.group(2).split()).strip()
            responsible_label = f"#{match.group(1)}"
            if responsible:
                responsible_label = f"{responsible_label} {responsible}"
            return f"member is a dependent of responsible member {responsible_label}; manual payment review required."

    for index, text in enumerate(texts):
        if text != "Linked Memberships:":
            continue
        linked_value = next((candidate for candidate in texts[index + 1 :] if candidate != "Linked Memberships:"), "")
        if linked_value and linked_value.strip() != "- none -":
            return f"member has linked membership data ({linked_value}); manual payment review required."
    return None


def wait_for_payment_dialog(
    member_id: str,
    timeout: float,
    activation_events: list[str] | None = None,
    *,
    main_hwnd: int | None = None,
    allow_credit_balance_decline: bool = False,
    expected_credit_balance: Decimal | None = None,
    credit_balance_events: list[dict] | None = None,
) -> int:
    deadline = time.time() + timeout
    gym_process_id = window_process_id(main_hwnd) if main_hwnd is not None else None
    while time.time() < deadline:
        hwnd = find_open_payment_dialog(member_id, process_id=gym_process_id)
        if hwnd:
            return hwnd
        blocking_dialog = find_dependent_payment_prompt(process_id=gym_process_id)
        if blocking_dialog:
            cancel_blocking_dialog(blocking_dialog)
            raise RuntimeError(blocking_dialog.reason)
        activation_dialog = find_member_activation_prompt(
            member_id,
            process_id=gym_process_id,
        )
        if activation_dialog:
            accept_activation_dialog(activation_dialog)
            if activation_events is not None:
                activation_events.append(activation_dialog.reason)
            time.sleep(0.3)
            continue
        if main_hwnd is not None:
            credit_prompt = find_credit_balance_prompt(main_hwnd)
            if credit_prompt:
                if not allow_credit_balance_decline:
                    raise PaymentSafetyError(
                        "Gym Assistant asked to apply a credit balance, but this update is not an authorized "
                        "FEP automatic bank payment."
                    )
                prompt_amount = abs(decimal_money(credit_prompt.display_amount))
                if expected_credit_balance is None or expected_credit_balance >= 0:
                    raise PaymentSafetyError(
                        "Gym Assistant showed a credit-balance prompt, but the selected member did not have "
                        "a matching credit balance before Record a Payment was clicked."
                    )
                if abs(expected_credit_balance) != prompt_amount:
                    raise PaymentSafetyError(
                        f"Gym Assistant credit prompt amount {credit_prompt.display_amount} does not match "
                        f"the selected member credit {expected_credit_balance}."
                    )
                if credit_balance_events:
                    raise PaymentSafetyError(
                        "Gym Assistant showed the credit-balance prompt more than once for one payment."
                    )
                decline_credit_balance_prompt(
                    credit_prompt,
                    max(0.5, min(timeout, deadline - time.time())),
                )
                if credit_balance_events is not None:
                    credit_balance_events.append(
                        {
                            "action": "declined_apply_credit_balance",
                            "display_amount": credit_prompt.display_amount,
                            "selected_member_credit": str(expected_credit_balance),
                        }
                    )
                time.sleep(0.2)
                continue
            unknown_prompt = find_unknown_gym_yes_no_prompt(main_hwnd)
            if unknown_prompt:
                raise PaymentSafetyError(unknown_prompt.reason)
        time.sleep(0.2)
    raise PaymentDialogTimeout(f"Timed out waiting for Member Payment dialog for #{member_id}.")


def find_record_payment_button(main_hwnd: int) -> WindowInfo | None:
    for child in enum_children(main_hwnd):
        if child.class_name == "Button" and child.text == "Record a Payment" and child.enabled:
            return child
    return None


def wait_until(predicate, timeout: float, message: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise RuntimeError(message)


def find_child(children: list[WindowInfo], *, text: str | None = None, class_name_: str | None = None, enabled: bool | None = None) -> WindowInfo:
    matches = []
    for child in children:
        if text is not None and child.text != text:
            continue
        if class_name_ is not None and child.class_name != class_name_:
            continue
        if enabled is not None and child.enabled != enabled:
            continue
        matches.append(child)
    if not matches:
        raise RuntimeError(f"Could not find child text={text!r} class={class_name_!r}.")
    return matches[0]


def value_right_of(children: list[WindowInfo], label_text: str, classes: tuple[str, ...] = ("Static", "Edit", "ComboBox")) -> WindowInfo:
    labels = [child for child in children if child.class_name == "Static" and child.text == label_text]
    if not labels:
        raise RuntimeError(f"Could not find label {label_text!r}.")
    label = labels[0]
    candidates = [
        child
        for child in children
        if child.class_name in classes
        and child.hwnd != label.hwnd
        and child.rect.left >= label.rect.right - 8
        and child.rect.overlaps_y(label.rect)
    ]
    if not candidates:
        raise RuntimeError(f"Could not find value to the right of {label_text!r}.")
    return sorted(candidates, key=lambda child: (abs(child.rect.center_y - label.rect.center_y), child.rect.left))[0]


def value_right_of_label_prefix(
    children: list[WindowInfo],
    label_prefix: str,
    classes: tuple[str, ...] = ("Static", "Edit", "ComboBox"),
) -> WindowInfo:
    labels = [
        child
        for child in children
        if child.class_name == "Static" and child.text.strip().startswith(label_prefix)
    ]
    if not labels:
        raise RuntimeError(f"Could not find label starting with {label_prefix!r}.")
    return value_right_of(children, labels[0].text, classes=classes)


def decimal_money(value) -> Decimal:
    text = str(value).replace("$", "").replace(",", "").strip()
    is_credit = bool(re.search(r"(?:\(\s*CR\s*\)|\bCR\b)\s*$", text, re.IGNORECASE))
    if is_credit:
        text = re.sub(r"(?:\(\s*CR\s*\)|\bCR\b)\s*$", "", text, flags=re.IGNORECASE).strip()
    try:
        parsed = Decimal(text).quantize(Decimal("0.01"))
        return -abs(parsed) if is_credit else parsed
    except (InvalidOperation, ValueError):
        raise RuntimeError(f"Could not parse money value {value!r}.")


def money_equal(left, right) -> bool:
    return decimal_money(left) == decimal_money(right)


def parse_period_start(period: str) -> date:
    year_text, month_text = period.split("-", 1)
    return date(int(year_text), int(month_text), 1)


def iso_to_gym_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return parsed.strftime("%d/%m/%Y")


def next_month_start(value: date) -> date:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return date(year, month, 1)


def select_member(main_hwnd: int, member_id: str, timeout: float) -> None:
    user32.SetForegroundWindow(main_hwnd)
    children = enum_children(main_hwnd)
    view_button = find_child(children, text="View", class_name_="Button", enabled=True)
    sibling_edits = [child for child in children if child.parent == view_button.parent and child.class_name == "Edit" and child.enabled]
    if not sibling_edits:
        raise RuntimeError("Could not find the member search input.")
    search_input = sorted(sibling_edits, key=lambda child: child.rect.left)[0]
    set_text(search_input.hwnd, member_id)
    click_button(view_button.hwnd)

    def payment_button_enabled():
        for child in enum_children(main_hwnd):
            if child.class_name == "Button" and child.text == "Record a Payment" and child.enabled:
                return child
        return None

    wait_until(payment_button_enabled, timeout, f"Timed out selecting Gym Assistant member #{member_id}.")


def selected_member_heading(children: list[WindowInfo], member_id: str) -> str:
    return next(
        (
            child.text
            for child in children
            if child.text and re.match(rf"^#{re.escape(member_id)}(?:\s|$)", child.text.strip())
        ),
        "",
    )


def read_selected_member_balance(main_hwnd: int, member_id: str) -> Decimal:
    children = enum_children(main_hwnd)
    if not selected_member_heading(children, member_id):
        raise PaymentSafetyError(
            f"Gym Assistant main view does not prove that member #{member_id} is selected."
        )
    return decimal_money(value_right_of(children, "Current Balance:").text)


def credit_balance_decline_authorized(update: dict) -> bool:
    try:
        member_id = str(update.get("member_id") or "").strip()
        period_start = parse_period_start(str(update.get("membership_period") or "").strip())
        gym_amount = decimal_money(update.get("gym_billing_amount"))
        bank_amount = decimal_money(update.get("bank_amount"))
        target_values = update.get("target_values") or {}
        target_due = date.fromisoformat(str(target_values.get("next_payment") or target_values.get("due_date")))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    return (
        str(update.get("source") or "").strip() == FEP_AUTO_PAYMENT_SOURCE
        and bool(re.fullmatch(r"\d+", member_id))
        and gym_amount > 0
        and bank_amount >= gym_amount
        and target_due == next_month_start(period_start)
    )


def open_payment_dialog(
    main_hwnd: int,
    member_id: str,
    timeout: float,
    activation_events: list[str] | None = None,
    *,
    update: dict | None = None,
    credit_balance_events: list[dict] | None = None,
) -> int:
    user32.SetForegroundWindow(main_hwnd)
    button = wait_until(
        lambda: find_record_payment_button(main_hwnd),
        timeout,
        "Record a Payment button is not enabled.",
    )
    allow_credit_balance_decline = credit_balance_decline_authorized(update or {})
    selected_member_balance = read_selected_member_balance(main_hwnd, member_id)
    stale_credit_prompt = find_credit_balance_prompt(main_hwnd)
    if stale_credit_prompt:
        raise PaymentSafetyError(
            "A Gym Assistant credit-balance prompt was already visible before this payment attempt; "
            "refusing to associate it with the selected member."
        )
    click_button(button.hwnd)
    try:
        return wait_for_payment_dialog(
            member_id,
            min(timeout, 2.0),
            activation_events,
            main_hwnd=main_hwnd,
            allow_credit_balance_decline=allow_credit_balance_decline,
            expected_credit_balance=selected_member_balance,
            credit_balance_events=credit_balance_events,
        )
    except PaymentDialogTimeout:
        post_command(main_hwnd, GA_COMMAND_RECORD_PAYMENT)
    try:
        return wait_for_payment_dialog(
            member_id,
            min(timeout, 2.0),
            activation_events,
            main_hwnd=main_hwnd,
            allow_credit_balance_decline=allow_credit_balance_decline,
            expected_credit_balance=selected_member_balance,
            credit_balance_events=credit_balance_events,
        )
    except PaymentDialogTimeout:
        user32.SetForegroundWindow(main_hwnd)
        click_button(button.hwnd)
        return wait_for_payment_dialog(
            member_id,
            timeout,
            activation_events,
            main_hwnd=main_hwnd,
            allow_credit_balance_decline=allow_credit_balance_decline,
            expected_credit_balance=selected_member_balance,
            credit_balance_events=credit_balance_events,
        )


def inspect_payment_dialog(
    dialog_hwnd: int,
    update: dict,
    credit_balance_event: dict | None = None,
) -> dict:
    target_values = update.get("target_values") or {}
    member_id = str(update.get("member_id") or "").strip()
    membership_period = str(update.get("membership_period") or "").strip()
    amount = decimal_money(update.get("gym_billing_amount"))
    period_start = parse_period_start(membership_period)
    target_due = target_values.get("next_payment") or target_values.get("due_date") or next_month_start(period_start).isoformat()

    children = enum_children(dialog_hwnd)
    observed = {
        "dialog_title": window_text(dialog_hwnd),
        "member_id": member_id,
        "billing_plan": value_right_of(children, "Billing Plan:").text,
        "billing_option": value_right_of(children, "Billing Option:").text,
        "billing_amount": value_right_of(children, "Billing Amount:").text,
        "current_balance": value_right_of(children, "Current Balance:").text,
        "last_paid_date": value_right_of(children, "Last Paid Date:").text,
        "current_due_date": value_right_of(children, "Current Due Date:").text,
        "billing_periods": get_text(value_right_of_label_prefix(children, "Billing Periods", ("ComboBox", "Edit")).hwnd),
        "membership_fees": get_text(value_right_of(children, "Membership Fees:", ("Edit", "Static")).hwnd),
        "other_fees": get_text(value_right_of(children, "Other Fees:", ("Edit", "Static")).hwnd),
        "payment_on_current_balance": get_text(
            value_right_of(children, "Payment on Current Balance:", ("Edit", "Static")).hwnd
        ),
        "total_payment_due": value_right_of(children, "Total Payment Due:").text,
        "next_payment_due": get_text(value_right_of(children, "Next Payment Due:", ("Edit", "Static")).hwnd),
    }

    if not observed["dialog_title"].startswith(f"Member Payment for #{member_id},"):
        raise RuntimeError(f"Payment dialog is for the wrong member: {observed['dialog_title']}")
    if observed["current_due_date"] != period_start.strftime("%d/%m/%Y"):
        raise RuntimeError(
            f"Current due date {observed['current_due_date']} does not match membership_period {membership_period}."
        )
    if not money_equal(observed["billing_amount"], amount):
        raise RuntimeError(f"Billing amount {observed['billing_amount']} does not match expected {amount}.")
    current_balance = decimal_money(observed["current_balance"])
    if credit_balance_event:
        prompt_amount = abs(decimal_money(credit_balance_event.get("display_amount")))
        if current_balance >= 0:
            raise RuntimeError(
                f"Current balance is {observed['current_balance']} after declining a credit; "
                "the original credit was not preserved."
            )
        if abs(current_balance) != prompt_amount:
            raise RuntimeError(
                f"Current credit balance {observed['current_balance']} does not match the declined prompt amount "
                f"{credit_balance_event.get('display_amount')}."
            )
    elif current_balance != Decimal("0.00"):
        raise RuntimeError(f"Current balance is {observed['current_balance']}; manual review required.")
    if observed["billing_periods"].strip() != "1":
        raise RuntimeError(f"Billing periods is {observed['billing_periods']}, expected 1.")
    if not money_equal(observed["membership_fees"], amount):
        raise RuntimeError(f"Membership fees {observed['membership_fees']} does not match expected {amount}.")
    if not money_equal(observed["other_fees"], Decimal("0.00")):
        raise RuntimeError(f"Other fees is {observed['other_fees']}; manual review required.")
    if not money_equal(observed["payment_on_current_balance"], Decimal("0.00")):
        raise RuntimeError(
            f"Payment on current balance is {observed['payment_on_current_balance']}; "
            "membership-only payment required."
        )
    if not money_equal(observed["total_payment_due"], amount):
        raise RuntimeError(f"Total payment due {observed['total_payment_due']} does not match expected {amount}.")
    if observed["next_payment_due"] != iso_to_gym_date(str(target_due)):
        raise RuntimeError(f"Next payment due {observed['next_payment_due']} does not match target {target_due}.")

    return observed


def cancel_dialog(dialog_hwnd: int) -> None:
    children = enum_children(dialog_hwnd)
    cancel = find_child(children, text="&Cancel", class_name_="Button", enabled=True)
    click_button(cancel.hwnd)


def apply_payment(
    dialog_hwnd: int,
    timeout: float,
    payment_method: str = "Credit Card",
    member_id: str | None = None,
) -> dict:
    gym_process_id = window_process_id(dialog_hwnd)
    children = enum_children(dialog_hwnd)
    record = find_child(children, text="&Record Payment", class_name_="Button", enabled=True)
    click_button(record.hwnd)

    deadline = time.time() + timeout
    transaction_hwnd = None
    while time.time() < deadline:
        transaction_hwnd = find_transaction_payment_dialog(process_id=gym_process_id)
        if transaction_hwnd:
            break
        if not find_open_payment_dialog(process_id=gym_process_id):
            grace_deadline = min(deadline, time.time() + 1.0)
            while time.time() < grace_deadline:
                transaction_hwnd = find_transaction_payment_dialog(process_id=gym_process_id)
                if transaction_hwnd:
                    break
                time.sleep(0.1)
            if not transaction_hwnd:
                raise PaymentSafetyError(
                    "Member Payment dialog closed without an observed transaction dialog; "
                    "payment outcome is ambiguous and requires manual reconciliation."
                )
            break
        time.sleep(0.1)

    if not transaction_hwnd:
        raise RuntimeError("Timed out waiting for Gym Assistant payment transaction dialog.")

    if not click_dialog_button(transaction_hwnd, {payment_method}):
        click_dialog_button(transaction_hwnd, {"Cancel"})
        raise RuntimeError(f"Could not find enabled payment method button {payment_method!r}.")

    def transaction_closed():
        return not find_transaction_payment_dialog(process_id=gym_process_id)

    wait_until(
        transaction_closed,
        timeout,
        f"Timed out waiting for Gym Assistant transaction dialog to close after selecting {payment_method}.",
    )

    if payment_method.strip().casefold() == "credit card":
        approval_deadline = time.time() + min(timeout, 5.0)
        approval_hwnd = None
        while time.time() < approval_deadline:
            approval_hwnd = find_credit_card_approval_dialog(process_id=gym_process_id)
            if approval_hwnd:
                break
            time.sleep(0.1)

        if approval_hwnd:
            if not click_dialog_button(approval_hwnd, {"Approved"}):
                click_dialog_button(approval_hwnd, {"Cancel"})
                raise RuntimeError("Could not find enabled Credit Card approval button.")

            wait_until(
                lambda: not find_credit_card_approval_dialog(process_id=gym_process_id),
                timeout,
                "Timed out waiting for Gym Assistant credit card approval dialog to close.",
            )

        result_deadline = time.time() + min(timeout, 5.0)
        result_hwnd = None
        while time.time() < result_deadline:
            result_hwnd = find_credit_card_result_dialog(process_id=gym_process_id)
            if result_hwnd:
                break
            time.sleep(0.1)

        if result_hwnd:
            if not click_dialog_button(result_hwnd, {"OK"}):
                raise RuntimeError("Could not find enabled OK button on Credit Card result dialog.")

            wait_until(
                lambda: not find_credit_card_result_dialog(process_id=gym_process_id),
                timeout,
                "Timed out waiting for Gym Assistant credit card result dialog to close.",
            )

    wait_until(
        lambda: (
            not find_open_payment_dialog(process_id=gym_process_id)
            and not find_transaction_payment_dialog(process_id=gym_process_id)
            and not find_credit_card_approval_dialog(process_id=gym_process_id)
            and not find_credit_card_result_dialog(process_id=gym_process_id)
        ),
        timeout,
        "Timed out waiting for Gym Assistant payment dialogs to close after applying payment.",
    )

    next_due_deadline = time.time() + min(timeout, 5.0)
    next_due_hwnd = None
    while time.time() < next_due_deadline:
        next_due_hwnd = find_next_payment_due_dialog(
            member_id,
            process_id=gym_process_id,
        )
        if next_due_hwnd:
            break
        time.sleep(0.1)

    if next_due_hwnd:
        if not click_dialog_button(next_due_hwnd, {"OK"}):
            raise RuntimeError("Could not find enabled OK button on next payment due dialog.")
        wait_until(
            lambda: not find_next_payment_due_dialog(
                member_id,
                process_id=gym_process_id,
            ),
            timeout,
            "Timed out waiting for Gym Assistant next payment due dialog to close.",
        )
    return {
        "transaction_dialog_observed": True,
        "payment_method": payment_method,
        "credit_card_approval_observed": bool(approval_hwnd) if payment_method.strip().casefold() == "credit card" else False,
        "credit_card_result_observed": bool(result_hwnd) if payment_method.strip().casefold() == "credit card" else False,
        "next_payment_due_dialog_observed": bool(next_due_hwnd),
    }


def read_member_payment_state(main_hwnd: int, member_id: str) -> dict:
    children = enum_children(main_hwnd)
    member_heading = selected_member_heading(children, member_id)
    if not member_heading:
        raise RuntimeError(f"Gym Assistant main view does not show member #{member_id}.")
    return {
        "member_heading": member_heading,
        "due_date": value_right_of(children, "Due Date:").text,
        "last_paid_date": value_right_of(children, "Last Paid Date:").text,
        "last_paid_amount": value_right_of(children, "Last Paid Amount:").text,
        "current_balance": value_right_of(children, "Current Balance:").text,
    }


def verify_member_payment_readback(
    main_hwnd: int,
    update: dict,
    timeout: float,
    expected_current_balance: Decimal,
) -> dict:
    member_id = str(update.get("member_id") or "").strip()
    target_values = update.get("target_values") or {}
    target_due = target_values.get("next_payment") or target_values.get("due_date")
    expected_last_paid = date.fromisoformat(str(target_values.get("last_payment")))
    membership_period_start = parse_period_start(str(update.get("membership_period") or ""))
    expected_amount = decimal_money(update.get("gym_billing_amount"))
    latest_acceptable_paid = date.today() + timedelta(days=1)

    select_member(main_hwnd, member_id, timeout)
    deadline = time.time() + timeout
    last_error = "Gym Assistant did not expose refreshed member values."
    while time.time() < deadline:
        try:
            observed = read_member_payment_state(main_hwnd, member_id)
            if observed["due_date"] != iso_to_gym_date(str(target_due)):
                raise RuntimeError(
                    f"Due date remained {observed['due_date']}; expected {iso_to_gym_date(str(target_due))}."
                )
            if not money_equal(observed["last_paid_amount"], expected_amount):
                raise RuntimeError(
                    f"Last paid amount is {observed['last_paid_amount']}; expected {expected_amount}."
                )
            actual_last_paid = datetime.strptime(observed["last_paid_date"], "%d/%m/%Y").date()
            booked_on_allowed_date = expected_last_paid <= actual_last_paid <= latest_acceptable_paid
            booked_for_membership_period = actual_last_paid == membership_period_start
            if not booked_on_allowed_date and not booked_for_membership_period:
                raise RuntimeError(
                    f"Last paid date is {observed['last_paid_date']}; expected either the paid membership "
                    f"period {membership_period_start.isoformat()} or a booking date between "
                    f"{expected_last_paid.isoformat()} and {latest_acceptable_paid.isoformat()}."
                )
            actual_current_balance = decimal_money(observed["current_balance"])
            if actual_current_balance != expected_current_balance:
                raise RuntimeError(
                    f"Current balance changed from {expected_current_balance} to "
                    f"{observed['current_balance']}."
                )
            return observed
        except (RuntimeError, ValueError) as exc:
            last_error = str(exc)
            time.sleep(0.2)
    raise PaymentSafetyError(
        "Gym Assistant payment was submitted but the refreshed member state could not be verified: "
        f"{last_error} Manual reconciliation is required."
    )


def run_writer(
    stdin_payload: dict,
    apply: bool,
    timeout: float,
    foreground_ui: bool = False,
    require_idle_seconds: float = 0,
    payment_method: str = "Credit Card",
) -> dict:
    source_root = Path(stdin_payload.get("source_root") or r"D:\Dreamz Fitness\Gym Assistant 2.6")
    update = stdin_payload.get("update") or {}
    member_id = str(update.get("member_id") or "").strip()
    if not member_id:
        raise RuntimeError("update.member_id is required.")
    if not foreground_ui:
        raise RuntimeError(
            "Refusing to drive the visible Gym Assistant UI without --foreground-ui. "
            "Use this only in a controlled session or on a dedicated/idle workstation."
        )
    if require_idle_seconds > 0:
        idle_seconds = desktop_idle_seconds()
        if idle_seconds < require_idle_seconds:
            return {
                "status": "deferred",
                "applied": False,
                "member_id": member_id,
                "reason": "desktop_not_idle",
                "idle_seconds": round(idle_seconds, 1),
                "required_idle_seconds": require_idle_seconds,
            }
    try:
        main_hwnd = find_main_window(source_root)
    except RuntimeError as exc:
        if apply and "Could not find the running Gym Assistant main window" in str(exc):
            return {
                "status": "deferred",
                "applied": False,
                "member_id": member_id,
                "reason": "gym_assistant_not_running",
                "error": str(exc),
            }
        raise
    gym_process_id = window_process_id(main_hwnd)
    if find_open_payment_dialog(process_id=gym_process_id):
        if apply:
            return {
                "status": "deferred",
                "applied": False,
                "member_id": member_id,
                "reason": "payment_dialog_already_open",
            }
        raise RuntimeError("A Gym Assistant payment dialog is already open. Close it before running the writer.")
    blocking_dialog = find_dependent_payment_prompt(process_id=gym_process_id)
    if blocking_dialog:
        cancel_blocking_dialog(blocking_dialog)
        raise RuntimeError(blocking_dialog.reason)
    select_member(main_hwnd, member_id, timeout)
    blocking_reason = member_view_blocking_reason(main_hwnd)
    if blocking_reason:
        raise RuntimeError(blocking_reason)
    activation_events: list[str] = []
    credit_balance_events: list[dict] = []
    try:
        dialog_hwnd = open_payment_dialog(
            main_hwnd,
            member_id,
            timeout,
            activation_events,
            update=update,
            credit_balance_events=credit_balance_events,
        )
    except PaymentDialogTimeout:
        if apply:
            return {
                "status": "deferred",
                "applied": False,
                "member_id": member_id,
                "reason": "payment_dialog_did_not_open",
            }
        raise
    if len(credit_balance_events) > 1:
        cancel_dialog(dialog_hwnd)
        raise PaymentSafetyError("Multiple credit-balance decisions were recorded for one payment.")
    credit_balance_event = credit_balance_events[0] if credit_balance_events else None
    try:
        observed = inspect_payment_dialog(dialog_hwnd, update, credit_balance_event)
    except Exception:
        cancel_dialog(dialog_hwnd)
        raise

    if not apply:
        cancel_dialog(dialog_hwnd)
        return {
            "status": "dry_run",
            "applied": False,
            "member_id": member_id,
            "observed": observed,
            "error": "Dry run only. Re-run with --apply to record the Gym Assistant payment.",
        }

    completion_evidence = apply_payment(
        dialog_hwnd,
        timeout,
        payment_method=payment_method,
        member_id=member_id,
    )
    expected_current_balance = decimal_money(observed["current_balance"])
    readback = verify_member_payment_readback(
        main_hwnd,
        update,
        timeout,
        expected_current_balance=expected_current_balance,
    )
    result = {
        "status": "applied",
        "applied": True,
        "member_id": member_id,
        "observed": observed,
        "completion_evidence": completion_evidence,
        "readback": readback,
    }
    if activation_events:
        result["activation_prompt_accepted"] = True
        result["activation_events"] = activation_events
    if credit_balance_event:
        result["credit_balance_declined"] = True
        result["credit_balance_event"] = credit_balance_event
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Write one queued FEP payment into local Gym Assistant.")
    parser.add_argument("--apply", action="store_true", help="Actually click Record Payment in Gym Assistant.")
    parser.add_argument(
        "--foreground-ui",
        action="store_true",
        help="Allow the writer to use the visible Gym Assistant desktop UI. Required with --apply.",
    )
    parser.add_argument(
        "--require-idle-seconds",
        type=float,
        default=0,
        help="With --apply, refuse to run unless the Windows desktop has been idle this many seconds.",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Seconds to wait for Gym Assistant UI changes.")
    parser.add_argument(
        "--payment-method",
        default="Credit Card",
        help="Gym Assistant transaction payment method to click after Record Payment.",
    )
    args = parser.parse_args()

    try:
        require_windows()
        payload = json.loads(sys.stdin.read() or "{}")
        result = run_writer(
            payload,
            apply=args.apply,
            timeout=args.timeout,
            foreground_ui=args.foreground_ui,
            require_idle_seconds=args.require_idle_seconds,
            payment_method=args.payment_method,
        )
        print(json.dumps(result, sort_keys=True))
        if result.get("status") == "deferred":
            return 0
        return 0 if args.apply else 2
    except Exception as exc:
        error = {"status": "failed", "error": str(exc)}
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
