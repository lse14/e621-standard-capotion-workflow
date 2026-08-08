@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\Reset-TokenBudgetRuntime.ps1" %*
exit /b %ERRORLEVEL%
