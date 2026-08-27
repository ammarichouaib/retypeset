@echo off
REM Build the Windows application: retypeset.exe, a portable zip, and an
REM installer if Inno Setup is present. Double-click this file.
REM
REM Needs: Windows, Python 3.10+ on PATH, and an internet connection the first
REM time (it downloads pandoc and the build tools). Takes about ten minutes.

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

powershell -ExecutionPolicy Bypass -File "%~dp0packaging\build_windows.ps1" %*
set RC=%ERRORLEVEL%

if %RC% EQU 0 (
  echo.
  echo Done. The application is in:  dist\retypeset\retypeset.exe
  echo A portable zip and, if Inno Setup is installed, an installer are in dist\
) else (
  echo.
  echo The build did not finish. The message above says why.
)
pause
exit /b %RC%
