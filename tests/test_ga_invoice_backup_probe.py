from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

import ga_invoice_backup_probe as probe


TARGET_RENEWAL = (
    "c20260215!1558 7001 1771185480 0 990001 90001 3 0 0 0 29"
    "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
)
TARGET_NEW_MEMBERSHIP = (
    "c20260301!0900 7002 1772355600 0 990002 90002 1 0 0 0 29"
    "|900000101 257 20260301 20260401 6500 0 0 6500 0 0 0"
)
OTHER_PROSHOP = (
    "c20260215!1559 7003 1771185540 0 0 99999 38 0 0 0 29"
    "|200 ProShop purchase"
)
CATALOG = "\n".join(
    [
        "CLASS=contract Dreamz test",
        "MEMBERTYPE_ID=900000101",
        "OPTION=1 MONTHS EFT 6500 0",
        "-",
    ]
)


class NonSeekableBytesIO(BytesIO):
    def seekable(self) -> bool:
        return False

    def seek(self, *args, **kwargs):
        raise OSError("non-seekable test stream")


def canonical_allowlist_hash(*member_ids: str) -> str:
    encoded = "\n".join(sorted(member_ids, key=int)).encode("ascii")
    return sha256(encoded).hexdigest()


def write_backup(
    path: Path,
    *,
    journal: str | bytes = "\n".join(
        [TARGET_RENEWAL, TARGET_NEW_MEMBERSHIP, OTHER_PROSHOP]
    ),
    catalog: str | bytes = CATALOG,
    extra_entries: list[tuple[zipfile.ZipInfo | str, bytes | str]] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Journal.jtx", journal)
        archive.writestr("Members.btx", catalog)
        for name, data in extra_entries or []:
            archive.writestr(name, data)


class InvoiceBackupProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backup = self.root / "GABackup-test.gbu"
        self.allowlist = self.root / "allowlist.txt"
        self.allowlist.write_text("90001\n90002\n", encoding="ascii")
        write_backup(self.backup)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_probe(self, **overrides) -> dict:
        values = {
            "backup_path": self.backup,
            "expected_length": self.backup.stat().st_size,
            "expected_sha256": sha256(self.backup.read_bytes()).hexdigest(),
            "allowlist_path": self.allowlist,
            "expected_allowlist_count": 2,
            "expected_allowlist_set_sha256": canonical_allowlist_hash(
                "90001", "90002"
            ),
        }
        values.update(overrides)
        return probe.probe_invoice_backup(**values)

    def test_valid_exact_backup_passes_with_sanitized_counts(self):
        result = self.run_probe()

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["integrity_proven"])
        self.assertEqual(result["reason_codes"], [])
        self.assertEqual(result["allowlist"]["count"], 2)
        self.assertEqual(result["source"]["stable_read_count"], 2)
        self.assertTrue(result["archive"]["crc_verified"])
        self.assertEqual(result["analysis"]["target_membership_event_count"], 2)
        self.assertEqual(result["analysis"]["positive_membership_event_count"], 2)
        self.assertEqual(result["analysis"]["target_member_without_event_count"], 0)
        encoded = json.dumps(result, sort_keys=True)
        for forbidden in (
            "90001",
            "90002",
            "contract Dreamz test",
            "6500",
            str(self.backup),
            str(self.allowlist),
            "ProShop purchase",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_valid_data_descriptors_are_bound_to_central_values(self):
        output = NonSeekableBytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("Journal.jtx", TARGET_RENEWAL)
            archive.writestr("Members.btx", CATALOG)
        self.backup.write_bytes(output.getvalue())

        result = self.run_probe()

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["integrity_proven"])

    def test_cli_emits_one_json_line_and_no_stderr(self):
        stdout = StringIO()
        stderr = StringIO()
        argv = [
            "--backup-path",
            str(self.backup),
            "--expected-length",
            str(self.backup.stat().st_size),
            "--expected-sha256",
            sha256(self.backup.read_bytes()).hexdigest(),
            "--allowlist-path",
            str(self.allowlist),
            "--expected-allowlist-count",
            "2",
            "--expected-allowlist-set-sha256",
            canonical_allowlist_hash("90001", "90002"),
        ]

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = probe.main(argv)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertTrue(json.loads(stdout.getvalue())["integrity_proven"])

    def test_invalid_cli_is_sanitized_and_fail_closed(self):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = probe.main([])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["reason_codes"], ["invalid_arguments"])

    def test_wrong_length_blocks_before_zip_open(self):
        with patch.object(probe.zipfile, "ZipFile") as zip_file:
            result = self.run_probe(
                expected_length=self.backup.stat().st_size + 1
            )

        zip_file.assert_not_called()
        self.assertEqual(result["reason_codes"], ["source_length_mismatch"])
        self.assertIsNone(result["source"])

    def test_wrong_archive_hash_blocks_before_zip_open(self):
        with patch.object(probe.zipfile, "ZipFile") as zip_file:
            result = self.run_probe(expected_sha256="0" * 64)

        zip_file.assert_not_called()
        self.assertEqual(result["reason_codes"], ["source_hash_mismatch"])
        self.assertIsNone(result["source"])

    def test_oversized_metadata_blocks_before_decompression_or_testzip(self):
        raw = bytearray(self.backup.read_bytes())
        with zipfile.ZipFile(self.backup, "r") as archive:
            journal_offset = archive.getinfo("Journal.jtx").header_offset
        oversized = probe.MAX_ENTRY_BYTES + 1
        raw[journal_offset + 22 : journal_offset + 26] = oversized.to_bytes(
            4, "little"
        )
        central = raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        raw[central + 24 : central + 28] = oversized.to_bytes(4, "little")
        self.backup.write_bytes(raw)

        with (
            patch.object(
                probe.zlib,
                "decompressobj",
                side_effect=AssertionError("decompressed before metadata gate"),
            ) as decompressor,
            patch.object(
                probe.zipfile.ZipFile,
                "testzip",
                side_effect=AssertionError("testzip ran before metadata gate"),
            ) as testzip,
        ):
            result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["archive_size_limit_exceeded"]
        )
        decompressor.assert_not_called()
        testzip.assert_not_called()

    def test_allowlist_binding_is_exact(self):
        result = self.run_probe(expected_allowlist_count=3)

        self.assertEqual(result["reason_codes"], ["allowlist_binding_mismatch"])
        self.assertIsNone(result["source"])

    def test_empty_allowlist_is_rejected_as_invalid(self):
        self.allowlist.write_bytes(b"")

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["allowlist_invalid"])
        self.assertIsNone(result["source"])

    def test_allowlist_member_above_uint32_is_rejected(self):
        self.allowlist.write_text("90001\n4294967296\n", encoding="ascii")

        result = self.run_probe(
            expected_allowlist_set_sha256=canonical_allowlist_hash(
                "90001", "4294967296"
            )
        )

        self.assertEqual(result["reason_codes"], ["allowlist_invalid"])
        self.assertIsNone(result["source"])

    def test_non_c_drive_is_rejected_before_filesystem_access(self):
        result = self.run_probe(backup_path=Path(r"Z:\mapped\backup.gbu"))

        self.assertEqual(result["reason_codes"], ["backup_path_invalid"])
        self.assertIsNone(result["source"])

    def test_duplicate_required_basename_is_rejected(self):
        write_backup(
            self.backup,
            extra_entries=[("Data/JOURNAL.JTX", TARGET_RENEWAL)],
        )

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["archive_duplicate_required_entry"]
        )

    def test_path_traversal_entry_is_rejected(self):
        write_backup(self.backup, extra_entries=[("../escape.txt", b"canary")])

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_unsafe_entry"])

    def test_zip_symlink_entry_is_rejected(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        write_backup(self.backup, extra_entries=[(link, b"private-canary")])

        with patch.object(
            probe.zlib,
            "decompressobj",
            side_effect=AssertionError("payload validation ran before type gate"),
        ) as decompressor:
            result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_unsupported_entry"])
        decompressor.assert_not_called()

    def test_windows_special_attributes_on_required_entry_are_rejected(self):
        for attribute in (0x08, 0x10, 0x40, 0x400):
            with self.subTest(attribute=hex(attribute)):
                journal_info = zipfile.ZipInfo("Journal.jtx")
                journal_info.create_system = 0
                journal_info.external_attr = attribute
                with zipfile.ZipFile(
                    self.backup, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    archive.writestr(journal_info, TARGET_RENEWAL)
                    archive.writestr("Members.btx", CATALOG)

                result = self.run_probe()

                self.assertEqual(
                    result["reason_codes"], ["archive_unsupported_entry"]
                )

    def test_unsupported_zip_compression_is_rejected(self):
        with zipfile.ZipFile(self.backup, "w") as archive:
            archive.writestr(
                "Journal.jtx", TARGET_RENEWAL, compress_type=zipfile.ZIP_BZIP2
            )
            archive.writestr("Members.btx", CATALOG)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_unsupported_entry"])

    def test_crc_corruption_is_rejected_without_exposing_entry(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("Journal.jtx", TARGET_RENEWAL)
            archive.writestr("Members.btx", CATALOG)
        raw = bytearray(self.backup.read_bytes())
        offset = raw.find(TARGET_RENEWAL.encode("ascii"))
        self.assertGreaterEqual(offset, 0)
        raw[offset] ^= 0x01
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_crc_invalid"])

    def test_raw_nul_central_and_local_filename_is_rejected(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("Journal.jtxXY", TARGET_RENEWAL)
            archive.writestr("Members.btx", CATALOG)
        raw = self.backup.read_bytes()
        self.assertEqual(raw.count(b"Journal.jtxXY"), 2)
        self.backup.write_bytes(raw.replace(b"Journal.jtxXY", b"Journal.jtx\x00X"))

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_unsafe_entry"])

    def test_local_encryption_flag_cannot_disagree_with_central_directory(self):
        raw = bytearray(self.backup.read_bytes())
        with zipfile.ZipFile(self.backup, "r") as archive:
            offset = archive.getinfo("Journal.jtx").header_offset
        raw[offset + 6] |= 0x01
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_coherent_encryption_flags_are_rejected_before_reading(self):
        raw = bytearray(self.backup.read_bytes())
        with zipfile.ZipFile(self.backup, "r") as archive:
            offset = archive.getinfo("Journal.jtx").header_offset
        central = raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        raw[offset + 6] |= 0x01
        raw[central + 8] |= 0x01
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_encrypted_entry"])

    def test_local_compression_cannot_disagree_with_central_directory(self):
        raw = bytearray(self.backup.read_bytes())
        with zipfile.ZipFile(self.backup, "r") as archive:
            offset = archive.getinfo("Journal.jtx").header_offset
        raw[offset + 8 : offset + 10] = (12).to_bytes(2, "little")
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_local_dos_timestamp_must_match_central_directory(self):
        for field_offset in (10, 12):
            with self.subTest(field_offset=field_offset):
                write_backup(self.backup)
                raw = bytearray(self.backup.read_bytes())
                with zipfile.ZipFile(self.backup, "r") as archive:
                    offset = archive.getinfo("Journal.jtx").header_offset
                raw[offset + field_offset] ^= 0x01
                self.backup.write_bytes(raw)

                result = self.run_probe()

                self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_local_crc_cannot_disagree_with_central_directory(self):
        raw = bytearray(self.backup.read_bytes())
        with zipfile.ZipFile(self.backup, "r") as archive:
            offset = archive.getinfo("Journal.jtx").header_offset
        raw[offset + 14] ^= 0x01
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_stored_entry_cannot_declare_an_unconsumed_extra_byte(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("Journal.jtx", TARGET_RENEWAL)
            archive.writestr("Members.btx", CATALOG)
        with zipfile.ZipFile(self.backup, "r") as archive:
            journal = archive.getinfo("Journal.jtx")
            journal_offset = journal.header_offset
            original_compressed_size = journal.compress_size

        raw = bytearray(self.backup.read_bytes())
        filename_length = int.from_bytes(
            raw[journal_offset + 26 : journal_offset + 28], "little"
        )
        extra_length = int.from_bytes(
            raw[journal_offset + 28 : journal_offset + 30], "little"
        )
        data_end = (
            journal_offset
            + 30
            + filename_length
            + extra_length
            + original_compressed_size
        )
        raw[data_end:data_end] = b"X"
        raw[journal_offset + 18 : journal_offset + 22] = (
            original_compressed_size + 1
        ).to_bytes(4, "little")

        central_positions: list[int] = []
        cursor = 0
        while True:
            cursor = raw.find(b"PK\x01\x02", cursor)
            if cursor < 0:
                break
            central_positions.append(cursor)
            cursor += 4
        self.assertEqual(len(central_positions), 2)
        for central in central_positions:
            name_length = int.from_bytes(
                raw[central + 28 : central + 30], "little"
            )
            name = bytes(raw[central + 46 : central + 46 + name_length])
            if name == b"Journal.jtx":
                raw[central + 20 : central + 24] = (
                    original_compressed_size + 1
                ).to_bytes(4, "little")
            elif name == b"Members.btx":
                member_offset = int.from_bytes(
                    raw[central + 42 : central + 46], "little"
                )
                raw[central + 42 : central + 46] = (member_offset + 1).to_bytes(
                    4, "little"
                )
            else:  # pragma: no cover - fixture invariant
                self.fail(f"unexpected central entry: {name!r}")
        eocd = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(eocd, 0)
        central_offset = int.from_bytes(raw[eocd + 16 : eocd + 20], "little")
        raw[eocd + 16 : eocd + 20] = (central_offset + 1).to_bytes(4, "little")
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_deflated_entry_cannot_declare_an_unconsumed_tail(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("Journal.jtx", TARGET_RENEWAL)
            archive.writestr("Members.btx", CATALOG)
        with zipfile.ZipFile(self.backup, "r") as archive:
            members = archive.getinfo("Members.btx")
            members_offset = members.header_offset
            original_compressed_size = members.compress_size

        raw = bytearray(self.backup.read_bytes())
        filename_length = int.from_bytes(
            raw[members_offset + 26 : members_offset + 28], "little"
        )
        extra_length = int.from_bytes(
            raw[members_offset + 28 : members_offset + 30], "little"
        )
        data_end = (
            members_offset
            + 30
            + filename_length
            + extra_length
            + original_compressed_size
        )
        tail = b"HIDDEN"
        raw[data_end:data_end] = tail
        declared_size = original_compressed_size + len(tail)
        raw[members_offset + 18 : members_offset + 22] = declared_size.to_bytes(
            4, "little"
        )

        eocd = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(eocd, 0)
        old_central_offset = int.from_bytes(raw[eocd + 16 : eocd + 20], "little")
        central = old_central_offset + len(tail)
        total_entries = int.from_bytes(raw[eocd + 10 : eocd + 12], "little")
        for _index in range(total_entries):
            self.assertEqual(bytes(raw[central : central + 4]), b"PK\x01\x02")
            name_length = int.from_bytes(raw[central + 28 : central + 30], "little")
            central_extra_length = int.from_bytes(
                raw[central + 30 : central + 32], "little"
            )
            comment_length = int.from_bytes(
                raw[central + 32 : central + 34], "little"
            )
            name = bytes(raw[central + 46 : central + 46 + name_length])
            if name == b"Members.btx":
                raw[central + 20 : central + 24] = declared_size.to_bytes(
                    4, "little"
                )
            central += 46 + name_length + central_extra_length + comment_length
        raw[eocd + 16 : eocd + 20] = (
            old_central_offset + len(tail)
        ).to_bytes(4, "little")
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_deflated_output_cannot_exceed_declared_uncompressed_size(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("Journal.jtx", b"ABCD")
            archive.writestr("Members.btx", CATALOG)
        with zipfile.ZipFile(self.backup, "r") as archive:
            journal_offset = archive.getinfo("Journal.jtx").header_offset

        raw = bytearray(self.backup.read_bytes())
        truncated_crc = zlib.crc32(b"ABC") & 0xFFFFFFFF
        raw[journal_offset + 14 : journal_offset + 18] = truncated_crc.to_bytes(
            4, "little"
        )
        raw[journal_offset + 22 : journal_offset + 26] = (3).to_bytes(
            4, "little"
        )
        central = raw.find(b"PK\x01\x02")
        self.assertGreaterEqual(central, 0)
        raw[central + 16 : central + 20] = truncated_crc.to_bytes(4, "little")
        raw[central + 24 : central + 28] = (3).to_bytes(4, "little")
        self.backup.write_bytes(raw)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["archive_invalid"])

    def test_extreme_compression_ratio_is_rejected(self):
        write_backup(
            self.backup,
            extra_entries=[("compressed-canary.bin", b"0" * (4 * 1024 * 1024))],
        )

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["archive_compression_ratio_exceeded"]
        )

    def test_malformed_allowlisted_membership_row_blocks_without_leak(self):
        private_canary = "PRIVATE-TARGET-CANARY"
        malformed = (
            "bad-timestamp 7004 1771185600 0 990004 90001 3 0 0 0 29"
            f"|{private_canary}"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 1
        )
        self.assertNotIn(private_canary, json.dumps(result))

    def test_allowlisted_row_with_nonnumeric_event_type_blocks(self):
        malformed = (
            "c20260215!1600 7006 1771185720 0 990006 90001 NOT-A-TYPE 0 0 0 29"
            "|PRIVATE-EVENT-CANARY"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 1
        )
        self.assertNotIn("PRIVATE-EVENT-CANARY", json.dumps(result))

    def test_membership_type_with_ambiguous_member_field_blocks(self):
        malformed = (
            "c20260215!1601 7007 1771185780 0 990007 NOT-A-MEMBER 3 0 0 0 29"
            "|PRIVATE-MEMBER-CANARY"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 1
        )
        self.assertNotIn("PRIVATE-MEMBER-CANARY", json.dumps(result))

    def test_header_truncated_after_allowlisted_member_blocks(self):
        malformed = "c20260215!1602 7008 1771185840 0 990008 90001"
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 1
        )

    def test_member_number_above_uint32_blocks_as_ambiguous(self):
        malformed = (
            "c20260215!1603 7009 1771185900 0 990009 4294967296 3 0 0 0 29"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )

    def test_raw_event_type_above_uint16_blocks_for_allowlisted_member(self):
        malformed = (
            "c20260215!1604 7010 1771185960 0 990010 90001 65539 0 0 0 29"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )

    def test_epoch_above_uint32_blocks_for_allowlisted_member(self):
        malformed = (
            "c20260215!1605 7011 4294967296 0 990011 90001 3 0 0 0 29"
            "|900000101 257 20260201 20260301 6500 0 0 6500 0 0 0"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )

    def test_nonnumeric_member_and_event_fields_block_as_ambiguous(self):
        malformed = (
            "c20260215!1606 7012 1771186080 0 990012 NOTMEMBER NOTEVENT 0 0 0 29"
            "|PRIVATE-AMBIGUOUS-CANARY"
        )
        write_backup(self.backup, journal="\n".join([TARGET_RENEWAL, malformed]))

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertNotIn("PRIVATE-AMBIGUOUS-CANARY", json.dumps(result))

    def test_unstructured_nonempty_row_blocks_as_ambiguous(self):
        write_backup(
            self.backup,
            journal="\n".join([TARGET_RENEWAL, "PRIVATE-UNSTRUCTURED-CANARY"]),
        )

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertNotIn("PRIVATE-UNSTRUCTURED-CANARY", json.dumps(result))

    def test_large_malformed_journal_fails_on_first_record_without_amplification(self):
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("Journal.jtx", b"x\n" * (1024 * 1024))
            archive.writestr("Members.btx", CATALOG)

        result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 1
        )

    def test_many_lf_only_blank_records_are_scanned_linearly(self):
        journal = b"\n" * (256 * 1024) + TARGET_RENEWAL.encode("ascii")
        with zipfile.ZipFile(
            self.backup, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("Journal.jtx", journal)
            archive.writestr("Members.btx", CATALOG)

        result = self.run_probe()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["analysis"]["target_membership_event_count"], 1)

    def test_target_event_count_is_bounded(self):
        write_backup(
            self.backup,
            journal="\n".join([TARGET_RENEWAL, TARGET_NEW_MEMBERSHIP]),
        )

        with patch.object(probe, "MAX_TARGET_MEMBERSHIP_EVENTS", 1):
            result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["target_event_limit_exceeded"])

    def test_journal_record_length_is_bounded_before_field_splitting(self):
        write_backup(self.backup, journal=TARGET_RENEWAL)

        with patch.object(probe, "MAX_JOURNAL_RECORD_BYTES", 10):
            result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["target_membership_parse_issue"]
        )

    def test_catalog_record_count_is_bounded(self):
        write_backup(self.backup, catalog="A=1\nB=2\nC=3")

        with patch.object(probe, "MAX_CATALOG_RECORDS", 2):
            result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["catalog_resource_limit_exceeded"]
        )

    def test_catalog_record_bound_accepts_confirmed_official_backup_scale(self):
        record_count = 393_248
        total_chars = 3_450_357
        option_line = "OPTION=1 MONTHS INV 0 0"
        option_count = 72
        longest_line = "x" * 181
        remaining_records = record_count - option_count - 1
        remaining_line_chars = (
            total_chars
            - (record_count - 1)
            - (len(option_line) * option_count)
            - len(longest_line)
        )
        short_length, longer_count = divmod(
            remaining_line_chars,
            remaining_records,
        )
        short_count = remaining_records - longer_count
        catalog = "".join(
            (
                (option_line + "\n") * option_count,
                longest_line + "\n",
                (("x" * (short_length + 1)) + "\n") * longer_count,
                (("x" * short_length) + "\n") * (short_count - 1),
                "x" * short_length,
            )
        )

        self.assertEqual(len(catalog), total_chars)
        self.assertEqual(len(catalog.splitlines()), record_count)
        self.assertEqual(max(map(len, catalog.splitlines())), 181)
        self.assertEqual(
            sum(line.startswith("OPTION=") for line in catalog.splitlines()),
            option_count,
        )

        probe._validate_catalog_resource_bounds(catalog)

    def test_catalog_record_bound_is_exact(self):
        at_limit = ("x\n" * (probe.MAX_CATALOG_RECORDS - 1)) + "x"

        probe._validate_catalog_resource_bounds(at_limit)
        with self.assertRaises(probe.ProbeBlocked) as blocked:
            probe._validate_catalog_resource_bounds(at_limit + "\nx")

        self.assertEqual(
            blocked.exception.reason_code,
            "catalog_resource_limit_exceeded",
        )

    def test_catalog_line_and_option_row_bounds_are_exact(self):
        probe._validate_catalog_resource_bounds("x" * probe.MAX_CATALOG_LINE_CHARS)
        with self.assertRaises(probe.ProbeBlocked):
            probe._validate_catalog_resource_bounds(
                "x" * (probe.MAX_CATALOG_LINE_CHARS + 1)
            )

        at_option_limit = "\n".join(
            ["OPTION=1 MONTHS INV 0 0"]
            * probe.MAX_CATALOG_OPTION_OR_ADDON_ROWS
        )
        probe._validate_catalog_resource_bounds(at_option_limit)
        with self.assertRaises(probe.ProbeBlocked):
            probe._validate_catalog_resource_bounds(
                at_option_limit + "\nMONTH_ADDON=1,0,0|test"
            )

    def test_catalog_line_iterator_matches_python_splitlines(self):
        separators = ("\r\n", "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85")
        cases = ["", "single", "single\n", "\n", "a\n\nb"]
        cases.extend(f"a{separator}b{separator}" for separator in separators)

        for value in cases:
            with self.subTest(value=value.encode("latin-1").hex()):
                self.assertEqual(
                    list(probe._iter_catalog_lines(value)),
                    value.splitlines(),
                )

    def test_catalog_line_length_is_bounded_before_option_splitting(self):
        write_backup(self.backup, catalog=CATALOG)

        with patch.object(probe, "MAX_CATALOG_LINE_CHARS", 10):
            result = self.run_probe()

        self.assertEqual(
            result["reason_codes"], ["catalog_resource_limit_exceeded"]
        )

    def test_catalog_control_separators_and_unicode_strip_cannot_bypass_caps(self):
        for separator in (b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e", b"\x85"):
            with self.subTest(separator=separator.hex()):
                catalog = separator.join(
                    [
                        b"CLASS=bounded test",
                        b"MEMBERTYPE_ID=900000101",
                        b"\xa0OPTION=1 MONTHS EFT 6500 0",
                        b"OPTION=1 MONTHS INV 6500 0",
                        b"-",
                    ]
                )
                write_backup(self.backup, catalog=catalog)

                with patch.object(
                    probe,
                    "MAX_CATALOG_OPTION_OR_ADDON_ROWS",
                    1,
                ):
                    result = self.run_probe()

                self.assertEqual(
                    result["reason_codes"],
                    ["catalog_resource_limit_exceeded"],
                )

    def test_latin1_nel_and_unicode_whitespace_cannot_hide_target_row(self):
        for padding in (b"", b"\xa0", b"\x1f", b"\xa0\x1f\t"):
            with self.subTest(padding=padding.hex()):
                journal = (
                    OTHER_PROSHOP.encode("ascii")
                    + b"\x85"
                    + padding
                    + TARGET_RENEWAL.encode("ascii")
                )
                write_backup(self.backup, journal=journal)

                result = self.run_probe()

                self.assertEqual(
                    result["reason_codes"], ["target_membership_parse_issue"]
                )

    def test_malformed_other_member_does_not_block_target_scope(self):
        malformed_other = (
            "bad-timestamp 7005 1771185660 0 990005 99999 3 0 0 0 29"
            "|PRIVATE-OTHER-CANARY"
        )
        write_backup(
            self.backup,
            journal="\n".join(
                [TARGET_RENEWAL, TARGET_NEW_MEMBERSHIP, malformed_other]
            ),
        )

        result = self.run_probe()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["analysis"]["target_membership_parse_issue_count"], 0
        )
        self.assertNotIn("PRIVATE-OTHER-CANARY", json.dumps(result))

    def test_allowlisted_member_without_event_is_counted_not_blocked(self):
        write_backup(self.backup, journal=TARGET_RENEWAL)

        result = self.run_probe()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["analysis"]["target_member_without_event_count"], 1)
        self.assertEqual(
            result["analysis"]["target_member_without_positive_event_count"], 1
        )

    def test_duplicate_catalog_key_blocks_silent_overwrite(self):
        duplicate_catalog = CATALOG + "\n" + CATALOG
        write_backup(self.backup, catalog=duplicate_catalog)

        result = self.run_probe()

        self.assertEqual(result["reason_codes"], ["catalog_ambiguous"])
        self.assertEqual(result["analysis"]["catalog_duplicate_key_count"], 1)

    def test_probe_creates_no_files_or_directory_changes(self):
        before = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }

        result = self.run_probe()

        after = {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(result["status"], "passed")
        self.assertEqual(before, after)

    def test_source_has_no_network_or_mutating_dependencies(self):
        source = Path(probe.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "import requests",
            "import socket",
            "import subprocess",
            "import urllib",
            ".extract(",
            ".extractall(",
            ".write_text(",
            ".write_bytes(",
            "os.remove(",
            "os.unlink(",
            "shutil.",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn('data.find(b"\\r"', source)


if __name__ == "__main__":
    unittest.main()
