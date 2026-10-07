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
ALIGNMENT_THRESHOLD = 0.7    # block peak needed to trust an offset (unrelated real pairs: max 0.52)
SEARCH_SECONDS = 10.0        # clock offset tolerated between the Omi and the Mac
SEGMENT_TOLERANCE = 0.1      # per-segment wiggle around its block's offset
MIN_SEGMENT_SECONDS = 2.0    # shorter segments are never dropped (chance matches; a duplicate is cheaper)
BLOCK_SECONDS = 60.0         # alignment block; each block gets its own offset, so drift is tracked
MIN_BLOCK_SECONDS = 30.0     # a final partial block shorter than this is not used


@dataclass
class Decisions:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    alignment_failed: bool = False
    alignments: list = field(default_factory=list)   # one per meeting, with its aligned blocks

def align(omi_frames, omi_epoch, meeting):
    """Clock offsets for one meeting, one per 60 s block of overlap.

    Each block is searched +-SEARCH_SECONDS, so blocks start SEARCH_SECONDS inside the overlap:
    a full-length comparison window must exist for every shift, including a negative offset when
    the Omi was already recording as the meeting began. Blocks below ALIGNMENT_THRESHOLD are
    unusable (the owner silent, or only room speech); a meeting with no usable block fails.
    """
    m_start, m_end = meeting["start_ms"] / 1000, meeting["end_ms"] / 1000
    o_end = omi_epoch + len(omi_frames) * FRAME_SECONDS
    lo = max(omi_epoch, m_start) + SEARCH_SECONDS
    hi = min(o_end, m_end) - SEARCH_SECONDS
    result = {"capture_id": meeting["capture_id"], "blocks": []}
    if hi - lo < MIN_BLOCK_SECONDS:
        return {**result, "status": "too little overlap"}
    block = lo
    while hi - block >= MIN_BLOCK_SECONDS:
        end = min(block + BLOCK_SECONDS, hi)
        if hi - end < MIN_BLOCK_SECONDS:
            end = hi                          # fold a short remainder into the last block
        envelope = rms_envelope(omi_frames, block - omi_epoch, end - omi_epoch)
        best = (None, 0.0, None)
        for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
            peak, offset = match_envelope(envelope, frames, block - m_start, SEARCH_SECONDS)
            if peak is not None and (best[0] is None or peak > best[0]):
                best = (peak, offset, name)
        if best[0] is not None and best[0] >= ALIGNMENT_THRESHOLD:
            result["blocks"].append({"start": round(block - m_start, 3), "end": round(end - m_start, 3),
                                     "score": round(best[0], 3), "offset_seconds": best[1],
                                     "channel": best[2]})
        result.setdefault("best_score", None)
        if best[0] is not None and (result["best_score"] is None or best[0] > result["best_score"]):
            result["best_score"] = round(best[0], 3)
        block = end
    result["status"] = "aligned" if result["blocks"] else "failed"
    return result


def _offset_at(alignment, meeting_seconds):
    """The offset of the aligned block nearest to a point in the meeting's timeline."""
    return min(alignment["blocks"],
               key=lambda b: abs((b["start"] + b["end"]) / 2 - meeting_seconds))["offset_seconds"]


def decide(segments, omi_frames, omi_epoch, meetings):
    result = Decisions()
    aligned = []
    for meeting in meetings:
        a = align(omi_frames, omi_epoch, meeting)
        result.alignments.append(a)
        if a["status"] == "aligned":
            aligned.append((meeting, a))
    attempted = [a for a in result.alignments if a["status"] != "too little overlap"]
    result.alignment_failed = bool(attempted) and not aligned
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        best = (None, None, None, None)
        if end - start >= MIN_SEGMENT_SECONDS:
            envelope = rms_envelope(omi_frames, start, end)
            for meeting, alignment in aligned:
                expected = omi_epoch + start - meeting["start_ms"] / 1000
                offset = _offset_at(alignment, expected)
                into = expected + offset
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
