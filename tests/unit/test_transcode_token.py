"""Unit tests for deterministic PMS token selection."""

from plex.transcode_stream import resolve_pms_token_for_proxy


def test_resolve_pms_token_prefers_env(monkeypatch):
    monkeypatch.setattr(
        "plex.transcode_stream.settings.plex_pms_token",
        "env-token",
    )
    monkeypatch.setattr(
        "plex.transcode_stream.settings.load_data",
        lambda: {"dev-1": {"token": "paired-token"}},
    )
    assert resolve_pms_token_for_proxy(device_uuid="dev-1") == "env-token"


def test_resolve_pms_token_uses_device_pairing(monkeypatch):
    monkeypatch.setattr("plex.transcode_stream.settings.plex_pms_token", None)
    monkeypatch.setattr(
        "plex.transcode_stream.settings.get_token_for_uuid",
        lambda uuid: "device-a-token" if uuid == "dev-a" else None,
    )
    monkeypatch.setattr(
        "plex.transcode_stream.settings.load_data",
        lambda: {
            "dev-a": {"token": "device-a-token"},
            "dev-b": {"token": "device-b-token"},
        },
    )
    assert resolve_pms_token_for_proxy(device_uuid="dev-a") == "device-a-token"


def test_resolve_pms_token_rejects_multi_account_without_env(monkeypatch):
    monkeypatch.setattr("plex.transcode_stream.settings.plex_pms_token", None)
    monkeypatch.setattr(
        "plex.transcode_stream.settings.get_token_for_uuid",
        lambda uuid: None,
    )
    monkeypatch.setattr(
        "plex.transcode_stream.settings.load_data",
        lambda: {
            "dev-a": {"token": "token-a"},
            "dev-b": {"token": "token-b"},
        },
    )
    assert resolve_pms_token_for_proxy() is None
