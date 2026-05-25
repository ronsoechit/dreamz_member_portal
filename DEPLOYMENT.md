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

.\.venv\Scripts\python.exe sync_agent.py `
  --source-root "D:\Dreamz Fitness\Gym Assistant 2.6" `
  --portal-url "https://<railway-app-url>" `
  --push-members `
  --upload-files
```
