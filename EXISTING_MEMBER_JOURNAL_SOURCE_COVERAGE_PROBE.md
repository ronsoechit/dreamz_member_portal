# Existing-member official Journal export preflight

Status: local source only; not installed or enabled on `DREAMZ-FRNTDSK`.

`ga_journal_source_coverage_probe.py` is a stdout-only, read-only preflight for
the existing-member PRE/POST Journal evidence flow. It does not claim Portal
work, call the network, write a file, control Gym Assistant or enable evidence.

## Correct source contract

The live source is the proprietary binary file:

`C:\Gym Assistant 2.6\Data\Journal.dat`

It is never parsed as JTX. The older `Backup\Journal.jtx` is a historical
export and is not treated as the current journal. At runtime, Portal Sync must
ask Gym Assistant itself to execute **Export Journal**, save that result to a
temporary path outside the Gym Assistant tree, validate every CRLF-delimited
record with classifier `dreamz.ga.journal.export-records.v2`, use only hashes
and counts for evidence, and then delete the temporary export. A version-only
or otherwise recordless export is rejected.

The preflight:

- accepts only the exact production `C:` data directory;
- rejects UNC paths, mapped drives, traversal, symlinks and reparse points;
- verifies two identical reads and unchanged identities for `Journal.dat` and
  `Gym Assistant 26.exe`;
- inventories `Journal.tmp`, rotated `.dat` files and historical `.jtx` files
  under the known `Data`, `Backup` and `Data\Backup` roots as a hashed source
  binding; these known segments are not parsed and are not automatically an
  error;
- returns `export_preflight_ready: true` only when the local prerequisites are
  stable;
- always returns `coverage_proven: false` and
  `official_export_required: true`, because only a fresh successful official
  export can prove record coverage;
- emits only hashes, counts, booleans and fixed reason codes. It never emits a
  source path, row, member number, name, amount or payload.

## Reviewed frontdesk preflight

This command is diagnostic only and must not be used as an installer:

```powershell
python .\ga_journal_source_coverage_probe.py --data-root "C:\Gym Assistant 2.6\Data"
```

Exit code `0` means the machine is ready for a separately controlled official
export test. Exit code `2` means fail closed. A ready result does not authorize
enabling the feature. The first official export, its sanitized validation
summary, rollback path and Portal/Signup compatibility must be reviewed first.

## Complete reason-code contract

- `active_journal_empty`
- `classifier_version_mismatch`
- `data_root_binding_mismatch`
- `data_root_reparse_point`
- `executable_empty`
- `executable_missing`
- `executable_reparse_point`
- `install_root_reparse_point`
- `probe_internal_error`
- `source_identity_changed`
- `source_missing`
- `source_reparse_point`
- `source_tree_reparse_point`
- `source_tree_scan_incomplete`
- `source_tree_unstable`
- `source_unavailable`
- `source_unstable`
- `wrong_data_root`

Any blocker keeps the evidence feature off and routes a pilot to manual review.
