param(
  [Parameter(Mandatory = $true)]
  [string]$RollbackPath,
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
if ([Environment]::MachineName -ine "DESKTOP-8KM7V7D") {
  throw "Restore refused: this runner is fixed to DESKTOP-8KM7V7D."
}
if ([Environment]::UserName -ine "ron") {
  throw "Restore refused: this runner is fixed to Windows user ron."
}

function Get-Sha256Hex {
  param([Parameter(Mandatory = $true)][string]$LiteralPath)

  return (
    Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256 -ErrorAction Stop
  ).Hash.ToUpperInvariant()
}

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$validator = Join-Path $PSScriptRoot "Test-DreamzReservePaymentRunnerRelease.ps1"
if (-not $PythonPath) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if (
    -not $pythonCommand -or
    [string]$pythonCommand.Source -match "(?i)\\WindowsApps\\"
  ) {
    throw "Restore requires an exact reviewed Python path; the Microsoft Store alias is not accepted."
  }
  $PythonPath = $pythonCommand.Source
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

$validationRaw = & powershell.exe `
  -NoLogo `
  -NoProfile `
  -ExecutionPolicy Bypass `
  -File $validator `
  -PythonPath $PythonPath
if ($LASTEXITCODE -ne 0) {
  throw "Restore refused because the current offline release validation failed."
}
$validation = try {
  ($validationRaw -join "`n") | ConvertFrom-Json
}
catch {
  throw "Restore refused because release validation returned invalid output."
}
if (-not [bool]$validation.valid) {
  throw "Restore refused because the current offline release is not valid."
}

$releaseManifest = Get-Content `
  -Raw `
  -LiteralPath (Join-Path $packageRoot "release-manifest.json") |
  ConvertFrom-Json
$rollbackRootPath = Join-Path `
  $env:LOCALAPPDATA `
  "Dreamz\ReserveLaptopPaymentRunnerRollback"
$rollbackRoot = (Resolve-Path -LiteralPath $rollbackRootPath).Path
$resolvedRollback = (Resolve-Path -LiteralPath $RollbackPath).Path
$prefix = $rollbackRoot.TrimEnd("\") + "\"
if (-not $resolvedRollback.StartsWith(
  $prefix,
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw "RollbackPath must be inside the fixed ReserveLaptopPaymentRunnerRollback directory."
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

foreach ($historyItem in @(
  Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction Stop
)) {
  if ($historyItem.FullName.Equals(
    $resolvedRollback,
    [StringComparison]::OrdinalIgnoreCase
  )) {
    continue
  }
  if (-not $historyItem.PSIsContainer) {
    throw "Restore refused because unexpected runner rollback history exists."
  }
  if ($historyItem.Name -notmatch "^(?:upgrade|uninstalled)-(?<Stamp>[0-9]{8}-[0-9]{6})$") {
    throw "Restore refused because unrecognized runner rollback history exists."
  }
  $historyTimestamp = [DateTime]::ParseExact(
    $Matches.Stamp,
    "yyyyMMdd-HHmmss",
    [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::None
  )
  if ($historyTimestamp -ge $selectedTimestamp) {
    throw "Restore refused because a newer or same-generation runner snapshot exists."
  }
}

$savedInstall = Join-Path $resolvedRollback "install"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\ReserveLaptopPaymentRunner"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Reserve Laptop Payment Runner.lnk"
if (-not (Test-Path -LiteralPath $savedInstall -PathType Container)) {
  throw "The rollback package does not contain an installation."
}
if (Test-Path -LiteralPath $installRoot) {
  throw "A runner is already installed. Uninstall it before restoring."
}
if (Test-Path -LiteralPath $startupLink) {
  throw "Restore refused because a runner Startup shortcut already exists."
}

$savedManifestPath = Join-Path $savedInstall "installed-manifest.json"
if (-not (Test-Path -LiteralPath $savedManifestPath -PathType Leaf)) {
  throw "Restore refused because the installed manifest is missing."
}
$savedManifest = try {
  Get-Content -Raw -LiteralPath $savedManifestPath | ConvertFrom-Json
}
catch {
  throw "Restore refused because the installed manifest is invalid."
}
if (
  [int]$savedManifest.schema -ne 1 -or
  [string]$savedManifest.release -cne "dreamz_reserve_payment_runner" -or
  [string]$savedManifest.version -cne "1.0.0" -or
  [string]$savedManifest.source_commit -cne [string]$releaseManifest.source_commit -or
  [string]$savedManifest.expected_computer -cne "DESKTOP-8KM7V7D" -or
  [string]$savedManifest.expected_windows_user -cne "ron" -or
  [string]$savedManifest.agent_id -cne "reserve_8km7v7d" -or
  [string]$savedManifest.source_root -cne "\\DREAMZ-FRNTDSK\Gym Assistant 2.6"
) {
  throw "Restore refused because the installed manifest target is not trusted."
}

$expectedInstalledFiles = @(
  "reserve_laptop_payment_runner\README.md"
  "reserve_laptop_payment_runner\__init__.py"
  "reserve_laptop_payment_runner\runner.py"
  "gymassistant_payment_writer.py"
  "scripts\Start-ReserveLaptopPaymentRunner.ps1"
  "scripts\Stop-ReserveLaptopPaymentRunner.ps1"
  "scripts\Test-ReserveLaptopPaymentRunnerPreflight.ps1"
) | Sort-Object
$savedHashMap = [ordered]@{}
foreach ($property in $savedManifest.file_hashes.PSObject.Properties) {
  $savedHashMap[$property.Name] = (
    [string]$property.Value
  ).ToUpperInvariant()
}
if (@(
  Compare-Object $expectedInstalledFiles (@($savedHashMap.Keys) | Sort-Object)
).Count -ne 0) {
  throw "Restore refused because the installed manifest file scope differs."
}
foreach ($relativePath in $expectedInstalledFiles) {
  $candidate = Join-Path $savedInstall $relativePath
  if (
    -not (Test-Path -LiteralPath $candidate -PathType Leaf) -or
    (Get-Sha256Hex -LiteralPath $candidate) -cne $savedHashMap[$relativePath]
  ) {
    throw "Restore refused because a saved runtime hash differs."
  }
}

$runtimeFile = Join-Path $savedInstall "runtime.json"
$ledgerFile = Join-Path $savedInstall "state\payment-receipts.json"
if (
  -not (Test-Path -LiteralPath $runtimeFile -PathType Leaf) -or
  -not (Test-Path -LiteralPath $ledgerFile -PathType Leaf)
) {
  throw "Restore refused because runtime or receipt ledger state is missing."
}
Push-Location $packageRoot
try {
  $healthOutput = & $PythonPath `
    -m reserve_laptop_payment_runner.runner `
    --config $runtimeFile `
    --health-check 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "Restore refused because the receipt ledger failed the trusted health check."
  }
}
finally {
  Pop-Location
}

Move-Item -LiteralPath $savedInstall -Destination $installRoot
[IO.File]::WriteAllText(
  (Join-Path $installRoot "python-path.txt"),
  $PythonPath
)

Write-Host "Restored reserve laptop payment runner from: $resolvedRollback"
Write-Host "No Startup shortcut was created and the runner was not started."
