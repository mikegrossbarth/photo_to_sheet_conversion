@echo off
setlocal
set PYTHON_EXE=C:\Users\User\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
if not exist "%PYTHON_EXE%" (
  echo Bundled Codex Python was not found:
  echo %PYTHON_EXE%
  pause
  exit /b 1
)
cd /d "%~dp0"
"%PYTHON_EXE%" "%~dp0app\ocr_app.py"
if errorlevel 1 pause

