"""Recording-wide speakers for MOSS transcripts (docs/superpowers/plans/2026-10-15-speaker-linking.md).

MOSS diarizes each 4-minute chunk on its own, so one person gets a new label in every chunk.
Nemotron-3-Diarization tracks up to 8 speakers across a whole recording. Each MOSS label joins
the Nemotron speaker it overlaps most, with two guards measured on AMI and the owner's 10-person
call: labels MOSS separated inside one chunk never merge (Nemotron's 8-speaker cap otherwise
forces it to), and a label whose best overlap covers less than MIN_SHARE of its speech keeps its
own identity."""

from __future__ import annotations

MIN_SHARE = 0.3


def parse_rttm(text):
    """(start, end, speaker) turns from RTTM SPEAKER lines; zero-length turns are dropped."""
    turns = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0] == "SPEAKER":
            start, length = float(fields[3]), float(fields[4])
            if length > 0:
                turns.append((start, start + length, fields[7]))
    return turns


def linkable(label):
    return label != "owner" and ":" in label and not label.endswith("S?")


def link_speakers(segments, turns, min_share=MIN_SHARE):
    """Map each chunk-local MOSS label to a recording-wide label `spk<n>`.

    Returns (mapping, report). Labels that are not linkable (the meeting owner, unknown `S?`
    turns) are absent from the mapping and must be left unchanged."""
    speech, overlap, chunk_of, first = {}, {}, {}, {}
    for segment in segments:
        label = str(segment["speaker"])
        if not linkable(label):
            continue
        start, end = float(segment["start"]), float(segment["end"])
        speech[label] = speech.get(label, 0.0) + max(0.0, end - start)
        chunk_of[label] = label.split(":")[0]
        first[label] = min(first.get(label, start), start)
        for turn_start, turn_end, speaker in turns:
            shared = min(turn_end, end) - max(turn_start, start)
            if shared > 0:
                votes = overlap.setdefault(label, {})
                votes[speaker] = votes.get(speaker, 0.0) + shared
    claims = {}
    for label, votes in overlap.items():
        speaker = max(sorted(votes), key=votes.get)          # sorted: ties resolve deterministically
        if speech[label] > 0 and votes[speaker] / speech[label] >= min_share:
            claims[label] = (speaker, votes[speaker])
    strongest = {}
    for label in sorted(claims):
        key = (chunk_of[label], claims[label][0])
        if key not in strongest or claims[label][1] > claims[strongest[key]][1]:
            strongest[key] = label
    linked = set(strongest.values())
    order = sorted(speech, key=lambda label: (first[label], label))
    names, mapping = {}, {}
    for label in order:
        if label in linked:
            speaker = claims[label][0]
            names.setdefault(speaker, f"spk{len(names) + 1}")
            mapping[label] = names[speaker]
    count = len(names)
    for label in order:
        if label not in mapping:
            count += 1
            mapping[label] = f"spk{count}"
    report = {"labels": len(speech), "linked": len(linked), "kept_apart": len(claims) - len(linked),
              "unlinked": len(speech) - len(claims), "speakers": len(set(mapping.values()))}
    return mapping, report
