#Requires -Version 5.1

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [switch]$ConfirmApply,

  [ValidateRange(1, 500)]
  [int]$Limit = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$expectedComputer = "DREAMZ-FRNTDSK"
$expectedUser = "Dreamz Fitness"
$expectedAgent = "frontdesk_dreamz"
$expectedSourceRoot = "C:\Gym Assistant 2.6"
$lifecycleMutexName = "Local\DreamzFrontdeskPaymentCodeChange"
$expectedHashesWithoutSelf = [ordered]@{
  "sync_agent.py" = "8E4E1B093207EDA39377D09498E917B51CAC9D0987B1FD48BC8B908A0459AD67"
  "gymassistant_payment_writer.py" = "49224368B4214310492E888C4431F7EABD187CC8C3D16E3AFF8A0677B8C932EA"
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

if (-not $ConfirmApply) {
  throw "Explicit -ConfirmApply is required. Nothing was claimed or written."
}
if ([Environment]::MachineName -ine $expectedComputer) {
  throw "This runner is restricted to DREAMZ-FRNTDSK."
}
$identityName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUser = ($identityName -split "\\")[-1]
if ($currentUser -ine $expectedUser) {
  throw "This runner is restricted to the documented Dreamz Fitness Windows context."
}
if (-not [Environment]::UserInteractive) {
  throw "Run this only from the interactive Frontdesk Windows session."
}

$lifecycleMutex = [Threading.Mutex]::new($false, $lifecycleMutexName)
$hasLifecycleLock = $false
try {
  $hasLifecycleLock = $lifecycleMutex.WaitOne(0)
  if (-not $hasLifecycleLock) {
    throw "A Frontdesk install, restore, or payment launcher is already active."
  }

  $root = $PSScriptRoot
  $runner = Join-Path $root "frontdesk_payment_runner\runner.py"
  $writer = Join-Path $root "gymassistant_payment_writer.py"
  $runtime = Join-Path $root "runtime.json"
  $ledger = Join-Path $root "state\payment-receipts.json"
  $pythonPathFile = Join-Path $root "python-path.txt"
  $manifestPath = Join-Path $root "installed-manifest.json"

  foreach ($required in @(
    $runtime,
    $ledger,
    $pythonPathFile,
    $manifestPath
  )) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
      throw "Installed runner is incomplete. Run inventory before retrying."
    }
  }

  try {
    $installedManifest = Get-Content -LiteralPath $manifestPath -Raw |
      ConvertFrom-Json
    $manifestHashes = [ordered]@{}
    foreach ($property in $installedManifest.file_hashes.PSObject.Properties) {
      $manifestHashes[$property.Name] = (
        [string]$property.Value
      ).ToUpperInvariant()
    }
  }
  catch {
    throw "The installed release manifest is invalid."
  }
  $expectedManifestKeys = @(
    @($expectedHashesWithoutSelf.Keys) + "process_fep_now.ps1"
  )
  if (
    [int]$installedManifest.schema -ne 2 -or
    [string]$installedManifest.release_version -ne "1.0.0" -or
    [string]$installedManifest.base_commit -ne "4fbfff17d0758282e106cbeab8368787e7c34ca8" -or
    [string]$installedManifest.source_commit -notmatch "^[0-9a-f]{40}$" -or
    [string]$installedManifest.expected_computer -ne $expectedComputer -or
    [string]$installedManifest.expected_windows_user -ne $expectedUser -or
    [string]$installedManifest.agent_id -ne $expectedAgent -or
    [string]$installedManifest.source_root -ne $expectedSourceRoot -or
    [int]$installedManifest.receipt_ledger_schema -ne 2 -or
    [string]$installedManifest.receipt_ledger_policy -ne "current-install-carried-forward" -or
    @(Compare-Object $expectedManifestKeys @($manifestHashes.Keys)).Count -ne 0
  ) {
    throw "The installed release manifest scope is invalid."
  }
  foreach ($relativePath in $expectedHashesWithoutSelf.Keys) {
    if (
      $manifestHashes[$relativePath] -ne
      $expectedHashesWithoutSelf[$relativePath]
    ) {
      throw "The installed manifest hash is not trusted for $relativePath."
    }
  }
  foreach ($relativePath in $manifestHashes.Keys) {
    $candidate = Join-Path $root $relativePath
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
      throw "Installed release item is missing: $relativePath"
    }
    if (
      (Get-Sha256Hex -LiteralPath $candidate) -ne
      $manifestHashes[$relativePath]
    ) {
      throw "Installed release hash failed for $relativePath."
    }
  }

  $runtimeConfig = Get-Content -LiteralPath $runtime -Raw | ConvertFrom-Json
  if (
    $runtimeConfig.source_root -ne $expectedSourceRoot -or
    [int]$runtimeConfig.idle_seconds -ne 300
  ) {
    throw "The fixed Frontdesk runtime scope is invalid."
  }

  $python = (Get-Content -LiteralPath $pythonPathFile -Raw).Trim()
  if (-not (Test-FullyQualifiedWindowsPath -Value $python)) {
    throw "The recorded Python path is not absolute."
  }
  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The recorded Python executable is unavailable."
  }
  $pythonVersion = & $python -B -I -c (
    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
  )
  if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.10") {
    throw "Python 3.10 or newer is required."
  }

  $processToken = [Environment]::GetEnvironmentVariable(
    "FEP_PAYMENT_RUNNER_TOKEN",
    "Process"
  )
  $token = [Environment]::GetEnvironmentVariable("SYNC_API_TOKEN", "Process")
  if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable("SYNC_API_TOKEN", "User")
  }
  if (-not $token) {
    $token = [Environment]::GetEnvironmentVariable("SYNC_API_TOKEN", "Machine")
  }
  if (-not $token) {
    throw "The existing Portal sync credential is unavailable in this Windows context."
  }

  Push-Location $root
  try {
    [Environment]::SetEnvironmentVariable(
      "FEP_PAYMENT_RUNNER_TOKEN",
      $token,
      "Process"
    )
    $token = $null

    Write-Host "Frontdesk payment-only one-shot starts after the built-in safety checks."
    $runnerProgram = (
      "import sys;" +
      "sys.path.insert(0,sys.argv[1]);" +
      "from frontdesk_payment_runner.runner import main;" +
      "raise SystemExit(main(sys.argv[2:]))"
    )
    & $python -B -I -c $runnerProgram $root `
      --config $runtime `
      --once `
      --max-command-limit $Limit
    $runnerExitCode = $LASTEXITCODE
    if ($runnerExitCode -ne 0) {
      throw "The payment-only run stopped safely with exit code $runnerExitCode. Review FEP before any retry."
    }
    Write-Host "Frontdesk payment-only one-shot finished."
  }
  finally {
    [Environment]::SetEnvironmentVariable(
      "FEP_PAYMENT_RUNNER_TOKEN",
      $processToken,
      "Process"
    )
    $token = $null
    Pop-Location
  }
}
finally {
  if ($hasLifecycleLock) {
    $lifecycleMutex.ReleaseMutex() | Out-Null
  }
  $lifecycleMutex.Dispose()
}
