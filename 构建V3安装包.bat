@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   MediaDownloader V3.0  一键构建（程序目录 + 安装包）
echo ============================================================
echo.

set PY=%~dp0.venv-build\Scripts\python.exe
set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
set BUILD_OUT=D:\_md_installer_build

if not exist "%PY%" (
  echo [X] 找不到构建用 Python：%PY%
  pause & exit /b 1
)
if not exist "%ISCC%" (
  echo [X] 找不到 Inno Setup 编译器：%ISCC%
  echo     请安装 Inno Setup 6 后重试
  pause & exit /b 1
)

echo [1/5] 清理旧的构建输出...
if exist dist_new rmdir /S /Q dist_new
if exist "%BUILD_OUT%" rmdir /S /Q "%BUILD_OUT%"
mkdir "%BUILD_OUT%" 2>nul

echo [2/5] PyInstaller 打包（约 3-5 分钟）...
"%PY%" -m PyInstaller --noconfirm --distpath dist_new MediaDownloader.spec
if not exist "dist_new\MediaDownloader\MediaDownloader.exe" (
  echo [X] 打包失败，见上方错误
  pause & exit /b 1
)

echo.
echo [3/5] 补齐 C++ 运行库（目标电脑缺 msvcp140 会启动失败）...
for %%D in (msvcp140.dll msvcp140_1.dll msvcp140_2.dll concrt140.dll) do (
  if exist "%SystemRoot%\System32\%%D" (
    copy /Y "%SystemRoot%\System32\%%D" "dist_new\MediaDownloader\_internal\%%D" >nul
    echo     已补 %%D
  ) else (
    echo     [跳过] 本机没有 %%D
  )
)

echo.
echo [4/5] 生成安装包（约 3-20 分钟）...
rem 输出目录刻意放在项目外：在项目内编译时，杀毒软件会锁住 Setup.exe，
rem 报 "Resource update error: EndUpdateResource failed (64)" 直接中断（实测）。
"%ISCC%" installer\MediaDownloader3.iss
if errorlevel 1 (
  echo [X] 安装包编译失败
  pause & exit /b 1
)

echo.
echo [5/5] 复制安装包到 installer_out...
if not exist installer_out mkdir installer_out
rem 不再写死包名：以前这里写的是 MediaDownloader_V3.0_Setup.exe，
rem 而 iss 实际按版本号出 MediaDownloader_V3.0.2_Setup.exe → 每次定版都复制失败。
rem 现在按「最新生成的那个」复制，版本号改了也不用动这里。
set "SETUP="
for /f "delims=" %%F in ('dir /b /o-d "%BUILD_OUT%\MediaDownloader_V*_Setup.exe" 2^>nul') do (
  if not defined SETUP set "SETUP=%BUILD_OUT%\%%F"
)
if not defined SETUP (
  echo [X] %BUILD_OUT% 下没找到 MediaDownloader_V*_Setup.exe
  pause & exit /b 1
)
copy /Y "%SETUP%" installer_out\ >nul
if errorlevel 1 (
  echo [X] 复制失败，安装包仍在 %BUILD_OUT%
  pause & exit /b 1
)
for %%F in ("%SETUP%") do set "SETUPNAME=%%~nxF"

echo.
echo ============================================================
echo   完成
echo   安装包： installer_out\%SETUPNAME%
echo   绿色版： dist_new\MediaDownloader\MediaDownloader.exe
echo ============================================================
echo.
echo 说明：
echo  - 安装包不会自动安装到本机，请在目标电脑上运行它。
echo  - 确认没问题后，把 dist_new\MediaDownloader 覆盖到 dist\MediaDownloader
echo    （脚本不自动覆盖，避免占用中的文件出问题）。
echo.
pause
