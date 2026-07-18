"""DIDL metadata for SM6 transcode proxy tracks."""

from plex.url_resolver import build_transcode_track_didl
from settings import settings
from tests.fixtures.network import FAKE_HOST_IP
from tests.fixtures.plex_tracks import FAKE_TRACK_KEY_A, hi_res_flac_track


def test_transcode_didl_includes_duration_and_estimated_size(monkeypatch):
    monkeypatch.setattr(settings, "audio_transcode_proxy_kbps", 320, raising=False)
    track = hi_res_flac_track(duration=180000)
    url = (
        f"http://{FAKE_HOST_IP}:32488/player/stream/transcode.mp3"
        f"?ratingKey={FAKE_TRACK_KEY_A}"
    )
    didl = build_transcode_track_didl(track, url)
    assert 'duration="0:03:00.000"' in didl
    assert 'size="7200000"' in didl  # 180s @ 320 kbps CBR
    assert url in didl
    assert "audio/mpeg" in didl
