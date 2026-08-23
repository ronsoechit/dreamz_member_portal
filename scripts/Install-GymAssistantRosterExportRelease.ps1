param(
    [string]$ExpectedWindowsUser = 'Dreamz Fitness',
    [string]$RepoRoot = 'C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging',
    [string]$PythonExe = 'C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging\.venv\Scripts\python.exe',
    [string]$GymAssistantExe = 'C:\Gym Assistant 2.6\Gym Assistant 26.exe',
    [string]$ExpectedDataPath = 'C:\Gym Assistant 2.6\Data',
    [string]$ExportRoot = 'C:\DreamzPortalSync\exports',
    [string]$BackupRoot = 'C:\DreamzPortalSync\backups',
    [string]$StateRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantRosterExport",
    [string]$BridgeWorkRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantBridge",
    [string]$TaskName = 'Dreamz Gym Assistant Roster Export',
    [datetime]$DailyAt = [datetime]::Today.AddHours(1)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest

$releaseCommit = '0c275afd6f054edb4506f4f6020bc5b7e8250a0c'
$repository = 'ronsoechit/dreamz_member_portal'
$payload = @(
    @{ Relative = 'scripts/gymassistant_roster_export/__init__.py'; Hash = 'BE3B605AE7CFCEAC733E4FC67B093678B66FDF5C43306E4C927573B21BC7053F' },
    @{ Relative = 'scripts/gymassistant_roster_export/__main__.py'; Hash = 'B3723CAD06CA89B4C18E311C0452502EA6B3291792BC7C011274DA48E8A90866' },
    @{ Relative = 'scripts/gymassistant_roster_export/roster_export.py'; Hash = '0EC9CD491C379CCDDDFEEFBAF0131381BDBFA43CE8546A846757D9A852228E24' },
    @{ Relative = 'scripts/gymassistant_roster_export/Invoke-GymAssistantDialogButton.ps1'; Hash = 'CFA5849D3BF8C5B55AA0AF9DDAC63288E2BAE28CD4778835F0B2570CD007AD94' },
    @{ Relative = 'scripts/gymassistant_roster_export/Run-GymAssistantRosterExport.ps1'; Hash = '055A224033657FB851B251ED304A14FAF54F63CA4188566AF86245569DF00B6F' },
    @{ Relative = 'scripts/gymassistant_roster_export/Install-GymAssistantRosterExportTask.ps1'; Hash = '52732C1B4504AFD6509960A914077BEF75CECC1ECEAA3456CC8B6FED355E8BA8' },
    @{ Relative = 'scripts/gymassistant_roster_export/Get-GymAssistantRosterExportStatus.ps1'; Hash = 'AD6EA691E4E549E0DD03CCA54A7BC70E93BE809F9E9B2320D17FE68D1E5501F1' },
    @{ Relative = 'scripts/gymassistant_roster_export/README.md'; Hash = 'E7545B700985EBBEF79747C8597171E8ED856F57327320749E99E91E08EF1BB1' }
)

function Assert-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Child
    )
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $childPath = [IO.Path]::GetFullPath($Child)
    if (-not $childPath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Pad valt buiten de toegestane map: $childPath"
    }
}

Write-Host 'Dreamz Gym Assistant ledenexport - gecontroleerde installatie' -ForegroundColor Cyan
Write-Host "Release: $releaseCommit"

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentUserLeaf = ($currentUser -split '\\')[-1]
$interactiveUser = (Get-CimInstance Win32_ComputerSystem).UserName
if ($env:COMPUTERNAME -ine 'DREAMZ-FRNTDSK') {
    throw "Verkeerde computer: $env:COMPUTERNAME. Verwacht: DREAMZ-FRNTDSK."
}
if ($currentUserLeaf -ine $ExpectedWindowsUser) {
    throw "Verkeerde Windows-gebruiker: $currentUser. Verwacht: $ExpectedWindowsUser."
}
if (-not $interactiveUser -or $interactiveUser -ine $currentUser) {
    throw "De installer moet rechtstreeks draaien als de actieve gebruiker $currentUser. Actieve gebruiker: $interactiveUser"
}

$targetPackage = Join-Path $RepoRoot 'scripts\gymassistant_roster_export'
$taskInstaller = Join-Path $targetPackage 'Install-GymAssistantRosterExportTask.ps1'
$credentialPath = Join-Path $StateRoot 'master-access.dpapi'
$bridgeStatePath = Join-Path $BridgeWorkRoot 'agent-state.json'
$bridgeEnabledPath = Join-Path $BridgeWorkRoot 'enabled.flag'
$officialCsv = Join-Path $ExportRoot 'MemberData.csv'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stagingRoot = Join-Path $env:TEMP "DreamzRosterExportRelease-$stamp-$PID"
$stagingPackage = Join-Path $stagingRoot 'scripts\gymassistant_roster_export'
$packageBackup = Join-Path $BackupRoot "roster-export-package-$stamp"
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingTaskXml = if ($existingTask) { Export-ScheduledTask -TaskName $TaskName } else { $null }
$credentialExisted = Test-Path -LiteralPath $credentialPath -PathType Leaf
$packageExisted = Test-Path -LiteralPath $targetPackage -PathType Container
$packageChanged = $false
$taskChanged = $false

Assert-ContainedPath -Root $RepoRoot -Child $targetPackage
Assert-ContainedPath -Root $env:TEMP -Child $stagingRoot
Assert-ContainedPath -Root $BackupRoot -Child $packageBackup

foreach ($required in @($RepoRoot, $PythonExe, $GymAssistantExe, $officialCsv)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Vereist pad ontbreekt: $required"
    }
}
foreach ($required in @(
    (Join-Path $RepoRoot 'ga_import.py'),
    (Join-Path $ExpectedDataPath 'Members.dat'),
    $bridgeStatePath,
    $bridgeEnabledPath
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Vereist bestand ontbreekt: $required"
    }
}

$bridgeState = Get-Content -LiteralPath $bridgeStatePath -Raw | ConvertFrom-Json
$bridgeAt = [DateTimeOffset]::Parse([string]$bridgeState.at)
$bridgeAge = [DateTimeOffset]::Now - $bridgeAt
if ($bridgeAge.TotalMinutes -gt 5) {
    throw "Signup Bridge-status is te oud ($([math]::Round($bridgeAge.TotalMinutes, 1)) minuten). Installatie gestopt."
}

try {
    Write-Host '1. Releasebestanden downloaden en hashes controleren...'
    New-Item -ItemType Directory -Path $stagingPackage -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'ga_import.py') -Destination (Join-Path $stagingRoot 'ga_import.py')
    foreach ($item in $payload) {
        $name = Split-Path -Leaf $item.Relative
        $destination = Join-Path $stagingPackage $name
        $url = "https://raw.githubusercontent.com/$repository/$releaseCommit/$($item.Relative)"
        Invoke-WebRequest -Uri $url -OutFile $destination -UseBasicParsing
        $actual = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        if ($actual -ne $item.Hash) {
            throw "Hashcontrole mislukt voor $name."
        }
        Write-Host "   $name OK"
    }

    $stagingRunner = Join-Path $stagingPackage 'roster_export.py'
    $stagingPowerShell = Get-ChildItem -LiteralPath $stagingPackage -Filter '*.ps1' -File
    foreach ($file in $stagingPowerShell) {
        $tokens = $null
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {
            throw "PowerShell-syntaxfout in $($file.Name): $($errors.Message -join '; ')"
        }
    }
    & $PythonExe -m py_compile $stagingRunner
    if ($LASTEXITCODE -ne 0) {
        throw 'Python-syntaxcontrole is mislukt.'
    }
    & $PythonExe $stagingRunner validate --candidate $officialCsv --minimum-members 4000 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'De huidige officiele CSV slaagt niet voor de validatie.'
    }

    Write-Host '2. Huidige pakket veilig back-uppen...'
    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    if ($packageExisted) {
        Copy-Item -LiteralPath $targetPackage -Destination $packageBackup -Recurse
    }

    Write-Host '3. Gecontroleerd pakket installeren...'
    $packageChanged = $true
    New-Item -ItemType Directory -Path $targetPackage -Force | Out-Null
    foreach ($item in $payload) {
        $name = Split-Path -Leaf $item.Relative
        Copy-Item -LiteralPath (Join-Path $stagingPackage $name) -Destination (Join-Path $targetPackage $name) -Force
    }

    Write-Host '4. Versleutelde Master Access-invoer controleren...'
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    if ($credentialExisted) {
        & $PythonExe (Join-Path $targetPackage 'roster_export.py') credential check --path $credentialPath | Out-Null
    } else {
        Write-Host '   Voer de Master Access-invoer tweemaal in. De invoer blijft onzichtbaar.' -ForegroundColor Yellow
        & $PythonExe (Join-Path $targetPackage 'roster_export.py') credential set --path $credentialPath | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'De versleutelde Master Access-invoer kon niet worden opgeslagen of gecontroleerd.'
    }

    Write-Host '5. Dagelijkse taak installeren zonder deze nu te starten...'
    & $taskInstaller `
        -PythonExe $PythonExe `
        -RepoRoot $RepoRoot `
        -ExpectedWindowsUser $ExpectedWindowsUser `
        -TaskName $TaskName `
        -GymAssistantExe $GymAssistantExe `
        -ExpectedDataPath $ExpectedDataPath `
        -ExportRoot $ExportRoot `
        -StateRoot $StateRoot `
        -BridgeWorkRoot $BridgeWorkRoot `
        -DailyAt $DailyAt
    if ($LASTEXITCODE -ne 0) {
        throw 'De geplande taak kon niet worden geinstalleerd.'
    }
    $taskChanged = $true

    $task = Get-ScheduledTask -TaskName $TaskName
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    if ($task.State -notin @('Ready', 'Disabled')) {
        throw "Onverwachte taakstatus na installatie: $($task.State)"
    }
    $taskUser = [string]$task.Principal.UserId
    $taskUserLeaf = ($taskUser -split '\\')[-1]
    if ($taskUser -ine $currentUser -and $taskUserLeaf -ine $currentUserLeaf) {
        throw "De taak is aan de verkeerde Windows-gebruiker gekoppeld: $($task.Principal.UserId)"
    }
    if ([string]$task.Principal.LogonType -ine 'Interactive') {
        throw "De taak heeft een onverwacht aanmeldingstype: $($task.Principal.LogonType)"
    }
    if ([string]$task.Principal.RunLevel -ine 'Limited') {
        throw "De taak heeft een onverwacht uitvoerniveau: $($task.Principal.RunLevel)"
    }
    if ($task.Actions.Count -ne 1 -or $task.Actions[0].Execute -ine 'powershell.exe') {
        throw 'De taak heeft een onverwachte actie.'
    }
    if ([string]$task.Actions[0].Arguments -match '(?i)(?:^|\s)-RunNow(?:\s|$)') {
        throw 'De taakactie bevat onverwacht een directe-startoptie.'
    }
    $taskStart = [datetime]::Parse([string]$task.Triggers[0].StartBoundary)
    if ($taskStart.Hour -ne $DailyAt.Hour -or $taskStart.Minute -ne $DailyAt.Minute) {
        throw "De taak staat niet op het verwachte tijdstip: $($taskStart.ToString('HH:mm'))"
    }

    Write-Host ''
    Write-Host 'INSTALLATIE GESLAAGD - ER IS GEEN EXPORT GESTART' -ForegroundColor Green
    [pscustomobject]@{
        TaskName = $task.TaskName
        State = $task.State
        NextRunTime = $taskInfo.NextRunTime
        WindowsUser = $currentUser
        Release = $releaseCommit
        PackageBackup = if ($packageExisted) { $packageBackup } else { $null }
    } | Format-List
} catch {
    Write-Host "Installatie mislukt; rollback wordt uitgevoerd: $($_.Exception.Message)" -ForegroundColor Red
    if ($taskChanged -or (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        if ($existingTaskXml) {
            Register-ScheduledTask -TaskName $TaskName -Xml $existingTaskXml -Force | Out-Null
        } else {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        }
    }
    if ($packageChanged) {
        if (Test-Path -LiteralPath $targetPackage) {
            Remove-Item -LiteralPath $targetPackage -Recurse -Force
        }
        if ($packageExisted -and (Test-Path -LiteralPath $packageBackup -PathType Container)) {
            Copy-Item -LiteralPath $packageBackup -Destination $targetPackage -Recurse
        }
    }
    if (-not $credentialExisted -and (Test-Path -LiteralPath $credentialPath -PathType Leaf)) {
        Remove-Item -LiteralPath $credentialPath -Force
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}
