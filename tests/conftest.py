"""Shared pytest configuration."""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hardware: optional tests that contact a real SM6 (SM6_TEST_RENDERING_CONTROL_URL)",
    )


def _is_stub_module(mod) -> bool:
    if mod is None:
        return True
    file_attr = getattr(mod, "__file__", "") or ""
    return file_attr.startswith("<stub")


def ensure_real_plex_package() -> None:
    """Point ``plex`` at the repo package (avoid import-time stubs from other tests)."""
    plex_pkg = sys.modules.get("plex")
    if _is_stub_module(plex_pkg) or not getattr(plex_pkg, "__path__", None):
        plex_pkg = types.ModuleType("plex")
        plex_pkg.__path__ = [str(ROOT / "plex")]
        plex_pkg.__file__ = str(ROOT / "plex" / "__init__.py")
        sys.modules["plex"] = plex_pkg


def reload_module(name: str):
    """Drop cached module (and submodules) then import the real one."""
    prefix = name + "."
    for key in list(sys.modules):
        if key == name or key.startswith(prefix):
            del sys.modules[key]
    importlib.invalidate_caches()
    return importlib.import_module(name)


def ensure_real_settings():
    """Restore ``settings`` when another test file left a MagicMock stub in sys.modules."""
    for name in ("pydantic", "pydantic_settings"):
        if _is_stub_module(sys.modules.get(name)):
            reload_module(name)
    mod = sys.modules.get("settings")
    if _is_stub_module(mod) or isinstance(getattr(mod, "settings", None), MagicMock):
        reload_module("settings")
    import settings as settings_pkg

    return settings_pkg
