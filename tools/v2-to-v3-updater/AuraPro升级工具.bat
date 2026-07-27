@echo off
chcp 65001 >nul 2>&1
setlocal

echo.
echo  正在启动 AuraPro V2 -^> V3 升级工具...
echo.

:: 检查 PowerShell 是否可用
where powershell >nul 2>&1
if %errorlevel% neq 0 (
    echo  错误：未找到 PowerShell，请确保 Windows PowerShell 已安装。
    pause
    exit /b 1
)

:: 以绕过执行策略运行脚本（不修改系统设置）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0updater.ps1"

exit /b %errorlevel%
