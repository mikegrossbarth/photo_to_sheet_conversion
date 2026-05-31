@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set VENV_DIR=%~dp0.venv
set PYTHON_VERSION=3.12.10
set PYTHON_INSTALL_DIR=%LocalAppData%\Programs\Python\Python312
set PYTHON_INSTALLER=%TEMP%\python-%PYTHON_VERSION%-amd64.exe

echo Setting up OCR Photos to Spreadsheet...
echo.

call :FindPython
if not defined PYTHON_CMD call :InstallPython
if not defined PYTHON_CMD (
  echo.
  echo Python setup failed. Please send a screenshot of this window.
  pause
  exit /b 1
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo Creating local virtual environment...
  "%PYTHON_CMD%" -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo.
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

set PYTHON_EXE=%VENV_DIR%\Scripts\python.exe

"%PYTHON_EXE%" "%~dp0app\check_dependencies.py" >nul 2>nul
if %errorlevel%==0 (
  echo Dependencies are already installed.
  echo.
  echo Setup complete.
  pause
  exit /b 0
)

echo Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 (
  echo.
  echo pip upgrade failed.
  pause
  exit /b 1
)

echo Installing app dependencies...
"%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Dependency install failed.
  pause
  exit /b 1
)

"%PYTHON_EXE%" "%~dp0app\write_dependency_stamp.py"
if errorlevel 1 (
  echo.
  echo Could not write dependency stamp.
  pause
  exit /b 1
)

echo.
echo Setup complete.
pause
exit /b 0

:FindPython
set PYTHON_CMD=
where py >nul 2>nul
if %errorlevel%==0 (
  for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON_CMD=%%P
  if defined PYTHON_CMD exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
  for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON_CMD=%%P
  if defined PYTHON_CMD exit /b 0
)
if exist "%PYTHON_INSTALL_DIR%\python.exe" (
  set PYTHON_CMD=%PYTHON_INSTALL_DIR%\python.exe
)
exit /b 0

:InstallPython
echo Python was not found. Downloading Python %PYTHON_VERSION% for Windows...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-amd64.exe' -OutFile '%PYTHON_INSTALLER%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
  echo.
  echo Could not download Python.
  echo Check your internet connection and try again.
  exit /b 1
)

echo Installing Python for this Windows user...
"%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 TargetDir="%PYTHON_INSTALL_DIR%" PrependPath=1 Include_pip=1 Include_test=0 SimpleInstall=1
if errorlevel 1 (
  echo.
  echo Python installer failed.
  exit /b 1
)

if exist "%PYTHON_INSTALL_DIR%\python.exe" (
  set PYTHON_CMD=%PYTHON_INSTALL_DIR%\python.exe
)
exit /b 0
