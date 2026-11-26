"""JSONDataStore audio settings — optional fields must not crash."""

from unittest.mock import MagicMock

from settings.datastore import JSONDataStore


def _make_store():
    mock_settings = MagicMock()
    mock_settings.load_data.return_value = {"__meta__": {}}
    return JSONDataStore(mock_settings)


def test_set_audio_settings_sample_rate_only():
    store = _make_store()
    store.set_audio_settings(sample_rate_hz=44100)


def test_set_audio_settings_bitrate_only():
    store = _make_store()
    store.set_audio_settings(bitrate_kbps=320)
