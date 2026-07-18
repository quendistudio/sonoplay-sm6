"""Plex DLNA ServerUDN resolution."""

from plex.dlna_browser import machine_identifier_to_dlna_udn


def test_machine_identifier_to_dlna_udn() -> None:
    machine_id = "3bc6d600ec61c760ca28d377e33cb8d87daf7211"
    assert machine_identifier_to_dlna_udn(machine_id) == (
        "3bc6d600-ec61-c760-ca28-d377e33cb8d8"
    )
