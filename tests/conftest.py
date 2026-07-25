"""Shared pytest configuration."""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Modules that stubbing SM6 adapter tests must not leave behind for other files.
SHARED_STUB_MODULES = (
    "settings",
    "pydantic",
    "pydantic.settings",
    "pydantic_settings",
    "plex.subscribe",
    "plex.plexserver",
    "plex.play_queue",
    "utils",
    "version",
    "dlna",
    "dlna.dlna_device",
    "dlna.discover",
    "dlna.virtual",
    "dlna.virtual.devices",
    "dlna.virtual.volume",
    "dlna.quirks",
    "fastapi",
    "fastapi.responses",
    "fastapi.templating",
    "fastapi.staticfiles",
    "starlette",
    "starlette.datastructures",
    "aiohttp",
    "uvicorn",
    "jinja2",
    "xmltodict",
)


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
    """Ensure ``plex`` is a real package path (keep already-loaded real submodules)."""
    plex_pkg = sys.modules.get("plex")
    if plex_pkg is None or _is_stub_module(plex_pkg) or getattr(
        plex_pkg, "__file__", ""
    ) == "<stub plex>":
        if "plex" in sys.modules:
            del sys.modules["plex"]
        for key in list(sys.modules):
            if key.startswith("plex.") and _is_stub_module(sys.modules.get(key)):
                del sys.modules[key]
        plex_pkg = types.ModuleType("plex")
        plex_pkg.__path__ = [str(ROOT / "plex")]
        plex_pkg.__file__ = str(ROOT / "plex" / "__init__.py")
        sys.modules["plex"] = plex_pkg


def ensure_real_dlna_package() -> None:
    """Load the real ``dlna`` package (stubs leave an empty module without exports)."""
    dlna_pkg = sys.modules.get("dlna")
    if dlna_pkg is None or _is_stub_module(dlna_pkg) or str(
        getattr(dlna_pkg, "__file__", "")
    ).startswith("<stub"):
        for key in list(sys.modules):
            if key == "dlna" or key.startswith("dlna."):
                del sys.modules[key]
        importlib.import_module("dlna")


def reload_module(name: str):
    """Drop cached module (and submodules) then import the real one."""
    prefix = name + "."
    for key in list(sys.modules):
        if key == name or key.startswith(prefix):
            del sys.modules[key]
    importlib.invalidate_caches()
    return importlib.import_module(name)


def purge_settings_modules() -> None:
    """Drop settings package so the next import loads a clean module."""
    for key in list(sys.modules):
        if key == "settings" or key.startswith("settings."):
            del sys.modules[key]


def ensure_real_settings():
    """Always reload ``settings`` against real pydantic (stubs leave broken modules)."""
    for name in ("pydantic", "pydantic_settings", "pydantic.settings"):
        mod = sys.modules.get(name)
        if name in sys.modules and (
            _is_stub_module(mod) or isinstance(mod, MagicMock)
        ):
            del sys.modules[name]
        prefix = name + "."
        for key in list(sys.modules):
            if key.startswith(prefix) and (
                _is_stub_module(sys.modules.get(key))
                or isinstance(sys.modules.get(key), MagicMock)
            ):
                del sys.modules[key]
    # If pydantic itself is missing after stub deletion, import the real one.
    if "pydantic" not in sys.modules or _is_stub_module(sys.modules.get("pydantic")):
        importlib.import_module("pydantic")
    if "pydantic_settings" not in sys.modules or _is_stub_module(
        sys.modules.get("pydantic_settings")
    ):
        importlib.import_module("pydantic_settings")
    purge_settings_modules()
    return reload_module("settings")


def drop_stub_modules(*names: str) -> None:
    """Remove ``<stub …>`` entries from ``sys.modules`` so later tests can import reals."""
    targets = names or SHARED_STUB_MODULES
    for name in targets:
        mod = sys.modules.get(name)
        if name in sys.modules and (
            _is_stub_module(mod) or isinstance(mod, MagicMock)
        ):
            del sys.modules[name]
        prefix = name + "."
        for key in list(sys.modules):
            child = sys.modules.get(key)
            if key.startswith(prefix) and (
                _is_stub_module(child) or isinstance(child, MagicMock)
            ):
                del sys.modules[key]
    ensure_real_plex_package()
    ensure_real_dlna_package()


def bare_sm6_adapter(cls):
    """``__new__`` adapter with SM6 latch fields that real ``__init__`` would set."""
    adapter = object.__new__(cls)
    adapter._sm6_outbound_until = None
    adapter._sm6_volume_grace_until = None
    adapter._sm6_relinquished_control = False
    adapter._sm6_sonoplay_owned_playback = False
    adapter._sm6_plex_play_in_progress = False
    adapter._sm6_plex_play_epoch = 0
    adapter._sm6_plex_clients_detached = False
    adapter._sm6_queue_base_offset = 0
    adapter._sm6_last_skip_previous_mono = None
    adapter._sm6_play_notify_task = None
    adapter._suppress_auto_next = False
    adapter._sm6_last_plex_playlist_fingerprint = None
    adapter.loop = None
    return adapter
