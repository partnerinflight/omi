"""Deterministic ASR contract fixture: one segment per half of the chunk, named by position.
Never used in production configuration."""

import json
import sys
import wave

if sys.argv[1] != "transcribe":
    raise SystemExit(2)
with wave.open(sys.argv[3]) as audio:
    duration = audio.getnframes() / audio.getframerate()
print(json.dumps({"segments": [
    {"start": 0, "end": duration / 2, "speaker": "S1", "text": "first half"},
    {"start": duration / 2, "end": duration, "speaker": "S2", "text": "second half"},
]}))
