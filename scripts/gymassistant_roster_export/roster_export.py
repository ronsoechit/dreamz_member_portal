from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from getpass import getpass
import argparse
import base64
import csv
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Iterator, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ga_import import (  # noqa: E402
    OFFICIAL_CSV_REQUIRED_COLUMNS,
    SUPPORTED_BILLING_STATUSES,
    parse_official_member_csv,
)


DEFAULT_MINIMUM_MEMBERS = 4000
DEFAULT_MAX_COUNT_CHANGE_PERCENT = 15.0
DEFAULT_STABILITY_SECONDS = 3.0
DEFAULT_UI_TIMEOUT_SECONDS = 90.0
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 900.0
SPECIAL_FEATURES_COMMAND_ID = 5020
EXPORT_COMMAND = "Export Members to Excel"
EXPORT_MUTEX_NAME = "Local\\DreamzGymAssistantRosterExport"
DPAPI_ENTROPY = b"DreamzGymAssistantRosterExport:v1"
ACCESSIBLE_BUTTON_HELPER = SCRIPT_DIR / "Invoke-GymAssistantDialogButton.ps1"


class RosterExportError(RuntimeError):
    """Raised when the export cannot be completed without risking bad data."""


class CandidateRejectedError(RosterExportError):
    """Raised after a rejected candidate has already been recorded in state."""


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_members: int = DEFAULT_MINIMUM_MEMBERS
    max_count_change_percent: float = DEFAULT_MAX_COUNT_CHANGE_PERCENT


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    path: str
    sha256: str
    size_bytes: int
    member_count: int
    issue_count: int
    critical_issue_count: int
    previous_member_count: int | None
    count_change_percent: float | None
    errors: tuple[str, ...]


@dataclass(frozen=True)
class PromotionResult:
    target: str
    backup: str | None
    report: ValidationReport


@dataclass(frozen=True)
class PublishedTargetGuard:
    target: Path
    existed: bool
    sha256: str | None
    recovery_path: Path | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_published_target_guard(target: Path, backup_dir: Path) -> PublishedTargetGuard:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        return PublishedTargetGuard(target=target, existed=False, sha256=None, recovery_path=None)
    if not target.is_file():
        raise RosterExportError(f"Het gepubliceerde ledenpad is geen bestand: {target}")

    original_hash = sha256_file(target)
    backup_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = backup_dir / (
        f".{target.name}.{os.getpid()}.{time.time_ns()}.pre-export"
    )
    shutil.copy2(target, recovery_path)
    with recovery_path.open("r+b") as handle:
        os.fsync(handle.fileno())
    if sha256_file(recovery_path) != original_hash:
        recovery_path.unlink(missing_ok=True)
        raise RosterExportError("De herstelkopie van de gepubliceerde ledenlijst is ongeldig.")
    return PublishedTargetGuard(
        target=target,
        existed=True,
        sha256=original_hash,
        recovery_path=recovery_path,
    )


def published_target_matches_guard(guard: PublishedTargetGuard) -> bool:
    if not guard.existed:
        return not guard.target.exists()
    return (
        guard.target.is_file()
        and guard.sha256 is not None
        and sha256_file(guard.target) == guard.sha256
    )


def restore_published_target(guard: PublishedTargetGuard) -> None:
    if published_target_matches_guard(guard):
        return
    if not guard.existed:
        if guard.target.is_dir():
            raise RosterExportError(
                f"Onverwachte map kan niet als ledenbestand worden verwijderd: {guard.target}"
            )
        guard.target.unlink(missing_ok=True)
        return
    if (
        guard.recovery_path is None
        or not guard.recovery_path.is_file()
        or guard.sha256 is None
        or sha256_file(guard.recovery_path) != guard.sha256
    ):
        raise RosterExportError("De oorspronkelijke gepubliceerde ledenlijst kan niet worden hersteld.")

    restore_path = guard.target.with_name(
        f".{guard.target.name}.{os.getpid()}.{time.time_ns()}.restore"
    )
    try:
        shutil.copy2(guard.recovery_path, restore_path)
        with restore_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(restore_path, guard.target)
    finally:
        restore_path.unlink(missing_ok=True)
    if sha256_file(guard.target) != guard.sha256:
        raise RosterExportError("Hashcontrole na herstel van de ledenlijst is mislukt.")


def release_published_target_guard(guard: PublishedTargetGuard | None) -> None:
    if guard and guard.recovery_path:
        guard.recovery_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def wait_for_stable_file(
    path: Path,
    *,
    stable_seconds: float = DEFAULT_STABILITY_SECONDS,
    timeout_seconds: float = 120.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[int, int] | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            time.sleep(0.25)
            continue
        signature = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size > 0 and signature == last_signature:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= stable_seconds:
                return
        else:
            stable_since = None
            last_signature = signature
        time.sleep(0.25)
    raise RosterExportError(f"Exportbestand werd niet stabiel binnen {timeout_seconds:.0f} seconden: {path}")


def _parsed_member_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    try:
        result = parse_official_member_csv(path)
    except (OSError, ValueError, UnicodeError):
        return None
    if any(issue.critical for issue in result.issues):
        return None
    return len(result.members)


def validate_candidate(
    candidate: str | Path,
    *,
    previous: str | Path | None = None,
    policy: ValidationPolicy | None = None,
) -> ValidationReport:
    candidate_path = Path(candidate).resolve()
    previous_path = Path(previous).resolve() if previous else None
    policy = policy or ValidationPolicy()
    errors: list[str] = []
    member_count = 0
    issue_count = 0
    critical_issue_count = 0
    previous_member_count = _parsed_member_count(previous_path) if previous_path else None
    count_change_percent: float | None = None
    digest = ""
    size_bytes = 0

    if not candidate_path.is_file():
        errors.append("Het kandidaatbestand ontbreekt.")
    else:
        size_bytes = candidate_path.stat().st_size
        digest = sha256_file(candidate_path)
        try:
            result = parse_official_member_csv(candidate_path)
        except (OSError, ValueError, UnicodeError, csv.Error) as exc:
            errors.append(f"De officiele Gym Assistant CSV kon niet worden gelezen: {exc}")
        else:
            member_count = len(result.members)
            issue_count = len(result.issues)
            critical_issue_count = sum(1 for issue in result.issues if issue.critical)
            member_ids = [str(member.get("member_id") or "") for member in result.members]
            invalid_ids = [member_id for member_id in member_ids if not member_id.isdigit()]
            duplicate_count = len(member_ids) - len(set(member_ids))
            invalid_statuses = sorted(
                {
                    str(member.get("billing_status") or "")
                    for member in result.members
                    if str(member.get("billing_status") or "") not in SUPPORTED_BILLING_STATUSES
                }
            )
            if critical_issue_count:
                errors.append(f"De CSV bevat {critical_issue_count} kritieke parserfout(en).")
            if invalid_ids:
                errors.append(f"De CSV bevat {len(invalid_ids)} ongeldig(e) lidnummer(s).")
            if duplicate_count:
                errors.append(f"De CSV bevat {duplicate_count} dubbel(e) lidnummer(s).")
            if invalid_statuses:
                errors.append("De CSV bevat onbekende billingstatussen.")
            if member_count < policy.minimum_members:
                errors.append(
                    f"De CSV bevat {member_count} actuele leden; minimaal {policy.minimum_members} vereist."
                )
            if previous_member_count:
                count_change_percent = (
                    abs(member_count - previous_member_count) / previous_member_count * 100.0
                )
                if count_change_percent > policy.max_count_change_percent:
                    errors.append(
                        "Het aantal actuele leden wijkt "
                        f"{count_change_percent:.2f}% af van de vorige geldige CSV; "
                        f"maximaal {policy.max_count_change_percent:.2f}% toegestaan."
                    )

    return ValidationReport(
        ok=not errors,
        path=str(candidate_path),
        sha256=digest,
        size_bytes=size_bytes,
        member_count=member_count,
        issue_count=issue_count,
        critical_issue_count=critical_issue_count,
        previous_member_count=previous_member_count,
        count_change_percent=count_change_percent,
        errors=tuple(errors),
    )


def promote_candidate(
    candidate: str | Path,
    target: str | Path,
    *,
    backup_dir: str | Path,
    state_path: str | Path,
    policy: ValidationPolicy | None = None,
) -> PromotionResult:
    candidate_path = Path(candidate).resolve()
    target_path = Path(target).resolve()
    backup_path_root = Path(backup_dir).resolve()
    state_file = Path(state_path).resolve()
    report = validate_candidate(candidate_path, previous=target_path, policy=policy)
    if not report.ok:
        atomic_write_json(
            state_file,
            {
                "schema_version": 1,
                "status": "rejected",
                "checked_at": utc_now().isoformat(),
                "candidate": asdict(report),
                "target": str(target_path),
            },
        )
        raise CandidateRejectedError("Kandidaat-CSV afgewezen: " + " ".join(report.errors))

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target_path.is_file():
        backup_path_root.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        backup = backup_path_root / f"MemberData-{stamp}-{sha256_file(target_path)[:12]}.csv"
        shutil.copy2(target_path, backup)

    with candidate_path.open("r+b") as handle:
        os.fsync(handle.fileno())
    if sha256_file(candidate_path) != report.sha256:
        raise RosterExportError("De kandidaat-CSV wijzigde na validatie; publicatie is gestopt.")
    os.replace(candidate_path, target_path)
    promoted_hash = sha256_file(target_path)
    if promoted_hash != report.sha256:
        if backup and backup.is_file():
            restore = target_path.with_name(f".{target_path.name}.{os.getpid()}.restore")
            shutil.copy2(backup, restore)
            os.replace(restore, target_path)
        else:
            target_path.unlink(missing_ok=True)
        raise RosterExportError("Hashcontrole na publicatie is mislukt.")

    result = PromotionResult(
        target=str(target_path),
        backup=str(backup) if backup else None,
        report=report,
    )
    atomic_write_json(
        state_file,
        {
            "schema_version": 1,
            "status": "published",
            "published_at": utc_now().isoformat(),
            "target": str(target_path),
            "backup": str(backup) if backup else None,
            "validation": asdict(report),
        },
    )
    return result


if os.name == "nt":
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple["DATA_BLOB", ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_secret(secret: str) -> bytes:
    if os.name != "nt":
        raise RosterExportError("DPAPI is alleen beschikbaar op Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    value, value_buffer = _blob(secret.encode("utf-8"))
    entropy, entropy_buffer = _blob(DPAPI_ENTROPY)
    output = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(value), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        del value_buffer, entropy_buffer


def unprotect_secret(ciphertext: bytes) -> str:
    if os.name != "nt":
        raise RosterExportError("DPAPI is alleen beschikbaar op Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    value, value_buffer = _blob(ciphertext)
    entropy, entropy_buffer = _blob(DPAPI_ENTROPY)
    output = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(value), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        del value_buffer, entropy_buffer


def save_credential(path: Path, secret: str) -> None:
    if not secret:
        raise RosterExportError("Een lege Master Access-invoer wordt niet opgeslagen.")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(protect_secret(secret))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(encoded + b"\n")
    os.replace(temporary, path)


def load_credential(path: Path) -> str:
    if not path.is_file():
        raise RosterExportError(f"Versleutelde Master Access-invoer ontbreekt: {path}")
    try:
        ciphertext = base64.b64decode(path.read_bytes().strip(), validate=True)
        return unprotect_secret(ciphertext)
    except Exception as exc:
        raise RosterExportError(
            "De Master Access-invoer kan niet door deze Windows-gebruiker worden ontsleuteld."
        ) from exc


class NamedMutex:
    def __init__(self, name: str = EXPORT_MUTEX_NAME) -> None:
        self.name = name
        self.handle: int | None = None
        self.owned = False

    def __enter__(self) -> "NamedMutex":
        if os.name != "nt":
            return self
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise ctypes.WinError()
        result = kernel32.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RosterExportError("Een andere ledenexport draait al.")
        self.owned = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if os.name == "nt" and self.handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.owned:
                kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
        self.handle = None
        self.owned = False


@contextmanager
def pause_signup_bridge(
    work_root: Path | None,
    *,
    timeout_seconds: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
) -> Iterator[None]:
    if not work_root or not work_root.is_dir():
        yield
        return

    enabled = work_root / "enabled.flag"
    paused_marker = work_root / "enabled.flag.roster-export-paused"
    state_path = work_root / "agent-state.json"
    changed = False
    pause_requested_at: datetime | None = None
    if paused_marker.exists():
        raise RosterExportError(f"Oude bridge-pauzemarkering aangetroffen: {paused_marker}")
    if enabled.exists():
        os.replace(enabled, paused_marker)
        changed = True
        pause_requested_at = utc_now()

    if state_path.exists():
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8-sig"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                state = {}
            state_at: datetime | None = None
            try:
                state_at = datetime.fromisoformat(str(state.get("at") or ""))
                if state_at.tzinfo is None:
                    state_at = state_at.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
            fresh_pause = (
                pause_requested_at is None
                or (state_at is not None and state_at >= pause_requested_at)
            )
            if state.get("status") == "paused" and fresh_pause:
                break
            time.sleep(1.0)
        else:
            if changed and paused_marker.exists():
                os.replace(paused_marker, enabled)
            raise RosterExportError("Signup Bridge werd niet op tijd veilig gepauzeerd.")
    elif changed:
        os.replace(paused_marker, enabled)
        raise RosterExportError("Signup Bridge-status ontbreekt; export veilig uitgesteld.")

    try:
        yield
    finally:
        if changed and paused_marker.exists():
            os.replace(paused_marker, enabled)


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    parent: int
    process_id: int
    control_id: int
    class_name: str
    text: str
    left: int
    top: int
    right: int
    bottom: int
    visible: bool
    enabled: bool


class Win32UI:
    WM_COMMAND = 0x0111
    WM_CLOSE = 0x0010
    WM_SETTEXT = 0x000C
    BM_CLICK = 0x00F5
    CB_GETCOUNT = 0x0146
    CB_GETLBTEXT = 0x0148
    CB_SETCURSEL = 0x014E
    LB_GETCOUNT = 0x018B
    LB_GETTEXT = 0x0189
    LB_SETCURSEL = 0x0186

    def __init__(self) -> None:
        if os.name != "nt":
            raise RosterExportError("Gym Assistant UI-automatisering is alleen beschikbaar op Windows.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetDlgCtrlID.argtypes = (wintypes.HWND,)
        self.user32.GetDlgCtrlID.restype = ctypes.c_int
        self.user32.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.GetDlgItem.restype = wintypes.HWND
        self.user32.GetParent.argtypes = (wintypes.HWND,)
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsWindowEnabled.argtypes = (wintypes.HWND,)
        self.user32.IsWindowEnabled.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.SendMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self.user32.SendMessageW.restype = ctypes.c_ssize_t
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self.user32.PostMessageW.restype = wintypes.BOOL

    def _text(self, handle: int) -> str:
        length = self.user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def _class_name(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value

    def _process_id(self, handle: int) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        return int(pid.value)

    def _info(self, handle: int, parent: int = 0) -> WindowInfo:
        rect = wintypes.RECT()
        self.user32.GetWindowRect(handle, ctypes.byref(rect))
        return WindowInfo(
            handle=int(handle),
            parent=int(parent),
            process_id=self._process_id(handle),
            control_id=int(self.user32.GetDlgCtrlID(handle)),
            class_name=self._class_name(handle),
            text=self._text(handle),
            left=int(rect.left),
            top=int(rect.top),
            right=int(rect.right),
            bottom=int(rect.bottom),
            visible=bool(self.user32.IsWindowVisible(handle)),
            enabled=bool(self.user32.IsWindowEnabled(handle)),
        )

    def top_windows(self, process_id: int | None = None) -> list[WindowInfo]:
        found: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(handle, _):
            info = self._info(handle)
            if (process_id is None or info.process_id == process_id) and info.visible:
                found.append(info)
            return True

        self.user32.EnumWindows(callback, 0)
        return found

    def children(self, parent: int) -> list[WindowInfo]:
        found: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(handle, _):
            found.append(self._info(handle, parent))
            return True

        self.user32.EnumChildWindows(parent, callback, 0)
        return found

    def wait_window(
        self,
        process_id: int,
        *,
        title_contains: str | None = None,
        title_exact: str | None = None,
        child_text: str | None = None,
        timeout_seconds: float = DEFAULT_UI_TIMEOUT_SECONDS,
    ) -> WindowInfo:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for window in self.top_windows(process_id):
                if title_exact is not None and window.text != title_exact:
                    continue
                if title_contains is not None and title_contains.casefold() not in window.text.casefold():
                    continue
                if child_text is not None and not any(
                    _normalize_text(child.text) == _normalize_text(child_text)
                    for child in self.children(window.handle)
                ):
                    continue
                return window
            time.sleep(0.2)
        expected = title_exact or title_contains or child_text or "verwacht venster"
        raise RosterExportError(f"Gym Assistant-venster niet gevonden: {expected}")

    def wait_descendant(
        self,
        process_id: int,
        *,
        text_contains: str,
        class_name: str | None = None,
        timeout_seconds: float = DEFAULT_UI_TIMEOUT_SECONDS,
    ) -> WindowInfo:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for top in self.top_windows(process_id):
                for child in self.children(top.handle):
                    if text_contains.casefold() not in child.text.casefold():
                        continue
                    if class_name and child.class_name.casefold() != class_name.casefold():
                        continue
                    return child
            time.sleep(0.2)
        raise RosterExportError(f"Gym Assistant-intern venster niet gevonden: {text_contains}")

    def send(self, handle: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
        return int(self.user32.SendMessageW(handle, message, wparam, lparam))

    def post(self, handle: int, message: int, wparam: int = 0, lparam: int = 0) -> None:
        if not self.user32.PostMessageW(handle, message, wparam, lparam):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle: int) -> None:
        self.post(handle, self.WM_CLOSE)

    def wait_not_visible(self, handle: int, *, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.user32.IsWindow(handle) or not self.user32.IsWindowVisible(handle):
                return
            time.sleep(0.1)
        raise RosterExportError("Gym Assistant sloot het verwachte venster niet tijdig.")

    def set_text(self, handle: int, value: str) -> None:
        buffer = ctypes.create_unicode_buffer(value)
        pointer = ctypes.cast(buffer, ctypes.c_void_p).value or 0
        self.send(handle, self.WM_SETTEXT, 0, pointer)

    def click(self, handle: int) -> None:
        if not self.user32.IsWindowEnabled(handle):
            raise RosterExportError("De verwachte knop is uitgeschakeld.")
        self.post(handle, self.BM_CLICK)

    def control_by_id(self, parent: int, control_id: int) -> WindowInfo:
        handle = int(self.user32.GetDlgItem(parent, control_id))
        if not handle:
            raise RosterExportError(f"Control ID {control_id} ontbreekt in Gym Assistant.")
        return self._info(handle, parent)

    def control_by_text(self, parent: int, text: str, *, class_name: str | None = None) -> WindowInfo:
        wanted = _normalize_text(text)
        matches = [
            child
            for child in self.children(parent)
            if _normalize_text(child.text) == wanted
            and (class_name is None or child.class_name.casefold() == class_name.casefold())
        ]
        if len(matches) != 1:
            raise RosterExportError(
                f"Verwachte control '{text}' niet uniek gevonden (aantal: {len(matches)})."
            )
        return matches[0]

    def button(self, parent: int, text: str) -> WindowInfo:
        return self.control_by_text(parent, text, class_name="Button")

    def select_list_item(self, handle: int, text: str) -> None:
        count = self.send(handle, self.LB_GETCOUNT)
        for index in range(count):
            buffer = ctypes.create_unicode_buffer(1024)
            pointer = ctypes.cast(buffer, ctypes.c_void_p).value or 0
            self.send(handle, self.LB_GETTEXT, index, pointer)
            if _normalize_text(buffer.value) == _normalize_text(text):
                if self.send(handle, self.LB_SETCURSEL, index) < 0:
                    raise RosterExportError(f"Lijstkeuze kon niet worden ingesteld: {text}")
                parent = int(self.user32.GetParent(handle))
                control_id = int(self.user32.GetDlgCtrlID(handle))
                self.post(parent, self.WM_COMMAND, control_id | (1 << 16), handle)
                return
        raise RosterExportError(f"Lijstkeuze ontbreekt: {text}")

    def activate_list_item(self, handle: int, text: str) -> None:
        self.select_list_item(handle, text)
        parent = int(self.user32.GetParent(handle))
        control_id = int(self.user32.GetDlgCtrlID(handle))
        self.post(parent, self.WM_COMMAND, control_id | (2 << 16), handle)

    def select_combo_item(self, handle: int, text: str) -> None:
        count = self.send(handle, self.CB_GETCOUNT)
        for index in range(count):
            buffer = ctypes.create_unicode_buffer(1024)
            pointer = ctypes.cast(buffer, ctypes.c_void_p).value or 0
            self.send(handle, self.CB_GETLBTEXT, index, pointer)
            if _normalize_text(buffer.value) == _normalize_text(text):
                if self.send(handle, self.CB_SETCURSEL, index) < 0:
                    raise RosterExportError(f"Keuze kon niet worden ingesteld: {text}")
                parent = int(self.user32.GetParent(handle))
                control_id = int(self.user32.GetDlgCtrlID(handle))
                self.post(parent, self.WM_COMMAND, control_id | (1 << 16), handle)
                time.sleep(0.05)
                return
        raise RosterExportError(f"Keuze ontbreekt: {text}")

    def combo_for_label(self, parent: int, label: str) -> WindowInfo:
        controls = self.children(parent)
        labels = [child for child in controls if _normalize_text(child.text) == _normalize_text(label)]
        if len(labels) != 1:
            raise RosterExportError(f"Label '{label}' niet uniek gevonden.")
        source = labels[0]
        source_y = (source.top + source.bottom) / 2
        candidates = [
            child
            for child in controls
            if child.class_name.casefold() == "combobox"
            and child.left >= source.right - 5
            and abs(((child.top + child.bottom) / 2) - source_y) <= 18
        ]
        if not candidates:
            raise RosterExportError(f"Keuzelijst naast '{label}' ontbreekt.")
        return min(candidates, key=lambda child: (abs(((child.top + child.bottom) / 2) - source_y), child.left))


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").replace("&", "").split()).casefold().rstrip(":")


def _dialog_text(window: WindowInfo, children: Sequence[WindowInfo]) -> str:
    return _normalize_text(" ".join([window.text, *(child.text for child in children if child.text)]))


def _exported_record_count(value: str) -> int | None:
    match = re.search(r"\b([\d,.]+)\s+member records exported\b", value, flags=re.IGNORECASE)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _is_expected_overwrite_prompt(value: str, candidate_path: Path) -> bool:
    normalized = _normalize_text(value)
    mentions_existing = any(
        marker in normalized
        for marker in ("already exists", "bestaat al", "bestaat reeds")
    )
    asks_to_replace = any(
        marker in normalized
        for marker in ("overwrite", "replace", "overschrijven", "vervangen")
    )
    expected_names = {
        candidate_path.name.casefold(),
        "memdata.dat",
    }
    return mentions_existing and asks_to_replace and any(name in normalized for name in expected_names)


def _classify_export_dialog(
    window: WindowInfo,
    children: Sequence[WindowInfo],
    candidate_path: Path,
) -> str | None:
    text = _dialog_text(window, children)
    title = _normalize_text(window.text)
    child_texts = {_normalize_text(child.text) for child in children if child.text}
    button_texts = {
        _normalize_text(child.text)
        for child in children
        if child.class_name.casefold() == "button" and child.text
    }

    if _exported_record_count(text) is not None:
        return "success"
    if _is_expected_overwrite_prompt(text, candidate_path):
        return "overwrite"
    if title == "confirm save as" and {"yes", "no"}.issubset(button_texts):
        return "overwrite"
    if "csv" in text and "tab-delimited" in text:
        return "format"
    if (
        ("save" in title and "as" in title)
        or ("opslaan" in title and "als" in title)
        or any(child.control_id == 1148 for child in children)
    ):
        return "save_as"
    if "special commands" in child_texts:
        return "special_commands"
    if window.text == "Export Member Data":
        return "export_member_data"
    if window.text == "Password":
        return "password"
    if "deluxe 5,000 edition" in text and "revision info" in text:
        return "about"
    if (
        ("financial" in text or "bank" in text or "credit card" in text)
        and bool(button_texts.intersection({"yes", "ja"}))
    ):
        return "financial"
    return None


def _find_gymassistant_main(ui: Win32UI, expected_data_path: str) -> WindowInfo | None:
    expected = str(Path(expected_data_path)).rstrip("\\/").casefold()
    matches = [
        window
        for window in ui.top_windows()
        if window.class_name == "GymAssistant26Task"
        and "Gym Assistant" in window.text
        and f"path={expected}" in window.text.casefold()
    ]
    if len(matches) > 1:
        raise RosterExportError("Meer dan een Gym Assistant-hoofdvenster gebruikt het verwachte datapad.")
    return matches[0] if matches else None


def _start_gymassistant(executable: Path, expected_data_path: str, ui: Win32UI) -> WindowInfo:
    if not executable.is_file():
        raise RosterExportError(f"Gym Assistant-programma ontbreekt: {executable}")
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        main = _find_gymassistant_main(ui, expected_data_path)
        if main:
            return main
        if process.poll() is not None:
            raise RosterExportError(f"Gym Assistant stopte tijdens opstarten (exitcode {process.returncode}).")
        time.sleep(0.5)
    raise RosterExportError("Gym Assistant startte niet binnen 60 seconden met het verwachte datapad.")


def _dump_ui(ui: Win32UI, process_id: int) -> list[dict]:
    payload: list[dict] = []
    for window in ui.top_windows(process_id):
        item = asdict(window)
        item["children"] = [asdict(child) for child in ui.children(window.handle)]
        payload.append(item)
    return payload


def invoke_accessible_dialog_button(
    dialog: WindowInfo,
    button: WindowInfo,
    button_label: str,
    *,
    helper_path: Path = ACCESSIBLE_BUTTON_HELPER,
) -> None:
    if os.name != "nt":
        raise RosterExportError(
            "Windows-toegankelijkheidsbediening is alleen op Windows beschikbaar."
        )
    if not helper_path.is_file():
        raise RosterExportError(f"Windows-toegankelijkheidshelper ontbreekt: {helper_path}")

    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper_path),
        "-ExpectedProcessId",
        str(dialog.process_id),
        "-WindowHandle",
        str(dialog.handle),
        "-ButtonHandle",
        str(button.handle),
        "-ExpectedTitle",
        dialog.text,
        "-ButtonLabel",
        button_label,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RosterExportError(
            f"Windows-toegankelijkheidsbediening reageerde niet tijdig op knop '{button_label}'."
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "onbekende fout").strip()
        raise RosterExportError(
            f"Windows-toegankelijkheidsbediening kon knop '{button_label}' "
            f"niet veilig activeren: {detail}"
        )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1].lstrip("\ufeff"))
    except (IndexError, json.JSONDecodeError) as exc:
        raise RosterExportError(
            "Windows-toegankelijkheidsbediening gaf geen controleerbaar "
            f"resultaat voor knop '{button_label}'."
        ) from exc
    if (
        result.get("status") != "invoked"
        or result.get("process_id") != dialog.process_id
        or result.get("window_handle") != dialog.handle
        or result.get("button_handle") != button.handle
        or result.get("button") != button_label
        or result.get("accessible_role") != 43
        or result.get("method") != "MSAA.accDoDefaultAction"
    ):
        raise RosterExportError(
            f"Windows-toegankelijkheidsbediening bevestigde knop '{button_label}' niet."
        )


class GymAssistantExporter:
    def __init__(
        self,
        *,
        executable: Path,
        expected_data_path: str,
        candidate_path: Path,
        credential_path: Path,
        ui_timeout_seconds: float = DEFAULT_UI_TIMEOUT_SECONDS,
        manual_auth: bool = False,
        ui: Win32UI | None = None,
        accessible_button_invoker: Callable[[WindowInfo, WindowInfo, str], None] | None = None,
    ) -> None:
        self.executable = executable
        self.expected_data_path = expected_data_path
        self.candidate_path = candidate_path
        self.credential_path = credential_path
        self.ui_timeout_seconds = ui_timeout_seconds
        self.manual_auth = manual_auth
        self.ui = ui or Win32UI()
        self.accessible_button_invoker = (
            accessible_button_invoker or invoke_accessible_dialog_button
        )

    def _main_window(self) -> WindowInfo:
        main = _find_gymassistant_main(self.ui, self.expected_data_path)
        return main or _start_gymassistant(self.executable, self.expected_data_path, self.ui)

    def _assert_clean_start(self, main: WindowInfo) -> None:
        other = [
            window
            for window in self.ui.top_windows(main.process_id)
            if window.handle != main.handle and window.visible
        ]
        if other:
            titles = ", ".join(repr(window.text or window.class_name) for window in other)
            raise RosterExportError(
                "Gym Assistant heeft al een dialoog of ledenvenster open; export uitgesteld: " + titles
            )
        stale_reports = [
            child
            for child in self.ui.children(main.handle)
            if child.visible and "membership list" in child.text.casefold()
        ]
        if stale_reports:
            raise RosterExportError(
                "Er staat nog een Membership List-rapport open; export veilig uitgesteld."
            )

    def _authenticate(self, main: WindowInfo) -> WindowInfo:
        self.ui.post(main.handle, self.ui.WM_COMMAND, SPECIAL_FEATURES_COMMAND_ID)
        try:
            password = self.ui.wait_window(
                main.process_id,
                title_exact="Password",
                timeout_seconds=3.0,
            )
        except RosterExportError:
            return self.ui.wait_window(
                main.process_id,
                child_text="Special Commands",
                timeout_seconds=self.ui_timeout_seconds,
            )

        if self.manual_auth:
            return self.ui.wait_window(
                main.process_id,
                child_text="Special Commands",
                timeout_seconds=self.ui_timeout_seconds,
            )
        secret = load_credential(self.credential_path)
        try:
            self.ui.set_text(self.ui.control_by_id(password.handle, 3).handle, secret)
            self.ui.click(self.ui.control_by_id(password.handle, 1).handle)
        finally:
            secret = ""
        return self.ui.wait_window(
            main.process_id,
            child_text="Special Commands",
            timeout_seconds=self.ui_timeout_seconds,
        )

    def _choose_export_command(self, special: WindowInfo) -> WindowInfo:
        listboxes = [
            control
            for control in self.ui.children(special.handle)
            if control.class_name.casefold() == "listbox"
        ]
        if len(listboxes) != 1:
            raise RosterExportError("De opdrachtenlijst in Special Commands is niet uniek gevonden.")
        self.ui.activate_list_item(listboxes[0].handle, EXPORT_COMMAND)
        return self.ui.wait_window(
            special.process_id,
            title_exact="Export Member Data",
            timeout_seconds=self.ui_timeout_seconds,
        )

    def _configure_filters(self, dialog: WindowInfo) -> WindowInfo:
        try:
            self.ui.click(self.ui.button(dialog.handle, "Clear Filters").handle)
            time.sleep(0.25)
        except RosterExportError:
            pass
        selections = (
            ("Plan Types", "All Plans"),
            ("Billing Status", "All"),
            ("Billing Options", "All"),
            ("Due Date", "All"),
            ("Contract Begin", "All"),
            ("Contract End", "All"),
            ("Signup Date", "All"),
            ("Visits Recorded", "All"),
            ("Search Fields", "none"),
            ("Member Flags", "- none -"),
            ("Sort By", "Membership Number"),
        )
        for label, value in selections:
            combo = self.ui.combo_for_label(dialog.handle, label)
            self.ui.select_combo_item(combo.handle, value)
        self.ui.click(self.ui.button(dialog.handle, "Generate Report").handle)
        return self.ui.wait_descendant(
            dialog.process_id,
            text_contains="Membership List",
            class_name="xGym Assistant1220doc9012",
            timeout_seconds=self.ui_timeout_seconds,
        )

    def _click_modal_button(self, dialog: WindowInfo, *labels: str) -> None:
        button: WindowInfo | None = None
        selected_label: str | None = None
        errors: list[str] = []
        for label in labels:
            try:
                button = self.ui.button(dialog.handle, label)
                selected_label = label
                break
            except RosterExportError as exc:
                errors.append(str(exc))
        if button is None or selected_label is None:
            raise RosterExportError(
                f"Geen verwachte knop gevonden in Gym Assistant ({', '.join(labels)}): "
                + "; ".join(errors)
            )

        prefer_accessibility = _normalize_text(dialog.text) == "confirm save as"
        if prefer_accessibility:
            self.accessible_button_invoker(dialog, button, selected_label)
        else:
            self.ui.click(button.handle)
        self.ui.wait_not_visible(dialog.handle, timeout_seconds=5.0)

    def _answer_export_prompts(self, process_id: int) -> int:
        deadline = time.monotonic() + self.ui_timeout_seconds
        handled: set[tuple[int, str]] = set()
        last_dialogs: list[str] = []
        while time.monotonic() < deadline:
            dialogs = [
                window
                for window in self.ui.top_windows(process_id)
                if window.class_name == "#32770"
            ]
            last_dialogs = [window.text or window.class_name for window in dialogs]
            acted = False
            for dialog in dialogs:
                children = self.ui.children(dialog.handle)
                kind = _classify_export_dialog(dialog, children, self.candidate_path)
                if kind is None:
                    continue
                key = (dialog.handle, kind)
                if kind == "success":
                    count = _exported_record_count(_dialog_text(dialog, children))
                    if count is None:
                        raise RosterExportError("De exportsuccesmelding bevat geen geldig recordaantal.")
                    self._click_modal_button(dialog, "Close", "Sluiten")
                    return count
                if key in handled:
                    continue
                if kind == "format":
                    self._click_modal_button(dialog, "CSV")
                elif kind == "financial":
                    self._click_modal_button(dialog, "Yes", "Ja")
                elif kind == "overwrite":
                    self._click_modal_button(dialog, "Yes", "Ja")
                elif kind == "save_as":
                    filename = next(
                        (
                            child
                            for child in children
                            if child.control_id == 1148 or child.class_name.casefold() == "edit"
                        ),
                        None,
                    )
                    if filename is None:
                        raise RosterExportError("Bestandsnaamveld in Opslaan als ontbreekt.")
                    self.ui.set_text(filename.handle, str(self.candidate_path))
                    self.ui.click(self.ui.control_by_id(dialog.handle, 1).handle)
                    self.ui.wait_not_visible(dialog.handle, timeout_seconds=5.0)
                else:
                    continue
                handled.add(key)
                acted = True
                time.sleep(0.2)
                break
            if not acted:
                time.sleep(0.2)
        visible = ", ".join(repr(title) for title in last_dialogs) or "geen"
        raise RosterExportError(
            "Gym Assistant bevestigde de voltooide ledenexport niet tijdig. "
            f"Zichtbare dialoogvensters: {visible}."
        )

    def _dismiss_known_dialog(self, window: WindowInfo, kind: str) -> None:
        try:
            if kind == "success":
                self._click_modal_button(window, "Close", "Sluiten")
            elif kind == "overwrite":
                self._click_modal_button(window, "No", "Nee")
            elif kind == "financial":
                self._click_modal_button(window, "No", "Nee", "Cancel", "Annuleren")
            elif kind in {"format", "save_as", "special_commands", "export_member_data", "password"}:
                self._click_modal_button(window, "Cancel", "Annuleren")
            elif kind == "about":
                self._click_modal_button(window, "OK")
            else:
                raise RosterExportError(f"Onbekend Gym Assistant-venstertype: {kind}")
        except RosterExportError as click_error:
            self.ui.close(window.handle)
            try:
                self.ui.wait_not_visible(window.handle, timeout_seconds=3.0)
            except RosterExportError as close_error:
                title = window.text or window.class_name
                raise RosterExportError(
                    f"Gym Assistant-venster '{title}' ({kind}) kon niet veilig worden gesloten: "
                    f"{click_error}"
                ) from close_error

    def _cleanup_export_windows(self, main: WindowInfo) -> None:
        deadline = time.monotonic() + 12.0
        quiet_since: float | None = None
        remaining: list[str] = []
        while time.monotonic() < deadline:
            closed_any = False
            report_open = False
            for child in self.ui.children(main.handle):
                if (
                    child.class_name == "xGym Assistant1220doc9012"
                    and "Membership List" in child.text
                ):
                    report_open = True
                    self.ui.close(child.handle)
                    closed_any = True

            remaining = []
            for window in self.ui.top_windows(main.process_id):
                if window.handle == main.handle:
                    continue
                kind = _classify_export_dialog(
                    window,
                    self.ui.children(window.handle),
                    self.candidate_path,
                )
                if kind:
                    self._dismiss_known_dialog(window, kind)
                    closed_any = True
                else:
                    remaining.append(window.text or window.class_name)

            if closed_any:
                quiet_since = None
                time.sleep(0.3)
                continue
            quiet_since = quiet_since or time.monotonic()
            if not report_open and not remaining and time.monotonic() - quiet_since >= 3.0:
                return
            time.sleep(0.2)
        visible = ", ".join(repr(title) for title in remaining) or "onbekend intern venster"
        raise RosterExportError(
            "Gym Assistant kon na de export niet veilig naar het hoofdscherm terugkeren. "
            f"Achtergebleven venster(s): {visible}."
        )

    def run(self) -> dict:
        main = self._main_window()
        self._assert_clean_start(main)
        self.candidate_path.parent.mkdir(parents=True, exist_ok=True)
        self.candidate_path.unlink(missing_ok=True)
        special: WindowInfo | None = None
        report_window: WindowInfo | None = None
        try:
            special = self._authenticate(main)
            export_dialog = self._choose_export_command(special)
            report_window = self._configure_filters(export_dialog)
            self.ui.click(self.ui.button(report_window.handle, "Export").handle)
            exported_records = self._answer_export_prompts(main.process_id)
            wait_for_stable_file(self.candidate_path)
            return {
                "status": "exported",
                "candidate": str(self.candidate_path),
                "records_exported": exported_records,
                "size_bytes": self.candidate_path.stat().st_size,
                "sha256": sha256_file(self.candidate_path),
            }
        except Exception:
            self.candidate_path.unlink(missing_ok=True)
            raise
        finally:
            self._cleanup_export_windows(main)


def run_export(args: argparse.Namespace) -> dict:
    candidate = Path(args.candidate).resolve()
    target = Path(args.target).resolve()
    state_path = Path(args.state_path).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    policy = ValidationPolicy(
        minimum_members=args.minimum_members,
        max_count_change_percent=args.max_count_change_percent,
    )
    started = utc_now()
    target_guard: PublishedTargetGuard | None = None
    try:
        with NamedMutex(), pause_signup_bridge(
            Path(args.bridge_work_root).resolve() if args.bridge_work_root else None,
            timeout_seconds=args.bridge_timeout_seconds,
        ):
            target_guard = create_published_target_guard(target, backup_dir)
            exporter = GymAssistantExporter(
                executable=Path(args.gymassistant_exe).resolve(),
                expected_data_path=args.expected_data_path,
                candidate_path=candidate,
                credential_path=Path(args.credential_path).resolve(),
                ui_timeout_seconds=args.ui_timeout_seconds,
                manual_auth=args.manual_auth,
            )
            export_info = exporter.run()
            if datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc) < started:
                candidate.unlink(missing_ok=True)
                raise RosterExportError("Gym Assistant leverde geen nieuw kandidaatbestand op.")
            if not published_target_matches_guard(target_guard):
                restore_published_target(target_guard)
                candidate.unlink(missing_ok=True)
                raise RosterExportError(
                    "Gym Assistant wijzigde onverwacht de bestaande gepubliceerde ledenlijst; "
                    "de oorspronkelijke lijst is hersteld en de nieuwe export is afgewezen."
                )
            promotion = promote_candidate(
                candidate,
                target,
                backup_dir=backup_dir,
                state_path=state_path,
                policy=policy,
            )
    except CandidateRejectedError:
        if target_guard:
            restore_published_target(target_guard)
        raise
    except KeyboardInterrupt:
        if target_guard:
            restore_published_target(target_guard)
        candidate.unlink(missing_ok=True)
        atomic_write_json(
            state_path,
            {
                "schema_version": 1,
                "status": "interrupted",
                "interrupted_at": utc_now().isoformat(),
                "target": str(target),
            },
        )
        raise
    except Exception as exc:
        failure: Exception = exc
        if target_guard:
            try:
                restore_published_target(target_guard)
            except Exception as restore_exc:
                failure = RosterExportError(
                    f"{exc} Aanvullende herstelfout: {restore_exc}"
                )
        candidate.unlink(missing_ok=True)
        atomic_write_json(
            state_path,
            {
                "schema_version": 1,
                "status": "failed",
                "failed_at": utc_now().isoformat(),
                "target": str(target),
                "error": str(failure),
            },
        )
        if failure is exc:
            raise
        raise failure from exc
    finally:
        release_published_target_guard(target_guard)
    return {
        "status": "published",
        "export": export_info,
        "target": promotion.target,
        "backup": promotion.backup,
        "validation": asdict(promotion.report),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dreamz Gym Assistant official-roster export")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Valideer of publiceer een kandidaat-CSV")
    validate.add_argument("--candidate", required=True)
    validate.add_argument("--target")
    validate.add_argument("--minimum-members", type=int, default=DEFAULT_MINIMUM_MEMBERS)
    validate.add_argument(
        "--max-count-change-percent", type=float, default=DEFAULT_MAX_COUNT_CHANGE_PERCENT
    )
    validate.add_argument("--promote", action="store_true")
    validate.add_argument("--backup-dir")
    validate.add_argument("--state-path")

    credential = subparsers.add_parser("credential", help="Beheer DPAPI Master Access-invoer")
    credential.add_argument("action", choices=("set", "check", "delete"))
    credential.add_argument("--path", required=True)

    inspect = subparsers.add_parser("inspect", help="Toon Gym Assistant vensters en controls")
    inspect.add_argument("--expected-data-path", required=True)
    inspect.add_argument("--gymassistant-exe")

    export = subparsers.add_parser("export", help="Exporteer, valideer en publiceer MemberData.csv")
    export.add_argument("--gymassistant-exe", required=True)
    export.add_argument("--expected-data-path", required=True)
    export.add_argument("--candidate", required=True)
    export.add_argument("--target", required=True)
    export.add_argument("--backup-dir", required=True)
    export.add_argument("--state-path", required=True)
    export.add_argument("--credential-path", required=True)
    export.add_argument("--bridge-work-root")
    export.add_argument("--minimum-members", type=int, default=DEFAULT_MINIMUM_MEMBERS)
    export.add_argument(
        "--max-count-change-percent", type=float, default=DEFAULT_MAX_COUNT_CHANGE_PERCENT
    )
    export.add_argument("--ui-timeout-seconds", type=float, default=DEFAULT_UI_TIMEOUT_SECONDS)
    export.add_argument(
        "--bridge-timeout-seconds", type=float, default=DEFAULT_BRIDGE_TIMEOUT_SECONDS
    )
    export.add_argument("--manual-auth", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            policy = ValidationPolicy(args.minimum_members, args.max_count_change_percent)
            if args.promote:
                if not args.target or not args.backup_dir or not args.state_path:
                    raise RosterExportError(
                        "--promote vereist --target, --backup-dir en --state-path."
                    )
                result = promote_candidate(
                    args.candidate,
                    args.target,
                    backup_dir=args.backup_dir,
                    state_path=args.state_path,
                    policy=policy,
                )
                payload = asdict(result)
            else:
                payload = asdict(
                    validate_candidate(args.candidate, previous=args.target, policy=policy)
                )
        elif args.command == "credential":
            path = Path(args.path).resolve()
            if args.action == "set":
                first = getpass("Gym Assistant Master Access-login of wachtwoord: ")
                second = getpass("Herhaal de invoer: ")
                if first != second:
                    raise RosterExportError("De twee invoeren komen niet overeen.")
                save_credential(path, first)
                first = second = ""
                payload = {"status": "stored", "path": str(path)}
            elif args.action == "check":
                secret = load_credential(path)
                valid = bool(secret)
                secret = ""
                payload = {"status": "readable" if valid else "invalid", "path": str(path)}
            else:
                path.unlink(missing_ok=True)
                payload = {"status": "deleted", "path": str(path)}
        elif args.command == "inspect":
            ui = Win32UI()
            main_window = _find_gymassistant_main(ui, args.expected_data_path)
            if not main_window and args.gymassistant_exe:
                main_window = _start_gymassistant(
                    Path(args.gymassistant_exe).resolve(), args.expected_data_path, ui
                )
            if not main_window:
                raise RosterExportError("Gym Assistant met het verwachte datapad is niet actief.")
            payload = {"windows": _dump_ui(ui, main_window.process_id)}
        else:
            payload = run_export(args)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    except RosterExportError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
