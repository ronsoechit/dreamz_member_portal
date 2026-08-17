#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$RepoDir = "C:\DreamzPortalSync\dreamz_member_portal-codex-railway-staging",
    [string]$SourceRoot = "C:\Gym Assistant 2.6",
    [string]$ExpectedComputer = "DREAMZ-FRNTDSK",
    [string]$ExpectedUser = "Dreamz Fitness",
    [switch]$ExecuteOfficialExport
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step {
    param([string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

$python = Join-Path $RepoDir ".venv\Scripts\python.exe"
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$expectedIdentity = "$ExpectedComputer\$ExpectedUser"

if ($env:COMPUTERNAME -ine $ExpectedComputer) {
    throw "Deze controle mag alleen op $ExpectedComputer draaien."
}
if ($currentIdentity -ine $expectedIdentity) {
    throw "Deze controle moet als $expectedIdentity draaien; huidig: $currentIdentity."
}
foreach ($requiredPath in @(
    $RepoDir,
    $python,
    (Join-Path $SourceRoot "Data\Journal.dat"),
    (Join-Path $SourceRoot "Gym Assistant 26.exe")
)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Vereist pad ontbreekt: $requiredPath"
    }
}

$env:CODEX_JOURNAL_EXPORT_SOURCE_ROOT = $SourceRoot
Push-Location $RepoDir
try {
    Write-Step "Read-only bronbinding controleren"
    $preflight = @'
import json
import os
from pathlib import Path

from ga_journal_source_coverage_probe import probe_journal_source_coverage

source_root = Path(os.environ["CODEX_JOURNAL_EXPORT_SOURCE_ROOT"])
result = probe_journal_source_coverage(source_root / "Data")
print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
if result.get("status") != "ready" or result.get("export_preflight_ready") is not True:
    raise SystemExit(2)
'@ | & $python -
    if ($LASTEXITCODE -ne 0) {
        throw "De read-only broncontrole is geblokkeerd. Er is niets geexporteerd."
    }
    Write-Output $preflight

    if (-not $ExecuteOfficialExport) {
        Write-Host ""
        Write-Host "PREFLIGHT GESLAAGD - GEEN EXPORT GESTART" -ForegroundColor Green
        Write-Host "Gebruik -ExecuteOfficialExport alleen in een rustig tijdvenster."
        return
    }

    Write-Step "Eenmalige officiele Export Journal uitvoeren"
    $report = @'
import json
import os
from pathlib import Path

from gymassistant_journal_export import (
    export_official_journal_snapshot,
    load_journal_export_config,
)

source_root = Path(os.environ["CODEX_JOURNAL_EXPORT_SOURCE_ROOT"])
config = load_journal_export_config(source_root)
snapshot = export_official_journal_snapshot(
    source_root=source_root,
    executable=config.executable,
    expected_data_path=config.expected_data_path,
    candidate_path=config.candidate_path,
    credential_path=config.credential_path,
    bridge_work_root=config.bridge_work_root,
    source_coverage_proven=True,
)
result = {
    "schema": "dreamz.ga.official-journal-export-test.v1",
    "status": "passed",
    "source_kind": "gym_assistant_official_journal_export",
    "source": {
        "byte_length": snapshot.byte_length,
        "source_sha256": snapshot.source_sha256,
        "stable_read_count": snapshot.stable_read_count,
        "locator_fingerprint_sha256": snapshot.locator_fingerprint_sha256,
        "data_path_fingerprint_sha256": snapshot.data_path_fingerprint_sha256,
        "source_binding_sha256": snapshot.source_binding_sha256,
    },
    "coverage": snapshot.coverage.as_sanitized_dict(),
    "temporary_export_removed": not config.candidate_path.exists(),
}
print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
if not result["coverage"]["complete"] or not result["temporary_export_removed"]:
    raise SystemExit(3)
'@ | & $python -
    if ($LASTEXITCODE -ne 0) {
        throw "De officiele Journal-exportcontrole is geblokkeerd. Er is niets gepubliceerd."
    }
    Write-Output $report

    Write-Host ""
    Write-Host "OFFICIELE JOURNAL-EXPORTCONTROLE GESLAAGD" -ForegroundColor Green
    Write-Host "Er is geen Portal-aanvraag verwerkt en geen lid gewijzigd."
}
finally {
    Remove-Item Env:CODEX_JOURNAL_EXPORT_SOURCE_ROOT -ErrorAction SilentlyContinue
    Pop-Location
}
