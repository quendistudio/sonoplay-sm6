"""RenderingControl URL rewrite must not break Plex :8050 proxy paths."""

from dlna.sm6_rendering_control import rewrite_sm6_rendering_control_url

_PROXY_RC = (
    "http://192.168.50.99:8050/e09a5789-074b-448c-9727-fe268db27f86/RenderingControl/control"
)
_PROXY_DESC = (
    "http://192.168.50.99:8050/e09a5789-074b-448c-9727-fe268db27f86/description.xml"
)
_NATIVE_DESC = "http://192.168.50.99:80/description.xml"


def test_proxy_preferred_keeps_uuid_path() -> None:
    assert rewrite_sm6_rendering_control_url(_PROXY_RC, _PROXY_DESC) == _PROXY_RC


def test_native_preferred_rewrites_host_and_truncates_to_rc() -> None:
    assert (
        rewrite_sm6_rendering_control_url(_PROXY_RC, _NATIVE_DESC)
        == "http://192.168.50.99:80/RenderingControl/control"
    )


def test_native_control_url_unchanged() -> None:
    native_rc = "http://192.168.50.99:80/RenderingControl/control"
    assert rewrite_sm6_rendering_control_url(native_rc, _NATIVE_DESC) == native_rc
