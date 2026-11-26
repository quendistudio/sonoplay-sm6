"""Shared pytest fixtures — no sys.modules stubbing."""

from __future__ import annotations

import pytest

# Break dlna_device ↔ plex.adapters circular import before any test module loads.
import plex.adapters  # noqa: E402,F401


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hardware: optional tests that contact a real SM6 (SM6_TEST_RENDERING_CONTROL_URL)",
    )


@pytest.fixture
def settings_override(monkeypatch):
    """Override attributes on the real ``settings.settings`` object."""
    from settings import settings as real_settings

    def _apply(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setattr(real_settings, key, value)
        return real_settings

    return _apply


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Isolate settings data / DLNA cache under a temporary directory."""
    from settings import settings as real_settings

    monkeypatch.setattr(real_settings, "data_file_name", str(tmp_path / "data.json"))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_sm6_module_registries():
    """Reset per-process SM6 registries between tests."""
    yield
    try:
        from dlna.sm6_dispatcher import _DISPATCHERS

        _DISPATCHERS.clear()
    except Exception:
        pass
    try:
        from dlna import sm6_control

        sm6_control._QUEUE_FOLDER_INFLIGHT.clear()
        sm6_control._NAVIGATOR_ID_CACHE.clear()
    except Exception:
        pass
    try:
        from plex import adapters as adapters_mod

        adapters_mod.adapters.clear()
    except Exception:
        pass
