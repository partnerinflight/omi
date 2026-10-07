"""Stereo meeting captures (L = owner's mic, R = meeting app): channel selection and
speech-band levels. Shared by the pipeline (owner attribution) and speaker clips."""

from __future__ import annotations
import math
import operator
import subprocess
import tempfile
from array import array

CHANNEL_INDEX = {"L": 0, "R": 1}
FRAME_SECONDS = 0.05
RATE = 8000
FRAME = int(RATE * FRAME_SECONDS)  # samples per channel per frame
OWNER_MARGIN_DB = 6.0


def downmix(channel):
    """ffmpeg arguments selecting one channel of a stereo capture, or the usual mono mix."""
    if channel is None:
        return ["-ac", "1"]
    return ["-af", f"pan=mono|c0=c{CHANNEL_INDEX[channel]}"]


def _decode_frames(audio, channel_count, timeout):
    """Mean-square 300-3400 Hz level of each 50 ms frame, one list per channel. Streams the decode."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-af", "highpass=f=300,lowpass=f=3400",
           "-ac", str(channel_count), "-ar", str(RATE), "-f", "s16le", "-"]
    errors = tempfile.TemporaryFile()  # a file, not a pipe: ffmpeg can never block on a full stderr
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    out = [[] for _ in range(channel_count)]
    width = 2 * channel_count
    try:
        while chunk := proc.stdout.read(FRAME * width):
            samples = array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % width])
            if not samples:
                break
            for index, values in enumerate(out):
                one = samples[index::channel_count]
                values.append(sum(map(operator.mul, one, one)) / len(one))
        if proc.wait(timeout=timeout) != 0:
            errors.seek(0)
            raise RuntimeError("ffmpeg could not decode the capture: " + errors.read().decode(errors="replace")[-2000:])
    finally:
        proc.stdout.close()
        errors.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return out


def stereo_frames(audio, timeout=14400):
    """([L...], [R...]) for a stereo meeting capture."""
    left, right = _decode_frames(audio, 2, timeout)
    return left, right


def mono_frames(audio, timeout=14400):
    """[...] for a mono Omi recording (any multi-channel input is mixed down)."""
    return _decode_frames(audio, 1, timeout)[0]


def rms_envelope(frames, start, end):
    """RMS values of the 50 ms frames covering [start, end) seconds."""
    first = max(0, int(start / FRAME_SECONDS))
    last = max(first + 1, math.ceil(end / FRAME_SECONDS))
    return [math.sqrt(value) for value in frames[first:last]]


MIN_MATCH_FRAMES = 16   # 0.8 s: fewer frames cannot identify a passage reliably


def _pearson(x, y):
    """None when either side is flat: a constant envelope carries no timing information."""
    n = len(x)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    dx = [v - mean_x for v in x]
    dy = [v - mean_y for v in y]
    norm = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if norm <= 1e-12:
        return None
    return sum(map(operator.mul, dx, dy)) / norm


def match_envelope(envelope, frames, at, max_shift):
    """Best correlation of `envelope` against equally long slices of `frames` (mean squares),
    centred on `at` seconds and shifted by up to +-max_shift seconds. Returns (peak, offset
    seconds) or (None, 0.0) when no comparable window exists.

    Only full-length windows are compared. A partial overlap of two frames correlates at exactly
    1.0 regardless of content, which would drop real speech near a meeting's edge.
    """
    length = len(envelope)
    if length < MIN_MATCH_FRAMES:
        return None, 0.0
    base = int(round(at / FRAME_SECONDS))
    span = int(round(max_shift / FRAME_SECONDS))
    best, best_shift = None, 0
    for shift in range(-span, span + 1):
        start = base + shift
        if start < 0 or start + length > len(frames):
            continue
        score = _pearson(envelope, [math.sqrt(v) for v in frames[start : start + length]])
        if score is not None and (best is None or score > best):
            best, best_shift = score, shift
    if best is None:
        return None, 0.0
    return best, round(best_shift * FRAME_SECONDS, 3)


def span_level_db(frames, start, end):
    first = max(0, int(start / FRAME_SECONDS))
    last = max(first + 1, math.ceil(end / FRAME_SECONDS))
    window = frames[first:last]
    mean = sum(window) / len(window) if window else 0.0
    return 10 * math.log10(mean) if mean > 0 else -math.inf


def mic_dominates(left, right, start, end, margin_db=OWNER_MARGIN_DB):
    """Owner attribution: the mic is at least margin_db louder than the remote channel, so remote
    audio bleeding into the mic is never credited to the owner."""
    mic = span_level_db(left, start, end)
    return mic > -math.inf and mic - span_level_db(right, start, end) >= margin_db
