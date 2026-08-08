@echo off
setlocal
set "PIP_NO_INDEX=1"
call "%~dp0Import-OcrResource.bat" -Apply %*
exit /b %ERRORLEVEL%
