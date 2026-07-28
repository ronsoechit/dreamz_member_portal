param(
  [Parameter(Mandatory = $true)]
  [string]$RollbackPath,
  [switch]$DoNotStart
)

$ErrorActionPreference = "Stop"
$rollbackRoot = (Resolve-Path -LiteralPath (Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunnerRollback")).Path
$resolvedRollback = (Resolve-Path -LiteralPath $RollbackPath).Path
$prefix = $rollbackRoot.TrimEnd("\") + "\"
if (-not $resolvedRollback.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "RollbackPath must be inside the fixed DreamzOfficePaymentRunnerRollback directory."
}
$rollbackName = [IO.Path]::GetFileName($resolvedRollback.TrimEnd("\"))
if ((Split-Path -Parent $resolvedRollback).TrimEnd("\") -ne $rollbackRoot.TrimEnd("\")) {
  throw "RollbackPath must be a direct child of the fixed rollback directory."
}
if ($rollbackName -notmatch "^uninstalled-(?<Stamp>[0-9]{8}-[0-9]{6})$") {
  throw "Only a complete uninstalled-* snapshot can be restored. Pre-upgrade snapshots may contain stale safety receipts and are intentionally refused."
}
$selectedTimestamp = [DateTime]::ParseExact(
  $Matches.Stamp,
  "yyyyMMdd-HHmmss",
  [Globalization.CultureInfo]::InvariantCulture,
  [Globalization.DateTimeStyles]::None
)

# A receipt ledger is monotonic safety evidence. Restoring an older snapshot
# could erase a writer_started/applied receipt and permit a duplicate UI write.
# Refuse rather than guessing whenever the selected uninstall is not provably
# the newest known runner operation.
foreach ($historyItem in @(Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction Stop)) {
  if ($historyItem.FullName.Equals($resolvedRollback, [StringComparison]::OrdinalIgnoreCase)) {
    continue
  }
  if (-not $historyItem.PSIsContainer) {
    throw "Restore refused because unexpected runner rollback history exists. Review it before restoring safety state."
  }
  if ($historyItem.Name -notmatch "^(?:upgrade|uninstalled)-(?<Stamp>[0-9]{8}-[0-9]{6})$") {
    throw "Restore refused because unrecognized runner rollback history exists. Review it before restoring safety state."
  }
  $historyTimestamp = [DateTime]::ParseExact(
    $Matches.Stamp,
    "yyyyMMdd-HHmmss",
    [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::None
  )
  if ($historyTimestamp -ge $selectedTimestamp) {
    throw "Restore refused because a newer or same-generation runner snapshot exists. Use the newest complete uninstall snapshot or reconcile the ledgers manually."
  }
}

$savedInstall = Join-Path $resolvedRollback "install"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunner"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Office Payment Runner.lnk"

if (-not (Test-Path -LiteralPath $savedInstall -PathType Container)) {
  throw "The rollback package does not contain an installation."
}
if (Test-Path -LiteralPath $installRoot) {
  throw "A runner is already installed. Uninstall it before restoring."
}
if (Test-Path -LiteralPath $startupLink) {
  throw "Restore refused because a runner Startup shortcut already exists. Review the newer or partial installation state first."
}

$pythonFile = Join-Path $savedInstall "python-path.txt"
$runtimeFile = Join-Path $savedInstall "runtime.json"
if (-not (Test-Path -LiteralPath $pythonFile -PathType Leaf)) {
  throw "The rollback package has no Python path."
}
$pythonPath = (Get-Content -Raw -LiteralPath $pythonFile).Trim()
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
  throw "The rollback package Python executable is unavailable."
}
Push-Location $savedInstall
try {
  $healthOutput = & $pythonPath -m dreamz_office_payment_runner.runner `
    --config $runtimeFile `
    --health-check 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "Rollback refused because its receipt ledger or runner version failed the non-mutating health check."
  }
}
finally {
  Pop-Location
}

Move-Item -LiteralPath $savedInstall -Destination $installRoot
$savedLink = Join-Path $resolvedRollback "startup-link.lnk"
if (Test-Path -LiteralPath $savedLink) {
  Copy-Item -LiteralPath $savedLink -Destination $startupLink -Force
}

if (-not $DoNotStart) {
  $startInstalled = Join-Path $installRoot "scripts\Start-DreamzOfficePaymentRunner.ps1"
  if (Test-Path -LiteralPath $startInstalled) {
    $powershellPath = (Get-Process -Id $PID).Path
    $startArguments = '-NoLogo -NoProfile -NonInteractive -File "' + $startInstalled + '" -ClearStopRequest'
    Start-Process -FilePath $powershellPath `
      -ArgumentList $startArguments `
      -WindowStyle Hidden
  }
}

Write-Host "Restored Dreamz Office payment runner from: $resolvedRollback"
