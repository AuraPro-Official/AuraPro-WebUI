#Requires -Version 5.1
# AuraPro V2 → V3 升级工具
# 此脚本不修改任何 AuraPro 源代码，仅清理旧版运行环境

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

# ──────────────────────────────────────────────────────────────
#  样式函数
# ──────────────────────────────────────────────────────────────
function Write-Banner {
    Clear-Host
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║                                              ║" -ForegroundColor Cyan
    Write-Host "  ║      AuraPro  V2 → V3  升级工具             ║" -ForegroundColor Cyan
    Write-Host "  ║                                              ║" -ForegroundColor Cyan
    Write-Host "  ╚══════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step([int]$n, [int]$total, [string]$msg) {
    Write-Host "  [" -NoNewline -ForegroundColor DarkGray
    Write-Host "$n/$total" -NoNewline -ForegroundColor Yellow
    Write-Host "] $msg" -ForegroundColor White
}

function Write-Ok([string]$msg) {
    Write-Host "        ✓ $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "        ⚠ $msg" -ForegroundColor Yellow
}

function Write-Err([string]$msg) {
    Write-Host "        ✗ $msg" -ForegroundColor Red
}

function Write-Info([string]$msg) {
    Write-Host "          $msg" -ForegroundColor DarkGray
}

function Pause-Exit([int]$code = 0) {
    Write-Host ""
    Write-Host "  按任意键退出..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
    exit $code
}

# ──────────────────────────────────────────────────────────────
#  读取 AuraPro 配置
# ──────────────────────────────────────────────────────────────
function Get-AuraProConfig {
    $userData = [System.IO.Path]::Combine($env:APPDATA, 'AuraPro')
    $configFile = [System.IO.Path]::Combine($userData, 'config.json')

    $installDir = $userData
    $dataDir    = [System.IO.Path]::Combine($userData, 'data')

    if (Test-Path $configFile) {
        try {
            $cfg = Get-Content $configFile -Raw | ConvertFrom-Json
            if ($cfg.installDir -and (Test-Path $cfg.installDir)) {
                $installDir = $cfg.installDir
            }
            if ($cfg.dataDir -and (Test-Path $cfg.dataDir)) {
                $dataDir = $cfg.dataDir
            } elseif ($cfg.installDir) {
                $dataDir = [System.IO.Path]::Combine($installDir, 'data')
            }
        } catch {
            # config.json 损坏时使用默认值
        }
    }

    return @{
        userData   = $userData
        configFile = $configFile
        installDir = $installDir
        dataDir    = $dataDir
        pythonExe  = [System.IO.Path]::Combine($installDir, 'python', 'python.exe')
        locksDir   = [System.IO.Path]::Combine($userData, 'locks')
        dbFile     = [System.IO.Path]::Combine($dataDir, 'webui.db')
        uploadsDir = [System.IO.Path]::Combine($dataDir, 'uploads')
        keyFile    = [System.IO.Path]::Combine($dataDir, '.key')
    }
}

# ──────────────────────────────────────────────────────────────
#  检测当前安装的包版本
# ──────────────────────────────────────────────────────────────
function Get-InstalledPackageVersion([string]$pythonExe, [string]$pkgName) {
    if (-not (Test-Path $pythonExe)) { return $null }
    try {
        $out = & $pythonExe -m pip show $pkgName 2>$null
        if ($LASTEXITCODE -eq 0) {
            $ver = ($out | Select-String 'Version:') -replace 'Version:\s*', ''
            return $ver.Trim()
        }
    } catch {}
    return $null
}

# ──────────────────────────────────────────────────────────────
#  步骤 1：终止所有 AuraPro 相关进程
# ──────────────────────────────────────────────────────────────
function Stop-AuraProProcesses([hashtable]$cfg) {
    Write-Step 1 5 "终止 AuraPro 进程..."

    # 终止 Electron 主进程（AuraPro.exe）
    $killed = 0
    foreach ($name in @('AuraPro', 'aurapro')) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
            try { $_.Kill(); $killed++ } catch {}
        }
    }
    if ($killed -gt 0) { Write-Ok "已终止 $killed 个 AuraPro 进程" }
    else                { Write-Info "未发现运行中的 AuraPro 进程" }

    # 终止占用 8080 / 8081 端口的后端 Python 进程
    $ports = @(8080, 8081, 8082)
    foreach ($port in $ports) {
        try {
            $connections = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
            $listening = $connections | Where-Object { $_.Port -eq $port }
            if ($listening) {
                $netLines = netstat -ano 2>$null | Select-String ":$port\s"
                foreach ($line in $netLines) {
                    $pid = ($line -split '\s+')[-1]
                    if ($pid -match '^\d+$' -and [int]$pid -gt 4) {
                        try { Stop-Process -Id ([int]$pid) -Force -ErrorAction Stop; Write-Ok "已终止占用端口 $port 的进程 (PID $pid)" }
                        catch {}
                    }
                }
            }
        } catch {}
    }

    # 终止 Python/uvicorn 子进程（来自 installDir）
    if (Test-Path $cfg.pythonExe) {
        $pythonDir = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetDirectoryName($cfg.pythonExe))
        Get-Process python -ErrorAction SilentlyContinue | Where-Object {
            try { $_.MainModule.FileName.StartsWith($pythonDir, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
        } | ForEach-Object {
            try { $_.Kill(); Write-Ok "已终止 Python 进程 (PID $($_.Id))" } catch {}
        }
    }

    Start-Sleep -Seconds 2
}

# ──────────────────────────────────────────────────────────────
#  步骤 2：备份用户数据
# ──────────────────────────────────────────────────────────────
function Backup-UserData([hashtable]$cfg) {
    Write-Step 2 5 "备份用户数据..."

    $timestamp  = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupRoot = [System.IO.Path]::Combine($cfg.userData, "backup_v2_$timestamp")
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

    # 备份数据库
    if (Test-Path $cfg.dbFile) {
        Copy-Item $cfg.dbFile -Destination ([System.IO.Path]::Combine($backupRoot, 'webui.db')) -Force
        Write-Ok "数据库已备份"
    } else {
        Write-Warn "未找到数据库文件，跳过"
    }

    # 备份上传文件（仅记录，不复制大文件）
    if (Test-Path $cfg.uploadsDir) {
        $uploadCount = (Get-ChildItem $cfg.uploadsDir -Recurse -File -ErrorAction SilentlyContinue).Count
        Write-Ok "上传目录已确认 ($uploadCount 个文件，原路保留，无需移动)"
    }

    # 记录备份信息
    $info = @{
        backupTime = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        installDir = $cfg.installDir
        dataDir    = $cfg.dataDir
    } | ConvertTo-Json
    $info | Out-File -FilePath ([System.IO.Path]::Combine($backupRoot, 'backup_info.json')) -Encoding utf8

    Write-Ok "备份目录：$backupRoot"
    return $backupRoot
}

# ──────────────────────────────────────────────────────────────
#  步骤 3：卸载旧版 Python 包
# ──────────────────────────────────────────────────────────────
function Uninstall-OldPackages([hashtable]$cfg) {
    Write-Step 3 5 "卸载旧版 Python 包..."

    if (-not (Test-Path $cfg.pythonExe)) {
        Write-Warn "未找到 Python 环境 ($($cfg.pythonExe))，跳过包卸载"
        return
    }

    $packages = @('aurapro-webui', 'aurapro-ui', 'open-webui')
    foreach ($pkg in $packages) {
        # 先用 uv pip（AuraPro V3 默认方式）
        try {
            $result = & $cfg.pythonExe -m uv pip uninstall -y $pkg 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "已卸载 (uv pip): $pkg"
                continue
            }
        } catch {}

        # 回退到普通 pip
        try {
            $result = & $cfg.pythonExe -m pip uninstall -y $pkg 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "已卸载 (pip): $pkg"
                continue
            }
        } catch {}

        Write-Info "未安装或已卸载: $pkg"
    }

    # 清理 pip / uv 缓存
    try { & $cfg.pythonExe -m pip cache purge 2>$null | Out-Null } catch {}
    try { & $cfg.pythonExe -m uv cache clean 2>$null | Out-Null } catch {}
    Write-Ok "已清理包缓存"
}

# ──────────────────────────────────────────────────────────────
#  步骤 4：清理锁文件 / 密钥 / 残留缓存
# ──────────────────────────────────────────────────────────────
function Clear-Stale([hashtable]$cfg) {
    Write-Step 4 5 "清理残留文件..."

    # 删除服务锁文件（防止下次启动认为服务仍在运行）
    if (Test-Path $cfg.locksDir) {
        Remove-Item -Path $cfg.locksDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "已清理锁文件目录"
    } else {
        Write-Info "锁文件目录不存在，跳过"
    }

    # 删除旧 secret key（V3 会重新生成；保留会导致 JWT 验证失败）
    if (Test-Path $cfg.keyFile) {
        Remove-Item -Path $cfg.keyFile -Force -ErrorAction SilentlyContinue
        Write-Ok "已删除旧 Secret Key（V3 将自动生成新密钥）"
        Write-Warn "注意：已登录的用户需要重新登录"
    }

    # 删除 __pycache__ / .pyc（旧版字节码可能与新版不兼容）
    $sitePackages = [System.IO.Path]::Combine($cfg.installDir, 'python', 'Lib', 'site-packages')
    if (Test-Path $sitePackages) {
        Get-ChildItem $sitePackages -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "已清理旧字节码缓存"
    }
}

# ──────────────────────────────────────────────────────────────
#  步骤 5：启动新版 AuraPro
# ──────────────────────────────────────────────────────────────
function Launch-AuraPro {
    Write-Step 5 5 "启动 AuraPro V3..."

    $candidates = @(
        [System.IO.Path]::Combine($env:LOCALAPPDATA, 'Programs', 'AuraPro', 'AuraPro.exe'),
        [System.IO.Path]::Combine($env:PROGRAMFILES,             'AuraPro', 'AuraPro.exe'),
        [System.IO.Path]::Combine($env:LOCALAPPDATA,             'AuraPro', 'AuraPro.exe')
    )

    $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

    if ($exe) {
        Start-Process $exe
        Write-Ok "已启动：$exe"
        Write-Info "AuraPro 将自动检测旧包已卸载，进入安装向导重新安装 V3 后端"
    } else {
        Write-Warn "未找到 AuraPro 可执行文件"
        Write-Info "请手动启动 AuraPro V3，程序会自动进入安装向导"
    }
}

# ──────────────────────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────────────────────
Write-Banner

$cfg = Get-AuraProConfig

# ── 检测安装情况
Write-Host "  正在检测 AuraPro 安装..." -ForegroundColor DarkGray
Write-Host ""

$hasPython = Test-Path $cfg.pythonExe
$hasDB     = Test-Path $cfg.dbFile

$auraProVersion       = if ($hasPython) { Get-InstalledPackageVersion $cfg.pythonExe 'aurapro-webui' } else { $null }
$legacyAuraProVersion = if ($hasPython) { Get-InstalledPackageVersion $cfg.pythonExe 'aurapro-ui'    } else { $null }
$openWebUIVer         = if ($hasPython) { Get-InstalledPackageVersion $cfg.pythonExe 'open-webui'    } else { $null }

Write-Host "  检测结果：" -ForegroundColor White
Write-Host "    · userData    ：$($cfg.userData)" -ForegroundColor DarkGray
Write-Host "    · installDir  ：$($cfg.installDir)" -ForegroundColor DarkGray
Write-Host "    · dataDir     ：$($cfg.dataDir)" -ForegroundColor DarkGray
Write-Host "    · Python      ：$(if ($hasPython) { '✓ 已安装' } else { '✗ 未找到' })" -ForegroundColor (if ($hasPython) { 'Green' } else { 'Red' })
Write-Host "    · aurapro-webui：$(if ($auraProVersion) { $auraProVersion } else { '未安装' })" -ForegroundColor (if ($auraProVersion) { 'Yellow' } else { 'DarkGray' })
Write-Host "    · aurapro-ui   ：$(if ($legacyAuraProVersion) { $legacyAuraProVersion } else { '未安装' })" -ForegroundColor (if ($legacyAuraProVersion) { 'Yellow' } else { 'DarkGray' })
Write-Host "    · open-webui   ：$(if ($openWebUIVer) { $openWebUIVer } else { '未安装' })" -ForegroundColor (if ($openWebUIVer) { 'Yellow' } else { 'DarkGray' })
Write-Host "    · 数据库      ：$(if ($hasDB) { '✓ 存在（将备份）' } else { '未找到' })" -ForegroundColor (if ($hasDB) { 'Green' } else { 'DarkGray' })
Write-Host ""

if (-not $hasPython -and -not $auraProVersion -and -not $legacyAuraProVersion -and -not $openWebUIVer) {
    Write-Host "  ⚠ 未检测到 AuraPro 安装，可能已是干净状态。" -ForegroundColor Yellow
    Write-Host "    直接启动 AuraPro V3 即可进入安装向导。" -ForegroundColor Yellow
    Write-Host ""
    Pause-Exit 0
}

# ── 用户确认
Write-Host "  升级步骤：" -ForegroundColor White
Write-Host "    1. 终止所有 AuraPro 相关进程" -ForegroundColor DarkGray
Write-Host "    2. 备份数据库到 $($cfg.userData)\backup_v2_*" -ForegroundColor DarkGray
Write-Host "    3. 卸载旧版 Python 包（aurapro-webui / aurapro-ui / open-webui）" -ForegroundColor DarkGray
Write-Host "    4. 清理锁文件、旧 Secret Key、字节码缓存" -ForegroundColor DarkGray
Write-Host "    5. 启动 AuraPro → 自动进入安装向导，重新安装 V3 后端" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ⚠ 注意事项：" -ForegroundColor Yellow
Write-Host "    · 聊天记录、文件、词汇表均会保留" -ForegroundColor Yellow
Write-Host "    · Secret Key 将重置，已登录用户需要重新登录" -ForegroundColor Yellow
Write-Host "    · 安装向导需要联网下载 V3 后端包（约 80MB）" -ForegroundColor Yellow
Write-Host "    · Python 环境和模型文件不会被删除" -ForegroundColor Yellow
Write-Host ""

$confirm = Read-Host "  确认继续升级？(y/N)"
if ($confirm -notmatch '^[yY]$') {
    Write-Host ""
    Write-Host "  已取消。" -ForegroundColor DarkGray
    Pause-Exit 0
}

Write-Host ""
Write-Host "  ─────────────────────────────────────────────────" -ForegroundColor DarkGray

# ── 执行步骤
$backupPath = $null
try {
    Stop-AuraProProcesses  $cfg
    $backupPath = Backup-UserData $cfg
    Uninstall-OldPackages  $cfg
    Clear-Stale            $cfg
    Launch-AuraPro
}
catch {
    Write-Host ""
    Write-Err "升级过程中发生错误："
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  $($_.ScriptStackTrace)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  请截图以上错误信息并联系 AuraPro 支持团队。" -ForegroundColor Yellow
    Pause-Exit 1
}

# ── 完成
Write-Host ""
Write-Host "  ─────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ✅ 升级准备完成！" -ForegroundColor Green
Write-Host ""
if ($backupPath) {
    Write-Host "  数据库备份位置：" -ForegroundColor White
    Write-Host "    $backupPath" -ForegroundColor Cyan
    Write-Host ""
}
Write-Host "  AuraPro V3 已启动，请：" -ForegroundColor White
Write-Host "    1. 在安装向导中点击「安装」按钮" -ForegroundColor DarkGray
Write-Host "    2. 等待后端下载安装完成（约 2-5 分钟）" -ForegroundColor DarkGray
Write-Host "    3. 安装完成后正常使用即可" -ForegroundColor DarkGray
Write-Host ""

Pause-Exit 0
