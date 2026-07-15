@echo off
REM Launcher for PATH: add this scripts\ folder to your PATH, then run:
REM   notion serve
REM   notion setup
REM   notion init --cookie "..."
setlocal
set "SCRIPT_DIR=%~dp0"
set "ROOT=%SCRIPT_DIR%.."
cd /d "%ROOT%"

if exist "%ROOT%\.venv\Scripts\python.exe" (
  "%ROOT%\.venv\Scripts\python.exe" -m notionchat %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python -m notionchat %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -m notionchat %*
  exit /b %ERRORLEVEL%
)

echo Error: Python not found. Activate .venv or install Python 3.11+.
exit /b 1
