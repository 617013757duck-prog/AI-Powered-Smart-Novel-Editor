@echo off
title AI小说工具 - 强制关闭与清理
setlocal enabledelayedexpansion

echo ============================================
echo   AI小说工具 - 强制关闭与清理
echo ============================================
echo.

rem ---------- 1. 关闭 Flask 后端 (端口 5000) ----------
echo [1/4] 关闭 Flask 后端 (端口 5000)
set FLASK_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do set FLASK_PID=%%a
if defined FLASK_PID (
  echo   发现 Flask 进程 PID %FLASK_PID%，正在终止...
  taskkill /F /PID %FLASK_PID% >nul 2>&1
  echo   Flask 进程已终止
) else (
  echo   未发现 Flask 监听进程
)
echo.

rem ---------- 2. 关闭所有 Python 进程 ----------
echo [2/4] 关闭所有 Python 进程
taskkill /F /IM python.exe >nul 2>&1
echo   已执行 Python 进程清理
echo.

rem ---------- 3. 关闭 WSL 虚拟机 ----------
echo [3/4] 关闭 WSL 虚拟机 (vmmemWSL)
tasklist /fi "imagename eq vmmemWSL" /fo csv /nh 2>nul | findstr /i "vmmemWSL" >nul
if errorlevel 1 (
  echo   vmmemWSL 未运行
) else (
  echo   检测到 vmmemWSL 正在运行，执行 wsl --shutdown ...
  wsl --shutdown 2>nul
  timeout /t 3 /nobreak >nul
  tasklist /fi "imagename eq vmmemWSL" /fo csv /nh 2>nul | findstr /i "vmmemWSL" >nul
  if errorlevel 1 (
    echo   vmmemWSL 已关闭
  ) else (
    echo   [警告] vmmemWSL 未能关闭，可重启电脑
  )
)
echo.

rem ---------- 4. 最终状态验证 ----------
echo [4/4] 最终状态验证
netstat -ano 2>nul | findstr ":5000" >nul
if errorlevel 1 (
  echo   [OK] 端口 5000 已释放
) else (
  echo   [警告] 端口 5000 仍被占用
)
tasklist /fi "imagename eq python.exe" /fo csv /nh 2>nul | findstr /i "python" >nul
if errorlevel 1 (
  echo   [OK] 无 Python 进程
) else (
  echo   [警告] 仍有 Python 进程运行
)
tasklist /fi "imagename eq vmmemWSL" /fo csv /nh 2>nul | findstr /i "vmmemWSL" >nul
if errorlevel 1 (
  echo   [OK] vmmemWSL 已关闭
) else (
  echo   [警告] vmmemWSL 仍在运行
)
echo.
echo 清理完成
pause
