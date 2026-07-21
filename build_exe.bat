@echo off
setlocal
cd /d "%~dp0"
call D:\Anaconda\Scripts\activate.bat FLIR
if errorlevel 1 goto :error

rem Install/verify Python dependencies (idempotent, quick when satisfied)
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

python packaging\create_icon.py
if errorlevel 1 goto :error

set FLIR_PACKAGE_MODE=onefile
set FLIR_PACKAGE_CONSOLE=0
python -m PyInstaller --noconfirm --clean --distpath release --workpath build\pyinstaller packaging\FLIR_Thermal_Player.spec
if errorlevel 1 goto :error

rem Regenerate SHA256SUMS.txt, BUILD_INFO.txt and README.txt for the release
python packaging\finalize_release.py
if errorlevel 1 goto :error

rem Smoke-test the built executable: open 2.seq, verify playback advances
release\FLIR_Thermal_Player.exe 2.seq --smoke-test
if errorlevel 1 goto :smoke_error

echo.
echo Built and verified: %CD%\release\FLIR_Thermal_Player.exe
exit /b 0

:smoke_error
echo.
echo WARNING: the executable was built but failed the smoke test.
exit /b 2

:error
set ERR=%errorlevel%
echo.
echo Packaging failed with exit code %ERR%.
exit /b %ERR%
