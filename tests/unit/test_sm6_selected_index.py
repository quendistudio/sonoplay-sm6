"""Tests SM6 selected index mapping for Plex playQueue rebuild."""

import importlib.util
import sys
import types
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_dlna_pkg = types.ModuleType("dlna")
_dlna_pkg.__path__ = [str(_root / "dlna")]
sys.modules.setdefault("dlna", _dlna_pkg)
_sm6_queue_stub = types.ModuleType("dlna.sm6_queue")
_sm6_queue_stub.reciva_radio_invoke_url = lambda description_url: description_url
sys.modules["dlna.sm6_queue"] = _sm6_queue_stub

_spec = importlib.util.spec_from_file_location(
    "dlna.sm6_playlist",
    _root / "dlna" / "sm6_playlist.py",
)
sm6_playlist = importlib.util.module_from_spec(_spec)
sys.modules["dlna.sm6_playlist"] = sm6_playlist
assert _spec.loader is not None
_spec.loader.exec_module(sm6_playlist)

Sm6PlaylistEntry = sm6_playlist.Sm6PlaylistEntry
Sm6PlaylistState = sm6_playlist.Sm6PlaylistState


def _state(*, queue_index: int, current_id: int, titles: list[str]) -> Sm6PlaylistState:
    tracks = tuple(
        Sm6PlaylistEntry(track_id=i, title=title, artist="Artist", album="Album")
        for i, title in enumerate(titles)
    )
    return Sm6PlaylistState(
        length=len(tracks),
        current_track_id=current_id,
        media_queue_index=queue_index,
        tracks=tracks,
    )


class _PickIndex:
    def _sm6_pick_selected_index(self, playlist_state, resolved_queue_map, *, rating_keys_len: int) -> int:
        queue_index = playlist_state.media_queue_index
        if 0 <= queue_index < len(resolved_queue_map):
            mapped = resolved_queue_map[queue_index]
            if mapped is not None and 0 <= mapped < rating_keys_len:
                return mapped
        current_id = playlist_state.current_track_id
        for i, entry in enumerate(playlist_state.tracks):
            if entry.track_id == current_id and i < len(resolved_queue_map):
                mapped = resolved_queue_map[i]
                if mapped is not None and 0 <= mapped < rating_keys_len:
                    return mapped
        return 0


def test_selected_index_prefers_media_queue_index_over_track_id():
    titles = [
        "The Capitalist Blues",
        "Money Is King",
        "Lavi Vye Neg",
        "Penha",
        "Heavy as Lead",
    ]
    state = _state(queue_index=4, current_id=2, titles=titles)
    resolved = [0, 1, 2, 3, 4]
    picker = _PickIndex()
    assert picker._sm6_pick_selected_index(state, resolved, rating_keys_len=5) == 4


def test_selected_index_falls_back_to_current_track_id():
    titles = ["A", "B", "C"]
    state = _state(queue_index=-1, current_id=1, titles=titles)
    resolved = [0, 1, 2]
    picker = _PickIndex()
    assert picker._sm6_pick_selected_index(state, resolved, rating_keys_len=3) == 1
