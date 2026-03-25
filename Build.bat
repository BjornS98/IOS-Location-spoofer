@echo off
setlocal enabledelayedexpansion

REM Name of your Python entry file (without .py extension)
set "APP_NAME=SpoofingUI"

REM Run PyInstaller
echo Building %APP_NAME%.exe with PyInstaller...
pyinstaller --onefile --windowed --debug=imports ^
  --icon="Assets\Icon.ico" ^
  --add-data "Assets\Map.html;Assets" ^
  --add-data "Assets\Icon.ico;Assets" ^
  --add-data "Server.py;." ^
  --hidden-import=zeroconf._utils.ipaddress ^
  --hidden-import=zeroconf._handlers.answers ^
  --hidden-import=zeroconf._handlers.questions ^
  --hidden-import=zeroconf._handlers.records ^
  --hidden-import=zeroconf._services.info ^
  --hidden-import=zeroconf._services.browsing ^
  --hidden-import=zeroconf._listener ^
  --collect-binaries=pytun_pmd3 ^
  "%APP_NAME%.py"

REM Check if EXE exists and move it to current folder
if exist "dist\%APP_NAME%.exe" (
    echo Moving %APP_NAME%.exe to current folder...
    move /Y "dist\%APP_NAME%.exe" .
) else (
    echo ERROR: dist\%APP_NAME%.exe not found.
    pause
    exit /b 1
)

REM Cleanup build artifacts
if exist dist (
    echo Deleting dist folder...
    rmdir /S /Q dist
)
if exist build (
    echo Deleting build folder...
    rmdir /S /Q build
)
if exist "%APP_NAME%.spec" (
    echo Deleting %APP_NAME%.spec...
    del /F /Q "%APP_NAME%.spec"
)

echo Done.
pause
