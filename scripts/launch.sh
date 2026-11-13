#!/usr/bin/env sh
# VS_ROOT is resolved by the H1 launcher, never from the caller's project.
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' '{"ok":false,"code":"UV_MISSING","message":"Install uv, then retry this launcher."}' >&2
  exit 20
fi
VS_PYTHON="$VS_ROOT/.venv/bin/python"
if [ ! -x "$VS_PYTHON" ]; then
  printf '%s\n' '{"ok":false,"code":"PYTHON_MISSING","message":"Prepare the project environment with uv sync --locked."}' >&2
  exit 22
fi
VS_BOOT_MARKER=$(mktemp "${TMPDIR:-/tmp}/video-story-boot.XXXXXXXX") || exit 22
rm -f "$VS_BOOT_MARKER"
export VS_BOOT_MARKER
"$VS_PYTHON" -I "$VS_ROOT/scripts/bootstrap.py" "$@"
VS_EXIT=$?
if [ ! -f "$VS_BOOT_MARKER" ]; then
  printf '%s\n' '{"ok":false,"code":"PYTHON_MISSING","message":"The project Python could not start its bootstrap."}' >&2
  exit 22
fi
rm -f "$VS_BOOT_MARKER"
exit "$VS_EXIT"
