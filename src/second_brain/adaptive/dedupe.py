"""Drop Omi transcript segments that duplicate a Mac meeting capture (spec section 3).

The Omi and the Mac record the same room with independent clocks, so each segment's envelope is
slid through a bounded window of the meeting's channels to find the clock offset. Only speech the
meeting already holds is dropped; anything else the Omi heard (someone in the room) is kept.
Nothing here deletes audio.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .channels import MIN_MATCH_FRAMES, match_envelope, rms_envelope

DROP_THRESHOLD = 0.6        # peak correlation that means "the meeting already has this"
ALIGNMENT_THRESHOLD = 0.3   # below this everywhere: the two recordings never lined up at all
SEARCH_SECONDS = 10.0       # clock offset tolerated between the Omi and the Mac


@dataclass
class Decisions:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    alignment_failed: bool = False


def _overlaps(seg_start, seg_end, meeting):
    return (seg_end >= meeting["start_ms"] / 1000 - SEARCH_SECONDS
            and seg_start <= meeting["end_ms"] / 1000 + SEARCH_SECONDS)


def decide(segments, omi_frames, omi_epoch, meetings):
    """`segments`: Omi ASR segments with `start`/`end` seconds into the recording.
    `omi_frames`: mean-square frames of the whole Omi recording. `omi_epoch`: epoch seconds of the
    recording's first sample. `meetings`: dicts with capture_id, start_ms, end_ms and the
    `left`/`right` mean-square frames of the capture."""
    result = Decisions()
    scores = []
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        envelope = rms_envelope(omi_frames, start, end)
        long_enough = len(envelope) >= MIN_MATCH_FRAMES
        overlapped = False
        best = (None, 0.0, None, None)
        for meeting in meetings:
            if not _overlaps(omi_epoch + start, omi_epoch + end, meeting):
                continue
            overlapped = True
            # Where this segment should sit inside the meeting's own timeline.
            into_meeting = omi_epoch + start - meeting["start_ms"] / 1000
            for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
                peak, offset = match_envelope(envelope, frames, into_meeting, SEARCH_SECONDS)
                if peak is not None and (best[0] is None or peak > best[0]):
                    best = (peak, offset, name, meeting["capture_id"])
        peak, offset, channel, capture = best
        if peak is None:
            # A segment that overlapped a meeting and still matched nothing is evidence the two
            # recordings do not line up; one too short to attempt is not.
            if overlapped and long_enough:
                scores.append(0.0)
            result.kept.append(dict(segment))
            continue
        scores.append(peak)
        if peak >= DROP_THRESHOLD:
            result.dropped.append({**segment, "deduped_by": capture, "channel": channel,
                                   "score": round(peak, 3), "offset_seconds": offset})
        else:
            result.kept.append({**segment, "dedupe_score": round(peak, 3)})
    # Nothing anywhere lined up: assume the clocks or the audio cannot be aligned and keep
    # everything rather than silently dropping a recording's worth of speech.
    if scores and max(scores) < ALIGNMENT_THRESHOLD:
        result.alignment_failed = True
        restored = [{k: v for k, v in d.items()
                     if k not in ("deduped_by", "channel", "score", "offset_seconds")}
                    for d in result.dropped]
        result.kept = sorted(result.kept + restored, key=lambda s: (s["start"], s["end"]))
        result.dropped = []
    return result
