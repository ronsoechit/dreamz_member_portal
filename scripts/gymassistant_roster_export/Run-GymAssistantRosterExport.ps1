param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [string]$GymAssistantExe = 'C:\Gym Assistant 2.6\Gym Assistant 26.exe',
    [string]$ExpectedDataPath = 'C:\Gym Assistant 2.6\Data',
    [string]$ExportRoot = 'C:\DreamzPortalSync\exports',
    [string]$StateRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantRosterExport",
    [string]$BridgeWorkRoot = "$env:LOCALAPPDATA\Dreamz\GymAssistantBridge",
    [int]$MinimumMembers = 4000,
    [double]$MaxCountChangePercent = 15.0
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$moduleRoot = Join-Path $RepoRoot 'scripts\gymassistant_roster_export'
$runner = Join-Path $moduleRoot 'roster_export.py'
$candidate = Join-Path (Join-Path $ExportRoot 'pending') 'MemberData.csv'
$target = Join-Path $ExportRoot 'MemberData.csv'
$backupDir = Join-Path $ExportRoot 'history'
$statePath = Join-Path $StateRoot 'last-run.json'
$credentialPath = Join-Path $StateRoot 'master-access.dpapi'
$logDir = Join-Path $StateRoot 'logs'

foreach ($required in @($PythonExe, $runner)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Vereist bestand ontbreekt: $required"
    }
}
foreach ($directory in @($ExportRoot, $backupDir, $StateRoot, $logDir)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logPath = Join-Path $logDir "roster-export-$stamp.log"
New-Item -ItemType File -Path $logPath -Force | Out-Null
"$(Get-Date -Format o) Gym Assistant roster export started." |
    Add-Content -LiteralPath $logPath -Encoding UTF8

try {
    & $PythonExe $runner export `
        --gymassistant-exe $GymAssistantExe `
        --expected-data-path $ExpectedDataPath `
        --candidate $candidate `
        --target $target `
        --backup-dir $backupDir `
        --state-path $statePath `
        --credential-path $credentialPath `
        --bridge-work-root $BridgeWorkRoot `
        --minimum-members $MinimumMembers `
        --max-count-change-percent $MaxCountChangePercent *>&1 |
        Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "De officiele ledenexport is veilig gestopt (exitcode $LASTEXITCODE)."
    }
    "$(Get-Date -Format o) Gym Assistant roster export completed successfully." |
        Add-Content -LiteralPath $logPath -Encoding UTF8
} catch {
    "$(Get-Date -Format o) Gym Assistant roster export failed: $($_.Exception.Message)" |
        Add-Content -LiteralPath $logPath -Encoding UTF8
    throw
} finally {
    Get-ChildItem -LiteralPath $logDir -Filter 'roster-export-*.log' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 60 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
