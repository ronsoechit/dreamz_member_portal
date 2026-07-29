param(
  [switch]$ClearStopRequest
)

$ErrorActionPreference = "Stop"
if ([Environment]::MachineName -ine "DESKTOP-8KM7V7D") {
  throw "Start refused: this runner is fixed to DESKTOP-8KM7V7D."
}
if ([Environment]::UserName -ine "ron") {
  throw "Start refused: this runner is fixed to Windows user ron."
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole(
  [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
  throw "Start refused from an elevated PowerShell."
}
$installRoot = Split-Path -Parent $PSScriptRoot
$stateRoot = Join-Path $installRoot "state"
$stopRequest = Join-Path $stateRoot "stop.request"
$tokenFile = Join-Path $installRoot "secrets\sync-token.dpapi"
$pythonFile = Join-Path $installRoot "python-path.txt"
$runnerFile = Join-Path $installRoot "reserve_laptop_payment_runner\runner.py"
$runtimeFile = Join-Path $installRoot "runtime.json"
$statusFile = Join-Path $stateRoot "status.json"
$launcherMutexName = "Local\DreamzReserveLaptopPaymentLauncher"
$launcherMutexCreated = $false
$launcherMutex = [Threading.Mutex]::new(
  $true,
  $launcherMutexName,
  [ref]$launcherMutexCreated
)
if (-not $launcherMutexCreated) {
  $launcherMutex.Dispose()
  exit 0
}

try {
$recordedPid = 0
if (Test-Path -LiteralPath $statusFile) {
  try {
    $status = Get-Content -Raw -LiteralPath $statusFile | ConvertFrom-Json
    if ($status.state -notin @("stopped", "configuration_error")) {
      $recordedPid = [int]$status.pid
    }
  }
  catch {
    $recordedPid = 0
  }
}
if ($recordedPid -gt 0 -and (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
  exit 0
}

if ($ClearStopRequest -and (Test-Path -LiteralPath $stopRequest)) {
  Remove-Item -LiteralPath $stopRequest -Force
}
if (Test-Path -LiteralPath $stopRequest) {
  exit 0
}
if (-not (Test-Path -LiteralPath $tokenFile)) {
  throw "The DPAPI-protected sync token is missing."
}
if (-not (Test-Path -LiteralPath $pythonFile)) {
  throw "The configured Python path is missing."
}
if (-not (Test-Path -LiteralPath $runnerFile)) {
  throw "The payment runner is missing."
}

$pythonPath = (Get-Content -Raw -LiteralPath $pythonFile).Trim()
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
  throw "The configured Python executable is unavailable."
}

$cipherText = (Get-Content -Raw -LiteralPath $tokenFile).Trim()
$secureValue = ConvertTo-SecureString $cipherText
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
try {
  $env:SYNC_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
  Push-Location $installRoot
  try {
    & $pythonPath -m reserve_laptop_payment_runner.runner --config $runtimeFile
    exit $LASTEXITCODE
  }
  finally {
    Pop-Location
  }
}
finally {
  $env:SYNC_API_TOKEN = $null
  if ($tokenPointer -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
  }
  $secureValue.Dispose()
}
}
finally {
  try {
    $launcherMutex.ReleaseMutex()
  }
  finally {
    $launcherMutex.Dispose()
  }
}
