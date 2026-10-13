@echo off
rem Сборка gpuflip.exe. Данные (база, .env, профиль Chrome, списки слов) exe хранит в %APPDATA%\gpuflip.
rem Нужен установленный Google Chrome: сбор идёт через него, свой браузер в exe не кладётся.
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name gpuflip ^
  --add-data "gpuflip\ui.html;gpuflip" ^
  --add-data "gpuflip\words;gpuflip\words" ^
  --add-data ".env.example;." ^
  --collect-all playwright ^
  --collect-submodules webview ^
  --hidden-import pystray._win32 ^
  --hidden-import winotify ^
  run_app.py || exit /b 1
echo.
echo Готово: dist\gpuflip\gpuflip.exe
endlocal
