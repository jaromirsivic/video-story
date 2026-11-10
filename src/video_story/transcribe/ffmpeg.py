"""FFmpeg discovery, optional download of a static build and audio conversion.

Lookup order: ``config.ffmpeg_path`` -> ``ffmpeg`` on ``PATH`` -> the bundled copy in
``<plugin root>/bin/ffmpeg/<os>-<arch>/``. Downloads happen only from
``video-story setup`` / ``doctor --fix``, never during a recording.
"""

from __future__ import annotations

import json
import logging
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from video_story.core.config import Config, plugin_root
from video_story.host.downloads import (
    DownloadError,
    ProgressCallback,
    download_file,
    fetch_json,
    fetch_text,
)

log = logging.getLogger(__name__)

BTBN_RELEASE_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest"
JVS_BASE_URL = "https://johnvansickle.com/ffmpeg/releases/"
SOURCE_FILE_NAME = "SOURCE.json"
EMPTY_WAV_MAX_BYTES = 78  # header only: ffmpeg wrote no samples

ArchiveKind = Literal["zip", "tar"]

# BtbN asset "target" names and archive kinds per (os, arch).
_BTBN_TARGETS: dict[tuple[str, str], tuple[str, ArchiveKind]] = {
    ("windows", "x86_64"): ("win64", "zip"),
    ("windows", "arm64"): ("winarm64", "zip"),
    ("linux", "x86_64"): ("linux64", "tar"),
    ("linux", "arm64"): ("linuxarm64", "tar"),
}
# John Van Sickle static builds (Linux only), with a published .md5 next to each archive.
_JVS_ARCHIVES: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "ffmpeg-release-amd64-static.tar.xz",
    ("linux", "arm64"): "ffmpeg-release-arm64-static.tar.xz",
}


class FfmpegNotFound(RuntimeError):
    """No usable ffmpeg executable was found (and none was downloaded)."""


class FfmpegError(RuntimeError):
    """ffmpeg ran but failed or produced no audio."""


@dataclass(frozen=True)
class FfmpegSource:
    """A downloadable FFmpeg archive and the checksum its publisher advertises."""

    name: str
    url: str
    kind: ArchiveKind
    sha256: str | None = None
    md5: str | None = None
    size: int | None = None


# Platform ---------------------------------------------------------------------


def platform_tag(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    """``(os, arch)`` with os in {windows, linux, macos} and arch x86_64/arm64."""
    system = system or sys.platform
    if system.startswith("win"):
        os_name = "windows"
    elif system.startswith("linux"):
        os_name = "linux"
    elif system == "darwin":
        os_name = "macos"
    else:
        os_name = system
    raw = (machine or _platform.machine() or "").lower()
    if raw in ("amd64", "x86_64", "x64", "em64t"):
        arch = "x86_64"
    elif raw in ("arm64", "aarch64", "arm64e"):
        arch = "arm64"
    else:
        arch = raw or "unknown"
    return os_name, arch


def executable_name(base: str, os_name: str | None = None) -> str:
    os_name = os_name or platform_tag()[0]
    return f"{base}.exe" if os_name == "windows" else base


def bundled_ffmpeg_dir(root: Path | None = None) -> Path:
    os_name, arch = platform_tag()
    return (root or plugin_root()) / "bin" / "ffmpeg" / f"{os_name}-{arch}"


def bundled_ffmpeg_path(root: Path | None = None) -> Path:
    return bundled_ffmpeg_dir(root) / executable_name("ffmpeg")


# Discovery --------------------------------------------------------------------


def find_ffmpeg(config: Config) -> Path | None:
    """Configured path, then ``PATH``, then the bundled copy; ``None`` when absent."""
    if config.ffmpeg_path is not None:
        candidate = Path(config.ffmpeg_path).expanduser()
        if candidate.is_dir():
            candidate = candidate / executable_name("ffmpeg")
        if candidate.is_file():
            return candidate
        log.warning("Configured ffmpeg_path %s does not exist; searching PATH", candidate)
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    bundled = bundled_ffmpeg_path()
    if bundled.is_file():
        return bundled
    return None


def ensure_ffmpeg(
    config: Config, download: bool = False, progress: ProgressCallback | None = None
) -> Path:
    """Return a usable ffmpeg path, downloading a static build when allowed."""
    found = find_ffmpeg(config)
    if found is not None:
        return found
    if download:
        return download_ffmpeg(bundled_ffmpeg_dir(), progress=progress)
    raise FfmpegNotFound(
        "ffmpeg was not found (config ffmpeg_path, PATH or the bundled copy). "
        "Run `video-story setup` to download it, or install ffmpeg and set ffmpeg_path."
    )


def ffmpeg_version(path: Path | str) -> str | None:
    """Version string from ``ffmpeg -version`` (for example ``7.1``), or ``None``."""
    try:
        proc = subprocess.run(
            [str(path), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or "").splitlines()[:1]
    if not first:
        return None
    match = re.match(r"ffmpeg version (\S+)", first[0])
    return match.group(1) if match else first[0].strip()


# Conversion -------------------------------------------------------------------


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def convert_to_16k_mono(ffmpeg: Path, src: Path | Sequence[Path], dst: Path) -> None:
    """Decode ``src`` to 16 kHz mono 16-bit PCM WAV at ``dst`` (whisper input).

    Several sources (the microphone and the computer's sound) are added together
    (``amix`` without level normalisation, as long as the longest of them)."""
    sources = [src] if isinstance(src, Path) else list(src)
    if not sources:
        raise ValueError("no audio source to convert")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
    ]
    for source in sources:
        cmd += ["-i", str(source)]
    if len(sources) > 1:
        inputs = "".join(f"[{i}:a]" for i in range(len(sources)))
        cmd += [
            "-filter_complex",
            f"{inputs}amix=inputs={len(sources)}:duration=longest:normalize=0[mix]",
            "-map",
            "[mix]",
        ]
    cmd += [
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    log.debug("Running %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", creationflags=_creation_flags()
        )
    except OSError as exc:
        raise FfmpegError(f"cannot run ffmpeg {ffmpeg}: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise FfmpegError(f"ffmpeg failed with exit code {proc.returncode}: {tail}")
    if not dst.exists() or dst.stat().st_size <= EMPTY_WAV_MAX_BYTES:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        names = ", ".join(source.name for source in sources)
        raise FfmpegError(f"ffmpeg produced no audio for {names}: {tail or 'empty output'}")


# Download ---------------------------------------------------------------------


def resolve_btbn_source(os_name: str, arch: str) -> FfmpegSource:
    """Newest stable ``n<major>.<minor>`` GPL asset of BtbN's rolling ``latest`` release.

    The GitHub Releases API also returns each asset's SHA-256 ``digest``.
    """
    try:
        target, kind = _BTBN_TARGETS[(os_name, arch)]
    except KeyError:
        raise FfmpegNotFound(f"no BtbN FFmpeg build for {os_name}-{arch}") from None
    ext = "zip" if kind == "zip" else "tar.xz"
    pattern = re.compile(rf"^ffmpeg-n(\d+)\.(\d+)-latest-{target}-gpl-\d+\.\d+\.{re.escape(ext)}$")
    release = fetch_json(BTBN_RELEASE_API)
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for asset in release.get("assets", []):
        match = pattern.match(str(asset.get("name", "")))
        if not match:
            continue
        version = (int(match.group(1)), int(match.group(2)))
        if best is None or version > best[0]:
            best = (version, asset)
    if best is None:
        raise DownloadError(f"no {target} gpl asset found in {BTBN_RELEASE_API}")
    asset = best[1]
    digest = str(asset.get("digest") or "")
    sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else None
    return FfmpegSource(
        name=f"BtbN/FFmpeg-Builds {asset['name']}",
        url=str(asset["browser_download_url"]),
        kind=kind,
        sha256=sha256,
        size=int(asset["size"]) if asset.get("size") is not None else None,
    )


def resolve_jvs_source(os_name: str, arch: str) -> FfmpegSource:
    """John Van Sickle static build with its published ``.md5``."""
    try:
        archive = _JVS_ARCHIVES[(os_name, arch)]
    except KeyError:
        raise FfmpegNotFound(f"no johnvansickle.com FFmpeg build for {os_name}-{arch}") from None
    url = JVS_BASE_URL + archive
    md5_text = fetch_text(url + ".md5").strip()
    md5 = md5_text.split()[0] if md5_text else None
    if md5 is None or not re.fullmatch(r"[0-9a-fA-F]{32}", md5):
        raise DownloadError(f"unexpected md5 file content at {url}.md5: {md5_text!r}")
    return FfmpegSource(name=f"johnvansickle.com {archive}", url=url, kind="tar", md5=md5.lower())


def candidate_sources(os_name: str, arch: str) -> list[Callable[[], FfmpegSource]]:
    """Source resolvers to try in order for this platform."""
    if os_name == "windows":
        return [lambda: resolve_btbn_source(os_name, arch)]
    if os_name == "linux":
        return [
            lambda: resolve_jvs_source(os_name, arch),
            lambda: resolve_btbn_source(os_name, arch),
        ]
    return []


def extract_binaries(
    archive: Path, kind: ArchiveKind, target_dir: Path, names: list[str]
) -> list[Path]:
    """Copy the executables named in ``names`` (base names) out of the archive.

    Only regular files whose base name matches are read; archive paths are never
    used to build the destination, which protects against path traversal.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    wanted = {name: target_dir / name for name in names}
    written: list[Path] = []
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = PurePosixPath(info.filename).name
                dest = wanted.pop(base, None)
                if dest is None:
                    continue
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(dest)
    else:
        with tarfile.open(archive, mode="r:*") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                base = PurePosixPath(member.name).name
                dest = wanted.pop(base, None)
                if dest is None:
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(dest)
    for path in written:
        if not sys.platform.startswith("win"):
            path.chmod(path.stat().st_mode | 0o111)
    return written


def download_ffmpeg(target_dir: Path, progress: ProgressCallback | None = None) -> Path:
    """Download a static FFmpeg into ``target_dir`` and return the ffmpeg path.

    Provenance (URL, archive hashes, size, time, ``ffmpeg -version``) is recorded in
    ``target_dir/SOURCE.json``.
    """
    os_name, arch = platform_tag()
    resolvers = candidate_sources(os_name, arch)
    if not resolvers:
        raise FfmpegNotFound(
            f"automatic FFmpeg download is not available for {os_name}-{arch}; "
            "install ffmpeg manually and set ffmpeg_path in config.json"
        )
    names = [executable_name("ffmpeg", os_name), executable_name("ffprobe", os_name)]
    errors: list[str] = []
    for resolve in resolvers:
        try:
            source = resolve()
        except (DownloadError, FfmpegNotFound, KeyError, ValueError) as exc:
            errors.append(str(exc))
            log.warning("FFmpeg source unavailable: %s", exc)
            continue
        log.info("Downloading FFmpeg from %s", source.url)
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ffmpeg-dl-", dir=target_dir) as tmp:
            archive = Path(tmp) / PurePosixPath(source.url).name
            try:
                result = download_file(
                    source.url,
                    archive,
                    expected_sha256=source.sha256,
                    expected_md5=source.md5,
                    progress=progress,
                )
                written = extract_binaries(archive, source.kind, target_dir, names)
            except (DownloadError, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
                errors.append(f"{source.name}: {exc}")
                log.warning("FFmpeg download from %s failed: %s", source.url, exc)
                continue
        ffmpeg_path = target_dir / names[0]
        if ffmpeg_path not in written or not ffmpeg_path.is_file():
            errors.append(f"{source.name}: archive did not contain {names[0]}")
            continue
        version = ffmpeg_version(ffmpeg_path)
        provenance = {
            "source": source.name,
            "url": source.url,
            "archive_sha256": result.sha256,
            "archive_md5": result.md5,
            "archive_size": result.size,
            "published_sha256": source.sha256,
            "published_md5": source.md5,
            "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "version": version,
            "files": [p.name for p in written],
            "platform": f"{os_name}-{arch}",
        }
        (target_dir / SOURCE_FILE_NAME).write_text(
            json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
        )
        log.info("FFmpeg %s installed to %s", version, target_dir)
        return ffmpeg_path
    raise FfmpegNotFound("FFmpeg could not be downloaded: " + "; ".join(errors))


__all__ = [
    "BTBN_RELEASE_API",
    "JVS_BASE_URL",
    "SOURCE_FILE_NAME",
    "FfmpegError",
    "FfmpegNotFound",
    "FfmpegSource",
    "bundled_ffmpeg_dir",
    "bundled_ffmpeg_path",
    "candidate_sources",
    "convert_to_16k_mono",
    "download_ffmpeg",
    "ensure_ffmpeg",
    "executable_name",
    "extract_binaries",
    "ffmpeg_version",
    "find_ffmpeg",
    "platform_tag",
    "resolve_btbn_source",
    "resolve_jvs_source",
]
