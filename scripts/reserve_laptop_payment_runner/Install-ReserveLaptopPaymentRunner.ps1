param(
  [Security.SecureString]$SyncToken,
  [string]$PythonPath = "",
  [switch]$CreateStartupShortcut
)

$ErrorActionPreference = "Stop"
$expectedComputer = "DESKTOP-8KM7V7D"
$expectedUser = "ron"

function Get-Sha256Hex {
  param([Parameter(Mandatory = $true)][string]$LiteralPath)

  return (
    Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256 -ErrorAction Stop
  ).Hash.ToUpperInvariant()
}

if (-not [Environment]::UserInteractive) {
  throw "Install from the reserve laptop's interactive Windows session."
}
if ([Environment]::MachineName -ine $expectedComputer) {
  throw "Install refused: this package is fixed to DESKTOP-8KM7V7D."
}
if ([Environment]::UserName -ine $expectedUser) {
  throw "Install refused: this package is fixed to Windows user ron."
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
  throw "Install refused: use the standard-user ron session, not an elevated PowerShell."
}

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runnerSource = Join-Path $packageRoot "reserve_laptop_payment_runner"
$writerSource = Join-Path $packageRoot "gymassistant_payment_writer.py"
$startSource = Join-Path $PSScriptRoot "Start-ReserveLaptopPaymentRunner.ps1"
$stopSource = Join-Path $PSScriptRoot "Stop-ReserveLaptopPaymentRunner.ps1"
$preflightSource = Join-Path $PSScriptRoot "Test-ReserveLaptopPaymentRunnerPreflight.ps1"
$validatorSource = Join-Path $PSScriptRoot "Test-DreamzReservePaymentRunnerRelease.ps1"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\ReserveLaptopPaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\ReserveLaptopPaymentRunnerRollback"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Reserve Laptop Payment Runner.lnk"
$existingInstall = Test-Path -LiteralPath $installRoot
$existingStartupLink = Test-Path -LiteralPath $startupLink
$existingReceiptLedger = Join-Path $installRoot "state\payment-receipts.json"
$priorRunnerHistory = @()
if (Test-Path -LiteralPath $rollbackRoot) {
  $priorRunnerHistory = @(
    Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction Stop
  )
}

if ($existingInstall -and -not (Test-Path -LiteralPath $existingReceiptLedger -PathType Leaf)) {
  throw "Upgrade refused because the existing receipt ledger is missing. Restore or reconcile the previous safety state; never initialize an empty ledger over an existing installation."
}
if (-not $existingInstall -and ($existingStartupLink -or $priorRunnerHistory.Count -gt 0)) {
  throw "Fresh install refused because prior runner state exists. Restore the newest complete uninstall snapshot or perform an explicitly reviewed recovery; a new empty receipt ledger is only allowed on a demonstrable first install."
}

foreach ($required in @(
  $runnerSource,
  $writerSource,
  $startSource,
  $stopSource,
  $preflightSource,
  $validatorSource
)) {
  if (-not (Test-Path -LiteralPath $required)) {
    throw "Required package item is missing: $required"
  }
}

if (-not $PythonPath) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if (
    -not $pythonCommand -or
    [string]$pythonCommand.Source -match "(?i)\\WindowsApps\\"
  ) {
    throw "A real Python runtime was not found. Supply -PythonPath with an exact reviewed python.exe path; the Microsoft Store alias is not accepted."
  }
  $PythonPath = $pythonCommand.Source
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonVersion = & $PythonPath -I -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.10") {
  throw "Python 3.10 or newer is required."
}

$validationRaw = & powershell.exe `
  -NoLogo `
  -NoProfile `
  -ExecutionPolicy Bypass `
  -File $validatorSource `
  -PythonPath $PythonPath
if ($LASTEXITCODE -ne 0) {
  throw "Install refused because the offline release validation failed."
}
try {
  $validation = ($validationRaw -join "`n") | ConvertFrom-Json
}
catch {
  throw "Install refused because the release validator returned invalid output."
}
if (-not [bool]$validation.valid) {
  throw "Install refused because the offline release is not valid."
}
$releaseManifestPath = Join-Path $packageRoot "release-manifest.json"
$releaseManifest = Get-Content -Raw -LiteralPath $releaseManifestPath |
  ConvertFrom-Json
$installedFileSources = [ordered]@{
  "reserve_laptop_payment_runner\README.md" = "reserve_laptop_payment_runner/README.md"
  "reserve_laptop_payment_runner\__init__.py" = "reserve_laptop_payment_runner/__init__.py"
  "reserve_laptop_payment_runner\runner.py" = "reserve_laptop_payment_runner/runner.py"
  "gymassistant_payment_writer.py" = "gymassistant_payment_writer.py"
  "scripts\Start-ReserveLaptopPaymentRunner.ps1" = "scripts/reserve_laptop_payment_runner/Start-ReserveLaptopPaymentRunner.ps1"
  "scripts\Stop-ReserveLaptopPaymentRunner.ps1" = "scripts/reserve_laptop_payment_runner/Stop-ReserveLaptopPaymentRunner.ps1"
  "scripts\Test-ReserveLaptopPaymentRunnerPreflight.ps1" = "scripts/reserve_laptop_payment_runner/Test-ReserveLaptopPaymentRunnerPreflight.ps1"
}

New-Item -ItemType Directory -Force -Path $rollbackRoot | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupPath = Join-Path $rollbackRoot ("upgrade-" + $timestamp)
$existingBackedUp = $false
$startupLinkBackedUp = $false
$rollbackCreated = $false
$newInstallCreated = $false

try {
  if ((Test-Path -LiteralPath $installRoot) -or (Test-Path -LiteralPath $startupLink)) {
    New-Item -ItemType Directory -Force -Path $backupPath | Out-Null
    $rollbackCreated = $true
  }
  if (Test-Path -LiteralPath $startupLink) {
    Copy-Item -LiteralPath $startupLink -Destination (Join-Path $backupPath "startup-link.lnk")
    $startupLinkBackedUp = $true
  }
  if (Test-Path -LiteralPath $installRoot) {
    & $stopSource -TimeoutSeconds 30
    Move-Item -LiteralPath $installRoot -Destination (Join-Path $backupPath "install")
    $existingBackedUp = $true
  }

  New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
  $newInstallCreated = $true
  foreach ($folder in @("scripts", "secrets", "state", "logs")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $installRoot $folder) | Out-Null
  }

  $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
  $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
  $acl = [Security.AccessControl.DirectorySecurity]::new()
  $acl.SetAccessRuleProtection($true, $false)
  $inheritance = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
  $propagation = [Security.AccessControl.PropagationFlags]::None
  $allow = [Security.AccessControl.AccessControlType]::Allow
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($currentSid, "FullControl", $inheritance, $propagation, $allow))
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($systemSid, "FullControl", $inheritance, $propagation, $allow))
  Set-Acl -LiteralPath $installRoot -AclObject $acl

  Copy-Item -LiteralPath $runnerSource -Destination $installRoot -Recurse
  Copy-Item -LiteralPath $writerSource -Destination $installRoot
  Copy-Item -LiteralPath $startSource -Destination (Join-Path $installRoot "scripts")
  Copy-Item -LiteralPath $stopSource -Destination (Join-Path $installRoot "scripts")
  Copy-Item -LiteralPath $preflightSource -Destination (Join-Path $installRoot "scripts")
  [IO.File]::WriteAllText((Join-Path $installRoot "python-path.txt"), $PythonPath)

  $installedHashes = [ordered]@{}
  foreach ($installedRelative in $installedFileSources.Keys) {
    $releaseRelative = $installedFileSources[$installedRelative]
    $releaseHashProperty = $releaseManifest.files.PSObject.Properties |
      Where-Object { $_.Name -ceq $releaseRelative } |
      Select-Object -First 1
    if ($null -eq $releaseHashProperty) {
      throw "Install refused because a required runtime hash is absent."
    }
    $installedPath = Join-Path $installRoot $installedRelative
    $actualHash = Get-Sha256Hex -LiteralPath $installedPath
    $expectedHash = ([string]$releaseHashProperty.Value).ToUpperInvariant()
    if ($actualHash -cne $expectedHash) {
      throw "Install refused because a copied runtime hash differs."
    }
    $installedHashes[$installedRelative] = $expectedHash
  }
  $installedManifest = [ordered]@{
    schema = 1
    release = "dreamz_reserve_payment_runner"
    version = "1.0.0"
    source_commit = [string]$releaseManifest.source_commit
    expected_computer = $expectedComputer
    expected_windows_user = $expectedUser
    agent_id = "reserve_8km7v7d"
    source_root = "\\DREAMZ-FRNTDSK\Gym Assistant 2.6"
    file_hashes = $installedHashes
  }
  [IO.File]::WriteAllText(
    (Join-Path $installRoot "installed-manifest.json"),
    ($installedManifest | ConvertTo-Json -Depth 5)
  )

  $runtime = [ordered]@{
    poll_seconds = 5
    idle_seconds = 5
    command_ttl_seconds = 900
    request_timeout_seconds = 20
    writer_timeout_seconds = 30
  }
  [IO.File]::WriteAllText(
    (Join-Path $installRoot "runtime.json"),
    ($runtime | ConvertTo-Json)
  )

  $oldReceiptLedger = if ($existingBackedUp) {
    Join-Path $backupPath "install\state\payment-receipts.json"
  } else {
    ""
  }
  $newReceiptLedger = Join-Path $installRoot "state\payment-receipts.json"
  if ($oldReceiptLedger -and (Test-Path -LiteralPath $oldReceiptLedger)) {
    Copy-Item -LiteralPath $oldReceiptLedger -Destination $newReceiptLedger
  }
  else {
    # The preflight above proves there is no install, Startup link, or prior
    # rollback history. Only that demonstrable first-install state may create
    # a new empty duplicate-prevention ledger.
    [IO.File]::WriteAllText($newReceiptLedger, '{"schema":2,"receipts":{}}')
  }

  Push-Location $installRoot
  try {
    $healthOutput = & $PythonPath -m reserve_laptop_payment_runner.runner `
      --config (Join-Path $installRoot "runtime.json") `
      --health-check 2>&1
    if ($LASTEXITCODE -ne 0) {
      throw "The copied receipt ledger or runner configuration failed the non-mutating health check. Upgrade refused."
    }
  }
  finally {
    Pop-Location
  }

  $tokenFile = Join-Path $installRoot "secrets\sync-token.dpapi"
  $backupToken = if ($existingBackedUp) { Join-Path $backupPath "install\secrets\sync-token.dpapi" } else { "" }
  if (-not $SyncToken -and $backupToken -and (Test-Path -LiteralPath $backupToken)) {
    Copy-Item -LiteralPath $backupToken -Destination $tokenFile
  }
  else {
    if (-not $SyncToken) {
      $SyncToken = Read-Host "Member Portal sync token" -AsSecureString
    }
    $dpapiBlob = ConvertFrom-SecureString $SyncToken
    [IO.File]::WriteAllText($tokenFile, $dpapiBlob)
  }

  if ($CreateStartupShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startupLink)
    $powershellPath = (Get-Process -Id $PID).Path
    if (-not (Test-Path -LiteralPath $powershellPath -PathType Leaf)) {
      throw "Could not resolve the current PowerShell executable."
    }
    $shortcut.TargetPath = $powershellPath
    $startInstalled = Join-Path $installRoot "scripts\Start-ReserveLaptopPaymentRunner.ps1"
    $shortcut.Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $startInstalled + '" -ClearStopRequest'
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.Description = "Dreamz payment-only runner for reserve laptop 8KM7V7D"
    $shortcut.Save()
  }
  elseif (Test-Path -LiteralPath $startupLink) {
    Remove-Item -LiteralPath $startupLink -Force
  }

  Write-Host "Installed payment-only runner for reserve_8km7v7d."
  Write-Host "Install root: $installRoot"
  Write-Host "The runner was not started."
  if ($rollbackCreated) {
    Write-Host "Rollback retained at: $backupPath"
  }
}
catch {
  if ($newInstallCreated -and (Test-Path -LiteralPath $installRoot)) {
    Remove-Item -LiteralPath $installRoot -Recurse -Force
  }
  if ($existingBackedUp -and (Test-Path -LiteralPath (Join-Path $backupPath "install"))) {
    Move-Item -LiteralPath (Join-Path $backupPath "install") -Destination $installRoot
  }
  if (Test-Path -LiteralPath $startupLink) {
    Remove-Item -LiteralPath $startupLink -Force
  }
  $oldLink = Join-Path $backupPath "startup-link.lnk"
  if ($startupLinkBackedUp -and (Test-Path -LiteralPath $oldLink)) {
    Copy-Item -LiteralPath $oldLink -Destination $startupLink -Force
  }
  throw
}
