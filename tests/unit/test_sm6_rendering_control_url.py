"""RenderingControl URL rewrite must not break Plex :8050 proxy paths."""

import importlib.util
import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Minimal stubs so sm6_rendering_control imports without the full app stack.
_settings = types.ModuleType("settings")
_settings.settings = types.SimpleNamespace(
    http_timeout_dlna=5,
    sm6_volume_debounce_seconds=0.15,
)
sys.modules.setdefault("settings", _settings)

_utils = types.ModuleType("utils")
_utils.UPNP_RC_SERVICE_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"
_utils.g = types.SimpleNamespace(http=None)
sys.modules.setdefault("utils", _utils)

_spec = importlib.util.spec_from_file_location(
    "dlna.sm6_rendering_control",
    _root / "dlna" / "sm6_rendering_control.py",
)
rc = importlib.util.module_from_spec(_spec)
sys.modules["dlna.sm6_rendering_control"] = rc
assert _spec.loader is not None
_spec.loader.exec_module(rc)

rewrite = rc.rewrite_sm6_rendering_control_url

_PROXY_RC = (
    "http://192.168.50.99:8050/e09a5789-074b-448c-9727-fe268db27f86/RenderingControl/control"
)
_PROXY_DESC = (
    "http://192.168.50.99:8050/e09a5789-074b-448c-9727-fe268db27f86/description.xml"
)
_NATIVE_DESC = "http://192.168.50.99:80/description.xml"


def test_proxy_preferred_keeps_uuid_path() -> None:
    assert rewrite(_PROXY_RC, _PROXY_DESC) == _PROXY_RC


def test_native_preferred_rewrites_host_and_truncates_to_rc() -> None:
    assert (
        rewrite(_PROXY_RC, _NATIVE_DESC)
        == "http://192.168.50.99:80/RenderingControl/control"
    )


def test_native_control_url_unchanged() -> None:
    native_rc = "http://192.168.50.99:80/RenderingControl/control"
    assert rewrite(native_rc, _NATIVE_DESC) == native_rc
