"""Deterministic ASR contract fixture for meeting tests, never used in production configuration.

Each half of the chunk becomes one segment if it is not silent. The text names the tone, so a test
can tell which channel's audio reached the transcriber: below 660 Hz is the owner, above is remote."""

import json
import sys
import wave
from array import array

OWNER_TEXT = "We decided the project launch moves to next Friday at four PM."
REMOTE_TEXT = "I will send the final pricing proposal to the client tomorrow."


def main():
    if sys.argv[1] != "transcribe":
        raise SystemExit(2)
    with wave.open(sys.argv[3]) as audio:
        rate = audio.getframerate()
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
    half = len(samples) // 2
    segments = []
    for index, part in enumerate((samples[:half], samples[half:])):
        if not part:
            continue
        rms = (sum(x * x for x in part) / len(part)) ** 0.5
        if rms < 300:
            continue
        crossings = sum(1 for a, b in zip(part, part[1:]) if (a < 0) != (b < 0))
        hz = crossings / 2 / (len(part) / rate)
        start = (0 if index == 0 else half) / rate
        end = (half if index == 0 else len(samples)) / rate
        segments.append({"start": start, "end": end, "speaker": "S1",
                         "text": OWNER_TEXT if hz < 660 else REMOTE_TEXT})
    print(json.dumps({"segments": segments}))


if __name__ == "__main__":
    main()
