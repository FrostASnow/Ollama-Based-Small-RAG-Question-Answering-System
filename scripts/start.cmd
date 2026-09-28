@echo off
rem ===========================================================================
rem  Offline RAG Document QA -- launcher
rem
rem  Double-click this file to start Ollama + backend + frontend.
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe reads .cmd files using the OEM code page (936 on Chinese Windows).
rem  UTF-8 Chinese text gets re-grouped byte-wise into different characters,
rem  which shreds comment lines and makes cmd try to execute the fragments.
rem  All Chinese messages are produced by start.ps1 instead.
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
