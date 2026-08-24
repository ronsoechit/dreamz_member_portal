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

$releaseCommit = '9aaef73ec2e920afe85334d8e56ce47120c496e4'
$repository = 'ronsoechit/dreamz_member_portal'
$payload = @(
    @{ Relative = 'scripts/gymassistant_roster_export/__init__.py'; Hash = '2F9E71512491E6CC712A880E1C91D7A148EF3BF57BA9F108A8DC778CAA4F1026' },
    @{ Relative = 'scripts/gymassistant_roster_export/__main__.py'; Hash = '67BDF68DFF9DE7FC109A1B9220F47205CE47ABA7FCF2014693DEDEAE17EBB9F0' },
    @{ Relative = 'scripts/gymassistant_roster_export/roster_export.py'; Hash = '80EE1721DCED2248CA8A671C4D5FF6CA441828FC94420B2E5521E015B54BC228' },
    @{ Relative = 'scripts/gymassistant_roster_export/Invoke-GymAssistantDialogButton.ps1'; Hash = '7E681FF66F17FD3E055E4F71D8CE3C5803C48CD6B8F62FA8AF4B882143846D71' },
    @{ Relative = 'scripts/gymassistant_roster_export/Run-GymAssistantRosterExport.ps1'; Hash = 'C09577A8128C57B00A431E105B1F42E4628B53D58176ECBA36331A548090702C' },
    @{ Relative = 'scripts/gymassistant_roster_export/Install-GymAssistantRosterExportTask.ps1'; Hash = 'EAE42F96D17B4DE7404AA26A750D8BDB7E04D9D99D8D74D86F1200C4B5A9A080' },
    @{ Relative = 'scripts/gymassistant_roster_export/Get-GymAssistantRosterExportStatus.ps1'; Hash = '7B201E9EEAA90BDDA3BB0625B512F2ADF43855312A8B51BE7BD2940939423EFA' },
    @{ Relative = 'scripts/gymassistant_roster_export/README.md'; Hash = 'F4F23B6F4B4FD06CBD48E801165C0BD2EBC3187FCB16810BD90A47E95934BD8C' }
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
