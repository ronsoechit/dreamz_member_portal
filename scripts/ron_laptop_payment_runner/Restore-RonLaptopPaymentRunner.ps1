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
