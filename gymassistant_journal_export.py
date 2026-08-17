from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Mapping

from ga_journal_snapshot import (
    OfficialJournalExportCoverage,
    validate_official_journal_export_bytes,
)
from scripts.gymassistant_roster_export.roster_export import (
    GymAssistantExporter,
    NamedMutex,
    RosterExportError,
    WindowInfo,
    _find_gymassistant_main,
    _normalize_text,
    pause_signup_bridge,
    wait_for_stable_file,
)


SOURCE_KIND = "gym_assistant_official_journal_export"
EXPORT_COMMAND = "Export Journal"
WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DEFAULT_UI_TIMEOUT_SECONDS = 90.0
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 900.0
GYMASSISTANT_EXE_ENV = "PORTAL_SYNC_JOURNAL_EXPORT_GYMASSISTANT_EXE"
EXPECTED_DATA_PATH_ENV = "PORTAL_SYNC_JOURNAL_EXPORT_EXPECTED_DATA_PATH"
CANDIDATE_PATH_ENV = "PORTAL_SYNC_JOURNAL_EXPORT_CANDIDATE_PATH"
CREDENTIAL_PATH_ENV = "PORTAL_SYNC_JOURNAL_EXPORT_CREDENTIAL_PATH"
BRIDGE_WORK_ROOT_ENV = "PORTAL_SYNC_JOURNAL_EXPORT_BRIDGE_WORK_ROOT"


class JournalExportError(RuntimeError):
    """Raised when current journal evidence cannot be proven safely."""


@dataclass(frozen=True)
class JournalExportConfig:
    source_root: Path
    executable: Path
    expected_data_path: str
    candidate_path: Path
    credential_path: Path
    bridge_work_root: Path


def load_journal_export_config(
    source_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> JournalExportConfig:
    environ = os.environ if environ is None else environ
    source_root = Path(source_root)
    local_app_data = str(environ.get("LOCALAPPDATA") or "").strip()
    if not local_app_data:
        raise JournalExportError("journal_export_local_app_data_missing")
    sync_root = Path(
        str(environ.get("DREAMZ_PORTAL_SYNC_ROOT") or r"C:\DreamzPortalSync")
    )
    return JournalExportConfig(
        source_root=source_root,
        executable=Path(
            str(
                environ.get(GYMASSISTANT_EXE_ENV)
                or source_root / "Gym Assistant 26.exe"
            )
        ),
        expected_data_path=str(
            environ.get(EXPECTED_DATA_PATH_ENV) or source_root / "Data"
        ),
        candidate_path=Path(
            str(
                environ.get(CANDIDATE_PATH_ENV)
                or sync_root / "journal-evidence" / "Journal.pending.jtx"
            )
        ),
        credential_path=Path(
            str(
                environ.get(CREDENTIAL_PATH_ENV)
                or Path(local_app_data)
                / "Dreamz"
                / "GymAssistantRosterExport"
                / "master-access.dpapi"
            )
        ),
        bridge_work_root=Path(
            str(
                environ.get(BRIDGE_WORK_ROOT_ENV)
                or Path(local_app_data) / "Dreamz" / "GymAssistantBridge"
            )
        ),
    )


@dataclass(frozen=True)
class _StableFile:
    data: bytes
    size_bytes: int
    mtime_ns: int
    source_sha256: str
    identity_sha256: str


@dataclass(frozen=True)
class _SourceBinding:
    data_path_fingerprint_sha256: str
    source_binding_sha256: str
    active_source_sha256: str
    active_size_bytes: int
    historical_segment_count: int
    historical_segment_set_sha256: str
    active_data: bytes = field(repr=False)


@dataclass(frozen=True)
class OfficialJournalExportSnapshot:
    data: bytes
    byte_length: int
    source_sha256: str
    stable_read_count: int
    locator_fingerprint_sha256: str
    data_path_fingerprint_sha256: str
    source_binding_sha256: str
    coverage: OfficialJournalExportCoverage


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _path_is_symlink_or_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return bool(
        path.is_symlink()
        or (
            int(getattr(metadata, "st_file_attributes", 0))
            & WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _canonical_path_text(path: Path) -> str:
    text = str(path.resolve(strict=True)).replace("\\", "/").rstrip("/")
    return text.casefold() if os.name == "nt" else text


def _path_fingerprint(path: Path) -> str:
    return hashlib.sha256(_canonical_path_text(path).encode("utf-8")).hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_export_paths(
    *,
    source_root: Path,
    executable: Path,
    expected_data_path: str,
    candidate_path: Path,
    credential_path: Path,
) -> tuple[Path, Path, Path, Path]:
    """Resolve fixed production inputs before any temporary file is removed."""
    source_input = Path(source_root)
    data_input = source_input / "Data"
    executable_input = Path(executable)
    credential_input = Path(credential_path)
    candidate_input = Path(candidate_path)

    if _path_is_symlink_or_reparse_point(source_input):
        raise JournalExportError("journal_install_root_reparse_point")
    if _path_is_symlink_or_reparse_point(data_input):
        raise JournalExportError("journal_data_path_reparse_point")
    if _path_is_symlink_or_reparse_point(executable_input):
        raise JournalExportError("journal_executable_reparse_point")
    if _path_is_symlink_or_reparse_point(credential_input):
        raise JournalExportError("journal_credential_reparse_point")
    if _path_is_symlink_or_reparse_point(candidate_input):
        raise JournalExportError("journal_export_candidate_reparse_point")

    try:
        resolved_source = source_input.resolve(strict=True)
        resolved_data = data_input.resolve(strict=True)
        resolved_expected_data = Path(expected_data_path).resolve(strict=True)
        resolved_executable = executable_input.resolve(strict=True)
        resolved_credential = credential_input.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise JournalExportError("journal_export_required_path_missing") from exc

    if resolved_data.parent != resolved_source or resolved_expected_data != resolved_data:
        raise JournalExportError("journal_data_path_binding_mismatch")
    if resolved_executable.parent != resolved_source:
        raise JournalExportError("journal_executable_binding_mismatch")
    if not resolved_executable.is_file() or not resolved_credential.is_file():
        raise JournalExportError("journal_export_required_file_missing")

    if candidate_input.suffix.casefold() != ".jtx":
        raise JournalExportError("journal_export_candidate_extension_invalid")
    try:
        resolved_candidate = candidate_input.resolve(strict=False)
    except OSError as exc:
        raise JournalExportError("journal_export_candidate_unavailable") from exc
    if _is_within(resolved_candidate, resolved_source):
        raise JournalExportError("journal_export_candidate_inside_source_root")
    if resolved_candidate == resolved_credential:
        raise JournalExportError("journal_export_candidate_conflicts_with_credential")

    existing_ancestor = candidate_input.parent
    while not existing_ancestor.exists() and existing_ancestor != existing_ancestor.parent:
        existing_ancestor = existing_ancestor.parent
    if _path_is_symlink_or_reparse_point(existing_ancestor):
        raise JournalExportError("journal_export_candidate_parent_reparse_point")
    candidate_input.parent.mkdir(parents=True, exist_ok=True)
    if _path_is_symlink_or_reparse_point(candidate_input.parent):
        raise JournalExportError("journal_export_candidate_parent_reparse_point")
    if candidate_input.exists():
        metadata = candidate_input.stat()
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1)) != 1:
            raise JournalExportError("journal_export_candidate_not_private_regular_file")

    return (
        resolved_source,
        resolved_executable,
        resolved_candidate,
        resolved_credential,
    )


def _file_identity_sha256(metadata: os.stat_result) -> str:
    device = int(metadata.st_dev)
    file_index = int(metadata.st_ino)
    if file_index <= 0:
        raise JournalExportError("journal_source_identity_unavailable")
    return _canonical_json_sha256(
        {"device": str(device), "file_index": str(file_index)}
    )


def _read_exact_file(path: Path) -> _StableFile:
    try:
        if _path_is_symlink_or_reparse_point(path):
            raise JournalExportError("journal_source_reparse_point")
        resolved_before = path.resolve(strict=True)
        before = resolved_before.stat()
        if not stat.S_ISREG(before.st_mode):
            raise JournalExportError("journal_source_not_regular")
        with resolved_before.open("rb") as handle:
            descriptor_before = os.fstat(handle.fileno())
            data = handle.read()
            descriptor_after = os.fstat(handle.fileno())
        resolved_after = path.resolve(strict=True)
        after = resolved_after.stat()
    except JournalExportError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise JournalExportError("journal_source_unavailable") from exc

    identities = {
        _file_identity_sha256(before),
        _file_identity_sha256(descriptor_before),
        _file_identity_sha256(descriptor_after),
        _file_identity_sha256(after),
    }
    if resolved_before != resolved_after or len(identities) != 1:
        raise JournalExportError("journal_source_identity_changed")
    if (
        before.st_size != descriptor_before.st_size
        or descriptor_before.st_size != descriptor_after.st_size
        or descriptor_after.st_size != after.st_size
        or before.st_mtime_ns != descriptor_before.st_mtime_ns
        or descriptor_before.st_mtime_ns != descriptor_after.st_mtime_ns
        or descriptor_after.st_mtime_ns != after.st_mtime_ns
        or len(data) != after.st_size
    ):
        raise JournalExportError("journal_source_unstable")
    return _StableFile(
        data=data,
        size_bytes=len(data),
        mtime_ns=int(after.st_mtime_ns),
        source_sha256=hashlib.sha256(data).hexdigest(),
        identity_sha256=identities.pop(),
    )


def _read_stable_file_twice(path: Path, *, max_attempts: int = 3) -> _StableFile:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for _attempt in range(max_attempts):
        try:
            first = _read_exact_file(path)
            second = _read_exact_file(path)
        except JournalExportError as exc:
            if str(exc) in {"journal_source_identity_changed", "journal_source_reparse_point"}:
                raise
            continue
        if first.identity_sha256 != second.identity_sha256:
            raise JournalExportError("journal_source_identity_changed")
        if first == second:
            return second
    raise JournalExportError("journal_source_unstable")


def _journal_segment_candidates(source_root: Path) -> tuple[tuple[str, Path], ...]:
    roots = (
        ("Data", source_root / "Data"),
        ("Backup", source_root / "Backup"),
        ("Data/Backup", source_root / "Data" / "Backup"),
    )
    candidates: list[tuple[str, Path]] = []
    for label, root in roots:
        if not root.exists():
            continue
        if _path_is_symlink_or_reparse_point(root):
            raise JournalExportError("journal_source_tree_reparse_point")
        try:
            entries = tuple(root.iterdir())
        except OSError as exc:
            raise JournalExportError("journal_source_tree_unavailable") from exc
        for path in entries:
            folded = path.name.casefold()
            if not path.is_file() or not (
                folded.startswith("journal")
                and path.suffix.casefold() in {".dat", ".tmp", ".jtx"}
            ):
                continue
            if label == "Data" and folded == "journal.dat":
                continue
            candidates.append((f"{label}/{folded}", path))
    return tuple(sorted(candidates, key=lambda item: item[0]))


def _capture_source_binding(source_root: Path, executable: Path) -> _SourceBinding:
    source_root = source_root.resolve(strict=True)
    data_root = (source_root / "Data").resolve(strict=True)
    if data_root.parent != source_root:
        raise JournalExportError("journal_data_path_binding_mismatch")
    if _path_is_symlink_or_reparse_point(source_root) or _path_is_symlink_or_reparse_point(data_root):
        raise JournalExportError("journal_data_path_reparse_point")

    active_path = data_root / "Journal.dat"
    active = _read_stable_file_twice(active_path)
    executable_snapshot = _read_stable_file_twice(executable)
    historical_segments = []
    for logical_name, path in _journal_segment_candidates(source_root):
        segment = _read_stable_file_twice(path)
        historical_segments.append(
            {
                "logical_name_sha256": hashlib.sha256(
                    logical_name.encode("utf-8")
                ).hexdigest(),
                "identity_sha256": segment.identity_sha256,
                "size_bytes": segment.size_bytes,
                "source_sha256": segment.source_sha256,
            }
        )

    historical_set_sha256 = _canonical_json_sha256(historical_segments)
    binding = {
        "contract": "dreamz.ga.official-journal-export-source-binding.v1",
        "data_path_fingerprint_sha256": _path_fingerprint(data_root),
        "active_journal_identity_sha256": active.identity_sha256,
        "gymassistant_executable_sha256": executable_snapshot.source_sha256,
        "historical_segment_count": len(historical_segments),
        "historical_segment_set_sha256": historical_set_sha256,
    }
    return _SourceBinding(
        data_path_fingerprint_sha256=binding["data_path_fingerprint_sha256"],
        source_binding_sha256=_canonical_json_sha256(binding),
        active_source_sha256=active.source_sha256,
        active_size_bytes=active.size_bytes,
        historical_segment_count=len(historical_segments),
        historical_segment_set_sha256=historical_set_sha256,
        active_data=active.data,
    )


class GymAssistantJournalExporter(GymAssistantExporter):
    """Drive Gym Assistant's own Special Features > Export Journal command."""

    def _main_window(self) -> WindowInfo:
        main = _find_gymassistant_main(self.ui, self.expected_data_path)
        if main is None:
            raise JournalExportError("journal_export_gymassistant_not_running")
        return main

    def _choose_journal_export(self, special: WindowInfo) -> None:
        listboxes = [
            control
            for control in self.ui.children(special.handle)
            if control.class_name.casefold() == "listbox"
        ]
        if len(listboxes) != 1:
            raise JournalExportError("journal_export_command_list_unavailable")
        self.ui.activate_list_item(listboxes[0].handle, EXPORT_COMMAND)

    def _answer_journal_export_prompts(self, process_id: int) -> None:
        deadline = time.monotonic() + self.ui_timeout_seconds
        saved = False
        while time.monotonic() < deadline:
            dialogs = [
                window
                for window in self.ui.top_windows(process_id)
                if window.class_name == "#32770"
            ]
            acted = False
            for dialog in dialogs:
                children = self.ui.children(dialog.handle)
                combined = _normalize_text(
                    " ".join([dialog.text, *(child.text for child in children)])
                )
                is_save_as = (
                    ("save" in _normalize_text(dialog.text) and "as" in _normalize_text(dialog.text))
                    or any(child.control_id == 1148 for child in children)
                )
                if is_save_as:
                    filename = next(
                        (
                            child
                            for child in children
                            if child.control_id == 1148
                            or child.class_name.casefold() == "edit"
                        ),
                        None,
                    )
                    if filename is None:
                        raise JournalExportError("journal_export_filename_control_missing")
                    self.ui.set_text(filename.handle, str(self.candidate_path))
                    self.ui.click(self.ui.control_by_id(dialog.handle, 1).handle)
                    saved = True
                    acted = True
                    break
                if "already exists" in combined or "replace" in combined:
                    self.ui.click(self.ui.button(dialog.handle, "Yes").handle)
                    acted = True
                    break
                if saved and "journal" in combined and "export" in combined:
                    try:
                        self.ui.click(self.ui.control_by_id(dialog.handle, 1).handle)
                    except RosterExportError:
                        self.ui.close(dialog.handle)
                    acted = True
                    break
            if saved and self.candidate_path.is_file():
                return
            if not acted:
                time.sleep(0.2)
        raise JournalExportError("journal_export_save_dialog_timeout")

    def run(self) -> dict:
        main = self._main_window()
        self._assert_clean_start(main)
        self.candidate_path.parent.mkdir(parents=True, exist_ok=True)
        self.candidate_path.unlink(missing_ok=True)
        try:
            special = self._authenticate(main)
            self._choose_journal_export(special)
            self._answer_journal_export_prompts(main.process_id)
            wait_for_stable_file(self.candidate_path)
            return {"status": "exported"}
        except Exception:
            self.candidate_path.unlink(missing_ok=True)
            raise
        finally:
            self._cleanup_export_windows(main)


def export_official_journal_snapshot(
    *,
    source_root: Path,
    executable: Path,
    expected_data_path: str,
    candidate_path: Path,
    credential_path: Path,
    bridge_work_root: Path | None,
    source_coverage_proven: bool,
    ui_timeout_seconds: float = DEFAULT_UI_TIMEOUT_SECONDS,
    bridge_timeout_seconds: float = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
    max_read_attempts: int = 3,
) -> OfficialJournalExportSnapshot:
    """Create, validate and return one current official journal export."""
    if source_coverage_proven is not True:
        raise JournalExportError("journal_source_coverage_unproven")

    (
        source_root,
        executable,
        candidate_path,
        credential_path,
    ) = _validate_export_paths(
        source_root=Path(source_root),
        executable=Path(executable),
        expected_data_path=expected_data_path,
        candidate_path=Path(candidate_path),
        credential_path=Path(credential_path),
    )
    with NamedMutex(), pause_signup_bridge(
            Path(bridge_work_root).resolve(strict=True)
            if bridge_work_root is not None
            else None,
            timeout_seconds=bridge_timeout_seconds,
        ):
        candidate_path.unlink(missing_ok=True)
        started_ns = time.time_ns()
        try:
            before = _capture_source_binding(source_root, executable)
            exporter = GymAssistantJournalExporter(
                executable=executable,
                expected_data_path=expected_data_path,
                candidate_path=candidate_path,
                credential_path=credential_path,
                ui_timeout_seconds=ui_timeout_seconds,
            )
            exporter.run()
            exported = _read_stable_file_twice(
                candidate_path,
                max_attempts=max_read_attempts,
            )
            if exported.mtime_ns + 2_000_000_000 < started_ns:
                raise JournalExportError("journal_export_not_fresh")
            coverage = validate_official_journal_export_bytes(exported.data)
            if not coverage.complete:
                raise JournalExportError("journal_export_coverage_invalid")
            after = _capture_source_binding(source_root, executable)
            if (
                before.data_path_fingerprint_sha256
                != after.data_path_fingerprint_sha256
                or before.source_binding_sha256 != after.source_binding_sha256
                or not after.active_data.startswith(before.active_data)
            ):
                raise JournalExportError("journal_source_binding_changed_during_export")

            logical_locator = _canonical_json_sha256(
                {
                    "command": EXPORT_COMMAND,
                    "contract": "dreamz.ga.official-journal-export-locator.v1",
                    "data_path_fingerprint_sha256": (
                        before.data_path_fingerprint_sha256
                    ),
                }
            )
            snapshot = OfficialJournalExportSnapshot(
                data=exported.data,
                byte_length=exported.size_bytes,
                source_sha256=exported.source_sha256,
                stable_read_count=2,
                locator_fingerprint_sha256=logical_locator,
                data_path_fingerprint_sha256=before.data_path_fingerprint_sha256,
                source_binding_sha256=before.source_binding_sha256,
                coverage=coverage,
            )
        finally:
            # The shared candidate belongs to the mutex-protected operation.
            # Remove it before releasing the mutex so another request cannot
            # have its newly-created export deleted by this request's cleanup.
            candidate_path.unlink(missing_ok=True)

    return snapshot
