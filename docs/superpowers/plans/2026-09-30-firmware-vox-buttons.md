# Speech-Selective VOX and Button Gestures (Firmware .15) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop keystrokes from keeping the Omi microphone awake, and move pause/resume, power-off and Wi-Fi setup to hold-then-release gestures confirmed by 1/2/3 vibrations, shipped as firmware `3.0.22-localwifi.15`.

**Architecture:** A new pure-C `vox_filter` module band-limits each 100 ms PCM block (250 Hz high-pass → 3.4 kHz low-pass) and applies a 3-of-5-blocks sustain rule; `mic.c` uses it for the silence timer. Button hold policy stays a pure header (`button_hold.h`) with release actions and vibration levels; `button.c` plays the levels through a new non-blocking `play_haptic_pulses()`. A host replay tool runs the production filter over pulled recordings to choose the threshold before flashing.

**Tech Stack:** C (Zephyr/NCS 2.9.0, nRF5340 with FPU), native C tests driven by Python `unittest`, ffmpeg for decoding recordings.

Spec: `docs/superpowers/specs/2026-09-30-vox-buttons-settings-design.md` §1–2. This is plan A; the Mac settings window is plan B.

---

## Conventions

- Firmware sources: `omi/firmware/omi` (called **FW**). Receiver/test package: `omi/firmware/scripts/omi-local` (called **RX**).
- Native tests run from RX: `.venv/bin/python -m unittest <module> -v` (venv exists; `pip install -e .` if not). Each test compiles production C with the host `cc`.
- Full suite from the repo root: `omi/firmware/scripts/omi-local/.venv/bin/python scripts/test.py`.
- Format changed C files with `clang-format -i <files>` (installed via Homebrew).
- Work on a branch `feature/fw-vox-buttons` created from `feature/wifi-local-upload`.
- Commit messages start with `firmware:`.
- Do **not** flash the device. Flashing is a separate step the owner requests.

## File structure

| File | Responsibility |
|---|---|
| `FW/src/lib/core/vox_filter.h`, `vox_filter.c` (create) | Speech-band level and sustain rule; no Zephyr dependencies |
| `FW/CMakeLists.txt` (modify) | Compile `vox_filter.c` |
| `FW/src/mic.c` (modify) | Silence timer uses `vox_filter`; motor-settle wait before manual resume |
| `FW/Kconfig`, `FW/omi.conf`, `FW/overlay-wifi-upload.conf` (modify) | New VAD options, tuned threshold, version `.15` |
| `FW/src/lib/core/button_hold.h` (modify) | Release actions and vibration levels |
| `FW/src/lib/core/button.c` (modify) | Gesture handling |
| `FW/src/haptic.c`, `FW/src/lib/core/haptic.h` (modify) | `play_haptic_pulses(count)` |
| `FW/src/portal.html` (modify) | Setup help text |
| `RX/omi_local/vox_replay.py` (create) | Threshold tuning on pulled recordings |
| `RX/tests/test_vox_filter_c.py`, `test_vox_replay.py` (create) | Filter and replay tests |
| `RX/tests/test_vox_c.py`, `test_wifi_config_c.py`, `test_manual_recording_c.py` (modify) | Updated production-code tests |
| `omi/firmware/AGENTS.md`, `PROVISIONING.md`, `VALIDATION_MAC.md`, root `SYSTEM.md` (modify) | Docs |

---

### Task 1: `vox_filter` module

**Files:**
- Create: `FW/src/lib/core/vox_filter.h`, `FW/src/lib/core/vox_filter.c`
- Modify: `FW/CMakeLists.txt`
- Test: `RX/tests/test_vox_filter_c.py`

- [ ] **Step 1: Write the failing test**

`RX/tests/test_vox_filter_c.py`:

```python
"""Execute the production speech-band VOX filter natively."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[3] / 'omi' / 'src' / 'lib' / 'core'


@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class VoxFilterTests(unittest.TestCase):
    def run_c(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include "vox_filter.h"
#define PI 3.14159265358979
static int16_t block[1600];
static void tone(double hz, double amp) {
    /* 1600 samples at 16 kHz hold whole cycles of 100 Hz and 1 kHz: phase stays continuous. */
    for (int i = 0; i < 1600; i++) block[i] = (int16_t) (amp * sin(2 * PI * hz * i / 16000.0));
}
static uint32_t settle(struct vox_filter *f) {
    uint32_t level = 0;
    for (int k = 0; k < 4; k++) level = vox_block_level(f, block, 1600);
    return level;
}
int main(void) {
''' + body + '''
    return 0;
}
''')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(CORE), str(root / 'test.c'),
                            str(CORE / 'vox_filter.c'), '-o', str(root / 'test'), '-lm'], check=True)
            return subprocess.check_output([str(root / 'test')]).decode()

    def test_speech_band_passes_and_low_frequencies_are_attenuated(self):
        out = self.run_c(r'''
    struct vox_filter f; vox_filter_reset(&f);
    tone(1000, 2000); uint32_t speech = settle(&f);
    vox_filter_reset(&f);
    tone(100, 2000); uint32_t hum = settle(&f);
    printf("%u %u", speech, hum);
''')
        speech, hum = map(int, out.split())
        self.assertGreater(speech, 1150)   # 2000 * 2/pi = 1273 before filtering; ~0.99 gain at 1 kHz
        self.assertLess(hum, 260)          # ~0.16 gain at 100 Hz

    def test_empty_block_is_silent(self):
        self.assertEqual(self.run_c(r'''
    struct vox_filter f; vox_filter_reset(&f);
    printf("%u", vox_block_level(&f, block, 0));
'''), '0')

    def test_sustain_rule_needs_three_of_last_five(self):
        self.run_c(r'''
    struct vox_filter f; vox_filter_reset(&f);
    assert(!vox_voice_detected(&f, 500, 150, 3, 5));   /* 1 of 5: a click */
    assert(!vox_voice_detected(&f, 10, 150, 3, 5));
    assert(!vox_voice_detected(&f, 500, 150, 3, 5));   /* 2 of 5 */
    assert(vox_voice_detected(&f, 500, 150, 3, 5));    /* 3 of 5 */
    assert(vox_voice_detected(&f, 10, 150, 3, 5));     /* still 3 of the last 5 */
    assert(!vox_voice_detected(&f, 10, 150, 3, 5));    /* oldest loud block aged out: 2 of 5 */
    assert(vox_voice_detected(&f, 150, 150, 1, 1));    /* threshold is inclusive */
''')

    def test_reset_clears_history_and_filter_state(self):
        out = self.run_c(r'''
    struct vox_filter f; vox_filter_reset(&f);
    vox_voice_detected(&f, 500, 150, 3, 5); vox_voice_detected(&f, 500, 150, 3, 5);
    tone(1000, 8000); vox_block_level(&f, block, 1600);
    vox_filter_reset(&f);
    assert(!vox_voice_detected(&f, 500, 150, 3, 5));
    for (int i = 0; i < 1600; i++) block[i] = 0;
    printf("%u", vox_block_level(&f, block, 1600));    /* no ringing carried over */
''')
        self.assertEqual(out, '0')


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_vox_filter_c -v`
Expected: errors — `vox_filter.h: No such file or directory`.

- [ ] **Step 3: Implement**

`FW/src/lib/core/vox_filter.h`:

```c
#ifndef OMI_VOX_FILTER_H
#define OMI_VOX_FILTER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Speech-band level for the silence timer (mic.c aad_track_silence). Each 100 ms block
 * of 16 kHz mono PCM passes a 250 Hz high-pass then a 3.4 kHz low-pass (Butterworth
 * biquads), so keystrokes and rumble -- loud mostly below 200 Hz -- barely register.
 * A block only counts as sound when at least `sustain` of the last `window` blocks were
 * at or above the threshold, so a single click cannot reset the timer.
 */
struct vox_biquad {
    float z1, z2;
};

struct vox_filter {
    struct vox_biquad hp, lp;
    uint8_t history; /* bit n set: the block n updates ago was loud */
};

void vox_filter_reset(struct vox_filter *f);
uint32_t vox_block_level(struct vox_filter *f, const int16_t *pcm, size_t frames);
bool vox_voice_detected(struct vox_filter *f, uint32_t level, uint32_t threshold, unsigned sustain,
                        unsigned window);

#endif
```

`FW/src/lib/core/vox_filter.c`:

```c
#include "vox_filter.h"

#include <string.h>

/* RBJ cookbook coefficients at 16 kHz, Q = 1/sqrt(2): b0, b1, b2, a1, a2 (a0 normalised). */
static const float HIGH_PASS_250[5] = {0.932932156f, -1.865864312f, 0.932932156f, -1.861361147f, 0.870367477f};
static const float LOW_PASS_3400[5] = {0.227117964f, 0.454235928f, 0.227117964f, -0.276664615f, 0.185136470f};

static float biquad(struct vox_biquad *s, const float c[5], float x)
{
    /* Transposed direct form II. */
    float y = c[0] * x + s->z1;
    s->z1 = c[1] * x - c[3] * y + s->z2;
    s->z2 = c[2] * x - c[4] * y;
    return y;
}

void vox_filter_reset(struct vox_filter *f)
{
    memset(f, 0, sizeof(*f));
}

uint32_t vox_block_level(struct vox_filter *f, const int16_t *pcm, size_t frames)
{
    if (frames == 0) {
        return 0;
    }
    float sum = 0.0f;
    for (size_t i = 0; i < frames; i++) {
        float y = biquad(&f->lp, LOW_PASS_3400, biquad(&f->hp, HIGH_PASS_250, (float) pcm[i]));
        sum += y < 0.0f ? -y : y;
    }
    return (uint32_t) (sum / (float) frames);
}

bool vox_voice_detected(struct vox_filter *f, uint32_t level, uint32_t threshold, unsigned sustain,
                        unsigned window)
{
    uint8_t mask = window >= 8 ? 0xFF : (uint8_t) ((1U << window) - 1U);
    f->history = (uint8_t) (((unsigned) f->history << 1 | (level >= threshold ? 1U : 0U)) & mask);
    unsigned loud = 0;
    for (uint8_t bits = f->history; bits; bits &= (uint8_t) (bits - 1)) {
        loud++;
    }
    return loud >= sustain;
}
```

In `FW/CMakeLists.txt`, add `src/lib/core/vox_filter.c` to the source list directly after the line `src/lib/core/codec.c`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_vox_filter_c -v`
Expected: 4 tests OK. Then `clang-format -i omi/firmware/omi/src/lib/core/vox_filter.{c,h}` from the repo root and re-run.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/vox_filter.c omi/firmware/omi/src/lib/core/vox_filter.h omi/firmware/omi/CMakeLists.txt omi/firmware/scripts/omi-local/tests/test_vox_filter_c.py
git commit -m "firmware: add speech-band VOX filter"
```

---

### Task 2: Replay tool for threshold tuning

**Files:**
- Create: `RX/omi_local/vox_replay.py`
- Test: `RX/tests/test_vox_replay.py`

- [ ] **Step 1: Write the failing test**

`RX/tests/test_vox_replay.py`:

```python
import math
import shutil
import unittest
from array import array

from omi_local import vox_replay as V


class AwakeSecondsTests(unittest.TestCase):
    def test_union_of_hold_windows_from_file_start(self):
        # Awake [0, 30) at wake, then 30 s after each reset: [0, 40) and [100, 130).
        self.assertEqual(V.awake_seconds([0.0, 10.0, 100.0], duration=200.0, hold=30.0), 70.0)

    def test_clipped_to_duration(self):
        self.assertEqual(V.awake_seconds([], duration=12.0, hold=30.0), 12.0)
        self.assertEqual(V.awake_seconds([5.0], duration=20.0, hold=30.0), 20.0)


@unittest.skipUnless(shutil.which('cc') and V.FILTER_SOURCE.exists(), 'native compiler and firmware source needed')
class ProductionFilterTests(unittest.TestCase):
    def test_levels_and_resets_use_the_firmware_filter(self):
        speech = array('h', (int(2000 * math.sin(2 * math.pi * 1000 * i / 16000)) for i in range(16000)))
        hum = array('h', (int(2000 * math.sin(2 * math.pi * 100 * i / 16000)) for i in range(16000)))
        lib = V.load_filter()
        speech_levels = V.block_levels(lib, speech)
        hum_levels = V.block_levels(lib, hum)
        self.assertEqual(len(speech_levels), 10)
        self.assertGreater(min(speech_levels[2:]), 1150)
        self.assertLess(max(hum_levels[2:]), 260)
        self.assertEqual(V.reset_times(lib, speech_levels, threshold=500, sustain=3, window=5)[0], 0.3)  # third loud block
        self.assertEqual(V.reset_times(lib, hum_levels, threshold=500, sustain=3, window=5), [])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_vox_replay -v`
Expected: `ImportError: cannot import name 'vox_replay'`.

- [ ] **Step 3: Implement**

`RX/omi_local/vox_replay.py`:

```python
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
    return [(i + 1) * 0.1 for i, level in enumerate(levels)
            if lib.vox_voice_detected(ctypes.byref(state), level, threshold, sustain, window)]


def raw_reset_times(pcm: array, threshold: int = OLD_THRESHOLD) -> list[float]:
    times = []
    for i, start in enumerate(range(0, len(pcm) - BLOCK + 1, BLOCK)):
        if sum(abs(s) for s in pcm[start:start + BLOCK]) / BLOCK >= threshold:
            times.append((i + 1) * 0.1)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_vox_replay -v`
Expected: 3 tests OK.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/scripts/omi-local/omi_local/vox_replay.py omi/firmware/scripts/omi-local/tests/test_vox_replay.py
git commit -m "firmware: add VOX replay tool for threshold tuning"
```

---

### Task 3: Choose the threshold from the owner's recordings

**Files:** Modify `omi/firmware/VALIDATION_MAC.md` (results only; the value is applied in Task 4).

- [ ] **Step 1: Run the replay**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m omi_local.vox_replay ~/omi-recordings > /tmp/vox_replay.tsv; column -t /tmp/vox_replay.tsv | head -100`

Reference sets from the owner's earlier analysis (confirmed speech by listening):
- **Speech**: `omi_20260928-220051_seq000000431552.opus`, `omi_20260928-213948_seq000000419257.opus`, `omi_20260929-153510_seq000000459951.opus`, `omi_20260929-154435_seq000000466621.opus`, `omi_20260928-210447_seq000000406897.opus`.
- **Typing / rumble**: all `omi_20260929-15*` files whose duration is about 30–60 s, plus `omi_20260929-152107_seq000000451221.opus` (175 s kept awake by low-frequency sound).

- [ ] **Step 2: Choose**

Pick the **largest** candidate threshold T such that every speech file's `newT_s` is at least 95% of its `old_awake_s`. Report, for the typing/rumble set, total `old_awake_s` versus total `newT_s`. If no candidate satisfies the speech rule, or the typing/rumble set does not drop by at least 50%, stop and report the table instead of guessing (the fallback discussed in the spec is a second high-pass section).

- [ ] **Step 3: Record**

Append to `omi/firmware/VALIDATION_MAC.md`:

```markdown
## Speech-band VOX threshold — 2026-09-30

Replayed the owner's pulled recordings through the production `vox_filter.c`
(`python -m omi_local.vox_replay`), 30 s hold, 3-of-5 sustain, first 500 ms skipped.
Chosen `CONFIG_OMI_VAD_ABS_THRESHOLD=<T>`: every speech file keeps >= 95% of its awake
time; typing/rumble files drop from <old total> s to <new total> s awake.

<paste the rows for the reference files from /tmp/vox_replay.tsv as a markdown table>
```

(Fill the three placeholders with the measured values; they are data from Step 2, not guesses.)

- [ ] **Step 4: Commit**

```bash
git add omi/firmware/VALIDATION_MAC.md
git commit -m "docs: record speech-band VOX threshold tuning"
```

---

### Task 4: Silence timer uses the speech-band VOX

**Files:**
- Modify: `FW/src/mic.c` (include, remove `avg_abs_amplitude`, `aad_track_silence`)
- Modify: `FW/Kconfig`, `FW/omi.conf`
- Test: `RX/tests/test_vox_c.py`

- [ ] **Step 1: Rewrite the silence-timer test**

In `RX/tests/test_vox_c.py`:

1. Change `compile_run` so every compile also builds the filter and links libm:

```python
    def compile_run(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'test.c').write_text(source)
            subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-I',str(SRC),str(root/'test.c'),
                            str(SRC/'lib/core/vox_filter.c'),'-o',str(root/'test'),'-lm'],check=True)
            return subprocess.check_output([str(root/'test')])
```

2. Replace `test_thirty_continuous_seconds_and_wake_reset` entirely with:

```python
    def test_speech_band_sustain_hold_and_wake_reset(self):
        self.compile_run(r'''
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <math.h>
#include <assert.h>
#include "lib/core/vox_filter.h"
#define CONFIG_OMI_VAD_ABS_THRESHOLD 150
#define CONFIG_OMI_VAD_SUSTAIN_BLOCKS 3
#define CONFIG_OMI_VAD_WINDOW_BLOCKS 5
#define CONFIG_OMI_VAD_HOLD_MS 30000
#define PI 3.14159265358979
static int aad_woke,aad_in_sleep,aad_req_sleep,aad_sem;
static int64_t aad_last_voice_ms,now;
static bool syncing;
static struct vox_filter vox;
int64_t k_uptime_get(void){return now;}
int atomic_get(int *p){return *p;}
void atomic_set(int *p,int v){*p=v;}
bool atomic_cas(int *p,int a,int b){if(*p!=a)return false;*p=b;return true;}
void k_sem_give(int *p){(*p)++;}
bool storage_transfer_active(void){return syncing;}
''' + function(SRC/'mic.c','static void aad_track_silence(const int16_t *buf, size_t n)\n{') + r'''
static int16_t quiet[1600], speech[1600], hum[1600];
static void block(const int16_t *b, int64_t t){now=t;aad_track_silence(b,1600);}
int main(void){
 for(int i=0;i<1600;i++){
  speech[i]=(int16_t)(600*sin(2*PI*1000*i/16000.0));   /* ~380 after the filter */
  hum[i]=(int16_t)(1200*sin(2*PI*100*i/16000.0));      /* ~120 after the filter */
 }
 for(int k=0;k<10;k++) block(hum,100+k*100);          /* loud low-frequency sound never counts */
 assert(aad_last_voice_ms==0);
 block(speech,2000); block(speech,2100); assert(aad_last_voice_ms==0);   /* 2 of 5 */
 block(speech,2200); assert(aad_last_voice_ms==2200);                    /* 3 of 5 */
 block(quiet,2300); block(quiet,2400); assert(aad_last_voice_ms==2400);  /* still 3 of 5 */
 block(quiet,2500); assert(aad_last_voice_ms==2400);                     /* 2 of 5 */
 now=2400+29999; aad_track_silence(quiet,1600); assert(!aad_req_sleep);
 now=2400+30000; aad_track_silence(quiet,1600); assert(aad_req_sleep && aad_sem==1);
 aad_req_sleep=0; aad_woke=1; block(quiet,100000); assert(aad_last_voice_ms==100000);
 block(speech,100100); block(speech,100200); assert(aad_last_voice_ms==100000); /* wake cleared history */
 syncing=true; block(quiet,200000); assert(!aad_req_sleep);
 syncing=false; block(quiet,200100); assert(aad_req_sleep);
 return 0;
}
''')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_vox_c -v`
Expected: `test_speech_band_sustain_hold_and_wake_reset` fails (hum resets the timer, or `vox` is unused and `-Werror` stops the build); the other two tests pass.

- [ ] **Step 3: Implement**

In `FW/src/mic.c`:

1. Add `#include "lib/core/vox_filter.h"` next to the other `lib/core` includes.
2. Delete the whole `static uint32_t avg_abs_amplitude(const int16_t *buf, size_t n)` function (its only caller changes below).
3. Directly above `static void aad_track_silence(const int16_t *buf, size_t n)` (the definition, not the forward declaration), add `static struct vox_filter vox; /* mic thread only */`.
4. Replace the start of `aad_track_silence`:

```c
    if (atomic_cas(&aad_woke, 1, 0)) {
        aad_last_voice_ms = now;
    }
    if (avg_abs_amplitude(buf, n) >= CONFIG_OMI_VAD_ABS_THRESHOLD) {
        aad_last_voice_ms = now;
    }
```

with:

```c
    if (atomic_cas(&aad_woke, 1, 0)) {
        aad_last_voice_ms = now;
        vox_filter_reset(&vox); /* every mic start: no stale filter state or history */
    }
    if (vox_voice_detected(&vox, vox_block_level(&vox, buf, n), CONFIG_OMI_VAD_ABS_THRESHOLD,
                           CONFIG_OMI_VAD_SUSTAIN_BLOCKS, CONFIG_OMI_VAD_WINDOW_BLOCKS)) {
        aad_last_voice_ms = now;
    }
```

In `FW/Kconfig`, replace the `help` text of `config OMI_VAD_ABS_THRESHOLD` with:

```
    help
        "Average absolute amplitude of a 100 ms block after the speech-band filter
         (250 Hz high-pass, 3.4 kHz low-pass). Enough such blocks (see
         OMI_VAD_SUSTAIN_BLOCKS) reset the silence timer that decides when to enter
         T5838 hardware AAD sleep. This does not gate streamed audio."
```

and add after the `OMI_VAD_ABS_THRESHOLD` entry:

```
config OMI_VAD_SUSTAIN_BLOCKS
    int "Speech-band blocks needed in the recent window to count as sound"
    range 1 8
    default 3
    depends on OMI_ENABLE_T5838_AAD
    help
        "At least this many of the last OMI_VAD_WINDOW_BLOCKS 100 ms blocks must reach
         OMI_VAD_ABS_THRESHOLD before the silence timer resets, so isolated clicks such
         as keystrokes do not keep the microphone awake."

config OMI_VAD_WINDOW_BLOCKS
    int "Recent 100 ms blocks considered by the VOX sustain rule"
    range 1 8
    default 5
    depends on OMI_ENABLE_T5838_AAD
```

In `FW/omi.conf`, replace `CONFIG_OMI_VAD_ABS_THRESHOLD=400` with the value T chosen in Task 3, and add below it:

```
CONFIG_OMI_VAD_SUSTAIN_BLOCKS=3
CONFIG_OMI_VAD_WINDOW_BLOCKS=5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `clang-format -i omi/firmware/omi/src/mic.c` (repo root), then from RX: `.venv/bin/python -m unittest tests.test_vox_c tests.test_vox_filter_c tests.test_manual_recording_c -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/mic.c omi/firmware/omi/Kconfig omi/firmware/omi/omi.conf omi/firmware/scripts/omi-local/tests/test_vox_c.py
git commit -m "firmware: reset the silence timer only on sustained speech-band sound"
```

---

### Task 5: Hold policy

**Files:**
- Modify: `FW/src/lib/core/button_hold.h` (whole file)
- Test: `RX/tests/test_wifi_config_c.py`

- [ ] **Step 1: Update the test**

In `RX/tests/test_wifi_config_c.py`:

1. In the embedded C harness, replace

```c
 if (!strcmp(argv[1], "hold")) {
  printf("%d", button_hold_action(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]))); return 0;
 }
```

with

```c
 if (!strcmp(argv[1], "hold")) {
  printf("%d", button_hold_action(atoi(argv[2]), atoi(argv[3]))); return 0;
 }
 if (!strcmp(argv[1], "level")) {
  printf("%d", button_hold_level(atoi(argv[2]), atoi(argv[3]))); return 0;
 }
```

2. Replace `test_deliberate_setup_and_power_off` with:

```python
    def test_hold_release_windows(self):
        # (held ms, wifi build, action): 0 none, 1 pause toggle, 2 power off, 3 setup
        for ms, wifi, action in [(100,1,0),(2999,1,0),(3000,1,1),(4999,1,1),(5000,1,0),(9999,1,0),
                                 (10000,1,2),(14999,1,2),(15000,1,0),(19999,1,0),(20000,1,3),(60000,1,3),
                                 (3000,0,1),(10000,0,2),(20000,0,0)]:
            self.assertEqual(self.run_c('hold',str(ms),str(wifi)),str(action), (ms, wifi))

    def test_vibration_levels_while_holding(self):
        for ms, wifi, level in [(2999,1,0),(3000,1,1),(9999,1,1),(10000,1,2),(19999,1,2),(20000,1,3),
                                (20000,0,2)]:
            self.assertEqual(self.run_c('level',str(ms),str(wifi)),str(level), (ms, wifi))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_wifi_config_c -v`
Expected: the harness fails to compile (`too few arguments to function 'button_hold_action'`, `button_hold_level` undeclared).

- [ ] **Step 3: Implement**

Replace `FW/src/lib/core/button_hold.h` with:

```c
#ifndef OMI_BUTTON_HOLD_H
#define OMI_BUTTON_HOLD_H
#include <stdbool.h>
#include <stdint.h>

/*
 * Every gesture is hold-then-release. While held, the motor marks each window as it is
 * reached (1 pulse: pause/resume, 2: power off, 3: Wi-Fi setup); releasing inside a window
 * commits it. Releasing early or in a dead band (5-10 s, 15-20 s) does nothing.
 */
enum button_hold_action { HOLD_NONE, HOLD_PAUSE_TOGGLE, HOLD_POWER_OFF, HOLD_SETUP };

#define BUTTON_PAUSE_MS 3000U
#define BUTTON_PAUSE_END_MS 5000U
#define BUTTON_POWER_OFF_MS 10000U
#define BUTTON_POWER_OFF_END_MS 15000U
#define BUTTON_SETUP_MS 20000U

static inline enum button_hold_action button_hold_action(uint32_t ms, bool wifi)
{
    if (ms >= BUTTON_PAUSE_MS && ms < BUTTON_PAUSE_END_MS)
        return HOLD_PAUSE_TOGGLE;
    if (ms >= BUTTON_POWER_OFF_MS && ms < BUTTON_POWER_OFF_END_MS)
        return HOLD_POWER_OFF;
    if (wifi && ms >= BUTTON_SETUP_MS)
        return HOLD_SETUP;
    return HOLD_NONE;
}

/* Vibration pulses for the window reached after holding `ms`; play each level once. */
static inline uint8_t button_hold_level(uint32_t ms, bool wifi)
{
    if (wifi && ms >= BUTTON_SETUP_MS)
        return 3;
    if (ms >= BUTTON_POWER_OFF_MS)
        return 2;
    if (ms >= BUTTON_PAUSE_MS)
        return 1;
    return 0;
}
#endif
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_wifi_config_c -v`
Expected: all OK. (`test_manual_recording_c` now fails to compile until Task 6 — expected.)

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/button_hold.h omi/firmware/scripts/omi-local/tests/test_wifi_config_c.py
git commit -m "firmware: hold-then-release button windows with vibration levels"
```

---

### Task 6: Gestures, vibration pulses and resume order

**Files:**
- Modify: `FW/src/lib/core/haptic.h`, `FW/src/haptic.c`, `FW/src/lib/core/button.c`, `FW/src/mic.c`
- Test: `RX/tests/test_manual_recording_c.py`

- [ ] **Step 1: Update the tests**

In `RX/tests/test_manual_recording_c.py`:

1. In `test_owner_serializes_pause_wake_resume_and_failures`:
   - add `#define MANUAL_RESUME_MOTOR_SETTLE_MS 150` after `#define AAD_PDM_SETTLE_MS 20`;
   - add `settles` to the `static int flushes,...` declaration list;
   - replace `void k_msleep(int ms){assert(ms==20);}` with
     `void k_msleep(int ms){if(ms==150){assert(!rail);settles++;}else assert(ms==20);}`;
   - replace the line `assert(!manual_paused && running && rail && sd && aad_woke && !buzzes);` with
     `assert(!manual_paused && running && rail && sd && aad_woke && !buzzes && settles==1);`.
2. Replace `test_button_release_toggles_once_and_preserves_long_holds` entirely with:

```python
    def test_hold_windows_vibrate_while_held_and_act_on_release(self):
        self.compile_run(r"""
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <stddef.h>
#include "lib/core/button_hold.h"
#define CONFIG_OMI_WIFI_UPLOAD 1
#define CONFIG_OMI_ENABLE_HAPTIC 1
#define IS_ENABLED(x) (x)
#define LOG_INF(...) ((void)0)
#define LOG_WRN(...) ((void)0)
#define LOG_PRINTK(...) ((void)0)
#define K_MSEC(x) (x)
#define BUTTON_CHECK_INTERVAL 40
#define BUTTON_PRESSED 1
#define BUTTON_RELEASED 0
#define TAP_THRESHOLD 300
#define DOUBLE_TAP_WINDOW 600
#define GRACE 1
typedef uint8_t u_int8_t;
typedef enum { BUTTON_EVENT_NONE, BUTTON_EVENT_SINGLE_TAP, BUTTON_EVENT_DOUBLE_TAP,
 BUTTON_EVENT_LONG_PRESS, BUTTON_EVENT_RELEASE } ButtonEvent;
struct k_work {int unused;};
static bool was_pressed,btn_is_pressed,is_off;
static uint8_t hold_level_played;
static uint32_t now,current_time,btn_press_start_time,btn_release_time,btn_last_tap_time;
static u_int8_t btn_last_event;
static int button_work,current_button_state,toggles,off,setup,buzz,last;
uint32_t k_uptime_get_32(void){return now;}
int mic_toggle_manual_pause(void){toggles++;return 0;}
void turnoff_all(void){off++;}
void wifi_upload_request_provisioning(void){setup++;}
void notify_long_tap(void){} void notify_tap(void){}
void notify_double_tap(void){} void notify_unpress(void){}
void play_haptic_pulses(uint8_t n){buzz++;last=n;}
void k_work_reschedule(int *w,int ms){(void)w;assert(ms==40);}
""" + function(SRC/'lib/core/button.c','void check_button_level(struct k_work *work_item)\n{') + r"""
void poll(uint32_t t,bool down){now=t;was_pressed=down;check_button_level(NULL);}
int main(void){
 poll(1000,true);poll(1120,false);assert(!toggles && !buzz);            /* short clicks do nothing */
 poll(2000,true);poll(4960,true);assert(!buzz);
 poll(5000,true);assert(buzz==1 && last==1 && !toggles);                  /* 1 pulse at 3 s, still held */
 poll(5040,true);assert(buzz==1);poll(6000,false);assert(toggles==1);    /* release at 4 s: pause toggle */
 poll(7000,true);poll(10000,true);assert(buzz==2);poll(13000,false);assert(toggles==1 && !off); /* 6 s: dead band */
 poll(14000,true);poll(17000,true);poll(24000,true);assert(buzz==4 && last==2);
 poll(25000,false);assert(off==1 && toggles==1);                           /* 11 s: power off */
 poll(26000,true);poll(43000,false);assert(off==1 && !setup);             /* 17 s: dead band */
 poll(44000,true);poll(64000,true);assert(last==3 && !setup);             /* 3 pulses at 20 s, still held */
 poll(64040,false);assert(setup==1 && off==1);                            /* setup on release */
 is_off=true;poll(70000,true);poll(73500,false);assert(toggles==1);      /* no toggle while off */
 return 0;
}
""")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_manual_recording_c -v`
Expected: both changed tests fail (`settles==1` assertion; the button harness fails to compile or asserts because `button.c` still uses `hold_handled`).

- [ ] **Step 3: Implement**

`FW/src/lib/core/haptic.h` — add below `void play_haptic_milli(uint32_t duration);`:

```c
/**
 * @brief Play `count` (1-3) 80 ms pulses, 120 ms apart, without blocking.
 */
void play_haptic_pulses(uint8_t count);
```

`FW/src/haptic.c`:

1. Below `static struct k_work_delayable haptic_off_work;` add:

```c
#define HAPTIC_PULSE_ON_MS 80
#define HAPTIC_PULSE_GAP_MS 120
static struct k_work_delayable pulse_work;
static uint8_t pulses_left;
static bool pulse_on;

static void pulse_work_handler(struct k_work *work)
{
    ARG_UNUSED(work);
    if (pulse_on) {
        gpio_pin_set_dt(&haptic_pin, 0);
        pulse_on = false;
        if (--pulses_left > 0)
            k_work_schedule(&pulse_work, K_MSEC(HAPTIC_PULSE_GAP_MS));
        return;
    }
    gpio_pin_set_dt(&haptic_pin, 1);
    pulse_on = true;
    k_work_schedule(&pulse_work, K_MSEC(HAPTIC_PULSE_ON_MS));
}
```

   (If `haptic_pin` is declared below this point in the file, place the block after its declaration instead.)
2. In `haptic_init`, after `k_work_init_delayable(&haptic_off_work, haptic_off_work_handler);` add
   `k_work_init_delayable(&pulse_work, pulse_work_handler);`.
3. After `play_haptic_milli`, add:

```c
void play_haptic_pulses(uint8_t count)
{
    if (count == 0 || !gpio_is_ready_dt(&haptic_pin))
        return;
    k_work_cancel_delayable(&haptic_off_work);
    k_work_cancel_delayable(&pulse_work);
    if (gpio_pin_configure_dt(&haptic_pin, GPIO_OUTPUT)) {
        LOG_ERR("Failed to configure haptic pin for pulses");
        return;
    }
    pulses_left = count > 3 ? 3 : count;
    pulse_on = false;
    k_work_schedule(&pulse_work, K_NO_WAIT);
}
```

`FW/src/lib/core/button.c`:

1. Replace `static bool hold_handled;` with `static uint8_t hold_level_played;`.
2. In `check_button_level`, in the pressed branch replace `hold_handled = false;` with `hold_level_played = 0;`.
3. In the released branch, replace

```c
        /* Toggle on each short release, independent of delayed single/double
         * tap notifications. Long holds never become recording clicks. */
        if (!is_off && button_recording_click(press_duration, hold_handled)) {
            int ret = mic_toggle_manual_pause();
            if (ret)
                LOG_WRN("Recording toggle unavailable (%d)", ret);
        }
```

with

```c
        /* Every gesture is hold-then-release; the vibration while held said which. */
        switch (button_hold_action(press_duration, IS_ENABLED(CONFIG_OMI_WIFI_UPLOAD))) {
        case HOLD_PAUSE_TOGGLE:
            if (!is_off) {
                int ret = mic_toggle_manual_pause();
                if (ret)
                    LOG_WRN("Recording toggle unavailable (%d)", ret);
            }
            break;
        case HOLD_POWER_OFF:
            turnoff_all();
            break;
        case HOLD_SETUP:
#ifdef CONFIG_OMI_WIFI_UPLOAD
            wifi_upload_request_provisioning();
            notify_long_tap();
#endif
            break;
        case HOLD_NONE:
            break;
        }
```

4. Replace the whole block that starts `if (!hold_handled && (btn_is_pressed || btn_release_time == current_time)) {` and ends at its matching closing brace (the power-off / setup block) with:

```c
#ifdef CONFIG_OMI_ENABLE_HAPTIC
    if (btn_is_pressed) {
        uint8_t level = button_hold_level(current_time - btn_press_start_time, IS_ENABLED(CONFIG_OMI_WIFI_UPLOAD));
        if (level > hold_level_played) {
            hold_level_played = level;
            play_haptic_pulses(level);
        }
    }
#endif
```

5. Ensure `button.c` includes `lib/core/haptic.h` (add `#include "haptic.h"` beside `#include "button.h"` if the existing includes don't already provide `play_haptic_milli`).

`FW/src/mic.c`:

1. After `#define AAD_PDM_SETTLE_MS 20` add
   `#define MANUAL_RESUME_MOTOR_SETTLE_MS 150 /* the 3 s hold pulse ends before the mic rail powers */`.
2. In `exit_manual_pause`, insert `k_msleep(MANUAL_RESUME_MOTOR_SETTLE_MS);` immediately before `t5838_aad_power(true);`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `clang-format -i omi/firmware/omi/src/lib/core/button.c omi/firmware/omi/src/haptic.c omi/firmware/omi/src/lib/core/haptic.h omi/firmware/omi/src/mic.c` (repo root), then from RX:
`.venv/bin/python -m unittest tests.test_manual_recording_c tests.test_wifi_config_c tests.test_vox_c tests.test_led_state_c tests.test_disconnect_power_c -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/lib/core/button.c omi/firmware/omi/src/haptic.c omi/firmware/omi/src/lib/core/haptic.h omi/firmware/omi/src/mic.c omi/firmware/scripts/omi-local/tests/test_manual_recording_c.py
git commit -m "firmware: pause, power off and setup by hold-then-release with pulse feedback"
```

---

### Task 7: Docs, portal help and version

**Files:** `FW/src/portal.html`, `omi/firmware/AGENTS.md`, `omi/firmware/PROVISIONING.md`, `SYSTEM.md`, `FW/omi.conf`, `FW/overlay-wifi-upload.conf`

- [ ] **Step 1: Portal help**

In `FW/src/portal.html` replace `Hold the button for 20 seconds to reopen it.` with
`To reopen it, hold the button until it buzzes three times (20 seconds), then release.`
Run the portal test: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_portal_page -v` (expect OK; if it asserts the old sentence, update the assertion to the new one).

- [ ] **Step 2: Firmware docs**

- `omi/firmware/AGENTS.md`: replace the paragraph starting "Short button releases (40–999 ms) toggle manual pause" and the paragraph starting "The Wi-Fi setup hold is 20 seconds" with:

```markdown
Every button gesture is hold-then-release (`button_hold.h`): release at 3–5 s toggles
manual pause (1 pulse at 3 s), 10–15 s powers off (2 pulses at 10 s), and 20 s or more
opens Wi-Fi setup (3 pulses at 20 s; Wi-Fi build only). Short clicks and releases in the
5–10 s / 15–20 s dead bands do nothing. Pulses play while held, so resume waits 150 ms
for the motor before powering the mic. Manual pause drains an end marker, disables
acoustic wake and powers down the mic rail; while paused, red pulses 200 ms every 3 s and
sound cannot resume it. `test_wifi_config_c.py` and `test_manual_recording_c.py` execute
the production policy, handler and owner paths. Keep the portal help in sync.

The silence timer measures a 250 Hz–3.4 kHz band-passed level (`vox_filter.c`) and needs
3 of the last 5 100 ms blocks at `CONFIG_OMI_VAD_ABS_THRESHOLD`, so keystrokes do not keep
the mic awake. Tune the threshold with `python -m omi_local.vox_replay <recordings>`.
```

- `omi/firmware/PROVISIONING.md`: in the setup and power-off paragraphs and the "Manual pause and wake feedback" section, replace the old gestures (20 s hold opens setup, 3–5 s release powers off, click under one second pauses) with the table:

```markdown
| Hold, then release | Vibration while holding | Action |
|---|---|---|
| 3–5 s | 1 pulse at 3 s | pause / resume recording |
| 10–15 s | 2 pulses at 10 s | power off |
| 20 s or more | 3 pulses at 20 s | open Wi-Fi setup (Wi-Fi build) |

Short clicks and releases between windows do nothing. From `3.0.22-localwifi.15`.
```

   Also change the sentence "The BLE-only build retains its three-second power-off hold." to "The BLE-only build uses the same pause and power-off windows; it has no setup gesture." and update the VOX paragraph that cites `CONFIG_OMI_VAD_ABS_THRESHOLD=300` to describe the speech-band measure and the tuned value.
- Root `SYSTEM.md`, "Firmware and transport": update the VOX bullet (speech-band level, 3-of-5 sustain, tuned threshold) and the button bullet ("Short button release toggles manual pause/resume") to the new windows; set the installed-image line only after flashing (leave `.14` now).

- [ ] **Step 3: Version**

In both `FW/omi.conf` and `FW/overlay-wifi-upload.conf`, change `3.0.22-localwifi.14` to `3.0.22-localwifi.15`.

- [ ] **Step 4: Full suite**

Run from the repo root: `omi/firmware/scripts/omi-local/.venv/bin/python scripts/test.py 2>&1 | grep -E '^Ran|^OK|Executed .* tests|FAILED'`
Expected: all blocks OK.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/omi/src/portal.html omi/firmware/AGENTS.md omi/firmware/PROVISIONING.md SYSTEM.md omi/firmware/omi/omi.conf omi/firmware/omi/overlay-wifi-upload.conf omi/firmware/scripts/omi-local/tests
git commit -m "firmware: document .15 gestures and speech-band VOX"
```

---

### Task 8: Build, verify and archive `.15` (no flashing)

- [ ] **Step 1: Build**

From the repo root:

```bash
export PATH=~/.local/bin:$PATH OMI_PROTOC_PYTHON=$HOME/ncs/protoc-venv/bin/python \
  OMI_PROTOC=$PWD/omi/firmware/scripts/protoc-native NCS_ROOT=$HOME/ncs/v2.9.0 ZEPHYR_BASE=$HOME/ncs/v2.9.0/zephyr
bash omi/firmware/scripts/build-cv1-macos.sh --wifi > /tmp/omi-build15.log 2>&1; echo exit=$?; tail -5 /tmp/omi-build15.log
```

Expected: `exit=0` and the three artifacts listed.

- [ ] **Step 2: Verify configuration and signatures**

```bash
B=omi/firmware/build/local-wifi
grep -hE 'CONFIG_(OMI_VAD_ABS_THRESHOLD|OMI_VAD_SUSTAIN_BLOCKS|OMI_VAD_WINDOW_BLOCKS|BT_DIS_FW_REV_STR)=' $B/omi/zephyr/.config
grep -E '^ +(FLASH|RAM):' /tmp/omi-build15.log | head -2
D=$(mktemp -d /tmp/ota15.XXXX); unzip -q $B/dfu_application.zip -d $D
IMG=$(find ~/ncs/v2.9.0/bootloader/mcuboot/scripts -name imgtool.py | head -1)
for f in $D/*.bin; do ~/.local/bin/nrfutil toolchain-manager launch --ncs-version v2.9.0 -- python3 "$IMG" verify -k omi/firmware/bootloader/mcuboot/root-rsa-2048.pem "$f"; done
shasum -a 256 $B/dfu_application.zip
cp $B/dfu_application.zip ~/omi-firmware/Omi_CV1_OTA_3.0.22-localwifi.15.zip
```

Expected: version `.15`, the tuned threshold, sustain 3, window 5; both images "correctly validated"; flash use under 949,760 bytes.

- [ ] **Step 3: Record**

Append to `omi/firmware/VALIDATION_MAC.md` a `## Firmware 3.0.22-localwifi.15 — 2026-09-30` section: the changes (gestures and speech-band VOX), test counts from Task 7, flash/RAM use, ZIP SHA-256, app and network digests from Step 2, the archive path, and "Built only; not installed on a device yet."

- [ ] **Step 4: Commit**

```bash
git add omi/firmware/VALIDATION_MAC.md
git commit -m "docs: record verified .15 firmware build"
```
