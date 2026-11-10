@echo off
setlocal
cd /d "%~dp0"
echo === Nightwatch setup for Windows ===
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3 was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
  echo or run:  winget install Python.Python.3.12
  goto :fail
)
echo Installing Python packages: numpy, opencv-python-headless, PySide6 ...
%PY% -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :fail
echo.
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg was not found on PATH.
  choice /c YN /m "Install ffmpeg now with winget"
  if errorlevel 2 (
    echo Skipped. You can point Nightwatch at an ffmpeg folder in its setup window.
  ) else (
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  )
) else (
  echo ffmpeg found.
)
echo.
echo Done. Double-click Nightwatch.bat to start.
pause
exit /b 0

:fail
echo.
echo Setup did not finish - see the messages above.
pause
exit /b 1
