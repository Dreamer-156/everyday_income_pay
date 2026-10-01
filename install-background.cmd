@echo off
setlocal
cscript.exe //NoLogo "%~dp0install-background.vbs"
if errorlevel 1 (
  echo Setup failed. Please read the error above.
  pause
  exit /b 1
)
call "%~dp0start-ledger.cmd"
endlocal
