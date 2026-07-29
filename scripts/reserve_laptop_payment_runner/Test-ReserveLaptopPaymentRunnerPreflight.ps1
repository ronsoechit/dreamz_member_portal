param()

$ErrorActionPreference = "Stop"
if ([Environment]::MachineName -ine "DESKTOP-8KM7V7D") {
  throw "Preflight refused: this runner is fixed to DESKTOP-8KM7V7D."
}
if ([Environment]::UserName -ine "ron") {
  throw "Preflight refused: this runner is fixed to Windows user ron."
}

$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\ReserveLaptopPaymentRunner"
$pythonFile = Join-Path $installRoot "python-path.txt"
$runtimeFile = Join-Path $installRoot "runtime.json"
$runnerFile = Join-Path $installRoot "reserve_laptop_payment_runner\runner.py"

foreach ($required in @($pythonFile, $runtimeFile, $runnerFile)) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
    throw "Preflight refused because an installed runner file is missing."
  }
}

$pythonPath = (Get-Content -Raw -LiteralPath $pythonFile).Trim()
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
  throw "Preflight refused because the configured Python executable is unavailable."
}

Push-Location $installRoot
try {
  & $pythonPath -m reserve_laptop_payment_runner.runner `
    --config $runtimeFile `
    --preflight
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
