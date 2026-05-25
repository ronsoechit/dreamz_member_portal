from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterable
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from ga_import import parse_gymassistant_export
from ga_documents import infer_member_document_records
from storage_backend import s3_client, upload_file_to_s3


DEFAULT_GYM_ASSISTANT_ROOT = Path(r"D:\Dreamz Fitness\Gym Assistant 2.6")
DEFAULT_MANIFEST_PATH = Path("instance/sync_manifest.json")
DEFAULT_STORAGE_PREFIX = "gymassistant"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


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
    latest_backup: str | None
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


def latest_backup_path(source_root: Path) -> Path | None:
    root = backup_root(source_root)
    if not root.exists():
        return None
    backups = [path for path in root.glob("*.gbu") if path.is_file()]
    return max(backups, key=lambda path: path.stat().st_mtime) if backups else None


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
    files: list[FileSignature] = []
    member_count = 0

    if backup:
        files.append(file_signature(backup, source_root, "backup"))
        member_count = len(parse_gymassistant_export(backup).members)

    files.extend(iter_attachment_files(source_root) or [])
    files.extend(iter_photo_files(source_root) or [])
    files.sort(key=lambda item: item.path)

    return SyncScan(
        source_root=str(source_root),
        scanned_at=utc_now_iso(),
        latest_backup=str(backup) if backup else None,
        member_count=member_count,
        relevant_file_count=len(files),
        relevant_total_bytes=sum(item.size for item in files),
        files=files,
    )


def manifest_payload(scan: SyncScan) -> dict:
    return {
        "source_root": scan.source_root,
        "scanned_at": scan.scanned_at,
        "latest_backup": scan.latest_backup,
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


def uploaded_document_records(source_root: Path, records: list[dict[str, str]], prefix: str, client=None) -> list[dict[str, str]]:
    uploaded = []
    for record in records:
        updated = dict(record)
        path = Path(updated.get("path") or "")
        if path.exists() and path.is_file():
            updated["path"] = upload_source_file(source_root, path, prefix, client=client)
        uploaded.append(updated)
    return uploaded


def build_sync_payload(
    source_root: Path,
    member_limit: int | None = None,
    upload_files: bool = False,
    storage_prefix: str = DEFAULT_STORAGE_PREFIX,
) -> dict:
    source_root = source_root.resolve()
    backup = latest_backup_path(source_root)
    if not backup:
        raise FileNotFoundError(f"No GymAssistant .gbu backup found under {backup_root(source_root)}")

    import_result = parse_gymassistant_export(backup)
    source_members = import_result.members[:member_limit] if member_limit else import_result.members
    members = [dict(member) for member in source_members]
    attachment_root = attachments_root(source_root)
    documents = {}
    client = s3_client() if upload_files else None

    if attachment_root.exists():
        for member in members:
            member_id = str(member["member_id"])
            records = infer_member_document_records(member_id, attachment_root)
            if records:
                documents[member_id] = (
                    uploaded_document_records(source_root, records, storage_prefix, client=client)
                    if upload_files else records
                )

            if upload_files:
                photo = member_photo_path(member_id, source_root)
                if photo:
                    member["photo_path"] = upload_source_file(source_root, photo, storage_prefix, client=client)

    return {
        "source": str(source_root),
        "backup": str(backup),
        "generated_at": utc_now_iso(),
        "members": [json_safe_member(member) for member in members],
        "documents": documents,
    }


def post_json(url: str, token: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urlrequest.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Sync-Token": token,
        },
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
    print(f"Latest backup: {scan.latest_backup or 'not found'}")
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
    parser.add_argument("--push-members", action="store_true", help="Push member and document metadata to the portal sync API.")
    parser.add_argument("--member-limit", type=int, help="Limit pushed members for testing.")
    parser.add_argument("--upload-files", action="store_true", help="Upload pushed member PDFs and photos to S3-compatible storage.")
    parser.add_argument("--storage-prefix", default=os.getenv("S3_PREFIX", DEFAULT_STORAGE_PREFIX), help="Object key prefix for uploaded files.")
    args = parser.parse_args()

    source_root = Path(args.source_root)
    manifest_path = Path(args.manifest)
    scan = scan_source(source_root)
    previous = load_manifest(manifest_path)
    diff = diff_manifest(scan, previous)
    print_scan_report(scan, diff=diff)

    if args.write_manifest:
        save_manifest(scan, manifest_path)
        print(f"Manifest written: {manifest_path}")

    if args.push_members:
        if not args.portal_url:
            raise SystemExit("--portal-url is required with --push-members")
        if not args.sync_token:
            raise SystemExit("--sync-token or SYNC_API_TOKEN is required with --push-members")
        payload = build_sync_payload(
            source_root,
            member_limit=args.member_limit,
            upload_files=args.upload_files,
            storage_prefix=args.storage_prefix,
        )
        endpoint = args.portal_url.rstrip("/") + "/api/sync/members"
        result = post_json(endpoint, args.sync_token, payload)
        print("Sync API response:")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
