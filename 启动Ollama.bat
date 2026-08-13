@echo off
echo Starting Ollama service (if not already running) ...
echo.

where ollama >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] ollama command not found. Please check Ollama is installed correctly.
    echo Default install path: C:\Users\ÑÇÔó\AppData\Local\Programs\Ollama
    echo.
    pause
    exit /b 1
)

:: Check if service already up
curl -s http://127.0.0.1:11434/api/tags -m 2 >nul 2>&1
if %errorlevel% equ 0 (
    echo Ollama service is already running.
) else (
    echo Launching ollama serve ...
    start "ollama-serve" cmd /c "ollama serve"
    echo Waiting 5 seconds for service to warm up ...
    ping -n 6 127.0.0.1 >nul
)

echo.
echo Available models:
curl -s http://127.0.0.1:11434/api/tags
echo.
echo Done. You can now launch the app with [Æô¶¯.bat]. Press any key to exit.
pause >nul
