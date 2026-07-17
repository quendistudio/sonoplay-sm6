"""Encode Plex source media once per track; share result across concurrent SM6 GETs."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffmpeg_mp3_file_args(source_url: str, output_path: Path, *, cbr_kbps: int) -> list[str]:
    """FLAC (or other) HTTP/file input → complete CBR MP3 on disk."""
    if cbr_kbps <= 0:
        raise ValueError("cbr_kbps must be positive")
    bitrate = f"{int(cbr_kbps)}k"
    return [
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_url,
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-write_xing",
        "1",
        str(output_path),
    ]


def cache_dir() -> Path:
    from settings import settings

    root = Path(settings.config_path) / "transcode_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_path_for(rating_key: str, *, cbr_kbps: int) -> Path:
    return cache_dir() / f"{rating_key}-{cbr_kbps}.mp3"


@dataclass
class _EncodeJob:
    task: asyncio.Task | None = None
    path: Path | None = None
    error: BaseException | None = None
    waiters: int = 0


_registry: dict[str, _EncodeJob] = {}
_registry_lock = asyncio.Lock()


def _job_key(rating_key: str, cbr_kbps: int) -> str:
    return f"{rating_key}:{cbr_kbps}"


async def _run_ffmpeg(source_url: str, output_path: Path, *, cbr_kbps: int) -> None:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".tmp.mp3")
    if tmp_path.exists():
        tmp_path.unlink()

    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        *ffmpeg_mp3_file_args(source_url, tmp_path, cbr_kbps=cbr_kbps),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _stderr = await proc.stderr.read() if proc.stderr else b""
    rc = await proc.wait()
    if rc != 0:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        detail = _stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg exited {rc}: {detail[:500]}")

    if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg produced empty MP3")

    tmp_path.replace(output_path)


async def ensure_transcoded_mp3(
    rating_key: str,
    *,
    source_url: str,
    cbr_kbps: int,
) -> Path:
    """
    Return path to a complete MP3 file for ``rating_key``.

    Concurrent callers share a single ffmpeg encode (one transcode per track).
    """
    key = _job_key(rating_key, cbr_kbps)
    output_path = cache_path_for(rating_key, cbr_kbps=cbr_kbps)

    async with _registry_lock:
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path

        job = _registry.get(key)
        if job is None or job.task is None or job.task.done():
            if job and job.task and job.task.done() and job.path and job.path.is_file():
                return job.path
            job = _EncodeJob(path=output_path)

            async def _worker() -> None:
                try:
                    await _run_ffmpeg(source_url, output_path, cbr_kbps=cbr_kbps)
                    job.path = output_path
                    logger.info(
                        "transcode cache ready ratingKey=%s cbr=%s kbps bytes=%s",
                        rating_key,
                        cbr_kbps,
                        output_path.stat().st_size,
                    )
                except BaseException as exc:
                    job.error = exc
                    async with _registry_lock:
                        if _registry.get(key) is job:
                            del _registry[key]
                    raise

            job.task = asyncio.create_task(_worker())
            _registry[key] = job

        job.waiters += 1
        task = job.task

    assert task is not None
    try:
        await task
    finally:
        async with _registry_lock:
            job = _registry.get(key)
            if job:
                job.waiters = max(0, job.waiters - 1)

    async with _registry_lock:
        job = _registry.get(key)
        if job and job.error:
            raise job.error
        if job and job.path and job.path.is_file():
            return job.path
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path

    raise RuntimeError("transcode finished without output file")
