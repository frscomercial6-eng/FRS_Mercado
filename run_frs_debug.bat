@echo off
setlocal
cd /d "%~dp0"
set APP_DIR=dist\FRS_Mercado
set LOG=%~dp0\error.log

if not exist "%APP_DIR%\FRS_Mercado.exe" (
    echo [%DATE% %TIME%] Arquivo executavel ausente: %APP_DIR%\FRS_Mercado.exe > "%LOG%"
    exit /b 1
)

echo [%DATE% %TIME%] Iniciando FRS_Mercado.exe > "%LOG%"
start "" /wait "%APP_DIR%\FRS_Mercado.exe" >> "%LOG%" 2>&1
set EXITCODE=%ERRORLEVEL%
echo [%DATE% %TIME%] Saida do processo: %EXITCODE% >> "%LOG%"
exit /b %EXITCODE%
