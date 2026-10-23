@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Cockpit Monitor - gerando o app portatil
echo.
echo  ===============================================
echo    COCKPIT MONITOR - gerando o app portatil
echo  ===============================================
echo.

where python >nul 2>nul || (
  echo [ERRO] Python nao encontrado. Instale em https://www.python.org/downloads/
  echo        e marque a opcao "Add python.exe to PATH" na instalacao.
  goto erro
)

echo [1/4] Instalando dependencias...
python -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto erro

if exist "lib\lhm_versao.txt" goto tem_dll
echo [2/4] Baixando o LibreHardwareMonitor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_lhm.ps1"
if errorlevel 1 goto sem_dll
if not exist "lib\LibreHardwareMonitorLib.dll" goto sem_dll
goto dll_ok
:tem_dll
echo [2/4] DLLs do LibreHardwareMonitor ja estao na pasta lib.
:dll_ok

if exist "tools\PresentMon.exe" goto tem_pm
echo       Baixando o PresentMon ^(medidor de FPS^)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_presentmon.ps1"
if errorlevel 1 echo       [aviso] PresentMon nao baixou - o relogio de FPS fica desativado.
:tem_pm
if not exist "tools" mkdir "tools"
if not exist "tools\LEIA-ME.txt" echo Ferramentas usadas pelo Cockpit Monitor > "tools\LEIA-ME.txt"

echo [3/4] Gerando CockpitMonitor.exe ^(leva alguns minutos^)...
python -m PyInstaller --noconfirm --clean --onedir --windowed --uac-admin --log-level WARN ^
  --name CockpitMonitor --icon cockpit.ico ^
  --add-data "lib;lib" --add-data "tools;tools" --add-data "fonts;fonts" --add-data "cockpit.ico;." --add-data "cockpit_logo.png;." ^
  --collect-all pythonnet --collect-all clr_loader ^
  cockpit_monitor.py || goto erro

echo [4/4] Montando a pasta portatil...
if exist "Portatil" rmdir /s /q "Portatil"
mkdir "Portatil"
xcopy /e /i /q /y "dist\CockpitMonitor" "Portatil\CockpitMonitor" >nul
(
  echo COCKPIT MONITOR - versao portatil
  echo.
  echo Basta abrir o CockpitMonitor.exe. Nao precisa instalar nem ter Python.
  echo Mantenha o .exe junto com a pasta _internal ^(copie sempre a pasta inteira^).
  echo Ele pede permissao de administrador para ler CPU, SSD e ventoinhas.
  echo.
  echo Para iniciar com o Windows: deixe o .exe numa pasta fixa e marque
  echo Configuracoes ^> Geral ^> "Iniciar junto com o Windows".
) > "Portatil\CockpitMonitor\LEIA-ME.txt"

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
  echo Gerando tambem o instalador...
  "%ISCC%" /Q installer.iss && echo Instalador: dist\CockpitMonitor_Setup.exe
)

echo.
echo  PRONTO!  App portatil em:  %~dp0Portatil\CockpitMonitor\CockpitMonitor.exe
echo  Copie a PASTA CockpitMonitor inteira ^(o .exe precisa da pasta _internal junto^).
echo.
explorer "%~dp0Portatil"
pause
exit /b 0

:sem_dll
echo [ERRO] Nao consegui baixar o LibreHardwareMonitor.
echo        Baixe em https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases
echo        e copie todas as .dll do zip para a pasta "lib". Depois rode de novo.

:erro
echo.
echo Algo deu errado - veja as mensagens acima e me mande o texto.
pause
exit /b 1
