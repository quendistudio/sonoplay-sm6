"""DIDL metadata for SM6 transcode proxy tracks."""

from unittest.mock import patch

from tests.conftest import ensure_real_settings
from tests.fixtures.network import FAKE_HOST_IP
from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A, hi_res_flac_track

ensure_real_settings()

from plex import url_resolver as url_resolver_mod  # noqa: E402
from plex.url_resolver import build_transcode_track_didl  # noqa: E402


def test_transcode_didl_includes_duration_and_estimated_size(monkeypatch):
    settings_pkg = ensure_real_settings()
    monkeypatch.setattr(settings_pkg.settings, "audio_transcode_proxy_kbps", 320)
    monkeypatch.setattr(settings_pkg.settings, "config_path", "config")
    monkeypatch.setattr(url_resolver_mod, "settings", settings_pkg.settings)
    track = hi_res_flac_track(duration=180000)
    url = (
        f"http://{FAKE_HOST_IP}:32488/player/stream/transcode.mp3"
        f"?ratingKey={FAKE_TRACK_KEY_A}"
    )
    with patch("plex.mp3_transcode_cache.cache_file_valid", return_value=False):
        didl = build_transcode_track_didl(track, url)
    assert 'duration="0:03:00.000"' in didl
    assert 'size="7200000"' in didl  # 180s @ 320 kbps CBR
    assert url in didl
    assert "audio/mpeg" in didl
