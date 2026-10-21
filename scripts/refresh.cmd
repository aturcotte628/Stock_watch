@echo off
rem Marketscope - daily data refresh. Run it from Task Scheduler.
setlocal
if not defined PY set "PY=python"
set "DIR=%~dp0.."
set "LOG=%DIR%\data\refresh.log"
cd /d "%DIR%"
if not exist data mkdir data

echo(>> "%LOG%"
echo ==================== %date% %time% : refresh start ====================>> "%LOG%"
"%PY%" load_history.py --refresh                             >> "%LOG%" 2>&1
"%PY%" load_history.py --refresh --interval 1h --workers 8   >> "%LOG%" 2>&1
"%PY%" load_history.py --refresh --interval 5m --workers 8   >> "%LOG%" 2>&1
echo ==================== %date% %time% : refresh done  ====================>> "%LOG%"
endlocal
