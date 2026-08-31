@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    set /p PASTA=Arraste a pasta do cliente para aqui e aperte Enter:
) else (
    set PASTA=%~1
)
set PASTA=%PASTA:"=%

if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo [ERRO] A variavel ANTHROPIC_API_KEY nao esta configurada.
    echo Veja o arquivo COMO_USAR.txt para instrucoes de configuracao.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "preencher_peticao.py" "%PASTA%"

echo.
pause
