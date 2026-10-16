"""Deterministic stand-in for `nemo-speech diarize INPUT --model M --format rttm --output OUT`,
never used in production configuration.

Usage as a speaker_linking_command prefix: [python, fake_nemo_speech.py, "--block", "<seconds>"].
Each block's first half is speaker_a and second half speaker_b, matching tests/fixtures/fake_moss.py,
which splits every MOSS chunk into an S1 half and an S2 half. Records what it was asked to read in
OUT.input.json so tests can check the audio it received."""

import json
import sys
import wave

args = sys.argv[1:]
block = float(args[args.index("--block") + 1]) if "--block" in args else 1e9
command = args[args.index("diarize"):]
audio, out = command[1], command[command.index("--output") + 1]
if command[command.index("--format") + 1] != "rttm" or "--model" not in command:
    raise SystemExit(2)
with wave.open(audio) as source:
    duration = source.getnframes() / source.getframerate()
    channels, rate = source.getnchannels(), source.getframerate()
lines, start = [], 0.0
while start < duration:
    middle, end = min(duration, start + block / 2), min(duration, start + block)
    lines.append(f"SPEAKER rec 1 {start:.3f} {middle - start:.3f} <NA> <NA> speaker_a <NA> <NA>")
    if end > middle:
        lines.append(f"SPEAKER rec 1 {middle:.3f} {end - middle:.3f} <NA> <NA> speaker_b <NA> <NA>")
    start = end
open(out, "w").write("\n".join(lines) + "\n")
json.dump({"audio": audio, "channels": channels, "rate": rate, "duration": duration}, open(out + ".input.json", "w"))
