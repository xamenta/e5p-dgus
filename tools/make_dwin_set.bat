@echo off
REM Builds a flash-ready DWIN_SET for the Ender 5 Plus screen.
REM Run AFTER opening DGUS-reloaded-Klipper-master\project\DWprj.hmi in
REM DGUS Tools and clicking "Generate" (that creates the 13/14/22 *.bin files).
REM
REM Result: DWIN_SET_FLASH\DWIN_SET  -> copy that DWIN_SET folder to the root of
REM a FAT32 (4096-byte allocation) microSD card under 8 GB.

setlocal
set "PRJ=%~dp0..\..\DGUS-reloaded-Klipper-master\Ender5_Project"
set "OUT=%~dp0..\..\DWIN_SET_FLASH\DWIN_SET"

if not exist "%PRJ%\DWIN_SET" (
    echo Cannot find %PRJ%\DWIN_SET
    exit /b 1
)

pushd "%PRJ%"
call rename.bat
popd

set MISSING=0
for %%f in (13_touch.bin 14_variable.bin 22_config.bin) do (
    if not exist "%PRJ%\DWIN_SET\%%f" (
        echo MISSING %%f  - open DWprj.hmi in DGUS Tools and click Generate first.
        set MISSING=1
    )
)
if "%MISSING%"=="1" exit /b 1

if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%"
xcopy /q /y "%PRJ%\DWIN_SET\*" "%OUT%\" > nul

REM Coming from Creality's firmware: use the CFG that wipes the screen's
REM memory before flashing, so no old Creality pages/icons are left behind.
copy /y "%PRJ%\..\extras\flash_reset\T5UID1.CFG" "%OUT%\T5UID1.CFG" > nul

echo.
echo Ready: %OUT%
echo Copy the DWIN_SET folder (not its contents) to the root of the SD card.
endlocal
