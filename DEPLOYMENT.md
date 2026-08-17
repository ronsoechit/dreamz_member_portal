# Dreamz Member Portal Deployment

## Railway staging setup

Create a staging project first. Do not use the staging URL publicly until the data, storage, and staff flow are verified.

### Services

1. Web service from this GitHub repository.
2. Postgres service.
3. S3-compatible bucket/object storage for member PDFs and photos.

### Required web environment variables

Set these on the Railway web service:

```text
SECRET_KEY=<long random value>
DATABASE_URL=<Railway Postgres connection URL>
STAFF_ADMIN_USERNAME=ron
STAFF_ADMIN_PASSWORD=<strong temporary password>
STAFF_MANAGER_USERNAME=manager
STAFF_MANAGER_PASSWORD=<strong temporary password>
STAFF_ADMIN_EMAIL=ron@dreamzfitness.com
STAFF_TOKEN=<long random staff token>
SYNC_API_TOKEN=<long random sync token>
# Optional, unique payment-only token for the dedicated Ron laptop runner.
FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP=<different long random token>
# Optional, unique payment-only token for the dedicated Dreamz Office PC runner.
FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE=<different long random token>
# Required, unique payment-only token for reserve agent reserve_8km7v7d.
FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D=<different long random token>
FEP_API_TOKEN=<long random FEP token>
SIGNUP_PORTAL_INTEGRATION_TOKEN=<shared random token of at least 32 characters>
PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS=
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS=
EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED=false
EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE_PROVEN=false
EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_PUBLIC_KEYS_JSON={}
EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_SIGNING_KEY_ID=
EXISTING_MEMBER_JOURNAL_EVIDENCE_PORTAL_PRIVATE_KEY=
EXISTING_MEMBER_JOURNAL_EVIDENCE_CLAIM_SECONDS=120
MEMBER_PORTAL_PUBLIC_URL=https://dreamzfitness.app
WORDPRESS_SCHEDULE_WEBHOOK_URL=https://dreamzfitness.com/wp-json/dreamz/v1/group-class-schedule
WORDPRESS_SCHEDULE_WEBHOOK_TOKEN=<shared random token used only for schedule publication>
SYNC_STALE_AFTER_MINUTES=30
SESSION_COOKIE_SECURE=true
EMAIL_DELIVERY_MODE=smtp
SMTP_HOST=<smtp host>
SMTP_PORT=465
SMTP_USER=<smtp username>
SMTP_PASS=<smtp password>
SMTP_FROM=<sender email>
SMTP_FROM_NAME=Dreamz Fitness
SMTP_TIMEOUT_SECONDS=20
DIRECT_DEBIT_DAY=28
STORAGE_BACKEND=s3
S3_BUCKET=<bucket name>
S3_ENDPOINT_URL=<S3-compatible endpoint URL>
S3_REGION=<region, or auto>
S3_ACCESS_KEY_ID=<storage access key>
S3_SECRET_ACCESS_KEY=<storage secret key>
S3_PREFIX=gymassistant
S3_ADDRESSING_STYLE=virtual
```

### First staging test

1. Deploy web + Postgres.
2. Open `/staff/login` and log in as admin.
3. Confirm `/staff/sync` loads.
4. Run the sync agent locally with a small member limit:

```powershell
$env:SYNC_API_TOKEN="<same token as Railway>"
.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "D:\Dreamz Fitness\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --push-members `
  --member-limit 10
```

5. Check `/staff/sync`, `/staff/data-audit`, and `/staff/email-log`.

### Signup portal activation rollout

Deploy the member portal before the signup application. Configure the same
`SIGNUP_PORTAL_INTEGRATION_TOKEN` on both services, but never place it in source
control or logs. The signup service must use
`MEMBER_PORTAL_URL=https://dreamzfitness.app`.

The signup application may enqueue an invitation only for an eligible completed
membership. The portal keeps the request pending until a later GymAssistant sync
contains exactly one member with the expected member ID, email address, and an
eligible membership plan. Only then does the portal send the localized activation
email. Repeated requests and syncs are idempotent and do not send a second email.

Roll out in this order:

1. Deploy the portal and verify its health and protected integration endpoint.
2. Deploy the signup application and verify that its health reports the portal
   integration as configured.
3. Test the delayed-sync path with a controlled test member before allowing a
   real signup to rely on the activation mail.
4. Check `/staff/email-log` and the portal invitation status if a request needs
   manual review. Do not automatically retry an ambiguous SMTP result because
   that could send a duplicate message.

Existing signups are marked for legacy reconciliation and do not receive a bulk
activation email. Existing members entered manually in GymAssistant can request a
normal one-time login code from the portal after the sync has imported their
unique email address.

The existing-member reverification handoff is a separate closed pilot. Deploy it
with `PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false`; this is also the
runtime default. Enabling the flag alone cannot release an activation email.
Each pilot allowlist must contain exactly one value, and the exact Gym Assistant
member number **and** the exact verified email address must match the request.
Empty, one-sided or multi-value allowlists fail closed in
`waiting_for_pilot_allowlist`. The protected flow is also SMTP-only: without
exact `EMAIL_DELIVERY_MODE=smtp` it remains in
`waiting_for_smtp_configuration`, and only an exact successful delivery result
may become `sent`. Add only the single controlled pilot identity during a
reviewed rollout, and never commit real member numbers or email addresses to
this file.

Existing-member requests must send `request_type=existing_member_reverification`
and the SHA-256 digest of the canonical request both in `idempotency_key` and the
`Idempotency-Key` header. The Portal binds the reference, request type, member
number, verified email and mapped plan, targets only the already-synced member,
and echoes `request_type`, `member_number` and `idempotency_key` to the protected
caller. It never creates a second Portal member.

The protected writer has a second, independent journal-evidence gate. Keep
`EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED=false` until all of the following have
been reviewed together: the authoritative binary `Data\Journal.dat` source
binding, a fresh validated Gym Assistant **Export Journal** result, the exact
Portal Sync classifier, the Portal and agent Ed25519 public-key maps, and a
Signup Bridge that advertises and enforces
`dreamz.signup-bridge.existing-member-journal-evidence.v3`. `Journal.dat` is
never parsed as JTX and `Backup\Journal.jtx` is never treated as the current
source. The Portal owns its
receipt-signing private key; the frontdesk Portal Sync agent owns a different
agent-signing private key. Never exchange, log, commit or reuse those private
keys.

When the gate is eventually enabled, the local `Dreamz Portal Sync` task also
requires these profile-scoped settings:

```text
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED=true
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_SOURCE_COVERAGE=dreamz.ga.journal.export-records.v2
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_AGENT_ID=frontdesk_dreamz
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_KEY_ID=<current agent key ID>
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_PRIVATE_KEY=<frontdesk-only Ed25519 private key>
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_TIMEOUT_SECONDS=15
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_LIMIT=1
PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_RETRIES=1
PORTAL_SYNC_JOURNAL_EXPORT_GYMASSISTANT_EXE=C:\Gym Assistant 2.6\Gym Assistant 26.exe
PORTAL_SYNC_JOURNAL_EXPORT_EXPECTED_DATA_PATH=C:\Gym Assistant 2.6\Data
PORTAL_SYNC_JOURNAL_EXPORT_CANDIDATE_PATH=C:\DreamzPortalSync\journal-evidence\Journal.pending.jtx
```

The export reuses the DPAPI-protected master-access credential and the Signup
Bridge work root by default. It acquires the same named export mutex, pauses
the bridge, never starts Gym Assistant by itself, and refuses to proceed while
another Gym Assistant dialog/member view
is open, and requires the temporary `.jtx` to live outside the Gym Assistant
installation. A version-only export or any unsupported record fails closed.
The agent validates the entire temporary export, returns only hashes/counts and
deletes the temporary JTX in all success and failure paths. No production feature
may be enabled until a controlled frontdesk export test has passed.

After the matching immutable Portal Sync package is installed with the feature
still disabled, run the operator check as the interactive
`DREAMZ-FRNTDSK\Dreamz Fitness` user. Running it without a switch performs only
the read-only source preflight:

```powershell
.\scripts\Test-OfficialGymAssistantJournalExport.ps1
```

Only in an agreed quiet window, with Gym Assistant already open at its clean
main member screen, run the same script with `-ExecuteOfficialExport`. That
action opens Gym Assistant's own `Export Journal` flow, validates the temporary
result, prints only hashes/counts, removes the temporary file, and processes no
Portal request or member change.

The five-minute task cycle must remain healthy. A signed PRE receipt is required
before the existing-member update becomes claimable. A successful local read-back
is provisional: only an exact signed POST receipt with verdict
`verified_unchanged` completes the workflow and permits the Portal invitation.
Missing, expired, unstable, incomplete, mismatched or legacy evidence always
stops in manual review and never replays the writer.

### Sync monitoring

The portal shows a warning on `/staff/sync` when no completed GymAssistant sync has been received for `SYNC_STALE_AFTER_MINUTES` minutes. The sync agent itself performs one scan and push per run, so automatic recovery after a power outage depends on the Windows Task Scheduler task or service on the frontdesk computer starting again after reboot. Configure that task to run on startup/login, repeat every few minutes, and run missed tasks as soon as possible.

### FEP payment write-back

FEP payment updates are accepted by the portal at `/api/fep/payment-update` with `FEP_API_TOKEN`. The portal validates and queues the update, but does not mark the member paid until the local GymAssistant sync confirms the changed payment fields.

Linked GymAssistant memberships are excluded from automatic write-back. If a member is a dependent of another member, or has dependents linked to the account, the portal returns `409` for manual review. The guarded UI writer also cancels GymAssistant's dependent-member prompt instead of choosing a responsible member automatically.

The frontdesk sync agent can listen for an explicit FEP command before pushing normal member sync data. This is the preferred production mode: normal syncs do not book payments unless FEP has first sent a `payment-process-request` for that target agent.

Payment agents are strictly allowlisted as `frontdesk_dreamz`, `ron_laptop`,
`dreamz_office` (`Dreamz Office PC`), and `reserve_8km7v7d`
(`Reserve laptop 8KM7V7D`). The optional
`FEP_PAYMENT_SYNC_TOKEN_RON_LAPTOP` and
`FEP_PAYMENT_SYNC_TOKEN_DREAMZ_OFFICE` variables and the mandatory
`FEP_PAYMENT_SYNC_TOKEN_RESERVE_8KM7V7D` variable provide payment-only,
agent-bound credentials. The reserve agent fails closed until its dedicated
token is configured. Roll them out one computer at a time:

1. Confirm that no payment command or update is running or processing.
2. Generate a new token that differs from `SYNC_API_TOKEN` and every other
   payment token.
3. Install that token only in the matching runner.
4. Set the matching Railway variable.
5. Verify its payment-command peek and confirm that `/api/sync/member-ids`
   returns `403` with that token.

As soon as a dedicated token is configured, the matching payment agent stops
accepting the shared `SYNC_API_TOKEN`. A dedicated token cannot operate another
payment agent and cannot call member, file, document, or other non-payment sync
endpoints. Use it only in a payment-only runner; do not put it in the normal
member-sync task or combine it with `--push-members`. If the optional variable
is absent, the existing shared-token behavior remains available for that agent.

All three payment agents share one global Gym Assistant writer lease. Multiple
commands may wait, but the portal permits only one running command or legacy
payment batch at a time across all computers. Expired UI-write claims are moved
to manual reconciliation instead of being automatically clicked again.

```powershell
$env:SYNC_API_TOKEN="<same token as Railway>"
$env:FEP_PAYMENT_WRITER_COMMAND="<local command that writes one payment update to GymAssistant>"
.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "D:\Dreamz Fitness\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --agent-id "frontdesk_dreamz" `
  --process-fep-command `
  --push-members
```

For controlled/manual sessions, the frontdesk sync agent can also process queued FEP updates directly before pushing normal member sync data:

```powershell
$env:SYNC_API_TOKEN="<same token as Railway>"
$env:FEP_PAYMENT_WRITER_COMMAND="<local command that writes one payment update to GymAssistant>"
.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "D:\Dreamz Fitness\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --process-fep-payments `
  --push-members
```

The writer command receives JSON on stdin and must update GymAssistant itself. The agent refuses to fake payment state when no writer command is configured.

Do not run a visible GymAssistant UI writer during normal frontdesk work. It can interrupt staff if they are entering a member, taking a payment, or using other software. Use one of these safe modes:

- preferred: an official GymAssistant batch/import/payment route, if available;
- acceptable: a dedicated/idle GymAssistant workstation/session that staff do not use;
- fallback: schedule the writer outside staffed hours or require a long Windows idle time.

The included `gymassistant_payment_writer.py` is a guarded UI writer for controlled testing. It only records a payment when explicitly started with `--apply --foreground-ui`; use `--require-idle-seconds` if it is ever scheduled on a staff workstation. If the desktop is not idle, the writer returns `deferred`, and the sync agent leaves the queued payment pending for a later run.

### Gym Assistant membership-invoice pilot

This pilot reads only Gym Assistant membership events (Journal action 1 or 3)
for explicitly allowlisted members. It creates a membership line from
`dues_cents`; it never uses a member's open balance, aggregate last-payment
amount, ProShop purchases, drinks, account payments, or free-text product
descriptions as an invoice amount. PT and Group PT remain blocked until they
have their own structured and price-verified source.

The historical 14-member import and invoice issuing are separate gates. The
first import uses the dedicated, atomic
`/api/sync/invoice-event-batches` endpoint. It must not use
`/api/sync/members`, because that endpoint also synchronizes member/document
data and reconciles portal invitations.

Use this controlled rollout:

1. Back up the production Postgres database. The first request after deployment
   creates the new additive invoice tables through the existing runtime schema
   bootstrap.
2. Confirm that `DF-<year>-000001` is approved as a separate legal invoice
   number series before issuing the first document. Only then set
   `INVOICE_NUMBER_SERIES_APPROVED=true`.
3. Deploy the code with both `INVOICE_GA_PILOT_ENABLED=false` and
   `INVOICE_ISSUING_ENABLED=false`.
4. Verify the admin-only `/staff/invoices` page. Confirm private S3/R2 storage
   is configured; Railway-local storage is not durable and must not be used for
   issued PDFs.
5. Set `INVOICE_GA_PILOT_ENABLED=true` and configure the exact private
   14-member cohort in `INVOICE_GA_PILOT_MEMBER_IDS` on the portal. Keep the
   private allowlist outside Git and deployment packages intended for broad
   sharing. Do not union this bootstrap cohort with monitor IDs.
6. Run `invoice_event_batch.py` only through a reviewed exact release package.
   Bind the first run to the already approved backup length/SHA-256, exact
   allowlist count/hash, exact target-event count and exact permitted short-
   fragment count. The package reads the `C:\Gym Assistant 2.6\Data\Backup`
   `.gbu` twice, scans the allowlisted Journal rows, builds the payload from
   that same in-memory scan, and submits only invoice events/status data.
7. Require the explicit invoice receipt to match the batch, source, event-set
   and member-set hashes and to report zero rejects/conflicts. A repeat with
   the same batch ID and body is a no-op; the same ID with a different body is
   rejected. One invalid event or conflict rolls back the whole batch. The
   bootstrap creates no draft, PDF, email, portal invitation or invoice number.
8. Create a fresh `.gbu` before preparing drafts. Run the same strict scanner
   against it so the portal has a current source timestamp. Keep
   `INVOICE_ISSUING_ENABLED=false`, then verify each latest membership amount
   and service period against Gym Assistant. Any open account balance, drinks
   or ProShop amount must not appear as an invoice line.
9. Use **Check membership events** to prepare drafts only for individually
   eligible members. A draft does not get
   an invoice number and is not visible to the member.
10. After the source, number series, legal fields, inclusive ABB treatment, and
   S3 write/read/download have been checked, set
   `INVOICE_ISSUING_ENABLED=true`. An admin must complete all three standard
   review checkboxes before an individual pilot invoice can be issued. If the
   Gym Assistant period differs from the catalog billing interval, an
   additional explicit period confirmation is required.
11. Sign in as that member and verify `/account/invoices` and the private
   PDF download in the member's selected portal language.

Issuing never sends an automatic email. To stop the pilot immediately, set
`INVOICE_ISSUING_ENABLED=false`; to hide the pilot from the member and stop
financial Journal imports as well, also set `INVOICE_GA_PILOT_ENABLED=false`.
Members retain access to their existing issued PDFs after the pilot flag is
disabled; those PDFs remain immutable in private object storage. The updated
frontdesk agent also keeps previously imported invoice members in its
read-only monitor set, so a later Gym Assistant void/reversal can still
invalidate the linked portal invoice while new drafts remain disabled.

### Storage note

The full GymAssistant folder is not uploaded to Railway. The portal database stores structured member data and document metadata. PDFs/photos should be uploaded to object storage once the storage adapter is enabled.

### File storage sync

Use an S3-compatible bucket for PDFs and member photos. Keep the bucket private; the portal proxies authenticated document/photo requests.

After setting the same S3 variables locally, run:

```powershell
$env:SYNC_API_TOKEN="<same token as Railway>"
$env:STORAGE_BACKEND="s3"
$env:S3_BUCKET="<bucket name>"
$env:S3_ENDPOINT_URL="<S3-compatible endpoint URL>"
$env:S3_REGION="<region or auto>"
$env:S3_ACCESS_KEY_ID="<storage access key>"
$env:S3_SECRET_ACCESS_KEY="<storage secret key>"
$env:S3_PREFIX="gymassistant"
$env:S3_ADDRESSING_STYLE="virtual"

.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "D:\Dreamz Fitness\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --push-members `
  --upload-files
```
