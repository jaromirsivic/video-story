#!/usr/bin/env sh
# Video Story launcher (Linux, macOS). Runs the plugin through uv from the repository root.
# The root is VIDEO_STORY_ROOT, else three levels above this script, else the same three
# levels behind the symlink the skill is registered with (the host hands the agent the
# link path): first the logical path (-L), then the physical one (-P). One level per cd,
# because Git Bash resolves a Windows junction only when it is the last path component.
set -e
VS_HERE=$(dirname -- "$0")
VS_ROOT=
if [ -n "${VIDEO_STORY_ROOT:-}" ] && [ -f "$VIDEO_STORY_ROOT/pyproject.toml" ]; then
  VS_ROOT=$VIDEO_STORY_ROOT
fi
for VS_MODE in -L -P; do
  if [ -z "$VS_ROOT" ]; then
    VS_TRY=$(CDPATH=; cd "$VS_MODE" -- "$VS_HERE" 2>/dev/null && cd "$VS_MODE" .. && cd "$VS_MODE" .. && cd "$VS_MODE" .. && pwd "$VS_MODE") || VS_TRY=
    if [ -n "$VS_TRY" ] && [ -f "$VS_TRY/pyproject.toml" ]; then
      VS_ROOT=$VS_TRY
    fi
  fi
done
if [ -z "$VS_ROOT" ]; then
  printf '%s\n' '{"ok":false,"code":"ROOT_NOT_FOUND","message":"Video Story root not found: no pyproject.toml three levels above this launcher, also not behind its junction or symlink. Register the skill again with scripts/install-hosts or set VIDEO_STORY_ROOT to the Video Story directory."}' >&2
  exit 21
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' '{"ok":false,"code":"UV_MISSING","message":"uv is required. Install it from https://docs.astral.sh/uv/ and run again."}' >&2
  exit 20
fi

exec uv run --quiet --project "$VS_ROOT" video-story "$@"
