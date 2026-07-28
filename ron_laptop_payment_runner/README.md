# Ron laptop payment-only runner

This package is a narrow interactive Windows worker for the existing
FEP → Member Portal → Gym Assistant payment command flow.

It is intentionally fixed to:

- agent `ron_laptop`;
- Gym Assistant root `Z:\`;
- exact Gym Assistant data-window marker `Path=Z:\Data`;
- the existing production Member Portal HTTPS endpoint;
- the existing guarded `gymassistant_payment_writer.py`.

It never imports `sync_agent.py`, scans Gym Assistant member files, pushes
members/documents, or starts an inbound listener.

## Safety model

Before it claims a command it verifies:

1. the process is Windows and belongs to the active console session;
2. the input desktop is `Default` (not the lock/sign-in/UAC desktop);
3. `Z:\Data` is reachable;
4. exactly one visible Gym Assistant window reports `Path=Z:\Data`;
5. no payment/transaction/dependent dialog is already open;
6. the desktop has been idle for at least five seconds by default.

The checks repeat after claiming the command, before every update batch, and
before every payment. The runner peeks and claims exactly one update at a time
and requires its `upload_id` to equal the command `upload_id` before any UI
writer call. A command older than the configured TTL (15 minutes by default) is
failed without claiming or writing its payment updates.

A cooperative stop is checked between every payment. When processing must stop
after a partial batch, the command is reported as failed with its partial
received/applied/failed/deferred summary; unclaimed updates remain available
for a deliberate FEP retry.

A named Windows mutex prevents two runner processes in the same interactive
session. Before the official UI writer is invoked, a durable local receipt
containing only an update id, an idempotency-key hash, state and timestamps is
written. Success advances it to `applied` before API acknowledgement. A writer
exception or interrupted `writer_started` state becomes manual reconciliation;
a later claim never clicks Gym Assistant again. Invalid, partially invalid, or
unwritable, or unexpectedly missing receipt state blocks the installed runner.

Logs and `state/status.json` contain only operational states and counts. They
do not contain member ids, names, amounts, API tokens, window titles, or raw
Gym Assistant/API errors.

## Installation

Installation is deliberately **not** automatic and is not part of a Portal
deployment. On Ron's laptop, from a reviewed release checkout:

```powershell
$token = Read-Host "Member Portal sync token" -AsSecureString
.\scripts\ron_laptop_payment_runner\Install-RonLaptopPaymentRunner.ps1 -SyncToken $token
```

The installer:

- installs under `%LOCALAPPDATA%\Dreamz\RonLaptopPaymentRunner`;
- protects the token with current-user DPAPI;
- restricts the install directory ACL to the current user and SYSTEM;
- creates a current-user Startup shortcut only;
- retains any previous installation in a timestamped rollback directory;
- preserves and strictly validates the previous receipt ledger before an
  upgrade can start;
- refuses an upgrade if the existing ledger is missing, and creates an empty
  ledger only when there is no installation, Startup shortcut, or rollback
  history (a demonstrable first install);
- starts a hidden current-user launcher unless `-DoNotStart` is supplied.

It does not create a service, Scheduled Task, machine environment variable,
firewall rule, listener, or Gym Assistant data-file write path.

The installer and support staff may run the non-mutating local health check:

```powershell
python -m ron_laptop_payment_runner.runner --health-check
```

It validates only runtime JSON and receipt-ledger structure. It does not read a
token, contact Portal, inspect Gym Assistant, claim work, or operate the UI.

## Stop, uninstall, restore

The cooperative stop never kills an unrelated process:

```powershell
.\scripts\ron_laptop_payment_runner\Stop-RonLaptopPaymentRunner.ps1
```

Uninstall removes the Startup shortcut and moves the complete installation,
including its DPAPI-protected token and sanitized audit state, to a recoverable
rollback directory:

```powershell
.\scripts\ron_laptop_payment_runner\Uninstall-RonLaptopPaymentRunner.ps1
```

Restore takes the exact rollback directory printed by uninstall:

```powershell
.\scripts\ron_laptop_payment_runner\Restore-RonLaptopPaymentRunner.ps1 `
  -RollbackPath "<printed rollback directory>"
```

Only complete `uninstalled-*` snapshots can be restored, and their local
health check must pass before files are moved. Pre-upgrade snapshots are kept
for automatic installer rollback/audit but are refused for later manual
restore because their receipt ledger can be stale after newer payments run.
Restore also refuses an uninstall snapshot when any newer or same-generation
runner snapshot, unrecognized rollback state, current installation, or Startup
shortcut exists. This prevents an older ledger from replacing newer
duplicate-prevention receipts.

## Important limitations

- The writer visibly operates the official Gym Assistant UI. It is not a
  background or data-file writer and must run only in Ron's unlocked,
  interactive session.
- Windows drive mappings are per logon session. The runner waits without
  claiming when `Z:` is absent.
- A crash or exception after the durable `writer_started` receipt intentionally
  blocks automatic retry, even if the writer may not yet have clicked. Staff
  must reconcile that one payment manually; this conservative false-positive
  is preferred over a possible duplicate Gym Assistant payment.
- FEP/Portal authorization and queue idempotency remain the server-side
  authority. This package does not add a second payment mutation route.
- The existing sync token currently authorizes more Portal sync APIs than this
  process needs, and the Portal trusts the requested agent id after token
  authentication. This runner is code-fixed to `ron_laptop` and calls only the
  payment endpoints, but a future server change should add a separate
  least-privilege, agent-bound payment-runner credential.
