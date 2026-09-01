@echo off
setlocal

cd /d "%~dp0"
set "PROJECT_PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PROJECT_PYTHON%" (
    echo [ERROR] Project Python environment was not found.
    echo Run tools\install_dependencies.ps1 first.
    pause
    exit /b 1
)

"%PROJECT_PYTHON%" "%~dp0app.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%" == "0" (
    echo.
    echo [ERROR] The application exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
