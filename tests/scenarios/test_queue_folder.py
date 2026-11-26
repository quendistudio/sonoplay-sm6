"""QueueFolder SOAP UX: no resend on timeout, serialize, BAD_NAVIGATOR retry."""
from __future__ import annotations

import asyncio

import pytest

from dlna.sm6_control import Sm6Control, _SOAP_RETRIES


@pytest.mark.asyncio
async def test_queue_folder_not_resent_on_timeout(fake_sm6):
    fake_sm6.timeout_labels.add("QueueFolder")
    sm6 = Sm6Control("http://192.168.50.99:8050/description.xml")

    with pytest.raises(RuntimeError, match="QueueFolder unavailable"):
        await sm6.queue_folder(
            "<DIDL/>",
            action="PLAY_NOW",
            server_udn="uuid:plex",
            navigator_id="nav",
        )
    # Non-idempotent: one attempt only (retry_on_timeout=False).
    assert fake_sm6._call_count.get("QueueFolder", 0) == 1


@pytest.mark.asyncio
async def test_idempotent_read_is_retried_on_timeout(fake_sm6):
    fake_sm6.timeout_labels.add("GetVolume")
    sm6 = Sm6Control("http://192.168.50.99:8050/description.xml")

    with pytest.raises(RuntimeError, match="GetVolume unavailable"):
        await sm6._post("http://x", "<body/>", '"urn:x#GetVolume"', label="GetVolume")
    assert fake_sm6._call_count.get("GetVolume", 0) == _SOAP_RETRIES


@pytest.mark.asyncio
async def test_queue_folder_survives_caller_cancel_and_blocks_next(fake_sm6):
    fake_sm6.hang_labels.add("QueueFolder")
    sm6 = Sm6Control("http://192.168.50.99:8050/description.xml")

    first = asyncio.create_task(
        sm6.queue_folder("<a/>", action="PLAY_NOW", server_udn="uuid:plex", navigator_id="nav")
    )
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        sm6.queue_folder("<b/>", action="APPEND", server_udn="uuid:plex", navigator_id="nav")
    )
    await asyncio.sleep(0.01)
    assert fake_sm6.labels() == []

    fake_sm6.release_hang()
    await second
    assert fake_sm6.labels().count("QueueFolder") == 2


@pytest.mark.asyncio
async def test_queue_folder_reregisters_on_bad_navigator(fake_sm6, monkeypatch):
    sm6 = Sm6Control("http://192.168.50.99:8050/description.xml")
    original_post = fake_sm6.post
    posts: list[str] = []

    def flaky_post(url, data=None, headers=None, timeout=None):
        body = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else (data or "")
        label = fake_sm6._label_from_request(headers, body)
        posts.append(label)
        if label == "QueueFolder" and posts.count("QueueFolder") == 1:

            class _Bad:
                status = 200

                async def text(self):
                    return "<Result>BAD_NAVIGATOR</Result>"

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Bad()
        return original_post(url, data=data, headers=headers, timeout=timeout)

    async def names():
        return ["Plex"]

    async def register(name):
        return "fresh-nav"

    async def query(name):
        return True, "fresh-nav"

    async def anon():
        return None

    monkeypatch.setattr(fake_sm6, "post", flaky_post)
    sm6._navigator_name_candidates = names  # type: ignore[method-assign]
    sm6._register_named_navigator = register  # type: ignore[method-assign]
    sm6._query_registered_navigator = query  # type: ignore[method-assign]
    sm6._register_anonymous_navigator = anon  # type: ignore[method-assign]
    sm6._remember_navigator_id("stale-nav")

    await sm6.queue_folder("<DIDL/>", action="PLAY_NOW", server_udn="uuid:plex")
    assert posts.count("QueueFolder") == 2
    assert sm6._cached_plex_navigator_id == "fresh-nav"


@pytest.mark.asyncio
async def test_delete_all_waits_for_in_flight_queue_folder(fake_sm6):
    fake_sm6.hang_labels.add("QueueFolder")
    sm6 = Sm6Control("http://192.168.50.99:8050/description.xml")

    append = asyncio.create_task(
        sm6.queue_folder("<a/>", action="APPEND", server_udn="uuid:plex", navigator_id="nav")
    )
    await asyncio.sleep(0)
    append.cancel()
    clear = asyncio.create_task(sm6.clear_queue())
    await asyncio.sleep(0.01)
    assert "DeleteAll" not in fake_sm6.labels()

    fake_sm6.release_hang()
    await clear
    labels = fake_sm6.labels()
    assert labels.index("QueueFolder") < labels.index("DeleteAll")
