@echo off
rem Build gpuflip.exe (run from the project folder). Data lives in %APPDATA%\gpuflip, not next to the exe.
rem Google Chrome must be installed: the collector drives it, no browser is bundled.
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name gpuflip ^
  --add-data "gpuflip\web;gpuflip\web" ^
  --add-data "gpuflip\words;gpuflip\words" ^
  --add-data ".env.example;." ^
  --collect-all playwright ^
  --collect-submodules webview ^
  --hidden-import pystray._win32 ^
  --hidden-import winotify ^
  run_app.py
if errorlevel 1 exit /b 1
echo.
echo Done: dist\gpuflip\gpuflip.exe
endlocal
