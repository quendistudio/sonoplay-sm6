"""Unit tests for Cambridge Stream Magic device detection."""

from plex.device_profiles import needs_plex_dlna_stream_url


class _Device:
    def __init__(self, *, manufacturer="", model_name="", name=""):
        self.manufacturer = manufacturer
        self.model_name = model_name
        self.name = name


def test_stream_magic_6_by_manufacturer_and_model_name():
    device = _Device(
        manufacturer="Cambridge Audio",
        model_name="Stream Magic 6",
        name="Salon",
    )
    assert needs_plex_dlna_stream_url(device)


def test_friendly_name_alone_is_not_enough():
    device = _Device(name="Stream Magic 6", manufacturer="", model_name="")
    assert not needs_plex_dlna_stream_url(device)


def test_modern_cambridge_cxn_not_matched():
    device = _Device(
        manufacturer="Cambridge Audio",
        model_name="CXN V2",
        name="CXN",
    )
    assert not needs_plex_dlna_stream_url(device)


def test_sonos_not_matched():
    device = _Device(
        manufacturer="Sonos, Inc.",
        model_name="Sonos One",
        name="Kitchen",
    )
    assert not needs_plex_dlna_stream_url(device)
