"""Which Mac meeting captures overlap an Omi recording.

The runtime owns this: it reads the receiver's capture markers and hands the pipeline only a
list of files to compare against, so the pipeline keeps no knowledge of the receiver layout.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import time

log = logging.getLogger("second_brain.meetings")

SEARCH_SECONDS = 10.0        # same tolerance the correlation search uses
OPEN_EXPIRY_SECONDS = 24 * 3600


@dataclass
class Overlaps:
    closed: list = field(default_factory=list)   # dicts: capture_id, start_ms, end_ms, audio
    open: list = field(default_factory=list)     # capture ids still being recorded or uploaded
    expired: list = field(default_factory=list)  # open too long to keep waiting for


def recording_span(metadata):
    """(start, end) epoch seconds of an Omi recording, or None when it cannot be placed in time."""
    if metadata.get("source") == "meeting":
        return None                              # a meeting is never deduped against meetings
    start = metadata.get("first_timestamp")
    if not isinstance(start, int) or start <= 0:
        return None                              # the device clock was never set
    seconds = metadata.get("audio_seconds")
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    return float(start), float(start) + float(seconds)


def overlapping(meetings_dir: Path, metadata: dict, now=None) -> Overlaps:
    span = recording_span(metadata)
    result = Overlaps()
    if span is None:
        return result
    start, end = span
    now = time.time() if now is None else now
    for marker in sorted((Path(meetings_dir) / ".captures").glob("*.json")):
        try:
            value = json.loads(marker.read_text(encoding="utf-8-sig"))
            state = value["state"]
            if state == "cancelled":
                continue
            capture_start = value["start_ms"] / 1000
            # An open capture has no end yet; treat it as still running.
            capture_end = value["end_ms"] / 1000 if state == "closed" else max(capture_start, now)
            if capture_end + SEARCH_SECONDS < start or capture_start - SEARCH_SECONDS > end:
                continue
            if state == "open":
                if now - float(value.get("updated", 0)) > OPEN_EXPIRY_SECONDS:
                    result.expired.append(value["capture_id"])
                else:
                    result.open.append(value["capture_id"])
                continue
            audio = Path(meetings_dir) / f"{value['capture_id']}.caf"
            if not audio.is_file():
                continue                          # committed audio deleted or moved: nothing to compare
            result.closed.append({"capture_id": value["capture_id"], "start_ms": value["start_ms"],
                                  "end_ms": value["end_ms"], "audio": str(audio)})
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            log.exception("Ignoring unreadable capture marker %s", marker.name)
    return result
