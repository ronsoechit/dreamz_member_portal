#Requires -Version 5.1

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$RollbackPath,

  [string]$PythonPath = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging\.venv\Scripts\python.exe"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedComputer = "DREAMZ-FRNTDSK"
$expectedUser = "Dreamz Fitness"
$expectedSourceRoot = "C:\Gym Assistant 2.6"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\FrontdeskPaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\FrontdeskPaymentRunnerRollback"
$runnerMutexName = "Local\DreamzFrontdeskPaymentManualOnce"
$lifecycleMutexName = "Local\DreamzFrontdeskPaymentCodeChange"
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
$legacySourceCommit = "f2678f6699d7e36c85946116461817131c7af525"
$legacyRuntimeHashes = [ordered]@{
  "sync_agent.py" = "4C7B9DA7B5CC651A727D0AA04C9FABDBA3AB05329461C328986FE7461D1D42D7"
  "gymassistant_payment_writer.py" = "13E8B3FFDC35C9675DDB3427EC1A35FB839F5060CDF8792A9589FF311F695201"
  "process_fep_now.ps1" = "64BF577872CF33FBC63D6CEF09BFC44BE0A4636B693C458A8D8CCA4799D9A3EB"
  "ga_import.py" = "14F82F2E8E4AE684659A8F400EABFADF4B32CB554D2314F8FA391E53645D788F"
  "ga_journal.py" = "5AF7346196AE785AED6C0BE25937F25ACB7DE6B8A8A3356C1D56D32CF1388712"
  "ga_documents.py" = "57A9F1D814CA4C8B70CE803AD13E6B02D4560077FD9491B204D58C6CC35858EC"
  "storage_backend.py" = "8E77D83176D06A37D1A7EE462A26F24131D491BF28846FE42BCBC1343B957694"
  "frontdesk_payment_runner\__init__.py" = "64B37E6A63625504B5529C497AB69BA6A91AE2AE33FBDB294AF20E1E11C125AC"
  "frontdesk_payment_runner\preflight.py" = "A60364D19AB937CFB63B49B2270D525046166DB2CAEE20015C98362547CA0685"
  "frontdesk_payment_runner\runner.py" = "95E38FF552B5CA07C034D3D58D68C39F6DE701EB5FEFA7796E8EC5E791901069"
  "frontdesk_payment_runner\README.md" = "04A0080DABC193417C3593674F6956991C7B61C1A992BE8E52E54D607A24EC20"
}

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

if ([Environment]::MachineName -ine $expectedComputer) {
  throw "Restore is restricted to DREAMZ-FRNTDSK."
}
$identityName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUser = ($identityName -split "\\")[-1]
if ($currentUser -ine $expectedUser) {
  throw "Restore is restricted to the documented Dreamz Fitness Windows context."
}
if (-not [Environment]::UserInteractive) {
  throw "Restore from the interactive Frontdesk Windows session."
}

if (-not (Test-Path -LiteralPath $releaseManifestPath -PathType Leaf)) {
  throw "Trusted release manifest is missing. Restore did not start."
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
  throw "Trusted release manifest is invalid. Restore did not start."
}
$releaseKeys = @($releaseHashes.Keys) | Sort-Object
if (
  [int]$releaseManifest.schema -ne 1 -or
  [string]$releaseManifest.release -ne "dreamz_frontdesk_payment_runner" -or
  [string]$releaseManifest.version -ne "1.0.0" -or
  [string]$releaseManifest.source_commit -notmatch "^[0-9a-f]{40}$" -or
  [string]$releaseManifest.eol_policy -ne "lf" -or
  [int]$releaseManifest.payload_file_count -ne $payloadAllowlist.Count -or
  @(Compare-Object $payloadAllowlist $releaseKeys).Count -ne 0
) {
  throw "Trusted release manifest scope is invalid. Restore did not start."
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
  throw "Trusted release contains unexpected files. Restore did not start."
}
foreach ($relativePath in $payloadAllowlist) {
  $candidate = Join-Path $packageRoot $relativePath.Replace("/", "\")
  if (
    (Get-Sha256Hex -LiteralPath $candidate) -ne
    $releaseHashes[$relativePath]
  ) {
    throw "Trusted release hash failed for $relativePath. Restore did not start."
  }
  if ([IO.File]::ReadAllBytes($candidate) -contains [byte]13) {
    throw "Trusted release EOL policy failed. Restore did not start."
  }
}

if (-not (Test-FullyQualifiedWindowsPath -Value $PythonPath)) {
  throw "PythonPath must be one exact absolute trusted path."
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonVersion = & $PythonPath -B -I -c (
  "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
)
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.10") {
  throw "Trusted Python 3.10 or newer is required."
}

$expectedRuntimeHashes = [ordered]@{}
foreach ($relativePath in $runtimeFiles) {
  $expectedRuntimeHashes[$relativePath.Replace("/", "\")] = (
    $releaseHashes[$relativePath]
  )
}

function Invoke-TrustedHealthCheck {
  param([string]$RuntimePath)

  $program = (
    "import pathlib,sys;" +
    "sys.path.insert(0,sys.argv[1]);" +
    "from frontdesk_payment_runner.runner import run_health_check;" +
    "raise SystemExit(run_health_check(pathlib.Path(sys.argv[2])))"
  )
  & $PythonPath -B -I -c $program $packageRoot $RuntimePath
  if ($LASTEXITCODE -ne 0) {
    throw "Trusted release code rejected the runtime or receipt ledger."
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

  if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
    throw "Restore refused because the current authoritative ledger is absent."
  }
  $currentLedger = Join-Path $installRoot "state\payment-receipts.json"
  $currentRuntime = Join-Path $installRoot "runtime.json"
  foreach ($required in @($currentLedger, $currentRuntime)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
      throw "Restore refused because current authoritative safety state is incomplete."
    }
  }
  Invoke-TrustedHealthCheck -RuntimePath $currentRuntime

  $resolvedRollbackRoot = (Resolve-Path -LiteralPath $rollbackRoot).Path
  $resolvedRollbackPath = (Resolve-Path -LiteralPath $RollbackPath).Path
  if (
    (Split-Path -Parent $resolvedRollbackPath) -ine $resolvedRollbackRoot -or
    (Split-Path -Leaf $resolvedRollbackPath) -notmatch "^upgrade-\d{8}-\d{6}-\d{3}$"
  ) {
    throw "RollbackPath must be one exact Frontdesk upgrade snapshot."
  }

  $snapshotInstall = Join-Path $resolvedRollbackPath "install"
  $snapshotManifest = Join-Path $snapshotInstall "installed-manifest.json"
  if (
    -not (Test-Path -LiteralPath $snapshotInstall -PathType Container) -or
    -not (Test-Path -LiteralPath $snapshotManifest -PathType Leaf)
  ) {
    throw "The selected rollback snapshot is incomplete."
  }
  try {
    $manifest = Get-Content -LiteralPath $snapshotManifest -Raw |
      ConvertFrom-Json
    $snapshotHashes = [ordered]@{}
    foreach ($property in $manifest.file_hashes.PSObject.Properties) {
      $snapshotHashes[$property.Name] = (
        [string]$property.Value
      ).ToUpperInvariant()
    }
  }
  catch {
    throw "Unsupported rollback release manifest: manifest cannot be parsed."
  }

  $snapshotHashKeysMatch = @(
    Compare-Object @($expectedRuntimeHashes.Keys) @($snapshotHashes.Keys)
  ).Count -eq 0
  $currentHashValuesMatch = $snapshotHashKeysMatch
  $legacyHashValuesMatch = $snapshotHashKeysMatch
  if ($snapshotHashKeysMatch) {
    foreach ($relativePath in $expectedRuntimeHashes.Keys) {
      if (
        $snapshotHashes[$relativePath] -ne
        $expectedRuntimeHashes[$relativePath]
      ) {
        $currentHashValuesMatch = $false
      }
      if (
        $snapshotHashes[$relativePath] -ne
        $legacyRuntimeHashes[$relativePath]
      ) {
        $legacyHashValuesMatch = $false
      }
    }
  }
  $commonManifestScopeValid = (
    [int]$manifest.schema -eq 2 -and
    [string]$manifest.release_version -eq "1.0.0" -and
    [string]$manifest.base_commit -eq "4fbfff17d0758282e106cbeab8368787e7c34ca8" -and
    [string]$manifest.expected_computer -eq $expectedComputer -and
    [string]$manifest.expected_windows_user -eq $expectedUser -and
    [string]$manifest.agent_id -eq "frontdesk_dreamz" -and
    [string]$manifest.source_root -eq $expectedSourceRoot -and
    [int]$manifest.receipt_ledger_schema -eq 2 -and
    [string]$manifest.receipt_ledger_policy -eq "current-install-carried-forward" -and
    $manifest.starts_during_install -eq $false -and
    $manifest.changes_scheduled_sync -eq $false
  )
  $manifestSourceCommit = ""
  if ($null -ne $manifest.PSObject.Properties["source_commit"]) {
    $manifestSourceCommit = [string]$manifest.source_commit
  }
  $isCurrentSnapshot = (
    $commonManifestScopeValid -and
    $manifestSourceCommit -eq [string]$releaseManifest.source_commit -and
    $currentHashValuesMatch
  )
  # The one pre-manifest-source_commit Frontdesk release is supported only
  # through its exact, externally pinned 11-file hash set. No snapshot code
  # is imported or executed during restore.
  $isPinnedLegacySnapshot = (
    $commonManifestScopeValid -and
    -not $manifestSourceCommit -and
    $legacyHashValuesMatch
  )
  if (-not $isCurrentSnapshot -and -not $isPinnedLegacySnapshot) {
    throw (
      "Unsupported rollback release manifest: only the current release " +
      "or pinned legacy source $legacySourceCommit is supported."
    )
  }
  $snapshotValidationHashes = if ($isPinnedLegacySnapshot) {
    $legacyRuntimeHashes
  }
  else {
    $expectedRuntimeHashes
  }

  foreach ($relativePath in $snapshotValidationHashes.Keys) {
    $candidate = Join-Path $snapshotInstall $relativePath
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
      throw "Rollback item is missing: $relativePath"
    }
    if (
      (Get-Sha256Hex -LiteralPath $candidate) -ne
      $snapshotValidationHashes[$relativePath]
    ) {
      throw "Rollback hash check failed for $relativePath."
    }
  }

  $snapshotRuntimePath = Join-Path $snapshotInstall "runtime.json"
  if (-not (Test-Path -LiteralPath $snapshotRuntimePath -PathType Leaf)) {
    throw "The selected rollback snapshot has no runtime configuration."
  }
  try {
    $snapshotRuntime = Get-Content -LiteralPath $snapshotRuntimePath -Raw |
      ConvertFrom-Json
  }
  catch {
    throw "The selected rollback runtime configuration is invalid."
  }
  if (
    [string]$snapshotRuntime.source_root -ne $expectedSourceRoot -or
    [int]$snapshotRuntime.idle_seconds -ne 300
  ) {
    throw "The selected rollback runtime has a different safety scope."
  }

  $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
  $stageRoot = Join-Path (
    Split-Path -Parent $installRoot
  ) (".FrontdeskPaymentRunner-restore-" + [guid]::NewGuid().ToString("N"))
  $currentBackup = Join-Path $rollbackRoot ("pre-restore-" + $stamp)
  $currentMoved = $false
  $restoredMoved = $false

  try {
    New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
    # A snapshot proves eligibility and supplies only its validated fixed
    # runtime config. Executable/static code always comes from this trusted
    # release package, so a pinned legacy snapshot can never reactivate its
    # weaker launcher or older lifecycle scripts.
    foreach ($relativePath in $expectedRuntimeHashes.Keys) {
      $destination = Join-Path $stageRoot $relativePath
      New-Item -ItemType Directory -Path (
        Split-Path -Parent $destination
      ) -Force | Out-Null
      Copy-Item -LiteralPath (
        Join-Path $packageRoot $relativePath
      ) -Destination $destination
      if (
        (Get-Sha256Hex -LiteralPath $destination) -ne
        $expectedRuntimeHashes[$relativePath]
      ) {
        throw "Trusted restore-stage hash failed for $relativePath."
      }
    }
    Copy-Item -LiteralPath $snapshotRuntimePath -Destination (
      Join-Path $stageRoot "runtime.json"
    )
    $restoredManifest = [ordered]@{
      schema = 2
      release_version = "1.0.0"
      base_commit = "4fbfff17d0758282e106cbeab8368787e7c34ca8"
      source_commit = [string]$releaseManifest.source_commit
      expected_computer = $expectedComputer
      expected_windows_user = $expectedUser
      agent_id = "frontdesk_dreamz"
      source_root = $expectedSourceRoot
      receipt_ledger_schema = 2
      receipt_ledger_policy = "current-install-carried-forward"
      starts_during_install = $false
      changes_scheduled_sync = $false
      file_hashes = $expectedRuntimeHashes
    }
    [IO.File]::WriteAllText(
      (Join-Path $stageRoot "installed-manifest.json"),
      ($restoredManifest | ConvertTo-Json -Depth 4)
    )

    # Current ledger is the only authoritative duplicate-prevention history.
    New-Item -ItemType Directory -Path (
      Join-Path $stageRoot "state"
    ) -Force | Out-Null
    Copy-Item -LiteralPath $currentLedger -Destination (
      Join-Path $stageRoot "state\payment-receipts.json"
    ) -Force
    [IO.File]::WriteAllText(
      (Join-Path $stageRoot "python-path.txt"),
      $PythonPath
    )

    Invoke-TrustedHealthCheck -RuntimePath (
      Join-Path $stageRoot "runtime.json"
    )

    New-Item -ItemType Directory -Path $currentBackup -Force | Out-Null
    Move-Item -LiteralPath $installRoot -Destination (
      Join-Path $currentBackup "install"
    )
    $currentMoved = $true

    Move-Item -LiteralPath $stageRoot -Destination $installRoot
    $restoredMoved = $true

    Invoke-TrustedHealthCheck -RuntimePath (
      Join-Path $installRoot "runtime.json"
    )

    Write-Host "Frontdesk payment-only runner recovered but not started."
    Write-Host "The current receipt ledger was carried forward."
    Write-Host "Only current trusted release code was installed and validated."
    if ($isPinnedLegacySnapshot) {
      Write-Host "Pinned legacy eligibility accepted; legacy code was not reactivated."
    }
    Write-Host "The normal Dreamz Portal Sync task was not changed."
    Write-Host "Replaced installation retained at: $currentBackup"
  }
  catch {
    if ($restoredMoved -and (Test-Path -LiteralPath $installRoot)) {
      Move-Item -LiteralPath $installRoot -Destination (
        Join-Path $rollbackRoot ("failed-restore-" + $stamp)
      )
    }
    elseif (Test-Path -LiteralPath $stageRoot) {
      $stageFullPath = [IO.Path]::GetFullPath($stageRoot)
      $stageParent = [IO.Path]::GetFullPath((Split-Path -Parent $installRoot))
      if (
        -not $stageFullPath.StartsWith(
          $stageParent.TrimEnd("\") + "\",
          [StringComparison]::OrdinalIgnoreCase
        ) -or
        (Split-Path -Leaf $stageFullPath) -notlike ".FrontdeskPaymentRunner-restore-*"
      ) {
        throw "Refusing cleanup outside the exact restore staging directory."
      }
      Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }

    $currentSnapshot = Join-Path $currentBackup "install"
    if ($currentMoved -and (Test-Path -LiteralPath $currentSnapshot)) {
      Move-Item -LiteralPath $currentSnapshot -Destination $installRoot
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
