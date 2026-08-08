@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\Clean-TokenBudgetArtifacts.ps1" %*
exit /b %ERRORLEVEL%
