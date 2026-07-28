[CmdletBinding()]
param(
    [string]$TaskName = "Dreamz Portal Sync",
    [string]$RepoDir = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging",
    [string]$RunnerPath = "C:\DreamzPortalSync\run_sync.ps1",
    [string]$BackupRoot = "C:\DreamzPortalSync\backups"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$DiagnosticDir = Join-Path $BackupRoot "sync-traceback-$Timestamp"
$RunnerBackup = Join-Path $DiagnosticDir "run_sync.ps1"
$RunnerCandidate = Join-Path $DiagnosticDir "run_sync.diagnostic.ps1"
$WrapperPath = Join-Path $RepoDir "sync_agent_diagnostic.py"
$TracebackPath = Join-Path $DiagnosticDir "python-traceback.txt"
$PythonPath = Join-Path $RepoDir ".venv\Scripts\python.exe"

function Assert-Leaf {
    param(
        [string]$Path,
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label niet gevonden: $Path"
    }
}

Write-Host "Dreamz Portal Sync - volledige foutdiagnose" -ForegroundColor White

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($task.State -ne "Disabled") {
    throw "De taak moet voor deze diagnose uitgeschakeld zijn. Huidige status: $($task.State)"
}

Assert-Leaf -Path $RunnerPath -Label "Runner"
Assert-Leaf -Path (Join-Path $RepoDir "sync_agent.py") -Label "Sync-agent"
Assert-Leaf -Path $PythonPath -Label "Python-omgeving"

New-Item -ItemType Directory -Path $DiagnosticDir -Force | Out-Null
Copy-Item -LiteralPath $RunnerPath -Destination $RunnerBackup -Force

$runnerText = [System.IO.File]::ReadAllText($RunnerPath)
$agentReferenceCount = [regex]::Matches(
    $runnerText,
    "sync_agent\.py"
).Count
if ($agentReferenceCount -ne 1) {
    throw "De runner bevat $agentReferenceCount verwijzingen naar sync_agent.py; de diagnose is niet veilig gestart."
}

$diagnosticRunnerText = $runnerText.Replace(
    "sync_agent.py",
    "sync_agent_diagnostic.py"
)
$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseInput(
    $diagnosticRunnerText,
    [ref]$tokens,
    [ref]$parseErrors
) | Out-Null
if ($parseErrors.Count -gt 0) {
    throw "De tijdelijke diagnostische runner kwam niet door de syntaxcontrole."
}

[System.IO.File]::WriteAllText(
    $RunnerCandidate,
    $diagnosticRunnerText,
    (New-Object System.Text.UTF8Encoding($false))
)

$wrapper = @'
from __future__ import annotations

import os
from pathlib import Path
import runpy
import traceback


traceback_path = Path(os.environ["CODEX_SYNC_TRACEBACK_PATH"])
sync_agent_path = Path(__file__).with_name("sync_agent.py")

try:
    runpy.run_path(str(sync_agent_path), run_name="__main__")
except BaseException:
    traceback_path.write_text(traceback.format_exc(), encoding="utf-8")
    raise
else:
    traceback_path.write_text("NO EXCEPTION\n", encoding="utf-8")
'@
[System.IO.File]::WriteAllText(
    $WrapperPath,
    $wrapper,
    (New-Object System.Text.UTF8Encoding($false))
)

& $PythonPath -m py_compile $WrapperPath
if ($LASTEXITCODE -ne 0) {
    throw "De diagnostische Python-wrapper kwam niet door de syntaxcontrole."
}

$previousErrorActionPreference = $ErrorActionPreference
$diagnosticExitCode = $null
try {
    Copy-Item -LiteralPath $RunnerCandidate -Destination $RunnerPath -Force
    $env:CODEX_SYNC_TRACEBACK_PATH = $TracebackPath

    Write-Host ""
    Write-Host "De gecontroleerde diagnose wordt uitgevoerd..." -ForegroundColor Cyan
    $ErrorActionPreference = "Continue"
    powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $RunnerPath
    $diagnosticExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
    Copy-Item -LiteralPath $RunnerBackup -Destination $RunnerPath -Force
    if (Test-Path -LiteralPath $WrapperPath -PathType Leaf) {
        Remove-Item -LiteralPath $WrapperPath -Force
    }
    Remove-Item Env:\CODEX_SYNC_TRACEBACK_PATH -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $TracebackPath -PathType Leaf)) {
    throw "De diagnostische run schreef geen tracebackbestand."
}

Write-Host ""
Write-Host "DIAGNOSE KLAAR" -ForegroundColor Green
Write-Host "Exitcode: $diagnosticExitCode"
Write-Host "Runner teruggezet: $RunnerPath"
Write-Host "Diagnosemap: $DiagnosticDir"
Write-Host ""
Get-Content -LiteralPath $TracebackPath
