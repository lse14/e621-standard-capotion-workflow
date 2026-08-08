@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\desktop_control.ps1" -Action Start %*
exit /b %ERRORLEVEL%
