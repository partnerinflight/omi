"""Replay pulled Omi recordings through the production VOX filter to tune its threshold.

    python -m omi_local.vox_replay ~/omi-recordings [--thresholds 100,150,200,250,300,400]

Compiles omi/src/lib/core/vox_filter.c from this checkout, decodes each .opus with ffmpeg
(16 kHz mono), and prints per file how long the microphone would stay awake under the
current rule (raw average amplitude >= 400 resets a 30 s hold) and under the new
speech-band rule at each candidate threshold. Development tool only.
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

FILTER_SOURCE = Path(__file__).resolve().parents[3] / "omi" / "src" / "lib" / "core" / "vox_filter.c"
BLOCK = 1600          # 100 ms at 16 kHz, as mic.c delivers
SKIP_BLOCKS = 5       # the firmware discards the first 500 ms after every mic start
HOLD_S = 30.0
OLD_THRESHOLD = 400


class _Biquad(ctypes.Structure):
    _fields_ = [("z1", ctypes.c_float), ("z2", ctypes.c_float)]


class _Filter(ctypes.Structure):
    _fields_ = [("hp", _Biquad), ("lp", _Biquad), ("history", ctypes.c_uint8)]


def load_filter():
    out = Path(tempfile.mkdtemp()) / ("libvox" + (".dylib" if sys.platform == "darwin" else ".so"))
    subprocess.run(["cc", "-O2", "-shared", "-fPIC", str(FILTER_SOURCE), "-o", str(out)], check=True)
    lib = ctypes.CDLL(str(out))
    lib.vox_block_level.restype = ctypes.c_uint32
    lib.vox_block_level.argtypes = [ctypes.POINTER(_Filter), ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]
    lib.vox_voice_detected.restype = ctypes.c_bool
    lib.vox_voice_detected.argtypes = [ctypes.POINTER(_Filter), ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint, ctypes.c_uint]
    return lib


def block_levels(lib, pcm: array) -> list[int]:
    state = _Filter()
    buffer = (ctypes.c_int16 * BLOCK)()
    levels = []
    for start in range(0, len(pcm) - BLOCK + 1, BLOCK):
        buffer[:] = pcm[start:start + BLOCK]
        levels.append(lib.vox_block_level(ctypes.byref(state), buffer, BLOCK))
    return levels


def reset_times(lib, levels: list[int], threshold: int, sustain: int = 3, window: int = 5) -> list[float]:
    state = _Filter()
    return [round((i + 1) * 0.1, 1) for i, level in enumerate(levels)
            if lib.vox_voice_detected(ctypes.byref(state), level, threshold, sustain, window)]


def raw_reset_times(pcm: array, threshold: int = OLD_THRESHOLD) -> list[float]:
    times = []
    for i, start in enumerate(range(0, len(pcm) - BLOCK + 1, BLOCK)):
        if sum(abs(s) for s in pcm[start:start + BLOCK]) / BLOCK >= threshold:
            times.append(round((i + 1) * 0.1, 1))
    return times


def awake_seconds(resets: list[float], duration: float, hold: float = HOLD_S) -> float:
    """Seconds awake if the file starts at a wake and each reset extends the hold."""
    total, awake_until, start = 0.0, 0.0, 0.0
    for t in [0.0] + sorted(resets):
        if t > awake_until:
            total += awake_until - start
            start = t
        awake_until = max(awake_until, t + hold)
    total += min(awake_until, duration) - start
    return round(min(total, duration), 1)


def decode(path: Path) -> array:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         check=True, capture_output=True).stdout
    pcm = array("h")
    pcm.frombytes(raw)
    return pcm[SKIP_BLOCKS * BLOCK:]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--thresholds", default="100,150,200,250,300,400")
    args = parser.parse_args(argv)
    thresholds = [int(t) for t in args.thresholds.split(",")]
    lib = load_filter()
    print("file\tduration_s\told_awake_s\t" + "\t".join(f"new{t}_s" for t in thresholds))
    for path in sorted(args.directory.glob("*.opus")):
        pcm = decode(path)
        duration = round(len(pcm) / 16000, 1)
        if duration < 1:
            continue
        levels = block_levels(lib, pcm)
        row = [path.name, str(duration), str(awake_seconds(raw_reset_times(pcm), duration))]
        row += [str(awake_seconds(reset_times(lib, levels, t), duration)) for t in thresholds]
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
