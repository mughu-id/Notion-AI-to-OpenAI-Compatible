@echo off
REM NotionChat Windows launcher — add this scripts\ folder to your user PATH.
REM Works from any directory. Example:
REM   notion serve
REM   notion setup
REM   notion init --cookie "..."
setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
REM Resolve project root (parent of scripts\)
for %%I in ("%SCRIPT_DIR%..") do set "ROOT=%%~fI"

if not defined NOTIONCHAT_HOME set "NOTIONCHAT_HOME=%ROOT%"
cd /d "%NOTIONCHAT_HOME%" 2>nul
if errorlevel 1 (
  echo Error: NOTIONCHAT_HOME is invalid: %NOTIONCHAT_HOME%
  exit /b 1
)

if exist "%NOTIONCHAT_HOME%\.venv\Scripts\python.exe" (
  "%NOTIONCHAT_HOME%\.venv\Scripts\python.exe" -m notionchat %*
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

echo Error: Python not found. Create a venv in the project and install deps:
echo   cd "%NOTIONCHAT_HOME%"
echo   python -m venv .venv
echo   .venv\Scripts\activate
echo   pip install -r requirements.txt
echo   pip install -e .
exit /b 1
