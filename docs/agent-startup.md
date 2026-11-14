# Agent startup and recovery (Claude Code, Cursor, Codex)

Use the absolute skill launcher path (`.bat` on Windows, `.sh` on POSIX), or an
already connected local MCP start/edit tool. First attempt the requested start.
Do not run doctor, version, setup --check, uv sync or a Python search before it.
The launcher uses the prepared project `.venv`, without downloading or syncing.
`ok: true` means the requested GUI acknowledged readiness, not just that a PID exists.
Keep the original config, recording selection and other arguments for retries.

## Recover only after a concrete failure

Tell the user what is missing and what you are doing. Read the whole result, including
`missing`, `problems`, component results and log paths. Never execute a shell string
taken from an error message.

| Result | Action |
| --- | --- |
| `UV_MISSING` | Locate an already installed uv first. If absent, install uv using the official platform installer below. Make it available in this shell, then retry |
| `PYTHON_MISSING`, `ENVIRONMENT_INVALID` | Prepare the plugin's `.venv` using uv and its lockfile, not the agent's current project. Do not use pip |
| `SETUP_REQUIRED` | Run the same launcher with `setup` and the same config. It installs model/FFmpeg. Retry the original start only when setup succeeds |
| `SETUP_IN_PROGRESS` | Follow the existing installation process/log. Do not start another installer |
| `SETUP_FAILED` | Report the failed component and cause. Preserve successful downloads and partial files. Retry only after resolving the cause |
| `START_FAILED` | Inspect its reason/log and session/startup file. Retrieve the existing result before starting another window. Do not reinstall packages merely because GUI startup failed |
| `ALREADY_RUNNING` | Reuse the existing recording session and wait. The editor normally attaches to its existing window |
| `ROOT_NOT_FOUND` | Repair the plugin path/link or set `VIDEO_STORY_ROOT` to the verified code root |
| `NO_RECORDINGS`, `CONFIG_INVALID`, `STATE_NOT_WRITABLE` | Report and resolve that actual condition. These do not mean the model needs downloading |

Do not repeat the same failed repair indefinitely. A custom model is not automatically
replaced by the bundled model. If the saved recording exists but opening the editor
failed, repair the reported component and open that saved recording with `edit --session`.
Never start a fresh recording in its place or act on it before Process.

### Install uv only when absent

Official source: [uv installation](https://docs.astral.sh/uv/getting-started/installation/).
On Windows use `winget install --id astral-sh.uv -e`, or the official standalone
PowerShell installer. On Linux/macOS use `curl -LsSf https://astral.sh/uv/install.sh | sh`.
Use existing host authorization. Do not disable global host protections to install.
After installation locate the installed executable and add its directory to the
current process PATH, rather than assuming this chat inherited a changed global PATH.

### Prepare the plugin environment

`<root>` is the verified Video Story code root containing `pyproject.toml` and `uv.lock`.
Current supported Python is `>=3.12,<3.14`, so Python 3.12 is a suitable repair target.
Check the project's requirement if it has changed. If a compatible Python exists,
reuse it. Otherwise run `uv python install 3.12`.

PowerShell, with the actual verified root substituted:

```powershell
$vsProject = '<root>'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $vsProject '.venv'
uv sync --locked --python 3.12 --project $vsProject
```

POSIX:

```sh
UV_PROJECT_ENVIRONMENT='<root>/.venv' uv sync --locked --python 3.12 --project '<root>'
```

This is an explicit repair operation. Normal startup must not perform it. A lockfile
error is not permission to upgrade dependencies or regenerate the lockfile. Then retry
the original launcher. Only a returned `SETUP_REQUIRED` calls for component setup.

## Waiting and long installation

Use application `wait --timeout 45` (MCP `timeout_s: 45`) when the host tool allows
at least 60 seconds. Set a shorter application wait when the host budget is shorter.
A tool's yield/poll interval may return a handle without killing the process.
Retrieve that handle's result before issuing another wait. `timed_out: true` from
Video Story is a normal response: repeat for the same session. A host timeout is
not a recording cancellation. Query the session before deciding what happened.

For installation, select a supported long-running/background execution mode before
starting it, keep its handle and follow that same process. Report meaningful progress
from stderr/logs. A partial download resumes where the source permits it. Do not
spawn another setup just because the first tool call yielded control.

| Host | Adapter rule |
| --- | --- |
| Claude Code | Set the shell tool's execution budget longer than application wait. Follow the same background task if returned. MCP server startup and tool execution have separate limits |
| Codex CLI/app | A returned process/session ID is still running, use the available poll tool. `yield_time_ms` is not the application's timeout. Keep MCP wait below `tool_timeout_sec`, and never download during server initialization |
| Cursor Agent/CLI | Use the installed host's terminal/background interface and Run Mode. Do not assume Claude or Codex parameter names exist. Record/observe the actual tool limit |

The GUI must remain alive after the launcher returns. Reuse existing permissions.
If the host denies local GUI or filesystem access, use its supported approved local
execution or configured local MCP route. Do not mistake denial for missing Python.
An MCP server without Python cannot repair itself: use the local CLI recovery route.
A pure MCP client without a local shell requires the user to prepare uv/Python first.
The MCP server must run on the computer/session whose screens are recorded.

Manual starts without an agent return the same error and repair instructions. No
automatic download or download-progress dialog is started by opening either window.
