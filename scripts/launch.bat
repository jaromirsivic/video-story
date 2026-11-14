@echo off
setlocal
rem Called with VS_ROOT already resolved by the H1 launcher.
where uv >nul 2>nul
if errorlevel 1 (
  >&2 echo {"ok":false,"code":"UV_MISSING","message":"Install uv, then retry this launcher."}
  exit /b 20
)
if not exist "%VS_ROOT%\.venv\Scripts\python.exe" (
  >&2 echo {"ok":false,"code":"PYTHON_MISSING","message":"Prepare the project environment with uv sync --locked."}
  exit /b 22
)
set "VS_BOOT_MARKER=%TEMP%\video-story-boot-%RANDOM%-%RANDOM%.tmp"
"%VS_ROOT%\.venv\Scripts\python.exe" -I "%VS_ROOT%\scripts\bootstrap.py" %*
set "VS_EXIT=%ERRORLEVEL%"
if not exist "%VS_BOOT_MARKER%" (
  >&2 echo {"ok":false,"code":"PYTHON_MISSING","message":"The project Python could not start its bootstrap."}
  exit /b 22
)
del /q "%VS_BOOT_MARKER%" >nul 2>nul
exit /b %VS_EXIT%
