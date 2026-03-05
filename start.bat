@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

:: Defaults
set NODE_COUNT=1
set DEBUG_FLAG=

:: Parse arguments (any order)
:parse_args
if "%~1"=="" goto done_args
if /i "%~1"=="--debug" (
    set DEBUG_FLAG=--debug
    shift
    goto parse_args
)
if /i "%~1"=="--nodes" (
    set NODE_COUNT=%~2
    shift & shift
    goto parse_args
)
shift
goto parse_args
:done_args

echo Starting onion network (%NODE_COUNT% node(s) per type)...
if defined DEBUG_FLAG echo Debug mode enabled for nodes.

start "Dir-Server"  cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\directory_server.py"
timeout /t 1 /nobreak >nul

start "Dest-Server" cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\server.py --port 9000"
timeout /t 1 /nobreak >nul

for /L %%i in (1,1,%NODE_COUNT%) do (
    set /a ENTRY_PORT=9000+%%i
    set /a MIDDLE_PORT=9100+%%i
    set /a EXIT_PORT=9200+%%i
    start "Entry-Node-%%i"  cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\node.py --type entry  --port !ENTRY_PORT!  %DEBUG_FLAG%"
    timeout /t 1 /nobreak >nul
    start "Middle-Node-%%i" cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\node.py --type middle --port !MIDDLE_PORT! %DEBUG_FLAG%"
    timeout /t 1 /nobreak >nul
    start "Exit-Node-%%i"   cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\node.py --type exit   --port !EXIT_PORT!   %DEBUG_FLAG%"
    timeout /t 1 /nobreak >nul
)

timeout /t 1 /nobreak >nul
start "Tor-Client"  cmd /k "cd /d "%~dp0" && venv\Scripts\activate && python Servers\client.py --dest-port 9000"

echo.
echo All components running. Press any key to stop everything...
pause >nul

:cleanup
echo Stopping all components...
taskkill /FI "WINDOWTITLE eq Dir-Server"  /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Dest-Server" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Tor-Client"  /T /F >nul 2>&1
for /L %%i in (1,1,%NODE_COUNT%) do (
    taskkill /FI "WINDOWTITLE eq Entry-Node-%%i"  /T /F >nul 2>&1
    taskkill /FI "WINDOWTITLE eq Middle-Node-%%i" /T /F >nul 2>&1
    taskkill /FI "WINDOWTITLE eq Exit-Node-%%i"   /T /F >nul 2>&1
)
echo Done.
