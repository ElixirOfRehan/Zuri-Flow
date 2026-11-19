@echo off
cd /d "%~dp0"
if not exist venv (
  echo Run install.bat first.
  pause
  exit /b 1
)
venv\Scripts\python.exe -m pip install pyinstaller
venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --noconsole --name ZuriFlow --icon logo.ico --add-data "logo-256.png;." --add-data "logo.ico;." --hidden-import pystray._win32 --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --hidden-import keyring.backends.Windows --hidden-import soundfile --hidden-import sounddevice --hidden-import pycaw --hidden-import comtypes --hidden-import PIL._tkinter_finder whisper_app.py
echo.
echo Built dist\ZuriFlow.exe
pause

