param(
  [string]$ExpectedComputerName = "DREAMZ-OFFICE-P",
  [string]$OutputPath = "",
  [switch]$SkipPortalHttpsCheck,
  [switch]$AllowNonOfficeForTesting
)

$ErrorActionPreference = "Stop"
$portalUrl = "https://dreamzmemberportal-production.up.railway.app/"
$reservedTransferRoot = "\\DREAMZ-OFFICE-P\Shared Operations"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class DreamzOfficeInventoryNative
{
    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO
    {
        public uint cbSize;
        public uint dwTime;
    }

    [DllImport("kernel32.dll")]
    public static extern uint WTSGetActiveConsoleSessionId();

    [DllImport("kernel32.dll")]
    public static extern uint GetTickCount();

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr OpenInputDesktop(
        uint dwFlags,
        bool fInherit,
        uint dwDesiredAccess
    );

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool CloseDesktop(IntPtr hDesktop);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool GetUserObjectInformation(
        IntPtr hObj,
        int nIndex,
        StringBuilder pvInfo,
        uint nLength,
        out uint lpnLengthNeeded
    );

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
}
"@

function Get-InputDesktopState {
  $desktopName = $null
  $desktopHandle = [DreamzOfficeInventoryNative]::OpenInputDesktop(0, $false, 0x0001)
  if ($desktopHandle -ne [IntPtr]::Zero) {
    try {
      [uint32]$required = 0
      [void][DreamzOfficeInventoryNative]::GetUserObjectInformation(
        $desktopHandle,
        2,
        $null,
        0,
        [ref]$required
      )
      if ($required -gt 0 -and $required -le 4096) {
        $builder = [Text.StringBuilder]::new([int]($required / 2 + 1))
        if ([DreamzOfficeInventoryNative]::GetUserObjectInformation(
          $desktopHandle,
          2,
          $builder,
          [uint32]($builder.Capacity * 2),
          [ref]$required
        )) {
          $desktopName = $builder.ToString()
        }
      }
    }
    finally {
      [void][DreamzOfficeInventoryNative]::CloseDesktop($desktopHandle)
    }
  }
  [ordered]@{
    name = $desktopName
    is_default = [string]::Equals(
      [string]$desktopName,
      "Default",
      [StringComparison]::OrdinalIgnoreCase
    )
    foreground_window_present = (
      [DreamzOfficeInventoryNative]::GetForegroundWindow() -ne [IntPtr]::Zero
    )
  }
}

function Get-IdleSeconds {
  $info = [DreamzOfficeInventoryNative+LASTINPUTINFO]::new()
  $info.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($info)
  if (-not [DreamzOfficeInventoryNative]::GetLastInputInfo([ref]$info)) {
    return $null
  }
  $elapsed = (
    [uint64][DreamzOfficeInventoryNative]::GetTickCount() -
    [uint64]$info.dwTime
  ) -band 0xffffffff
  return [math]::Round(([double]$elapsed / 1000), 1)
}

function Get-PythonInventory {
  $python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if (-not $python) {
    return [ordered]@{
      available = $false
      version = $null
      supported = $false
    }
  }
  $version = $null
  try {
    $version = (& $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null |
      Select-Object -First 1)
    if ($LASTEXITCODE -ne 0) {
      $version = $null
    }
  }
  catch {
    $version = $null
  }
  $supported = $false
  if ($version -and $version -match "^(?<Major>[0-9]+)\.(?<Minor>[0-9]+)\.") {
    $supported = (
      [int]$Matches.Major -gt 3 -or
      ([int]$Matches.Major -eq 3 -and [int]$Matches.Minor -ge 10)
    )
  }
  [ordered]@{
    available = [bool]$version
    version = $version
    supported = $supported
  }
}

function Get-DriveKind {
  param([string]$Path)
  if ($Path -match "^\\\\") {
    return "unc"
  }
  if ($Path -match "^[A-Za-z]:\\") {
    return "drive_letter"
  }
  return "other"
}

function Test-FullyQualifiedWindowsPath {
  param([string]$Path)
  return [bool](
    $Path -match "^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/]|$))"
  )
}

function Test-ReservedTransferPath {
  param([string]$Path)
  $driveRoot = [IO.Path]::GetPathRoot($Path)
  if ([string]::Equals($driveRoot, "X:\", [StringComparison]::OrdinalIgnoreCase)) {
    return $true
  }
  return $Path.StartsWith(
    $reservedTransferRoot,
    [StringComparison]::OrdinalIgnoreCase
  )
}

function Get-GymAssistantInventory {
  $processes = @(
    Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.ProcessName -like "Gym Assistant*" }
  )
  $visibleCount = 0
  $recognized = @()
  foreach ($process in $processes) {
    $windowHandle = [IntPtr]::Zero
    $title = ""
    try {
      $windowHandle = $process.MainWindowHandle
      $title = [string]$process.MainWindowTitle
    }
    catch {
      continue
    }
    if ($windowHandle -eq [IntPtr]::Zero) {
      continue
    }
    $visibleCount += 1
    if (-not $title.StartsWith("Gym Assistant", [StringComparison]::OrdinalIgnoreCase)) {
      continue
    }
    $pathMarker = $title.LastIndexOf("Path=", [StringComparison]::OrdinalIgnoreCase)
    if ($pathMarker -lt 0) {
      continue
    }
    $rawPath = $title.Substring($pathMarker + 5).Trim().TrimEnd("]", ")", "}")
    if (-not $rawPath -or -not (Test-FullyQualifiedWindowsPath -Path $rawPath)) {
      continue
    }
    try {
      $dataPath = [IO.Path]::GetFullPath($rawPath)
    }
    catch {
      continue
    }
    $sourceRoot = Split-Path -Parent $dataPath
    $driveRoot = [IO.Path]::GetPathRoot($dataPath)
    $mappedTarget = $null
    if ($driveRoot -match "^(?<Drive>[A-Za-z]):\\$") {
      $drive = Get-PSDrive -Name $Matches.Drive -PSProvider FileSystem -ErrorAction SilentlyContinue
      if ($drive -and $drive.DisplayRoot) {
        $mappedTarget = [string]$drive.DisplayRoot
      }
    }
    $recognized += [ordered]@{
      data_path = $dataPath
      source_root = $sourceRoot
      data_leaf_is_exact = [string]::Equals(
        [IO.Path]::GetFileName($dataPath.TrimEnd("\")),
        "Data",
        [StringComparison]::OrdinalIgnoreCase
      )
      data_path_reachable = Test-Path -LiteralPath $dataPath -PathType Container
      source_root_reachable = Test-Path -LiteralPath $sourceRoot -PathType Container
      drive_kind = Get-DriveKind -Path $dataPath
      mapped_target = $mappedTarget
      reserved_transfer_path = Test-ReservedTransferPath -Path $dataPath
    }
  }
  $distinctPaths = @(
    $recognized |
      ForEach-Object { $_.data_path } |
      Sort-Object -Unique
  )
  [ordered]@{
    process_count = $processes.Count
    visible_main_window_count = $visibleCount
    recognized_path_window_count = $recognized.Count
    distinct_recognized_data_path_count = $distinctPaths.Count
    candidates = $recognized
  }
}

function Get-PowerInventory {
  $schemeGuid = $null
  try {
    $schemeOutput = (& powercfg.exe /GETACTIVESCHEME 2>$null) -join " "
    if ($LASTEXITCODE -eq 0 -and $schemeOutput -match "(?<Guid>[0-9a-fA-F-]{36})") {
      $schemeGuid = $Matches.Guid.ToLowerInvariant()
    }
  }
  catch {
    $schemeGuid = $null
  }
  $acSeconds = $null
  $dcSeconds = $null
  if ($schemeGuid) {
    $subSleep = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
    $standbyIdle = "29f6c1db-86da-48c5-9fdb-f2b67b1f44da"
    $settingPath = "HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes\$schemeGuid\$subSleep\$standbyIdle"
    try {
      $setting = Get-ItemProperty -LiteralPath $settingPath -ErrorAction Stop
      if ($null -ne $setting.ACSettingIndex) {
        $acSeconds = [int64]$setting.ACSettingIndex
      }
      if ($null -ne $setting.DCSettingIndex) {
        $dcSeconds = [int64]$setting.DCSettingIndex
      }
    }
    catch {
      $acSeconds = $null
      $dcSeconds = $null
    }
  }
  [ordered]@{
    active_scheme_detected = [bool]$schemeGuid
    ac_sleep_seconds = $acSeconds
    dc_sleep_seconds = $dcSeconds
    ac_sleep_disabled = ($null -ne $acSeconds -and $acSeconds -eq 0)
  }
}

function Get-PortalHttpsInventory {
  if ($SkipPortalHttpsCheck) {
    return [ordered]@{
      checked = $false
      reachable = $null
      http_status = $null
    }
  }
  $statusCode = $null
  $reachable = $false
  try {
    $response = Invoke-WebRequest -Uri $portalUrl -Method Head -UseBasicParsing -TimeoutSec 10
    $statusCode = [int]$response.StatusCode
    $reachable = $true
  }
  catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      $statusCode = [int]$_.Exception.Response.StatusCode
      $reachable = $true
    }
  }
  [ordered]@{
    checked = $true
    reachable = $reachable
    http_status = $statusCode
  }
}

$computerName = [string]$env:COMPUTERNAME
$hostnameMatches = [string]::Equals(
  $computerName,
  $ExpectedComputerName,
  [StringComparison]::OrdinalIgnoreCase
)
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$interactiveUser = $null
try {
  $interactiveUser = [string](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName
}
catch {
  $interactiveUser = $null
}
$currentSessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
$activeConsoleSessionId = [int][DreamzOfficeInventoryNative]::WTSGetActiveConsoleSessionId()
$desktop = Get-InputDesktopState
$python = Get-PythonInventory
$gymAssistant = Get-GymAssistantInventory
$power = Get-PowerInventory
$portal = Get-PortalHttpsInventory

$startupRoot = [Environment]::GetFolderPath("Startup")
$installRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunner"
$rollbackRoot = Join-Path $env:LOCALAPPDATA "Dreamz\DreamzOfficePaymentRunnerRollback"
$startupLink = Join-Path $startupRoot "Dreamz Office Payment Runner.lnk"
$rollbackCount = 0
if (Test-Path -LiteralPath $rollbackRoot -PathType Container) {
  $rollbackCount = @(
    Get-ChildItem -LiteralPath $rollbackRoot -Force -ErrorAction SilentlyContinue
  ).Count
}

$candidate = if ($gymAssistant.candidates.Count -eq 1) {
  $gymAssistant.candidates[0]
}
else {
  $null
}
$interactiveOwnerMatches = (
  $interactiveUser -and
  [string]::Equals(
    [string]$currentIdentity,
    [string]$interactiveUser,
    [StringComparison]::OrdinalIgnoreCase
  )
)
$installationPrerequisitesMet = (
  ($hostnameMatches -or $AllowNonOfficeForTesting) -and
  [Environment]::UserInteractive -and
  $interactiveOwnerMatches -and
  $currentSessionId -eq $activeConsoleSessionId -and
  $desktop.is_default -and
  $desktop.foreground_window_present -and
  $python.supported -and
  $candidate -and
  $candidate.data_leaf_is_exact -and
  $candidate.data_path_reachable -and
  $candidate.source_root_reachable -and
  -not $candidate.reserved_transfer_path -and
  (Test-Path -LiteralPath $startupRoot -PathType Container)
)
$remotePrerequisitesMet = (
  $installationPrerequisitesMet -and
  $power.ac_sleep_disabled -and
  ($SkipPortalHttpsCheck -or $portal.reachable)
)

$result = [ordered]@{
  schema = 1
  inventory = "dreamz_office_payment_runner"
  read_only = $true
  expected_computer_name = $ExpectedComputerName
  computer_name = $computerName
  hostname_matches = $hostnameMatches
  session = [ordered]@{
    current_identity_owns_interactive_desktop = $interactiveOwnerMatches
    current_process_is_active_console = ($currentSessionId -eq $activeConsoleSessionId)
    user_interactive = [Environment]::UserInteractive
    input_desktop = $desktop.name
    input_desktop_is_default = $desktop.is_default
    foreground_window_present = $desktop.foreground_window_present
    desktop_idle_seconds = Get-IdleSeconds
  }
  python = $python
  gym_assistant = $gymAssistant
  startup = [ordered]@{
    startup_folder_available = Test-Path -LiteralPath $startupRoot -PathType Container
    runner_shortcut_exists = Test-Path -LiteralPath $startupLink -PathType Leaf
    install_exists = Test-Path -LiteralPath $installRoot -PathType Container
    receipt_ledger_exists = Test-Path -LiteralPath (Join-Path $installRoot "state\payment-receipts.json") -PathType Leaf
    rollback_history_count = $rollbackCount
  }
  power = $power
  portal_https = $portal
  reserved_transfer = [ordered]@{
    x_drive_is_transfer_only = $true
    x_drive_present = Test-Path -LiteralPath "X:\"
    documented_target = $reservedTransferRoot
  }
  installation_prerequisites_met = [bool]$installationPrerequisitesMet
  unattended_remote_prerequisites_met = [bool]$remotePrerequisitesMet
}

$json = $result | ConvertTo-Json -Depth 8
if ($OutputPath) {
  if ([Management.Automation.WildcardPattern]::ContainsWildcardCharacters($OutputPath)) {
    throw "OutputPath must be one exact JSON file path without wildcards."
  }
  $fullOutputPath = [IO.Path]::GetFullPath($OutputPath)
  $outputDirectory = [IO.Path]::GetDirectoryName($fullOutputPath)
  if (
    -not $outputDirectory -or
    -not (Test-Path -LiteralPath $outputDirectory -PathType Container)
  ) {
    throw "OutputPath must point into an existing directory."
  }
  if (
    -not [string]::Equals(
      [IO.Path]::GetExtension($fullOutputPath),
      ".json",
      [StringComparison]::OrdinalIgnoreCase
    )
  ) {
    throw "OutputPath must have a .json extension."
  }
  $temporaryOutputPath = Join-Path $outputDirectory (
    "." + [IO.Path]::GetFileName($fullOutputPath) + "." +
    [Guid]::NewGuid().ToString("N") + ".tmp"
  )
  try {
    $utf8WithoutBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText(
      $temporaryOutputPath,
      $json + [Environment]::NewLine,
      $utf8WithoutBom
    )
    if (Test-Path -LiteralPath $fullOutputPath -PathType Leaf) {
      [IO.File]::Replace(
        $temporaryOutputPath,
        $fullOutputPath,
        $null,
        $true
      )
    }
    else {
      [IO.File]::Move($temporaryOutputPath, $fullOutputPath)
    }
  }
  finally {
    if (Test-Path -LiteralPath $temporaryOutputPath -PathType Leaf) {
      [IO.File]::Delete($temporaryOutputPath)
    }
  }
}
$json
if (-not $hostnameMatches -and -not $AllowNonOfficeForTesting) {
  exit 3
}
