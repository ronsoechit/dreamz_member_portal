param(
  [Parameter(Mandatory = $true)]
  [string]$RollbackPath,
  [switch]$DoNotStart
)

$ErrorActionPreference = "Stop"
$rollbackRoot = (Resolve-Path -LiteralPath (Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunnerRollback")).Path
$resolvedRollback = (Resolve-Path -LiteralPath $RollbackPath).Path
$prefix = $rollbackRoot.TrimEnd("\") + "\"
if (-not $resolvedRollback.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "RollbackPath must be inside the fixed RonLaptopPaymentRunnerRollback directory."
}
$rollbackName = [IO.Path]::GetFileName($resolvedRollback.TrimEnd("\"))
if (-not $rollbackName.StartsWith("uninstalled-", [StringComparison]::OrdinalIgnoreCase)) {
  throw "Only a complete uninstalled-* snapshot can be restored. Pre-upgrade snapshots may contain stale safety receipts and are intentionally refused."
}

$savedInstall = Join-Path $resolvedRollback "install"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunner"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Ron Laptop Payment Runner.lnk"

if (-not (Test-Path -LiteralPath $savedInstall -PathType Container)) {
  throw "The rollback package does not contain an installation."
}
if (Test-Path -LiteralPath $installRoot) {
  throw "A runner is already installed. Uninstall it before restoring."
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
  $healthOutput = & $pythonPath -m ron_laptop_payment_runner.runner `
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
  $startInstalled = Join-Path $installRoot "scripts\Start-RonLaptopPaymentRunner.ps1"
  if (Test-Path -LiteralPath $startInstalled) {
    $powershellPath = (Get-Process -Id $PID).Path
    $startArguments = '-NoLogo -NoProfile -NonInteractive -File "' + $startInstalled + '" -ClearStopRequest'
    Start-Process -FilePath $powershellPath `
      -ArgumentList $startArguments `
      -WindowStyle Hidden
  }
}

Write-Host "Restored Ron laptop payment runner from: $resolvedRollback"
