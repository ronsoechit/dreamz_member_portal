from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Iterable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from ga_import import ImportIssue, ImportResult, parse_gymassistant_export, parse_member_log
from ga_documents import infer_member_document_records
from storage_backend import s3_client, upload_file_to_s3


DEFAULT_GYM_ASSISTANT_ROOT = Path(r"D:\Dreamz Fitness\Gym Assistant 2.6")
DEFAULT_MANIFEST_PATH = Path("instance/sync_manifest.json")
DEFAULT_STORAGE_PREFIX = "gymassistant"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
LIVE_MEMBER_DATA_WARNING_GRACE_SECONDS = 24 * 60 * 60


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def data_root(source_root: Path) -> Path:
    return source_root / "Data"


def attachments_root(source_root: Path) -> Path:
    return data_root(source_root) / "Attachments"


def photos_root(source_root: Path) -> Path:
    return data_root(source_root) / "Pictures"


def backup_root(source_root: Path) -> Path:
    return data_root(source_root) / "Backup"


def temp_files_root(source_root: Path) -> Path:
    return data_root(source_root) / "Temp Files"


def live_members_path(source_root: Path) -> Path:
    return data_root(source_root) / "Members.btx"


def live_members_dat_path(source_root: Path) -> Path:
    return data_root(source_root) / "Members.dat"


def latest_backup_path(source_root: Path) -> Path | None:
    root = backup_root(source_root)
    if not root.exists():
        return None
    backups = [path for path in root.glob("*.gbu") if path.is_file()]
    return max(backups, key=lambda path: path.stat().st_mtime) if backups else None


def iter_member_log_files(source_root: Path, backup: Path | None = None) -> Iterable[Path]:
    root = temp_files_root(source_root)
    if not root.exists():
        return

    candidates = [
        root / "AddedMembers.btx",
        root / "Added Members.txt",
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


def parse_members_with_live_logs(member_source: Path) -> ImportResult:
    result = parse_gymassistant_export(member_source)
    member_map = {str(member["member_id"]): dict(member) for member in result.members}
    issues: list[ImportIssue] = list(result.issues)
    backup = member_source if member_source.suffix.lower() == ".gbu" else None
    source_root = member_source.parents[2] if len(member_source.parents) >= 3 and member_source.parent.name == "Backup" else None
    if source_root:
        for log_file in iter_member_log_files(source_root, backup):
            log_result = parse_member_log(log_file)
            issues.extend(log_result.issues)
            is_added_members_file = log_file.name.lower() in {"addedmembers.btx", "added members.txt"}
            for member in log_result.members:
                member_id = str(member["member_id"])
                if member_id in member_map:
                    if is_added_members_file:
                        continue
                    member_map[member_id].update(dict(member))
                else:
                    member_map[member_id] = dict(member)
    return ImportResult(list(member_map.values()), issues)


def relative_path(path: Path, source_root: Path) -> str:
    return path.relative_to(source_root).as_posix()


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
    if not source_root.exists():
        raise FileNotFoundError(f"GymAssistant source root not found: {source_root}")

    backup = latest_backup_path(source_root)
    live_members = live_members_path(source_root)
    live_dat = live_members_dat_path(source_root)
    files: list[FileSignature] = []
    member_count = 0
    member_source: Path | None = None

    if live_members.exists():
        files.append(file_signature(live_members, source_root, "member_data"))
        member_source = live_members
        member_count = len(parse_gymassistant_export(live_members).members)
    elif backup:
        member_source = backup
        member_count = len(parse_members_with_live_logs(backup).members)

    if backup:
        files.append(file_signature(backup, source_root, "backup"))
    if live_dat.exists():
        files.append(file_signature(live_dat, source_root, "live_member_data"))
    for log_file in iter_member_log_files(source_root, backup):
        files.append(file_signature(log_file, source_root, "member_log"))

    files.extend(iter_attachment_files(source_root) or [])
    files.extend(iter_photo_files(source_root) or [])
    files.sort(key=lambda item: item.path)

    return SyncScan(
        source_root=str(source_root),
        scanned_at=utc_now_iso(),
        member_source=str(member_source) if member_source else None,
        latest_backup=str(backup) if backup else None,
        warning=live_member_data_warning(source_root, backup),
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
) -> dict:
    source_root = source_root.resolve()
    backup = latest_backup_path(source_root)
    member_source = live_members_path(source_root)
    if not member_source.exists():
        member_source = backup
    if not member_source:
        raise FileNotFoundError(
            f"No live Members.btx or GymAssistant .gbu backup found under {data_root(source_root)}"
        )

    import_result = parse_members_with_live_logs(member_source) if member_source.suffix.lower() == ".gbu" else parse_gymassistant_export(member_source)
    source_members = import_result.members[:member_limit] if member_limit else import_result.members
    members = [dict(member) for member in source_members]
    attachment_root = attachments_root(source_root)
    documents = {}
    if upload_files and upload_via_portal and (not portal_url or not sync_token):
        raise ValueError("portal_url and sync_token are required when upload_via_portal is enabled.")
    if upload_files and upload_via_portal and upload_changed_only and not storage_bucket:
        raise ValueError("storage_bucket is required when upload_changed_only is enabled with upload_via_portal.")
    client = None if upload_via_portal else (s3_client() if upload_files else None)
    portal_upload_uris = {}
    changed_file_paths = changed_file_paths or set()
    existing_member_ids = {str(member_id) for member_id in (existing_member_ids or set())}
    missing_file_keys = {str(key).replace("\\", "/").lstrip("/") for key in (missing_file_keys or set())}

    def file_storage_key(path: Path) -> str:
        return storage_key(storage_prefix, relative_path(path.resolve(), source_root))

    def should_upload(path: Path, member_id: str | None = None) -> bool:
        if not upload_changed_only:
            return True
        if member_id and member_id not in existing_member_ids:
            return True
        if file_storage_key(path) in missing_file_keys:
            return True
        return relative_path(path.resolve(), source_root) in changed_file_paths

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
            if photo:
                key = file_storage_key(photo)
                if should_upload(photo, member_id=member_id):
                    upload_task_map[key] = photo

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
                photo = member_photo_path(member_id, source_root)
                if photo:
                    if upload_via_portal:
                        key = file_storage_key(photo)
                        member["photo_path"] = portal_uri_for_key(key)
                    else:
                        member["photo_path"] = upload_source_file(source_root, photo, storage_prefix, client=client)

    return {
        "source": str(source_root),
        "backup": str(backup),
        "member_source": str(member_source),
        "warning": live_member_data_warning(source_root, backup),
        "generated_at": utc_now_iso(),
        "members": [json_safe_member(member) for member in members],
        "documents": documents,
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
    parser.add_argument("--storage-bucket", default=os.getenv("AWS_S3_BUCKET_NAME"), help="S3 bucket name used to build URIs for unchanged uploaded files.")
    parser.add_argument("--storage-prefix", default=os.getenv("S3_PREFIX", DEFAULT_STORAGE_PREFIX), help="Object key prefix for uploaded files.")
    args = parser.parse_args()

    source_root = Path(args.source_root)
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
        existing_member_ids = None
        missing_file_keys = set()
        if args.upload_files and args.upload_via_portal and args.upload_changed_only:
            existing_member_ids = get_existing_member_ids(args.portal_url, args.sync_token)
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
        )
        endpoint = args.portal_url.rstrip("/") + "/api/sync/members"
        result = post_json(endpoint, args.sync_token, payload)
        print("Sync API response:")
        print(json.dumps(result, indent=2))
        if args.write_manifest:
            save_manifest(scan, manifest_path)
            print(f"Manifest written: {manifest_path}")


if __name__ == "__main__":
    main()
