#!/usr/bin/env python3
"""Hardware validation for SM6 InsertPlaylistTrack / DeletePlaylistTrack / MovePlaylistTrack.

Requires SM6_TEST_DESCRIPTION_URL (device description.xml URL) and a playing Plex track DIDL
from the existing queue or resolver.

Example:
  set SM6_TEST_DESCRIPTION_URL=http://<sm6-host>:8050/description.xml
  python scripts/test_sm6_playlist_edit_hardware.py --help
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dlna.sm6_control import Sm6Control


async def _run(args: argparse.Namespace) -> int:
    url = os.environ.get("SM6_TEST_DESCRIPTION_URL") or args.description_url
    if not url:
        print("Set SM6_TEST_DESCRIPTION_URL or pass --description-url", file=sys.stderr)
        return 2

    sm6 = Sm6Control(url)
    before = await sm6.read_playlist_state(fetch_tracks=True)
    print(
        f"BEFORE length={before.length} index={before.media_queue_index} "
        f"current_id={before.current_track_id} tail={len(before.tracks)}"
    )

    if args.scenario in ("insert", "all") and args.didl_file:
        didl = Path(args.didl_file).read_text(encoding="utf-8")
        insert_pos = before.media_queue_index + 1
        print(f"InsertPlaylistTrack position={insert_pos}")
        await sm6.insert_playlist_track(insert_position=insert_pos, didl=didl)
        after_insert = await sm6.read_playlist_state(fetch_tracks=True)
        print(f"AFTER INSERT length={after_insert.length}")
        if after_insert.length <= before.length:
            print("FAIL: playlist length did not increase after insert")
            return 1
        print("OK: InsertPlaylistTrack increased playlist length")

    if args.scenario in ("deleteall", "all"):
        state = await sm6.read_playlist_state(fetch_tracks=True)
        if state.length <= 1:
            print("SKIP deleteall: queue already empty tail")
        else:
            print("DeleteAll (clear upcoming queue, keep current playback)")
            await sm6.clear_queue()
            after = await sm6.read_playlist_state(fetch_tracks=True)
            print(f"AFTER DELETEALL length={after.length} index={after.media_queue_index}")
            if after.length > 1:
                print("FAIL: DeleteAll did not clear upcoming tracks")
                return 1
            print("OK: DeleteAll cleared upcoming queue")

    if args.scenario in ("delete", "all"):
        state = await sm6.read_playlist_state(fetch_tracks=True)
        tail = state.tracks[state.media_queue_index + 1 :]
        if not tail:
            print("SKIP delete: no tail track to remove")
        else:
            target = tail[-1]
            print(f"DeletePlaylistTrack id={target.track_id} title={target.title!r}")
            await sm6.delete_playlist_track(playlist_track_id=target.track_id)
            after_delete = await sm6.read_playlist_state(fetch_tracks=True)
            print(f"AFTER DELETE length={after_delete.length}")
            if after_delete.length >= state.length:
                print("FAIL: playlist length did not decrease after delete")
                return 1
            print("OK: DeletePlaylistTrack decreased playlist length")

    if args.scenario in ("move", "all"):
        state = await sm6.read_playlist_state(fetch_tracks=True)
        tail = state.tracks[state.media_queue_index + 1 :]
        if len(tail) < 2:
            print("SKIP move: need at least two tail tracks")
        else:
            from_idx = state.media_queue_index + 2
            to_idx = state.media_queue_index + 1
            print(f"MovePlaylistTrack from={from_idx} to={to_idx}")
            await sm6.move_playlist_track(from_index=from_idx, to_index=to_idx)
            print("OK: MovePlaylistTrack SOAP accepted")

    final = await sm6.read_playlist_state(fetch_tracks=True)
    print(
        f"FINAL length={final.length} index={final.media_queue_index} "
        f"tracks={[e.title for e in final.tracks]}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate SM6 playlist edit SOAP actions")
    parser.add_argument("--description-url", default="")
    parser.add_argument(
        "--scenario",
        choices=("insert", "delete", "deleteall", "move", "all"),
        default="all",
    )
    parser.add_argument(
        "--didl-file",
        help="DIDL-Lite XML file for InsertPlaylistTrack test",
        default="",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
