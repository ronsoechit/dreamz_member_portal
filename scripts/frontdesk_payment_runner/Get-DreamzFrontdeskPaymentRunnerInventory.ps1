#Requires -Version 5.1

[CmdletBinding()]
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Dreamz\FrontdeskPaymentRunner"),
  [string]$TrustedPythonPath = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging\.venv\Scripts\python.exe",
  [switch]$SkipDesktopPreflight
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedComputer = "DREAMZ-FRNTDSK"
$expectedUser = "Dreamz Fitness"
$runnerMutexName = "Local\DreamzFrontdeskPaymentManualOnce"
$lifecycleMutexName = "Local\DreamzFrontdeskPaymentCodeChange"
$expectedHashes = [ordered]@{
  "sync_agent.py" = "8E4E1B093207EDA39377D09498E917B51CAC9D0987B1FD48BC8B908A0459AD67"
  "gymassistant_payment_writer.py" = "49224368B4214310492E888C4431F7EABD187CC8C3D16E3AFF8A0677B8C932EA"
  "process_fep_now.ps1" = "6A8004A3742C7AB4DA4842318D4A78AC15FB369F15C15E711B5CAEDB7FCF65B6"
  "ga_import.py" = "A5E95581586CA8031D4BD0D7303C9D9915AA10D3531700E03DF5574D5E2428B6"
  "ga_journal.py" = "4A7B2A28F284373B32DB6FCF20343CC1B8C0DA3A89369A5B7F0A78DEA56B63FC"
  "ga_documents.py" = "5CF0FE7C0EEA9F2743820D4EE91A38889367F4FF102D7FD8D37205AFCD9FD54B"
  "storage_backend.py" = "B8DED1814132890C49A8B273B7F3AD45D2A7DF377CBFAE5AE9584A0FDF570EC5"
  "frontdesk_payment_runner\__init__.py" = "64B37E6A63625504B5529C497AB69BA6A91AE2AE33FBDB294AF20E1E11C125AC"
  "frontdesk_payment_runner\preflight.py" = "A60364D19AB937CFB63B49B2270D525046166DB2CAEE20015C98362547CA0685"
  "frontdesk_payment_runner\runner.py" = "95E38FF552B5CA07C034D3D58D68C39F6DE701EB5FEFA7796E8EC5E791901069"
  "frontdesk_payment_runner\README.md" = "58C2998FE12952E16858284CC8B57430A442B0EEE9878C615559727956CD36AC"
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

function Test-MutexExists {
  param([string]$Name)

  $handle = $null
  try {
    $handle = [Threading.Mutex]::OpenExisting($Name)
    return $true
  }
  catch [Threading.WaitHandleCannotBeOpenedException] {
    return $false
  }
  finally {
    if ($null -ne $handle) {
      $handle.Dispose()
    }
  }
}

$identityName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUser = ($identityName -split "\\")[-1]
$installExists = Test-Path -LiteralPath $InstallRoot -PathType Container

$fileChecks = @()
foreach ($relativePath in $expectedHashes.Keys) {
  $candidate = Join-Path $InstallRoot $relativePath
  $exists = Test-Path -LiteralPath $candidate -PathType Leaf
  $hashMatches = $false
  if ($exists) {
    $hashMatches = (
      (Get-Sha256Hex -LiteralPath $candidate) -eq
      $expectedHashes[$relativePath]
    )
  }
  $fileChecks += [ordered]@{
    file = $relativePath
    exists = $exists
    hash_matches = $hashMatches
  }
}
$allStaticFilesValid = (
  $installExists -and
  @($fileChecks | Where-Object { -not $_.exists -or -not $_.hash_matches }).Count -eq 0
)

$manifestPath = Join-Path $InstallRoot "installed-manifest.json"
$manifestValid = $false
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
  try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifestHashes = [ordered]@{}
    foreach ($property in $manifest.file_hashes.PSObject.Properties) {
      $manifestHashes[$property.Name] = (
        [string]$property.Value
      ).ToUpperInvariant()
    }
    $manifestHashKeysMatch = @(
      Compare-Object @($expectedHashes.Keys) @($manifestHashes.Keys)
    ).Count -eq 0
    $manifestHashValuesMatch = $manifestHashKeysMatch
    if ($manifestHashValuesMatch) {
      foreach ($relativePath in $expectedHashes.Keys) {
        if ($manifestHashes[$relativePath] -ne $expectedHashes[$relativePath]) {
          $manifestHashValuesMatch = $false
          break
        }
      }
    }
    $manifestValid = (
      [int]$manifest.schema -eq 2 -and
      [string]$manifest.release_version -eq "1.0.0" -and
      [string]$manifest.base_commit -eq "4fbfff17d0758282e106cbeab8368787e7c34ca8" -and
      [string]$manifest.source_commit -match "^[0-9a-f]{40}$" -and
      [string]$manifest.expected_computer -eq $expectedComputer -and
      [string]$manifest.expected_windows_user -eq $expectedUser -and
      [string]$manifest.agent_id -eq "frontdesk_dreamz" -and
      [string]$manifest.source_root -eq "C:\Gym Assistant 2.6" -and
      [int]$manifest.receipt_ledger_schema -eq 2 -and
      [string]$manifest.receipt_ledger_policy -eq "current-install-carried-forward" -and
      $manifest.starts_during_install -eq $false -and
      $manifest.changes_scheduled_sync -eq $false -and
      $manifestHashValuesMatch
    )
  }
  catch {
    $manifestValid = $false
  }
}

$pythonPathFile = Join-Path $InstallRoot "python-path.txt"
$recordedPython = ""
if (Test-Path -LiteralPath $pythonPathFile -PathType Leaf) {
  $recordedPython = (Get-Content -LiteralPath $pythonPathFile -Raw).Trim()
}
$trustedPythonResolved = ""
if (
  (Test-FullyQualifiedWindowsPath -Value $TrustedPythonPath) -and
  (Test-Path -LiteralPath $TrustedPythonPath -PathType Leaf)
) {
  $trustedPythonResolved = (Resolve-Path -LiteralPath $TrustedPythonPath).Path
}
$recordedPythonMatches = (
  $trustedPythonResolved -and
  (Test-FullyQualifiedWindowsPath -Value $recordedPython) -and
  [IO.Path]::GetFullPath($recordedPython) -ieq $trustedPythonResolved
)
$trustedInstall = (
  $allStaticFilesValid -and
  $manifestValid -and
  $recordedPythonMatches
)

$pythonSupported = $false
$pythonVersion = $null
if ($trustedInstall) {
  $pythonVersion = & $trustedPythonResolved -B -I -c (
    "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"
  )
  $pythonSupported = (
    $LASTEXITCODE -eq 0 -and
    [version]$pythonVersion -ge [version]"3.10"
  )
}

$ledgerHealth = [ordered]@{
  checked = $false
  valid = $false
  receipt_count = $null
  reason = if ($trustedInstall) { "not_checked" } else { "untrusted_install" }
}
$runtimePath = Join-Path $InstallRoot "runtime.json"
if (
  $trustedInstall -and
  $pythonSupported -and
  (Test-Path -LiteralPath $runtimePath -PathType Leaf)
) {
  $healthProgram = (
    "import pathlib,sys;" +
    "sys.path.insert(0,sys.argv[1]);" +
    "from frontdesk_payment_runner.runner import run_health_check;" +
    "raise SystemExit(run_health_check(pathlib.Path(sys.argv[2])))"
  )
  $healthRaw = & $trustedPythonResolved -B -I -c $healthProgram `
    $InstallRoot `
    $runtimePath 2>&1
  $healthExitCode = $LASTEXITCODE
  try {
    $healthPayload = ($healthRaw -join "") | ConvertFrom-Json
    $healthReceiptCount = $null
    if ($null -ne $healthPayload.PSObject.Properties["receipt_count"]) {
      $healthReceiptCount = [int]$healthPayload.receipt_count
    }
    $ledgerHealth = [ordered]@{
      checked = $true
      valid = ($healthExitCode -eq 0 -and [bool]$healthPayload.ok)
      receipt_count = $healthReceiptCount
      reason = if ($healthExitCode -eq 0) {
        "ready"
      }
      else {
        [string]$healthPayload.reason
      }
    }
  }
  catch {
    $ledgerHealth = [ordered]@{
      checked = $true
      valid = $false
      receipt_count = $null
      reason = "health_output_invalid"
    }
  }
}

$preflight = [ordered]@{
  checked = $false
  ok = $false
  reason = if ($trustedInstall) { "not_checked" } else { "untrusted_install" }
}
if (
  -not $SkipDesktopPreflight -and
  $trustedInstall -and
  $pythonSupported -and
  [Environment]::MachineName -ieq $expectedComputer -and
  $currentUser -ieq $expectedUser
) {
  $preflightProgram = (
    "import runpy,sys;" +
    "sys.path.insert(0,sys.argv[1]);" +
    "runpy.run_module('frontdesk_payment_runner.preflight',run_name='__main__')"
  )
  $preflightRaw = & $trustedPythonResolved -B -I -c $preflightProgram `
    $InstallRoot 2>&1
  $preflightExitCode = $LASTEXITCODE
  try {
    $preflightPayload = ($preflightRaw -join "") | ConvertFrom-Json
    $preflight = [ordered]@{
      checked = $true
      ok = [bool]$preflightPayload.ok
      reason = [string]$preflightPayload.reason
    }
    if ($null -ne $preflightPayload.PSObject.Properties["eligible_window_count"]) {
      $preflight.eligible_window_count = [int]$preflightPayload.eligible_window_count
    }
    if ($preflightExitCode -eq 0 -and -not $preflight.ok) {
      $preflight.reason = "invalid_preflight_result"
    }
  }
  catch {
    $preflight = [ordered]@{
      checked = $true
      ok = $false
      reason = "preflight_output_invalid"
    }
  }
}

[ordered]@{
  schema = 2
  inventory = "dreamz_frontdesk_payment_runner"
  read_only = $true
  expected_computer_name = $expectedComputer
  computer_name_matches = ([Environment]::MachineName -ieq $expectedComputer)
  expected_windows_context_matches = ($currentUser -ieq $expectedUser)
  user_interactive = [Environment]::UserInteractive
  install_exists = $installExists
  installed_manifest_valid = $manifestValid
  static_files_valid = $allStaticFilesValid
  trusted_install = $trustedInstall
  files = $fileChecks
  python = [ordered]@{
    available = [bool]$trustedPythonResolved
    recorded_path_matches = $recordedPythonMatches
    supported = $pythonSupported
    version = $pythonVersion
  }
  runner_active = (Test-MutexExists -Name $runnerMutexName)
  lifecycle_active = (Test-MutexExists -Name $lifecycleMutexName)
  receipt_ledger = $ledgerHealth
  desktop_preflight = $preflight
  credential_check = "not_performed"
  normal_scheduled_sync_check = "not_performed"
} | ConvertTo-Json -Depth 7
