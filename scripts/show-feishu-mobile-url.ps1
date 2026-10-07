#Requires -Version 5.1
<#
.SYNOPSIS
  显示当前有效的飞书移动端临时公网地址（以正在运行的 cloudflared 容器日志为准）。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'  # docker writes progress to stderr; gate on exit codes instead

$RepoRoot      = Split-Path -Parent $PSScriptRoot
$Compose       = Join-Path $RepoRoot 'docker-compose.yml'
$RuntimeDir    = Join-Path $RepoRoot '.runtime'
$UrlFile       = Join-Path $RuntimeDir 'feishu-mobile-url.txt'
$TunnelService = 'cloudflared'
$FeishuProfile = 'feishu'
# Match the assigned tunnel host, but never `api.trycloudflare.com`.
$UrlRegex      = 'https://(?!api\.)[a-zA-Z0-9-]+\.trycloudflare\.com'

function Hint-Start {
    Write-Host '隧道未运行或未找到有效地址。请先执行：' -ForegroundColor Yellow
    Write-Host '  .\scripts\start-feishu-mobile.ps1'
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[错误] 未找到 docker 命令。' -ForegroundColor Red
    exit 1
}

# Is the cloudflared container actually running?
$cid = (& docker compose -f $Compose --profile $FeishuProfile ps -q $TunnelService 2>$null | Select-Object -First 1)
$running = $false
if ($cid) {
    $state = (& docker inspect -f '{{.State.Running}}' $cid 2>$null)
    if ($state -eq 'true') { $running = $true }
}

$fileUrl = $null
if (Test-Path $UrlFile) { $fileUrl = (Get-Content $UrlFile -Raw).Trim() }

if (-not $running) {
    Write-Host '[提示] cloudflared 隧道当前没有运行。' -ForegroundColor Yellow
    if ($fileUrl) { Write-Host "（文件中记录的旧地址 $fileUrl 可能已失效，不应继续使用）" -ForegroundColor DarkYellow }
    Hint-Start
    exit 1
}

# Tunnel running → re-extract the live URL from current logs (authoritative).
$logs = (& docker compose -f $Compose --profile $FeishuProfile logs --no-color --tail 300 $TunnelService 2>&1 | Out-String)
$matches = [regex]::Matches($logs, $UrlRegex)
$liveUrl = $null
if ($matches.Count -gt 0) { $liveUrl = $matches[$matches.Count - 1].Value }

if (-not $liveUrl) {
    Write-Host '[提示] 隧道在运行，但日志里暂时没有公网地址（可能仍在建立或已断流）。' -ForegroundColor Yellow
    Hint-Start
    exit 1
}

# Reconcile file with live log; live log wins.
if ($liveUrl -ne $fileUrl) {
    if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }
    [System.IO.File]::WriteAllText($UrlFile, $liveUrl, (New-Object System.Text.UTF8Encoding($false)))
}

$line = ('=' * 60)
Write-Host $line -ForegroundColor Yellow
Write-Host '当前飞书移动端公网地址：' -ForegroundColor Yellow
Write-Host $liveUrl -ForegroundColor Green
Write-Host ('移动端主页建议填写：{0}/m' -f $liveUrl) -ForegroundColor Green
Write-Host $line -ForegroundColor Yellow
Write-Output $liveUrl
exit 0
