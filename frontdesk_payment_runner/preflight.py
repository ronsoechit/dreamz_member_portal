from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from pathlib import Path

import gymassistant_payment_writer as payment_writer
from frontdesk_payment_runner import runner as scoped_runner


EXPECTED_COMPUTER_NAME = "DREAMZ-FRNTDSK"
EXPECTED_WINDOWS_USER = "Dreamz Fitness"
EXPECTED_SOURCE_ROOT = Path(r"C:\Gym Assistant 2.6")
EXPECTED_DATA_ROOT = r"C:\Gym Assistant 2.6\Data"
MINIMUM_IDLE_SECONDS = 300

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2


def normalize_windows_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(str(value).strip().strip('"')))


def gymassistant_title_data_path(title: str) -> str | None:
    text = str(title or "").strip()
    if not text.startswith("Gym Assistant") or "Path=" not in text:
        return None
    value = text.rsplit("Path=", 1)[1].strip().rstrip("])}").strip()
    return normalize_windows_path(value) if value else None


def _windows_desktop_state() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "windows_required"

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    user32.OpenInputDesktop.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND

    process_session = wintypes.DWORD(0)
    if not kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(),
        ctypes.byref(process_session),
    ):
        return False, "session_unavailable"
    if int(process_session.value) != int(kernel32.WTSGetActiveConsoleSessionId()):
        return False, "session_not_active"

    desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not desktop:
        return False, "desktop_unavailable"
    try:
        required = wintypes.DWORD(0)
        user32.GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0 or required.value > 4096:
            return False, "desktop_unavailable"
        size = (required.value // ctypes.sizeof(ctypes.c_wchar)) + 1
        buffer = ctypes.create_unicode_buffer(size)
        if not user32.GetUserObjectInformationW(
            desktop,
            UOI_NAME,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(required),
        ):
            return False, "desktop_unavailable"
        if buffer.value.casefold() != "default":
            return False, "desktop_locked"
    finally:
        user32.CloseDesktop(desktop)

    if not user32.GetForegroundWindow():
        return False, "desktop_unavailable"
    return True, "ready"


def _eligible_gymassistant_window_count() -> int:
    expected = normalize_windows_path(EXPECTED_DATA_ROOT)
    return sum(
        1
        for window in payment_writer.enum_top_windows()
        if window.visible and gymassistant_title_data_path(window.text) == expected
    )


def _payment_dialog_is_open() -> bool:
    checks = (
        payment_writer.find_open_payment_dialog,
        payment_writer.find_transaction_payment_dialog,
        payment_writer.find_credit_card_approval_dialog,
        payment_writer.find_credit_card_result_dialog,
        payment_writer.find_dependent_payment_prompt,
    )
    return any(bool(check()) for check in checks)


def run_preflight(required_idle_seconds: int = MINIMUM_IDLE_SECONDS) -> dict[str, object]:
    required_idle = max(MINIMUM_IDLE_SECONDS, int(required_idle_seconds))
    computer_name = scoped_runner.native_computer_name()
    windows_user = scoped_runner.native_windows_user()
    checks: list[tuple[bool, str]] = [
        (
            bool(computer_name)
            and computer_name.casefold() == EXPECTED_COMPUTER_NAME.casefold(),
            "wrong_computer",
        ),
        (
            bool(windows_user)
            and windows_user.casefold() == EXPECTED_WINDOWS_USER.casefold(),
            "wrong_windows_user",
        ),
        (
            EXPECTED_SOURCE_ROOT.is_dir()
            and (EXPECTED_SOURCE_ROOT / "Data").is_dir(),
            "gymassistant_source_unavailable",
        ),
    ]
    for ok, reason in checks:
        if not ok:
            return {"ok": False, "reason": reason}

    desktop_ready, desktop_reason = _windows_desktop_state()
    if not desktop_ready:
        return {"ok": False, "reason": desktop_reason}

    try:
        idle_seconds = float(payment_writer.desktop_idle_seconds())
    except Exception:
        return {"ok": False, "reason": "desktop_idle_unavailable"}
    if idle_seconds < required_idle:
        return {
            "ok": False,
            "reason": "desktop_not_idle",
            "required_idle_seconds": required_idle,
        }

    try:
        eligible_windows = _eligible_gymassistant_window_count()
    except Exception:
        return {"ok": False, "reason": "gymassistant_window_check_failed"}
    if eligible_windows != 1:
        return {
            "ok": False,
            "reason": "expected_gymassistant_window_count_mismatch",
            "eligible_window_count": eligible_windows,
        }

    try:
        if _payment_dialog_is_open():
            return {"ok": False, "reason": "payment_dialog_already_open"}
    except Exception:
        return {"ok": False, "reason": "payment_dialog_check_failed"}

    return {
        "ok": True,
        "reason": "ready",
        "required_idle_seconds": required_idle,
        "eligible_window_count": 1,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Frontdesk payment-runner preflight. It does not contact "
            "the Portal, claim a command, or operate Gym Assistant."
        )
    )
    parser.add_argument(
        "--require-idle-seconds",
        type=int,
        default=MINIMUM_IDLE_SECONDS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_preflight(args.require_idle_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
