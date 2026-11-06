@echo off
setlocal
cd /d "%~dp0"
echo.
echo  Building SpiDrive
echo  =================

echo.
echo  Step 1 of 4: build tools...
set "PY=venv\Scripts\python.exe"
if exist "%PY%" goto have_venv
where py >nul 2>&1 && py -3 -m venv venv
if not exist "%PY%" python -m venv venv
if not exist "%PY%" goto failed
:have_venv
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller pystray pillow
if errorlevel 1 goto failed
"%PY%" -m pip install --disable-pip-version-check -q cryptg >nul 2>&1
echo  OK

echo.
echo  Step 2 of 4: icon, app ID, rclone, WinFsp...
"%PY%" build_helper.py
if errorlevel 1 goto failed

echo.
echo  Step 3 of 4: SpiDrive.exe (takes a minute or two)...
set "RCL="
if exist "build\rclone.exe" set RCL=--add-binary "build\rclone.exe;."
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN --windowed --name SpiDrive ^
  --icon "build\spidrive.ico" --add-data "web;web" %RCL% ^
  --collect-submodules telethon --collect-submodules python_socks --hidden-import proxy --hidden-import pystray._win32 --hidden-import app_id ^
  --hidden-import server --hidden-import setup_server ^
  app.py
if errorlevel 1 goto failed
echo  OK: dist\SpiDrive\SpiDrive.exe

echo.
echo  Step 4 of 4: the installer...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC goto have_iscc
echo  Installing Inno Setup (free) to build the installer...
winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC goto have_iscc
echo.
echo  Inno Setup is missing. SpiDrive.exe is ready in dist\SpiDrive, but to get the
echo  one-file installer, install Inno Setup from https://jrsoftware.org/isdl.php
echo  and run build.bat again.
pause
exit /b 1
:have_iscc
"%ISCC%" /Q SpiDrive.iss
if errorlevel 1 goto failed

echo.
echo  Done:  dist\SpiDrive-Setup.exe
echo.
pause
exit /b 0

:failed
echo.
echo  The build stopped because of the error above.
pause
exit /b 1
