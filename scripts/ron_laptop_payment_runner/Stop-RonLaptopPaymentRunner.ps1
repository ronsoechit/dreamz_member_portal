param(
  [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunner"
$stateRoot = Join-Path $installRoot "state"
$statusFile = Join-Path $stateRoot "status.json"
$stopRequest = Join-Path $stateRoot "stop.request"

if (-not (Test-Path -LiteralPath $installRoot)) {
  Write-Host "Ron laptop payment runner is not installed."
  exit 0
}

New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
[IO.File]::WriteAllText($stopRequest, ([DateTimeOffset]::UtcNow.ToString("o")))

$runnerPid = 0
if (Test-Path -LiteralPath $statusFile) {
  try {
    $status = Get-Content -Raw -LiteralPath $statusFile | ConvertFrom-Json
    if ($status.state -notin @("stopped", "configuration_error")) {
      $runnerPid = [int]$status.pid
    }
  }
  catch {
    $runnerPid = 0
  }
}

if ($runnerPid -gt 0) {
  $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
  while ([DateTime]::UtcNow -lt $deadline) {
    if (-not (Get-Process -Id $runnerPid -ErrorAction SilentlyContinue)) {
      Write-Host "Ron laptop payment runner stopped cooperatively."
      exit 0
    }
    Start-Sleep -Milliseconds 500
  }
  throw "The runner did not stop within the timeout. Nothing was killed; retry after the current guarded UI action finishes."
}

Write-Host "Stop request written. No active runner PID was recorded."
