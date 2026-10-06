@echo off
setlocal EnableExtensions DisableDelayedExpansion
title DevMoney - Verificacoes locais e offline
set "AUDIT_NO_PAUSE="
if /I "%~1"=="--no-pause" set "AUDIT_NO_PAUSE=1"

echo Executando verificacoes locais e offline.
echo Este arquivo nao compila, nao assina e nao publica uma release.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\verify_release_local.ps1"
set "AUDIT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not defined AUDIT_NO_PAUSE pause
exit /b %AUDIT_EXIT_CODE%
