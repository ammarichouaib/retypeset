@echo off
REM Publish this project to GitHub. Double-click, or run from a terminal with
REM extra arguments, e.g.:  publish_to_github.bat --tag v0.8.2
REM
REM The token is read from %GITHUB_TOKEN% if it is set; otherwise you are asked
REM for it and nothing is echoed as you paste.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH" during setup, then open a new terminal.
  pause
  exit /b 1
)

python tools\publish_github.py %*
set RC=%ERRORLEVEL%

if %RC% NEQ 0 (
  echo.
  echo Publishing did not complete. The message above says why.
)
pause
exit /b %RC%
