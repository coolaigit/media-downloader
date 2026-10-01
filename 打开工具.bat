@echo off
chcp 936 >nul
setlocal EnableExtensions
title 媒体下载工具
cd /d "%~dp0"

echo ============================================
echo            媒体下载工具  启动中
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python check_deps.py
if errorlevel 1 goto depfail

REM 1D 起：端口决策 / 已有实例识别 / 自动打开浏览器
REM 全部由 launcher.py 统一处理（CLI --port > 环境变量 > 自动扫描 8848-8870），
REM 本脚本不再自己挑端口 —— 改端口请用：python launcher.py --port 端口号
python launcher.py
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
  echo 服务已退出。
) else (
  echo 服务异常退出（代码 %EXITCODE%）。
  echo 运行日志: %LOCALAPPDATA%\MediaDownloader\logs
)
pause
endlocal
exit /b %EXITCODE%

:nopython
echo.
echo [错误] 未找到 python，请先安装 Python 3.10 或更高版本，并勾选加入 PATH。
echo.
pause
exit /b 1

:depfail
echo.
echo [错误] 启动自检未通过，请按上面的提示处理后重试。
echo.
pause
exit /b 1
