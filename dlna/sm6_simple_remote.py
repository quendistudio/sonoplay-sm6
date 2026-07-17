"""SOAP KeyPressed — Cambridge Stream Magic remote (RecivaSimpleRemote)."""
from __future__ import annotations

_SIMPLE_REMOTE_NS = "urn:UuVol-com:service:UuVolSimpleRemote:1"
SIMPLE_REMOTE_SOAP_ACTION = f'"{_SIMPLE_REMOTE_NS}#KeyPressed"'

KEY_PLAY_PAUSE = "PLAY_PAUSE"
KEY_STOP = "STOP"
KEY_SKIP_NEXT = "SKIP_NEXT"
KEY_SKIP_PREVIOUS = "SKIP_PREVIOUS"
KEY_INFO = "INFO"
KEY_DURATION_SHORT = "SHORT"


def simple_remote_invoke_url(description_url: str) -> str:
    base = description_url.rsplit("/", 1)[0]
    return f"{base}/RecivaSimpleRemote/invoke"


def build_key_pressed_body(key: str, duration: str = KEY_DURATION_SHORT) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        f'<u:KeyPressed xmlns:u="{_SIMPLE_REMOTE_NS}">'
        f"<Key>{key}</Key><Duration>{duration}</Duration>"
        "</u:KeyPressed>"
        "</s:Body></s:Envelope>"
    )
