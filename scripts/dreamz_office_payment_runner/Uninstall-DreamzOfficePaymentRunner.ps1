param(
  [int]$StopTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunnerRollback"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Office Payment Runner.lnk"
$stopScript = Join-Path $installRoot "scripts\Stop-DreamzOfficePaymentRunner.ps1"

if (-not (Test-Path -LiteralPath $installRoot)) {
  Write-Host "Dreamz Office payment runner is not installed."
  exit 0
}

if (Test-Path -LiteralPath $stopScript) {
  & $stopScript -TimeoutSeconds $StopTimeoutSeconds
}

New-Item -ItemType Directory -Force -Path $rollbackRoot | Out-Null
$rollbackPath = Join-Path $rollbackRoot ("uninstalled-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $rollbackPath | Out-Null
if (Test-Path -LiteralPath $startupLink) {
  Copy-Item -LiteralPath $startupLink -Destination (Join-Path $rollbackPath "startup-link.lnk")
  Remove-Item -LiteralPath $startupLink -Force
}
Move-Item -LiteralPath $installRoot -Destination (Join-Path $rollbackPath "install")

Write-Host "Uninstalled without deleting the previous package or audit state."
Write-Host "Recoverable rollback path: $rollbackPath"
