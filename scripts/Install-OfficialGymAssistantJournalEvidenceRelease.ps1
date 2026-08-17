#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$TaskName = "Dreamz Portal Sync",
    [string]$RepoDir = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging",
    [string]$RunnerPath = "C:\DreamzPortalSync\run_sync.ps1",
    [string]$LogRoot = "C:\DreamzPortalSync\logs",
    [string]$ExpectedComputer = "DREAMZ-FRNTDSK",
    [string]$ExpectedUser = "Dreamz Fitness"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$releaseCommit = "508c283c5fe1a6c6f099c16fb27a7aaf95179628"
$rawRoot = "https://raw.githubusercontent.com/ronsoechit/dreamz_member_portal/$releaseCommit"
$syncRoot = "C:\DreamzPortalSync"
$python = Join-Path $RepoDir ".venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stagingRoot = Join-Path $env:TEMP "DreamzJournalEvidence-$stamp-$PID"
$backupRoot = Join-Path $syncRoot "backups\journal-evidence-$stamp"
$backupPayload = Join-Path $backupRoot "payload"
$manifestPath = Join-Path $backupRoot "rollback-manifest.json"
$featureName = "PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED"

$releaseFiles = @(
    [pscustomobject]@{
        Relative = "sync_agent.py"
        Sha256 = "515A9A5A2C2330B74B2D2028D2BEBDDF3B1333E29D4B4526E62405997DDD37DC"
    },
    [pscustomobject]@{
        Relative = "ga_journal_snapshot.py"
        Sha256 = "1A0D7521AD477AE5B25A88D8D5E16F7446CD9449249F775FEACBE807285E78C9"
    },
    [pscustomobject]@{
        Relative = "ga_journal_source_coverage_probe.py"
        Sha256 = "1266A588FB103D031E743AF749AA6F761A465498A9534B38E8885F4A67FD23EA"
    },
    [pscustomobject]@{
        Relative = "gymassistant_journal_export.py"
        Sha256 = "99728DF540FE45D2B6FD4821E85CF1CA23C17F89C41D0E025B4FA28119959885"
    },
    [pscustomobject]@{
        Relative = "portal_sync_journal_evidence.py"
        Sha256 = "F5F831E8BF5F974C7531E8CD35F7EA8573EDEC5DB800B9BA318D43827117903A"
    },
    [pscustomobject]@{
        Relative = "scripts/gymassistant_roster_export/__init__.py"
        Sha256 = "2F9E71512491E6CC712A880E1C91D7A148EF3BF57BA9F108A8DC778CAA4F1026"
    },
    [pscustomobject]@{
        Relative = "scripts/gymassistant_roster_export/roster_export.py"
        Sha256 = "9B177FDEC76625FFD951B39648410AF93771933090843E4F282644B31CCEB847"
    },
    [pscustomobject]@{
        Relative = "scripts/Test-OfficialGymAssistantJournalExport.ps1"
        Sha256 = "6136779E55F4963CAEAE989E5256B64AE098130555B3756C7E139035D5C55A04"
    }
)

$taskDisabled = $false
$backupReady = $false
$changesInstalled = $false
$cryptoReady = $false
$existingFiles = New-Object "System.Collections.Generic.List[string]"
$newFiles = New-Object "System.Collections.Generic.List[string]"

function Write-Step {
    param([string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-PathExists {
    param(
        [string]$Path,
        [string]$Label,
        [string]$PathType
    )

    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) {
        throw "$Label ontbreekt: $Path"
    }
}

function Get-ContainedPath {
    param(
        [string]$Root,
        [string]$Relative
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath(
        (Join-Path $rootFull ($Relative -replace '/', '\'))
    )
    if (-not $candidate.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Releasepad ontsnapt uit de doelmap: $Relative"
    }
    return $candidate
}

function Disable-SyncTaskAndWait {
    $script:taskDisabled = $true
    Disable-ScheduledTask -TaskName $TaskName | Out-Null

    $deadline = (Get-Date).AddMinutes(10)
    do {
        $task = Get-ScheduledTask -TaskName $TaskName
        if ($task.State -ne "Running") {
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "De lopende Portal Sync was na tien minuten nog niet klaar."
}

function Enable-AndStartSyncTask {
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    $script:taskDisabled = $false
}

function Get-LatestSyncLog {
    param([datetime]$NotBefore)

    return Get-ChildItem -LiteralPath $LogRoot -Filter "sync-*.log" -File |
        Where-Object { $_.LastWriteTime -ge $NotBefore.AddSeconds(-5) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Wait-ForSuccessfulTaskRun {
    param(
        [datetime]$PreviousLastRun,
        [datetime]$RequestedAt
    )

    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        $task = Get-ScheduledTask -TaskName $TaskName
        $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
        $finished = (
            $taskInfo.LastRunTime -gt $PreviousLastRun -and
            $task.State -ne "Running"
        )
    } while (-not $finished -and (Get-Date) -lt $deadline)

    if (-not $finished) {
        throw "De controletaak was na tien minuten nog niet afgerond."
    }
    if ([int64]$taskInfo.LastTaskResult -ne 0) {
        throw "De controletaak gaf resultaat $($taskInfo.LastTaskResult)."
    }

    $log = Get-LatestSyncLog -NotBefore $RequestedAt
    if (-not $log) {
        throw "Het log van de controletaak ontbreekt."
    }
    $logText = Get-Content -LiteralPath $log.FullName -Raw
    foreach ($requiredPattern in @(
        '"status"\s*:\s*"success"',
        '"member_snapshot_protocol"\s*:\s*"official_csv_v2"',
        '"members_snapshot_complete"\s*:\s*true',
        '"sync_run_id"\s*:\s*\d+'
    )) {
        if ($logText -notmatch $requiredPattern) {
            Get-Content -LiteralPath $log.FullName -Tail 100
            throw "Het log mist een verplichte normale-synccontrole."
        }
    }
    if ($logText -match "Existing-member journal evidence response") {
        throw "De uitgeschakelde Journal-evidencefeature werd onverwacht uitgevoerd."
    }
    return $log
}

function Restore-PreviousFiles {
    Write-Host ""
    Write-Host "ROLLBACK: vorige Portal Sync-bestanden worden teruggezet." -ForegroundColor Yellow
    Disable-SyncTaskAndWait

    foreach ($relative in $existingFiles) {
        $backup = Get-ContainedPath -Root $backupPayload -Relative $relative
        $target = Get-ContainedPath -Root $RepoDir -Relative $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $backup -Destination $target -Force
    }
    foreach ($relative in $newFiles) {
        $target = Get-ContainedPath -Root $RepoDir -Relative $relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
        }
    }
}

try {
    Write-Host "Dreamz Portal Sync - officiele Journal-evidencecode" -ForegroundColor White
    Write-Host "Release: $releaseCommit"
    Write-Host "De feature blijft uit en deze installer start geen Journal-export."

    Write-Step "Computer, gebruiker, paden en uitgeschakelde feature controleren"
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $expectedIdentity = "$ExpectedComputer\$ExpectedUser"
    if ($env:COMPUTERNAME -ine $ExpectedComputer) {
        throw "Deze installer mag alleen op $ExpectedComputer draaien."
    }
    if ($currentIdentity -ine $expectedIdentity) {
        throw "Deze installer moet als $expectedIdentity draaien; huidig: $currentIdentity."
    }
    foreach ($required in @(
        @{ Path = $RepoDir; Label = "Portal Sync-map"; Type = "Container" },
        @{ Path = $RunnerPath; Label = "Portal Sync-runner"; Type = "Leaf" },
        @{ Path = $LogRoot; Label = "Portal Sync-logmap"; Type = "Container" },
        @{ Path = $python; Label = "Portal Sync-Python"; Type = "Leaf" }
    )) {
        Assert-PathExists -Path $required.Path -Label $required.Label -PathType $required.Type
    }
    Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null

    foreach ($scope in @("Process", "User", "Machine")) {
        $value = [Environment]::GetEnvironmentVariable($featureName, $scope)
        if ($value -eq "true") {
            throw "$featureName staat op $scope-niveau al aan. Installatie geweigerd."
        }
    }
    $runnerText = Get-Content -LiteralPath $RunnerPath -Raw
    if ($runnerText -match '(?im)^\s*\$env:PORTAL_SYNC_EXISTING_MEMBER_JOURNAL_EVIDENCE_ENABLED\s*=\s*["'']true["'']') {
        throw "De runner zet de Journal-evidencefeature al aan. Installatie geweigerd."
    }

    Write-Step "Immutable releasebestanden downloaden en hashes controleren"
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    foreach ($file in $releaseFiles) {
        $download = Get-ContainedPath -Root $stagingRoot -Relative $file.Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $download) -Force | Out-Null
        $urlPath = $file.Relative -replace '\\', '/'
        Invoke-WebRequest -Uri "$rawRoot/$urlPath" -OutFile $download -UseBasicParsing
        $actual = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
        if ($actual -ne $file.Sha256) {
            throw "Hashcontrole mislukt voor $($file.Relative)."
        }
        Write-Host "$($file.Relative): hash OK"
    }

    $pythonFiles = @(
        $releaseFiles |
            Where-Object { $_.Relative.EndsWith(".py", [StringComparison]::OrdinalIgnoreCase) } |
            ForEach-Object { Get-ContainedPath -Root $stagingRoot -Relative $_.Relative }
    )
    & $python -m py_compile @pythonFiles
    if ($LASTEXITCODE -ne 0) {
        throw "De release kwam niet door de Python-syntaxcontrole."
    }

    $operatorScript = Get-ContainedPath `
        -Root $stagingRoot `
        -Relative "scripts/Test-OfficialGymAssistantJournalExport.ps1"
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $operatorScript,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors.Count -gt 0) {
        throw "Het operator-script kwam niet door de PowerShell-syntaxcontrole."
    }

    Write-Step "Portal Sync-taak rustig stoppen en reservekopie maken"
    Disable-SyncTaskAndWait
    New-Item -ItemType Directory -Path $backupPayload -Force | Out-Null
    foreach ($file in $releaseFiles) {
        $target = Get-ContainedPath -Root $RepoDir -Relative $file.Relative
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $backup = Get-ContainedPath -Root $backupPayload -Relative $file.Relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup -Force
            [void]$existingFiles.Add($file.Relative)
        }
        else {
            [void]$newFiles.Add($file.Relative)
        }
    }
    @{
        schema = "dreamz.portal-sync.journal-evidence-rollback.v1"
        release = $releaseCommit
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        existing_files = @($existingFiles)
        new_files = @($newFiles)
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $backupReady = $true
    Write-Host "Reservekopie: $backupRoot"

    Write-Step "Geteste code installeren met de feature uit"
    $changesInstalled = $true
    foreach ($file in $releaseFiles) {
        $source = Get-ContainedPath -Root $stagingRoot -Relative $file.Relative
        $target = Get-ContainedPath -Root $RepoDir -Relative $file.Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Force
        $installed = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($installed -ne $file.Sha256) {
            throw "Hashcontrole na installatie mislukt voor $($file.Relative)."
        }
    }
    Push-Location $RepoDir
    try {
        $installedPythonFiles = @(
            $releaseFiles |
                Where-Object { $_.Relative.EndsWith(".py", [StringComparison]::OrdinalIgnoreCase) } |
                ForEach-Object { $_.Relative -replace '/', '\' }
        )
        & $python -m py_compile @installedPythonFiles
        if ($LASTEXITCODE -ne 0) {
            throw "De geinstalleerde Python-code kwam niet door de syntaxcontrole."
        }
        & $python -c "import sync_agent; import gymassistant_journal_export"
        if ($LASTEXITCODE -ne 0) {
            throw "Een vereiste Python-module ontbreekt of kon niet worden geladen."
        }
        & $python -c "import cryptography" 2>$null
        $cryptoReady = $LASTEXITCODE -eq 0
        & $python sync_agent.py --help | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "De nieuwe sync-agent kon niet veilig worden geladen."
        }
    }
    finally {
        Pop-Location
    }

    Write-Step "Normale automatische sync controleren; geen Journal-export"
    $previous = Get-ScheduledTaskInfo -TaskName $TaskName
    $requestedAt = Get-Date
    Enable-AndStartSyncTask
    $log = Wait-ForSuccessfulTaskRun `
        -PreviousLastRun $previous.LastRunTime `
        -RequestedAt $requestedAt

    Write-Host ""
    Write-Host "INSTALLATIE GESLAAGD - JOURNAL-EVIDENCE BLIJFT UIT" -ForegroundColor Green
    Write-Host "Normale sync: $($log.FullName)"
    Write-Host "Reservekopie: $backupRoot"
    Write-Host "Er is geen Journal-export of ledenwijziging uitgevoerd."
    if ($cryptoReady) {
        Write-Host "Handtekeningdependency: gereed" -ForegroundColor Green
    }
    else {
        Write-Host "Handtekeningdependency ontbreekt; pilotactivering blijft geblokkeerd." -ForegroundColor Yellow
    }
}
catch {
    $failure = $_
    Write-Host ""
    Write-Host "INSTALLATIE MISLUKT: $($failure.Exception.Message)" -ForegroundColor Red
    if ($backupReady -and $changesInstalled) {
        try {
            Restore-PreviousFiles
        }
        catch {
            Write-Host "ROLLBACK MISLUKT: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "Reservekopie: $backupRoot" -ForegroundColor Yellow
            throw $failure
        }
    }
    if ($taskDisabled) {
        try {
            Enable-AndStartSyncTask
            Write-Host "De vorige Portal Sync is opnieuw gestart." -ForegroundColor Yellow
        }
        catch {
            Write-Host "De Portal Sync kon niet automatisch worden herstart." -ForegroundColor Red
        }
    }
    throw $failure
}
finally {
    if (Test-Path -LiteralPath $stagingRoot -PathType Container) {
        $resolvedStaging = [IO.Path]::GetFullPath($stagingRoot)
        $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        $safeLeaf = (Split-Path -Leaf $resolvedStaging) -like "DreamzJournalEvidence-*"
        if (
            $safeLeaf -and
            $resolvedStaging.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)
        ) {
            Remove-Item -LiteralPath $resolvedStaging -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
