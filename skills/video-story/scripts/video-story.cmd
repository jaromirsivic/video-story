@echo off
setlocal
rem Video Story launcher (Windows). Runs the plugin through uv from the repository root.
rem The root is VIDEO_STORY_ROOT, else three levels above this script, else the same three
rem levels behind the junction or directory symlink the skill is registered with (the host
rem hands the agent the link path). "dir /AL" prints the link target; PowerShell is the fallback.
set "VS_ROOT="
set "DIRCMD="
if defined VIDEO_STORY_ROOT if exist "%VIDEO_STORY_ROOT%\pyproject.toml" for %%I in ("%VIDEO_STORY_ROOT%\.") do set "VS_ROOT=%%~fI"
if not defined VS_ROOT if exist "%~dp0..\..\..\pyproject.toml" for %%I in ("%~dp0..\..\..") do set "VS_ROOT=%%~fI"
if not defined VS_ROOT for %%P in (".|..\..\.." "..|..\.." "..\..|..") do for /f "tokens=1,2 delims=|" %%A in (%%P) do for %%D in ("%~dp0%%A") do for /f "delims=-abcdefghijkmnopqrstuvwxyz" %%L in ("%%~aD") do if not defined VS_ROOT (
  for /f "tokens=1* delims=>" %%G in ('dir /AL "%%~fD*" 2^>nul') do for /f "tokens=*" %%Z in ("%%H") do for /f "tokens=1,2 delims=[]" %%S in ("%%Z") do if /i "%%S"=="%%~nxD " if /i "%%~fT"=="%%T" if exist "%%T\%%B\pyproject.toml" for %%R in ("%%T\%%B") do set "VS_ROOT=%%~fR"
  if not defined VS_ROOT (
    set "VS_LINK=%%~fD"
    for /f "delims=" %%T in ('powershell -NoProfile -NonInteractive -Command "$i = Get-Item -LiteralPath $env:VS_LINK -Force; [IO.Path]::GetFullPath([IO.Path]::Combine($i.Parent.FullName, @($i.Target)[0]))" 2^>nul') do if exist "%%T\%%B\pyproject.toml" for %%R in ("%%T\%%B") do set "VS_ROOT=%%~fR"
  )
)
if not defined VS_ROOT (
  >&2 echo {"ok":false,"code":"ROOT_NOT_FOUND","message":"Video Story root not found: no pyproject.toml three levels above this launcher, also not behind its junction or symlink. Register the skill again with scripts/install-hosts or set VIDEO_STORY_ROOT to the Video Story directory."}
  exit /b 21
)

where uv >nul 2>nul
if errorlevel 1 (
  >&2 echo {"ok":false,"code":"UV_MISSING","message":"uv is required. Install it from https://docs.astral.sh/uv/ and run again."}
  exit /b 20
)

uv run --quiet --project "%VS_ROOT%" video-story %*
exit /b %ERRORLEVEL%
