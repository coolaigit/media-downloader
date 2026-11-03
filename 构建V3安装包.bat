@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  MediaDownloader V3 一键构建（程序目录 + 安装包）
rem
rem  本文件必须保存为 GBK(ANSI) 编码 + CRLF 换行 —— 与「打开工具.bat」一致。
rem  注意：存成 UTF-8 或 LF 会出两种不同故障（2026-11-03 实测）：
rem     UTF-8 + LF   -> cmd 解析错行，exit 255 且一行输出都没有（双击像"一闪而过"）
rem     UTF-8 + CRLF -> 能跑，但中文可能乱码
rem ============================================================
cd /d "%~dp0"
echo ============================================================
echo   MediaDownloader V3.0  一键构建（程序目录 + 安装包）
echo ============================================================
echo.

set "PY=%~dp0.venv-build\Scripts\python.exe"
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
set "BUILD_OUT=D:\_md_installer_build"

rem ------------------------------------------------------------
rem  注意：下面这些 if 块里必须用 !VAR! 而不是 %VAR%。
rem  原因：%ISCC% 的值含 "(x86)"，用 %ISCC% 会在**解析阶段**就把块里的
rem  右括号当成块的结尾，于是报 "\Inno was unexpected at this time"，
rem  脚本直接烂在第 19 行 —— 这是原版一直跑不起来的原因之一。
rem ------------------------------------------------------------
if not exist "!PY!" (
  echo [X] 找不到构建用 Python
  echo     预期：!PY!
  pause & exit /b 1
)
if not exist "!ISCC!" (
  echo [X] 找不到 Inno Setup 编译器
  echo     预期：!ISCC!
  echo     请安装 Inno Setup 6 后重试：https://jrsoftware.org/isdl.php
  pause & exit /b 1
)

echo [1/5] 清理旧的构建输出...
if exist dist_new rmdir /S /Q dist_new
if exist "!BUILD_OUT!" rmdir /S /Q "!BUILD_OUT!"
mkdir "!BUILD_OUT!" 2>nul

echo [2/5] PyInstaller 打包（约 3-5 分钟）...
"!PY!" -m PyInstaller --noconfirm --distpath dist_new MediaDownloader.spec
if not exist "dist_new\MediaDownloader\MediaDownloader.exe" (
  echo [X] 打包失败，见上方错误
  pause & exit /b 1
)

echo.
echo [3/5] 补齐 C++ 运行库（目标电脑缺 msvcp140 会启动失败）...
for %%D in (msvcp140.dll msvcp140_1.dll msvcp140_2.dll concrt140.dll) do (
  if exist "!SystemRoot!\System32\%%D" (
    copy /Y "!SystemRoot!\System32\%%D" "dist_new\MediaDownloader\_internal\%%D" >nul
    echo     已补 %%D
  ) else (
    echo     [跳过] 本机没有 %%D
  )
)

echo.
echo [4/5] 生成安装包（约 3-20 分钟）...
rem 输出目录刻意放在项目外：在项目内编译时，杀毒软件会锁住 Setup.exe，
rem 报 "Resource update error: EndUpdateResource failed (64)" 直接中断（实测）。
"!ISCC!" installer\MediaDownloader3.iss
if errorlevel 1 (
  echo [X] 安装包编译失败
  pause & exit /b 1
)

echo.
echo [5/5] 复制安装包到 installer_out...
if not exist installer_out mkdir installer_out
rem 不写死包名：iss 按版本号出 MediaDownloader_V3.0.x_Setup.exe，改了版本号这里不用动
if not exist "!BUILD_OUT!\MediaDownloader_V*_Setup.exe" (
  echo [X] 在 !BUILD_OUT! 下没找到 MediaDownloader_V*_Setup.exe
  pause & exit /b 1
)
copy /Y "!BUILD_OUT!\MediaDownloader_V*_Setup.exe" installer_out\ >nul
if errorlevel 1 (
  echo [X] 复制失败，安装包仍在 !BUILD_OUT!
  pause & exit /b 1
)

echo.
echo ============================================================
echo   完成
echo   安装包： installer_out\
dir /b installer_out\MediaDownloader_V*_Setup.exe
echo   绿色版： dist_new\MediaDownloader\MediaDownloader.exe
echo ============================================================
echo.
echo 说明：
echo  - 安装包不会自动安装到本机，请在目标电脑上运行它。
echo  - 安装包的源目录就是 dist_new\MediaDownloader（本次刚构建的），
echo    dist\MediaDownloader 是你本地在跑的绿色版，脚本不会动它。
echo.
pause
