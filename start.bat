@echo off
REM Condominio — start both backend and frontend on Windows.
REM Backend runs in this window; frontend opens in a new one.

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate
)

if not exist frontend\node_modules (
    echo Installing frontend dependencies...
    pushd frontend
    call npm install
    popd
)

start "Condominio frontend" cmd /k "cd frontend && npm run dev"
python -m backend.main
