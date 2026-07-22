@echo off
:: CONDA_ENV is where the conda environment is installed, not the project folder.
:: Moving the project folder does NOT require changing this line -- only change it
:: if the conda environment itself gets reinstalled or moved.
:: (Kept ASCII-only on purpose: non-ASCII bytes in this file were found to corrupt
:: variable parsing intermittently when this script is launched by Windows Task
:: Scheduler, though not when run interactively -- see git history for details.)
:: The line below is a placeholder, not a real path -- setup_wizard.py rewrites it
:: automatically the first time you set a Python interpreter in Settings and click
:: Save. Don't commit your own real path back here; this file stays tracked in git
:: and a machine-specific path doesn't belong in the public repo.
set CONDA_ENV=C:\Users\YourName\miniconda3\envs\whisper-env
set PATH=%CONDA_ENV%;%CONDA_ENV%\Library\mingw-w64\bin;%CONDA_ENV%\Library\usr\bin;%CONDA_ENV%\Library\bin;%CONDA_ENV%\Scripts;%CONDA_ENV%\bin;%PATH%

cd /d %~dp0

:: logs\ is gitignored (it's per-machine runtime output, not something that belongs in
:: version control), so a fresh clone doesn't have it. Without this, the very first
:: redirect below fails silently -- cmd.exe can't open a log file in a directory that
:: doesn't exist, so the whole line never runs, no notification ever shows, and there's
:: no visible error anywhere. This bit a genuinely fresh install end to end.
if not exist logs mkdir logs

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