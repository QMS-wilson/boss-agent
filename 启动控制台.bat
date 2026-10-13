@echo off
rem ============================================================
rem  Launch the V3 local Agent console (dev mode / source tree)
rem
rem  Why a .bat: the deps (tkinterdnd2 / openpyxl / xlrd / pymssql)
rem  live in THIS interpreter only. A bare `python agent_gui.py`
rem  picks up whatever `python` is on PATH and exits immediately.
rem
rem  Uses pythonw.exe so no extra console window stays open.
rem ============================================================
cd /d "%~dp0"
start "" "C:\Users\24773\AppData\Local\Programs\Python\Python313\pythonw.exe" agent_gui.py
