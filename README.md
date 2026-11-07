# Video Story

Video Story is a plugin and skill for Claude Code, Codex and Claude Desktop (and ChatGPT through a remote MCP server). While you work, you narrate what you are doing or what you want done. Video Story records your microphone, takes periodic screenshots of every connected screen and tracks the cursor. When you stop, the audio is transcribed offline with whisper.cpp and the agent receives a timed transcript together with the frames, so it can connect what you said with what was on screen and where the cursor was.

- Windows and Linux (X11) are supported in this version. macOS support is planned.
- Everything runs locally: no audio or images leave the machine.
- The plugin UI and all messages are in English; the transcript keeps the language you spoke.

See [docs/installation.md](docs/installation.md) for setup and [implementation-plan.md](implementation-plan.md) for the design (in Czech).
