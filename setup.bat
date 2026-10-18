@echo off
title Semantic Health Ledger Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ledger.ps1" setup
echo.
if %ERRORLEVEL% NEQ 0 (
    echo *** Setup failed. Check the errors above. ***
) else (
    echo *** Setup complete ***
)
echo.
pause
