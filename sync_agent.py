from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
from typing import Callable, Iterable, Mapping
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote
import zipfile

from ga_import import (
    ImportIssue,
    ImportResult,
    parse_gymassistant_export,
    parse_member_log_events,
)
from ga_journal import (
    parse_gymassistant_billing_catalog_text,
    service_period_matches_catalog_interval,
)
from ga_invoice_target_probe import scan_target_invoice_membership_events
from ga_journal_snapshot import parse_member_scoped_journal_snapshot_bytes
from gymassistant_journal_export import (
    SOURCE_KIND as OFFICIAL_JOURNAL_EXPORT_SOURCE_KIND,
    JournalExportConfig,
    OfficialJournalExportSnapshot,
    export_official_journal_snapshot,
    load_journal_export_config,
)
from ga_documents import infer_member_document_records
from portal_sync_journal_evidence import (
    process_journal_evidence_queue_if_configured,
)
from storage_backend import s3_client, upload_file_to_s3


DEFAULT_GYM_ASSISTANT_ROOT = Path(r"D:\Dreamz Fitness\Gym Assistant 2.6")
DEFAULT_MANIFEST_PATH = Path("instance/sync_manifest.json")
DEFAULT_STORAGE_PREFIX = "gymassistant"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
LIVE_MEMBER_DATA_WARNING_GRACE_SECONDS = 24 * 60 * 60
DEFAULT_OFFICIAL_CSV_MAX_AGE_HOURS = 36
DEFAULT_MEMBER_SYNC_API_TIMEOUT_SECONDS = 180
MIN_MEMBER_SYNC_API_TIMEOUT_SECONDS = 30
MAX_MEMBER_SYNC_API_TIMEOUT_SECONDS = 900
OFFICIAL_MEMBER_SOURCE_KIND = "gymassistant_official_member_export_csv"
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class FileSignature:
    path: str
    kind: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SyncScan:
    source_root: str
    scanned_at: str
    member_source: str | None
    latest_backup: str | None
    warning: str | None
    member_count: int
    relevant_file_count: int
    relevant_total_bytes: int
    files: list[FileSignature]


@dataclass(frozen=True)
class SyncDiff:
    added: list[str]
    changed: list[str]
    removed: list[str]


@dataclass(frozen=True)
class MemberOverlayEvent:
    occurred_at: datetime
    priority: int
    row_number: int
    path: Path
    action: str
    member: dict


@dataclass(frozen=True)
class StableExactFileSnapshot:
    data: bytes
    byte_length: int
    source_sha256: str
    file_identity_sha256: str
    stable_read_count: int


@dataclass(frozen=True)
class _ExactFileRead:
    data: bytes
    byte_length: int
    mtime_ns: int
    file_identity: tuple[int, int]
    file_identity_sha256: str


class JournalSourceUnstableError(RuntimeError):
    pass


class JournalSourceIdentityChangedError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_root(source_root: Path) -> Path:
    return source_root / "Data"


def attachments_root(source_root: Path) -> Path:
    return data_root(source_root) / "Attachments"


def photos_root(source_root: Path) -> Path:
    return data_root(source_root) / "Pictures"


def backup_root(source_root: Path) -> Path:
    roots = backup_root_candidates(source_root)
    return next((root for root in roots if root.exists()), roots[0])


def temp_files_root(source_root: Path) -> Path:
    return data_root(source_root) / "Temp Files"


def live_members_path(source_root: Path) -> Path:
    configured_path = os.getenv("GYM_ASSISTANT_MEMBERS_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    candidates = [
        data_root(source_root) / "Members.btx",
        source_root / "Members.btx",
    ]
    candidates.extend(root / "Members.btx" for root in backup_root_candidates(source_root))
    return next((path for path in candidates if path.is_file()), candidates[0])


def live_members_dat_path(source_root: Path) -> Path:
    return data_root(source_root) / "Members.dat"


def live_journal_path(source_root: Path) -> Path:
    candidates = []
    configured_path = os.getenv("GYM_ASSISTANT_JOURNAL_PATH", "").strip()
    if configured_path:
        candidates.append(Path(configured_path))
    candidates.extend([
        data_root(source_root) / "Journal.jtx",
        source_root / "Journal.jtx",
    ])
    candidates.extend(root / "Journal.jtx" for root in backup_root_candidates(source_root))
    return next((path for path in candidates if path.is_file()), candidates[0])


def _path_is_symlink_or_reparse_point(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        path.is_symlink()
        or (
            int(getattr(path_stat, "st_file_attributes", 0))
            & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def authoritative_live_journal_path(source_root: Path) -> Path:
    """Return only the authoritative live binary Data/Journal.dat.

    The binary file is used only to bind an official Gym Assistant journal
    export to the live data directory. It is never parsed as JTX. Invoice sync
    may still fall back to backups through ``live_journal_path``; preservation
    evidence must fail closed rather than observe a backup or alternate source.
    """
    source_root = Path(source_root)
    configured_data_directory = source_root / "Data"
    if _path_is_symlink_or_reparse_point(configured_data_directory):
        raise ValueError("authoritative live journal data path is a reparse point")
    data_directory = configured_data_directory.resolve(strict=True)
    candidate = configured_data_directory / "Journal.dat"
    if any(part.casefold() == "backup" for part in candidate.parts):
        raise ValueError("authoritative live journal cannot be under a backup root")
    if _path_is_symlink_or_reparse_point(candidate):
        raise ValueError("authoritative live Journal.dat is a reparse point")
    if not candidate.is_file():
        raise FileNotFoundError("authoritative live Data/Journal.dat is missing")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != data_directory or resolved.name.casefold() != "journal.dat":
        raise ValueError("authoritative live journal data-path identity drifted")
    return resolved


def backup_root_candidates(source_root: Path) -> list[Path]:
    candidates = []
    configured_root = os.getenv("GYM_ASSISTANT_BACKUP_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root))
    candidates.extend([
        data_root(source_root) / "Backup",
        source_root / "Backup",
    ])
    if source_root.name.lower() == "backup":
        candidates.append(source_root)
    unique = []
    seen = set()
    for candidate in candidates:
        normalized = str(candidate.resolve()).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return unique


def latest_backup_path(source_root: Path) -> Path | None:
    backups = [
        path
        for root in backup_root_candidates(source_root)
        if root.exists()
        for path in root.glob("*.gbu")
        if path.is_file()
    ]
    return max(backups, key=lambda path: path.stat().st_mtime) if backups else None


def backup_contains_invoice_snapshot(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as backup:
            names = {Path(name).name.casefold() for name in backup.namelist()}
    except (OSError, zipfile.BadZipFile):
        return False
    return {"journal.jtx", "members.btx"}.issubset(names)


def latest_invoice_backup_path(source_root: Path) -> Path | None:
    backups = [
        path
        for root in backup_root_candidates(source_root)
        if root.exists()
        for path in root.glob("*.gbu")
        if path.is_file() and backup_contains_invoice_snapshot(path)
    ]
    return max(backups, key=lambda path: path.stat().st_mtime) if backups else None


def read_stable_file_snapshot(path: Path) -> tuple[bytes, str, str]:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise ValueError(f"GymAssistant source changed while it was being read: {path}")
    snapshot_at = (
        datetime.fromtimestamp(after.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
    return data, snapshot_at, hashlib.sha256(data).hexdigest()


def _canonical_fingerprint(value: dict) -> str:
    encoded = json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_identity(stat_result: os.stat_result) -> tuple[int, int]:
    identity = (int(stat_result.st_dev), int(stat_result.st_ino))
    if identity[1] <= 0:
        raise JournalSourceIdentityChangedError(
            "authoritative live journal has no usable file identity"
        )
    return identity


def _read_exact_file_once(path: Path) -> _ExactFileRead:
    try:
        if _path_is_symlink_or_reparse_point(path):
            raise JournalSourceIdentityChangedError(
                "authoritative live journal is a reparse point"
            )
        resolved_before = path.resolve(strict=True)
        path_before = resolved_before.stat()
        if not stat.S_ISREG(path_before.st_mode):
            raise JournalSourceIdentityChangedError(
                "authoritative live journal is not a regular file"
            )
        with resolved_before.open("rb") as handle:
            descriptor_before = os.fstat(handle.fileno())
            data = handle.read()
            descriptor_after = os.fstat(handle.fileno())
        if _path_is_symlink_or_reparse_point(path):
            raise JournalSourceIdentityChangedError(
                "authoritative live journal became a reparse point"
            )
        resolved_after = path.resolve(strict=True)
        path_after = resolved_after.stat()
    except JournalSourceIdentityChangedError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise JournalSourceUnstableError(
            "authoritative live journal could not be read stably"
        ) from exc

    identities = {
        _file_identity(path_before),
        _file_identity(descriptor_before),
        _file_identity(descriptor_after),
        _file_identity(path_after),
    }
    if resolved_before != resolved_after or len(identities) != 1:
        raise JournalSourceIdentityChangedError(
            "authoritative live journal was replaced during observation"
        )
    if (
        descriptor_before.st_size != descriptor_after.st_size
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or path_before.st_size != path_after.st_size
        or path_before.st_mtime_ns != path_after.st_mtime_ns
        or descriptor_after.st_size != path_after.st_size
        or len(data) != descriptor_after.st_size
    ):
        raise JournalSourceUnstableError(
            "authoritative live journal changed during observation"
        )

    identity = identities.pop()
    return _ExactFileRead(
        data=data,
        byte_length=len(data),
        mtime_ns=int(path_after.st_mtime_ns),
        file_identity=identity,
        file_identity_sha256=_canonical_fingerprint(
            {
                "device": str(identity[0]),
                "file_index": str(identity[1]),
            }
        ),
    )


def read_stable_file_snapshot_twice(
    path: Path,
    *,
    max_attempts: int = 3,
) -> StableExactFileSnapshot:
    """Require two consecutive byte-identical reads of the same exact file."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    path = Path(path)
    last_error: Exception | None = None
    for _attempt in range(max_attempts):
        try:
            first = _read_exact_file_once(path)
            second = _read_exact_file_once(path)
        except JournalSourceIdentityChangedError:
            raise
        except JournalSourceUnstableError as exc:
            last_error = exc
            continue
        if first.file_identity != second.file_identity:
            raise JournalSourceIdentityChangedError(
                "authoritative live journal was replaced between exact reads"
            )
        if (
            first.data == second.data
            and first.byte_length == second.byte_length
            and first.mtime_ns == second.mtime_ns
            and first.file_identity_sha256 == second.file_identity_sha256
        ):
            return StableExactFileSnapshot(
                data=second.data,
                byte_length=second.byte_length,
                source_sha256=hashlib.sha256(second.data).hexdigest(),
                file_identity_sha256=second.file_identity_sha256,
                stable_read_count=2,
            )
        last_error = JournalSourceUnstableError(
            "authoritative live journal differed between exact reads"
        )
    raise JournalSourceUnstableError(
        "authoritative live journal did not produce two stable exact reads"
    ) from last_error


def build_existing_member_journal_evidence(
    source_root: Path,
    *,
    member_number: str | int,
    source_coverage_proven: bool = False,
    max_read_attempts: int = 3,
    export_config: JournalExportConfig | None = None,
    environ: Mapping[str, str] | None = None,
    snapshot_exporter: Callable[..., OfficialJournalExportSnapshot] = (
        export_official_journal_snapshot
    ),
) -> dict:
    """Build the unsigned, privacy-safe Portal Sync evidence core.

    This helper intentionally does not claim a Portal request, sign an envelope
    or post a result. Source coverage defaults to unproven, so an unwired caller
    cannot trigger Gym Assistant or accidentally create complete evidence.
    ``Journal.dat`` is checked only as the live source identity; all scoped
    records come from a fresh, validated official ``Export Journal`` result.
    """
    if not isinstance(source_coverage_proven, bool):
        raise ValueError("source_coverage_proven must be a boolean")
    if not source_coverage_proven:
        raise ValueError("official journal export source coverage is unproven")
    source_root = Path(source_root)
    data_directory = (source_root / "Data").resolve(strict=True)
    journal_path = authoritative_live_journal_path(source_root)
    if journal_path.parent != data_directory:
        raise ValueError("authoritative live journal data-path identity drifted")
    config = export_config or load_journal_export_config(
        source_root,
        environ=environ,
    )
    if Path(config.source_root).resolve(strict=True) != source_root.resolve(strict=True):
        raise ValueError("journal export source-root identity drifted")
    exported = snapshot_exporter(
        source_root=source_root,
        executable=config.executable,
        expected_data_path=config.expected_data_path,
        candidate_path=config.candidate_path,
        credential_path=config.credential_path,
        bridge_work_root=config.bridge_work_root,
        source_coverage_proven=source_coverage_proven,
        max_read_attempts=max_read_attempts,
    )
    scope = parse_member_scoped_journal_snapshot_bytes(
        exported.data,
        member_number=member_number,
        source_coverage_proven=source_coverage_proven,
    )
    return {
        "schema": "dreamz.portal-sync.member-journal-evidence-core.v1",
        "member_number": scope.member_number,
        "observed_at": utc_now_iso(),
        "source": {
            "kind": OFFICIAL_JOURNAL_EXPORT_SOURCE_KIND,
            "locator_fingerprint_sha256": exported.locator_fingerprint_sha256,
            "data_path_fingerprint_sha256": (
                exported.data_path_fingerprint_sha256
            ),
            "file_identity_sha256": exported.source_binding_sha256,
            "byte_length": exported.byte_length,
            "source_sha256": exported.source_sha256,
            "stable_read_count": exported.stable_read_count,
        },
        "scope": scope.as_evidence_scope(),
    }


def backup_snapshot_entries(data: bytes) -> tuple[bytes, bytes]:
    with zipfile.ZipFile(BytesIO(data)) as backup:
        journal_entry = next(
            (
                name
                for name in backup.namelist()
                if Path(name).name.casefold() == "journal.jtx"
            ),
            None,
        )
        members_entry = next(
            (
                name
                for name in backup.namelist()
                if Path(name).name.casefold() == "members.btx"
            ),
            None,
        )
        if not journal_entry or not members_entry:
            raise ValueError(
                "GymAssistant backup must contain Journal.jtx and Members.btx."
            )
        journal_bytes = backup.read(journal_entry)
        members_bytes = backup.read(members_entry)
    return journal_bytes, members_bytes


def iter_member_log_files(source_root: Path, backup: Path | None = None) -> Iterable[Path]:
    root = temp_files_root(source_root)
    if not root.exists():
        return

    # AddedMembers.btx is cumulative and has no event timestamps. Replaying it
    # after a new addition can resurrect members absent from the official CSV.
    candidates = [
        root / "Added Members.txt",
        root / "Deleted Members.txt",
    ]
    update_root = root / "Member Updates"
    if update_root.exists():
        candidates.extend(sorted(update_root.glob("EditMembers*.txt"), key=lambda path: path.stat().st_mtime))

    backup_mtime = backup.stat().st_mtime if backup else None
    seen: set[Path] = set()
    for path in candidates:
        if not path.exists() or not path.is_file() or path in seen:
            continue
        seen.add(path)
        if backup_mtime is not None and path.stat().st_mtime <= backup_mtime:
            continue
        yield path


def live_member_data_warning(source_root: Path, backup: Path | None) -> str | None:
    live_dat = live_members_dat_path(source_root)
    if not backup or not live_dat.exists():
        return None
    live_age_delta = live_dat.stat().st_mtime - backup.stat().st_mtime
    if live_age_delta > LIVE_MEMBER_DATA_WARNING_GRACE_SECONDS and not any(iter_member_log_files(source_root, backup)):
        return (
            "GymAssistant Members.dat is more than 24 hours newer than the latest .gbu backup. "
            "The portal imported the latest backup, so recently added or edited members may not appear until a new GymAssistant backup is created."
        )
    return None


def member_log_action(path: Path) -> str:
    normalized = path.name.casefold().replace(" ", "")
    if normalized == "deletedmembers.txt":
        return "delete"
    if normalized in {"addedmembers.btx", "addedmembers.txt"}:
        return "add"
    return "update"


def member_source_kind(path: Path) -> str:
    if path.suffix.casefold() == ".csv":
        return OFFICIAL_MEMBER_SOURCE_KIND
    if path.suffix.casefold() == ".gbu":
        return "gymassistant_backup"
    if path.suffix.casefold() == ".btx":
        return "gymassistant_members_btx"
    return "gymassistant_export"


def official_csv_max_age_hours() -> int:
    raw = os.getenv(
        "GYM_ASSISTANT_CSV_MAX_AGE_HOURS",
        str(DEFAULT_OFFICIAL_CSV_MAX_AGE_HOURS),
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OFFICIAL_CSV_MAX_AGE_HOURS
    return max(value, 1)


def member_sync_api_timeout_seconds() -> int:
    raw = os.getenv(
        "MEMBER_SYNC_API_TIMEOUT_SECONDS",
        str(DEFAULT_MEMBER_SYNC_API_TIMEOUT_SECONDS),
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MEMBER_SYNC_API_TIMEOUT_SECONDS
    return min(
        max(value, MIN_MEMBER_SYNC_API_TIMEOUT_SECONDS),
        MAX_MEMBER_SYNC_API_TIMEOUT_SECONDS,
    )


def official_csv_source_warning(path: Path) -> str | None:
    now = datetime.now(timezone.utc)
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    if modified_at > now + timedelta(minutes=5):
        return (
            "The official GymAssistant member CSV has a future modification time. "
            "The member set was imported as non-authoritative."
        )
    age = now - modified_at
    max_age = timedelta(hours=official_csv_max_age_hours())
    if age > max_age:
        return (
            "The official GymAssistant member CSV is stale "
            f"({age.total_seconds() / 3600:.1f} hours old; "
            f"maximum {official_csv_max_age_hours()} hours). "
            "The member set was imported as non-authoritative."
        )
    return None


def member_source_warning(
    source_root: Path,
    member_source: Path,
    backup: Path | None,
) -> str | None:
    if member_source.suffix.casefold() == ".csv":
        return official_csv_source_warning(member_source)
    return live_member_data_warning(source_root, backup)


def parse_member_source_with_live_logs(
    member_source: Path,
    source_root: Path | None = None,
) -> tuple[ImportResult, dict]:
    result = parse_gymassistant_export(member_source)
    member_map = {str(member["member_id"]): dict(member) for member in result.members}
    issues: list[ImportIssue] = list(result.issues)
    inferred_source_root = None
    if member_source.parent.name.lower() == "backup":
        if member_source.parent.parent.name.lower() == "data":
            inferred_source_root = member_source.parent.parent.parent
        else:
            inferred_source_root = member_source.parent.parent
    source_root = (source_root or inferred_source_root)

    baseline_at = datetime.fromtimestamp(member_source.stat().st_mtime)
    pending_events: list[MemberOverlayEvent] = []
    overlay_files: list[dict] = []
    ignored_before_baseline = 0
    overlay_stable = True
    if source_root:
        for log_file in iter_member_log_files(source_root, member_source):
            before = log_file.stat()
            log_result = parse_member_log_events(log_file)
            after = log_file.stat()
            file_stable = (
                before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
            )
            overlay_stable = overlay_stable and file_stable
            issues.extend(log_result.issues)
            action = member_log_action(log_file)
            priority = {"add": 0, "update": 1, "delete": 2}[action]
            file_event_at = datetime.fromtimestamp(after.st_mtime)
            overlay_files.append({
                "path": relative_path(log_file.resolve(), source_root.resolve()),
                "size": after.st_size,
                "mtime": datetime.fromtimestamp(
                    after.st_mtime,
                    timezone.utc,
                ).replace(microsecond=0).isoformat(),
                "sha256": file_sha256(log_file),
                "stable_during_read": file_stable,
            })
            for event in log_result.events:
                occurred_at = event.occurred_at or file_event_at
                if occurred_at <= baseline_at:
                    ignored_before_baseline += 1
                    continue
                pending_events.append(
                    MemberOverlayEvent(
                        occurred_at=occurred_at,
                        priority=priority,
                        row_number=event.row_number,
                        path=log_file,
                        action=action,
                        member=dict(event.member),
                    )
                )

    pending_events.sort(
        key=lambda event: (
            event.occurred_at,
            event.priority,
            str(event.path).casefold(),
            event.row_number,
        )
    )
    added = updated = deleted = orphan_updates = 0
    event_digest = hashlib.sha256()
    for event in pending_events:
        member_id = str(event.member["member_id"])
        event_digest.update(
            json.dumps(
                {
                    "occurred_at": event.occurred_at.isoformat(),
                    "action": event.action,
                    "member_id": member_id,
                    "member": json_safe_member(event.member),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if event.action == "delete":
            if member_map.pop(member_id, None) is not None:
                deleted += 1
            continue
        if event.action == "add" and member_id in member_map:
            continue
        if member_id in member_map:
            member_map[member_id].update(dict(event.member))
            updated += 1
        else:
            if (
                event.action == "update"
                and member_source.suffix.casefold() == ".csv"
            ):
                orphan_updates += 1
                issues.append(
                    ImportIssue(
                        event.row_number,
                        "member_id",
                        member_id,
                        "Edit event references a member absent from the official CSV",
                        critical=True,
                    )
                )
                continue
            new_member = dict(event.member)
            if (
                not str(new_member.get("billing_status") or "").strip()
                or new_member.get("is_active") is None
            ):
                new_member["billing_status"] = "UNKNOWN"
                new_member["is_active"] = None
                issues.append(
                    ImportIssue(
                        event.row_number,
                        "billing_status",
                        "",
                        "Added member event is missing a valid GymAssistant ST status",
                        critical=True,
                    )
                )
            member_map[member_id] = new_member
            added += 1

    overlay = {
        "baseline_count": len(result.members),
        "event_file_count": len(overlay_files),
        "event_count": len(pending_events),
        "added_count": added,
        "updated_count": updated,
        "deleted_count": deleted,
        "orphan_update_count": orphan_updates,
        "ignored_before_baseline_count": ignored_before_baseline,
        "latest_event_at": (
            pending_events[-1].occurred_at.isoformat()
            if pending_events
            else None
        ),
        "events_sha256": event_digest.hexdigest(),
        "stable_during_read": overlay_stable,
        "files": overlay_files,
    }
    return ImportResult(list(member_map.values()), issues), overlay


def parse_members_with_live_logs(
    member_source: Path,
    source_root: Path | None = None,
) -> ImportResult:
    result, _ = parse_member_source_with_live_logs(
        member_source,
        source_root=source_root,
    )
    return result


def configured_invoice_pilot_member_ids() -> set[str]:
    raw = os.getenv("INVOICE_GA_PILOT_MEMBER_IDS", "")
    return {
        member_id.strip()
        for member_id in raw.split(",")
        if member_id.strip().isdigit() and int(member_id.strip()) > 0
    }


def invoice_membership_event_payload(
    source_root: Path,
    backup: Path | None,
    member_ids: set[str],
) -> tuple[
    list[dict],
    int,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    if not member_ids:
        return [], 0, None, None, None, None, None

    invoice_backup = latest_invoice_backup_path(source_root)
    journal = live_journal_path(source_root)
    members_path = live_members_path(source_root)
    source = None
    catalog_source = None
    source_snapshot_at = None
    source_sha256 = None
    catalog_sha256 = None
    catalog_issue_count = 0
    catalog_options = []
    target_scan = None
    try:
        if invoice_backup:
            (
                backup_bytes,
                source_snapshot_at,
                source_sha256,
            ) = read_stable_file_snapshot(invoice_backup)
            journal_bytes, members_bytes = backup_snapshot_entries(backup_bytes)
            target_scan = scan_target_invoice_membership_events(
                journal_bytes,
                member_ids,
            )
            catalog_options, _ = parse_gymassistant_billing_catalog_text(
                members_bytes.decode("latin-1", errors="replace")
            )
            source = catalog_source = str(invoice_backup)
            catalog_sha256 = hashlib.sha256(members_bytes).hexdigest()
        elif journal.is_file() and members_path.is_file():
            (
                journal_bytes,
                journal_snapshot_at,
                source_sha256,
            ) = read_stable_file_snapshot(journal)
            (
                members_bytes,
                members_snapshot_at,
                catalog_sha256,
            ) = read_stable_file_snapshot(members_path)
            target_scan = scan_target_invoice_membership_events(
                journal_bytes,
                member_ids,
            )
            catalog_options, _ = parse_gymassistant_billing_catalog_text(
                members_bytes.decode("latin-1", errors="replace")
            )
            source = str(journal)
            catalog_source = str(members_path)
            source_snapshot_at = min(
                journal_snapshot_at,
                members_snapshot_at,
            )
        else:
            return [], 1, None, None, None, None, None
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return (
            [],
            1,
            f"GymAssistant invoice snapshot could not be read: {exc}",
            None,
            None,
            None,
            None,
        )
    if target_scan is None:
        return [], 1, None, None, None, None, None
    options_by_key = {}
    for option in catalog_options:
        key = (option.membership_type_id, option.billing_option_code)
        if key in options_by_key:
            catalog_issue_count += 1
            continue
        options_by_key[key] = option
    issue_count = target_scan.target_issue_count + catalog_issue_count
    if issue_count:
        return (
            [],
            issue_count,
            source,
            catalog_source,
            source_snapshot_at,
            source_sha256,
            catalog_sha256,
        )
    records = []
    for event in target_scan.events:
        record = event.as_sync_record()
        option = options_by_key.get((event.membership_type_id, event.billing_option_code))
        if option:
            record["catalog_plan_name"] = option.plan_name
            record["catalog_base_amount_cents"] = option.base_amount_cents
            record["catalog_interval_count"] = option.interval_count
            record["catalog_interval_unit"] = option.interval_unit
            record["catalog_match_status"] = (
                "match"
                if event.dues_cents == option.base_amount_cents
                else "mismatch"
            )
            record["catalog_period_match_status"] = (
                "match"
                if service_period_matches_catalog_interval(
                    event.service_period_start,
                    event.service_period_end_exclusive,
                    option.interval_count,
                    option.interval_unit,
                )
                else "mismatch"
            )
        else:
            record["catalog_plan_name"] = None
            record["catalog_base_amount_cents"] = None
            record["catalog_interval_count"] = None
            record["catalog_interval_unit"] = None
            record["catalog_match_status"] = "missing"
            record["catalog_period_match_status"] = "missing"
        records.append(record)

    return (
        records,
        0,
        source,
        catalog_source,
        source_snapshot_at,
        source_sha256,
        catalog_sha256,
    )


def relative_path(path: Path, source_root: Path) -> str:
    try:
        return path.relative_to(source_root).as_posix()
    except ValueError:
        parent_fingerprint = hashlib.sha256(str(path.parent).casefold().encode("utf-8")).hexdigest()[:12]
        return f"_external/{parent_fingerprint}/{path.name}"


def file_signature(path: Path, source_root: Path, kind: str) -> FileSignature:
    stat = path.stat()
    return FileSignature(
        path=relative_path(path, source_root),
        kind=kind,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def iter_attachment_files(source_root: Path) -> Iterable[FileSignature]:
    root = attachments_root(source_root)
    if not root.exists():
        return
    for path in root.rglob("*.pdf"):
        if path.is_file():
            yield file_signature(path, source_root, "attachment_pdf")


def iter_photo_files(source_root: Path) -> Iterable[FileSignature]:
    root = photos_root(source_root)
    if not root.exists():
        return
    for path in root.iterdir():
        if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS:
            yield file_signature(path, source_root, "photo")


def scan_source(source_root: Path) -> SyncScan:
    source_root = source_root.resolve()
    if not source_root.exists() and not any(root.exists() for root in backup_root_candidates(source_root)):
        raise FileNotFoundError(f"GymAssistant source root not found: {source_root}")

    backup = latest_backup_path(source_root)
    live_members = live_members_path(source_root)
    configured_member_source = bool(
        os.getenv("GYM_ASSISTANT_MEMBERS_PATH", "").strip()
    )
    if configured_member_source and not live_members.is_file():
        raise FileNotFoundError(
            f"Configured GymAssistant member source does not exist: {live_members}"
        )
    live_dat = live_members_dat_path(source_root)
    files: list[FileSignature] = []
    member_count = 0
    member_source: Path | None = None

    if live_members.exists():
        source_file_kind = (
            "member_export_csv"
            if live_members.suffix.casefold() == ".csv"
            else "member_data"
        )
        files.append(file_signature(live_members, source_root, source_file_kind))
        member_source = live_members
        member_count = len(
            parse_member_source_with_live_logs(
                live_members,
                source_root=source_root,
            )[0].members
        )
    elif backup:
        member_source = backup
        member_count = len(
            parse_member_source_with_live_logs(
                backup,
                source_root=source_root,
            )[0].members
        )

    if backup:
        files.append(file_signature(backup, source_root, "backup"))
    if live_dat.exists():
        files.append(file_signature(live_dat, source_root, "live_member_data"))
    for log_file in iter_member_log_files(source_root, member_source):
        files.append(file_signature(log_file, source_root, "member_log"))

    files.extend(iter_attachment_files(source_root) or [])
    files.extend(iter_photo_files(source_root) or [])
    files.sort(key=lambda item: item.path)

    return SyncScan(
        source_root=str(source_root),
        scanned_at=utc_now_iso(),
        member_source=str(member_source) if member_source else None,
        latest_backup=str(backup) if backup else None,
        warning=(
            member_source_warning(source_root, member_source, backup)
            if member_source
            else None
        ),
        member_count=member_count,
        relevant_file_count=len(files),
        relevant_total_bytes=sum(item.size for item in files),
        files=files,
    )


def manifest_payload(scan: SyncScan) -> dict:
    return {
        "source_root": scan.source_root,
        "scanned_at": scan.scanned_at,
        "member_source": scan.member_source,
        "latest_backup": scan.latest_backup,
        "warning": scan.warning,
        "member_count": scan.member_count,
        "files": [asdict(item) for item in scan.files],
    }


def json_safe_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def json_safe_member(member: dict) -> dict:
    return {
        key: json_safe_value(value)
        for key, value in member.items()
    }


def storage_key(prefix: str, relative_file_path: str) -> str:
    prefix = prefix.strip("/")
    relative_file_path = relative_file_path.replace("\\", "/").lstrip("/")
    return f"{prefix}/{relative_file_path}" if prefix else relative_file_path


def stored_s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def versioned_photo_storage_key(source_root: Path, path: Path, prefix: str) -> str:
    relative = Path(relative_path(path.resolve(), source_root.resolve()))
    version = file_sha256(path)[:16]
    versioned_name = f"{relative.stem}-{version}{relative.suffix.lower()}"
    return storage_key(prefix, relative.with_name(versioned_name).as_posix())


def member_photo_path(member_id: str, source_root: Path) -> Path | None:
    root = photos_root(source_root)
    if not root.exists():
        return None

    stem = str(member_id).strip().zfill(7)
    for extension in sorted(PHOTO_EXTENSIONS):
        candidate = root / f"{stem}{extension}"
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def upload_source_file(source_root: Path, path: Path, prefix: str, client=None) -> str:
    relative = relative_path(path.resolve(), source_root.resolve())
    return upload_file_to_s3(path, storage_key(prefix, relative), client=client)


def post_file(url: str, token: str, path: Path, key: str, timeout: int = 180) -> dict:
    request = urlrequest.Request(
        url,
        data=path.read_bytes(),
        method="POST",
        headers={
            "X-Sync-Token": token,
            "X-Storage-Key": key,
            "X-Content-Type": "application/pdf" if path.suffix.lower() == ".pdf" else "image/jpeg",
        },
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"File upload API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach file upload API: {exc}") from exc


def upload_files_parallel(upload_tasks: list[tuple[str, Path]], portal_url: str, token: str, workers: int = 4) -> dict[str, str]:
    if not upload_tasks:
        return {}

    endpoint = portal_url.rstrip("/") + "/api/sync/files"
    completed = 0
    total = len(upload_tasks)
    results: dict[str, str] = {}

    def upload_one(task):
        key, path = task
        result = post_file(endpoint, token, path, key)
        uri = result.get("uri")
        if not uri:
            raise RuntimeError(f"File upload API did not return a URI for {path}")
        return key, uri

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(upload_one, task) for task in upload_tasks]
        for future in as_completed(futures):
            key, uri = future.result()
            results[key] = uri
            completed += 1
            if completed == 1 or completed % 50 == 0 or completed == total:
                print(f"Uploaded {completed}/{total} files")

    return results


def upload_file_to_portal(source_root: Path, path: Path, prefix: str, portal_url: str, token: str) -> str:
    relative = relative_path(path.resolve(), source_root.resolve())
    key = storage_key(prefix, relative)
    endpoint = portal_url.rstrip("/") + "/api/sync/files"
    result = post_file(endpoint, token, path, key)
    uri = result.get("uri")
    if not uri:
        raise RuntimeError(f"File upload API did not return a URI for {path}")
    return uri


def uploaded_source_file(
    source_root: Path,
    path: Path,
    prefix: str,
    client=None,
    portal_url: str | None = None,
    sync_token: str | None = None,
) -> str:
    if portal_url and sync_token:
        return upload_file_to_portal(source_root, path, prefix, portal_url, sync_token)
    return upload_source_file(source_root, path, prefix, client=client)


def uploaded_document_records(
    source_root: Path,
    records: list[dict[str, str]],
    prefix: str,
    client=None,
    portal_url: str | None = None,
    sync_token: str | None = None,
) -> list[dict[str, str]]:
    uploaded = []
    for record in records:
        updated = dict(record)
        path = Path(updated.get("path") or "")
        if path.exists() and path.is_file():
            updated["path"] = uploaded_source_file(
                source_root,
                path,
                prefix,
                client=client,
                portal_url=portal_url,
                sync_token=sync_token,
            )
        uploaded.append(updated)
    return uploaded


def build_sync_payload(
    source_root: Path,
    member_limit: int | None = None,
    upload_files: bool = False,
    storage_prefix: str = DEFAULT_STORAGE_PREFIX,
    upload_via_portal: bool = False,
    portal_url: str | None = None,
    sync_token: str | None = None,
    upload_workers: int = 4,
    upload_changed_only: bool = False,
    changed_file_paths: set[str] | None = None,
    existing_member_ids: set[str] | None = None,
    missing_file_keys: set[str] | None = None,
    storage_bucket: str | None = None,
    invoice_member_ids: set[str] | None = None,
) -> dict:
    source_root = source_root.resolve()
    backup = latest_backup_path(source_root)
    member_source = live_members_path(source_root)
    configured_member_source = bool(
        os.getenv("GYM_ASSISTANT_MEMBERS_PATH", "").strip()
    )
    if configured_member_source and not member_source.is_file():
        raise FileNotFoundError(
            f"Configured GymAssistant member source does not exist: {member_source}"
        )
    if not member_source.exists():
        member_source = backup
    if not member_source:
        raise FileNotFoundError(
            "No configured GymAssistant member CSV, Members.btx, or .gbu backup "
            f"was found under {data_root(source_root)}"
        )

    member_source_before = member_source.stat()
    member_source_bytes = member_source.read_bytes()
    member_source_sha256 = hashlib.sha256(member_source_bytes).hexdigest()
    member_source_snapshot_at = datetime.fromtimestamp(
        member_source_before.st_mtime,
        timezone.utc,
    ).replace(microsecond=0).isoformat()
    import_result, member_overlay = parse_member_source_with_live_logs(
        member_source,
        source_root=source_root,
    )
    member_source_after = member_source.stat()
    member_source_stable = (
        member_source_before.st_size == member_source_after.st_size
        and member_source_before.st_mtime_ns == member_source_after.st_mtime_ns
        and len(member_source_bytes) == member_source_after.st_size
    )
    source_members = import_result.members[:member_limit] if member_limit else import_result.members
    members = [dict(member) for member in source_members]
    member_ids = [str(member.get("member_id") or "").strip() for member in members]
    unique_member_ids = sorted(set(member_ids))
    source_warning = member_source_warning(source_root, member_source, backup)
    scope_complete = member_limit is None
    skipped_record_count = sum(
        1
        for issue in import_result.issues
        if str(issue.message or "").casefold().startswith("skipped ")
    )
    critical_issue_count = sum(
        1
        for issue in import_result.issues
        if issue.critical
    )
    unknown_billing_status_count = sum(
        1
        for member in import_result.members
        if (
            not str(member.get("billing_status") or "").strip()
            or str(member.get("billing_status") or "").strip().upper() == "UNKNOWN"
            or member.get("is_active") is None
        )
    )
    if critical_issue_count:
        issue_warning = (
            "The GymAssistant member source contains "
            f"{critical_issue_count} critical parsing issue(s). "
            "The member set was imported as non-authoritative."
        )
        source_warning = (
            f"{source_warning}\n{issue_warning}"
            if source_warning
            else issue_warning
        )
        if member_source.suffix.casefold() == ".csv":
            raise ValueError(issue_warning)
    complete_member_source = (
        member_source.suffix.casefold() == ".csv"
        and member_source_kind(member_source) == OFFICIAL_MEMBER_SOURCE_KIND
    )
    source_age_seconds = max(
        0,
        int(
            (
                datetime.now(timezone.utc)
                - datetime.fromtimestamp(
                    member_source_after.st_mtime,
                    timezone.utc,
                )
            ).total_seconds()
        ),
    )
    member_ids_sha256 = hashlib.sha256(
        "\n".join(unique_member_ids).encode("utf-8")
    ).hexdigest()
    snapshot_id = hashlib.sha256(
        json.dumps(
            {
                "source_sha256": member_source_sha256,
                "source_mtime_ns": member_source_after.st_mtime_ns,
                "member_ids_sha256": member_ids_sha256,
                "overlay_events_sha256": member_overlay["events_sha256"],
                "sent_count": len(members),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    member_snapshot = {
        "schema_version": 2,
        "scope_complete": scope_complete,
        "source_stable_during_read": member_source_stable,
        "overlay_stable_during_read": member_overlay["stable_during_read"],
        "authoritative_for_absence": bool(
            scope_complete
            and complete_member_source
            and member_source_stable
            and member_overlay["stable_during_read"]
            and not source_warning
            and members
            and len(unique_member_ids) == len(member_ids)
            and all(member_id.isdigit() and int(member_id) > 0 for member_id in member_ids)
            and skipped_record_count == 0
            and critical_issue_count == 0
            and unknown_billing_status_count == 0
        ),
        "source_kind": member_source_kind(member_source),
        "source_path": str(member_source),
        "source_mtime": member_source_snapshot_at,
        "source_mtime_ns": member_source_after.st_mtime_ns,
        "source_size": member_source_after.st_size,
        "source_sha256": member_source_sha256,
        "source_age_seconds": source_age_seconds,
        "snapshot_id": snapshot_id,
        "baseline_count": member_overlay["baseline_count"],
        "source_count": len(import_result.members),
        "sent_count": len(members),
        "unique_count": len(unique_member_ids),
        "member_ids_sha256": member_ids_sha256,
        "parse_issue_count": len(import_result.issues),
        "critical_issue_count": critical_issue_count,
        "unknown_billing_status_count": unknown_billing_status_count,
        "skipped_record_count": skipped_record_count,
        "overlay": member_overlay,
    }
    selected_invoice_member_ids = (
        configured_invoice_pilot_member_ids()
        if invoice_member_ids is None
        else {
            str(member_id).strip()
            for member_id in invoice_member_ids
            if str(member_id).strip().isdigit() and int(str(member_id).strip()) > 0
        }
    )
    (
        invoice_events,
        invoice_journal_issue_count,
        invoice_journal_source,
        invoice_catalog_source,
        invoice_source_snapshot_at,
        invoice_source_sha256,
        invoice_catalog_sha256,
    ) = invoice_membership_event_payload(
        source_root,
        backup,
        selected_invoice_member_ids,
    )
    attachment_root = attachments_root(source_root)
    documents = {}
    if upload_files and upload_via_portal and (not portal_url or not sync_token):
        raise ValueError("portal_url and sync_token are required when upload_via_portal is enabled.")
    if upload_files and upload_via_portal and upload_changed_only and not storage_bucket:
        raise ValueError("storage_bucket is required when upload_changed_only is enabled with upload_via_portal.")
    client = None if upload_via_portal else (s3_client() if upload_files else None)
    portal_upload_uris = {}
    photo_upload_keys: dict[str, str] = {}
    changed_file_paths = changed_file_paths or set()
    existing_member_ids_known = existing_member_ids is not None
    existing_member_ids = {str(member_id) for member_id in (existing_member_ids or set())}
    missing_file_keys = {str(key).replace("\\", "/").lstrip("/") for key in (missing_file_keys or set())}

    def file_storage_key(path: Path) -> str:
        return storage_key(storage_prefix, relative_path(path.resolve(), source_root))

    def should_upload(path: Path, member_id: str | None = None) -> bool:
        if not upload_changed_only:
            return True
        if member_id and existing_member_ids_known and member_id not in existing_member_ids:
            return True
        if file_storage_key(path) in missing_file_keys:
            return True
        return relative_path(path.resolve(), source_root) in changed_file_paths

    def should_upload_photo(path: Path, member_id: str) -> bool:
        if not upload_changed_only:
            return True
        if existing_member_ids_known and member_id not in existing_member_ids:
            return True
        if relative_path(path.resolve(), source_root) in changed_file_paths:
            return True
        if not missing_file_keys:
            return False
        legacy_key = file_storage_key(path)
        versioned_key = versioned_photo_storage_key(source_root, path, storage_prefix)
        return legacy_key in missing_file_keys or versioned_key in missing_file_keys

    def portal_uri_for_key(key: str) -> str:
        return portal_upload_uris.get(key) or stored_s3_uri(storage_bucket or "", key)

    if upload_files and upload_via_portal:
        upload_task_map: dict[str, Path] = {}
        for member in members:
            member_id = str(member["member_id"])
            records = infer_member_document_records(member_id, attachment_root)
            for record in records:
                path = Path(record.get("path") or "")
                if path.exists() and path.is_file():
                    key = file_storage_key(path)
                    if should_upload(path, member_id=member_id):
                        upload_task_map[key] = path
            photo = member_photo_path(member_id, source_root)
            if photo and should_upload_photo(photo, member_id):
                key = versioned_photo_storage_key(source_root, photo, storage_prefix)
                upload_task_map[key] = photo
                photo_upload_keys[member_id] = key

        print(f"Uploading {len(upload_task_map)} files through portal API...")
        portal_upload_uris = upload_files_parallel(
            sorted(upload_task_map.items()),
            portal_url=portal_url,
            token=sync_token,
            workers=upload_workers,
        )

    if attachment_root.exists():
        for member in members:
            member_id = str(member["member_id"])
            records = infer_member_document_records(member_id, attachment_root)
            if records:
                if upload_files and upload_via_portal:
                    uploaded_records = []
                    for record in records:
                        updated = dict(record)
                        path = Path(updated.get("path") or "")
                        if path.exists() and path.is_file():
                            key = file_storage_key(path)
                            updated["path"] = portal_uri_for_key(key)
                        uploaded_records.append(updated)
                    documents[member_id] = uploaded_records
                else:
                    documents[member_id] = (
                        uploaded_document_records(source_root, records, storage_prefix, client=client)
                        if upload_files else records
                    )

    if upload_files:
        for member in members:
            member_id = str(member["member_id"])
            photo = member_photo_path(member_id, source_root)
            if not photo:
                continue
            if upload_via_portal:
                key = photo_upload_keys.get(member_id)
                if key and key in portal_upload_uris:
                    member["photo_path"] = portal_upload_uris[key]
            elif should_upload_photo(photo, member_id):
                key = versioned_photo_storage_key(source_root, photo, storage_prefix)
                member["photo_path"] = upload_file_to_s3(photo, key, client=client)

    return {
        "source": str(source_root),
        "backup": str(backup),
        "member_source": str(member_source),
        "warning": source_warning,
        "generated_at": utc_now_iso(),
        "member_snapshot": member_snapshot,
        "members": [json_safe_member(member) for member in members],
        "documents": documents,
        "invoice_pilot_member_ids": sorted(selected_invoice_member_ids),
        "invoice_membership_events": invoice_events,
        "invoice_journal_issue_count": invoice_journal_issue_count,
        "invoice_journal_source": invoice_journal_source,
        "invoice_catalog_source": invoice_catalog_source,
        "invoice_source_snapshot_at": invoice_source_snapshot_at,
        "invoice_source_sha256": invoice_source_sha256,
        "invoice_catalog_sha256": invoice_catalog_sha256,
    }


def post_json(url: str, token: str, payload: dict, timeout: int = 60, extra_headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Sync-Token": token,
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urlrequest.Request(
        url,
        data=data,
        method="POST",
        headers=headers,
    )
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Sync API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach sync API: {exc}") from exc


def get_existing_member_ids(portal_url: str, token: str, timeout: int = 60) -> set[str] | None:
    endpoint = portal_url.rstrip("/") + "/api/sync/member-ids"
    http_request = urlrequest.Request(
        endpoint,
        method="GET",
        headers={"X-Sync-Token": token},
    )
    try:
        with urlrequest.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except Exception:
        return None
    member_ids = payload.get("member_ids")
    if not isinstance(member_ids, list):
        return None
    return {str(member_id) for member_id in member_ids}


def get_invoice_monitor_member_ids(
    portal_url: str,
    token: str,
    timeout: int = 60,
) -> set[str]:
    endpoint = portal_url.rstrip("/") + "/api/sync/member-ids"
    http_request = urlrequest.Request(
        endpoint,
        method="GET",
        headers={"X-Sync-Token": token},
    )
    try:
        with urlrequest.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Invoice monitor API returned HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not load the required invoice reversal monitor set: {exc}"
        ) from exc
    member_ids = payload.get("invoice_monitor_member_ids")
    if not isinstance(member_ids, list):
        raise RuntimeError(
            "Portal did not return invoice_monitor_member_ids; "
            "member sync stopped to preserve reversal monitoring."
        )
    return {
        str(member_id).strip()
        for member_id in member_ids
        if str(member_id).strip().isdigit()
    }


def get_missing_file_keys(portal_url: str, token: str, timeout: int = 300) -> set[str]:
    endpoint = portal_url.rstrip("/") + "/api/sync/missing-file-keys"
    http_request = urlrequest.Request(
        endpoint,
        method="GET",
        headers={"X-Sync-Token": token},
    )
    try:
        with urlrequest.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except Exception:
        return set()
    missing_keys = payload.get("missing_keys")
    if not isinstance(missing_keys, list):
        return set()
    return {str(key).replace("\\", "/").lstrip("/") for key in missing_keys}


def get_fep_payment_updates(portal_url: str, token: str, limit: int = 50, timeout: int = 60, agent_id: str = "frontdesk_dreamz") -> list[dict]:
    endpoint = portal_url.rstrip("/") + f"/api/sync/fep-payment-updates?limit={max(1, min(limit, 100))}&agent_id={quote(agent_id or 'frontdesk_dreamz')}"
    http_request = urlrequest.Request(
        endpoint,
        method="GET",
        headers={"X-Sync-Token": token, "X-Sync-Agent": agent_id or "frontdesk_dreamz"},
    )
    try:
        with urlrequest.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FEP payment queue API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach FEP payment queue API: {exc}") from exc
    updates = payload.get("updates")
    return updates if isinstance(updates, list) else []


def post_fep_payment_update_result(portal_url: str, token: str, update_id: int, payload: dict, timeout: int = 60, agent_id: str = "frontdesk_dreamz") -> dict:
    endpoint = portal_url.rstrip("/") + f"/api/sync/fep-payment-updates/{update_id}/result"
    payload = dict(payload or {})
    payload.setdefault("agent_id", agent_id or "frontdesk_dreamz")
    return post_json(endpoint, token, payload, timeout=timeout, extra_headers={"X-Sync-Agent": agent_id or "frontdesk_dreamz"})


def get_fep_payment_process_command(portal_url: str, token: str, timeout: int = 60, agent_id: str = "frontdesk_dreamz") -> dict | None:
    endpoint = portal_url.rstrip("/") + f"/api/sync/fep-payment-process-commands?agent_id={quote(agent_id or 'frontdesk_dreamz')}"
    http_request = urlrequest.Request(
        endpoint,
        method="GET",
        headers={"X-Sync-Token": token, "X-Sync-Agent": agent_id or "frontdesk_dreamz"},
    )
    try:
        with urlrequest.urlopen(http_request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FEP payment process command API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach FEP payment process command API: {exc}") from exc
    commands = payload.get("commands")
    if isinstance(commands, list) and commands:
        command = commands[0]
        return command if isinstance(command, dict) else None
    return None


def post_fep_payment_process_command_result(portal_url: str, token: str, command_id: int, payload: dict, timeout: int = 60, agent_id: str = "frontdesk_dreamz") -> dict:
    endpoint = portal_url.rstrip("/") + f"/api/sync/fep-payment-process-commands/{command_id}/result"
    payload = dict(payload or {})
    payload.setdefault("agent_id", agent_id or "frontdesk_dreamz")
    return post_json(endpoint, token, payload, timeout=timeout, extra_headers={"X-Sync-Agent": agent_id or "frontdesk_dreamz"})


def run_fep_payment_writer(command: str, source_root: Path, update: dict, timeout: int = 300) -> dict:
    if not command:
        raise RuntimeError("FEP payment writer command is not configured. Refusing to fake Gym Assistant payment state.")

    request_payload = {
        "source_root": str(source_root.resolve()),
        "update": update,
    }
    args = shlex.split(command, posix=os.name != "nt")
    completed = subprocess.run(
        args,
        input=json.dumps(request_payload),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"writer exited with code {completed.returncode}"
        try:
            error_payload = json.loads(detail)
            if isinstance(error_payload, dict) and error_payload.get("error"):
                detail = str(error_payload["error"])
        except json.JSONDecodeError:
            pass
        raise RuntimeError(detail)
    output = (completed.stdout or "").strip()
    if not output:
        return {"status": "applied"}
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"writer returned non-JSON output: {output}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("writer returned JSON, but not an object.")
    result.setdefault("status", "applied")
    return result


def process_fep_payment_updates(
    source_root: Path,
    portal_url: str,
    token: str,
    writer_command: str,
    limit: int = 50,
    agent_id: str = "frontdesk_dreamz",
) -> dict:
    updates = get_fep_payment_updates(portal_url, token, limit=limit, agent_id=agent_id)
    summary = {"received": len(updates), "applied": 0, "failed": 0, "deferred": 0}
    for update in updates:
        update_id = int(update["id"])
        try:
            writer_result = run_fep_payment_writer(writer_command, source_root, update)
            if writer_result.get("status") == "deferred":
                summary["deferred"] += 1
                post_fep_payment_update_result(portal_url, token, update_id, writer_result, agent_id=agent_id)
                continue
            writer_result["status"] = "applied"
            post_fep_payment_update_result(portal_url, token, update_id, writer_result, agent_id=agent_id)
            summary["applied"] += 1
        except Exception as exc:
            post_fep_payment_update_result(
                portal_url,
                token,
                update_id,
                {"status": "failed", "error": str(exc)},
                agent_id=agent_id,
            )
            summary["failed"] += 1
    return summary


def process_fep_payment_command(
    source_root: Path,
    portal_url: str,
    token: str,
    writer_command: str,
    default_limit: int = 25,
    agent_id: str = "frontdesk_dreamz",
) -> dict:
    command = get_fep_payment_process_command(portal_url, token, agent_id=agent_id)
    if not command:
        return {"claimed": False, "status": "idle", "received": 0, "applied": 0, "failed": 0, "deferred": 0}

    command_id = int(command["id"])
    requested_limit = command.get("requested_limit") or default_limit
    try:
        limit = max(1, min(int(requested_limit), 500))
    except (TypeError, ValueError):
        limit = max(1, min(int(default_limit), 500))

    try:
        summary = {"received": 0, "applied": 0, "failed": 0, "deferred": 0}
        remaining = limit
        while remaining > 0:
            batch_limit = min(remaining, 100)
            batch_summary = process_fep_payment_updates(
                source_root,
                portal_url,
                token,
                writer_command,
                limit=batch_limit,
                agent_id=agent_id,
            )
            batch_received = int(batch_summary.get("received") or 0)
            for key in summary:
                summary[key] += int(batch_summary.get(key) or 0)
            if batch_received <= 0:
                break
            remaining -= batch_received
        result_payload = {
            "status": "completed",
            "summary": summary,
            "command": {
                "id": command_id,
                "requested_limit": limit,
                "target_agent": agent_id,
            },
        }
        post_fep_payment_process_command_result(portal_url, token, command_id, result_payload, agent_id=agent_id)
        return {"claimed": True, "command_id": command_id, **summary}
    except Exception as exc:
        error_payload = {
            "status": "failed",
            "error": str(exc),
            "command": {
                "id": command_id,
                "requested_limit": limit,
                "target_agent": agent_id,
            },
        }
        try:
            post_fep_payment_process_command_result(portal_url, token, command_id, error_payload, agent_id=agent_id)
        finally:
            pass
        return {"claimed": True, "command_id": command_id, "received": 0, "applied": 0, "failed": 1, "deferred": 0, "error": str(exc)}


def load_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(scan: SyncScan, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest_payload(scan), indent=2), encoding="utf-8")


def diff_manifest(scan: SyncScan, previous: dict | None) -> SyncDiff:
    current_files = {item.path: item for item in scan.files}
    previous_files = {
        item["path"]: item
        for item in (previous or {}).get("files", [])
        if isinstance(item, dict) and "path" in item
    }

    added = sorted(path for path in current_files if path not in previous_files)
    removed = sorted(path for path in previous_files if path not in current_files)
    changed = []
    for path, current in current_files.items():
        previous_item = previous_files.get(path)
        if not previous_item:
            continue
        if current.size != previous_item.get("size") or current.mtime_ns != previous_item.get("mtime_ns"):
            changed.append(path)

    return SyncDiff(added=added, changed=sorted(changed), removed=removed)


def summarize_by_kind(files: list[FileSignature]) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for item in files:
        bucket = summary.setdefault(item.kind, {"count": 0, "bytes": 0, "mb": 0.0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["bytes"] = int(bucket["bytes"]) + item.size
    for bucket in summary.values():
        bucket["mb"] = round(int(bucket["bytes"]) / 1024 / 1024, 2)
    return summary


def print_scan_report(scan: SyncScan, diff: SyncDiff | None = None) -> None:
    print(f"Source root: {scan.source_root}")
    print(f"Scanned at: {scan.scanned_at}")
    print(f"Member source: {scan.member_source or 'not found'}")
    print(f"Latest backup: {scan.latest_backup or 'not found'}")
    if scan.warning:
        print(f"Warning: {scan.warning}")
    print(f"Parsed members: {scan.member_count}")
    print(f"Relevant files: {scan.relevant_file_count}")
    print(f"Relevant size: {round(scan.relevant_total_bytes / 1024 / 1024, 2)} MB")

    for kind, values in summarize_by_kind(scan.files).items():
        print(f"- {kind}: {values['count']} files, {values['mb']} MB")

    if diff is not None:
        print("Changes since manifest:")
        print(f"- added: {len(diff.added)}")
        print(f"- changed: {len(diff.changed)}")
        print(f"- removed: {len(diff.removed)}")
        for label, paths in (("added", diff.added), ("changed", diff.changed), ("removed", diff.removed)):
            for path in paths[:10]:
                print(f"  {label}: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only GymAssistant sync scanner for the Dreamz member portal.")
    parser.add_argument("--source-root", default=str(DEFAULT_GYM_ASSISTANT_ROOT), help="GymAssistant installation root.")
    parser.add_argument(
        "--backup-root",
        default=os.getenv("GYM_ASSISTANT_BACKUP_ROOT", ""),
        help="Optional GymAssistant backup directory containing Members.btx and .gbu files.",
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST_PATH), help="Local manifest path for change detection.")
    parser.add_argument("--write-manifest", action="store_true", help="Write/update the local manifest after scanning.")
    parser.add_argument("--portal-url", help="Portal base URL, for example http://127.0.0.1:5000.")
    parser.add_argument("--sync-token", default=os.getenv("SYNC_API_TOKEN"), help="Sync API token. Defaults to SYNC_API_TOKEN.")
    parser.add_argument("--agent-id", default=os.getenv("SYNC_AGENT_ID", "frontdesk_dreamz"), help="Payment queue agent id, for example frontdesk_dreamz or ron_laptop.")
    parser.add_argument("--push-members", action="store_true", help="Push member and document metadata to the portal sync API.")
    parser.add_argument("--process-fep-command", action="store_true", help="Process one FEP-triggered payment command for this agent before pushing member sync data.")
    parser.add_argument("--process-fep-payments", action="store_true", help="Process queued FEP payment updates before pushing member sync data.")
    parser.add_argument("--fep-payment-writer", default=os.getenv("FEP_PAYMENT_WRITER_COMMAND", ""), help="Local command that writes one FEP payment update to Gym Assistant. Receives JSON on stdin.")
    parser.add_argument("--fep-payment-limit", type=int, default=50, help="Maximum queued FEP payment updates to process per run.")
    parser.add_argument("--member-limit", type=int, help="Limit pushed members for testing.")
    parser.add_argument("--upload-files", action="store_true", help="Upload pushed member PDFs and photos to S3-compatible storage.")
    parser.add_argument("--upload-via-portal", action="store_true", help="Upload files through the portal API so S3 credentials stay on Railway.")
    parser.add_argument("--upload-workers", type=int, default=4, help="Concurrent file uploads when using --upload-via-portal.")
    parser.add_argument("--upload-changed-only", action="store_true", help="Upload only files added or changed since the local manifest.")
    parser.add_argument("--upload-missing-files", action="store_true", help="Also backfill files that the portal reports as missing. Use manually, not for normal scheduled syncs.")
    parser.add_argument("--storage-bucket", default=os.getenv("AWS_S3_BUCKET_NAME"), help="S3 bucket name used to build URIs for unchanged uploaded files.")
    parser.add_argument("--storage-prefix", default=os.getenv("S3_PREFIX", DEFAULT_STORAGE_PREFIX), help="Object key prefix for uploaded files.")
    args = parser.parse_args()

    source_root = Path(args.source_root)
    if args.backup_root:
        os.environ["GYM_ASSISTANT_BACKUP_ROOT"] = str(Path(args.backup_root).resolve())
    manifest_path = Path(args.manifest)

    if args.process_fep_command:
        if not args.fep_payment_writer:
            print("FEP payment command skipped: --fep-payment-writer or FEP_PAYMENT_WRITER_COMMAND is not configured.")
        elif not args.portal_url or not args.sync_token:
            print("FEP payment command skipped: --portal-url and --sync-token/SYNC_API_TOKEN are required.")
        else:
            try:
                result = process_fep_payment_command(
                    source_root,
                    args.portal_url,
                    args.sync_token,
                    args.fep_payment_writer,
                    default_limit=args.fep_payment_limit,
                    agent_id=args.agent_id,
                )
            except Exception as exc:
                result = {
                    "claimed": False,
                    "status": "skipped",
                    "error": str(exc),
                    "received": 0,
                    "applied": 0,
                    "failed": 0,
                    "deferred": 0,
                }
            print("FEP payment command response:")
            print(json.dumps(result, indent=2))

    if args.process_fep_payments:
        if not args.portal_url:
            raise SystemExit("--portal-url is required with --process-fep-payments")
        if not args.sync_token:
            raise SystemExit("--sync-token or SYNC_API_TOKEN is required with --process-fep-payments")
        if not args.fep_payment_writer:
            raise SystemExit("--fep-payment-writer or FEP_PAYMENT_WRITER_COMMAND is required with --process-fep-payments")
        result = process_fep_payment_updates(
            source_root,
            args.portal_url,
            args.sync_token,
            args.fep_payment_writer,
            limit=args.fep_payment_limit,
            agent_id=args.agent_id,
        )
        print("FEP payment processing response:")
        print(json.dumps(result, indent=2))

    journal_evidence_summary = process_journal_evidence_queue_if_configured(
        source_root=source_root,
        portal_url=args.portal_url,
        sync_token=args.sync_token,
        agent_id=args.agent_id,
        snapshot_builder=build_existing_member_journal_evidence,
    )
    if journal_evidence_summary["enabled"]:
        print("Existing-member journal evidence response:")
        print(json.dumps(journal_evidence_summary, indent=2))

    scan = scan_source(source_root)
    previous = load_manifest(manifest_path)
    diff = diff_manifest(scan, previous)
    print_scan_report(scan, diff=diff)

    if args.write_manifest and not args.push_members:
        save_manifest(scan, manifest_path)
        print(f"Manifest written: {manifest_path}")

    if args.push_members:
        if not args.portal_url:
            raise SystemExit("--portal-url is required with --push-members")
        if not args.sync_token:
            raise SystemExit("--sync-token or SYNC_API_TOKEN is required with --push-members")
        invoice_member_ids = (
            configured_invoice_pilot_member_ids()
            | get_invoice_monitor_member_ids(args.portal_url, args.sync_token)
        )
        existing_member_ids = None
        missing_file_keys = set()
        if args.upload_files and args.upload_via_portal and args.upload_changed_only:
            existing_member_ids = get_existing_member_ids(args.portal_url, args.sync_token)
            if args.upload_missing_files:
                missing_file_keys = get_missing_file_keys(args.portal_url, args.sync_token)
        payload = build_sync_payload(
            source_root,
            member_limit=args.member_limit,
            upload_files=args.upload_files,
            storage_prefix=args.storage_prefix,
            upload_via_portal=args.upload_via_portal,
            portal_url=args.portal_url,
            sync_token=args.sync_token,
            upload_workers=args.upload_workers,
            upload_changed_only=args.upload_changed_only,
            changed_file_paths=set(diff.added + diff.changed),
            existing_member_ids=existing_member_ids,
            missing_file_keys=missing_file_keys,
            storage_bucket=args.storage_bucket,
            invoice_member_ids=invoice_member_ids,
        )
        endpoint = args.portal_url.rstrip("/") + "/api/sync/members"
        result = post_json(
            endpoint,
            args.sync_token,
            payload,
            timeout=member_sync_api_timeout_seconds(),
        )
        print("Sync API response:")
        print(json.dumps(result, indent=2))
        if args.write_manifest:
            save_manifest(scan, manifest_path)
            print(f"Manifest written: {manifest_path}")


if __name__ == "__main__":
    main()
