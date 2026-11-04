@echo off
rem Run the PowerShell launcher from cmd.exe or a double-click, without changing the
rem machine's execution policy. Usage: cairn setup | serve | dispatch | check | test | start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0cairn.ps1" %*
exit /b %ERRORLEVEL%
