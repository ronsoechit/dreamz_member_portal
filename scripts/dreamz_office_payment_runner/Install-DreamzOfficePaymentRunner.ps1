param(
  [Parameter(Mandatory = $true)]
  [string]$SourceRoot,
  [Security.SecureString]$PaymentToken,
  [string]$PythonPath = "",
  [switch]$NoStartup,
  [switch]$DoNotStart
)

$ErrorActionPreference = "Stop"
$expectedComputerName = "DREAMZ-OFFICE-P"

if (-not [Environment]::UserInteractive) {
  throw "Install from the interactive Windows session that owns Dreamz Office Gym Assistant."
}
if (-not [string]::Equals(
  [string]$env:COMPUTERNAME,
  $expectedComputerName,
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw "Installation is restricted to the reviewed Dreamz Office host $expectedComputerName."
}

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runnerSource = Join-Path $packageRoot "dreamz_office_payment_runner"
$writerSource = Join-Path $packageRoot "gymassistant_payment_writer.py"
$startSource = Join-Path $PSScriptRoot "Start-DreamzOfficePaymentRunner.ps1"
$stopSource = Join-Path $PSScriptRoot "Stop-DreamzOfficePaymentRunner.ps1"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunnerRollback"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Office Payment Runner.lnk"
$existingInstall = Test-Path -LiteralPath $installRoot
$existingStartupLink = Test-Path -LiteralPath $startupLink
$existingReceiptLedger = Join-Path $installRoot "state\payment-receipts.json"
$priorRunnerHistory = @()
if (Test-Path -LiteralPath $rollbackRoot) {
  $priorRunnerHistory = @(
    Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction Stop
  )
}

if ([Management.Automation.WildcardPattern]::ContainsWildcardCharacters($SourceRoot)) {
  throw "SourceRoot must be one exact absolute Windows path without wildcards."
}
$sourceRootItem = Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop
if (-not [string]::Equals(
  [string]$sourceRootItem.Provider.Name,
  "FileSystem",
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw "SourceRoot must resolve through the Windows FileSystem provider."
}
$sourceRootProviderPath = [string]$sourceRootItem.ProviderPath
if (
  -not $sourceRootProviderPath -or
  -not (Test-Path -LiteralPath $sourceRootProviderPath -PathType Container)
) {
  throw "SourceRoot must be an existing directory."
}
$sourceRootLogicalDrive = [string]$sourceRootItem.Drive.Name
if ([string]::Equals(
  $sourceRootLogicalDrive,
  "X",
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw "SourceRoot X:\ is reserved for reviewed package transfer and may not be used as the Gym Assistant runtime root."
}
$resolvedSourcePath = [string]$sourceRootItem.Path
if ($resolvedSourcePath -match "^[^:]+::") {
  $providerQualifiedPath = $resolvedSourcePath.Substring(
    $resolvedSourcePath.IndexOf("::", [StringComparison]::Ordinal) + 2
  )
  $resolvedSourcePath = if ($providerQualifiedPath -match "^[A-Za-z]:[\\/]") {
    $providerQualifiedPath
  }
  else {
    $sourceRootProviderPath
  }
}
$fullSourceRoot = [IO.Path]::GetFullPath($resolvedSourcePath)
$fullProviderSourceRoot = [IO.Path]::GetFullPath($sourceRootProviderPath)
$sourcePathRoot = [IO.Path]::GetPathRoot($fullSourceRoot)
$resolvedSourceRoot = if (
  $fullSourceRoot.Equals($sourcePathRoot, [StringComparison]::OrdinalIgnoreCase)
) {
  $sourcePathRoot
}
else {
  $fullSourceRoot.TrimEnd("\")
}
if (
  $resolvedSourceRoot -notmatch
    "^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/]|$))"
) {
  throw "SourceRoot must be an exact absolute Windows path."
}
$sourceSegments = $resolvedSourceRoot -split "[\\/]"
if ($sourceSegments -contains ".." -or $sourceSegments -contains ".") {
  throw "SourceRoot may not contain relative path segments."
}
$expectedDataRoot = Join-Path $resolvedSourceRoot "Data"
if (-not (Test-Path -LiteralPath $expectedDataRoot -PathType Container)) {
  throw "SourceRoot must contain an existing Data directory."
}
$sourceDrive = [IO.Path]::GetPathRoot($resolvedSourceRoot)
if ([string]::Equals($sourceDrive, "X:\", [StringComparison]::OrdinalIgnoreCase)) {
  throw "SourceRoot X:\ is reserved for reviewed package transfer and may not be used as the Gym Assistant runtime root."
}
$reservedTransferRoot = "\\DREAMZ-OFFICE-P\Shared Operations"
if ($resolvedSourceRoot.StartsWith(
  $reservedTransferRoot,
  [StringComparison]::OrdinalIgnoreCase
) -or $fullProviderSourceRoot.StartsWith(
  $reservedTransferRoot,
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw "The Shared Operations transfer location may not be used as the Gym Assistant runtime root."
}

if ($existingInstall -and -not (Test-Path -LiteralPath $existingReceiptLedger -PathType Leaf)) {
  throw "Upgrade refused because the existing receipt ledger is missing. Restore or reconcile the previous safety state; never initialize an empty ledger over an existing installation."
}
if ($existingInstall) {
  $existingRuntimeFile = Join-Path $installRoot "runtime.json"
  if (-not (Test-Path -LiteralPath $existingRuntimeFile -PathType Leaf)) {
    throw "Upgrade refused because the existing runtime configuration is missing."
  }
  try {
    $existingRuntime = Get-Content -Raw -LiteralPath $existingRuntimeFile | ConvertFrom-Json
    $existingSourceRoot = [IO.Path]::GetFullPath([string]$existingRuntime.source_root)
  }
  catch {
    throw "Upgrade refused because the existing SourceRoot cannot be validated."
  }
  if (-not $existingSourceRoot.Equals(
    $fullSourceRoot,
    [StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Upgrade refused because SourceRoot differs from the installed runner. Reconcile the target and receipt ledger before changing Gym Assistant data roots."
  }
}
if (-not $existingInstall -and ($existingStartupLink -or $priorRunnerHistory.Count -gt 0)) {
  throw "Fresh install refused because prior runner state exists. Restore the newest complete uninstall snapshot or perform an explicitly reviewed recovery; a new empty receipt ledger is only allowed on a demonstrable first install."
}

foreach ($required in @($runnerSource, $writerSource, $startSource, $stopSource)) {
  if (-not (Test-Path -LiteralPath $required)) {
    throw "Required package item is missing: $required"
  }
}

if (-not $PythonPath) {
  $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
  if (-not $pythonCommand) {
    throw "Python 3 was not found. Supply -PythonPath with an exact python.exe path."
  }
  $PythonPath = $pythonCommand.Source
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonVersion = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.10") {
  throw "Python 3.10 or newer is required."
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
  [IO.File]::WriteAllText((Join-Path $installRoot "python-path.txt"), $PythonPath)

  $runtime = [ordered]@{
    source_root = $resolvedSourceRoot
    poll_seconds = 5
    idle_seconds = 300
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
    $healthOutput = & $PythonPath -m dreamz_office_payment_runner.runner `
      --config (Join-Path $installRoot "runtime.json") `
      --health-check 2>&1
    if ($LASTEXITCODE -ne 0) {
      throw "The copied receipt ledger or runner configuration failed the non-mutating health check. Upgrade refused."
    }
  }
  finally {
    Pop-Location
  }

  $tokenFile = Join-Path $installRoot "secrets\payment-token.dpapi"
  $backupToken = if ($existingBackedUp) { Join-Path $backupPath "install\secrets\payment-token.dpapi" } else { "" }
  if (-not $PaymentToken -and $backupToken -and (Test-Path -LiteralPath $backupToken)) {
    Copy-Item -LiteralPath $backupToken -Destination $tokenFile
  }
  else {
    if (-not $PaymentToken) {
      $PaymentToken = Read-Host "Dreamz Office payment-runner token" -AsSecureString
    }
    $protectedToken = ConvertFrom-SecureString $PaymentToken
    [IO.File]::WriteAllText($tokenFile, $protectedToken)
  }

  if (-not $NoStartup) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startupLink)
    $powershellPath = (Get-Process -Id $PID).Path
    if (-not (Test-Path -LiteralPath $powershellPath -PathType Leaf)) {
      throw "Could not resolve the current PowerShell executable."
    }
    $shortcut.TargetPath = $powershellPath
    $startInstalled = Join-Path $installRoot "scripts\Start-DreamzOfficePaymentRunner.ps1"
    $shortcut.Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $startInstalled + '" -ClearStopRequest'
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.Description = "Dreamz payment-only runner for the Dreamz Office PC"
    $shortcut.Save()
  }
  elseif (Test-Path -LiteralPath $startupLink) {
    Remove-Item -LiteralPath $startupLink -Force
  }

  if (-not $DoNotStart) {
    $startInstalled = Join-Path $installRoot "scripts\Start-DreamzOfficePaymentRunner.ps1"
    $powershellPath = (Get-Process -Id $PID).Path
    $startArguments = '-NoLogo -NoProfile -NonInteractive -File "' + $startInstalled + '" -ClearStopRequest'
    Start-Process -FilePath $powershellPath `
      -ArgumentList $startArguments `
      -WindowStyle Hidden
  }

  Write-Host "Installed payment-only runner for dreamz_office."
  Write-Host "Install root: $installRoot"
  Write-Host "Configured Gym Assistant source root: $resolvedSourceRoot"
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
