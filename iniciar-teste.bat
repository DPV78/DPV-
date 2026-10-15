@echo off
REM Carteira Estrategica - instalacao e inicio para TESTE no proprio computador (Windows).
REM Dê dois cliques neste arquivo. Na primeira vez ele instala tudo; nas seguintes, apenas inicia.
chcp 65001 >nul
cd /d "%~dp0"
set LOG=%~dp0instalacao.log
echo ==== %date% %time% ==== > "%LOG%"

echo.
echo [1/5] Procurando o Python...
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto sempython
%PY% -c "import sys; assert sys.version_info >= (3, 11), sys.version" >>"%LOG%" 2>&1 || goto sempython
%PY% --version

echo [2/5] Preparando o ambiente (.venv)...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv >>"%LOG%" 2>&1 || goto erro
)

echo [3/5] Instalando dependencias (a primeira vez pode levar alguns minutos)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt >>"%LOG%" 2>&1 || goto erro

echo [4/5] Configurando chaves e administrador...
".venv\Scripts\python.exe" scripts\preparar_env.py || goto erro

echo [5/5] Iniciando o aplicativo...
echo.
echo   Endereco: http://127.0.0.1:8000
echo   O navegador abre sozinho em alguns segundos.
echo   MANTENHA ESTA JANELA ABERTA. Para encerrar, feche-a ou aperte Ctrl+C.
echo.
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
echo.
echo O aplicativo foi encerrado. Se nao foi voce quem encerrou, copie as mensagens acima e envie ao suporte.
pause
exit /b 0

:sempython
echo.
echo ERRO: Python 3.11 ou mais novo nao encontrado.
echo (Se o Windows abrir a Microsoft Store ao digitar "python", o Python nao esta instalado de verdade.)
echo Instale o Python 3.11 ou mais novo em https://www.python.org/downloads/
echo e MARQUE a opcao "Add python.exe to PATH" na primeira tela. Depois rode este arquivo de novo.
pause
exit /b 1

:erro
echo.
echo ERRO durante a instalacao. Ultimas linhas do registro (instalacao.log):
echo ------------------------------------------------------------
powershell -NoProfile -Command "Get-Content -Tail 25 '%LOG%'"
echo ------------------------------------------------------------
echo Copie as linhas acima e envie. O registro nao contem senhas.
pause
exit /b 1
