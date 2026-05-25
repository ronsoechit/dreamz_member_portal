from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import re

from pypdf import PdfReader, PdfWriter


logging.getLogger("pypdf").setLevel(logging.ERROR)


DOCUMENT_FIELD_ORDER = ("form_path", "contract_path", "mandate_path")

PRIMARY_DOCUMENT_TYPES = {
    "form_path": "signup_form",
    "contract_path": "contract",
    "mandate_path": "direct_debit_mandate",
}

DOCUMENT_TYPE_LABELS = {
    "signup_form": "Signup Form",
    "contract": "Contract",
    "direct_debit_mandate": "Direct Debit Mandate",
    "combined_contract_mandate": "Contract + Direct Debit Mandate",
    "group_pt": "Group PT / Personal Training",
    "cancellation": "Cancellation",
    "id_document": "ID Document",
    "waiver": "Waiver",
    "receipt": "Receipt",
    "other": "Other Document",
}

FORM_PATTERNS = (
    "signup",
    "sign up",
    "inscrip",
    "registration",
)
CONTRACT_PATTERNS = (
    "contract",
    "cntr",
    "contr",
)
MANDATE_PATTERNS = (
    "direct debit",
    "debit",
    "mandate",
    "bank form",
    "dd",
    "ach",
    "eft",
)
GROUP_PT_PATTERNS = (
    "group pt",
    "groep pt",
    "pt app",
    "pt form",
    "personal training",
    "group training",
)
CANCELLATION_PATTERNS = (
    "cancelation",
    "cancellation",
    "cancelations",
    "cancellations",
)
ID_PATTERNS = (
    "id",
    "identification",
    "passport",
    "sedula",
)
WAIVER_PATTERNS = (
    "waiver",
    "waivers",
)
RECEIPT_PATTERNS = (
    "receipt",
    "receipts",
)
CONTRACT_TEXT_PATTERNS = (
    "membership agreement",
    "membership contract",
    "contract",
    "agreement",
    "terms and conditions",
)
MANDATE_TEXT_PATTERNS = (
    "direct debit",
    "debit mandate",
    "mandate",
    "bank account",
    "authorization",
    "authorisation",
)


@dataclass(frozen=True)
class DocumentIndexResult:
    documents: dict[str, dict[str, str]]
    document_records: dict[str, list[dict[str, str]]]
    scanned_members: int
    scanned_files: int


def member_attachment_folder(member_id: str) -> str:
    return str(member_id).strip().zfill(7)


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def pattern_score(name: str, patterns: tuple[str, ...]) -> int:
    normalized = normalize_name(name)
    compact = normalized.replace(" ", "")
    score = 0
    for pattern in patterns:
        pattern_normalized = normalize_name(pattern)
        if pattern_normalized and pattern_normalized in normalized:
            score += 10
        elif pattern.replace(" ", "") in compact:
            score += 8
    return score


def classify_pdf(path: Path) -> set[str]:
    name = path.stem
    fields = set()

    if pattern_score(name, FORM_PATTERNS):
        fields.add("form_path")
    if pattern_score(name, CONTRACT_PATTERNS):
        fields.add("contract_path")
    if pattern_score(name, MANDATE_PATTERNS):
        fields.add("mandate_path")

    return fields


def classify_document_types(path: Path) -> set[str]:
    name = path.stem
    document_types = set()

    if pattern_score(name, FORM_PATTERNS):
        document_types.add("signup_form")
    if pattern_score(name, CONTRACT_PATTERNS):
        document_types.add("contract")
    if pattern_score(name, MANDATE_PATTERNS):
        document_types.add("direct_debit_mandate")
    if pattern_score(name, GROUP_PT_PATTERNS):
        document_types.add("group_pt")
    if pattern_score(name, CANCELLATION_PATTERNS):
        document_types.add("cancellation")
    if pattern_score(name, ID_PATTERNS):
        document_types.add("id_document")
    if pattern_score(name, WAIVER_PATTERNS):
        document_types.add("waiver")
    if pattern_score(name, RECEIPT_PATTERNS):
        document_types.add("receipt")

    return document_types or {"other"}


def candidate_sort_key(path: Path, field: str) -> tuple[int, float, str]:
    pattern_groups = {
        "form_path": FORM_PATTERNS,
        "contract_path": CONTRACT_PATTERNS,
        "mandate_path": MANDATE_PATTERNS,
    }
    score = pattern_score(path.stem, pattern_groups[field])
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0
    return score, modified, path.name.lower()


def index_member_documents(member_dir: Path) -> dict[str, str]:
    candidates = {field: [] for field in DOCUMENT_FIELD_ORDER}
    for path in member_dir.rglob("*.pdf"):
        if not path.is_file():
            continue
        for field in classify_pdf(path):
            candidates[field].append(path)

    selected = {}
    for field, paths in candidates.items():
        if paths:
            selected[field] = str(max(paths, key=lambda path: candidate_sort_key(path, field)))
    return selected


def split_pdf_pages(source_path: Path, output_path: Path, page_indexes: list[int]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as source_file:
        reader = PdfReader(source_file)
        writer = PdfWriter()
        for page_index in page_indexes:
            writer.add_page(reader.pages[page_index])
        with output_path.open("wb") as output_file:
            writer.write(output_file)


def page_texts(source_path: Path) -> list[str] | None:
    try:
        with source_path.open("rb") as source_file:
            reader = PdfReader(source_file)
            return [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None


def content_based_contract_mandate_groups(source_path: Path) -> tuple[list[int], list[int]] | None:
    texts = page_texts(source_path)
    if not texts or not any(text.strip() for text in texts):
        return None

    contract_pages = []
    mandate_pages = []
    for index, text in enumerate(texts):
        normalized = normalize_name(text)
        contract_score = pattern_score(normalized, CONTRACT_TEXT_PATTERNS)
        mandate_score = pattern_score(normalized, MANDATE_TEXT_PATTERNS)
        if contract_score > mandate_score:
            contract_pages.append(index)
        elif mandate_score > contract_score:
            mandate_pages.append(index)

    if not contract_pages or not mandate_pages:
        return None

    return contract_pages, mandate_pages


def page_order_contract_mandate_groups(source_path: Path) -> tuple[list[int], list[int]] | None:
    try:
        with source_path.open("rb") as source_file:
            reader = PdfReader(source_file)
            page_count = len(reader.pages)
    except Exception:
        return None

    if page_count < 2 or page_count % 2:
        return None

    split_at = page_count // 2
    return list(range(0, split_at)), list(range(split_at, page_count))


def combined_contract_mandate_groups(source_path: Path) -> tuple[list[int], list[int]] | None:
    return content_based_contract_mandate_groups(source_path) or page_order_contract_mandate_groups(source_path)


def split_combined_contract_mandate(
    member_id: str,
    document_paths: dict[str, str],
    output_root: Path | None,
) -> dict[str, str]:
    if not output_root:
        return document_paths

    contract_path = document_paths.get("contract_path")
    mandate_path = document_paths.get("mandate_path")
    if not contract_path or contract_path != mandate_path:
        return document_paths

    source_path = Path(contract_path)
    if not source_path.exists() or not source_path.is_file():
        return document_paths

    page_groups = combined_contract_mandate_groups(source_path)
    if not page_groups:
        return document_paths

    contract_pages, mandate_pages = page_groups
    member_output_dir = output_root / member_attachment_folder(member_id)
    split_paths = {
        "contract_path": member_output_dir / "contract.pdf",
        "mandate_path": member_output_dir / "direct-debit-mandate.pdf",
    }
    split_pdf_pages(source_path, split_paths["contract_path"], contract_pages)
    split_pdf_pages(source_path, split_paths["mandate_path"], mandate_pages)

    updated = dict(document_paths)
    updated["contract_path"] = str(split_paths["contract_path"])
    updated["mandate_path"] = str(split_paths["mandate_path"])
    return updated


def split_combined_document_records(
    member_id: str,
    source_path: Path,
    output_root: Path | None,
) -> list[dict[str, str]] | None:
    if not output_root:
        return None

    page_groups = combined_contract_mandate_groups(source_path)
    if not page_groups:
        return None

    contract_pages, mandate_pages = page_groups
    member_output_dir = output_root / member_attachment_folder(member_id)
    split_paths = {
        "contract": member_output_dir / "contract.pdf",
        "direct_debit_mandate": member_output_dir / "direct-debit-mandate.pdf",
    }
    split_pdf_pages(source_path, split_paths["contract"], contract_pages)
    split_pdf_pages(source_path, split_paths["direct_debit_mandate"], mandate_pages)

    return [
        {
            "document_type": "contract",
            "title": DOCUMENT_TYPE_LABELS["contract"],
            "path": str(split_paths["contract"]),
            "source_filename": source_path.name,
        },
        {
            "document_type": "direct_debit_mandate",
            "title": DOCUMENT_TYPE_LABELS["direct_debit_mandate"],
            "path": str(split_paths["direct_debit_mandate"]),
            "source_filename": source_path.name,
        },
    ]


def document_record_sort_key(record: dict[str, str]) -> tuple[int, str, str]:
    type_order = {
        "signup_form": 10,
        "contract": 20,
        "direct_debit_mandate": 30,
        "combined_contract_mandate": 35,
        "group_pt": 40,
        "cancellation": 50,
        "id_document": 60,
        "waiver": 70,
        "receipt": 80,
        "other": 90,
    }
    return (
        type_order.get(record.get("document_type", "other"), 99),
        record.get("source_filename", "").lower(),
        record.get("path", "").lower(),
    )


def index_member_document_records(
    member_id: str,
    member_dir: Path,
    split_root: Path | None = None,
) -> list[dict[str, str]]:
    records = []

    for path in sorted(member_dir.rglob("*.pdf"), key=lambda candidate: candidate.name.lower()):
        if not path.is_file():
            continue

        document_types = classify_document_types(path)
        if {"contract", "direct_debit_mandate"}.issubset(document_types):
            split_records = split_combined_document_records(member_id, path, split_root)
            if split_records:
                records.extend(split_records)
            else:
                records.append(
                    {
                        "document_type": "combined_contract_mandate",
                        "title": DOCUMENT_TYPE_LABELS["combined_contract_mandate"],
                        "path": str(path),
                        "source_filename": path.name,
                    }
                )
            continue

        for document_type in sorted(document_types):
            records.append(
                {
                    "document_type": document_type,
                    "title": DOCUMENT_TYPE_LABELS.get(document_type, DOCUMENT_TYPE_LABELS["other"]),
                    "path": str(path),
                    "source_filename": path.name,
                }
            )

    return sorted(records, key=document_record_sort_key)


def primary_document_paths(document_records: list[dict[str, str]]) -> dict[str, str]:
    selected = {}
    reverse_field_map = {document_type: field for field, document_type in PRIMARY_DOCUMENT_TYPES.items()}

    for record in document_records:
        field = reverse_field_map.get(record["document_type"])
        if record["document_type"] == "combined_contract_mandate":
            selected.setdefault("contract_path", str(Path(record["path"])))
            selected.setdefault("mandate_path", str(Path(record["path"])))
            continue
        if not field:
            continue
        path = Path(record["path"])
        selected[field] = str(path)

    return selected


def index_attachments_root(attachments_root: Path, split_root: Path | None = None) -> DocumentIndexResult:
    documents = {}
    document_records = {}
    scanned_members = 0
    scanned_files = 0

    if not attachments_root.exists():
        return DocumentIndexResult(documents, document_records, scanned_members, scanned_files)

    for member_dir in attachments_root.iterdir():
        if not member_dir.is_dir():
            continue
        scanned_members += 1
        scanned_files += sum(1 for path in member_dir.rglob("*.pdf") if path.is_file())
        member_id = member_dir.name.lstrip("0") or "0"
        records = index_member_document_records(member_id, member_dir, split_root)
        member_documents = primary_document_paths(records)
        if member_documents:
            documents[member_id] = member_documents
        if records:
            document_records[member_id] = records

    return DocumentIndexResult(documents, document_records, scanned_members, scanned_files)


def infer_member_documents(
    member_id: str,
    attachments_root: Path | None,
    split_root: Path | None = None,
) -> dict[str, str]:
    if not attachments_root:
        return {}

    member_dir = attachments_root / member_attachment_folder(member_id)
    if not member_dir.exists():
        return {}

    return primary_document_paths(index_member_document_records(member_id, member_dir, split_root))


def infer_member_document_records(
    member_id: str,
    attachments_root: Path | None,
    split_root: Path | None = None,
) -> list[dict[str, str]]:
    if not attachments_root:
        return []

    member_dir = attachments_root / member_attachment_folder(member_id)
    if not member_dir.exists():
        return []

    return index_member_document_records(member_id, member_dir, split_root)
