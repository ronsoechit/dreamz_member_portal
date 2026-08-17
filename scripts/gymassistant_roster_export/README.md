# Gym Assistant official roster export

This runner automates Gym Assistant's own **Export Members to Excel** flow. It
writes to `MemberData.pending.csv`, validates the official schema and member
population, and only then atomically replaces `MemberData.csv` used by Portal
Sync.

## Safety behavior

- It never reads or writes `Members.dat` directly.
- It refuses to run while another export is active.
- On the frontdesk it pauses the Signup Bridge and waits until the bridge
  confirms `paused` before opening export dialogs.
- Existing Gym Assistant dialogs cause the run to stop without touching the
  current CSV.
- Missing headers, critical parser issues, unknown statuses, duplicate IDs, a
  low member count, or a population jump over the configured threshold reject
  the candidate.
- A rejected or interrupted candidate never replaces the last valid CSV.
- Every completed, rejected, or failed run writes a sanitized `last-run.json`,
  so an old success cannot hide a later export failure.
- The Master Access login/password is encrypted with Windows DPAPI for the
  scheduled-task user and is never printed in logs.

The scheduled task must run as the same interactive Windows user as Gym
Assistant, using **Run only when user is logged on**. A locked screen keeps that
session active; the runner sends native window messages and does not unlock
Windows or use mouse coordinates.

## Local validation

```powershell
python scripts\gymassistant_roster_export\roster_export.py validate `
  --candidate C:\path\to\MemberData.csv `
  --minimum-members 4000
```

## Store Master Access input

Run this once as the same Windows user that will own the scheduled task:

```powershell
python scripts\gymassistant_roster_export\roster_export.py credential set `
  --path "$env:LOCALAPPDATA\Dreamz\GymAssistantRosterExport\master-access.dpapi"
```

No frontdesk installation or production change is performed by this package
until the local end-to-end test has passed and a separate deployment is
approved.

## Prepared scheduled task

`Install-GymAssistantRosterExportTask.ps1` registers a daily 01:00 task for the
current Windows user. It uses an interactive logon token, starts when a missed
run becomes available, retries temporary failures twice, and launches the
PowerShell wrapper hidden. The installer first verifies the DPAPI credential,
the Gym Assistant data path, the existing official CSV, and that it is running
directly as the explicitly named interactive Windows user. The task runs with
normal user rights; it does not require an elevated Gym Assistant process.

Example for the frontdesk profile:

```powershell
.\Install-GymAssistantRosterExportTask.ps1 `
  -PythonExe C:\path\to\python.exe `
  -RepoRoot C:\path\to\dreamz_member_portal `
  -ExpectedWindowsUser "Dreamz Fitness"
```

Use `Get-GymAssistantRosterExportStatus.ps1` to inspect the task, published CSV,
last sanitized state, and recent logs without opening Gym Assistant.
