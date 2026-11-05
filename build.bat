@echo off
cd /d "%~dp0"
echo [1/2] Installing build tools...
python -m pip install -r requirements.txt pyinstaller -U
if errorlevel 1 goto err
echo [2/2] Building (first run also downloads ffmpeg and deno)...
python build.py
if errorlevel 1 goto err
echo.
echo Done: release\YT Downloader
explorer release
pause
goto :eof
:err
echo Build failed.
pause