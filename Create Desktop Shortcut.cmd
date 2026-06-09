@echo off
rem Double-click to drop a "Claude Overlay" shortcut (with the orb icon) on your Desktop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-shortcut.ps1"
echo.
pause
