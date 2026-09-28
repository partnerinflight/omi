"""Deterministic ASR contract fixture, never used in production configuration."""

import json
import sys
import wave

if sys.argv[1] != "transcribe":
    raise SystemExit(2)
with wave.open(sys.argv[3]) as audio:
    duration = audio.getnframes() / audio.getframerate()
print(
    json.dumps(
        {
            "segments": [
                {
                    "start": 0,
                    "end": duration / 2,
                    "speaker": "S1",
                    "text": "We decided the project launch will be next Friday at four PM. Remember to schedule the client meeting and send the final pricing proposal.",
                },
                {
                    "start": duration / 2,
                    "end": duration,
                    "speaker": "S2",
                    "text": "I will send the project proposal tomorrow and follow up with the client before the launch deadline.",
                },
            ]
        }
    )
)
