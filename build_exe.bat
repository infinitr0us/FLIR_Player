@echo off
setlocal
cd /d "%~dp0"
call D:\Anaconda\Scripts\activate.bat FLIR
if errorlevel 1 goto :error

rem Install/verify Python dependencies (idempotent, quick when satisfied)
python -m pip install --quiet --disable-pip-version-check -r requirements.txt -r packaging-requirements.txt
if errorlevel 1 goto :error

python packaging\create_icon.py
if errorlevel 1 goto :error

set FLIR_PACKAGE_MODE=onefile
set FLIR_PACKAGE_CONSOLE=0
python -m PyInstaller --noconfirm --clean --distpath release --workpath build\pyinstaller packaging\FLIR_Thermal_Player.spec
if errorlevel 1 goto :error

rem Mark the new build unverified until its smoke test succeeds.
python packaging\finalize_release.py
if errorlevel 1 goto :error

rem Smoke-test the built executable as on a user's PC: with a Windows-only PATH,
rem so no Conda runtime DLL can mask a missing bundled one. Open local\data\2.seq
rem (and 1.ats when present), verify playback advances and that an Excel workbook
rem export succeeds. A crash exits with a negative NTSTATUS code, which
rem "if errorlevel 1" would miss, so the exit code must be exactly 0.
rem Throw-away preferences keep builds out of the developer's recent files.
set "FLIR_SETTINGS_FILE=%TEMP%\flir-build-smoke-settings.ini"
set "BUILD_PATH=%PATH%"
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem"
release\FLIR_Thermal_Player.exe local\data\2.seq --smoke-test
if not "%errorlevel%"=="0" goto :smoke_error
if not exist local\data\1.ats goto :smoke_done
release\FLIR_Thermal_Player.exe local\data\1.ats --smoke-test
if not "%errorlevel%"=="0" goto :smoke_error
:smoke_done
set "PATH=%BUILD_PATH%"
set "FLIR_SETTINGS_FILE="

python packaging\finalize_release.py --smoke-tested
if errorlevel 1 goto :error

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
