@echo off
chcp 65001 >nul
cd /d "%~dp0"
"_venv\Scripts\python.exe" test_isolation_check.py %*
echo.
pause
