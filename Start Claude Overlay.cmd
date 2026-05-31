@echo off
rem Launch Claude Overlay with no console window. Portable: finds pythonw/pyw on PATH.
cd /d "%~dp0"
where pythonw >nul 2>nul && ( start "" pythonw "%~dp0claude_overlay.py" & exit /b )
where pyw     >nul 2>nul && ( start "" pyw     "%~dp0claude_overlay.py" & exit /b )
echo Could not find pythonw / pyw on PATH.
echo Install Python 3 from python.org, then run setup.cmd first.
pause
