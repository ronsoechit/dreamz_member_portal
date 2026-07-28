param(
  [Security.SecureString]$SyncToken,
  [string]$PythonPath = "",
  [switch]$NoStartup,
  [switch]$DoNotStart
)

$ErrorActionPreference = "Stop"

if (-not [Environment]::UserInteractive) {
  throw "Install from Ron's interactive Windows session."
}

$packageRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runnerSource = Join-Path $packageRoot "ron_laptop_payment_runner"
$writerSource = Join-Path $packageRoot "gymassistant_payment_writer.py"
$startSource = Join-Path $PSScriptRoot "Start-RonLaptopPaymentRunner.ps1"
$stopSource = Join-Path $PSScriptRoot "Stop-RonLaptopPaymentRunner.ps1"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunnerRollback"
$startupRoot = [Environment]::GetFolderPath("Startup")
$startupLink = Join-Path $startupRoot "Dreamz Ron Laptop Payment Runner.lnk"

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
    poll_seconds = 5
    idle_seconds = 2
    command_ttl_seconds = 900
    request_timeout_seconds = 20
    writer_timeout_seconds = 30
  }
  [IO.File]::WriteAllText(
    (Join-Path $installRoot "runtime.json"),
    ($runtime | ConvertTo-Json)
  )

  $tokenFile = Join-Path $installRoot "secrets\sync-token.dpapi"
  $backupToken = if ($existingBackedUp) { Join-Path $backupPath "install\secrets\sync-token.dpapi" } else { "" }
  if (-not $SyncToken -and $backupToken -and (Test-Path -LiteralPath $backupToken)) {
    Copy-Item -LiteralPath $backupToken -Destination $tokenFile
  }
  else {
    if (-not $SyncToken) {
      $SyncToken = Read-Host "Member Portal sync token" -AsSecureString
    }
    $protectedToken = ConvertFrom-SecureString $SyncToken
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
    $startInstalled = Join-Path $installRoot "scripts\Start-RonLaptopPaymentRunner.ps1"
    $shortcut.Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $startInstalled + '" -ClearStopRequest'
    $shortcut.WorkingDirectory = $installRoot
    $shortcut.Description = "Dreamz payment-only runner for Ron laptop"
    $shortcut.Save()
  }
  elseif (Test-Path -LiteralPath $startupLink) {
    Remove-Item -LiteralPath $startupLink -Force
  }

  if (-not $DoNotStart) {
    $startInstalled = Join-Path $installRoot "scripts\Start-RonLaptopPaymentRunner.ps1"
    $powershellPath = (Get-Process -Id $PID).Path
    $startArguments = '-NoLogo -NoProfile -NonInteractive -File "' + $startInstalled + '" -ClearStopRequest'
    Start-Process -FilePath $powershellPath `
      -ArgumentList $startArguments `
      -WindowStyle Hidden
  }

  Write-Host "Installed payment-only runner for ron_laptop."
  Write-Host "Install root: $installRoot"
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
