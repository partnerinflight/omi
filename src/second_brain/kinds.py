"""Job kinds: `omi` (an ordinary Omi recording), `meeting` (a Mac meeting capture) or `lecture`
(an Omi recording made in lecture mode). Discovery stores the kind in each job's metadata;
jobs queued before kinds were stored derive it from their receipt fields."""

from __future__ import annotations

KINDS = ("omi", "meeting", "lecture")


def _from_receipt(metadata: dict) -> str:
    if metadata.get("source") == "meeting":
        return "meeting"
    # The receiver writes "mode" for Omi recordings; a missing or unknown mode is normal.
    return "lecture" if metadata.get("mode") == "lecture" else "omi"


def job_kind(metadata: dict) -> str:
    """The job's kind: the stored one, else derived the way discovery derives it."""
    kind = metadata.get("kind")
    return kind if kind in KINDS else _from_receipt(metadata)


def with_kind(metadata: dict) -> dict:
    """Receipt metadata with its kind stored. A kind already present in a sidecar is never trusted."""
    return {**metadata, "kind": _from_receipt(metadata)}
