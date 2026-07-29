#Requires -Version 5.1

[CmdletBinding()]
param(
  [string]$PythonPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$manifestPath = Join-Path $packageRoot "release-manifest.json"
$payloadAllowlist = @(
  "reserve_laptop_payment_runner/README.md"
  "reserve_laptop_payment_runner/__init__.py"
  "reserve_laptop_payment_runner/runner.py"
  "gymassistant_payment_writer.py"
  "scripts/reserve_laptop_payment_runner/Install-ReserveLaptopPaymentRunner.ps1"
  "scripts/reserve_laptop_payment_runner/Restore-ReserveLaptopPaymentRunner.ps1"
  "scripts/reserve_laptop_payment_runner/Start-ReserveLaptopPaymentRunner.ps1"
  "scripts/reserve_laptop_payment_runner/Stop-ReserveLaptopPaymentRunner.ps1"
  "scripts/reserve_laptop_payment_runner/Test-DreamzReservePaymentRunnerRelease.ps1"
  "scripts/reserve_laptop_payment_runner/Test-ReserveLaptopPaymentRunnerPreflight.ps1"
  "scripts/reserve_laptop_payment_runner/Uninstall-ReserveLaptopPaymentRunner.ps1"
) | Sort-Object
$archiveAllowlist = @($payloadAllowlist + "release-manifest.json") | Sort-Object

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

$manifestValid = $false
$manifestReason = "manifest_missing"
$manifestFiles = [ordered]@{}
$sourceCommit = $null
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
  try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw |
      ConvertFrom-Json
    $sourceCommit = [string]$manifest.source_commit
    foreach ($property in $manifest.files.PSObject.Properties) {
      $manifestFiles[$property.Name.Replace("\", "/")] = (
        [string]$property.Value
      ).ToUpperInvariant()
    }
    $manifestKeys = @($manifestFiles.Keys) | Sort-Object
    $manifestFileSetMatches = @(
      Compare-Object $payloadAllowlist $manifestKeys
    ).Count -eq 0
    $manifestValid = (
      [int]$manifest.schema -eq 1 -and
      [string]$manifest.release -eq "dreamz_reserve_payment_runner" -and
      [string]$manifest.version -eq "1.0.0" -and
      [string]$manifest.expected_computer -eq "DESKTOP-8KM7V7D" -and
      [string]$manifest.expected_windows_user -eq "ron" -and
      [string]$manifest.agent_id -eq "reserve_8km7v7d" -and
      [string]$manifest.source_root -eq "\\DREAMZ-FRNTDSK\Gym Assistant 2.6" -and
      $manifest.starts_during_install -eq $false -and
      $manifest.creates_startup_by_default -eq $false -and
      $manifest.requires_dedicated_payment_token -eq $true -and
      $sourceCommit -match "^[0-9a-f]{40}$" -and
      [string]$manifest.eol_policy -eq "lf" -and
      [int]$manifest.payload_file_count -eq $payloadAllowlist.Count -and
      $manifestFileSetMatches
    )
    $manifestReason = if ($manifestValid) {
      "valid"
    }
    else {
      "manifest_scope_invalid"
    }
  }
  catch {
    $manifestValid = $false
    $manifestReason = "manifest_parse_invalid"
  }
}

$packagePrefix = $packageRoot.TrimEnd("\") + "\"
$actualFiles = @(
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
$allowlistExact = @(
  Compare-Object $archiveAllowlist $actualFiles
).Count -eq 0

$hashChecks = @()
$eolChecks = @()
foreach ($relativePath in $payloadAllowlist) {
  $candidate = Join-Path $packageRoot $relativePath.Replace("/", "\")
  $exists = Test-Path -LiteralPath $candidate -PathType Leaf
  $matches = $false
  $lfOnly = $false
  if ($exists -and $manifestFiles.Contains($relativePath)) {
    $matches = (
      (Get-Sha256Hex -LiteralPath $candidate) -eq
      $manifestFiles[$relativePath]
    )
    $bytes = [IO.File]::ReadAllBytes($candidate)
    $lfOnly = -not ($bytes -contains [byte]13)
  }
  $hashChecks += [ordered]@{
    file = $relativePath
    exists = $exists
    hash_matches = $matches
  }
  $eolChecks += [ordered]@{
    file = $relativePath
    lf_only = $lfOnly
  }
}
$allHashesMatch = (
  $manifestValid -and
  @($hashChecks | Where-Object { -not $_.exists -or -not $_.hash_matches }).Count -eq 0
)
$allPayloadIsLf = @(
  $eolChecks | Where-Object { -not $_.lf_only }
).Count -eq 0

$powershellScripts = @(
  Join-Path $PSScriptRoot "Install-ReserveLaptopPaymentRunner.ps1"
  Join-Path $PSScriptRoot "Restore-ReserveLaptopPaymentRunner.ps1"
  Join-Path $PSScriptRoot "Start-ReserveLaptopPaymentRunner.ps1"
  Join-Path $PSScriptRoot "Stop-ReserveLaptopPaymentRunner.ps1"
  Join-Path $PSScriptRoot "Test-DreamzReservePaymentRunnerRelease.ps1"
  Join-Path $PSScriptRoot "Test-ReserveLaptopPaymentRunnerPreflight.ps1"
  Join-Path $PSScriptRoot "Uninstall-ReserveLaptopPaymentRunner.ps1"
)
$parseErrors = @()
if ($allHashesMatch -and $allowlistExact) {
  foreach ($script in $powershellScripts) {
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile(
      $script,
      [ref]$tokens,
      [ref]$errors
    )
    foreach ($parseError in $errors) {
      $parseErrors += [ordered]@{
        file = Split-Path -Leaf $script
        error_id = $parseError.ErrorId
      }
    }
  }
}
else {
  $parseErrors += [ordered]@{
    file = "release"
    error_id = "hash_or_allowlist_failed_before_parse"
  }
}

if (-not $PythonPath) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if (
    $pythonCommand -and
    [string]$pythonCommand.Source -notmatch "(?i)\\WindowsApps\\"
  ) {
    $PythonPath = $pythonCommand.Source
  }
}
$pythonSyntaxOk = $false
if (
  $allHashesMatch -and
  $allowlistExact -and
  $PythonPath -and
  (Test-Path -LiteralPath $PythonPath -PathType Leaf)
) {
  $pythonFiles = @(
    "gymassistant_payment_writer.py"
    "reserve_laptop_payment_runner/runner.py"
  )
  $pythonSyntaxOk = $true
  foreach ($pythonFile in $pythonFiles) {
    $candidate = Join-Path $packageRoot $pythonFile.Replace("/", "\")
    & $PythonPath -B -I -c (
      "import ast,pathlib,sys;" +
      "ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))"
    ) $candidate 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      $pythonSyntaxOk = $false
      break
    }
  }
}

$forbiddenItems = @(
  $actualFiles |
    Where-Object {
      (Split-Path -Leaf $_) -match (
        "^(?:\.env|.*\.db3?|.*\.sqlite3?|.*\.pem|.*\.key)$"
      ) -or
      (
        (Split-Path -Leaf $_) -match "(?:token|secret|cookie|credential)" -and
        (Split-Path -Leaf $_) -ne "Test-DreamzReservePaymentRunnerRelease.ps1"
      )
    }
)
$result = [ordered]@{
  schema = 2
  release = "dreamz_reserve_payment_runner"
  version = "1.0.0"
  source_commit = $sourceCommit
  read_only_validation = $true
  manifest_valid = $manifestValid
  manifest_reason = $manifestReason
  exact_allowlist_ok = $allowlistExact
  payload_file_count = $payloadAllowlist.Count
  archive_file_count = $actualFiles.Count
  hashes_ok = $allHashesMatch
  eol_policy = "lf"
  eol_policy_ok = $allPayloadIsLf
  powershell_parse_ok = ($parseErrors.Count -eq 0)
  powershell_parse_errors = $parseErrors
  python_syntax_checked = [bool]$PythonPath
  python_syntax_ok = $pythonSyntaxOk
  forbidden_item_count = $forbiddenItems.Count
  file_hashes = $hashChecks
  valid = (
    $manifestValid -and
    $allowlistExact -and
    $allHashesMatch -and
    $allPayloadIsLf -and
    $parseErrors.Count -eq 0 -and
    $pythonSyntaxOk -and
    $forbiddenItems.Count -eq 0
  )
}
$result | ConvertTo-Json -Depth 7
if (-not $result.valid) {
  exit 3
}
