@echo off
setlocal
cd /d "%~dp0"

echo Iniciando o sistema...
echo (para desligar, feche esta janela)
echo.

start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"
".venv\Scripts\python.exe" "app.py"

pause
