@echo off
setlocal
cd /d "%~dp0"
echo ==========================================================
echo   MediaDownloader ISOLATED TEST - onefile EXE builder
echo ==========================================================
echo.
if not exist "_venv\Scripts\python.exe" (
  echo [X] Missing _venv\Scripts\python.exe
  pause
  exit /b 1
)
echo [1/1] PyInstaller onefile build, 3-10 minutes ...
"_venv\Scripts\python.exe" -m PyInstaller --noconfirm --distpath _dist_test --workpath _build_test MediaDownloader_Test.spec
if errorlevel 1 (
  echo.
  echo [X] Build failed. Read the output above.
  pause
  exit /b 1
)
if not exist "_dist_test\MediaDownloader_TEST.exe" (
  echo [X] Expected output not found: _dist_test\MediaDownloader_TEST.exe
  pause
  exit /b 1
)
echo.
echo [OK] Single-file EXE:
dir "_dist_test\MediaDownloader_TEST.exe"
echo.
echo Copy it anywhere and double-click. All runtime data stays in
echo   ^<that folder^>\MediaDownloader_TEST\
echo It never touches the installed V3.0.3 (data, downloads, or ports).
echo.
echo Next: run  "验证测试版隔离.bat"  while the test window is open.
pause
