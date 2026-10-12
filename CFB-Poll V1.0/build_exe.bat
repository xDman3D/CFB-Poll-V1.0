@echo off
REM Builds CFBPoll.exe. Run this on Windows with Python 3.10 or newer installed.
REM All the .py files must be in the SAME folder as this script.
for %%F in (launcher.py updater.py app_config.py backtest.py cfb_data.py cfb_stats.py cfb_poll_app.py efficiency.py ratings.py) do (
  if not exist "%%F" (
    echo.
    echo ERROR: %%F is missing from this folder. Put ALL the .py files together, then run this again.
    pause
    exit /b 1
  )
)
echo Installing build tools...
python -m pip install --upgrade pip
python -m pip install requests numpy pyinstaller
echo.
echo Building the program (this takes a minute or two)...
python -m PyInstaller --onefile --windowed --clean --name CFBPoll --hidden-import app_config --hidden-import updater --hidden-import backtest --hidden-import cfb_data --hidden-import cfb_stats --hidden-import efficiency --hidden-import ratings --hidden-import cfb_poll_app --hidden-import statistics --hidden-import hashlib --hidden-import zipfile --hidden-import csv --hidden-import json --hidden-import concurrent.futures --hidden-import urllib.request --hidden-import itertools --hidden-import collections --hidden-import decimal --hidden-import fractions --hidden-import random --hidden-import shutil --hidden-import tempfile --hidden-import heapq --hidden-import bisect --hidden-import tkinter.simpledialog launcher.py
echo.
echo ============================================================
echo Done. Your program is:  dist\CFBPoll.exe
echo You can copy that single file anywhere and double-click it.
echo ============================================================
pause
