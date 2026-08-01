# Existing-member Journal source coverage probe

Status: local source only; not installed or run on `DREAMZ-FRNTDSK`.

`ga_journal_source_coverage_probe.py` is a standalone, stdout-only read-only
probe for the existing-member PRE/POST Journal evidence design. It does not
claim Portal work, use a token, call a network endpoint, mutate Gym Assistant,
or enable the evidence feature.

## Exact source contract

The production CLI is deliberately bound to the verified Dreamz locator:

`C:\Gym Assistant 2.6\Data\Journal.jtx`

The caller supplies the exact `Data` directory, not a journal file and not the
Gym Assistant install directory. The probe:

- accepts only `Gym Assistant 2.6\Data\Journal.jtx` under the hash-bound active
  Dreamz data-root;
- rejects UNC, mapped-drive, traversal and alternate-drive input through a
  purely lexical exact-`C:` check before any filesystem operation;
- rejects a missing source instead of falling back to `Backup`, `.bak`, an
  environment override, a symlink/reparse point, or another copy;
- requires two consecutive byte-identical reads with the same file identity,
  then confirms the same identity, bytes and metadata again after both tree
  scans;
- uses the exact classifier
  `dreamz.ga.journal.member-lines.v1` and validates every non-empty row against
  its 11-token header grammar and zero-based member-number field 5;
- scans the data tree twice for other journal-like or `.jtx` files and blocks on any
  possible rotated/historical segment, incomplete scan, reparse point or scan
  drift;
- emits only hashes, counts, booleans and fixed reason codes. It never emits a
  path, journal row, member number, member name, amount or payload.

## Future reviewed frontdesk command

Do not run this as an installer or as part of an ordinary Portal Sync cycle.
During a separately reviewed read-only frontdesk check, run:

```powershell
python .\ga_journal_source_coverage_probe.py --data-root "C:\Gym Assistant 2.6\Data"
```

Exit code `0` means the machine-readable JSON has `coverage_proven: true` and
no reason codes. Exit code `2` means fail-closed/block. A passing observation is
evidence for review; it does not by itself authorize setting
`PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE` or enabling the
feature.

The probe writes no result file. If an audit artifact is ever required, the
operator must redirect stdout to an explicitly reviewed location outside the
Gym Assistant root and preserve the resulting hash separately.

## Complete reason-code contract

- `classifier_version_mismatch`
- `data_root_binding_mismatch`
- `data_root_reparse_point`
- `install_root_reparse_point`
- `journal_empty`
- `member_number_field_unproven`
- `possible_historical_journal_segments`
- `probe_internal_error`
- `source_identity_changed`
- `source_missing`
- `source_reparse_point`
- `source_tree_reparse_point`
- `source_tree_scan_incomplete`
- `source_tree_unstable`
- `source_unavailable`
- `source_unstable`
- `unsupported_record_grammar`
- `wrong_data_root`

Any blocker keeps the existing-member evidence feature off and routes the
pilot to manual review.
