@echo off
setlocal
set "APP_DIR=%~dp0"
set "CODEX_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%CODEX_PY%" (
    "%CODEX_PY%" "%APP_DIR%dtm_to_sav_gui.py"
    exit /b %errorlevel%
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%APP_DIR%dtm_to_sav_gui.py"
    exit /b %errorlevel%
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%APP_DIR%dtm_to_sav_gui.py"
    exit /b %errorlevel%
)

echo Python 3 was not found.
echo Install Python 3 with Tkinter, then run dtm_to_sav_gui.py.
pause
exit /b 2

rem To build a one-file EXE from this folder:
rem python -m pip install pyinstaller
rem python -m PyInstaller --clean --onefile --windowed --name DiscordTimes_QuestSync --icon "assets\Icon.ico" --add-data "assets;assets" --add-data "army_catalog.json;." --add-data "army_native_profiles.json;." dtm_to_sav_gui.py
