@echo off
rem Launch the AJAZZ mouse battery tray silently (no console window).
rem Looks for pythonw.exe in the usual places instead of hardcoding one path.
setlocal
set PYW=
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "C:\Python313\pythonw.exe"
  "C:\Python312\pythonw.exe"
  "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
) do if exist %%P if not defined PYW set PYW=%%~P

if not defined PYW for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYW set PYW=%%P
if not defined PYW for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYW set PYW=%%P

if not defined PYW (
  echo Python not found. Install Python 3 or edit this file with your pythonw.exe path.
  pause
  exit /b 1
)

start "" "%PYW%" "%~dp0ajazz_tray.py"
exit
