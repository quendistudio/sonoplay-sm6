"""Fictional Plex track metadata for unit tests.

Use only synthetic artist/album/title names and rating keys in the 900xxx range.
Never copy real library metadata, hostnames, or tokens from a production Plex server.
"""

from types import SimpleNamespace

# Synthetic rating keys (not tied to any real Plex library).
FAKE_TRACK_KEY_A = "900001"
FAKE_TRACK_KEY_B = "900002"
FAKE_TRACK_KEY_C = "900003"

# Example renderer transcode limits (device profile, not personal config).
EXAMPLE_THRESHOLD_KBPS = 5000
EXAMPLE_SAMPLE_RATE_HZ = 96000


def hi_res_flac_track(
    *,
    title: str = "Example Track A",
    album: str = "Example Hi-Res Album",
    artist: str = "Example Artist",
    rating_key: str = FAKE_TRACK_KEY_A,
    sample_rate: str | int = "192000",
    bitrate: str | int = "8256",
    duration: int | None = None,
) -> SimpleNamespace:
    """Plex-like hi-res FLAC track (192 kHz) with string fields like the Plex API."""
    part = SimpleNamespace(
        key=f"/library/parts/{rating_key}/file.flac",
        file=f"01 - {title}.flac",
    )
    media = SimpleNamespace(
        bitrate=bitrate,
        audioSampleRate=sample_rate,
        audioChannels=2,
        container="flac",
        Part=[part],
    )
    track = SimpleNamespace(
        ratingKey=rating_key,
        title=title,
        grandparentTitle=artist,
        parentTitle=album,
        type="track",
        Media=[media],
    )
    if duration is not None:
        track.duration = duration
    return track
