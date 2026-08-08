@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\Import-OcrResource.ps1" %*
exit /b %ERRORLEVEL%
