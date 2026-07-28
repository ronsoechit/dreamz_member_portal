@echo off
setlocal

set "INVENTORY_DIR=%~dp0"
set "INVENTORY_SCRIPT=%INVENTORY_DIR%Get-DreamzOfficePaymentRunnerInventory.ps1"
set "INVENTORY_OUTPUT=%INVENTORY_DIR%Dreamz-Office-PC-inventarisatie.json"

if not exist "%INVENTORY_SCRIPT%" (
  echo Inventarisatiescript niet gevonden.
  exit /b 2
)

powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%INVENTORY_SCRIPT%" -OutputPath "%INVENTORY_OUTPUT%"
set "INVENTORY_EXIT=%ERRORLEVEL%"

if "%INVENTORY_EXIT%"=="0" (
  echo.
  echo Inventarisatie opgeslagen:
  echo "%INVENTORY_OUTPUT%"
) else (
  echo.
  echo Inventarisatie eindigde met foutcode %INVENTORY_EXIT%.
  echo Controleer de melding hierboven. Een JSON-bestand kan al zijn opgeslagen.
)

exit /b %INVENTORY_EXIT%
