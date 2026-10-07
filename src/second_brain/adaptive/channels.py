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


def stereo_frames(audio, timeout=14400):
    """Mean-square 300–3400 Hz level of each 50 ms frame: ([L...], [R...]). Streams the decode."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-af", "highpass=f=300,lowpass=f=3400",
           "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-"]
    errors = tempfile.TemporaryFile()  # a file, not a pipe: ffmpeg can never block on a full stderr
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    left, right = [], []
    try:
        while chunk := proc.stdout.read(FRAME * 4):
            samples = array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % 4])
            if not samples:
                break
            l, r = samples[0::2], samples[1::2]
            left.append(sum(map(operator.mul, l, l)) / len(l))
            right.append(sum(map(operator.mul, r, r)) / len(r))
        if proc.wait(timeout=timeout) != 0:
            errors.seek(0)
            raise RuntimeError("ffmpeg could not decode the capture: " + errors.read().decode(errors="replace")[-2000:])
    finally:
        proc.stdout.close()
        errors.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return left, right


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
