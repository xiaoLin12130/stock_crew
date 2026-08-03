# 一键启动：后端（.venv uvicorn:8502）+ Cloudflare Tunnel 快速隧道（无需服务器）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\start_tunnel.ps1

$ErrorActionPreference = "Stop"
Set-Location "H:\stock_review_crew"

# 0. 网络代理（东财 HTTP 改写已内建；外部 API 走本机代理）
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"

# 1. 确保后端（.venv uvicorn:8502）在运行（8501 常被 synalysis 占用）
if (-not (Get-NetTCPConnection -LocalPort 8502 -State Listen -ErrorAction SilentlyContinue)) {
    Write-Host "启动后端服务（8502）..."
    Start-Process "H:\stock_review_crew\.venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8502" `
        -WorkingDirectory "H:\stock_review_crew" -WindowStyle Hidden
    Start-Sleep -Seconds 7
}

# 2. 确保 cloudflared 存在（可从 synalysis .tmp 复用）
$cf = "H:\stock_review_crew\.tmp\cloudflared.exe"
if (-not (Test-Path $cf)) {
    if (Test-Path "H:\synalysis_crew\.tmp\cloudflared.exe") {
        Copy-Item "H:\synalysis_crew\.tmp\cloudflared.exe" $cf
    } else {
        Write-Host "未找到 cloudflared，请先下载到 $cf（github.com/cloudflare/cloudflared releases，走代理）" -ForegroundColor Red
        exit 1
    }
}

# 3. 启动快速隧道并等待公网地址
$log = "H:\stock_review_crew\.tmp\tunnel.log"
$err = "H:\stock_review_crew\.tmp\tunnel.err"
Remove-Item $log, $err -Force -ErrorAction SilentlyContinue
Start-Process $cf -ArgumentList "tunnel", "--url", "http://127.0.0.1:8502", "--no-autoupdate" `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $txt = ""
    if (Test-Path $log) { $txt += Get-Content -Raw $log }
    if (-not $txt -and (Test-Path $err)) { $txt += Get-Content -Raw $err }
    $m = [regex]::Match($txt, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($m.Success) {
        Write-Host ""
        Write-Host "公网地址: $($m.Value)" -ForegroundColor Green
        Write-Host "提示: 快速隧道地址每次重启会变化；电脑关机后服务停止；公网地址请勿公开分享。" -ForegroundColor Yellow
        exit 0
    }
}
Write-Host "隧道启动超时，请查看 $err" -ForegroundColor Red
exit 1
