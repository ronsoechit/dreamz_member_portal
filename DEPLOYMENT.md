# Dreamz Member Portal Deployment

## Railway staging setup

Create a separate staging project first. A branch or environment name alone
does not provide isolation. Staging must use its own PostgreSQL service, private
bucket, Railway hostname and staging-only secrets. Do not copy production
variables into staging.

The staging database starts empty. Never populate it from a Gym Assistant
production export. Keep outbound email in log-only mode and all production
integrations disabled.

### Services

1. Web service from this GitHub repository.
2. Postgres service.
3. S3-compatible bucket/object storage for member PDFs and photos.

### Required web environment variables

Set these on the Railway web service:

```text
SECRET_KEY=<long random value>
DATABASE_URL=${{Postgres.DATABASE_URL}}
STAFF_ADMIN_USERNAME=staging_admin
STAFF_ADMIN_PASSWORD=<strong temporary password>
STAFF_MANAGER_USERNAME=staging_manager
STAFF_MANAGER_PASSWORD=<strong temporary password>
STAFF_ADMIN_EMAIL=staging-no-reply@dreamzfitness.invalid
STAFF_TOKEN=<long random staff token>
SYNC_API_TOKEN=<long random sync token>
FEP_API_TOKEN=<long random FEP token>
SIGNUP_PORTAL_INTEGRATION_TOKEN=<shared random token of at least 32 characters>
PORTAL_EXISTING_MEMBER_REVERIFICATION_ENABLED=false
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS=
PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS=
MEMBER_PORTAL_PUBLIC_URL=https://<staging-service>.up.railway.app
WORDPRESS_SCHEDULE_WEBHOOK_URL=
WORDPRESS_SCHEDULE_WEBHOOK_TOKEN=
SYNC_STALE_AFTER_MINUTES=30
SESSION_COOKIE_SECURE=true
EMAIL_DELIVERY_MODE=log
COACH_AI_MODE=fallback
WHATSAPP_LOGIN_ENABLED=false
INVOICE_GA_PILOT_ENABLED=false
INVOICE_ISSUING_ENABLED=false
INVOICE_NUMBER_SERIES_APPROVED=false
RUNTIME_SCHEMA_STRICT=true
DIRECT_DEBIT_DAY=28
STORAGE_BACKEND=s3
S3_BUCKET=${{memberportalstagingfiles.BUCKET}}
S3_ENDPOINT_URL=${{memberportalstagingfiles.ENDPOINT}}
S3_REGION=${{memberportalstagingfiles.REGION}}
S3_ACCESS_KEY_ID=${{memberportalstagingfiles.ACCESS_KEY_ID}}
S3_SECRET_ACCESS_KEY=${{memberportalstagingfiles.SECRET_ACCESS_KEY}}
S3_PREFIX=staging/gymassistant
S3_ADDRESSING_STYLE=virtual
```

Generate staging secrets directly in Railway or pass them through the Railway
CLI's stdin option. Never put secret values in shell arguments, source files,
chat, logs or local `.env` files.

### First staging test

1. Deploy the web service against the empty staging Postgres and bucket.
2. Verify `/healthz` returns process status `ok`.
3. Verify `/readyz` returns database status `ok`, runtime schema ready and the
   expected release SHA.
4. Audit the target PostgreSQL catalog against the active-workout constraint,
   index and foreign-key contract.
5. Verify `/login` and `/staff/login` render over HTTPS.
6. Use only a clearly synthetic member fixture for authenticated flow testing.
   If no reviewed fixture loader exists yet, leave staging member data empty.
7. Confirm the bucket remains private and empty until an explicit synthetic
   upload smoke is performed.
8. Record the deployment ID and release SHA before any later rollout.

Do not run the sync agent, FEP writer, signup integration, WordPress schedule
publication, SMTP delivery or a Gym Assistant file upload against isolated
staging.

### Staging rollback

For a failed first deployment, stop the staging web deployment and keep its
domain away from reviewers until corrected. Because the initial database and
bucket contain no member data, recreate them rather than attempting a partial
schema downgrade.

Before any later schema-changing staging release that contains synthetic test
data, create and verify a Railway volume backup. Record the previous deployment
ID and exact Git SHA so the application can be rolled back independently of
the database. Never use these staging instructions as production rollback
authority.

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
The exact member number must be present in
`PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_MEMBER_IDS` **and** the exact
verified email address must be present in
`PORTAL_EXISTING_MEMBER_REVERIFICATION_PILOT_EMAILS`. Empty or one-sided
allowlists deny every pilot request. Add only the single controlled pilot
identity during a reviewed rollout, and never commit real member numbers or
email addresses to this file.

### Sync monitoring

The portal shows a warning on `/staff/sync` when no completed GymAssistant sync
has been received for `SYNC_STALE_AFTER_MINUTES` minutes. The sync agent itself
performs one scan and push per run. Do not create or change its frontdesk
Scheduled Task from this guide. First verify the live task, active data root,
installer, impact and rollback against the shared Dreamz remote-operations
runbook. Any frontdesk task change requires an exact owner-approved scope and a
safe maintenance window.

### FEP payment write-back

FEP payment updates are accepted by the portal at `/api/fep/payment-update` with `FEP_API_TOKEN`. The portal validates and queues the update, but does not mark the member paid until the local GymAssistant sync confirms the changed payment fields.

Linked GymAssistant memberships are excluded from automatic write-back. If a member is a dependent of another member, or has dependents linked to the account, the portal returns `409` for manual review. The guarded UI writer also cancels GymAssistant's dependent-member prompt instead of choosing a responsible member automatically.

The frontdesk sync agent can listen for an explicit FEP command before pushing normal member sync data. This is the preferred production mode: normal syncs do not book payments unless FEP has first sent a `payment-process-request` for that target agent.

```powershell
$env:SYNC_API_TOKEN="<same token as Railway>"
$env:FEP_PAYMENT_WRITER_COMMAND="<local command that writes one payment update to GymAssistant>"
.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "C:\Gym Assistant 2.6" `
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
  --source-root "C:\Gym Assistant 2.6" `
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
5. Set `INVOICE_GA_PILOT_ENABLED=true` and configure exactly one controlled
   member in `INVOICE_GA_PILOT_MEMBER_IDS=<pilot-member-id>` on the portal.
   Configure the same value for the frontdesk sync-agent task. Do not add other
   members during the first pilot. Point the agent at the actual backup
   directory with `GYM_ASSISTANT_BACKUP_ROOT=<backup-directory>` or pass
   `--backup-root "<backup-directory>"`. The agent uses the newest `.gbu`
   containing both `Members.btx` and `Journal.jtx`. Create a fresh `.gbu`
   after the controlled payment; the portal checks the actual snapshot
   timestamp and hashes and blocks snapshots with relevant parse errors.
6. Keep `INVOICE_ISSUING_ENABLED=false`, run a fresh frontdesk sync, and verify
   the member, membership amount, and service period against Gym Assistant.
   Any open account balance must not appear as an invoice line.
7. Use **Check membership events** to prepare the draft. A draft does not get
   an invoice number and is not visible to the member.
8. After the source, number series, legal fields, inclusive ABB treatment, and
   S3 write/read/download have been checked, set
   `INVOICE_ISSUING_ENABLED=true`. An admin must complete all three standard
   review checkboxes before the one pilot invoice can be issued. If the
   Gym Assistant period differs from the catalog billing interval, an
   additional explicit period confirmation is required.
9. Sign in as the pilot member and verify `/account/invoices` and the private
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
  --source-root "C:\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --push-members `
  --upload-files
```
