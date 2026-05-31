@echo off
setlocal
cd /d "%~dp0"
set PYTHON_EXE=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
  echo Local app environment was not found. Running setup...
  echo.
  call "%~dp0install_dependencies.bat"
  if errorlevel 1 exit /b 1
)
"%PYTHON_EXE%" "%~dp0app\check_dependencies.py" >nul 2>nul
if errorlevel 1 (
  echo Dependencies are missing or requirements changed. Running setup...
  echo.
  call "%~dp0install_dependencies.bat"
  if errorlevel 1 exit /b 1
)
"%PYTHON_EXE%" "%~dp0app\ocr_app.py"
if errorlevel 1 pause
