@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\bootstrap_install.ps1" -ProjectRoot "%~dp0." %*
exit /b %ERRORLEVEL%
