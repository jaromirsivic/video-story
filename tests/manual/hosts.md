# Host checklist: Codex and Cursor on Windows

Claude Code on Windows is verified end to end. Codex and Cursor are set up from their
documentation only (`docs/installation.md`, section 3). This checklist is the part no
agent can do: one person, the real hosts, about 20 minutes per host. Its outcome decides
the follow-up work (detaching the window from the host's job or sandbox, the `wait`
timeout, the MCP route), so write down the observations of the table at the end exactly,
also when a step works.

## Before you start

- In the repository: `uv sync`, then `uv run video-story setup --check` must exit 0
  (model and FFmpeg present), so the skill-link runs do not depend on the network.
- Note the versions: `codex --version`; Cursor **Help > About**; `cursor-agent --version`.
- No Video Story window is open (`uv run video-story sessions` shows no running one).
- Make an empty test folder, for example `C:\vs-host-test`, and start every host session
  there. That is the agent's workspace; the recording folder and `~/.video-story` lie
  outside it on purpose.
- Keep the host's default permission settings (Codex: the default permissions mode;
  Cursor: the Run Mode you normally use - write down which).
- Say the same thing in every recording, a task whose result is easy to check: "Create a
  file hello.txt in the workspace with the text: hello from video story."

## Codex (CLI in PowerShell; the ChatGPT desktop app the same way if you use it)

| # | Do | Must happen |
| --- | --- | --- |
| C1 | `& .\scripts\install-hosts.ps1 -Codex` | Prints `created ...\.agents\skills\video-story` and `...-edit` (and `removed` for a link left in `.codex\skills`) |
| C2 | `codex` in the test folder, then `/skills` | `video-story` and `video-story-edit` are listed, each once (O1) |
| C3 | Type `Start Video Story` | The agent runs `...\skills\video-story\scripts\video-story.cmd run --detach` (O2). Codex asks to run it outside the sandbox, or the agent asks you first (O3): approve |
| C4 | Look at the screen | The Video Story window appears and is still open after the agent has shown the result with `session_id` (O4) |
| C5 | Watch the agent | It calls `wait --session <id> --timeout 240` and calls it again after `timed_out` (O6) |
| C6 | Record, say the task, Stop, Video Editor (default), wait for the green arrow to reach the end, Process | The control window closes, the editor opens, Process closes it |
| C7 | Watch the agent | `wait` returns `processed`; it runs `timeline --session <id>`, reads `timeline_file` (O8) and creates `hello.txt` |
| C8 | Ask: "Run the Video Story `sessions` command and tell me the last line of its output" | Note whether the output reached the agent whole or cut, and the marker the host put in (O7) |
| C9 | Say `Edit Video Story`, then close the editor without Process | `edit --detach` opens the editor (same O3/O4 observations); the agent reports `closed` and does nothing |
| C10 | `& .\scripts\install-hosts.ps1 -Codex -Remove` | Prints `removed` for both links |

Plugin variant (after C10): clone the repository fresh, for example to
`C:\vs-plugin`; `codex plugin marketplace add C:\vs-plugin`; in `codex` open `/plugins`,
tab `video-story-local`, install `video-story`, start a new session in the test folder.
Repeat C2-C7 (O1, O10). The first run creates the environment inside
`~/.codex/plugins/cache/video-story-local/video-story/local/`: note whether Codex asked for
network or outside-sandbox permission and how long it took. Afterwards uninstall it in
`/plugins` and run `codex plugin marketplace remove video-story-local`.

MCP variant (only when C4 failed): add the `[mcp_servers.video-story]` block from
`docs/installation.md` to `~/.codex/config.toml`, restart `codex`, `/mcp` lists
`video-story`; say `Start Video Story`. Note whether the agent used `video_story_start`,
whether the window stayed open and whether `video_story_wait` calls ended early (O11).
Remove the block afterwards.

## Cursor (Agent chat in the editor; then `cursor-agent`)

| # | Do | Must happen |
| --- | --- | --- |
| U1 | With no Video Story link in `.claude\skills`, `.agents\skills` or `.codex\skills`: `& .\scripts\install-hosts.ps1 -Cursor` | Prints `created ...\.cursor\skills\video-story` and `...-edit` |
| U2 | Restart Cursor, open the test folder, **Customize > Skills** | Both skills are listed, each once (O1) |
| U3 | New Agent chat: `Start Video Story` | The agent runs `video-story.cmd run --detach` (O2); note any approval card or "outside the sandbox" notice (O3) |
| U4 | Look at the screen | The window appears and stays open after the command has returned (O4) |
| U5 | Watch the agent | `wait --timeout 240` runs to its end or to the event, and is called again after `timed_out` (O6) |
| U6 | Record, say the task, Stop, Video Editor, Process | As in C6 |
| U7 | Watch the agent | It reads `timeline_file` (O8) and creates `hello.txt` |
| U8 | The `sessions` question of C8 | O7 |
| U9 | `& .\scripts\install-hosts.ps1 -Claude`, restart Cursor, **Customize > Skills** | The script printed a note that Cursor now finds the skills twice; note how Cursor lists them (O1). Then `-Cursor` again: it removes its own link and says Cursor loads the skills from `.claude\skills` |
| U10 | `cursor-agent` in the test folder: `Start Video Story`, record a few seconds, Stop, Video Editor, Process | Same observations as U3-U7, in the second column for `cursor-agent` |
| U11 | `& .\scripts\install-hosts.ps1 -Cursor -Remove` (and `-Claude` again if Claude Code needs it) | Prints what was removed and where Cursor still loads the skills from |

Plugin variant (with no Video Story link in any skills folder of U1, so nothing is
listed twice): `git clone <repository> $env:USERPROFILE\.cursor\plugins\local\video-story`,
restart Cursor or run **Developer: Reload Window**; **Customize** shows the plugin with
both skills; repeat U3-U7 (O10). Then `cursor-agent --plugin-dir C:\vs-plugin` with a
second clone and the U10 check. Delete the clone afterwards.

MCP variant (only when U4 failed): add the block from `docs/installation.md` to
`~/.cursor/mcp.json`, enable the server in **Customize**, `Start Video Story`; O11.
Remove it afterwards.

## Observations to write down

Write one value per cell; "n/a" when the step was not reached. For anything that failed,
copy the exact text (the agent's tool output or the host's message), not a summary.

| | Observation | Codex CLI | Codex app | Cursor editor | cursor-agent |
| --- | --- | --- | --- | --- | --- |
| O1 | Skills listed: once / twice / missing (how a duplicate looks) | | | | |
| O2 | The exact command line the agent ran for `run --detach` | | | | |
| O3 | Did the host ask to run it outside the sandbox (prompt text), did the agent ask, or did it run without asking | | | | |
| O4 | Window appeared? Still open 60 s after the command returned? If it closed: after how many seconds, and was that when the command ended | | | | |
| O5 | Exact error text of any failing step (JSON `code` and `message`, or the host's message) | | | | |
| O6 | `wait --timeout 240`: returned by itself / cut by the host after N s (host's message) | | | | |
| O7 | Long output (`sessions`): arrived whole / cut at about N characters, marker text | | | | |
| O8 | Reading `timeline_file` outside the workspace: allowed / asked first / refused | | | | |
| O9 | Acted after Process (hello.txt exists); did nothing after closing the editor | | | | |
| O10 | Plugin install: worked / exact error; first run: network or sandbox prompt, duration | | | | |
| O11 | MCP (only if needed): window stayed open; `video_story_wait` ended early after N s | | | | |
