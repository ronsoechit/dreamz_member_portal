from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


RELEASE_NAME = "dreamz_reserve_payment_runner"
RELEASE_VERSION = "1.0.0"
MANIFEST_NAME = "release-manifest.json"
FIXED_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

PAYLOAD_FILES = (
    "reserve_laptop_payment_runner/README.md",
    "reserve_laptop_payment_runner/__init__.py",
    "reserve_laptop_payment_runner/runner.py",
    "gymassistant_payment_writer.py",
    "scripts/reserve_laptop_payment_runner/Install-ReserveLaptopPaymentRunner.ps1",
    "scripts/reserve_laptop_payment_runner/Restore-ReserveLaptopPaymentRunner.ps1",
    "scripts/reserve_laptop_payment_runner/Start-ReserveLaptopPaymentRunner.ps1",
    "scripts/reserve_laptop_payment_runner/Stop-ReserveLaptopPaymentRunner.ps1",
    "scripts/reserve_laptop_payment_runner/Test-DreamzReservePaymentRunnerRelease.ps1",
    "scripts/reserve_laptop_payment_runner/Test-ReserveLaptopPaymentRunnerPreflight.ps1",
    "scripts/reserve_laptop_payment_runner/Uninstall-ReserveLaptopPaymentRunner.ps1",
)

SECRET_PATTERNS = (
    re.compile(r"bearer\s+[a-z0-9._-]{20,}", re.IGNORECASE),
    re.compile(
        r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[a-z0-9._-]{20,}",
        re.IGNORECASE,
    ),
)


def canonical_text_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if b"\0" in raw:
        raise ValueError(f"release payload must be UTF-8 text: {path}")
    text = raw.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def validate_payload_text(relative_path: str, payload: bytes) -> None:
    text = payload.decode("utf-8")
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"possible secret in release payload: {relative_path}")


def manifest_bytes(source_commit: str, hashes: dict[str, str]) -> bytes:
    manifest = {
        "schema": 1,
        "release": RELEASE_NAME,
        "version": RELEASE_VERSION,
        "source_commit": source_commit,
        "expected_computer": "DESKTOP-8KM7V7D",
        "expected_windows_user": "ron",
        "agent_id": "reserve_8km7v7d",
        "source_root": r"\\DREAMZ-FRNTDSK\Gym Assistant 2.6",
        "starts_during_install": False,
        "creates_startup_by_default": False,
        "requires_dedicated_payment_token": True,
        "eol_policy": "lf",
        "payload_file_count": len(PAYLOAD_FILES),
        "files": {path: hashes[path] for path in sorted(hashes)},
    }
    return (
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")


def zip_info(relative_path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative_path, FIXED_ZIP_TIMESTAMP)
    # Stored entries avoid zlib-version-dependent output. The source-only
    # release is small, so portability is more valuable than compression.
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def git_bytes(source_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        (
            "git",
            "-c",
            "core.autocrlf=false",
            "-C",
            str(source_root),
            *arguments,
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git source validation failed: {detail}")
    return result.stdout


def validated_payloads_from_commit(
    source_root: Path,
    source_commit: str,
) -> dict[str, bytes]:
    head = git_bytes(source_root, "rev-parse", "--verify", "HEAD").decode(
        "ascii"
    ).strip()
    if head != source_commit:
        raise ValueError("source_commit must equal the repository HEAD")

    payloads: dict[str, bytes] = {}
    for relative_path in PAYLOAD_FILES:
        source = source_root / Path(relative_path)
        if not source.is_file():
            raise FileNotFoundError(f"release item is missing: {relative_path}")
        committed = git_bytes(
            source_root,
            "show",
            f"{source_commit}:{relative_path}",
        )
        if b"\0" in committed:
            raise ValueError(
                f"release payload must be UTF-8 text: {relative_path}"
            )
        try:
            committed_text = committed.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"release payload must be UTF-8 text: {relative_path}"
            ) from exc
        committed_payload = (
            committed_text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .encode("utf-8")
        )
        working_payload = canonical_text_bytes(source)
        if working_payload != committed_payload:
            raise ValueError(
                "release payload differs from source_commit: "
                f"{relative_path}"
            )
        validate_payload_text(relative_path, committed_payload)
        payloads[relative_path] = committed_payload
    return payloads


def build_release(source_root: Path, output_dir: Path, source_commit: str) -> dict:
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source_commit must be one exact lowercase Git commit")

    payloads = validated_payloads_from_commit(source_root, source_commit)
    hashes = {
        relative_path: sha256_bytes(payload)
        for relative_path, payload in payloads.items()
    }

    manifest = manifest_bytes(source_commit, hashes)
    archive_name = (
        f"DreamzReservePaymentRunner-{RELEASE_VERSION}-"
        f"{source_commit[:12]}.zip"
    )
    zip_path = output_dir / archive_name
    checksum_path = output_dir / f"{archive_name}.sha256.txt"
    if zip_path.exists() or checksum_path.exists():
        raise FileExistsError("release output already exists")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=output_dir) as temporary:
        temporary_zip = Path(temporary) / archive_name
        with zipfile.ZipFile(
            temporary_zip,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            for relative_path in sorted(payloads):
                archive.writestr(zip_info(relative_path), payloads[relative_path])
            archive.writestr(zip_info(MANIFEST_NAME), manifest)
        temporary_zip.replace(zip_path)

    archive_hash = sha256_bytes(zip_path.read_bytes())
    checksum_path.write_text(
        f"{archive_hash}  {archive_name}\n",
        encoding="ascii",
        newline="\n",
    )
    return {
        "schema": 1,
        "release": RELEASE_NAME,
        "version": RELEASE_VERSION,
        "source_commit": source_commit,
        "zip": str(zip_path),
        "sha256": archive_hash,
        "payload_file_count": len(PAYLOAD_FILES),
        "archive_file_count": len(PAYLOAD_FILES) + 1,
        "manifest": MANIFEST_NAME,
        "eol_policy": "lf",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic, LF-normalized reserve-laptop release media."
        )
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = build_release(
        args.source_root,
        args.output_dir,
        args.source_commit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
