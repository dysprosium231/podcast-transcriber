@echo off
:: 这一行不是项目路径，是conda环境的安装位置，和项目文件夹搬到哪个盘无关。
:: 只有当conda环境本身被重装/搬动时才需要改这里。
set CONDA_ENV=D:\condaenvs\whisper-conda
set PATH=%CONDA_ENV%;%CONDA_ENV%\Library\mingw-w64\bin;%CONDA_ENV%\Library\usr\bin;%CONDA_ENV%\Library\bin;%CONDA_ENV%\Scripts;%CONDA_ENV%\bin;%PATH%

cd /d %~dp0

"%CONDA_ENV%\python.exe" prompt_before_run.py >> logs\prompt_debug.log 2>&1
echo EXITCODE_WAS_%errorlevel% >> logs\prompt_debug.log
if errorlevel 1 goto :skip

echo ABOUT_TO_START_MAIN_SCRIPT >> logs\prompt_debug.log
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set TIMESTAMP=%%T
set LOGFILE=logs\run_%TIMESTAMP%.log
"%CONDA_ENV%\python.exe" daily_podcast.py >> "%LOGFILE%" 2>&1
goto :eof

:skip
echo todayskipped >> logs\skip_log.txt
goto :eof