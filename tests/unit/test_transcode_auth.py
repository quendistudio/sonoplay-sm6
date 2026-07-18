"""Unit tests for transcode URL signing."""

import time

import pytest

from plex.transcode_auth import (
    build_signed_transcode_query,
    verify_signed_transcode_request,
)


@pytest.fixture
def paired_device(monkeypatch):
    monkeypatch.setattr(
        "plex.transcode_auth.settings.get_token_for_uuid",
        lambda uuid: "test-plex-token-abc" if uuid == "device-uuid-1" else None,
    )
    monkeypatch.setattr(
        "plex.transcode_auth._device_is_registered_sm6",
        lambda uuid: uuid == "device-uuid-1",
    )


def test_signed_transcode_roundtrip(paired_device):
    query = build_signed_transcode_query("900001", "device-uuid-1", ttl_seconds=3600)
    assert "ratingKey=900001" in query
    assert "device=device-uuid-1" in query
    assert "sig=" in query

    parts = dict(p.split("=", 1) for p in query.split("&"))
    assert verify_signed_transcode_request(
        parts["ratingKey"],
        parts["device"],
        int(parts["exp"]),
        parts["sig"],
    )


def test_signed_transcode_rejects_expired(paired_device):
    query = build_signed_transcode_query("900001", "device-uuid-1", ttl_seconds=-10)
    parts = dict(p.split("=", 1) for p in query.split("&"))
    assert not verify_signed_transcode_request(
        parts["ratingKey"],
        parts["device"],
        int(parts["exp"]),
        parts["sig"],
    )


def test_signed_transcode_rejects_wrong_device(paired_device):
    query = build_signed_transcode_query("900001", "device-uuid-1")
    parts = dict(p.split("=", 1) for p in query.split("&"))
    assert not verify_signed_transcode_request(
        parts["ratingKey"],
        "other-device",
        int(parts["exp"]),
        parts["sig"],
    )
