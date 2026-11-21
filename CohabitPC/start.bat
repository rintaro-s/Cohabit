@echo off
rem Start Cohabit PC (Windows). Needs Python 3.9+ only; nothing to pip install.
cd /d "%~dp0"
where py >NUL 2>NUL
if %errorlevel%==0 (py -3 cohabit_pc.py %*) else (python cohabit_pc.py %*)
pause
