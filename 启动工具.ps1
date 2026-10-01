# 媒体下载工具 - PowerShell 启动脚本
# 1D 起：端口决策 / 已有实例识别 / 自动打开浏览器统一由 launcher.py 处理，
# 本脚本不再自己挑端口 —— 改端口请用：python launcher.py --port 端口号
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

Write-Host "============================================"
Write-Host "           媒体下载工具  启动中"
Write-Host "============================================"

# 1. 检查依赖
$check = 'import fastapi, uvicorn, requests, bs4, lxml, PIL, yt_dlp'
python -c $check *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[提示] 缺少依赖，正在安装..."
    python -m pip install --disable-pip-version-check fastapi uvicorn requests beautifulsoup4 lxml pillow yt-dlp
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
}

# 2. ffmpeg
if (-not (Test-Path (Join-Path $PSScriptRoot 'bin\ffmpeg.exe'))) {
    if (Test-Path (Join-Path $PSScriptRoot 'setup_ffmpeg.py')) {
        Write-Host "[提示] 正在提取 ffmpeg ..."
        python setup_ffmpeg.py
    }
}

# 3. 启动（前台；端口/实例/浏览器由 launcher.py 统一决策）
python launcher.py
exit $LASTEXITCODE
