@echo off
rem One-time setup. Double-click this file, or run "setup.bat" in this folder.
rem Creates the Python environment, installs the libraries, tests the feeds and the code.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Creating the Python environment in .venv ...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Could not create the environment. Is Python 3.12 installed?
        pause
        exit /b 1
    )
)

echo Installing libraries ...
.venv\Scripts\python -m pip install --upgrade pip --quiet
.venv\Scripts\python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo Installing the libraries failed; see the messages above.
    pause
    exit /b 1
)

echo.
echo Checking feeds (OK = working, FAIL = needs replacing) ...
.venv\Scripts\python check_feeds.py

echo.
echo Running tests ...
.venv\Scripts\python -m pytest -q tests

echo.
echo Done. Next, make an edition and open the reading page:
echo     .venv\Scripts\python newsbot.py run Morning
echo     .venv\Scripts\python newsbot.py open
pause
