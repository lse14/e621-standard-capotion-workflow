@echo off
setlocal
set "ANIMA_PROJECT_ROOT=%~dp0."
"%ANIMA_PROJECT_ROOT%\.runtime-build\runtimes\core\python.exe" -B -I "%ANIMA_PROJECT_ROOT%\packaging\scripts\ocr_gpu_resource.py" --project-root "%ANIMA_PROJECT_ROOT%" --action reset %*
exit /b %ERRORLEVEL%
