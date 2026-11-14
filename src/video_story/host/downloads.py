"""Streaming downloads with checksum verification (standard library only).

Used by ``video-story setup`` / ``doctor --fix`` to fetch FFmpeg builds and the
whisper model. Nothing in this module runs during a recording.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_story import __version__
from video_story.core.fileio import atomic_write_text

log = logging.getLogger(__name__)

USER_AGENT = f"video-story/{__version__} (+https://github.com/video-story)"
DEFAULT_TIMEOUT = 60.0
DEFAULT_ATTEMPTS = 3
CHUNK_SIZE = 1 << 20

ProgressCallback = Callable[[int, int | None], None]


class DownloadError(RuntimeError):
    """A download failed after all retries."""


class ChecksumMismatch(DownloadError):
    """The downloaded file does not match the published checksum."""


@dataclass(frozen=True)
class DownloadResult:
    url: str
    path: Path
    size: int
    sha256: str
    sha1: str
    md5: str
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "sha1": self.sha1,
            "md5": self.md5,
            "attempts": self.attempts,
        }


def _request(url: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    return urllib.request.Request(url, headers=merged)


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    """Hex digest of a file computed in chunks (large files are fine)."""
    digest = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digests(path: Path) -> dict[str, str]:
    hashes = {name: hashlib.new(name) for name in ("sha256", "sha1", "md5")}
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            for digest in hashes.values():
                digest.update(chunk)
    return {name: digest.hexdigest() for name, digest in hashes.items()}


def _check(expected: str | None, actual: str, algorithm: str, url: str) -> None:
    if expected is None:
        return
    expected = expected.strip().lower()
    if expected.startswith(f"{algorithm}:"):
        expected = expected.split(":", 1)[1]
    if expected != actual.lower():
        raise ChecksumMismatch(f"{algorithm} mismatch for {url}: expected {expected}, got {actual}")


def fetch_text(
    url: str, *, timeout: float = DEFAULT_TIMEOUT, headers: dict[str, str] | None = None
) -> str:
    """Download a small text resource (checksum files, JSON APIs)."""
    try:
        with urllib.request.urlopen(_request(url, headers), timeout=timeout) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"cannot fetch {url}: {exc}") from exc


def fetch_json(
    url: str, *, timeout: float = DEFAULT_TIMEOUT, headers: dict[str, str] | None = None
) -> Any:
    merged = {"Accept": "application/vnd.github+json"}
    if headers:
        merged.update(headers)
    text = fetch_text(url, timeout=timeout, headers=merged)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"invalid JSON from {url}: {exc}") from exc


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)
    return isinstance(exc, urllib.error.URLError | TimeoutError | ConnectionError | OSError)


def download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    expected_md5: str | None = None,
    expected_sha1: str | None = None,
    progress: ProgressCallback | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> DownloadResult:
    """Stream ``url`` to ``dest`` and verify the given checksums.

    The file is written to ``dest.part`` and renamed only after the checksums match,
    so an interrupted download never leaves a half written ``dest`` behind.
    ``progress(downloaded_bytes, total_bytes_or_None)`` is called roughly once per
    megabyte. Transient network errors are retried with a short back-off; a checksum
    mismatch is not retried.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    metadata = part.with_suffix(part.suffix + ".json")
    identity = {"url": url, "sha256": expected_sha256, "sha1": expected_sha1, "md5": expected_md5}
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            size, digests = _stream_to_file(url, part, progress, timeout, headers, identity)
        except Exception as exc:
            last_error = exc
            if attempt < attempts and _retryable(exc):
                delay = min(30.0, 2.0 ** (attempt - 1))
                log.warning("Download of %s failed (%s); retrying in %.0fs", url, exc, delay)
                time.sleep(delay)
                continue
            # Keep the partial file and its source identity for a later process.
            raise DownloadError(f"cannot download {url}: {exc}") from exc
        try:
            _check(expected_sha256, digests["sha256"], "sha256", url)
            _check(expected_sha1, digests["sha1"], "sha1", url)
            _check(expected_md5, digests["md5"], "md5", url)
        except ChecksumMismatch:
            part.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            raise
        os.replace(part, dest)
        metadata.unlink(missing_ok=True)
        log.info("Downloaded %s (%d bytes) to %s", url, size, dest)
        return DownloadResult(
            url=url,
            path=dest,
            size=size,
            sha256=digests["sha256"],
            sha1=digests["sha1"],
            md5=digests["md5"],
            attempts=attempt,
        )
    raise DownloadError(f"cannot download {url}: {last_error}")  # pragma: no cover


def _stream_to_file(
    url: str,
    part: Path,
    progress: ProgressCallback | None,
    timeout: float,
    headers: dict[str, str] | None,
    identity: dict[str, Any],
    fresh: bool = False,
) -> tuple[int, dict[str, str]]:
    metadata = part.with_suffix(part.suffix + ".json")
    try:
        previous = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    etag = previous.get("etag")
    strong_etag = etag if etag and not etag.startswith("W/") else None
    verified_source = strong_etag or any(identity.get(k) for k in ("sha256", "sha1", "md5"))
    offset = (
        part.stat().st_size
        if not fresh and part.is_file() and previous.get("identity") == identity and verified_source
        else 0
    )
    request_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    if offset:
        request_headers["Range"] = f"bytes={offset}-"
        if strong_etag:
            request_headers["If-Range"] = strong_etag
    try:
        response = urllib.request.urlopen(_request(url, request_headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            # A complete partial is accepted only with an expected checksum and length.
            if previous.get("total") == offset and any(
                identity.get(k) for k in ("sha256", "sha1", "md5")
            ):
                return offset, _file_digests(part)
            return _stream_to_file(url, part, progress, timeout, headers, identity, fresh=True)
        raise
    with response:
        status = getattr(response, "status", 200)
        if status == 206:
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", "")
            )
            if (
                not match
                or int(match[1]) != offset
                or int(match[2]) < offset
                or (
                    offset
                    and strong_etag
                    and response.headers.get("ETag", strong_etag) != strong_etag
                )
            ):
                if fresh:
                    raise DownloadError("invalid Content-Range on a fresh download")
                return _stream_to_file(url, part, progress, timeout, headers, identity, fresh=True)
            total = int(match[3]) if match[3] != "*" else None
        else:
            offset = 0  # Range ignored / representation changed: replace, never append.
            total = None
        length = response.headers.get("Content-Length")
        if total is None and length and length.isdigit():
            total = offset + int(length)
        downloaded = offset
        with part.open("ab" if offset else "wb") as out:
            atomic_write_text(
                metadata,
                json.dumps(
                    {
                        "identity": identity,
                        "etag": response.headers.get("ETag"),
                        "total": total,
                    }
                ),
            )
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)
    if total is not None and downloaded != total:
        raise DownloadError(f"incomplete download of {url}: {downloaded} of {total} bytes")
    return downloaded, _file_digests(part)


__all__ = [
    "CHUNK_SIZE",
    "USER_AGENT",
    "ChecksumMismatch",
    "DownloadError",
    "DownloadResult",
    "ProgressCallback",
    "download_file",
    "fetch_json",
    "fetch_text",
    "file_digest",
]
