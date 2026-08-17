param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedWindowsUser,
    [string]$TaskName = 'Dreamz Gym Assistant Roster Export',
    [string]$GymAssistantExe = 'C:\Gym Assistant 2.6\Gym Assistant 26.exe',
    [string]$ExpectedDataPath = 'C:\Gym Assistant 2.6\Data',
    [string]$ExportRoot = 'C:\DreamzPortalSync\exports',
    [string]$StateRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantRosterExport",
    [string]$BridgeWorkRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantBridge",
    [datetime]$DailyAt = [datetime]::Today.AddHours(1),
    [int]$MinimumMembers = 4000,
    [double]$MaxCountChangePercent = 15.0,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($env:OS -ne 'Windows_NT') {
    throw 'Deze installer werkt alleen op Windows.'
}

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUserLeaf = ($currentUser -split '\\')[-1]
$interactiveUser = (Get-CimInstance Win32_ComputerSystem).UserName
if ($currentUserLeaf -ine $ExpectedWindowsUser) {
    throw "Verkeerde Windows-gebruiker: $currentUser. Verwacht: $ExpectedWindowsUser."
}
if (-not $interactiveUser -or $interactiveUser -ine $currentUser) {
    throw "De installer moet rechtstreeks draaien als de ingelogde gebruiker $currentUser. Actieve gebruiker: $interactiveUser"
}

$packageRoot = Join-Path $RepoRoot 'scripts\gymassistant_roster_export'
$runner = Join-Path $packageRoot 'roster_export.py'
$wrapper = Join-Path $packageRoot 'Run-GymAssistantRosterExport.ps1'
$credentialPath = Join-Path $StateRoot 'master-access.dpapi'
$target = Join-Path $ExportRoot 'MemberData.csv'

foreach ($required in @($PythonExe, $runner, $wrapper, $GymAssistantExe, $target)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Vereist bestand ontbreekt: $required"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $ExpectedDataPath 'Members.dat') -PathType Leaf)) {
    throw "Het verwachte Gym Assistant-datapad is niet geldig: $ExpectedDataPath"
}
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null

& $PythonExe $runner credential check --path $credentialPath | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'De versleutelde Master Access-invoer ontbreekt of hoort bij een andere Windows-gebruiker.'
}
& $PythonExe $runner validate `
    --candidate $target `
    --minimum-members $MinimumMembers `
    --max-count-change-percent $MaxCountChangePercent | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'De huidige MemberData.csv slaagt niet voor de validatie; taak wordt niet geinstalleerd.'
}

$arguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $wrapper),
    '-PythonExe', ('"{0}"' -f $PythonExe),
    '-RepoRoot', ('"{0}"' -f $RepoRoot),
    '-GymAssistantExe', ('"{0}"' -f $GymAssistantExe),
    '-ExpectedDataPath', ('"{0}"' -f $ExpectedDataPath),
    '-ExportRoot', ('"{0}"' -f $ExportRoot),
    '-StateRoot', ('"{0}"' -f $StateRoot),
    '-BridgeWorkRoot', ('"{0}"' -f $BridgeWorkRoot),
    '-MinimumMembers', $MinimumMembers,
    '-MaxCountChangePercent', $MaxCountChangePercent.ToString([Globalization.CultureInfo]::InvariantCulture)
) -join ' '

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Exports the official Gym Assistant member roster, validates it, and safely publishes MemberData.csv for Portal Sync.' `
    -Force | Out-Null

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName |
    Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns

Write-Host "Taak geinstalleerd voor $currentUser om $($DailyAt.ToString('HH:mm'))." -ForegroundColor Green
Write-Host 'De taak draait alleen in de ingelogde interactieve sessie en blijft ook bij schermvergrendeling beschikbaar.'
