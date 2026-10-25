@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   MediaDownloader 更新器 / Updater
echo ==========================================
echo.

tasklist /FI "IMAGENAME eq MediaDownloader.exe" 2>nul | find /I "MediaDownloader.exe" >nul
if not errorlevel 1 (
  echo [!] 检测到 MediaDownloader 正在运行
  echo     请先完全关闭客户端（含托盘图标），再双击本脚本
  echo.
  echo     Please close MediaDownloader completely first.
  echo.
  pause
  exit /b 1
)

if not exist "dist_new\MediaDownloader\MediaDownloader.exe" (
  echo [!] 找不到新版本：dist_new\MediaDownloader
  echo     New build not found.
  pause
  exit /b 1
)

if exist "dist\MediaDownloader" (
  echo [1/2] 备份旧版本 -^> dist\MediaDownloader_old
  if exist "dist\MediaDownloader_old" rmdir /S /Q "dist\MediaDownloader_old"
  move "dist\MediaDownloader" "dist\MediaDownloader_old" >nul
)

echo [2/2] 安装新版本
move "dist_new\MediaDownloader" "dist\MediaDownloader" >nul
if exist "dist\MediaDownloader\MediaDownloader.exe" (
  echo.
  echo [OK] 更新完成
  echo      启动：dist\MediaDownloader\MediaDownloader.exe
  echo      回滚：把 dist\MediaDownloader_old 改回 dist\MediaDownloader 即可
) else (
  echo.
  echo [X] 更新失败，可能有进程还在占用文件
  echo     请重启电脑后再试，或手动把 dist_new\MediaDownloader 改名放到 dist\ 下
)
echo.
pause
