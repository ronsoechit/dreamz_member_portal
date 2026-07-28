# Dreamz Office PC payment-only runner

This package is a narrow interactive Windows worker for the existing
FEP → Member Portal → Gym Assistant payment command flow.

It is intentionally fixed to:

- agent `dreamz_office`;
- label `Dreamz Office PC`;
- the existing production Member Portal HTTPS endpoint;
- the existing guarded `gymassistant_payment_writer.py`;
- one exact Gym Assistant source root recorded by the installer.

The source root cannot be supplied through the command line or an environment
variable. The installer requires an existing absolute Windows directory whose
`Data` child also exists, writes the normalized path to `runtime.json`, and
refuses an upgrade that attempts to change it. The runner then requires exactly
one visible Gym Assistant window whose `Path=` value equals
`<SourceRoot>\Data`.

It never imports `sync_agent.py`, scans Gym Assistant member files, pushes
members/documents, starts an inbound listener, creates a service or Scheduled
Task, or writes Gym Assistant data files directly.

## Required inventory before installation

Run the separate read-only inventory first:

```powershell
.\scripts\dreamz_office_payment_runner\Get-DreamzOfficePaymentRunnerInventory.ps1
```

For a non-technical Office-PC user, double-click:

```text
scripts\dreamz_office_payment_runner\START-INVENTARISATIE.cmd
```

The launcher runs local Windows PowerShell without a persistent execution-policy
change and atomically writes
`Dreamz-Office-PC-inventarisatie.json` beside the launcher. It does not request
administrator rights, install anything, or ask for a token. The PowerShell
script also keeps printing the same sanitized JSON to stdout and supports an
explicit `-OutputPath` when needed.

The owner must verify the reported interactive session, exact recognized Gym
Assistant data path, matching visible-window count, drive/share reachability,
Python version, Startup availability, power/sleep state, and existing runner
state. The inventory does not read member files, tokens, process command lines,
or arbitrary window titles and does not install or alter system/application
configuration. When `-OutputPath` is used, its only write is the requested
sanitized JSON report, replaced atomically through a temporary file in the same
directory.

Do not guess the source root. Pass the parent of the reported `Data` directory
to the installer only after it has been reviewed.

The current reviewed candidate hostname is `DREAMZ-OFFICE-P`, inferred from the
Office transfer share. Both installer and runner are fixed to that hostname.
The inventory must confirm it before installation; if the actual Office host
differs, stop and review the package instead of bypassing this binding.

## Safety model

Before it claims a command it verifies:

1. the process is Windows and belongs to the active console session;
2. the input desktop is `Default` (not the lock/sign-in/UAC desktop);
3. the configured source root and its `Data` child are reachable;
4. exactly one visible Gym Assistant window reports the exact configured data
   root;
5. no payment/transaction/dependent dialog is already open;
6. the desktop has been idle for at least 300 seconds by default.

The checks repeat after claiming the command, before every update batch, and
before every payment. The runner peeks and claims exactly one update at a time
and requires its `upload_id` to equal the command `upload_id` before any UI
writer call. A command older than 15 minutes by default is failed without
claiming or writing payment updates.

A cooperative stop is checked between every payment. A named Windows mutex
prevents two Office runners in the same interactive session. Before the
official UI writer is invoked, a durable local receipt stores only an update
id, an idempotency-key hash, state and timestamps. Success advances it to
`applied` before API acknowledgement. A writer exception or interrupted
`writer_started` state becomes manual reconciliation; a later claim never
clicks Gym Assistant again. Invalid, missing or unwritable receipt state blocks
the installed runner.

Logs and `state/status.json` contain only operational states, configured source
root and counts. They do not contain member ids, names, amounts, API tokens,
window titles, or raw Gym Assistant/API errors.

## Installation

Installation is deliberately not automatic and is not part of a Portal
deployment. Install only in the same interactive Windows user profile that owns
the visible Office Gym Assistant session:

```powershell
$sourceRoot = Read-Host "Exact SourceRoot reviewed from the inventory JSON"
$token = Read-Host "Dreamz Office payment-runner token" -AsSecureString
.\scripts\dreamz_office_payment_runner\Install-DreamzOfficePaymentRunner.ps1 `
  -SourceRoot $sourceRoot `
  -PaymentToken $token `
  -DoNotStart
```

The installer:

- installs under `%LOCALAPPDATA%\Dreamz\DreamzOfficePaymentRunner`;
- protects the dedicated payment token with current-user DPAPI;
- restricts the install-directory ACL to the current user and SYSTEM;
- creates a current-user Startup shortcut only;
- retains the previous installation in a timestamped rollback directory;
- preserves and validates the previous receipt ledger before an upgrade;
- refuses to initialize an empty ledger over any prior runner history;
- refuses an upgrade that changes the configured source root;
- starts a hidden current-user launcher unless `-DoNotStart` is supplied.

It does not create a service, Scheduled Task, machine environment variable,
firewall rule, listener, or Gym Assistant data-file write path.

The non-mutating local health check validates only runtime JSON and receipt
structure:

```powershell
python -m dreamz_office_payment_runner.runner --health-check
```

It does not read a token, contact Portal, inspect Gym Assistant, claim work, or
operate the UI.

## Stop, uninstall and restore

```powershell
.\scripts\dreamz_office_payment_runner\Stop-DreamzOfficePaymentRunner.ps1
.\scripts\dreamz_office_payment_runner\Uninstall-DreamzOfficePaymentRunner.ps1
.\scripts\dreamz_office_payment_runner\Restore-DreamzOfficePaymentRunner.ps1 `
  -RollbackPath "<printed rollback directory>"
```

Stop is cooperative and never kills another process. Uninstall moves the full
installation, including its DPAPI token and safety ledger, to a recoverable
rollback directory. Restore accepts only the newest provably safe complete
uninstall snapshot; it refuses pre-upgrade or stale-ledger snapshots.

## Important limitations

- The writer visibly operates the official Gym Assistant UI. It must run only
  in the active, unlocked Office session while the workstation is genuinely
  free.
- Five minutes idle is a guard, not proof that a staff member has finished
  working. An admin must select this computer only when the office workstation
  is known to be unused.
- Drive mappings are per logon session. The runner waits without claiming when
  the configured source is absent.
- A crash after the durable `writer_started` receipt intentionally blocks
  automatic retry. Staff must reconcile that payment manually.
- FEP/Portal authorization, upload scoping and queue idempotency remain the
  server-side authority. This package does not add another payment mutation
  route.
- The dedicated token must be bound server-side to `dreamz_office` and to the
  payment endpoints before this runner is started in production.
