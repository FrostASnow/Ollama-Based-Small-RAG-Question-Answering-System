@echo off
rem ===========================================================================
rem  build-launcher.cmd -- ASCII only (cmd.exe reads OEM codepage)
rem  Builds RAG-QA.exe with the .NET Framework C# compiler (no SDK needed).
rem ===========================================================================
chcp 65001 >nul 2>&1
setlocal

set "ROOT=%~dp0.."
set "PS=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PS=pwsh.exe"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-launcher.ps1" %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo [FAIL] build failed with exit code %CODE%
)

echo.
pause
exit /b %CODE%
