@echo off
setlocal
if /I "%~1"=="/?" goto :usage
if /I "%~1"=="-Help" goto :usage
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\scripts\desktop_control.ps1" -Action Install %*
exit /b %ERRORLEVEL%

:usage
echo Usage: Install-WebUI.bat [-OcrMode None^|Cpu^|Gpu]
exit /b 0
