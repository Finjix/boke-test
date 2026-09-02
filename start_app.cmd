@echo off
setlocal

cd /d "%~dp0"
set "BOOTSTRAP_SCRIPT=%~dp0tools\install_dependencies.ps1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PROJECT_PYTHON=%~dp0runtime\python3.13.15\python.exe"
set "PROJECT_READY=%~dp0runtime\python3.13.15\.project-ready"
set "FFMPEG_BIN=%~dp0tools\ffmpeg\bin\ffmpeg.exe"
set "FFPROBE_BIN=%~dp0tools\ffmpeg\bin\ffprobe.exe"

if not exist "%BOOTSTRAP_SCRIPT%" (
    echo [ERROR] Dependency bootstrap script was not found.
    echo Expected: %BOOTSTRAP_SCRIPT%
    pause
    exit /b 1
)
if not exist "%POWERSHELL%" (
    echo [ERROR] Windows PowerShell was not found.
    echo Expected: %POWERSHELL%
    pause
    exit /b 1
)

if not exist "%PROJECT_READY%" goto bootstrap
if not exist "%PROJECT_PYTHON%" goto bootstrap
if not exist "%FFMPEG_BIN%" goto bootstrap
if not exist "%FFPROBE_BIN%" goto bootstrap
goto run

:bootstrap
echo Preparing project-local Python, FFmpeg and Python dependencies...
"%POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_SCRIPT%"
if errorlevel 1 (
    echo.
    echo [ERROR] Project dependency bootstrap failed.
    pause
    exit /b 1
)

if not exist "%PROJECT_PYTHON%" (
    echo [ERROR] Project-local Python 3.13.15 was not found after bootstrap.
    pause
    exit /b 1
)

:run
"%PROJECT_PYTHON%" "%~dp0app.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%" == "0" (
    echo.
    echo [ERROR] The application exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
