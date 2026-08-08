@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\Import-TokenizerResources.ps1" %*
exit /b %ERRORLEVEL%
