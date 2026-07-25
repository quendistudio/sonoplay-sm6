"""Tests for DLNA SOAP exponential backoff."""

import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "dlna.soap_backoff",
    _root / "dlna" / "soap_backoff.py",
)
soap_backoff = importlib.util.module_from_spec(_spec)
sys.modules["dlna.soap_backoff"] = soap_backoff
assert _spec.loader is not None
_spec.loader.exec_module(soap_backoff)
soap_backoff_seconds = soap_backoff.soap_backoff_seconds


def test_soap_backoff_zero_when_no_failures() -> None:
    assert soap_backoff_seconds(0) == 0.0


def test_soap_backoff_exponential_with_cap() -> None:
    assert soap_backoff_seconds(1, base=0.5, maximum=30.0) == 0.5
    assert soap_backoff_seconds(2, base=0.5, maximum=30.0) == 1.0
    assert soap_backoff_seconds(3, base=0.5, maximum=30.0) == 2.0
    assert soap_backoff_seconds(8, base=0.5, maximum=30.0) == 30.0
