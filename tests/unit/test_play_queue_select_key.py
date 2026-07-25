"""PlayQueue.select_track_key — album parent key matching (Plexamp thematic queues)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.test_play_queue import FakePlexLib

_ROOT = Path(__file__).resolve().parents[2]


def _load_play_queue_class_isolated():
    """Load play_queue without evicting other plex.* modules from sys.modules."""
    spec = importlib.util.spec_from_file_location(
        "sonoplay_play_queue_select_key_test",
        _ROOT / "plex" / "play_queue.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.PlayQueue


@pytest.fixture
def select_track_queue():
    PlayQueue = _load_play_queue_class_isolated()
    queue = PlayQueue("/playQueues/1", FakePlexLib())
    queue.total_count = AsyncMock(return_value=2)
    return queue


@pytest.mark.asyncio
async def test_select_track_key_matches_parent_album_key(select_track_queue):
    play_queue = select_track_queue
    play_queue.get_info = AsyncMock(side_effect=lambda: play_queue.info)
    play_queue.set_selected_offset = AsyncMock()
    play_queue.start_offset = 0
    play_queue.info = SimpleNamespace(
        Metadata=[
            SimpleNamespace(
                key="/library/metadata/900001",
                ratingKey="900001",
                parentRatingKey="800001",
                title="First thematic track",
            ),
            SimpleNamespace(
                key="/library/metadata/900002",
                ratingKey="900002",
                parentRatingKey="800002",
                title="Second thematic track",
            ),
        ],
    )

    assert await play_queue.select_track_key("/library/metadata/800001") is True
    play_queue.set_selected_offset.assert_awaited_once_with(0)


@pytest.mark.asyncio
async def test_select_track_key_paginates_before_parent_album_match(select_track_queue):
    play_queue = select_track_queue
    play_queue.get_info = AsyncMock(side_effect=lambda: play_queue.info)
    play_queue.set_selected_offset = AsyncMock()
    play_queue.start_offset = 0
    play_queue.info = SimpleNamespace(
        Metadata=[
            SimpleNamespace(
                key="/library/metadata/900001",
                ratingKey="900001",
                parentRatingKey="800001",
                title="First thematic track",
            ),
        ],
    )
    play_queue._ensure_all_tracks_loaded = AsyncMock(
        side_effect=lambda: play_queue.info.Metadata.append(
            SimpleNamespace(
                key="/library/metadata/900002",
                ratingKey="900002",
                parentRatingKey="800002",
                title="Second thematic track",
            ),
        ),
    )

    assert await play_queue.select_track_key("/library/metadata/800002") is True
    play_queue._ensure_all_tracks_loaded.assert_awaited_once()
    play_queue.set_selected_offset.assert_awaited_once_with(1)
