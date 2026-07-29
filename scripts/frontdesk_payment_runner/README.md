# Dreamz Frontdesk payment-runner 1.0.0

Status: offline release media. Building or extracting this package changes
nothing on `DREAMZ-FRNTDSK`.

## Purpose

This package lets the documented `Dreamz Fitness` Windows session manually
process one explicit `frontdesk_dreamz` command through the existing
FEP/Member Portal queue and guarded Gym Assistant UI writer.

The normal `Dreamz Portal Sync` Scheduled Task is deliberately untouched. The
installer creates no task, service, Startup shortcut, firewall rule, listener,
machine environment variable, or credential. It starts no process after
installation.

The package contains the coherent payment subset based on Portal staging commit
`4fbfff17d0758282e106cbeab8368787e7c34ca8`, plus the reviewed Gym Assistant
writer safety fixes from:

- `f6e2a9ecea0c6c5af8309869e944d483d809e863` — credit-balance prompts are
  declined only for a provably matching automatic FEP membership payment,
  current-balance payment remains zero, and member state is read back;
- `15f4847e588ad9be2ebda2c84adc976c9f7b4376` — a payment form that never opens
  is a retry-safe deferred result; post-submit ambiguity is never retried
  automatically.

The Frontdesk runner also carries the audited Office 1.0.2 crash-safety state
machine: it writes a durable local `writer_started` receipt before any UI
action, writes `applied` before the Portal acknowledgement, and never performs
a second click after an ambiguous result or lost acknowledgement.

## Before transfer

1. Use only the immutable ZIP and SHA-256 supplied with this release. The ZIP
   is built deterministically from one exact Git commit, normalizes all text to
   LF, and its manifest pins the exact 17-file payload (including every
   lifecycle script).
2. Verify the ZIP SHA-256 before extraction.
3. Do not add `.env`, token, cookie, database, browser-profile, member, bank, or
   Gym Assistant data files to the package.

The package does not contain or request a credential on the command line. At
run time it uses the existing `SYNC_API_TOKEN` already available to the
documented Frontdesk Windows context. If that credential is unavailable, it
stops before claiming a command.

## Read-only release validation

From an extracted local release folder:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\frontdesk_payment_runner\Test-DreamzFrontdeskPaymentRunnerRelease.ps1 `
  -PythonPath 'C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging\.venv\Scripts\python.exe'
```

This first enforces the exact 17-file allowlist and every manifest hash, then
checks PowerShell/Python syntax. No Python payload is executed while the
allowlist or a hash is invalid. It does not contact the Portal, inspect a
payment record, claim work, or operate Gym Assistant.

## Installation

Run only on `DREAMZ-FRNTDSK`, signed in as the documented `Dreamz Fitness`
Windows user:

```powershell
.\scripts\frontdesk_payment_runner\Install-DreamzFrontdeskPaymentRunner.ps1
```

The install location is:

```text
%LOCALAPPDATA%\Dreamz\FrontdeskPaymentRunner
```

The installer validates every runtime file hash, the exact computer and
Windows context, Python 3.10+, imports, PowerShell syntax, the fixed source
root, and the duplicate-prevention receipt ledger. An upgrade carries the
current ledger forward before moving the old installation into a timestamped
rollback snapshot. A genuinely new empty ledger is created only when no
installation and no rollback history exist. On installation failure the prior
installation is restored automatically.

Install, restore, and the one-shot launcher use the same lifecycle-then-runner
mutex order. This keeps installed files stable from validation until the child
runner exits and makes every lifecycle operation fail closed while a payment
writer is active.

The installer does **not** run the desktop preflight and does **not** start a
payment command.

Before the first live one-shot, use the approved read-only Frontdesk inventory
to confirm that the existing `Dreamz Portal Sync` task is not also processing
`frontdesk_dreamz` payment commands. Do not run two Gym Assistant payment
writers concurrently. This package intentionally does not stop, disable, or
edit that Scheduled Task.

## Read-only installed inventory

```powershell
.\scripts\frontdesk_payment_runner\Get-DreamzFrontdeskPaymentRunnerInventory.ps1
```

On the exact Frontdesk context this also runs the read-only desktop preflight,
but only after all installed static hashes, the exact installed manifest, and
the recorded trusted Python path are valid. A corrupt install therefore runs
no installed Python. The JSON contains only booleans, hashes, versions, counts,
and sanitized reason codes. It does not read or print a token, member, amount,
window title, or task arguments.

## Later one-shot execution

Only after the owner has separately verified in FEP that the intended command
is pending for **Frontdesk Dreamz**:

1. Disconnect TeamViewer and stop all remote input.
2. Leave the documented Gym Assistant window visible and unlocked.
3. Do not touch mouse or keyboard for at least five minutes.
4. In the already-open local PowerShell window run:

```powershell
& "$env:LOCALAPPDATA\Dreamz\FrontdeskPaymentRunner\process_fep_now.ps1" `
  -ConfirmApply
```

The preflight fails closed unless all of these are true:

- exact host and Windows context;
- active, unlocked `Default` desktop;
- at least 300 seconds idle;
- exact `C:\Gym Assistant 2.6\Data` source is reachable;
- exactly one visible Gym Assistant window reports that data path;
- no known payment/dependent/transaction dialog is already open;
- installed runner and writer hashes still match;
- the existing schema-2 duplicate-prevention ledger is valid.

After the preflight, the runner claims at most one explicit Frontdesk command,
processes no more than 500 updates, and exits. It does not scan/push members,
upload files, write a sync manifest, loop, or alter the Scheduled Task.

Any post-submit uncertainty remains manual reconciliation. Never click the
one-shot command again merely because a UI result is unclear; inspect FEP and
Gym Assistant first.

## Recovery from a rollback snapshot

Installation failure restores the old install automatically. For a later
manual recovery, use only the exact `upgrade-YYYYMMDD-HHMMSS-fff` directory
printed by the installer:

```powershell
.\scripts\frontdesk_payment_runner\Restore-DreamzFrontdeskPaymentRunner.ps1 `
  -RollbackPath '<exact printed rollback directory>'
```

Restore verifies the snapshot's exact location, manifest, and static file
hashes. It accepts only the current exact hash set or the one explicitly pinned
legacy Frontdesk hash set; unknown or modified snapshots fail closed. The
snapshot proves recovery eligibility and supplies only its validated fixed
runtime configuration. Every executable and static runtime file is always
rehydrated from the current, fully verified release package, and a new current
manifest records the current source commit and hashes. A pinned legacy launcher
or runner is therefore never reactivated.

Restore also does **not** restore the snapshot's older receipt ledger. The
current authoritative ledger is carried into the recovered installation and
validated first. Old status, log, stop, or other transient files are not
revived. The replaced installation is retained as `pre-restore-*`; no
Scheduled Task is changed and nothing is started. This is guarded recovery,
not a promise to put old executable code back into service.
