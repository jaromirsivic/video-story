"""Real HTTP requests against a local server, with persistent partial files."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from video_story.host.downloads import DownloadError, download_file


@pytest.fixture
def server():
    state = {"payload": b"hello" * 1000, "etag": '"v1"', "requests": [], "mode": "partial"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Range")
            state["requests"].append(header)
            payload = state["payload"]
            start = int(header.split("=")[1].split("-")[0]) if header else 0
            mode = state["mode"]
            if mode == "416" and header:
                self.send_response(416)
                self.end_headers()
                return
            if mode == "ignore" or self.headers.get("If-Range", state["etag"]) != state["etag"]:
                start = 0
            self.send_response(206 if start else 200)
            self.send_header("ETag", state["etag"])
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            self.send_header("Content-Length", str(len(payload) - start))
            self.end_headers()
            data = payload[start:]
            if mode == "slow":
                try:
                    for offset in range(0, len(data), 128):
                        self.wfile.write(data[offset : offset + 128])
                        self.wfile.flush()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            else:
                self.wfile.write(data[: len(data) // 2] if mode == "partial" else data)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    yield state, f"http://127.0.0.1:{http.server_port}/file"
    http.shutdown()
    worker.join()
    http.server_close()


@pytest.mark.parametrize("mode", ["resume", "ignore", "416", "changed"])
def test_resume_or_restart_never_appends_a_full_response(server, tmp_path, mode):
    state, url = server
    dest = tmp_path / "file.bin"
    sha = hashlib.sha256(state["payload"]).hexdigest()
    with pytest.raises(DownloadError):
        download_file(url, dest, attempts=1, expected_sha256=sha)
    assert dest.with_suffix(".bin.part").stat().st_size == 2500
    state["mode"] = mode
    if mode == "changed":
        state["etag"] = '"v2"'
    download_file(url, dest, attempts=1, expected_sha256=sha)
    assert dest.read_bytes() == state["payload"]
    assert state["requests"][1] == "bytes=2500-"
    assert not dest.with_suffix(".bin.part.json").exists()


def test_complete_part_416_needs_checksum(server, tmp_path):
    state, url = server
    state["mode"] = "416"
    dest = tmp_path / "file"
    part = tmp_path / "file.part"
    part.write_bytes(state["payload"])
    sha = hashlib.sha256(state["payload"]).hexdigest()
    part.with_suffix(".part.json").write_text(
        json.dumps(
            {
                "identity": {"url": url, "sha256": sha, "sha1": None, "md5": None},
                "etag": '"v1"',
                "total": len(state["payload"]),
            }
        )
    )
    download_file(url, dest, expected_sha256=sha)
    assert dest.read_bytes() == state["payload"]


def test_resume_in_new_process_after_kill(server, tmp_path):
    state, url = server
    state["mode"] = "slow"
    state["payload"] = b"chunk" * 10000
    sha = hashlib.sha256(state["payload"]).hexdigest()
    dest = tmp_path / "new-process.bin"
    code = (
        "import sys; from video_story.host import downloads; downloads.CHUNK_SIZE=128; "
        "downloads.download_file(sys.argv[1], __import__('pathlib').Path(sys.argv[2]), "
        "expected_sha256=sys.argv[3], attempts=1)"
    )
    # The base interpreter is a real process, not Windows' venv redirector.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    args = [sys._base_executable, "-c", code, url, str(dest), sha]
    process = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    part = dest.with_suffix(".bin.part")
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if part.exists() and part.stat().st_size > 0:
                break
            time.sleep(0.02)
        assert part.exists() and part.stat().st_size > 0
    finally:
        process.terminate()
        process.wait(timeout=10)
    saved = part.stat().st_size
    state["mode"] = "resume"
    subprocess.run(args, env=env, check=True, capture_output=True, timeout=10)
    assert f"bytes={saved}-" in state["requests"]
    assert dest.read_bytes() == state["payload"]
