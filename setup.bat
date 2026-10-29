@echo off
echo ========================================================
echo  Race Engineer for F1 25 - Windows Setup
echo ========================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.11+ was not found in your PATH.
    echo Please install Python 3.11 or later from python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

echo [1/3] Creating Python virtual environment in .venv ...
python -m venv .venv
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

echo [2/3] Activating virtual environment ...
call .venv\Scripts\activate.bat

echo [3/3] Upgrading pip and installing dependencies ...
python -m pip install --upgrade pip
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo  Setup completed successfully!
echo  Run "run.bat" to start the race engineer.
echo  Run "run.bat --list-devices" to check audio devices.
echo ========================================================
pause
