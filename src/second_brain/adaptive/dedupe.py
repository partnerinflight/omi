"""Drop Omi transcript segments that duplicate a Mac meeting capture (spec section 3).

Two stages, because the Omi and the Mac clocks differ by one roughly constant offset:
1. `align`: estimate that offset once per meeting from the whole overlapping stretch (at least
   30 s). A long envelope gives an unambiguous peak; below ALIGNMENT_THRESHOLD the recordings do
   not line up and nothing is dropped against that meeting.
2. Each segment of at least 2 s is scored only within +-0.1 s of that offset.

Searching +-10 s per segment instead (the spec's original rule) was measured on real Omi speech
to drop 93% of unrelated 0.8 s segments and 9% of 8 s ones by chance: the best of ~800 shifted
comparisons routinely exceeds 0.6. That would delete exactly the in-room speech this keeps.
Nothing here deletes audio.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .channels import FRAME_SECONDS, match_envelope, rms_envelope

DROP_THRESHOLD = 0.6         # per-segment correlation that means "the meeting already has this"
ALIGNMENT_THRESHOLD = 0.5    # whole-overlap peak needed to trust an offset (unrelated real pairs: max 0.35)
SEARCH_SECONDS = 10.0        # clock offset tolerated between the Omi and the Mac
SEGMENT_TOLERANCE = 0.1      # per-segment wiggle around the estimated offset
MIN_SEGMENT_SECONDS = 2.0    # shorter segments are never dropped (chance matches; a duplicate is cheaper)
MIN_ALIGN_SECONDS = 30.0     # overlap needed to estimate the offset at all
MAX_ALIGN_SECONDS = 600.0    # cost cap; clock drift over an hour (~20 ppm, ~0.07 s) fits SEGMENT_TOLERANCE

@dataclass
class Decisions:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    alignment_failed: bool = False
    alignments: list = field(default_factory=list)

def align(omi_frames, omi_epoch, meeting):
    """One clock offset per meeting from the longest overlapping stretch, or None."""
    m_start, m_end = meeting["start_ms"] / 1000, meeting["end_ms"] / 1000
    o_end = omi_epoch + len(omi_frames) * FRAME_SECONDS
    lo, hi = max(omi_epoch, m_start), min(o_end, m_end)
    if hi - lo < MIN_ALIGN_SECONDS:
        return {"capture_id": meeting["capture_id"], "status": "too little overlap"}
    hi = min(hi, lo + MAX_ALIGN_SECONDS)
    envelope = rms_envelope(omi_frames, lo - omi_epoch, hi - omi_epoch)
    best = (None, 0.0, None)
    for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
        peak, offset = match_envelope(envelope, frames, lo - m_start, SEARCH_SECONDS)
        if peak is not None and (best[0] is None or peak > best[0]):
            best = (peak, offset, name)
    peak, offset, channel = best
    result = {"capture_id": meeting["capture_id"], "score": None if peak is None else round(peak, 3)}
    if peak is None or peak < ALIGNMENT_THRESHOLD:
        return {**result, "status": "failed"}
    return {**result, "status": "aligned", "offset_seconds": offset, "channel": channel}

def decide(segments, omi_frames, omi_epoch, meetings):
    result = Decisions()
    aligned = []
    for meeting in meetings:
        a = align(omi_frames, omi_epoch, meeting)
        result.alignments.append(a)
        if a["status"] == "aligned":
            aligned.append((meeting, a["offset_seconds"]))
    attempted = [a for a in result.alignments if a["status"] != "too little overlap"]
    result.alignment_failed = bool(attempted) and not aligned
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        best = (None, None, None, None)
        if end - start >= MIN_SEGMENT_SECONDS:
            envelope = rms_envelope(omi_frames, start, end)
            for meeting, offset in aligned:
                into = omi_epoch + start - meeting["start_ms"] / 1000 + offset
                for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
                    peak, shift = match_envelope(envelope, frames, into, SEGMENT_TOLERANCE)
                    if peak is not None and (best[0] is None or peak > best[0]):
                        best = (peak, round(offset + shift, 3), name, meeting["capture_id"])
        peak, offset, channel, capture = best
        if peak is not None and peak >= DROP_THRESHOLD:
            result.dropped.append({**segment, "deduped_by": capture, "deduped_channel": channel,
                                   "score": round(peak, 3), "offset_seconds": offset})
        elif peak is not None:
            result.kept.append({**segment, "dedupe_score": round(peak, 3)})
        else:
            result.kept.append(dict(segment))
    return result
