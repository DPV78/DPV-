@echo off
REM Inicia a Carteira Estrategica no Windows. Para iniciar junto com o servidor,
REM crie uma tarefa no Agendador de Tarefas (disparo: "Ao iniciar o computador") apontando para este arquivo.
cd /d %~dp0..
call .venv\Scripts\activate.bat
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
