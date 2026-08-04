@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动后端 + Cloudflare 公网隧道（启动后打印公网地址）...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start_tunnel.ps1"
echo.
echo 提示：公网地址每次重启会变化；请勿公开分享。
pause
