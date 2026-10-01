@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 launch.py %*
) else (
    python launch.py %*
)
if errorlevel 1 (
    echo [ERROR] Python 3.11+ with Tcl/Tk is required. See docs/STARTUP.md.
    pause
)
endlocal
