#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskName = "Dreamz Portal Sync"
$syncRoot = "C:\DreamzPortalSync"
$repo = Join-Path $syncRoot "dreamz_member_portal-codex-railway-staging"
$runner = Join-Path $syncRoot "run_sync.ps1"
$agent = Join-Path $repo "sync_agent.py"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$logRoot = Join-Path $syncRoot "logs"
$releaseCommit = "a92ae5756f0a5cf1786e0f89ef38a1fe285ff5e3"
$releaseUrl = (
    "https://raw.githubusercontent.com/ronsoechit/" +
    "dreamz_member_portal/$releaseCommit/sync_agent.py"
)
$expectedAgentHash = "8E4E1B093207EDA39377D09498E917B51CAC9D0987B1FD48BC8B908A0459AD67"
$download = Join-Path $env:TEMP "sync_agent-$releaseCommit.py"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $syncRoot "backups\member-sync-timeout-$stamp"
$backupAgent = Join-Path $backupRoot "sync_agent.py"
$manualSucceeded = $false

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Get-LatestSyncLog {
    param([datetime]$NotBefore)

    return Get-ChildItem -LiteralPath $logRoot -Filter "sync-*.log" -File |
        Where-Object { $_.LastWriteTime -ge $NotBefore.AddSeconds(-5) } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Assert-SuccessLog {
    param(
        [System.IO.FileInfo]$Log,
        [string]$RunLabel
    )

    if (-not $Log) {
        throw "$RunLabel heeft geen nieuw sync-logbestand gemaakt."
    }

    $content = Get-Content -LiteralPath $Log.FullName -Raw
    $checks = @{
        "API status success" = $content -match '"status"\s*:\s*"success"'
        "Officiele CSV protocol" = $content -match '"member_snapshot_protocol"\s*:\s*"official_csv_v2"'
        "Complete snapshot" = $content -match '"members_snapshot_complete"\s*:\s*true'
        "Sync run id" = $content -match '"sync_run_id"\s*:\s*\d+'
    }

    $failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value })
    if ($failed.Count -gt 0) {
        Write-Host ""
        Write-Host "Laatste logregels:" -ForegroundColor Yellow
        Get-Content -LiteralPath $Log.FullName -Tail 120
        $labels = ($failed.Name | Sort-Object) -join ", "
        throw "$RunLabel mist verplichte succescontroles: $labels"
    }

    Write-Host "$RunLabel geslaagd: $($Log.FullName)" -ForegroundColor Green
}

Write-Host "Dreamz Portal Sync - member API timeout fix" -ForegroundColor Yellow
Write-Host "Release: $releaseCommit"

Write-Step "Paden, taak en runner controleren"
foreach ($requiredPath in @($repo, $runner, $agent, $python, $logRoot)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Vereist pad ontbreekt: $requiredPath"
    }
}

$task = Get-ScheduledTask -TaskName $taskName
$runnerContent = Get-Content -LiteralPath $runner -Raw
if ($runnerContent -match "sync_agent_diagnostic\.py") {
    throw "De runner verwijst nog naar de tijdelijke diagnose-agent."
}
if ($runnerContent -notmatch "sync_agent\.py") {
    throw "De runner verwijst niet naar sync_agent.py."
}

Write-Step "Taak uitschakelen en controleren dat geen agent actief is"
Disable-ScheduledTask -TaskName $taskName | Out-Null
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$agentProcesses = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -like "*sync_agent.py*" -and
            $_.CommandLine -like "*$repo*"
        }
)
if ($agentProcesses.Count -gt 0) {
    throw "Er draait nog een sync-agent. Wacht tot deze klaar is en voer het script opnieuw uit."
}

Write-Step "Releasebestand downloaden en hash controleren"
Invoke-WebRequest -Uri $releaseUrl -OutFile $download -UseBasicParsing
$actualHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
if ($actualHash -ne $expectedAgentHash) {
    throw "Hashcontrole mislukt. Er wordt niets geinstalleerd."
}

$downloadedContent = Get-Content -LiteralPath $download -Raw
if (
    $downloadedContent -notmatch "DEFAULT_MEMBER_SYNC_API_TIMEOUT_SECONDS\s*=\s*180" -or
    $downloadedContent -notmatch "timeout=member_sync_api_timeout_seconds\(\)"
) {
    throw "Het releasebestand bevat niet de verwachte timeoutfix."
}

Write-Step "Reservekopie maken en nieuwe agent installeren"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $agent -Destination $backupAgent -Force
Copy-Item -LiteralPath $download -Destination $agent -Force

try {
    Write-Step "Python-syntax controleren"
    & $python -m py_compile $agent
    if ($LASTEXITCODE -ne 0) {
        throw "Python-syntaxcontrole is mislukt met exitcode $LASTEXITCODE."
    }

    Write-Step "Gecontroleerde handmatige sync starten; dit kan enkele minuten duren"
    $manualStartedAt = Get-Date
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner
    $manualExitCode = $LASTEXITCODE
    $manualLog = Get-LatestSyncLog -NotBefore $manualStartedAt
    if ($manualExitCode -ne 0) {
        if ($manualLog) {
            Get-Content -LiteralPath $manualLog.FullName -Tail 120
        }
        throw "Handmatige sync is mislukt met exitcode $manualExitCode."
    }
    Assert-SuccessLog -Log $manualLog -RunLabel "Handmatige sync"
    $manualSucceeded = $true

    Write-Step "Scheduled Task inschakelen en een echte taakrun controleren"
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    $previousInfo = Get-ScheduledTaskInfo -TaskName $taskName
    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddMinutes(5)
    $scheduledInfo = $null

    do {
        Start-Sleep -Seconds 5
        $scheduledTask = Get-ScheduledTask -TaskName $taskName
        $scheduledInfo = Get-ScheduledTaskInfo -TaskName $taskName
        $newRunStarted = $scheduledInfo.LastRunTime -gt $previousInfo.LastRunTime
        $newRunFinished = $newRunStarted -and $scheduledTask.State -ne "Running"
    } until ($newRunFinished -or (Get-Date) -ge $deadline)

    if (-not $newRunFinished) {
        throw "De automatische taakrun was na vijf minuten nog niet afgerond."
    }
    if ($scheduledInfo.LastTaskResult -ne 0) {
        throw "De automatische taakrun gaf resultaat $($scheduledInfo.LastTaskResult)."
    }

    $scheduledLog = Get-LatestSyncLog -NotBefore $scheduledInfo.LastRunTime
    Assert-SuccessLog -Log $scheduledLog -RunLabel "Automatische taakrun"

    Write-Step "Eindstatus"
    Get-ScheduledTask -TaskName $taskName |
        Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $taskName |
        Format-List LastRunTime, LastTaskResult, NextRunTime, NumberOfMissedRuns
    Write-Host "UPDATE VOLLEDIG GESLAAGD" -ForegroundColor Green
    Write-Host "Reservekopie: $backupRoot"
}
catch {
    Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue |
        Out-Null
    if (-not $manualSucceeded -and (Test-Path -LiteralPath $backupAgent)) {
        Copy-Item -LiteralPath $backupAgent -Destination $agent -Force
        Write-Host "De vorige agent is teruggezet." -ForegroundColor Yellow
    }
    Write-Host "De Scheduled Task blijft uitgeschakeld." -ForegroundColor Yellow
    throw
}
finally {
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
}
