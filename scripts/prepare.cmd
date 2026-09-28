@echo off
rem ===========================================================================
rem  Offline RAG Document QA -- one-time online preparation
rem
rem  Double-click to install dependencies + download embedding model +
rem  download portable Ollama + pull the LLM. Needs internet, run once.
rem  After it finishes the app works fully offline.
rem
rem  IMPORTANT: keep this file ASCII-only (see start.cmd for the reason).
rem ===========================================================================
setlocal
cd /d "%~dp0.."

chcp 65001 >nul 2>nul

echo ===========================================================================
echo  Offline RAG Document QA -- Preparation
echo ===========================================================================
echo.
echo  This step needs internet access and will:
echo    1. Prepare the Python 3.12 runtime   (.venv)
echo    2. Install Python dependencies       (~300 MB)
echo    3. Download all-MiniLM-L6-v2         (~90 MB)
echo    4. Download portable Ollama + LLM    (~1.5 GB + ~1.1 GB)
echo.
echo  When it is done, everything runs offline.
echo.
pause

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare.ps1" %*
) else (
    powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0prepare.ps1" %*
)

echo.
echo Preparation script finished. Press any key to close this window.
pause >nul
