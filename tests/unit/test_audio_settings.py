import sys
import types
from unittest.mock import MagicMock

from tests.conftest import drop_stub_modules, purge_settings_modules

# Stub heavy deps; pydantic stubs let settings import in isolation.
_STUB_MODULES = [
    "dotmap",
    "aiohttp",
    "uvicorn",
    "starlette",
    "starlette.datastructures",
    "fastapi",
    "fastapi.responses",
    "fastapi.templating",
    "fastapi.staticfiles",
    "pydantic",
    "pydantic.settings",
    "pydantic_settings",
    "jinja2",
]

for _name in _STUB_MODULES:
    if _name not in sys.modules:
        mod = types.ModuleType(_name)
        mod.__path__ = []
        mod.__file__ = f"<stub {_name}>"
        mod.__getattr__ = lambda attr: MagicMock()
        sys.modules[_name] = mod

purge_settings_modules()

from settings.datastore import JSONDataStore  # noqa: E402

# Drop pydantic stubs but keep the settings module (needs _data_lock at runtime).
drop_stub_modules(
    "pydantic",
    "pydantic.settings",
    "pydantic_settings",
)


def _make_store():
    mock_settings = MagicMock()
    mock_settings.load_data.return_value = {"__meta__": {}}
    return JSONDataStore(mock_settings)


def test_set_audio_settings_sample_rate_only():
    """set_audio_settings must not crash when only sample_rate_hz is provided."""
    store = _make_store()
    store.set_audio_settings(sample_rate_hz=44100)


def test_set_audio_settings_bitrate_only():
    """set_audio_settings must work when only bitrate_kbps is provided."""
    store = _make_store()
    store.set_audio_settings(bitrate_kbps=320)
