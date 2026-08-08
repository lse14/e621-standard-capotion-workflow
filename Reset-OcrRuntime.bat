@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\Reset-OcrRuntime.ps1" %*
exit /b %ERRORLEVEL%
