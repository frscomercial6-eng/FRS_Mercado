@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0FRS_Mercado.exe" (
	"%~dp0FRS_Mercado.exe" %*
	exit /b %errorlevel%
)
where py >nul 2>nul && py "%~dp0exportar_produtos_xls.py" %* && exit /b %errorlevel%
where python >nul 2>nul && python "%~dp0exportar_produtos_xls.py" %* && exit /b %errorlevel%
echo Python nao encontrado. Execute o aplicativo instalado ou use o arquivo CSV.
exit /b 1
