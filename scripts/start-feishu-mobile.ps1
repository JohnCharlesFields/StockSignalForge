#Requires -Version 5.1
<#
.SYNOPSIS
  启动项目 + Cloudflare Quick Tunnel，输出供飞书移动端主页使用的临时公网 HTTPS 地址。
.DESCRIPTION
  - 复用现有 docker-compose.yml；Web 服务名 vibe-trading（容器内端口 8899）。
  - cloudflared 经 Docker 内网访问 http://vibe-trading:8899（不是宿主机 127.0.0.1:19090）。
  - 仅在 feishu profile 下启动隧道，不暴露 cloudflared 端口、不删任何数据卷。
#>
[CmdletBinding()]
param()

# Native tools (docker) write progress to stderr; with 'Stop' that would abort
# the script. Use 'Continue' and gate every critical step on $LASTEXITCODE.
$ErrorActionPreference = 'Continue'

# ---- paths / constants ----
$RepoRoot      = Split-Path -Parent $PSScriptRoot
$Compose       = Join-Path $RepoRoot 'docker-compose.yml'
$RuntimeDir    = Join-Path $RepoRoot '.runtime'
$UrlFile       = Join-Path $RuntimeDir 'feishu-mobile-url.txt'
$WebService    = 'vibe-trading'
$TunnelService = 'cloudflared'
$FeishuProfile = 'feishu'
# Match the assigned tunnel host, but never `api.trycloudflare.com` (that string
# also appears in cloudflared's API-request log/error lines).
$UrlRegex      = 'https://(?!api\.)[a-zA-Z0-9-]+\.trycloudflare\.com'

function Write-Err([string]$msg) { Write-Host "[错误] $msg" -ForegroundColor Red }
function Write-Step([string]$msg) { Write-Host "[步骤] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg) { Write-Host "[成功] $msg" -ForegroundColor Green }
function Fail([string]$msg) { Write-Err $msg; exit 1 }

function Invoke-Compose {
    # Run `docker compose -f <file> <args...>`; stderr is captured by the tool.
    & docker compose -f $Compose @args
}

# ============================================================
# 1. 环境检查
# ============================================================
Write-Step '检查 Docker 环境…'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail '未找到 docker 命令。请安装 Docker Desktop 并加入 PATH。'
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Engine 不可用。请先启动 Docker Desktop。' }

& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail '未找到 docker compose（v2）。请升级 Docker Desktop。' }

if (-not (Test-Path $Compose)) { Fail "找不到 Compose 文件：$Compose" }
& docker compose -f $Compose config *> $null
if ($LASTEXITCODE -ne 0) { Fail "Compose 配置解析失败：$Compose" }
Write-Ok 'Docker 环境正常。'

# ============================================================
# 2. 准备运行目录（清掉上次的旧地址）
# ============================================================
if (-not (Test-Path $RuntimeDir)) { New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null }
if (Test-Path $UrlFile) { Remove-Item $UrlFile -Force }

# ============================================================
# 3. 启动 Web 服务（构建）+ 重新创建 cloudflared（保证日志只含本次地址）
# ============================================================
Write-Step '构建并启动 Web 服务（vibe-trading）…'
Invoke-Compose up -d --build $WebService
if ($LASTEXITCODE -ne 0) { Fail 'Web 服务启动失败。请检查上面的构建输出。' }

# ============================================================
# 4. 等待 Web 服务健康
# ============================================================
Write-Step '等待 Web 服务通过健康检查…'
$webCid = (Invoke-Compose ps -q $WebService | Select-Object -First 1)
if (-not $webCid) { Fail '无法获取 Web 服务容器 ID。' }

$healthy = $false
for ($i = 0; $i -lt 40; $i++) {
    $status = (& docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' $webCid 2>$null)
    if ($status -eq 'healthy') { $healthy = $true; break }
    if ($status -eq 'none') {
        # No healthcheck defined: fall back to "running" state.
        $run = (& docker inspect -f '{{.State.Running}}' $webCid 2>$null)
        if ($run -eq 'true') { $healthy = $true; break }
    }
    Start-Sleep -Seconds 3
}
if (-not $healthy) {
    Write-Err 'Web 服务未在预期时间内就绪。最近日志：'
    Invoke-Compose logs --no-color --tail 60 $WebService
    Fail 'Web 服务未就绪，已中止（不会伪造公网地址）。'
}
Write-Ok 'Web 服务已就绪。'

# ------------------------------------------------------------
# 4b. 解析真实 Cloudflare edge IP（绕过 Clash/Mihomo 的 Fake-IP）
#     宿主机若配了代理（Clash），用它经 DoH 拿真实 IP，注入容器 TUNNEL_EDGE。
#     失败则跳过（cloudflared 走默认发现；Fake-IP 环境下可能连不上边缘）。
# ------------------------------------------------------------
if (-not $env:CLOUDFLARED_EDGE) {
    $hostProxy = $env:HTTPS_PROXY
    if (-not $hostProxy) { $hostProxy = $env:HTTP_PROXY }
    if ($hostProxy) {
        Write-Step '经宿主机代理解析真实 Cloudflare edge IP（规避 Fake-IP）…'
        $edgeList = @()
        foreach ($region in 'region1.v2.argotunnel.com', 'region2.v2.argotunnel.com') {
            $json = (& curl.exe -s -x $hostProxy --max-time 15 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$region&type=A" 2>$null)
            if ($json) {
                try {
                    $ips = ($json | ConvertFrom-Json).Answer | Where-Object { $_.type -eq 1 } | ForEach-Object { $_.data } | Select-Object -First 3
                    foreach ($ip in $ips) { $edgeList += ("{0}:7844" -f $ip) }
                } catch { }
            }
        }
        if ($edgeList.Count -gt 0) {
            $env:CLOUDFLARED_EDGE = ($edgeList -join ',')
            Write-Ok ("已注入 edge IP：{0}" -f $env:CLOUDFLARED_EDGE)
        } else {
            Write-Err '解析真实 edge IP 失败，cloudflared 将走默认发现（Fake-IP 环境下可能失败）。'
        }
    }
}

Write-Step '重新创建 cloudflared 隧道容器（feishu profile）…'
Invoke-Compose --profile $FeishuProfile up -d --force-recreate --no-deps $TunnelService
if ($LASTEXITCODE -ne 0) { Fail 'cloudflared 容器启动失败。' }

# ============================================================
# 5. 从本次 cloudflared 日志提取 trycloudflare 地址（取最后一个匹配）
# ============================================================
Write-Step '等待 Cloudflare 分配临时公网地址…（代理较慢时可能需要 1-2 分钟）'
$publicUrl = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 3
    $logs = (Invoke-Compose logs --no-color --tail 300 $TunnelService 2>&1 | Out-String)
    $matches = [regex]::Matches($logs, $UrlRegex)
    if ($matches.Count -gt 0) { $publicUrl = $matches[$matches.Count - 1].Value; break }
}
if (-not $publicUrl) {
    Write-Err '未能从 cloudflared 日志中提取到公网地址。完整最近日志：'
    Invoke-Compose logs --no-color --tail 200 $TunnelService
    Fail 'Quick Tunnel 建立失败（可能被网络/代理阻断）。未生成公网地址。'
}
Write-Ok "已获取地址：$publicUrl"

# ============================================================
# 6. 真实 HTTP 验证（允许重定向；401/403=隧道通但需登录）
# ============================================================
# Wait for the edge data-plane connector to register; before that the edge
# returns 530/1033 even though the hostname already exists.
Write-Step '等待隧道连接到 Cloudflare 边缘…'
for ($i = 0; $i -lt 30; $i++) {
    $logs = (Invoke-Compose logs --no-color --tail 300 $TunnelService 2>&1 | Out-String)
    if ($logs -match 'Registered tunnel connection') { break }
    Start-Sleep -Seconds 3
}

Write-Step "验证公网地址可达：$publicUrl"
$tunnelOk = $false
$needLogin = $false
$code = ''
# 530/502/503/000 = edge up but origin/connector not ready yet → retry.
for ($i = 0; $i -lt 15; $i++) {
    $code = (& curl.exe -s -o NUL -L --max-time 25 -w '%{http_code}' $publicUrl 2>$null)
    if ($code -match '^(2\d\d|3\d\d)$') { $tunnelOk = $true; break }
    if ($code -eq '401' -or $code -eq '403') { $tunnelOk = $true; $needLogin = $true; break }
    Start-Sleep -Seconds 4
}

if (-not $tunnelOk) {
    Write-Err "验证失败：HTTP 状态码 = $code（000=连接失败/超时/DNS；530/1033=隧道边缘未就绪）。"
    Write-Err 'cloudflared 最近日志：'
    Invoke-Compose logs --no-color --tail 80 $TunnelService
    Fail '公网地址未通过真实访问验证，已中止（不会声称成功）。'
}

# ============================================================
# 7. 保存地址 + 醒目输出
# ============================================================
[System.IO.File]::WriteAllText($UrlFile, $publicUrl, (New-Object System.Text.UTF8Encoding($false)))

$line = ('=' * 60)
Write-Host ''
Write-Host $line -ForegroundColor Yellow
Write-Host '飞书移动端公网地址：' -ForegroundColor Yellow
Write-Host $publicUrl -ForegroundColor Green
Write-Host ''
Write-Host '移动端主页建议填写（手机自动跳转 /m）：' -ForegroundColor Yellow
Write-Host ("{0}/m" -f $publicUrl) -ForegroundColor Green
if ($needLogin) {
    Write-Host ''
    Write-Host '提示：隧道已连通，但应用返回需要登录（401/403）。' -ForegroundColor DarkYellow
}
Write-Host ''
Write-Host '地址已保存至：' -ForegroundColor Yellow
Write-Host '.runtime/feishu-mobile-url.txt'
Write-Host ''
Write-Host '飞书配置位置：' -ForegroundColor Yellow
Write-Host '飞书开放平台 → 自建应用 → 网页应用 → 移动端主页'
Write-Host ''
Write-Host '注意：这是临时 Quick Tunnel 地址。' -ForegroundColor DarkYellow
Write-Host '重新创建 cloudflared 容器后，地址可能变化。' -ForegroundColor DarkYellow
Write-Host $line -ForegroundColor Yellow

# Final machine-readable output line.
Write-Output $publicUrl
exit 0
