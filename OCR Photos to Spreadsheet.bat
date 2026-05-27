@echo off
setlocal
cd /d "%~dp0"
set PYTHON_EXE=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
  echo Local app environment was not found.
  echo.
  echo Run install_dependencies.bat first.
  pause
  exit /b 1
)
"%PYTHON_EXE%" "%~dp0app\ocr_app.py"
if errorlevel 1 pause
