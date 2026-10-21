@echo off
rem Double-click in Explorer: opens the guided upload.
chcp 65001 >nul
cd /d "%USERPROFILE%"
set XU_QUEST_RECEIPT=1
if exist "%~dp0xsolla_upload.py" (py -3 "%~dp0xsolla_upload.py" & goto end)
where xsolla-upload >nul 2>nul && (xsolla-upload & goto end)
if exist "%LOCALAPPDATA%\xsolla-upload\xsolla-upload.exe" ("%LOCALAPPDATA%\xsolla-upload\xsolla-upload.exe" & goto end)
if exist "%~dp0..\xsolla_upload.py" (py -3 "%~dp0..\xsolla_upload.py" & goto end)
echo xsolla-upload not found. Keep this file inside the xsolla-upload folder, or install: irm .../install.ps1 ^| iex
:end
echo.
pause
