@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title AI Novel Studio - Install Dependencies

rem ============ Prep: log file ============
set "APP_DIR=%~dp0"
set "NOW_D=%date%"
set "NOW_T=%time%"
set "NOW_D=%NOW_D:/=-%"
set "NOW_D=%NOW_D:\=-%"
set "NOW_T=%NOW_T::=%"
set "NOW_T=%NOW_T:.=%"
set "NOW_T=%NOW_T: =0%"
set "LOG_FILE=%APP_DIR%install_%NOW_D:~0,10%_%NOW_T:~0,6%.log"
if exist "%LOG_FILE%" del "%LOG_FILE%" >nul 2>&1
call :LOG "============================================================"
call :LOG "    AI Novel Studio - Install Dependencies"
call :LOG "============================================================"
call :LOG "Log file : %LOG_FILE%"
call :LOG ""
call :LOG "[INFO] Python 3.8+ is REQUIRED for chromadb / sentence-transformers."

rem ============ 1/3: find suitable python ============
set "PY_CMD="

call :LOG ""
call :LOG "[1/3] Detecting Python Environment (>= 3.8 required) ..."
call :LOG ""

for %%P in (
  "D:\Anaconda3\envs\chatglm3\python.exe"
  "D:\Anaconda3\pkgs\python-3.11.15-hb00fc5c_1\python.exe"
  "D:\Anaconda3\pkgs\python-3.10.20-hb00fc5c_1\python.exe"
  "D:\Anaconda3\pkgs\python-3.10.20-h1044e36_0\python.exe"
  "D:\Anaconda3\pkgs\python-3.10.11-he1021f5_3\python.exe"
  "D:\Anaconda3\pkgs\python-3.9.23-h716150d_0\python.exe"
  "D:\Anaconda3\python.exe"
  "C:\ProgramData\anaconda3\python.exe"
  "C:\Users\ÑÇÔó\anaconda3\python.exe"
) do (
  if exist "%%~P" (
    call :CHECK_PY_VER "%%~P"
    if !errorlevel! equ 0 (
      call :LOG "   candidate %%~P  ->  OK  (>= 3.8)"
      set "PY_CMD=%%~P"
      goto :PY_FOUND
    ) else (
      call :LOG "   candidate %%~P  ->  SKIP (too old, < 3.8)"
    )
  )
)

where python >nul 2>&1
if !errorlevel! equ 0 (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if exist "%%i" (
      call :CHECK_PY_VER "%%i"
      if !errorlevel! equ 0 (
        call :LOG "   PATH candidate %%i  ->  OK  (>= 3.8)"
        set "PY_CMD=%%i"
        goto :PY_FOUND
      ) else (
        call :LOG "   PATH candidate %%i  ->  SKIP (too old, < 3.8)"
      )
    )
  )
)

call :LOG ""
call :LOG "[FATAL] No usable Python (>= 3.8) found!"
call :LOG "        Recommended env: D:\Anaconda3\envs\chatglm3\python.exe (Python 3.10)"
echo.
echo [FATAL] No suitable Python ^>= 3.8 found. See log: %LOG_FILE%
echo.
pause
exit /b 1

:PY_FOUND

rem ============ 2/3: confirm python ============
call :LOG ""
call :LOG "[2/3] Using Python : %PY_CMD%"
"%PY_CMD%" --version >> "%LOG_FILE%" 2>&1
for /f "tokens=*" %%v in ('"%PY_CMD%" -c "import sys;print(sys.version.split()[0])" 2^>nul') do call :LOG "[2/3] Python version  : %%v"
call :LOG ""

rem ============ 3/3: pip install ============
call :LOG "[3/3] Installing pip packages. First run: 5-15 min (chromadb + ST are large)"
call :LOG "      Using mirror: https://pypi.tuna.tsinghua.edu.cn/simple  (faster in China)"
call :LOG ""

cd /d "%APP_DIR%"

call :LOG "--- Step: upgrade pip ---"
"%PY_CMD%" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
  call :LOG "   [WARN] pip upgrade failed; continue with current pip ..."
) else (
  call :LOG "   OK"
)

call :LOG ""
call :LOG "--- Step: install requirements.txt (Tsinghua mirror) ---"
"%PY_CMD%" -m pip install -r "%APP_DIR%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple >> "%LOG_FILE%" 2>&1
set RC=%errorlevel%

if %RC% neq 0 (
  call :LOG "   Mirror install FAILED (rc=%RC%). Retry WITHOUT mirror as fallback ..."
  "%PY_CMD%" -m pip install -r "%APP_DIR%requirements.txt" >> "%LOG_FILE%" 2>&1
  set RC2=!errorlevel!
  if !RC2! neq 0 (
    call :LOG "[FATAL] pip install failed (rc=!RC2!)."
    call :LOG "        Please check log: %LOG_FILE%"
    call :LOG ""
    call :LOG "Manual workaround (PowerShell / CMD):"
    call :LOG "  ^& "%PY_CMD%" -m pip install -r "%APP_DIR%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple"
    echo.
    echo [ERROR] Install failed. Please check log: %LOG_FILE%
    echo.
    echo Manual command (PowerShell):
    echo   ^& "%PY_CMD%" -m pip install -r "%APP_DIR%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
  )
)

call :LOG ""
call :LOG "============================================================"
call :LOG "  Install COMPLETE! Double-click [Æô¶¯.bat] to launch."
call :LOG "============================================================"
call :LOG ""
echo.
echo ============================================================
echo   Install COMPLETE! Double-click [Æô¶¯.bat] to launch.
echo ============================================================
echo.
echo Full log saved to: %LOG_FILE%
pause
exit /b 0

rem ============ Subroutines ============

rem --- :CHECK_PY_VER <py>  ->  0 ok (>=3.8)   2 old/broken
:CHECK_PY_VER
setlocal
"%~1" -c "import sys; exit(0 if sys.version_info >= (3,8) else 2)" >nul 2>&1
if errorlevel 2 ( endlocal & exit /b 2 )
if errorlevel 1 ( endlocal & exit /b 2 )
endlocal & exit /b 0

rem --- :LOG <msg>  (safe for empty msg -> blank line)
:LOG
if "%~1"=="" (
  echo.
  echo [%date% %time%] >> "%LOG_FILE%"
  goto :eof
)
echo %~1
echo [%date% %time%] %~1 >> "%LOG_FILE%"
goto :eof
