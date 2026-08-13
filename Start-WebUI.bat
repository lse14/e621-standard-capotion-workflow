@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\desktop_control.ps1" -Action Start %*
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
    echo.
    echo Start-WebUI failed with exit code %exitCode%.
    echo Check launcher logs under "%~dp0.runtime-build\launcher".
    pause
)
exit /b %exitCode%
