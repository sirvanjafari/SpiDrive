@echo off
setlocal
title SpiDrive
cd /d "%~dp0"

rem  SpiDrive: one file that installs what is missing, then starts SpiDrive.
rem  Run it every time. The first run takes a few minutes; later runs start in seconds.
rem    SpiDrive.bat           start with the icon next to the clock
rem    SpiDrive.bat console   start in this window and show the log here

echo.
echo  SpiDrive
echo  ========

rem ---------------------------------------------------------------- 1. Python
set "PYEXE=venv\Scripts\python.exe"
if exist "%PYEXE%" goto have_venv
echo.
echo  [1/5] Looking for Python...
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto no_python
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto old_python
echo  Creating the venv folder...
%PY% -m venv venv
if not exist "%PYEXE%" goto failed
:have_venv
echo  [1/5] Python: OK

rem ---------------------------------------------------------------- 2. packages
fc /b requirements.txt venv\spidrive_requirements.txt >nul 2>&1
if not errorlevel 1 goto packages_ok
echo  [2/5] Installing the packages SpiDrive needs (first time only)...
"%PYEXE%" -m pip install --disable-pip-version-check -q -r requirements.txt pystray pillow
if not errorlevel 1 goto packages_done
call :local_http_proxy
if not defined LOCALPROXY goto pip_failed
echo  Direct download failed. Trying again through %LOCALPROXY% ...
set "HTTP_PROXY=%LOCALPROXY%"
set "HTTPS_PROXY=%LOCALPROXY%"
"%PYEXE%" -m pip install --disable-pip-version-check -q -r requirements.txt pystray pillow
if errorlevel 1 goto pip_failed
:packages_done
"%PYEXE%" -m pip install --disable-pip-version-check -q cryptg >nul 2>&1
copy /y requirements.txt venv\spidrive_requirements.txt >nul
:packages_ok
echo  [2/5] Packages: OK

rem ---------------------------------------------------------------- 3. WinFsp
call :check_winfsp
if defined WINFSP goto winfsp_ok
echo  [3/5] Installing WinFsp, which lets SpiDrive create drive S:.
echo        Windows will ask for permission: click Yes.
call :has_winget
if defined WINGET winget install --id WinFsp.WinFsp -e --accept-package-agreements --accept-source-agreements >nul
call :check_winfsp
if defined WINFSP goto winfsp_ok
echo.
echo  WinFsp could not be installed automatically. The download page opens now:
echo  install the .msi file, then run SpiDrive.bat again.
echo  SpiDrive still starts, but without drive S: until WinFsp is installed.
start "" "https://winfsp.dev/rel/"
goto winfsp_done
:winfsp_ok
echo  [3/5] WinFsp: OK
:winfsp_done

rem ---------------------------------------------------------------- 4. rclone
call :find_rclone
if defined RCLONE goto rclone_ok
echo  [4/5] Installing rclone, which connects drive S:...
call :has_winget
if defined WINGET winget install --id Rclone.Rclone -e --accept-package-agreements --accept-source-agreements >nul
call :find_rclone
if defined RCLONE goto rclone_ok
echo.
echo  rclone could not be installed automatically. The download page opens now:
echo  download "Windows - Intel/AMD - 64 Bit", copy rclone.exe from the zip into
echo  %~dp0
echo  and run SpiDrive.bat again. SpiDrive still starts, without drive S:.
start "" "https://rclone.org/downloads/"
goto rclone_done
:rclone_ok
echo  [4/5] rclone: OK
:rclone_done

rem the old network-drive version of S: would block the new drive
powershell -NoProfile -Command "Get-CimInstance Win32_NetworkConnection -ErrorAction SilentlyContinue | Where-Object { $_.RemoteName -like '*DavWWWRoot*' } | ForEach-Object { net use $_.LocalName /delete /y | Out-Null }" >nul 2>&1

rem ---------------------------------------------------------------- 5. start
echo  [5/5] Starting SpiDrive...
if /i "%~1"=="console" goto run_console
start "" "venv\Scripts\pythonw.exe" app.py --tray
echo.
echo  SpiDrive is running. Look for its icon next to the clock.
echo  The first time, a setup page opens in your browser: log in to Telegram there.
echo  If Telegram is blocked, SpiDrive finds a proxy by itself (or paste one on that page).
echo  Log file: %LOCALAPPDATA%\SpiDrive\spidrive.log
timeout /t 8 >nul
exit /b 0

:run_console
"%PYEXE%" app.py --console
pause
exit /b 0

rem ================================================================ problems

:no_python
echo.
echo  Python is not installed. The download page opens now.
echo  Install Python 3.10 or newer and tick "Add python.exe to PATH" on the first screen,
echo  then run SpiDrive.bat again.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:old_python
echo.
echo  Your Python is too old. Install Python 3.10 or newer from python.org,
echo  then run SpiDrive.bat again.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:pip_failed
echo.
echo  The packages could not be downloaded. Turn on your VPN for this first install,
echo  then run SpiDrive.bat again. Later starts do not need it.
pause
exit /b 1

:failed
echo.
echo  Something went wrong above. Run SpiDrive.bat again; if it repeats, send the message above.
pause
exit /b 1

rem ================================================================ helpers

:check_winfsp
set "WINFSP="
if exist "%ProgramFiles(x86)%\WinFsp\bin\winfsp-x64.dll" set "WINFSP=1"
if exist "%ProgramFiles%\WinFsp\bin\winfsp-x64.dll" set "WINFSP=1"
exit /b 0

:has_winget
set "WINGET="
where winget >nul 2>&1 && set "WINGET=1"
exit /b 0

:find_rclone
set "RCLONE="
if exist "%~dp0rclone.exe" set "RCLONE=%~dp0rclone.exe"
if not defined RCLONE for /f "delims=" %%i in ('where rclone 2^>nul') do if not defined RCLONE set "RCLONE=%%i"
if not defined RCLONE if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\rclone.exe" set "RCLONE=%LOCALAPPDATA%\Microsoft\WinGet\Links\rclone.exe"
if not defined RCLONE for /f "delims=" %%i in ('dir /s /b "%LOCALAPPDATA%\Microsoft\WinGet\Packages\rclone.exe" 2^>nul') do if not defined RCLONE set "RCLONE=%%i"
exit /b 0

:local_http_proxy
rem an HTTP proxy from a VPN app on this PC (v2rayN 10809 and 10811, Clash 7890, Nekoray 2081)
set "LOCALPROXY="
for %%p in (10809 7890 2081 10811) do (
  if not defined LOCALPROXY (
    powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', %%p); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1 && set "LOCALPROXY=http://127.0.0.1:%%p"
  )
)
exit /b 0
