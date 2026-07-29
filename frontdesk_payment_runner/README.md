# Frontdesk payment-only one-shot runner

This is an offline installation package for the existing controlled flow:

`FEP → Member Portal queue → frontdesk_dreamz → guarded Gym Assistant UI writer`

It does not create a new payment path. It installs a separate manual runner and
does not replace, disable, start, stop, or edit the normal `Dreamz Portal Sync`
Scheduled Task.

The runner is fixed to:

- computer `DREAMZ-FRNTDSK`;
- Windows user `Dreamz Fitness`;
- agent `frontdesk_dreamz`;
- Gym Assistant root `C:\Gym Assistant 2.6`;
- production Member Portal over HTTPS;
- a minimum of 300 seconds desktop idle time.

Installation never starts the runner. A later, explicit manual command is
required to claim and process one already-approved Frontdesk command.

Before the first live run, a read-only inventory must confirm that the existing
`Dreamz Portal Sync` task is not also processing `frontdesk_dreamz` payment
commands. This one-shot must never overlap another Gym Assistant payment writer.
The package deliberately does not disable, stop, or edit that Scheduled Task.

Before any Gym Assistant UI action the runner durably records `writer_started`.
It records `applied` before acknowledging the Portal. An interrupted, malformed,
or otherwise uncertain result is therefore never clicked a second time
automatically. Install and restore always carry the newest local receipt ledger
forward. The launcher keeps a lifecycle mutex from installed-file validation
until its child exits; install and restore take that mutex first and the writer
mutex second, so code cannot be swapped during a run.

See `scripts\frontdesk_payment_runner\README.md` for the reviewed transfer,
inventory, installation, one-shot execution, and rollback commands.
