param(
  [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\RonLaptopPaymentRunner"
$stateRoot = Join-Path $installRoot "state"
$stopRequest = Join-Path $stateRoot "stop.request"
$runnerMutexName = "Local\DreamzRonLaptopPaymentRunner"
$launcherMutexName = "Local\DreamzRonLaptopPaymentLauncher"

function Test-NamedMutexActive {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name
  )
  $mutexHandle = $null
  try {
    $mutexHandle = [Threading.Mutex]::OpenExisting($Name)
    return $true
  }
  catch [Threading.WaitHandleCannotBeOpenedException] {
    return $false
  }
  catch [UnauthorizedAccessException] {
    # Access uncertainty is active/unsafe for lifecycle mutation.
    return $true
  }
  finally {
    if ($null -ne $mutexHandle) {
      $mutexHandle.Dispose()
    }
  }
}

if (-not (Test-Path -LiteralPath $installRoot)) {
  Write-Host "Ron laptop payment runner is not installed."
  exit 0
}

New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
[IO.File]::WriteAllText($stopRequest, ([DateTimeOffset]::UtcNow.ToString("o")))

$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
$quietMutexChecks = 0
while ([DateTime]::UtcNow -lt $deadline) {
  $runnerMutexActive = Test-NamedMutexActive -Name $runnerMutexName
  $launcherMutexActive = Test-NamedMutexActive -Name $launcherMutexName
  if ($runnerMutexActive -or $launcherMutexActive) {
    $quietMutexChecks = 0
  }
  else {
    $quietMutexChecks += 1
  }

  # Several consecutive absent checks also cover legacy launchers that do not
  # yet own the dedicated launcher mutex.
  if ($quietMutexChecks -ge 5) {
    Write-Host "Ron laptop payment runner stopped cooperatively."
    exit 0
  }
  Start-Sleep -Milliseconds 500
}

throw "The runner did not stop within the timeout. Nothing was killed; retry after the current guarded UI action finishes."
