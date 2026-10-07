#Requires -Version 5.1
<#
.SYNOPSIS
  停止并删除 cloudflared 临时隧道容器。不动 Web 服务、数据库或任何数据卷。
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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[错误] 未找到 docker 命令。' -ForegroundColor Red
    exit 1
}

Write-Host '[步骤] 停止并删除 cloudflared 隧道容器（不影响 Web/数据）…' -ForegroundColor Cyan
# Only remove the cloudflared service. Never `down -v`.
& docker compose -f $Compose --profile $FeishuProfile rm -sf $TunnelService
if ($LASTEXITCODE -ne 0) {
    Write-Host '[错误] 停止 cloudflared 失败。' -ForegroundColor Red
    exit 1
}

# The saved URL is now stale; drop it so nobody mistakes it for active.
if (Test-Path $UrlFile) { Remove-Item $UrlFile -Force }

Write-Host '[成功] 临时隧道已停止。Web 服务与数据保持运行。' -ForegroundColor Green
Write-Host '如需重新生成公网地址，请执行：.\scripts\start-feishu-mobile.ps1'
exit 0
