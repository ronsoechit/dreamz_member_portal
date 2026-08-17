param(
    [string]$TaskName = 'Dreamz Gym Assistant Roster Export',
    [string]$StateRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantRosterExport",
    [string]$ExportRoot = 'C:\DreamzPortalSync\exports'
)

$ErrorActionPreference = 'Stop'

Write-Host 'SCHEDULED TASK' -ForegroundColor Cyan
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $TaskName |
    Format-List LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns

Write-Host 'PUBLICATIE' -ForegroundColor Cyan
$target = Join-Path $ExportRoot 'MemberData.csv'
if (Test-Path -LiteralPath $target -PathType Leaf) {
    Get-Item -LiteralPath $target | Select-Object FullName,Length,LastWriteTime
    Get-FileHash -LiteralPath $target -Algorithm SHA256 | Select-Object Algorithm,Hash
} else {
    Write-Warning "Geen gepubliceerde CSV gevonden: $target"
}

Write-Host 'LAATSTE RUN' -ForegroundColor Cyan
$statePath = Join-Path $StateRoot 'last-run.json'
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    Get-Content -LiteralPath $statePath -Raw
} else {
    Write-Warning "Nog geen statusbestand gevonden: $statePath"
}

Write-Host 'LAATSTE LOGS' -ForegroundColor Cyan
Get-ChildItem -LiteralPath (Join-Path $StateRoot 'logs') -Filter 'roster-export-*.log' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 5 FullName,Length,LastWriteTime
