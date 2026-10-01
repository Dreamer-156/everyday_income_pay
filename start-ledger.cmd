@echo off
setlocal
pushd "%~dp0"
set "LEDGER_PY=%~dp0.venv\Scripts\python.exe"
if exist "%LEDGER_PY%" goto ready
set "LEDGER_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%LEDGER_PY%" goto ready
where py >nul 2>nul
if not errorlevel 1 (
  set "LEDGER_PY=py"
  goto ready
)
where python >nul 2>nul
if not errorlevel 1 (
  set "LEDGER_PY=python"
  goto ready
)
echo Python 3.10+ was not found. Install Python, then try again.
pause
popd
exit /b 1
:ready
"%LEDGER_PY%" -c "import openpyxl,pdfplumber" >nul 2>nul
if errorlevel 1 (
  echo Missing dependency. Run:
  echo "%LEDGER_PY%" -m pip install -r requirements.txt
  pause
  popd
  exit /b 1
)
"%LEDGER_PY%" launch.py %*
if errorlevel 1 pause
popd
endlocal
