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
FEP_API_TOKEN=<long random FEP token>
SYNC_STALE_AFTER_MINUTES=30
SESSION_COOKIE_SECURE=true
EMAIL_DELIVERY_MODE=smtp
SMTP_HOST=<smtp host>
SMTP_PORT=465
SMTP_USER=<smtp username>
SMTP_PASS=<smtp password>
SMTP_FROM=<sender email>
SMTP_FROM_NAME=Dreamz Fitness
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

### Sync monitoring

The portal shows a warning on `/staff/sync` when no completed GymAssistant sync has been received for `SYNC_STALE_AFTER_MINUTES` minutes. The sync agent itself performs one scan and push per run, so automatic recovery after a power outage depends on the Windows Task Scheduler task or service on the frontdesk computer starting again after reboot. Configure that task to run on startup/login, repeat every few minutes, and run missed tasks as soon as possible.

### FEP payment write-back

FEP payment updates are accepted by the portal at `/api/fep/payment-update` with `FEP_API_TOKEN`. The portal validates and queues the update, but does not mark the member paid until the local GymAssistant sync confirms the changed payment fields.

Linked GymAssistant memberships are excluded from automatic write-back. If a member is a dependent of another member, or has dependents linked to the account, the portal returns `409` for manual review. The guarded UI writer also cancels GymAssistant's dependent-member prompt instead of choosing a responsible member automatically.

The frontdesk sync agent can process queued FEP updates before pushing normal member sync data:

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
