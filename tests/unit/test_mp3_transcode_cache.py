"""Tests for FLAC→MP3 transcode cache."""

import asyncio
from pathlib import Path

import pytest

from plex.mp3_transcode_cache import (
    cache_path_for,
    ensure_transcoded_mp3,
    ffmpeg_mp3_file_args,
)
from plex.transcode_stream import build_sm6_transcode_proxy_url


def test_ffmpeg_mp3_file_args(tmp_path):
    out = tmp_path / "out.mp3"
    args = ffmpeg_mp3_file_args("http://plex.example/file.flac", out, cbr_kbps=192)
    assert args[args.index("-i") + 1] == "http://plex.example/file.flac"
    assert str(out) in args
    assert "-c:a" in args and "libmp3lame" in args
    assert args[args.index("-b:a") + 1] == "192k"
    assert "-write_xing" in args


def test_build_sm6_transcode_proxy_url(monkeypatch):
    from tests.fixtures.network import FAKE_HOST_IP
    from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A

    monkeypatch.setattr(
        "plex.transcode_stream.settings.host_ip",
        FAKE_HOST_IP,
        raising=False,
    )
    monkeypatch.setattr(
        "plex.transcode_stream.settings.http_port",
        32488,
        raising=False,
    )
    monkeypatch.setattr(
        "plex.transcode_stream.settings.get_token_for_uuid",
        lambda uuid: "test-token" if uuid == "dev-1" else None,
    )
    url = build_sm6_transcode_proxy_url(FAKE_TRACK_KEY_A, "dev-1")
    assert url.startswith(
        f"http://{FAKE_HOST_IP}:32488/player/stream/transcode.mp3?"
    )
    assert "ratingKey=900001" in url
    assert "device=dev-1" in url
    assert "sig=" in url


@pytest.mark.asyncio
async def test_concurrent_gets_share_one_encode(monkeypatch, tmp_path):
    calls = {"n": 0}

    async def fake_ffmpeg(
        source_url: str,
        output_path: Path,
        *,
        cbr_kbps: int,
        plex_token: str | None = None,
    ) -> None:
        calls["n"] += 1
        await asyncio.sleep(0.05)
        output_path.write_bytes(b"\xff\xfb" + b"\x00" * 128)

    monkeypatch.setattr(
        "plex.mp3_transcode_cache.cache_path_for",
        lambda rating_key, cbr_kbps=192: tmp_path / f"{rating_key}.mp3",
    )
    monkeypatch.setattr("plex.mp3_transcode_cache._run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr("plex.mp3_transcode_cache._registry", {})
    monkeypatch.setattr(
        "plex.mp3_transcode_cache._registry_lock",
        asyncio.Lock(),
    )

    paths = await asyncio.gather(
        ensure_transcoded_mp3("42", source_url="http://x/flac", cbr_kbps=192),
        ensure_transcoded_mp3("42", source_url="http://x/flac", cbr_kbps=192),
        ensure_transcoded_mp3("42", source_url="http://x/flac", cbr_kbps=192),
    )
    assert calls["n"] == 1
    assert paths[0] == paths[1] == paths[2]
    assert paths[0].is_file()
