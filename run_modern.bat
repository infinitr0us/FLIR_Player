@echo off
setlocal
call D:\Anaconda\Scripts\activate.bat FLIR
python "%~dp0flir_player_app.py" %*
if errorlevel 1 pause
endlocal
