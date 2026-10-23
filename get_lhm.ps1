# Baixa o LibreHardwareMonitor (versao .NET Framework, a que o Python consegue carregar)
# e copia as DLLs para a pasta lib. Tambem guarda o app completo em LibreHardwareMonitor\
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$r = Invoke-RestMethod 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' -Headers @{ 'User-Agent' = 'CockpitMonitor' }
$zips = @($r.assets | Where-Object { $_.name -like '*.zip' })
# A versao certa e a SEM ".NET" no nome (ex.: LibreHardwareMonitor.zip).
# A "LibreHardwareMonitor.NET.10.zip" e para .NET 10 e NAO funciona com o Python.
$a = $zips | Where-Object { $_.name -eq 'LibreHardwareMonitor.zip' } | Select-Object -First 1
if (-not $a) { $a = $zips | Where-Object { $_.name -notmatch '\.NET|net\d' } | Select-Object -First 1 }
if (-not $a) { throw ('Nao achei a versao .NET Framework. Arquivos: ' + (($zips | ForEach-Object name) -join ', ')) }
Write-Host ("      versao " + $r.tag_name + " - " + $a.name)
$tmp = Join-Path $env:TEMP 'lhm_cockpit'
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item $tmp -ItemType Directory | Out-Null
$zip = Join-Path $tmp 'lhm.zip'
Invoke-WebRequest $a.browser_download_url -OutFile $zip -UseBasicParsing
$x = Join-Path $tmp 'x'
Expand-Archive $zip $x -Force
$lib = Join-Path $here 'lib'
New-Item $lib -ItemType Directory -Force | Out-Null
Get-ChildItem $lib -Filter *.dll | Remove-Item -Force          # tira DLLs antigas/erradas
Get-ChildItem $x -Recurse -Filter *.dll | Copy-Item -Destination $lib -Force
Get-ChildItem $lib | Unblock-File
Set-Content (Join-Path $lib 'lhm_versao.txt') ("netfx " + $r.tag_name + " " + $a.name)
# app completo (abra o LibreHardwareMonitor.exe como admin se a CPU ficar em N/D)
$app = Join-Path $here 'LibreHardwareMonitor'
Remove-Item $app -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item $x $app -Recurse
Get-ChildItem $app -Recurse | Unblock-File
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
