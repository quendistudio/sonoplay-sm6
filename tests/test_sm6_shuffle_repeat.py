"""Tests SM6 SOAP shuffle/repeat (PlaylistExtension)."""

import importlib.util
import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_dlna_pkg = types.ModuleType("dlna")
_dlna_pkg.__path__ = [str(_root / "dlna")]
sys.modules.setdefault("dlna", _dlna_pkg)

_spec = importlib.util.spec_from_file_location(
    "dlna.sm6_queue",
    _root / "dlna" / "sm6_queue.py",
)
sm6_queue = importlib.util.module_from_spec(_spec)
sys.modules["dlna.sm6_queue"] = sm6_queue
assert _spec.loader is not None
_spec.loader.exec_module(sm6_queue)

build_get_repeat_body = sm6_queue.build_get_repeat_body
build_get_shuffle_body = sm6_queue.build_get_shuffle_body
build_set_repeat_body = sm6_queue.build_set_repeat_body
build_set_shuffle_body = sm6_queue.build_set_shuffle_body
parse_repeat_response = sm6_queue.parse_repeat_response
parse_shuffle_response = sm6_queue.parse_shuffle_response
SET_REPEAT_SOAP_ACTION = sm6_queue.SET_REPEAT_SOAP_ACTION
SET_SHUFFLE_SOAP_ACTION = sm6_queue.SET_SHUFFLE_SOAP_ACTION

_SAMPLE_SHUFFLE_ON = "<aShuffle>1</aShuffle>"
_SAMPLE_SHUFFLE_OFF = "<aShuffle>0</aShuffle>"
_SAMPLE_REPEAT_ON = "<aRepeat>1</aRepeat>"
_SAMPLE_REPEAT_OFF = "<aRepeat>0</aRepeat>"


def test_set_shuffle_body_on_off() -> None:
    on = build_set_shuffle_body(True)
    off = build_set_shuffle_body(False)
    assert "SetShuffle" in on
    assert "<aShuffle>1</aShuffle>" in on
    assert SET_SHUFFLE_SOAP_ACTION.endswith("#SetShuffle\"")
    assert "<aShuffle>0</aShuffle>" in off


def test_set_repeat_body_on_off() -> None:
    on = build_set_repeat_body(True)
    off = build_set_repeat_body(False)
    assert "SetRepeat" in on
    assert "<aRepeat>1</aRepeat>" in on
    assert SET_REPEAT_SOAP_ACTION.endswith("#SetRepeat\"")
    assert "<aRepeat>0</aRepeat>" in off


def test_get_shuffle_repeat_bodies() -> None:
    shuffle = build_get_shuffle_body()
    repeat = build_get_repeat_body()
    assert "Shuffle" in shuffle
    assert "Repeat" in repeat
    assert "PlaylistExtension" in shuffle
    assert "PlaylistExtension" in repeat


def test_parse_shuffle_response() -> None:
    assert parse_shuffle_response(_SAMPLE_SHUFFLE_ON) is True
    assert parse_shuffle_response(_SAMPLE_SHUFFLE_OFF) is False
    assert parse_shuffle_response("<aShuffle>true</aShuffle>") is True


def test_parse_repeat_response() -> None:
    assert parse_repeat_response(_SAMPLE_REPEAT_ON) is True
    assert parse_repeat_response(_SAMPLE_REPEAT_OFF) is False
    assert parse_repeat_response("<aRepeat>true</aRepeat>") is True
