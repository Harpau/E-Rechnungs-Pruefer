@echo off
setlocal
cd /d "%~dp0\.."
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import fastapi, lxml, pypdf, uvicorn" >nul 2>&1
if errorlevel 1 python -m pip install -r requirements.txt
python -m app --open %*
endlocal
