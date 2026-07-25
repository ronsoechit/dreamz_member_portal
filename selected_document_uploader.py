from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable

from ga_documents import infer_member_document_records, member_attachment_folder
from sync_agent import (
    DEFAULT_GYM_ASSISTANT_ROOT,
    DEFAULT_STORAGE_PREFIX,
    attachments_root,
    latest_backup_path,
    live_members_path,
    parse_member_source,
    post_json,
    read_stable_file_snapshot,
    relative_path,
    storage_key,
    upload_files_parallel,
)


MEMBER_DOCUMENTS_ENDPOINT = "/api/sync/member-documents"


@dataclass(frozen=True)
class SelectedDocument:
    member_id: str
    document_type: str
    source_filename: str
    source_path: Path
    storage_key: str
    sha256: str
    size_bytes: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.member_id, self.document_type, self.source_filename


@dataclass(frozen=True)
class SelectedDocumentPlan:
    source_root: Path
    member_source: Path
    member_ids: tuple[str, ...]
    expected_document_count: int
    documents: tuple[SelectedDocument, ...]


def canonical_member_id(value: object) -> str:
    raw = str(value).strip()
    if not raw or not raw.isdigit() or int(raw) <= 0:
        raise ValueError(f"Invalid Gym Assistant member ID: {value!r}")
    return str(int(raw))


def parse_member_ids(values: str | Iterable[str]) -> tuple[str, ...]:
    raw = values if isinstance(values, str) else " ".join(str(value) for value in values)
    tokens = [token for token in re.split(r"[\s,]+", raw.strip()) if token]
    if not tokens:
        raise ValueError("At least one member ID is required.")

    member_ids: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        member_id = canonical_member_id(token)
        if member_id in seen:
            raise ValueError(f"Duplicate member ID in exact selection: {member_id}")
        seen.add(member_id)
        member_ids.append(member_id)
    return tuple(member_ids)


def member_source_path(source_root: Path) -> Path:
    live_source = live_members_path(source_root)
    if live_source.is_file():
        return live_source
    backup = latest_backup_path(source_root)
    if backup and backup.is_file():
        return backup
    raise FileNotFoundError(
        f"No live Members.btx or GymAssistant .gbu backup found for {source_root}"
    )


def _ensure_path_within(path: Path, parent: Path) -> Path:
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    try:
        resolved_path.relative_to(resolved_parent)
    except ValueError as exc:
        raise ValueError(
            f"Selected document escapes the expected member attachment folder: {path}"
        ) from exc
    return resolved_path


def _document_identity_from_manifest(record: object) -> tuple[str, str, str]:
    if not isinstance(record, dict):
        raise ValueError("Each expected manifest document must be an object.")
    member_id = canonical_member_id(record.get("member_id"))
    document_type = str(record.get("document_type") or "").strip()
    source_filename = str(record.get("source_filename") or "").strip()
    if not document_type:
        raise ValueError(f"Expected manifest document has no document_type: {record!r}")
    if (
        not source_filename
        or Path(source_filename).name != source_filename
        or source_filename in {".", ".."}
    ):
        raise ValueError(
            f"Expected manifest source_filename must be a plain file name: {source_filename!r}"
        )
    return member_id, document_type, source_filename


def validate_expected_manifest(
    manifest_path: Path,
    member_ids: tuple[str, ...],
    expected_document_count: int,
    documents: tuple[SelectedDocument, ...],
) -> None:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read expected document manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Expected document manifest must contain a JSON object.")

    manifest_member_ids = parse_member_ids(
        [str(value) for value in payload.get("expected_member_ids", [])]
    )
    if set(manifest_member_ids) != set(member_ids) or len(manifest_member_ids) != len(member_ids):
        raise ValueError(
            "Expected manifest member IDs do not exactly match the requested member IDs."
        )

    manifest_count = payload.get("expected_document_count")
    if (
        isinstance(manifest_count, bool)
        or not isinstance(manifest_count, int)
        or manifest_count <= 0
    ):
        raise ValueError("Expected manifest must contain a positive expected_document_count.")
    if manifest_count != expected_document_count:
        raise ValueError(
            "Expected manifest document count does not match --expected-document-count."
        )

    expected_identities = [
        _document_identity_from_manifest(record)
        for record in payload.get("documents", [])
    ]
    if len(expected_identities) != manifest_count:
        raise ValueError(
            "Expected manifest documents length does not match expected_document_count."
        )
    if len(set(expected_identities)) != len(expected_identities):
        raise ValueError("Expected manifest contains duplicate document identities.")

    actual_identities = [document.identity for document in documents]
    if set(actual_identities) != set(expected_identities):
        missing = sorted(set(expected_identities) - set(actual_identities))
        unexpected = sorted(set(actual_identities) - set(expected_identities))
        raise ValueError(
            "Selected Gym Assistant documents do not exactly match the expected manifest. "
            f"Missing: {missing or 'none'}; unexpected: {unexpected or 'none'}"
        )


def build_selected_document_plan(
    source_root: Path,
    member_ids: str | Iterable[str],
    expected_document_count: int,
    *,
    storage_prefix: str = DEFAULT_STORAGE_PREFIX,
    expected_manifest: Path | None = None,
) -> SelectedDocumentPlan:
    if (
        isinstance(expected_document_count, bool)
        or not isinstance(expected_document_count, int)
        or expected_document_count <= 0
    ):
        raise ValueError("expected_document_count must be a positive integer.")

    source_root = source_root.resolve()
    selected_member_ids = parse_member_ids(member_ids)
    selected_set = set(selected_member_ids)
    member_source = member_source_path(source_root)
    import_result = parse_member_source(member_source)
    available_member_ids = {
        canonical_member_id(member.get("member_id"))
        for member in import_result.members
        if member.get("member_id") is not None
    }
    missing_member_ids = sorted(selected_set - available_member_ids, key=int)
    if missing_member_ids:
        raise ValueError(
            "Exact member selection stopped: these member IDs are absent from the "
            f"Gym Assistant source: {', '.join(missing_member_ids)}"
        )

    attachment_root = attachments_root(source_root)
    if not attachment_root.is_dir():
        raise FileNotFoundError(
            f"Gym Assistant attachment directory not found: {attachment_root}"
        )

    documents: list[SelectedDocument] = []
    seen_identities: set[tuple[str, str, str]] = set()
    for member_id in selected_member_ids:
        member_dir = attachment_root / member_attachment_folder(member_id)
        attachment_files = [
            path
            for path in member_dir.rglob("*")
            if path.is_file()
        ] if member_dir.is_dir() else []
        unsupported_files = [
            path
            for path in attachment_files
            if path.suffix.casefold() != ".pdf"
        ]
        if unsupported_files:
            relative_names = sorted(
                str(path.relative_to(member_dir)).replace("\\", "/")
                for path in unsupported_files
            )
            raise ValueError(
                "Exact member selection stopped: member "
                f"{member_id} has non-PDF attachment files that would otherwise be "
                f"omitted: {relative_names}"
            )

        records = infer_member_document_records(member_id, attachment_root)
        if not records:
            raise ValueError(
                f"Exact member selection stopped: member {member_id} has no PDF documents."
            )

        for record in records:
            source_path = Path(record.get("path") or "")
            resolved_path = _ensure_path_within(source_path, member_dir)
            if (
                not resolved_path.is_file()
                or resolved_path.suffix.casefold() != ".pdf"
            ):
                raise ValueError(
                    f"Selected attachment is not an existing PDF for member {member_id}: "
                    f"{source_path}"
                )

            document_type = str(record.get("document_type") or "").strip()
            source_filename = str(record.get("source_filename") or "").strip()
            if not document_type or source_filename != resolved_path.name:
                raise ValueError(
                    f"Invalid document metadata inferred for member {member_id}: {record!r}"
                )

            identity = (member_id, document_type, source_filename)
            if identity in seen_identities:
                raise ValueError(
                    "Exact member selection stopped: duplicate document identity "
                    f"{identity!r}."
                )
            seen_identities.add(identity)

            contents, _, sha256 = read_stable_file_snapshot(resolved_path)
            documents.append(
                SelectedDocument(
                    member_id=member_id,
                    document_type=document_type,
                    source_filename=source_filename,
                    source_path=resolved_path,
                    storage_key=storage_key(
                        storage_prefix,
                        relative_path(resolved_path, source_root),
                    ),
                    sha256=sha256,
                    size_bytes=len(contents),
                )
            )

    if len(documents) != expected_document_count:
        per_member_counts = {
            member_id: sum(1 for document in documents if document.member_id == member_id)
            for member_id in selected_member_ids
        }
        raise ValueError(
            "Exact document count mismatch: "
            f"expected {expected_document_count}, found {len(documents)} "
            f"({per_member_counts})."
        )

    plan = SelectedDocumentPlan(
        source_root=source_root,
        member_source=member_source,
        member_ids=selected_member_ids,
        expected_document_count=expected_document_count,
        documents=tuple(documents),
    )
    if expected_manifest:
        validate_expected_manifest(
            expected_manifest,
            plan.member_ids,
            plan.expected_document_count,
            plan.documents,
        )
    return plan


def bind_payload(
    plan: SelectedDocumentPlan,
    uploaded_uris: dict[str, str],
) -> dict:
    documents = []
    for document in plan.documents:
        storage_uri = str(uploaded_uris.get(document.storage_key) or "").strip()
        if not storage_uri.startswith("s3://"):
            raise RuntimeError(
                f"File upload did not return an S3 URI for {document.source_filename} "
                f"(member {document.member_id})."
            )
        documents.append(
            {
                "member_id": document.member_id,
                "document_type": document.document_type,
                "source_filename": document.source_filename,
                "storage_uri": storage_uri,
                "sha256": document.sha256,
                "size_bytes": document.size_bytes,
            }
        )
    return {
        "expected_member_ids": list(plan.member_ids),
        "expected_document_count": plan.expected_document_count,
        "documents": documents,
    }


def upload_selected_documents(
    plan: SelectedDocumentPlan,
    portal_url: str,
    sync_token: str,
    *,
    upload_workers: int = 4,
) -> dict:
    if not portal_url.strip():
        raise ValueError("portal_url is required for document upload.")
    if not sync_token.strip():
        raise ValueError("sync_token is required for document upload.")

    unique_documents_by_key: dict[str, SelectedDocument] = {}
    for document in plan.documents:
        existing = unique_documents_by_key.get(document.storage_key)
        if existing and existing.source_path != document.source_path:
            raise ValueError(
                f"Two different source files map to storage key {document.storage_key!r}."
            )
        unique_documents_by_key[document.storage_key] = document

    with tempfile.TemporaryDirectory(prefix="dreamz-selected-documents-") as tmp:
        snapshot_root = Path(tmp)
        upload_tasks: list[tuple[str, Path]] = []
        for index, (key, document) in enumerate(
            sorted(unique_documents_by_key.items()),
            start=1,
        ):
            contents, _, sha256 = read_stable_file_snapshot(document.source_path)
            if sha256 != document.sha256 or len(contents) != document.size_bytes:
                raise RuntimeError(
                    "A selected Gym Assistant document changed after preflight; "
                    f"nothing was uploaded: {document.source_path}"
                )
            snapshot_path = snapshot_root / f"{index:04d}" / document.source_filename
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_bytes(contents)
            upload_tasks.append((key, snapshot_path))

        uploaded_uris = upload_files_parallel(
            upload_tasks,
            portal_url=portal_url,
            token=sync_token,
            workers=upload_workers,
        )

    payload = bind_payload(plan, uploaded_uris)
    endpoint = portal_url.rstrip("/") + MEMBER_DOCUMENTS_ENDPOINT
    response = post_json(endpoint, sync_token, payload)
    if response.get("ok") is not True:
        raise RuntimeError(
            "Member-document binding API did not explicitly confirm success."
        )
    return response


def print_plan(plan: SelectedDocumentPlan) -> None:
    print("Selected document preflight passed.")
    print(f"Member source: {plan.member_source}")
    print(f"Selected members: {len(plan.member_ids)}")
    print(f"Selected document records: {len(plan.documents)}")
    for member_id in plan.member_ids:
        member_documents = [
            document for document in plan.documents if document.member_id == member_id
        ]
        print(f"- {member_id}: {len(member_documents)} document(s)")
        for document in member_documents:
            print(f"  {document.document_type}: {document.source_filename}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Upload an exact, prevalidated selection of Gym Assistant member PDFs "
            "without invoking the normal member-sync route."
        )
    )
    parser.add_argument(
        "--source-root",
        default=str(DEFAULT_GYM_ASSISTANT_ROOT),
        help="Gym Assistant installation root.",
    )
    parser.add_argument(
        "--backup-root",
        default=os.getenv("GYM_ASSISTANT_BACKUP_ROOT", ""),
        help="Optional Gym Assistant backup directory containing Members.btx or .gbu files.",
    )
    parser.add_argument(
        "--member-ids",
        nargs="+",
        required=True,
        help="Exact member IDs, separated by commas and/or whitespace.",
    )
    parser.add_argument(
        "--expected-document-count",
        type=int,
        required=True,
        help="Exact number of inferred document records required before any upload.",
    )
    parser.add_argument(
        "--expected-manifest",
        type=Path,
        help="Optional JSON manifest requiring exact member IDs, filenames, and types.",
    )
    parser.add_argument(
        "--portal-url",
        help="Portal base URL. Required only with --execute.",
    )
    parser.add_argument(
        "--sync-token",
        default=os.getenv("SYNC_API_TOKEN", ""),
        help="Sync API token. Defaults to SYNC_API_TOKEN.",
    )
    parser.add_argument(
        "--storage-prefix",
        default=os.getenv("S3_PREFIX", DEFAULT_STORAGE_PREFIX),
        help="Canonical object-key prefix used by the portal.",
    )
    parser.add_argument(
        "--upload-workers",
        type=int,
        default=4,
        help="Concurrent PDF uploads.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Upload PDFs and bind metadata through /api/sync/member-documents. "
            "Without this flag the command is a read-only dry run."
        ),
    )
    args = parser.parse_args()

    if args.backup_root:
        os.environ["GYM_ASSISTANT_BACKUP_ROOT"] = str(
            Path(args.backup_root).resolve()
        )

    plan = build_selected_document_plan(
        Path(args.source_root),
        args.member_ids,
        args.expected_document_count,
        storage_prefix=args.storage_prefix,
        expected_manifest=args.expected_manifest,
    )
    print_plan(plan)

    if not args.execute:
        print("Dry run only: no files were uploaded and no portal data was changed.")
        return
    if not args.portal_url:
        raise SystemExit("--portal-url is required with --execute")
    if not args.sync_token:
        raise SystemExit("--sync-token or SYNC_API_TOKEN is required with --execute")

    response = upload_selected_documents(
        plan,
        args.portal_url,
        args.sync_token,
        upload_workers=args.upload_workers,
    )
    print("Selected document upload and binding completed.")
    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
