@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   MediaDownloader  清理旧版本备份
echo ============================================================
echo.
echo 将删除以下 4 个旧版本备份目录（每个约 0.71 GB，共约 2.8 GB）：
echo    dist\v20261025-douyin        抖音修复版存档
echo    dist\v20261026-ig-fix        Instagram 修复版存档
echo    dist\MediaDownloader_old      更早的旧包
echo    dist\_prev                    上一版临时备份
echo.
echo 不会删除：
echo    dist\MediaDownloader          当前 V3.0（正在用的绿色版）
echo    installer_out\                V3.0 安装包
echo    installer\ / dist_new\ 等构建脚本与产物
echo.
set /p ok=确认删除？输入 Y 后回车继续（其它任意键取消）：
if /i not "%ok%"=="Y" (
  echo.
  echo 已取消，什么都没删。
  pause & exit /b 0
)
echo.
for %%D in (v20261025-douyin v20261026-ig-fix MediaDownloader_old _prev) do (
  if exist "dist\%%D" (
    rmdir /S /Q "dist\%%D"
    if exist "dist\%%D" (echo   [失败] dist\%%D 仍存在，可能被占用) else (echo   [OK] 已删除 dist\%%D)
  ) else (
    echo   [跳过] dist\%%D 不存在
  )
)
echo.
echo ============================================================
echo   清理完成。当前目录：
echo ============================================================
dir /b /ad dist
echo.
pause
