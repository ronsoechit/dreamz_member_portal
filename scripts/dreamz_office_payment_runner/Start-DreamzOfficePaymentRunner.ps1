param(
  [switch]$ClearStopRequest
)

$ErrorActionPreference = "Stop"
$installRoot = Split-Path -Parent $PSScriptRoot
$stateRoot = Join-Path $installRoot "state"
$stopRequest = Join-Path $stateRoot "stop.request"
$tokenFile = Join-Path $installRoot "secrets\payment-token.dpapi"
$pythonFile = Join-Path $installRoot "python-path.txt"
$runnerFile = Join-Path $installRoot "dreamz_office_payment_runner\runner.py"
$runtimeFile = Join-Path $installRoot "runtime.json"
$launcherMutexName = "Local\DreamzOfficePaymentLauncher"
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
# Never use the last status PID as process identity: Windows can reuse it after
# a reboot or crash. The launcher mutex above and the runner's own named mutex
# are the authoritative duplicate-process guards.
if ($ClearStopRequest -and (Test-Path -LiteralPath $stopRequest)) {
  Remove-Item -LiteralPath $stopRequest -Force
}
if (Test-Path -LiteralPath $stopRequest) {
  exit 0
}
if (-not (Test-Path -LiteralPath $tokenFile)) {
  throw "The DPAPI-protected Office payment-runner token is missing."
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
$secureToken = ConvertTo-SecureString $cipherText
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
  $env:FEP_PAYMENT_RUNNER_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
  Push-Location $installRoot
  try {
    & $pythonPath -m dreamz_office_payment_runner.runner --config $runtimeFile
    exit $LASTEXITCODE
  }
  finally {
    Pop-Location
  }
}
finally {
  $env:FEP_PAYMENT_RUNNER_TOKEN = $null
  if ($tokenPointer -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
  }
  $secureToken.Dispose()
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
