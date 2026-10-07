# Windows Docker startup for the public source release
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'agent/.env')) {
    Copy-Item -LiteralPath 'agent/.env.example' -Destination 'agent/.env'
    Write-Host 'Created agent/.env. Configure your own provider keys before using research tools.'
}
docker info > $null
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop first.' }
docker compose up -d --build vibe-trading
if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed.' }
Write-Host 'Web app: http://127.0.0.1:19090'
