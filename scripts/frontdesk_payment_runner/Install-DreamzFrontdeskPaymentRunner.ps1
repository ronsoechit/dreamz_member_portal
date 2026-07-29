#Requires -Version 5.1

[CmdletBinding()]
param(
  [string]$PythonPath = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging\.venv\Scripts\python.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedComputer = "DREAMZ-FRNTDSK"
$expectedUser = "Dreamz Fitness"
$releaseVersion = "1.0.0"
$baseCommit = "4fbfff17d0758282e106cbeab8368787e7c34ca8"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\FrontdeskPaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\FrontdeskPaymentRunnerRollback"
$runnerMutexName = "Local\DreamzFrontdeskPaymentManualOnce"
$lifecycleMutexName = "Local\DreamzFrontdeskPaymentCodeChange"
$expectedSourceRoot = "C:\Gym Assistant 2.6"

if ([Environment]::MachineName -ine $expectedComputer) {
  throw "Installation is restricted to DREAMZ-FRNTDSK."
}
$identityName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUser = ($identityName -split "\\")[-1]
if ($currentUser -ine $expectedUser) {
  throw "Installation is restricted to the documented Dreamz Fitness Windows context."
}
if (-not [Environment]::UserInteractive) {
  throw "Install from the interactive Frontdesk Windows session."
}

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$releaseManifestPath = Join-Path $packageRoot "release-manifest.json"
$payloadAllowlist = @(
  "frontdesk_payment_runner/README.md"
  "frontdesk_payment_runner/__init__.py"
  "frontdesk_payment_runner/preflight.py"
  "frontdesk_payment_runner/runner.py"
  "ga_documents.py"
  "ga_import.py"
  "ga_journal.py"
  "gymassistant_payment_writer.py"
  "process_fep_now.ps1"
  "scripts/frontdesk_payment_runner/Get-DreamzFrontdeskPaymentRunnerInventory.ps1"
  "scripts/frontdesk_payment_runner/Install-DreamzFrontdeskPaymentRunner.ps1"
  "scripts/frontdesk_payment_runner/README.md"
  "scripts/frontdesk_payment_runner/Restore-DreamzFrontdeskPaymentRunner.ps1"
  "scripts/frontdesk_payment_runner/START-INVENTARISATIE.cmd"
  "scripts/frontdesk_payment_runner/Test-DreamzFrontdeskPaymentRunnerRelease.ps1"
  "storage_backend.py"
  "sync_agent.py"
) | Sort-Object
$runtimeFiles = @(
  "sync_agent.py"
  "gymassistant_payment_writer.py"
  "process_fep_now.ps1"
  "ga_import.py"
  "ga_journal.py"
  "ga_documents.py"
  "storage_backend.py"
  "frontdesk_payment_runner/__init__.py"
  "frontdesk_payment_runner/preflight.py"
  "frontdesk_payment_runner/runner.py"
  "frontdesk_payment_runner/README.md"
)

function Test-FullyQualifiedWindowsPath {
  param([string]$Value)

  if ([string]::IsNullOrWhiteSpace($Value) -or $Value.IndexOf([char]0) -ge 0) {
    return $false
  }
  return (
    $Value -match "^[A-Za-z]:[\\/]" -or
    $Value -match "^[\\/]{2}[^\\/]+[\\/][^\\/]+(?:[\\/]|$)"
  )
}

function Get-Sha256Hex {
  param([string]$LiteralPath)

  $stream = [IO.File]::Open(
    $LiteralPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
  )
  $sha256 = [Security.Cryptography.SHA256]::Create()
  try {
    return (
      [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace("-", "")
    )
  }
  finally {
    $sha256.Dispose()
    $stream.Dispose()
  }
}

if (-not (Test-Path -LiteralPath $releaseManifestPath -PathType Leaf)) {
  throw "Release manifest is missing. Nothing was installed."
}
try {
  $releaseManifest = Get-Content -LiteralPath $releaseManifestPath -Raw |
    ConvertFrom-Json
  $releaseHashes = [ordered]@{}
  foreach ($property in $releaseManifest.files.PSObject.Properties) {
    $releaseHashes[$property.Name.Replace("\", "/")] = (
      [string]$property.Value
    ).ToUpperInvariant()
  }
}
catch {
  throw "Release manifest is invalid. Nothing was installed."
}
$releaseKeys = @($releaseHashes.Keys) | Sort-Object
if (
  [int]$releaseManifest.schema -ne 1 -or
  [string]$releaseManifest.release -ne "dreamz_frontdesk_payment_runner" -or
  [string]$releaseManifest.version -ne $releaseVersion -or
  [string]$releaseManifest.source_commit -notmatch "^[0-9a-f]{40}$" -or
  [string]$releaseManifest.eol_policy -ne "lf" -or
  [int]$releaseManifest.payload_file_count -ne $payloadAllowlist.Count -or
  @(Compare-Object $payloadAllowlist $releaseKeys).Count -ne 0
) {
  throw "Release manifest scope is invalid. Nothing was installed."
}

$packagePrefix = $packageRoot.TrimEnd("\") + "\"
$actualReleaseFiles = @(
  Get-ChildItem -LiteralPath $packageRoot -Recurse -File |
    ForEach-Object {
      if (
        -not $_.FullName.StartsWith(
          $packagePrefix,
          [StringComparison]::OrdinalIgnoreCase
        )
      ) {
        throw "Release file escaped package root."
      }
      $_.FullName.Substring($packagePrefix.Length).Replace("\", "/")
    }
) | Sort-Object
$archiveAllowlist = @($payloadAllowlist + "release-manifest.json") | Sort-Object
if (@(Compare-Object $archiveAllowlist $actualReleaseFiles).Count -ne 0) {
  throw "Release contains missing or unexpected files. Nothing was installed."
}

foreach ($relativePath in $payloadAllowlist) {
  $source = Join-Path $packageRoot $relativePath.Replace("/", "\")
  $actualHash = Get-Sha256Hex -LiteralPath $source
  if ($actualHash -ne $releaseHashes[$relativePath]) {
    throw "Release hash check failed for $relativePath. Nothing was installed."
  }
  if ([IO.File]::ReadAllBytes($source) -contains [byte]13) {
    throw "Release EOL policy failed for $relativePath. Nothing was installed."
  }
}

$expectedHashes = [ordered]@{
}
foreach ($relativePath in $runtimeFiles) {
  $expectedHashes[$relativePath.Replace("/", "\")] = $releaseHashes[$relativePath]
}

if (-not (Test-FullyQualifiedWindowsPath -Value $PythonPath)) {
  throw "PythonPath must be an exact absolute path."
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonVersion = & $PythonPath -B -I -c (
  "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
)
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.10") {
  throw "Python 3.10 or newer is required."
}

function Invoke-TrustedHealthCheck {
  param(
    [string]$ModuleRoot,
    [string]$RuntimePath
  )

  $program = (
    "import pathlib,sys;" +
    "sys.path.insert(0,sys.argv[1]);" +
    "from frontdesk_payment_runner.runner import run_health_check;" +
    "raise SystemExit(run_health_check(pathlib.Path(sys.argv[2])))"
  )
  & $PythonPath -B -I -c $program $ModuleRoot $RuntimePath
  if ($LASTEXITCODE -ne 0) {
    throw "The trusted release rejected the runtime or receipt ledger."
  }
}

$lifecycleMutex = [Threading.Mutex]::new($false, $lifecycleMutexName)
$runnerMutex = [Threading.Mutex]::new($false, $runnerMutexName)
$hasLifecycleLock = $false
$hasRunnerLock = $false
try {
  $hasLifecycleLock = $lifecycleMutex.WaitOne(0)
  if (-not $hasLifecycleLock) {
    throw "Another Frontdesk launcher or lifecycle operation is active."
  }
  $hasRunnerLock = $runnerMutex.WaitOne(0)
  if (-not $hasRunnerLock) {
    throw "A Frontdesk payment writer is active."
  }

  $existingInstall = Test-Path -LiteralPath $installRoot -PathType Container
  $existingLedger = Join-Path $installRoot "state\payment-receipts.json"
  $priorHistory = @()
  if (Test-Path -LiteralPath $rollbackRoot -PathType Container) {
    $priorHistory = @(
      Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction Stop
    )
  }
  if (
    $existingInstall -and
    -not (Test-Path -LiteralPath $existingLedger -PathType Leaf)
  ) {
    throw "Upgrade refused because the current duplicate-prevention ledger is missing."
  }
  if ($existingInstall) {
    $existingRuntimePath = Join-Path $installRoot "runtime.json"
    if (-not (Test-Path -LiteralPath $existingRuntimePath -PathType Leaf)) {
      throw "Upgrade refused because the current fixed runtime configuration is missing."
    }
    try {
      $existingRuntime = Get-Content -LiteralPath $existingRuntimePath -Raw |
        ConvertFrom-Json
      $existingRuntimeSource = [IO.Path]::GetFullPath(
        [string]$existingRuntime.source_root
      ).TrimEnd("\")
    }
    catch {
      throw "Upgrade refused because the current source root cannot be validated."
    }
    if (
      $existingRuntimeSource -ine $expectedSourceRoot -or
      [int]$existingRuntime.idle_seconds -ne 300
    ) {
      throw "Upgrade refused because the current runner has a different safety scope."
    }
  }
  if (-not $existingInstall -and $priorHistory.Count -gt 0) {
    throw "Fresh install refused because prior runner history exists. Reconcile the newest ledger before installing."
  }

  New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
  $stageRoot = Join-Path (
    Split-Path -Parent $installRoot
  ) (".FrontdeskPaymentRunner-stage-" + [guid]::NewGuid().ToString("N"))
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
  $backupPath = Join-Path $rollbackRoot ("upgrade-" + $stamp)
  $oldInstallMoved = $false
  $newInstallMoved = $false

  try {
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule(
      [Security.AccessControl.FileSystemAccessRule]::new(
        $currentSid,
        "FullControl",
        $inheritance,
        $propagation,
        $allow
      )
    )
    $acl.AddAccessRule(
      [Security.AccessControl.FileSystemAccessRule]::new(
        $systemSid,
        "FullControl",
        $inheritance,
        $propagation,
        $allow
      )
    )
    Set-Acl -LiteralPath $stageRoot -AclObject $acl

    foreach ($relativePath in $expectedHashes.Keys) {
      $destination = Join-Path $stageRoot $relativePath
      $destinationParent = Split-Path -Parent $destination
      New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
      Copy-Item -LiteralPath (
        Join-Path $packageRoot $relativePath
      ) -Destination $destination
      if (
        (Get-Sha256Hex -LiteralPath $destination) -ne
        $expectedHashes[$relativePath]
      ) {
        throw "Staged release hash failed for $relativePath."
      }
    }
    [IO.File]::WriteAllText(
      (Join-Path $stageRoot "python-path.txt"),
      $PythonPath
    )

    $runtime = [ordered]@{
      source_root = $expectedSourceRoot
      poll_seconds = 5
      idle_seconds = 300
      command_ttl_seconds = 900
      request_timeout_seconds = 20
      writer_timeout_seconds = 30
    }
    [IO.File]::WriteAllText(
      (Join-Path $stageRoot "runtime.json"),
      ($runtime | ConvertTo-Json)
    )

    $stageState = Join-Path $stageRoot "state"
    New-Item -ItemType Directory -Path $stageState -Force | Out-Null
    $stageLedger = Join-Path $stageState "payment-receipts.json"
    if ($existingInstall) {
      Copy-Item -LiteralPath $existingLedger -Destination $stageLedger
    }
    else {
      [IO.File]::WriteAllText(
        $stageLedger,
        '{"schema":2,"receipts":{}}'
      )
    }

    $manifest = [ordered]@{
      schema = 2
      release_version = $releaseVersion
      base_commit = $baseCommit
      source_commit = [string]$releaseManifest.source_commit
      expected_computer = $expectedComputer
      expected_windows_user = $expectedUser
      agent_id = "frontdesk_dreamz"
      source_root = $expectedSourceRoot
      receipt_ledger_schema = 2
      receipt_ledger_policy = "current-install-carried-forward"
      starts_during_install = $false
      changes_scheduled_sync = $false
      file_hashes = $expectedHashes
    }
    [IO.File]::WriteAllText(
      (Join-Path $stageRoot "installed-manifest.json"),
      ($manifest | ConvertTo-Json -Depth 4)
    )

    foreach ($pythonFile in @(
      "sync_agent.py",
      "gymassistant_payment_writer.py",
      "ga_import.py",
      "ga_journal.py",
      "ga_documents.py",
      "storage_backend.py",
      "frontdesk_payment_runner\preflight.py",
      "frontdesk_payment_runner\runner.py"
    )) {
      & $PythonPath -B -I -c (
        "import ast,pathlib,sys;" +
        "ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))"
      ) (Join-Path $stageRoot $pythonFile)
      if ($LASTEXITCODE -ne 0) {
        throw "Python syntax validation failed for $pythonFile."
      }
    }

    Invoke-TrustedHealthCheck `
      -ModuleRoot $stageRoot `
      -RuntimePath (Join-Path $stageRoot "runtime.json")

    $processScriptText = Get-Content -LiteralPath (
      Join-Path $stageRoot "process_fep_now.ps1"
    ) -Raw
    [void][scriptblock]::Create($processScriptText)

    if ($existingInstall) {
      New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
      Move-Item -LiteralPath $installRoot -Destination (
        Join-Path $backupPath "install"
      )
      $oldInstallMoved = $true
    }

    Move-Item -LiteralPath $stageRoot -Destination $installRoot
    $newInstallMoved = $true

    Invoke-TrustedHealthCheck `
      -ModuleRoot $installRoot `
      -RuntimePath (Join-Path $installRoot "runtime.json")

    Write-Host "Frontdesk payment-only runner installed but not started."
    Write-Host "Release version: $releaseVersion"
    Write-Host "Install root: $installRoot"
    if ($oldInstallMoved) {
      Write-Host "Rollback retained at: $backupPath"
    }
    Write-Host "The normal Dreamz Portal Sync task was not changed."
  }
  catch {
    if ($newInstallMoved -and (Test-Path -LiteralPath $installRoot)) {
      $failedPath = Join-Path $rollbackRoot ("failed-install-" + $stamp)
      Move-Item -LiteralPath $installRoot -Destination $failedPath
    }
    elseif (Test-Path -LiteralPath $stageRoot) {
      $stageFullPath = [IO.Path]::GetFullPath($stageRoot)
      $stageParent = [IO.Path]::GetFullPath((Split-Path -Parent $installRoot))
      if (
        -not $stageFullPath.StartsWith(
          $stageParent.TrimEnd("\") + "\",
          [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Split-Path -Leaf $stageFullPath) -notlike ".FrontdeskPaymentRunner-stage-*"
      ) {
        throw "Refusing cleanup outside the exact Frontdesk staging directory."
      }
      Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }

    $oldInstall = Join-Path $backupPath "install"
    if ($oldInstallMoved -and (Test-Path -LiteralPath $oldInstall)) {
      Move-Item -LiteralPath $oldInstall -Destination $installRoot
    }
    throw
  }
}
finally {
  if ($hasRunnerLock) {
    $runnerMutex.ReleaseMutex() | Out-Null
  }
  if ($hasLifecycleLock) {
    $lifecycleMutex.ReleaseMutex() | Out-Null
  }
  $runnerMutex.Dispose()
  $lifecycleMutex.Dispose()
}
