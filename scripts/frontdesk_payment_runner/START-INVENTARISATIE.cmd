@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-DreamzFrontdeskPaymentRunnerInventory.ps1"
endlocal
