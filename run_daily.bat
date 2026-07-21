@echo off
:: CONDA_ENV is where the conda environment is installed, not the project folder.
:: Moving the project folder does NOT require changing this line -- only change it
:: if the conda environment itself gets reinstalled or moved.
:: (Kept ASCII-only on purpose: non-ASCII bytes in this file were found to corrupt
:: variable parsing intermittently when this script is launched by Windows Task
:: Scheduler, though not when run interactively -- see git history for details.)
set CONDA_ENV=D:\condaenvs\whisper-conda
set PATH=%CONDA_ENV%;%CONDA_ENV%\Library\mingw-w64\bin;%CONDA_ENV%\Library\usr\bin;%CONDA_ENV%\Library\bin;%CONDA_ENV%\Scripts;%CONDA_ENV%\bin;%PATH%

cd /d %~dp0

"%CONDA_ENV%\python.exe" prompt_before_run.py >> logs\prompt_debug.log 2>&1
set PROMPT_EXIT=%errorlevel%
echo EXITCODE_WAS_%PROMPT_EXIT% >> logs\prompt_debug.log
:: prompt_before_run.py only ever exits with 0 (proceed) or 1 (cancel) on its own.
:: Anything else means it was killed/crashed abnormally -- treat that as "unknown state,
:: don't run" rather than falling through to a full GPU transcription run unattended.
if not "%PROMPT_EXIT%"=="0" goto :skip

echo ABOUT_TO_START_MAIN_SCRIPT >> logs\prompt_debug.log
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set TIMESTAMP=%%T
set LOGFILE=logs\run_%TIMESTAMP%.log
"%CONDA_ENV%\python.exe" daily_podcast.py >> "%LOGFILE%" 2>&1
goto :eof

:skip
echo todayskipped >> logs\skip_log.txt
goto :eof