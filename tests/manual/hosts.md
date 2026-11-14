# Host verification: Claude Code, Cursor and Codex

## H5 status, 13 November 2026

The implementation is present. Full cross-host acceptance is **not complete**.
Do not infer a successful agent workflow merely from MCP health/tool discovery.
The artifacts below are local, ignored test output under `tests/results/h5/`.

| Host / environment | Actual observation | Still required |
| --- | --- | --- |
| Codex desktop, this chat | Real CLI start acknowledged the recorder and editor. Process returned the correct recording/crop. Attach, two consecutive 45-second waits and close without Process returned the expected events | Complete narrated Record to Stop to Process flow, MCP tools called by the desktop host, repair driven by a fresh agent session |
| Claude Code 2.1.318 / Opus 5.5 | Real model session passed CLI editor open, two short waits and normal close without Process. Native host MCP edit/wait returned Process with the correct recording and crop. Missing-model CLI returned SETUP_REQUIRED, exit 4 | Narrated recorder flow, agent-driven dependency repair and remaining V1-V12 variants |
| Cursor Agent CLI 2026.11.11-b912975 / Grok 4.7 Medium | Fresh resumed CLI editor open, two short waits and normal Alt+F4 close passed, final event=closed. Native Cursor MCP discovery lists 14 tools. Model-driven MCP edit/Process remains **BLOCKED / NOT VERIFIED**, details below | Native MCP edit/Process after usage capacity returns, recorder and repair flows, Cursor editor Agent UI |
| Codex CLI 0.167.0-alpha.4.1 / GPT-6.1-Sol Medium | Real model session passed CLI editor open/wait/normal close, native MCP edit/wait/Process, and corrected missing-model check with exit 4 | Narrated recorder flow, dependency repair and remaining V1-V12 variants |
| Ubuntu / WSL, Python 3.13.16 | Actual `.sh` start and MCP transport tests: 32 passed, 2 Windows-only tests skipped. Additional startup/setup/lock tests: 16 passed, 1 GUI test deselected. Fast missing-model detection returned exit 4 | Native desktop recording, host matrix on Linux |
| macOS | No macOS runtime available on this machine | `.sh`, GUI and host matrix on macOS |

Evidence: [Windows full suite](../results/h5/unit-final.log),
[Linux smoke](../results/h5/linux-smoke.log),
[Claude connection](../results/h5/claude-mcp-health.log),
[Cursor tools](../results/h5/cursor-mcp-tools.log),
[Codex editor Process](../results/h5/codex-app-editor-process.json),
[Codex editor attach](../results/h5/codex-app-attach.json),
[two completed 45-second waits](../results/h5/codex-app-wait-2.json),
[close without Process](../results/h5/codex-app-editor-closed.json),
[real component installation](../results/h5/real-setup-result.json).

Agent-run evidence: [Claude report](../results/h5/agent-claude/report.json),
[Codex report](../results/h5/agent-codex/report.json),
[Cursor resumed report](../results/h5/agent-cursor-grok/report-resumed.json),
[Cursor coordinator verification](../results/h5/cursor-final-verification.json),
[Cursor source-change check](../results/h5/cursor-resume-source-check.json).

### Cursor continuation: normal close passed, MCP blocked

The interrupted run was not counted as a normal close. A new editor session,
`editor-64fb1d3f61e1.json`, was opened for test recording `20261113-163001`.
The coordinator observed the actual editor and closed it with Alt+F4, without
Process or process termination. Cursor's subsequent CLI wait returned exit 0,
`event=closed` and `timed_out=false`. Both earlier 2-second waits returned
`event=open` and `timed_out=true`.

The resumed model session reported that native H5 MCP tools were not exposed in
its tool set. A second MCP-only continuation used explicit `--approve-mcps`, with
other user servers disabled only in the isolated test environment. That attempt
ended before any model tool invocation with **ActionRequiredError: You've hit your
usage limit**. It did not open an editor through MCP and did not observe Process.
These are separate observations: the quota failure does not explain or resolve
the earlier tool-visibility issue.

After the quota failure, the installed Cursor CLI successfully connected to H5 and
listed all 14 tools with exit 0. This was discovery only, with no model request and
no tool execution. It proves local server/catalog availability, **not** successful
agent-driven MCP use. See [catalog result](../results/h5/cursor-mcp-catalog-after-limit.json)
and [raw quota error](../results/h5/agent-cursor-grok/stderr-mcp-check.log).

Next: after Cursor usage capacity is restored, configure H5 before the model session
starts, verify the native tool set, then run `video_story_edit` and
`video_story_wait_editor` through Cursor until a real GUI Process returns the
selected recording, crop and instructions. The successful CLI close need not be repeated.

The original Composer 2.5 attempt was rejected by this account's Free-plan access.
The user explicitly authorized Grok 4.7 instead. No subscription was purchased.
Claude authentication was refreshed and its installed CLI updated from 2.1.268 to
2.1.318 because Opus 5.5 required a newer client. Cursor automatically updated its
CLI from 2026.06.26 to 2026.11.11 during preparation.

The agents changed no application source, skills, existing tests or documentation.
The coordinator updated this verification record afterwards. Temporary project MCP
configuration was removed and the test processes exited. The Codex report retains
an initial invalid test-config attempt which accidentally opened a recorder, then
the corrected negative check. That aborted attempt is not counted as a successful
missing-component test.

Permission scope: these were explicitly authorized local tests. Cursor used
`--trust --force --sandbox disabled`, Claude used per-run permission bypass, and
Codex CLI used `danger-full-access` with approval policy `never`. The MCP-only Cursor
retry additionally used `--approve-mcps` with only H5 enabled in the isolated test
configuration. These results do not validate every host's default sandbox or its
approval prompts; those H4/V11 combinations remain separate checks.

The final full Windows run passed 954 tests. Later targeted
checks include a real transfer interrupted by killing its process, then resumed in
a new process. Normal-start benchmarks (instrumented real Qt windows, not total
agent latency): recorder median 416 to 409 ms, editor 824 to 842 ms over ten repeated
starts. These measurements do not prove a speedup or replace host testing.

Use the scenarios and acceptance matrix in the
[H5 implementation plan](../../implementation-plan-h5-startup-and-wait.md#5-matice-claude-cursor-a-codex).
Record a failed/unavailable scenario as such. The user explicitly authorized these
test-only agent sessions, direct CLI execution and an unlimited common deadline.
They were not authorized to implement or edit application code.

## Remaining manual scenarios

For each supported host variant, run V1-V12 from the H5 plan, both CLI and MCP,
for the recorder and editor. Use isolated recordings, state and dependency fixtures.
Never remove the user's model or libraries to test the missing-dependency branch.
Capture exact commands, tool results, host limits, version and permission mode.
Verify that a long tool call returning a handle is followed on that same handle.
Only Process authorizes consuming a recording, closing must return without action.

The detailed host steps below remain useful for links/plugin setup and GUI survival.
Their expected wait is now 45 seconds, with a host budget of at least 60 seconds.

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
| C3 | Type `Start Video Story` | The agent runs `...\skills\video-story\scripts\video-story.bat run --detach` (O2). Codex asks to run it outside the sandbox, or the agent asks you first (O3): approve |
| C4 | Look at the screen | The Video Story window appears and is still open after the agent has shown the result with `session_id` (O4) |
| C5 | Watch the agent | It calls `wait --session <id> --timeout 45` and calls it again after `timed_out` (O6) |
| C6 | Record, say the task, Stop, Video Editor (default), wait for the green arrow to reach the end, Process | The control window closes, the editor opens, Process closes it |
| C7 | Watch the agent | `wait` returns `processed`; it runs `timeline --session <id>`, reads `timeline_file` (O8) and creates `hello.txt` |
| C8 | Ask: "Run the Video Story `sessions` command and tell me the last line of its output" | Note whether the output reached the agent whole or cut, and the marker the host put in (O7) |
| C9 | Say `Edit Video Story`, then close the editor without Process | `edit --detach` opens the editor (same O3/O4 observations); the agent reports `closed` and does nothing |
| C10 | `& .\scripts\install-hosts.ps1 -Codex -Remove` | Prints `removed` for both links |

Plugin variant (after C10): clone the repository fresh, for example to
`C:\vs-plugin`; `codex plugin marketplace add C:\vs-plugin`; in `codex` open `/plugins`,
tab `video-story-local`, install `video-story`, start a new session in the test folder.
Repeat C2-C7 (O1, O10). After a reported environment error, the agent prepares the environment inside
`~/.codex/plugins/cache/video-story-local/video-story/local/`: note whether Codex asked for
network or outside-sandbox permission and how long it took. Afterwards uninstall it in
`/plugins` and run `codex plugin marketplace remove video-story-local`.

MCP variant (required in addition to the CLI path): add the `[mcp_servers.video-story]` block from
`docs/installation.md` to `~/.codex/config.toml`, restart `codex`, `/mcp` lists
`video-story`; say `Start Video Story`. Note whether the agent used `video_story_start`,
whether the window stayed open and whether `video_story_wait` calls ended early (O11).
Remove the block afterwards.

## Cursor (Agent chat in the editor; then `cursor-agent`)

| # | Do | Must happen |
| --- | --- | --- |
| U1 | With no Video Story link in `.claude\skills`, `.agents\skills` or `.codex\skills`: `& .\scripts\install-hosts.ps1 -Cursor` | Prints `created ...\.cursor\skills\video-story` and `...-edit` |
| U2 | Restart Cursor, open the test folder, **Customize > Skills** | Both skills are listed, each once (O1) |
| U3 | New Agent chat: `Start Video Story` | The agent runs `video-story.bat run --detach` (O2); note any approval card or "outside the sandbox" notice (O3) |
| U4 | Look at the screen | The window appears and stays open after the command has returned (O4) |
| U5 | Watch the agent | `wait --timeout 45` runs to its end or to the event, and is called again after `timed_out` (O6) |
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

MCP variant (required in addition to the CLI path): add the block from `docs/installation.md` to
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
| O6 | `wait --timeout 45`: returned by itself / cut by the host after N s (host's message) | | | | |
| O7 | Long output (`sessions`): arrived whole / cut at about N characters, marker text | | | | |
| O8 | Reading `timeline_file` outside the workspace: allowed / asked first / refused | | | | |
| O9 | Acted after Process (hello.txt exists); did nothing after closing the editor | | | | |
| O10 | Plugin install: worked / exact error; first run: network or sandbox prompt, duration | | | | |
| O11 | MCP (required): window stayed open; `video_story_wait` ended early after N s | | | | |
