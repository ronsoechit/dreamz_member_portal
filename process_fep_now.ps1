param(
  [int]$Minutes = 30,
  [int]$IntervalSeconds = 10,
  [int]$Limit = 10,
  [int]$MaxTotal = 10
)

$ErrorActionPreference = "Stop"

$taskName = "Dreamz Portal Sync"
$syncHome = "C:\DreamzPortalSync"
$root = Join-Path $syncHome "dreamz_member_portal-codex-railway-staging"
$python = Join-Path $root ".venv\Scripts\python.exe"
$manifest = Join-Path $syncHome "sync_manifest.json"
$logDir = Join-Path $syncHome "logs"
$portalUrl = "https://dreamzmemberportal-production.up.railway.app"
$bucket = "dreamz-member-files-ku5q4"
$writerCommand = '.\.venv\Scripts\python.exe gymassistant_payment_writer.py --apply --foreground-ui --timeout 30'
$mutexName = "Global\DreamzPortalSync"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$sessionLog = Join-Path $logDir ("fep-now-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-SessionLog {
  param([string]$Message)
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Write-Host $line
  $line | Out-File -FilePath $sessionLog -Append -Encoding utf8
}

function Get-PendingCount {
  $response = Invoke-RestMethod `
    -Uri "$portalUrl/api/sync/fep-payment-updates?limit=$Limit" `
    -Headers @{ "X-Sync-Token" = $token }
  return @($response.updates).Count
}

if (-not (Test-Path -LiteralPath $python)) {
  throw "Python venv not found: $python"
}

$token = [Environment]::GetEnvironmentVariable("SYNC_API_TOKEN", "Machine")
if (-not $token) {
  $token = [Environment]::GetEnvironmentVariable("SYNC_API_TOKEN", "User")
}
if (-not $token) {
  throw "SYNC_API_TOKEN is not set for this Windows user or machine."
}

$mutex = New-Object System.Threading.Mutex($false, $mutexName)
$hasLock = $mutex.WaitOne(0)
if (-not $hasLock) {
  throw "Another Dreamz Portal Sync run is already active. Wait for it to finish, then run this again."
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$taskWasEnabled = $false

try {
  if ($task -and $task.State -ne "Disabled") {
    $taskWasEnabled = $true
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    Write-SessionLog "Temporarily disabled scheduled task '$taskName' during Process FEP Now session."
  }

  Set-Location $root
  $deadline = (Get-Date).AddMinutes($Minutes)
  $processedBudget = 0

  Write-SessionLog "Process FEP Now started. minutes=$Minutes interval=$IntervalSeconds limit=$Limit max_total=$MaxTotal"
  Write-SessionLog "Log file: $sessionLog"

  while ((Get-Date) -lt $deadline -and $processedBudget -lt $MaxTotal) {
    $pending = Get-PendingCount
    Write-SessionLog "Pending FEP payments: $pending"

    if ($pending -le 0) {
      Start-Sleep -Seconds $IntervalSeconds
      continue
    }

    $remaining = [Math]::Max(1, $MaxTotal - $processedBudget)
    $runLimit = [Math]::Min($Limit, [Math]::Min($pending, $remaining))
    Write-SessionLog "Processing up to $runLimit payment(s) now."

    & $python "sync_agent.py" `
      --source-root "C:\Gym Assistant 2.6" `
      --manifest $manifest `
      --portal-url $portalUrl `
      --sync-token $token `
      --process-fep-payments `
      --fep-payment-writer $writerCommand `
      --fep-payment-limit $runLimit `
      --push-members `
      --upload-files `
      --upload-via-portal `
      --upload-changed-only `
      --storage-bucket $bucket `
      --upload-workers 8 `
      --write-manifest 2>&1 | Tee-Object -FilePath $sessionLog -Append

    if ($LASTEXITCODE -ne 0) {
      throw "sync_agent.py failed with exit code $LASTEXITCODE. See $sessionLog"
    }

    $processedBudget += $runLimit
    Write-SessionLog "Processed budget used: $processedBudget / $MaxTotal"
  }

  Write-SessionLog "Process FEP Now finished."
}
finally {
  if ($taskWasEnabled) {
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    Write-Host "Re-enabled scheduled task '$taskName'."
  }
  if ($hasLock) {
    $mutex.ReleaseMutex() | Out-Null
  }
  $mutex.Dispose()
}
