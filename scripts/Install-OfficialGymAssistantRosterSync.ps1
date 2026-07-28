[CmdletBinding()]
param(
    [string]$TaskName = "Dreamz Portal Sync",
    [string]$RepoDir = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging",
    [string]$RunnerPath = "C:\DreamzPortalSync\run_sync.ps1",
    [string]$CsvPath = "C:\DreamzPortalSync\exports\MemberData.csv",
    [string]$SourceRoot = "C:\Gym Assistant 2.6",
    [int]$MaxCsvAgeHours = 36
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"

$ReleaseCommit = "c015534510df8c78b316018e7ec42b4d413e6cc5"
$RawRoot = "https://raw.githubusercontent.com/ronsoechit/dreamz_member_portal/$ReleaseCommit"
$PythonPath = Join-Path $RepoDir ".venv\Scripts\python.exe"
$BackupRoot = "C:\DreamzPortalSync\backups"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = Join-Path $BackupRoot "official-roster-$Timestamp"
$PayloadDir = Join-Path $BackupDir "payload"
$LogRoot = "C:\DreamzPortalSync\logs"

$ReleaseFiles = @{
    "sync_agent.py" = @{
        Url = "$RawRoot/sync_agent.py"
        Sha256 = "1093378367AFDC1D32A334ACFFB3D530A9AA4DB1EF29F42A441DBD3C2A2A4173"
    }
    "ga_import.py" = @{
        Url = "$RawRoot/ga_import.py"
        Sha256 = "A5E95581586CA8031D4BD0D7303C9D9915AA10D3531700E03DF5574D5E2428B6"
    }
}

$BeginConfigMarker = "# BEGIN CODEX OFFICIAL GYMASSISTANT MEMBER CSV"
$EndConfigMarker = "# END CODEX OFFICIAL GYMASSISTANT MEMBER CSV"
$TaskTouched = $false
$BackupReady = $false
$ChangesInstalled = $false

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
        throw "$Label niet gevonden: $Path"
    }
}

function Disable-SyncTaskAndWait {
    $script:TaskTouched = $true
    Disable-ScheduledTask -TaskName $TaskName | Out-Null

    $deadline = (Get-Date).AddMinutes(10)
    do {
        $task = Get-ScheduledTask -TaskName $TaskName
        if ($task.State -ne "Running") {
            return
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "De lopende portalsync was na 10 minuten nog niet klaar. Er is niets vervangen."
}

function Stop-SyncTaskForRollback {
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2

    $agentProcesses = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*sync_agent.py*" -and
        $_.CommandLine -like "*$RepoDir*"
    }
    foreach ($process in $agentProcesses) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function New-RunnerCandidate {
    param(
        [string]$SourcePath,
        [string]$DestinationPath
    )

    $runnerText = [System.IO.File]::ReadAllText($SourcePath)
    $sourceLines = [regex]::Split($runnerText, "\r?\n")
    $cleanLines = New-Object "System.Collections.Generic.List[string]"
    $insideManagedBlock = $false

    foreach ($line in $sourceLines) {
        if ($line.Trim() -eq $BeginConfigMarker) {
            if ($insideManagedBlock) {
                throw "De CSV-configuratie bevat een dubbel beginblok."
            }
            $insideManagedBlock = $true
            continue
        }
        if ($line.Trim() -eq $EndConfigMarker) {
            if (-not $insideManagedBlock) {
                throw "De CSV-configuratie bevat een eindblok zonder beginblok."
            }
            $insideManagedBlock = $false
            continue
        }
        if (-not $insideManagedBlock) {
            [void]$cleanLines.Add($line)
        }
    }
    if ($insideManagedBlock) {
        throw "De bestaande CSV-configuratie is onvolledig."
    }

    $invokeIndexes = New-Object "System.Collections.Generic.List[int]"
    for ($index = 0; $index -lt $cleanLines.Count; $index++) {
        $line = $cleanLines[$index]
        if (
            ($line -match "^\s*&\s+\`$python\s+") -and
            ($line -match "sync_agent\.py")
        ) {
            [void]$invokeIndexes.Add($index)
        }
    }
    if ($invokeIndexes.Count -ne 1) {
        throw "De sync_agent.py-aanroep in run_sync.ps1 kon niet eenduidig worden gevonden."
    }

    $invokeIndex = $invokeIndexes[0]
    $indent = [regex]::Match($cleanLines[$invokeIndex], "^\s*").Value
    $patchedLines = New-Object "System.Collections.Generic.List[string]"
    for ($index = 0; $index -lt $cleanLines.Count; $index++) {
        if ($index -eq $invokeIndex) {
            [void]$patchedLines.Add($indent + $BeginConfigMarker)
            [void]$patchedLines.Add(
                $indent + '$env:GYM_ASSISTANT_MEMBERS_PATH = "' + $CsvPath + '"'
            )
            [void]$patchedLines.Add(
                $indent + '$env:GYM_ASSISTANT_CSV_MAX_AGE_HOURS = "' + $MaxCsvAgeHours + '"'
            )
            [void]$patchedLines.Add($indent + $EndConfigMarker)
        }
        [void]$patchedLines.Add($cleanLines[$index])
    }

    $patchedText = [string]::Join("`r`n", $patchedLines)
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseInput(
        $patchedText,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors.Count -gt 0) {
        throw "De aangepaste run_sync.ps1 kwam niet door de PowerShell-syntaxcontrole."
    }

    [System.IO.File]::WriteAllText(
        $DestinationPath,
        $patchedText,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Restore-PreviousVersion {
    Write-Host ""
    Write-Host "ROLLBACK: de vorige sync-agent en runner worden teruggezet." -ForegroundColor Yellow
    Stop-SyncTaskForRollback

    foreach ($fileName in $ReleaseFiles.Keys) {
        $backupPath = Join-Path $BackupDir $fileName
        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Copy-Item -LiteralPath $backupPath -Destination (Join-Path $RepoDir $fileName) -Force
        }
    }
    $runnerBackup = Join-Path $BackupDir "run_sync.ps1"
    if (Test-Path -LiteralPath $runnerBackup -PathType Leaf) {
        Copy-Item -LiteralPath $runnerBackup -Destination $RunnerPath -Force
    }

    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "De vorige versie is teruggezet en opnieuw gestart." -ForegroundColor Yellow
}

try {
    Write-Host "Dreamz Portal Sync - officiele GymAssistant ledenlijst" -ForegroundColor White
    Write-Host "Releasecommit: $ReleaseCommit"

    Write-Step "Voorwaarden controleren"
    Assert-PathExists -Path $RepoDir -Label "Synchronisatiemap" -PathType "Container"
    Assert-PathExists -Path $RunnerPath -Label "Runner" -PathType "Leaf"
    Assert-PathExists -Path $CsvPath -Label "Officiele leden-CSV" -PathType "Leaf"
    Assert-PathExists -Path $SourceRoot -Label "GymAssistant-map" -PathType "Container"
    Assert-PathExists -Path $PythonPath -Label "Python-omgeving" -PathType "Leaf"
    Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null

    $csv = Get-Item -LiteralPath $CsvPath
    $csvAge = (Get-Date) - $csv.LastWriteTime
    if ($csvAge.TotalHours -gt $MaxCsvAgeHours) {
        throw (
            "De officiele leden-CSV is {0:N1} uur oud; maximaal toegestaan is {1} uur." -f
            $csvAge.TotalHours,
            $MaxCsvAgeHours
        )
    }
    if ($csv.Length -lt 100000) {
        throw "De officiele leden-CSV is onverwacht klein."
    }

    Write-Step "Releasebestanden downloaden en hashes controleren"
    New-Item -ItemType Directory -Path $PayloadDir -Force | Out-Null
    foreach ($fileName in $ReleaseFiles.Keys) {
        $payloadPath = Join-Path $PayloadDir $fileName
        Invoke-WebRequest `
            -Uri $ReleaseFiles[$fileName].Url `
            -OutFile $payloadPath `
            -UseBasicParsing

        $actualHash = (Get-FileHash -LiteralPath $payloadPath -Algorithm SHA256).Hash
        if ($actualHash -ne $ReleaseFiles[$fileName].Sha256) {
            throw "Hashcontrole mislukt voor $fileName. Er wordt niets geinstalleerd."
        }
        Write-Host "$fileName hash OK"
    }

    & $PythonPath -m py_compile `
        (Join-Path $PayloadDir "ga_import.py") `
        (Join-Path $PayloadDir "sync_agent.py")
    if ($LASTEXITCODE -ne 0) {
        throw "De gedownloade Python-bestanden kwamen niet door de syntaxcontrole."
    }

    Write-Step "Veilige runner-configuratie voorbereiden"
    $runnerCandidate = Join-Path $PayloadDir "run_sync.ps1"
    New-RunnerCandidate -SourcePath $RunnerPath -DestinationPath $runnerCandidate
    Write-Host "Runner-configuratie en PowerShell-syntax zijn geldig."

    Write-Step "Reservekopie maken"
    foreach ($fileName in $ReleaseFiles.Keys) {
        Copy-Item `
            -LiteralPath (Join-Path $RepoDir $fileName) `
            -Destination (Join-Path $BackupDir $fileName) `
            -Force
    }
    Copy-Item -LiteralPath $RunnerPath -Destination (Join-Path $BackupDir "run_sync.ps1") -Force
    $BackupReady = $true
    Write-Host "Reservekopie: $BackupDir"

    Write-Step "Taak tijdelijk uitschakelen en lopende run rustig laten afronden"
    Disable-SyncTaskAndWait
    $unexpectedProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*sync_agent.py*" -and
        $_.CommandLine -like "*$RepoDir*"
    })
    if ($unexpectedProcesses.Count -gt 0) {
        throw "Er draait nog een onverwacht sync_agent.py-proces. De update is niet geinstalleerd."
    }

    Write-Step "Nieuwe agent en CSV-configuratie installeren"
    foreach ($fileName in $ReleaseFiles.Keys) {
        Copy-Item `
            -LiteralPath (Join-Path $PayloadDir $fileName) `
            -Destination (Join-Path $RepoDir $fileName) `
            -Force
    }
    Copy-Item -LiteralPath $runnerCandidate -Destination $RunnerPath -Force
    $ChangesInstalled = $true

    foreach ($fileName in $ReleaseFiles.Keys) {
        $installedHash = (
            Get-FileHash -LiteralPath (Join-Path $RepoDir $fileName) -Algorithm SHA256
        ).Hash
        if ($installedHash -ne $ReleaseFiles[$fileName].Sha256) {
            throw "Hashcontrole na installatie mislukt voor $fileName."
        }
    }

    Push-Location $RepoDir
    try {
        & $PythonPath -m py_compile "ga_import.py" "sync_agent.py"
        if ($LASTEXITCODE -ne 0) {
            throw "De geinstalleerde Python-bestanden kwamen niet door de syntaxcontrole."
        }
        & $PythonPath "sync_agent.py" "--help" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "De nieuwe sync-agent kon niet worden geladen."
        }

        Write-Step "CSV plus live wijzigingslogs lokaal valideren"
        $env:GYM_ASSISTANT_MEMBERS_PATH = $CsvPath
        $env:GYM_ASSISTANT_CSV_MAX_AGE_HOURS = [string]$MaxCsvAgeHours
        $env:CODEX_GYM_ASSISTANT_SOURCE_ROOT = $SourceRoot

        @'
import json
import os
from pathlib import Path

from ga_import import SUPPORTED_BILLING_STATUSES
from sync_agent import (
    member_source_kind,
    official_csv_source_warning,
    parse_member_source_with_live_logs,
)

csv_path = Path(os.environ["GYM_ASSISTANT_MEMBERS_PATH"])
source_root = Path(os.environ["CODEX_GYM_ASSISTANT_SOURCE_ROOT"])
before = csv_path.stat()
result, overlay = parse_member_source_with_live_logs(
    csv_path,
    source_root=source_root,
)
after = csv_path.stat()

member_ids = [str(member.get("member_id") or "").strip() for member in result.members]
critical_issues = [issue for issue in result.issues if issue.critical]
invalid_status_count = sum(
    1
    for member in result.members
    if (
        str(member.get("billing_status") or "").strip().upper()
        not in SUPPORTED_BILLING_STATUSES
        or not isinstance(member.get("is_active"), bool)
        or member.get("is_active")
        != (
            str(member.get("billing_status") or "").strip().upper()
            == "ACTIVE"
        )
    )
)

checks = {
    "source_kind": member_source_kind(csv_path)
    == "gymassistant_official_member_export_csv",
    "source_fresh": official_csv_source_warning(csv_path) is None,
    "source_stable": (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    ),
    "overlay_stable": overlay.get("stable_during_read") is True,
    "minimum_member_count": len(result.members) >= 4000,
    "unique_member_ids": len(member_ids) == len(set(member_ids)),
    "valid_member_ids": all(
        member_id.isdigit() and int(member_id) > 0
        for member_id in member_ids
    ),
    "no_critical_issues": not critical_issues,
    "valid_statuses": invalid_status_count == 0,
}

report = {
    "members_after_overlay": len(result.members),
    "parse_issue_count": len(result.issues),
    "critical_issue_count": len(critical_issues),
    "invalid_status_count": invalid_status_count,
    "overlay": {
        key: overlay.get(key)
        for key in (
            "baseline_count",
            "event_file_count",
            "event_count",
            "added_count",
            "updated_count",
            "deleted_count",
            "orphan_update_count",
            "ignored_before_baseline_count",
            "stable_during_read",
        )
    },
    "checks": checks,
}
print(json.dumps(report, indent=2))
if not all(checks.values()):
    raise SystemExit("LOCAL VALIDATION: FAIL")
print("LOCAL VALIDATION: PASS")
'@ | & $PythonPath -
        if ($LASTEXITCODE -ne 0) {
            throw "De officiele CSV en wijzigingslogs kwamen niet door de lokale validatie."
        }
    }
    finally {
        Pop-Location
    }

    Write-Step "Een gecontroleerde productie-sync starten"
    $previousInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    $previousLastRun = $previousInfo.LastRunTime
    $syncRequestedAt = Get-Date

    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        $task = Get-ScheduledTask -TaskName $TaskName
        $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
        $newRunFinished = (
            $taskInfo.LastRunTime -gt $previousLastRun -and
            $task.State -ne "Running"
        )
    } while (-not $newRunFinished -and (Get-Date) -lt $deadline)

    if (-not $newRunFinished) {
        throw "De eerste productie-sync was na 10 minuten nog niet afgerond."
    }
    if ([int64]$taskInfo.LastTaskResult -ne 0) {
        throw "De eerste productie-sync eindigde met resultaat $($taskInfo.LastTaskResult)."
    }

    $latestLog = Get-ChildItem -LiteralPath $LogRoot -Filter "sync-*.log" -File |
        Where-Object { $_.LastWriteTime -ge $syncRequestedAt.AddMinutes(-1) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latestLog) {
        throw "Het logbestand van de eerste productie-sync kon niet worden gevonden."
    }

    $logText = Get-Content -LiteralPath $latestLog.FullName -Raw
    if ($logText -notmatch '"status"\s*:\s*"success"') {
        throw "De portal bevestigde geen succesvolle sync."
    }
    if ($logText -notmatch '"members_snapshot_complete"\s*:\s*true') {
        throw "De portal bevestigde de ledenlijst niet als complete actuele snapshot."
    }
    if ($logText -notmatch '"member_snapshot_protocol"\s*:\s*"official_csv_v2"') {
        throw "De portal bevestigde niet het verwachte official_csv_v2-protocol."
    }

    Write-Host ""
    Write-Host "UPDATE EN EERSTE SYNC GESLAAGD" -ForegroundColor Green
    Write-Host "Taakstatus: $($task.State)"
    Write-Host "Laatste run: $($taskInfo.LastRunTime)"
    Write-Host "Laatste resultaat: $($taskInfo.LastTaskResult)"
    Write-Host "Logbestand: $($latestLog.FullName)"
    Write-Host "Reservekopie: $BackupDir"
    Write-Host ""
    Get-Content -LiteralPath $latestLog.FullName -Tail 50
}
catch {
    $failure = $_
    Write-Host ""
    Write-Host "UPDATE MISLUKT: $($failure.Exception.Message)" -ForegroundColor Red

    if ($BackupReady -and ($TaskTouched -or $ChangesInstalled)) {
        try {
            Restore-PreviousVersion
        }
        catch {
            Write-Host "AUTOMATISCHE ROLLBACK MISLUKT: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "Reservekopie: $BackupDir" -ForegroundColor Yellow
        }
    }
    elseif ($TaskTouched) {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }

    throw $failure
}
