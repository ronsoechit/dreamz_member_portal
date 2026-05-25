from __future__ import annotations

import argparse
import json
from datetime import date, datetime
import os
from pathlib import Path

from ga_documents import infer_member_document_records, infer_member_documents, index_attachments_root
from ga_import import ImportResult, parse_gymassistant_export


DEFAULT_EXPORT_PATHS = [
    Path("instance/Member Detail Report.xls"),
    Path("instance/Report.xls"),
    Path("instance/Report.txt"),
    Path("data/Member Detail Report.xls"),
    Path("data/Report.txt"),
]
DEFAULT_OVERRIDES_PATH = Path("data/member_overrides.json")
DEFAULT_GYM_ASSISTANT_DATA_ROOT = Path(r"D:\Dreamz Fitness\Gym Assistant 2.6\Data")
DEFAULT_DOCUMENT_CACHE_ROOT = Path("instance/generated_documents")
DEFAULT_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp")
STALE_EXPORT_CUTOFF = datetime(2026, 1, 1)

DOCUMENT_PATH_CANDIDATES = {
    "form_path": [
        "forms/{member_id}_signup_form.pdf",
        "forms/{member_id}_signup.pdf",
        "forms/{member_id}_form.pdf",
        "signup_forms/{member_id}_signup_form.pdf",
        "signup_forms/{member_id}_signup.pdf",
        "signup_forms/{member_id}_form.pdf",
    ],
    "contract_path": [
        "contracts/{member_id}_contract.pdf",
    ],
    "mandate_path": [
        "mandates/{member_id}_mandate.pdf",
    ],
}


def find_latest_backup_export(data_root: Path | None = None) -> Path | None:
    configured = os.getenv("GYM_ASSISTANT_BACKUP_PATH")
    if configured:
        path = Path(configured)
        if path.exists():
            return path

    configured_root = os.getenv("GYM_ASSISTANT_DATA_ROOT")
    root = Path(configured_root) if configured_root else (data_root or DEFAULT_GYM_ASSISTANT_DATA_ROOT)
    backup_root = root / "Backup"
    if not backup_root.exists():
        return None

    backups = [
        path
        for path in backup_root.glob("*.gbu")
        if path.is_file()
    ]
    if not backups:
        return None

    return max(backups, key=lambda path: path.stat().st_mtime)


def assert_not_stale_export(path: Path, allow_stale_export: bool = False) -> None:
    if allow_stale_export:
        return
    if path.suffix.lower() not in {".xls", ".xlsx", ".xml", ".txt", ".prn"}:
        return
    last_modified = datetime.fromtimestamp(path.stat().st_mtime)
    if last_modified < STALE_EXPORT_CUTOFF:
        raise SystemExit(
            "Refusing to import stale 2025 GymAssistant export: "
            f"{path} (last modified {last_modified:%Y-%m-%d %H:%M}). "
            "Use the current .gbu backup from GymAssistant, or pass --allow-stale-export intentionally."
        )


def resolve_export_path(cli_path: str | None, allow_stale_export: bool = False) -> Path:
    if cli_path:
        path = Path(cli_path)
        if not path.exists():
            raise SystemExit(f"Export file not found: {path}")
        assert_not_stale_export(path, allow_stale_export)
        return path

    latest_backup = find_latest_backup_export()
    if latest_backup:
        return latest_backup

    for path in DEFAULT_EXPORT_PATHS:
        if path.exists():
            assert_not_stale_export(path, allow_stale_export)
            return path

    candidates = ", ".join(str(path) for path in DEFAULT_EXPORT_PATHS)
    raise SystemExit(
        "No current GymAssistant backup found and no legacy export is usable. "
        f"Checked backup root and: {candidates}"
    )


def resolve_attachments_root(cli_path: str | None = None) -> Path | None:
    if cli_path:
        return Path(cli_path)

    configured = os.getenv("GYM_ASSISTANT_ATTACHMENTS_ROOT")
    if configured:
        return Path(configured)

    data_root = os.getenv("GYM_ASSISTANT_DATA_ROOT")
    if data_root:
        return Path(data_root) / "Attachments"

    if DEFAULT_GYM_ASSISTANT_DATA_ROOT.exists():
        return DEFAULT_GYM_ASSISTANT_DATA_ROOT / "Attachments"

    return None


def resolve_photos_root(cli_path: str | None = None) -> Path | None:
    if cli_path:
        return Path(cli_path)

    configured = os.getenv("GYM_ASSISTANT_PHOTOS_ROOT")
    if configured:
        return Path(configured)

    data_root = os.getenv("GYM_ASSISTANT_DATA_ROOT")
    if data_root:
        return Path(data_root) / "Pictures"

    if DEFAULT_GYM_ASSISTANT_DATA_ROOT.exists():
        return DEFAULT_GYM_ASSISTANT_DATA_ROOT / "Pictures"

    return None


def resolve_document_cache_root(cli_path: str | None = None) -> Path:
    if cli_path:
        return Path(cli_path).resolve()
    configured = os.getenv("DOCUMENT_CACHE_ROOT")
    if configured:
        return Path(configured).resolve()
    return DEFAULT_DOCUMENT_CACHE_ROOT.resolve()


def infer_member_photo(member_id: str, photos_root: Path | None) -> str | None:
    if not photos_root:
        return None

    member_folder = str(member_id).strip().zfill(7)
    for extension in DEFAULT_PHOTO_EXTENSIONS:
        photo_path = photos_root / f"{member_folder}{extension}"
        if photo_path.exists() and photo_path.is_file():
            return str(photo_path)

    return None


def ensure_sqlite_column(db, table_name: str, column_name: str, column_definition: str) -> None:
    if db.engine.dialect.name != "sqlite":
        return

    from sqlalchemy import text

    rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    if any(row[1] == column_name for row in rows):
        return

    db.session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))
    db.session.commit()


def infer_document_paths(
    member_id: str,
    static_root: Path | None = None,
    attachments_root: Path | None = None,
    split_root: Path | None = None,
) -> dict[str, str]:
    static_root = static_root or Path("static")
    document_paths = {}

    for field, candidates in DOCUMENT_PATH_CANDIDATES.items():
        for candidate in candidates:
            relative_path = candidate.format(member_id=member_id)
            if (static_root / relative_path).exists():
                document_paths[field] = relative_path.replace("\\", "/")
                break

    document_paths.update(infer_member_documents(member_id, attachments_root, split_root=split_root))
    return document_paths


def parse_override_value(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def load_member_overrides(path: Path | None = None) -> dict[str, dict]:
    path = path or DEFAULT_OVERRIDES_PATH
    if not path.exists():
        return {}

    raw_overrides = json.loads(path.read_text(encoding="utf-8"))
    return {
        member_id: {
            key: parse_override_value(value)
            for key, value in values.items()
        }
        for member_id, values in raw_overrides.items()
    }


def sync_members(
    members: list[dict],
    db,
    Member,
    MemberDocument=None,
    overrides: dict[str, dict] | None = None,
    attachments_root: Path | None = None,
    split_root: Path | None = None,
    photos_root: Path | None = None,
    refresh_documents: bool = False,
) -> tuple[int, int]:
    new = updated = 0
    core_keys = {column.name for column in Member.__table__.columns}
    overrides = overrides if overrides is not None else load_member_overrides()

    for member_data in members:
        existing = Member.query.filter_by(member_id=member_data["member_id"]).first()
        core_data = {key: value for key, value in member_data.items() if key in core_keys}
        core_data.update({
            key: value
            for key, value in overrides.get(member_data["member_id"], {}).items()
            if key in core_keys
        })
        document_paths = infer_document_paths(
            member_data["member_id"],
            attachments_root=attachments_root,
            split_root=split_root,
        )
        document_records = infer_member_document_records(
            member_data["member_id"],
            attachments_root,
            split_root=split_root,
        ) if MemberDocument else []
        photo_path = infer_member_photo(member_data["member_id"], photos_root)
        if photo_path and "photo_path" in core_keys:
            core_data["photo_path"] = photo_path

        if existing:
            for key, value in core_data.items():
                setattr(existing, key, value)
            for key, value in document_paths.items():
                if refresh_documents or not getattr(existing, key):
                    setattr(existing, key, value)
            updated += 1
        else:
            core_data.update(document_paths)
            db.session.add(Member(**core_data))
            new += 1

        if MemberDocument is not None and attachments_root:
            MemberDocument.query.filter_by(member_id=member_data["member_id"]).delete()
            for display_order, document_record in enumerate(document_records):
                db.session.add(MemberDocument(
                    member_id=member_data["member_id"],
                    document_type=document_record["document_type"],
                    title=document_record["title"],
                    path=document_record["path"],
                    source_filename=document_record.get("source_filename"),
                    display_order=display_order,
                ))

    db.session.commit()
    return new, updated


def clear_portal_member_data(db, Member, MemberDocument=None) -> int:
    if MemberDocument is not None:
        MemberDocument.query.delete()
    deleted_members = Member.query.delete()
    db.session.commit()
    return deleted_members


def print_import_summary(result: ImportResult, export_path: Path) -> None:
    print(f"Parsed {len(result.members)} members from {export_path}")
    print(f"Import issues: {len(result.issues)}")

    for issue in result.issues[:20]:
        print(
            f"- row {issue.row_number}, {issue.field}: "
            f"{issue.value!r} ({issue.message})"
        )

    if len(result.issues) > 20:
        print(f"- ... {len(result.issues) - 20} more issues")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import GymAssistant members into the portal database.")
    parser.add_argument(
        "export_path",
        nargs="?",
        help="Path to a GymAssistant .gbu backup, Members.btx file, .xls XML Spreadsheet, or fixed-width .txt report.",
    )
    parser.add_argument(
        "--allow-stale-export",
        action="store_true",
        help="Allow importing the old 2025 report export intentionally. Not recommended for portal testing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate the export without writing to the database.",
    )
    parser.add_argument(
        "--attachments-root",
        help="Path to GymAssistant Data\\Attachments for read-only document indexing.",
    )
    parser.add_argument(
        "--documents-report",
        action="store_true",
        help="Scan attachments and print a read-only document indexing summary.",
    )
    parser.add_argument(
        "--refresh-documents",
        action="store_true",
        help="Replace existing document paths with indexed GymAssistant attachment paths.",
    )
    parser.add_argument(
        "--split-combined-documents",
        action="store_true",
        help="Create protected split copies for 2-page combined contract/direct-debit PDFs.",
    )
    parser.add_argument(
        "--document-cache-root",
        help="Directory for generated split document PDFs.",
    )
    parser.add_argument(
        "--photos-root",
        help="Path to GymAssistant Data\\Pictures for member photos.",
    )
    parser.add_argument(
        "--replace-portal-members",
        action="store_true",
        help="Clear member data from the portal database before importing the current GymAssistant source.",
    )
    args = parser.parse_args()

    export_path = resolve_export_path(args.export_path, allow_stale_export=args.allow_stale_export)
    result = parse_gymassistant_export(export_path)
    print_import_summary(result, export_path)
    attachments_root = resolve_attachments_root(args.attachments_root)
    photos_root = resolve_photos_root(args.photos_root)
    split_root = (
        resolve_document_cache_root(args.document_cache_root)
        if args.split_combined_documents and not args.dry_run
        else None
    )

    if args.documents_report:
        if attachments_root:
            index = index_attachments_root(attachments_root, split_root=split_root)
            print(f"Document root: {attachments_root}")
            print(f"Photo root: {photos_root or 'not configured/found'}")
            split_status = split_root or ("disabled in dry-run" if args.split_combined_documents else "disabled")
            print(f"Split document cache: {split_status}")
            print(f"Scanned member folders: {index.scanned_members}")
            print(f"Scanned PDF files: {index.scanned_files}")
            print(f"Members with indexed documents: {len(index.documents)}")
            for member_id, documents in list(sorted(index.documents.items()))[:20]:
                found = ", ".join(f"{field}={Path(path).name}" for field, path in documents.items())
                print(f"- {member_id}: {found}")
        else:
            print("Document root: not configured/found")

    if args.dry_run:
        return

    from dreamz_portal import Member, MemberDocument, app, db

    with app.app_context():
        db.create_all()
        ensure_sqlite_column(db, "member", "photo_path", "VARCHAR")
        if args.replace_portal_members:
            deleted_members = clear_portal_member_data(db, Member, MemberDocument=MemberDocument)
            print(f"Cleared portal member data. Deleted member rows: {deleted_members}")
        new, updated = sync_members(
            result.members,
            db,
            Member,
            MemberDocument=MemberDocument,
            attachments_root=attachments_root,
            split_root=split_root,
            photos_root=photos_root,
            refresh_documents=args.refresh_documents,
        )
        print(f"Database sync complete. New: {new}, Updated: {updated}")


if __name__ == "__main__":
    main()
