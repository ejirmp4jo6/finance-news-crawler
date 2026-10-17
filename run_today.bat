@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 crawler.py %*
set "crawler_result=%errorlevel%"
echo Finished with exit code %crawler_result%. See data folder and report JSON.
pause
exit /b %crawler_result%
