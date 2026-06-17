@echo off
rem Build USB-I2C-TestTool.exe
cd /d "%~dp0"

echo Cleaning old build...
rmdir /s /q build dist 2>nul
del /f /q *.spec 2>nul

echo Building EXE...
python -m PyInstaller --onefile --name "USB-I2C-TestTool" ^
    --add-data "example_script.i2c;." ^
    i2c_test_tool.py

echo.
echo Done! EXE at:
echo   %~dp0dist\USB-I2C-TestTool.exe
pause
