@echo off
rem Starts the Nightwatch GUI without a console window. Files/folders dropped on this .bat get queued.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0nightwatch.py" %* & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0nightwatch.py" %* & exit /b 0)
echo Python was not found. Run setup-windows.bat first.
pause
