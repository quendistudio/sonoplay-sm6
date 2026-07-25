"""Tests for PlayQueue playability / transcode detection."""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.conftest import ensure_real_plex_package, ensure_real_settings, reload_module
from tests.fixtures.plex_tracks import (
    EXAMPLE_SAMPLE_RATE_HZ,
    EXAMPLE_THRESHOLD_KBPS,
    FAKE_TRACK_KEY_A,
    FAKE_TRACK_KEY_B,
    hi_res_flac_track,
)


def _load_play_queue_class():
    """Other unit tests stub plex.play_queue; reload the real module when needed."""
    mod = sys.modules.get("plex.play_queue")
    if mod is None or isinstance(getattr(mod, "PlayQueue", None), MagicMock):
        ensure_real_plex_package()
        mod = reload_module("plex.play_queue")
    return mod.PlayQueue


class FakePlexLib:
    def __init__(self):
        self.metadata_by_key: dict = {}
        self.client_identifier = "90000000-0000-0000-0000-000000000001"
        self.protocol = "https"
        self.address = "plex.example"
        self.port = 32400
        self.token = "test-token"

    async def fetch_metadata(self, key: str):
        rating_key = key.rstrip("/").rsplit("/", 1)[-1]
        return self.metadata_by_key.get(rating_key)

    def build_url(self, resource, token=True):
        url = f"{self.protocol}://{self.address}:{self.port}{resource}"
        if token and "X-Plex-Token=" not in resource:
            sep = "&" if "?" in resource else "?"
            url += f"{sep}X-Plex-Token={self.token}"
        return url


@pytest.fixture
def play_queue():
    PlayQueue = _load_play_queue_class()
    return PlayQueue("/playQueues/1", FakePlexLib())


@pytest.fixture
def transcode_thresholds(monkeypatch):
    settings_pkg = ensure_real_settings()
    from settings import Settings

    real_settings = Settings()
    monkeypatch.setattr(settings_pkg, "settings", real_settings)
    monkeypatch.setattr(
        real_settings, "audio_transcode_threshold_kbps", EXAMPLE_THRESHOLD_KBPS
    )
    monkeypatch.setattr(
        real_settings, "audio_transcode_max_sample_rate_hz", EXAMPLE_SAMPLE_RATE_HZ
    )
    return real_settings


class TestIsTrackPlayable:
    def test_hi_res_string_fields_need_transcode(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track()
        assert play_queue.is_track_playable(track) is False

    def test_hi_res_int_fields_need_transcode(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(
            title="Example Track B",
            rating_key=FAKE_TRACK_KEY_B,
            sample_rate=192000,
            bitrate=6000,
        )
        assert play_queue.is_track_playable(track) is False

    def test_high_bitrate_only_triggers_transcode(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(sample_rate="48000", bitrate="6000")
        assert play_queue.is_track_playable(track) is False

    def test_within_limits_is_playable(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(sample_rate="96000", bitrate="3200")
        assert play_queue.is_track_playable(track) is True

    def test_cd_quality_is_playable(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(sample_rate="44100", bitrate="1411")
        assert play_queue.is_track_playable(track) is True

    def test_no_media_is_playable_until_enriched(self, play_queue, transcode_thresholds):
        track = SimpleNamespace(
            ratingKey=FAKE_TRACK_KEY_A,
            title="Example Track A",
            parentTitle="Example Hi-Res Album",
        )
        assert play_queue.is_track_playable(track) is True

    def test_empty_sample_rate_strings_are_playable_until_enriched(
        self, play_queue, transcode_thresholds
    ):
        track = hi_res_flac_track(sample_rate="", bitrate="")
        assert play_queue.is_track_playable(track) is True

    def test_plex_string_compare_bug_is_type_error(self):
        with pytest.raises(TypeError):
            _ = "192000" > EXAMPLE_SAMPLE_RATE_HZ

    def test_dotmap_phantom_attrs_treated_as_missing(self, play_queue, transcode_thresholds):
        from dotmap import DotMap

        media = DotMap({"container": "flac"})
        track = DotMap({"ratingKey": FAKE_TRACK_KEY_A, "Media": [media]})
        assert play_queue._media_playability_values(media) == (None, None)
        assert play_queue.is_track_playable(track) is True


class TestTrackNeedsTranscode:
    @pytest.mark.asyncio
    async def test_hi_res_track_with_string_fields(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(title="Example Track C")
        assert await play_queue.track_needs_transcode(track) is True

    @pytest.mark.asyncio
    async def test_enriches_track_without_media_via_fetch_metadata(
        self, play_queue, transcode_thresholds
    ):
        track = SimpleNamespace(
            ratingKey=FAKE_TRACK_KEY_A,
            title="Example Track A",
            parentTitle="Example Hi-Res Album",
        )
        play_queue.plex_lib.metadata_by_key[FAKE_TRACK_KEY_A] = hi_res_flac_track()
        assert await play_queue.track_needs_transcode(track) is True

    @pytest.mark.asyncio
    async def test_playable_track_does_not_need_transcode(self, play_queue, transcode_thresholds):
        track = hi_res_flac_track(sample_rate="96000", bitrate="3200")
        assert await play_queue.track_needs_transcode(track) is False


class TestBuildTranscodeUrl:
    def test_music_endpoint_and_required_query_params(self, play_queue, transcode_thresholds):
        url = play_queue.build_transcode_url(hi_res_flac_track())
        assert "/music/:/transcode/universal/start.m3u8" in url
        assert "protocol=hls" in url
        assert "maxAudioBitrate=320" in url
        assert "mediaBufferSize=12288" in url
        assert "directStream=0" in url
        assert "directStreamAudio=1" in url
        assert "hasMDE=1" in url
        assert "location=lan" in url
        assert "X-Plex-Client-Profile-Name=Generic" in url
        assert "X-Plex-Client-Profile-Extra=" in url
        assert "add-transcode-target" in url
        assert "protocol=hls" in url
        assert "mpegts" in url
        assert "audio.bitrate" in url
        assert "X-Plex-Client-Identifier=" in url
        assert "X-Plex-Product=" in url
        assert f"path=%2Flibrary%2Fmetadata%2F{FAKE_TRACK_KEY_A}" in url

    def test_sm6_proxy_url_points_at_sonoplay(self, play_queue, transcode_thresholds, monkeypatch):
        from settings import Settings
        from tests.fixtures.network import FAKE_HOST_IP

        monkeypatch.setattr(transcode_thresholds, "host_ip", FAKE_HOST_IP, raising=False)
        monkeypatch.setattr(transcode_thresholds, "http_port", 32488, raising=False)
        monkeypatch.setattr("plex.transcode_stream.settings", transcode_thresholds)
        monkeypatch.setattr("plex.transcode_auth.settings", transcode_thresholds)
        monkeypatch.setattr(
            Settings,
            "get_token_for_uuid",
            lambda self, uuid: "test-token" if uuid == "dev-1" else None,
        )
        url = play_queue.build_sm6_transcode_proxy_url(
            hi_res_flac_track(), device_uuid="dev-1"
        )
        assert url.startswith(
            f"http://{FAKE_HOST_IP}:32488/player/stream/transcode.mp3?"
        )
        assert f"ratingKey={FAKE_TRACK_KEY_A}" in url
        assert "device=dev-1" in url
        assert "32400" not in url
        assert "start.m3u8" not in url


class TestPlayQueueServerClear:
    def test_play_queue_id_from_container(self):
        PlayQueue = _load_play_queue_class()
        assert PlayQueue.play_queue_id_from_container("/playQueues/3888?own=1") == 3888
        assert PlayQueue.play_queue_id_from_container(None) is None

    @pytest.mark.asyncio
    async def test_clear_server_play_queue(self, monkeypatch):
        PlayQueue = _load_play_queue_class()
        lib = FakePlexLib()
        lib.request_headers = lambda *, accept_json=False: {"Accept": "application/json"}

        class FakeResponse:
            status = 200

            async def text(self):
                return ""

        class FakeDeleteCtx:
            def __init__(self):
                self._url = None

            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *args):
                return False

        def fake_delete(url, headers=None):
            assert "/playQueues/3888/items" in url
            return FakeDeleteCtx()

        monkeypatch.setattr("plex.play_queue.g.http.delete", fake_delete)
        assert await PlayQueue.clear_server_play_queue(lib, 3888) is True
