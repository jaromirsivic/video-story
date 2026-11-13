"""Real stdio protocol, distinct from testing any particular agent host."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client


def test_stdio_stays_connected_after_missing_components(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"recording_folder": str(tmp_path / "recordings")}))
    code = tmp_path / "empty-code"
    code.mkdir()
    (code / "pyproject.toml").write_text("[project]\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_story", "--config", str(config), "mcp"],
        env={
            **os.environ,
            "VIDEO_STORY_ROOT": str(code),
            "VIDEO_STORY_HOME": str(tmp_path / "state"),
            "PATH": "",
        },
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    async def check():
        with anyio.fail_after(15):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("video_story_start", {})
                    assert result.structured_content["code"] == "SETUP_REQUIRED"
                    assert result.structured_content["missing"] == ["model", "ffmpeg"]
                    after = await client.list_tools()
                    assert any(t.name == "video_story_edit" for t in after.tools)

    anyio.run(check)
    assert not (tmp_path / "recordings").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="cmd quoting on Windows")
def test_stdio_bat_with_spaces_and_real_bootstrap(tmp_path):
    root = Path(__file__).resolve().parents[2]
    link = tmp_path / "plugin with space"
    subprocess.run(
        [os.environ["ComSpec"], "/d", "/c", "mklink", "/J", str(link), str(root)],
        check=True,
        capture_output=True,
    )
    config = tmp_path / "config with space.json"
    config.write_text(
        json.dumps(
            {
                "recording_folder": str(tmp_path / "recordings"),
                "whisper_model_path": str(tmp_path / "missing.bin"),
            }
        )
    )
    env = dict(os.environ)
    env.pop("VIDEO_STORY_ROOT", None)
    env["VIDEO_STORY_HOME"] = str(tmp_path / "state")
    env["PATH"] = (
        str(Path(shutil.which("uv")).parent)
        + os.pathsep
        + str(Path(os.environ["SystemRoot"]) / "System32")
    )
    params = StdioServerParameters(
        command=os.environ["ComSpec"],
        args=[
            "/d",
            "/c",
            "call",
            str(link / "skills/video-story/scripts/video-story.bat"),
            "--config",
            str(config),
            "mcp",
        ],
        env=env,
        cwd=str(tmp_path),
    )

    async def check():
        with anyio.fail_after(15):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("video_story_start", {})
                    assert result.structured_content["code"] == "SETUP_REQUIRED"

    try:
        anyio.run(check)
    finally:
        link.rmdir()  # remove only the test junction, never its target
