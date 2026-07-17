"""Tests Plex enqueue → SM6 QueueFolder action mapping."""

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

sm6_action_for_enqueue = sm6_queue.sm6_action_for_enqueue


def test_enqueue_mapping() -> None:
    assert sm6_action_for_enqueue("replace") == "REPLACE"
    assert sm6_action_for_enqueue("add") == "APPEND"
    assert sm6_action_for_enqueue("next") == "PLAY_NEXT"
    assert sm6_action_for_enqueue("play") == "PLAY_NOW"
