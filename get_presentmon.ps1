# Baixa o PresentMon (Intel, codigo aberto, licenca MIT) - usado para medir o FPS dos jogos
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$r = Invoke-RestMethod 'https://api.github.com/repos/GameTechDev/PresentMon/releases/latest' -Headers @{ 'User-Agent' = 'CockpitMonitor' }
$a = $r.assets | Where-Object { $_.name -match '^PresentMon-[\d\.]+-x64\.exe$' } | Select-Object -First 1
if (-not $a) { throw ('Nao achei o PresentMon x64. Arquivos: ' + (($r.assets | ForEach-Object name) -join ', ')) }
Write-Host ("      PresentMon " + $r.tag_name)
$tools = Join-Path $here 'tools'
New-Item $tools -ItemType Directory -Force | Out-Null
$dest = Join-Path $tools 'PresentMon.exe'
Invoke-WebRequest $a.browser_download_url -OutFile $dest -UseBasicParsing
Unblock-File $dest
