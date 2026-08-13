@echo off
title AI小说工具 - 启动服务
setlocal enabledelayedexpansion
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

rem ============ 查找 Python ============
set "PY_CMD="
for %%P in (
  "D:\Anaconda3\envs\chatglm3\python.exe"
  "D:\Anaconda3\python.exe"
  "C:\ProgramData\anaconda3\python.exe"
  "C:\ProgramData\miniconda3\python.exe"
) do (
  if exist "%%~P" if not defined PY_CMD set "PY_CMD=%%~P"
)
if not defined PY_CMD (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY_CMD set "PY_CMD=%%i"
  )
)
if not defined PY_CMD (
  echo.
  echo [错误] 未找到可用的 Python。
  echo 请安装 Anaconda，或修改本文件顶部的 Python 路径。
  echo.
  pause
  exit /b 1
)

rem ============ 日志目录 ============
set "LOG_DIR=%APP_DIR%logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\启动记录.log"
echo [%date% %time%] 启动 Flask, Python=%PY_CMD% >> "%LOG_FILE%"

echo ============================================
echo   AI 小说精修工作台 启动器
echo   使用 Python : %PY_CMD%
echo   服务地址   : http://127.0.0.1:5000/
echo   关闭本窗口即停止服务
echo ============================================
echo.

rem ============ 打开浏览器 ============
start "" "http://127.0.0.1:5000/"

rem ============ 启动 Flask ============
echo 正在启动服务，请稍候...
echo.
call "%PY_CMD%" -u "%APP_DIR%app.py"
set "EXIT_RC=%errorlevel%"
echo [%date% %time%] 服务退出, 代码=%EXIT_RC% >> "%LOG_FILE%"
echo.
echo 服务已退出 (代码 %EXIT_RC%)。
pause
