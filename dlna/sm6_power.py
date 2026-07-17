"""SM6 power state SOAP (UuVolControl:5, RecivaRadio/invoke)."""
from __future__ import annotations

import re

_UUVOL_CONTROL_NS = "urn:UuVol-com:service:UuVolControl:5"

GET_POWER_STATE_ACTION = f'"{_UUVOL_CONTROL_NS}#GetPowerState"'
SET_POWER_STATE_ACTION = f'"{_UUVOL_CONTROL_NS}#SetPowerState"'

_RET_POWER_STATE = re.compile(
    r"<RetPowerStateValue>([^<]*)</RetPowerStateValue>",
    re.IGNORECASE,
)

_POWER_STATES = frozenset({"ON", "OFF", "IDLE"})


def _soap_envelope(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<s:Body>{inner}</s:Body></s:Envelope>"
    )


def build_get_power_state_body() -> str:
    return _soap_envelope(f'<u:GetPowerState xmlns:u="{_UUVOL_CONTROL_NS}"/>')


def build_set_power_state_body(state: str) -> str:
    value = state.strip().upper()
    if value not in _POWER_STATES:
        raise ValueError(f"invalid SM6 power state: {state!r}")
    return _soap_envelope(
        f'<u:SetPowerState xmlns:u="{_UUVOL_CONTROL_NS}">'
        f"<NewPowerStateValue>{value}</NewPowerStateValue>"
        "</u:SetPowerState>"
    )


def parse_power_state(xml: str) -> str | None:
    match = _RET_POWER_STATE.search(xml)
    if not match:
        return None
    value = match.group(1).strip().upper()
    if value in _POWER_STATES:
        return value
    return None
