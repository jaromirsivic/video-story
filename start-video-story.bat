@echo off
setlocal
if "%~1"=="" (
  call "%~dp0skills\video-story\scripts\video-story.bat" run --detach
) else (
  call "%~dp0skills\video-story\scripts\video-story.bat" %* --detach
)
exit /b %ERRORLEVEL%
