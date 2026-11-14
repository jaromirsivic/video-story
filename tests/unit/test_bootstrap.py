"""Run the real no-install bootstrap / Windows launcher in isolated environments."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_bootstrap_missing_package_and_python_version(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "bootstrap.py"
    shutil.copyfile(ROOT / "scripts/bootstrap.py", script)
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nrequires-python = ">=3.12,<3.14"\n')
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "version"], capture_output=True, text=True
    )
    assert result.returncode == 23
    assert json.loads(result.stderr)["code"] == "ENVIRONMENT_INVALID"
    project.write_text('[project]\nrequires-python = ">=99.0"\n')
    result = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True)
    assert result.returncode == 23
    assert json.loads(result.stderr)["requirement"] == ">=99.0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .bat")
def test_real_bat_never_runs_uv_and_preserves_arguments(tmp_path):
    root = tmp_path / "space ž"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    launcher = scripts / "launch.bat"
    shutil.copyfile(ROOT / "scripts/launch.bat", launcher)
    uv = shutil.which("uv")
    assert uv
    subprocess.run(
        [uv, "venv", "--no-python-downloads", "--python", sys.executable, str(root / ".venv")],
        check=True,
        capture_output=True,
    )
    (scripts / "bootstrap.py").write_text(
        "import os,sys,json\nfrom pathlib import Path\n"
        'Path(os.environ["VS_BOOT_MARKER"]).touch()\n'
        "print(json.dumps(sys.argv[1:]))\nsys.exit(7)\n",
        encoding="utf-8",
    )
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "uv.cmd").write_text("@echo off\nexit /b 99\n")
    env = dict(
        os.environ,
        VS_ROOT=str(root),
        PATH=str(stub) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32"),
    )
    command = f'"{os.environ["ComSpec"]}" /d /s /c ""{launcher}" version "a b""'
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == ["version", "a b"]
    # No interpreter and no uv have distinct errors, without running another Python.
    (root / ".venv/Scripts/python.exe").rename(root / ".venv/Scripts/python.saved")
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 22 and json.loads(result.stderr)["code"] == "PYTHON_MISSING"
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 20 and json.loads(result.stderr)["code"] == "UV_MISSING"
