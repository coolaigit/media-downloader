# 媒体下载工具 - PowerShell 启动脚本
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$port = 8848

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

# 3. 端口占用则递增
while ($port -le 8870) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $busy) { break }
    Write-Host "[提示] 端口 $port 已占用，尝试 $(( $port + 1 ))"
    $port++
}
if ($port -gt 8870) { throw "8848-8870 端口均被占用" }

Write-Host ""
Write-Host "  访问地址:  http://127.0.0.1:$port"
Write-Host "  下载目录:  $(Join-Path $PSScriptRoot 'downloads')"
Write-Host ""
Write-Host "  按 Ctrl+C 停止服务"
Write-Host "============================================"
Write-Host ""

# 4. 打开浏览器
Start-Job -ScriptBlock {
    param($p)
    Start-Sleep -Seconds 3
    Start-Process "http://127.0.0.1:$p"
} -ArgumentList $port | Out-Null

# 5. 启动服务
python -m uvicorn app.main:app --host 127.0.0.1 --port $port --log-level warning
