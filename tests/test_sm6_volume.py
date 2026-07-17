"""Tests Plex ↔ SM6 volume conversion.

Optional hardware teardown: set SM6_TEST_RENDERING_CONTROL_URL to the SM6
RenderingControl SOAP endpoint; after this module finishes, SetVolume step 28
(100 %) is sent so the amplifier is left at full volume.
"""

import importlib.util
import os
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_dlna_pkg = types.ModuleType("dlna")
_dlna_pkg.__path__ = [str(_root / "dlna")]
sys.modules.setdefault("dlna", _dlna_pkg)

for name, filename in (
    ("dlna.volume_curve", "volume_curve.py"),
    ("dlna.sm6_volume", "sm6_volume.py"),
):
    spec = importlib.util.spec_from_file_location(name, _root / "dlna" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

sm6_volume = sys.modules["dlna.sm6_volume"]
volume_curve = sys.modules["dlna.volume_curve"]

VolumeRange = sm6_volume.VolumeRange
SM6_RANGE = VolumeRange(minimum=0, maximum=28, step=1)
DLNA_100_RANGE = VolumeRange(minimum=0, maximum=100, step=1)

device_to_step = sm6_volume.device_to_step
device_to_dlna_level = sm6_volume.device_to_dlna_level
device_to_plex = sm6_volume.device_to_plex
dlna_level_to_device = sm6_volume.dlna_level_to_device
dlna_level_to_plex = sm6_volume.dlna_level_to_plex
plex_to_device = sm6_volume.plex_to_device
plex_to_dlna_level = sm6_volume.plex_to_dlna_level
step_to_dlna_level = sm6_volume.step_to_dlna_level
plexamp_volume_step = sm6_volume.plexamp_volume_step
plex_to_step = sm6_volume.plex_to_step
plex_for_step = sm6_volume.plex_for_step
device_step = volume_curve.device_step
ui_to_device = volume_curve.ui_to_device
VOLUME_STEPS = volume_curve.VOLUME_STEPS

_UPNP_RC = "urn:schemas-upnp-org:service:RenderingControl:1"
_FULL_VOLUME_SOAP = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>"
    f'<u:SetVolume xmlns:u="{_UPNP_RC}">'
    "<InstanceID>0</InstanceID>"
    "<Channel>Master</Channel>"
    f"<DesiredVolume>{VOLUME_STEPS}</DesiredVolume>"
    "</u:SetVolume>"
    "</s:Body>"
    "</s:Envelope>"
)


@pytest.fixture(scope="module", autouse=True)
def _restore_sm6_hardware_volume_after_module() -> None:
    yield
    url = os.environ.get("SM6_TEST_RENDERING_CONTROL_URL")
    if not url:
        return
    request = urllib.request.Request(
        url,
        data=_FULL_VOLUME_SOAP.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_UPNP_RC}#SetVolume"',
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                pytest.fail(
                    f"SM6 volume restore HTTP {response.status} (expected step {VOLUME_STEPS})"
                )
    except urllib.error.URLError as exc:
        pytest.fail(f"SM6 volume restore to step {VOLUME_STEPS} failed: {exc}")


def test_step_to_dlna_level_sm6_native() -> None:
    assert step_to_dlna_level(0, SM6_RANGE) == 0
    assert step_to_dlna_level(6, SM6_RANGE) == 6
    assert step_to_dlna_level(28, SM6_RANGE) == 28


def test_plexamp_volume_step_requires_hardware_step() -> None:
    assert plexamp_volume_step(22, 23, hardware_step=None, max_step_delta=8) is None


def test_plexamp_volume_step_from_hardware() -> None:
    assert plexamp_volume_step(22, 23, hardware_step=15, max_step_delta=8) == 16
    assert plexamp_volume_step(22, 21, hardware_step=15, max_step_delta=8) == 14


def test_plex_to_dlna_level_sm6() -> None:
    assert plex_to_dlna_level(0, SM6_RANGE) == 0
    assert plex_to_dlna_level(2, SM6_RANGE) == 6
    assert plex_to_dlna_level(100, SM6_RANGE) == 28


def test_dlna_level_truncates_not_rounds() -> None:
    device = ui_to_device(0.83)
    assert device_to_dlna_level(device, SM6_RANGE) == int(device * 28)


def test_plex_to_device_uses_curve() -> None:
    assert plex_to_device(0) == 0.0
    assert plex_to_device(50) == ui_to_device(0.5)
    assert plex_to_device(100) == 1.0


def test_device_to_plex_roundtrip_halving() -> None:
    device = device_step(21)
    assert device_to_plex(device) == 50


def test_dlna_level_to_device_sm6() -> None:
    assert dlna_level_to_device(0, SM6_RANGE) == 0.0
    assert dlna_level_to_device(6, SM6_RANGE) == device_step(6)
    assert dlna_level_to_device(1, SM6_RANGE) == device_step(1)
    assert dlna_level_to_device(28, SM6_RANGE) == 1.0


def test_dlna_level_to_device_generic_0_100() -> None:
    assert dlna_level_to_device(53, DLNA_100_RANGE) == 0.53
    assert dlna_level_to_device(100, DLNA_100_RANGE) == 1.0


def test_read_sm6_level_to_plex() -> None:
    assert dlna_level_to_plex(15, SM6_RANGE) == 22
    assert dlna_level_to_plex(28, SM6_RANGE) == 100


def test_sm6_get_set_roundtrip() -> None:
    device = device_step(15)
    dlna = device_to_dlna_level(device, SM6_RANGE)
    assert dlna == 15
    parsed = dlna_level_to_device(dlna, SM6_RANGE)
    assert device_to_step(parsed) == 15
    assert device_to_plex(parsed) == device_to_plex(device)


def test_plex_step_mapping() -> None:
    assert plex_to_step(0) == 0
    assert plex_for_step(0) == 0
    assert plex_for_step(VOLUME_STEPS) == 100
    assert plex_to_step(plex_for_step(VOLUME_STEPS)) == VOLUME_STEPS
    for step in (15, 21, 22, VOLUME_STEPS):
        assert plex_to_step(plex_for_step(step)) == step
        assert step_to_dlna_level(step, SM6_RANGE) == step
    steps = [plex_to_step(p) for p in range(101)]
    assert all(a <= b for a, b in zip(steps, steps[1:], strict=False))


def test_step_to_dlna_level_generic_0_100_range() -> None:
    assert step_to_dlna_level(0, DLNA_100_RANGE) == 0
    assert step_to_dlna_level(28, DLNA_100_RANGE) == 100


def test_dlna_get_set_roundtrip_all_steps() -> None:
    for step in range(VOLUME_STEPS + 1):
        device = device_step(step)
        dlna = device_to_dlna_level(device, SM6_RANGE)
        parsed = dlna_level_to_device(dlna, SM6_RANGE)
        assert device_to_step(parsed) == step
        if step == 0:
            assert device_to_plex(parsed) == 0
        else:
            assert device_to_plex(parsed) == device_to_plex(device)


def test_sm6_full_volume_step_28() -> None:
    """Last test: 100 % Plex ↔ step 28 ↔ UPnP max on native SM6 range."""
    assert VOLUME_STEPS == 28
    assert device_step(VOLUME_STEPS) == 1.0
    assert device_to_step(1.0) == VOLUME_STEPS
    assert plex_to_step(100) == VOLUME_STEPS
    assert plex_for_step(VOLUME_STEPS) == 100
    assert plex_to_dlna_level(100, SM6_RANGE) == VOLUME_STEPS
    assert dlna_level_to_plex(VOLUME_STEPS, SM6_RANGE) == 100
    assert step_to_dlna_level(VOLUME_STEPS, SM6_RANGE) == VOLUME_STEPS
    assert dlna_level_to_device(VOLUME_STEPS, SM6_RANGE) == 1.0
