@echo off
rem Video Story starter (Windows). Double-click it to open the recording window;
rem "start-video-story.bat edit" opens the Video Story Editor with the stored recordings.
rem The first start prepares FFmpeg and the Whisper model (about 550 MB).
setlocal
title Video Story
cd /d "%~dp0"

set "VS_COMMAND=run"
if /i "%~1"=="edit" set "VS_COMMAND=edit"

where uv >nul 2>nul
if errorlevel 1 goto :no_uv

rem "setup --check" knows where the model and FFmpeg are looked for (the state folder,
rem then this folder, PATH); exit code 0: nothing is missing.
uv run --project . video-story --log-level WARNING setup --check >nul
if not errorlevel 1 goto :start

echo First start: preparing FFmpeg and the Whisper model, about 550 MB. This takes a few minutes.
uv run --project . video-story setup
if errorlevel 1 goto :failed
uv run --project . video-story --log-level WARNING setup --check >nul
if errorlevel 1 goto :failed

:start
echo Starting Video Story...
uv run --project . video-story --log-level WARNING %VS_COMMAND% --detach >nul
if errorlevel 1 goto :failed
exit /b 0

:no_uv
echo uv is not installed. Install it with:  winget install astral-sh.uv
echo or from https://docs.astral.sh/uv/ and start Video Story again.
pause
exit /b 20

:failed
echo.
echo Video Story could not start; the message above says why.
pause
exit /b 1
