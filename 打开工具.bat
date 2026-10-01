@echo off
chcp 936 >nul
setlocal EnableExtensions
title 媒体下载工具
cd /d "%~dp0"

set "PORT=8848"

echo ============================================
echo            媒体下载工具  启动中
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python check_deps.py
if errorlevel 1 goto depfail

goto findport

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

REM 端口占用检测：端口空闲时 errorlevel 为 1
:findport
REM 已有实例在跑就直接打开浏览器，绝不双实例（双实例会抢带宽、任务互相看不见）
curl -s -o nul -m 3 http://127.0.0.1:8848/api/status >nul 2>nul
if not errorlevel 1 (
  echo [提示] 工具已在运行，直接打开浏览器。
  start "" http://127.0.0.1:8848
  timeout /t 2 /nobreak >nul
  exit /b 0
)
netstat -ano | findstr /c:":%PORT% " | findstr /c:"LISTENING" >nul 2>nul
if errorlevel 1 goto portok
echo [提示] 端口 %PORT% 已被占用，尝试下一个端口
set /a PORT=%PORT%+1
if %PORT% GTR 8870 goto portfail
goto findport

:portfail
echo.
echo [错误] 8848 到 8870 端口都被占用了，请关闭其它程序后重试。
echo.
pause
exit /b 1

:portok

echo.
echo  访问地址:  http://127.0.0.1:%PORT%
echo  下载目录:  %cd%\downloads
echo.
echo  关闭本窗口即可停止服务。
echo ============================================
echo.

REM ---- 5. 延迟打开浏览器（等服务起来）----
REM 用 ping 计时而非 timeout：timeout 在无控制台输入（后台/重定向启动）时
REM 会报「不支持输入重新定向」并立即失败；ping 在任何情况下都可用。
start "" /b cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:%PORT%"

REM ---- 6. 启动服务（serve.py：faulthandler + 日志落盘，崩溃可尸检）----
python serve.py

echo.
echo 服务已退出。
pause
endlocal
