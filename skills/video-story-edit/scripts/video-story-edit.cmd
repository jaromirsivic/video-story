@echo off
setlocal
rem Video Story Editor launcher (Windows). Runs the plugin through uv from the repository root.
set "VS_ROOT=%~dp0..\..\.."
for %%I in ("%VS_ROOT%") do set "VS_ROOT=%%~fI"

where uv >nul 2>nul
if errorlevel 1 (
  >&2 echo {"ok":false,"code":"UV_MISSING","message":"uv is required. Install it from https://docs.astral.sh/uv/ and run again."}
  exit /b 20
)

uv run --quiet --project "%VS_ROOT%" video-story %*
exit /b %ERRORLEVEL%
