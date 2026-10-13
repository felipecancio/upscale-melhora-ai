@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -c "import PIL" 1>nul 2>nul || py -3 -m pip install -r requirements.txt
py -3 -m alto
if errorlevel 1 (
  echo.
  echo O Alto encerrou com erro.
  pause
)
