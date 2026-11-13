"""``scripts/install-hosts`` links the skills into the folder each host reads.

The script of the current platform (``.ps1`` with junctions on Windows, ``.sh`` with
symbolic links elsewhere) runs against a temporary home directory (``USERPROFILE`` and
``HOME`` of the child process), so the real profile is never touched. The links point at
this checkout's ``skills/``; they are always removed as links, never followed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILLS = ("video-story", "video-story-edit")
WINDOWS = sys.platform == "win32"
CLAUDE = Path(".claude", "skills")
AGENTS = Path(".agents", "skills")
LEGACY_CODEX = Path(".codex", "skills")
CURSOR = Path(".cursor", "skills")
ALL_DIRS = (CLAUDE, AGENTS, LEGACY_CODEX, CURSOR)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


def _remove_link(path: Path) -> None:
    """Remove the link itself: rmdir for a junction, unlink for a symlink. Never rmtree."""
    if WINDOWS and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink()


def _make_link(link: Path, target: Path) -> None:
    """A link like the one an older install or another tool left behind."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        comspec = os.environ.get("ComSpec", "cmd.exe")
        subprocess.run(
            [comspec, "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        os.symlink(target, link, target_is_directory=True)


def _links(home: Path) -> dict[str, Path]:
    """Every link in the four skills folders, as ``"<dir>/<name>" -> resolved target``."""
    found = {}
    for folder in ALL_DIRS:
        if (home / folder).is_dir():
            for entry in (home / folder).iterdir():
                assert _is_link(entry), f"not a link: {entry}"
                found[(folder / entry.name).as_posix()] = entry.resolve()
    return found


def _expected(*folders: Path) -> dict[str, Path]:
    return {
        (folder / skill).as_posix(): (REPO / "skills" / skill).resolve()
        for folder in folders
        for skill in SKILLS
    }


@pytest.fixture
def home(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    try:  # can this machine create the kind of link the script makes?
        _make_link(tmp_path / "probe", REPO / "skills")
        _remove_link(tmp_path / "probe")
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"cannot create a directory link here: {exc}")
    yield home
    for folder in ALL_DIRS:  # leave no link behind for the tmp_path clean-up to follow
        if (home / folder).is_dir():
            for entry in (home / folder).iterdir():
                if _is_link(entry):
                    _remove_link(entry)


@pytest.fixture
def install(home: Path):
    def _install(*switches: str, expect_ok: bool = True) -> str:
        env = dict(os.environ, USERPROFILE=str(home), HOME=str(home))
        if WINDOWS:
            script = REPO / "scripts" / "install-hosts.ps1"
            names = {s: f"-{s.capitalize()}" for s in ("claude", "codex", "cursor", "remove")}
            command = [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]
        else:
            script = REPO / "scripts" / "install-hosts.sh"
            names = {s: f"--{s}" for s in ("claude", "codex", "cursor", "remove")}
            command = ["sh", str(script)]
        proc = subprocess.run(
            command + [names[s] for s in switches],
            env=env,
            cwd=home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert (proc.returncode == 0) == expect_ok, (proc.stdout, proc.stderr)
        return proc.stdout

    return _install


def test_each_host_gets_its_documented_folder(home: Path, install):
    out = install("claude")
    assert _links(home) == _expected(CLAUDE)
    assert out.count("Claude Code: created") == 2
    assert "claude_desktop_config.json" in out and "mcp_servers" not in out

    assert "Claude Code: unchanged" in install("claude")  # idempotent
    assert _links(home) == _expected(CLAUDE)

    install("claude", "remove")
    out = install("codex")
    assert _links(home) == _expected(AGENTS)
    assert "[mcp_servers.video-story]" in out and "tool_timeout_sec" in out

    install("codex", "remove")
    out = install("cursor")
    assert _links(home) == _expected(CURSOR)
    assert '"type"' in out and "mcp.json" in out


def test_codex_moves_a_legacy_link(home: Path, install):
    _make_link(home / LEGACY_CODEX / "video-story", REPO / "skills" / "video-story")
    out = install("codex")
    assert "legacy location" in out
    assert _links(home) == _expected(AGENTS)

    _make_link(home / LEGACY_CODEX / "video-story", REPO / "skills" / "video-story")
    install("codex", "remove")
    assert _links(home) == {}


def test_cursor_does_not_load_the_same_skill_twice(home: Path, install):
    install("claude")
    out = install("cursor")
    assert out.count("no link needed") == 2 and ".claude" in out
    assert _links(home) == _expected(CLAUDE)

    install("claude", "remove")
    install("cursor")
    assert _links(home) == _expected(CURSOR)
    out = install("claude")
    assert out.count("twice") == 2  # the note, while both links exist
    out = install("cursor")
    assert out.count("Cursor: removed") == 2
    assert _links(home) == _expected(CLAUDE)

    install("claude", "codex", "cursor")  # Cursor already reads both of the others
    assert _links(home) == _expected(CLAUDE, AGENTS)


def test_remove_takes_only_links(home: Path, install):
    install("claude", "codex", "cursor")
    real = home / CURSOR / "video-story"
    real.mkdir(parents=True)  # a real folder of the same name is never touched
    (real / "keep.txt").write_text("x", encoding="utf-8")
    install("claude", "codex", "cursor", "remove")
    assert (real / "keep.txt").is_file()
    real_target_still_there = REPO / "skills" / "video-story" / "SKILL.md"
    assert real_target_still_there.is_file()
    (real / "keep.txt").unlink()
    real.rmdir()
    assert _links(home) == {}

    real = home / CLAUDE / "video-story"
    real.mkdir(parents=True)
    install("claude", expect_ok=False)  # refuses to replace a real folder
    real.rmdir()


def test_a_host_switch_is_required(install):
    install(expect_ok=False)
    install("remove", expect_ok=False)
