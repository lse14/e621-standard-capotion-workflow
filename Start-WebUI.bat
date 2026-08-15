@echo off
setlocal
if not exist "%~dp0.runtime-build\manifests\install-state.json" goto :bootstrap

:start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\desktop_control.ps1" -Action Start %*
set "exitCode=%ERRORLEVEL%"
if "%exitCode%"=="0" exit /b 0
goto :failed

:bootstrap
echo Installation state is missing; resuming source bootstrap.
call "%~dp0Install-WebUI.bat"
set "exitCode=%ERRORLEVEL%"
if "%exitCode%"=="0" exit /b 0

:failed
echo.
echo Start-WebUI failed with exit code %exitCode%.
echo Check launcher logs under "%~dp0.runtime-build\launcher".
pause
exit /b %exitCode%
