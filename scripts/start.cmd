@echo off
rem ===========================================================================
rem  Offline RAG Document QA -- launcher
rem  Double-click to start Ollama + backend + frontend.
rem  IMPORTANT: keep this file ASCII-only -- cmd.exe reads .cmd with the OEM code page
rem  (936), so UTF-8 Chinese comments get shredded and executed as commands.
rem ===========================================================================
setlocal
cd /d "%~dp0.."

rem Switch console to UTF-8 so the PowerShell output renders correctly
chcp 65001 >nul 2>nul

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
) else (
    powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
)

echo.
echo Service stopped. Press any key to close this window.
pause >nul
