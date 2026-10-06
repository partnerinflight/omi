"""Execute the production recorded-audio high-pass and its place in mic.c natively."""
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'omi' / 'src'
CORE = SRC / 'lib' / 'core'


def function(path, name):
    source = path.read_text(); start = source.index(name)
    return source[start:source.index('\n}\n', start) + 3]


PRELUDE = r'''
#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include "lib/core/mic_highpass.h"
#define PI 3.14159265358979
static int16_t block[1600];
__attribute__((unused)) static void tone(double hz, double amp) {
    /* 1600 samples at 16 kHz hold whole cycles of every tone used here. */
    for (int i = 0; i < 1600; i++) block[i] = (int16_t) lrint(amp * sin(2 * PI * hz * i / 16000.0));
}
__attribute__((unused)) static double rms(const int16_t *b, int n) {
    double s = 0; for (int i = 0; i < n; i++) s += (double) b[i] * b[i]; return sqrt(s / n);
}
/* Gain in dB of a settled tone (one second of warm-up). */
__attribute__((unused)) static double gain_db(double hz) {
    struct mic_highpass f; mic_highpass_reset(&f);
    for (int k = 0; k < 10; k++) { tone(hz, 10000); mic_highpass_apply(&f, block, 1600); }
    return 20 * log10(rms(block, 1600) / (10000 / sqrt(2)));
}
'''


@unittest.skipUnless(shutil.which('cc'), 'native C compiler required')
class MicHighpassTests(unittest.TestCase):
    def run_c(self, body, extra=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'test.c').write_text(PRELUDE + extra + 'int main(void) {\n' + body + '\n    return 0;\n}\n')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(SRC), str(root / 'test.c'),
                            str(CORE / 'mic_highpass.c'), '-o', str(root / 'test'), '-lm'], check=True)
            return subprocess.check_output([str(root / 'test')]).decode()

    def test_rumble_is_removed_and_speech_band_kept(self):
        out = self.run_c(r'''
    double hz[] = {30, 50, 60, 100, 150, 300, 1000, 3400};
    for (int i = 0; i < 8; i++) printf("%g ", gain_db(hz[i]));
''')
        g = dict(zip((30, 50, 60, 100, 150, 300, 1000, 3400), map(float, out.split())))
        # Analog 4th-order Butterworth at 100 Hz: -41.8, -24.1, -17.8, -3.0, -0.17 dB.
        self.assertLess(g[30], -38)
        self.assertLess(g[50], -21)
        self.assertLess(g[60], -15)
        self.assertAlmostEqual(g[100], -3.0, delta=0.5)
        for f in (150, 300, 1000, 3400):
            self.assertGreater(g[f], -0.5, f)
            self.assertLess(g[f], 0.1, f)

    def test_block_boundaries_do_not_change_the_output(self):
        self.run_c(r'''
    static int16_t once[1600], split[1600];
    struct mic_highpass a, b; mic_highpass_reset(&a); mic_highpass_reset(&b);
    tone(40, 9000); for (int i = 0; i < 1600; i++) block[i] += (int16_t) (i % 7 * 300);
    for (int i = 0; i < 1600; i++) once[i] = split[i] = block[i];
    mic_highpass_apply(&a, once, 1600);
    mic_highpass_apply(&b, split, 333); mic_highpass_apply(&b, split + 333, 0);
    mic_highpass_apply(&b, split + 333, 1267);
    for (int i = 0; i < 1600; i++) assert(once[i] == split[i]);
''')

    def test_full_scale_steps_saturate_instead_of_wrapping(self):
        out = self.run_c(r'''
    struct mic_highpass f; mic_highpass_reset(&f);
    int16_t step[400];
    for (int i = 0; i < 400; i++) step[i] = i < 200 ? 32767 : -32768;
    mic_highpass_apply(&f, step, 400);
    /* -32768 -> 32767 overshoots past int16 range: it must clamp to the same sign. */
    printf("%d %d", step[0], step[200]);
''')
        first, swing = out.split()
        self.assertEqual(int(first), int(32767 * 0.964626232 * 0.984818525))  # both sections' b0
        self.assertEqual(swing, '-32768')

    def test_reset_clears_ringing(self):
        self.assertEqual(self.run_c(r'''
    struct mic_highpass f; mic_highpass_reset(&f);
    tone(50, 20000); mic_highpass_apply(&f, block, 1600);
    mic_highpass_reset(&f);
    for (int i = 0; i < 1600; i++) block[i] = 0;
    mic_highpass_apply(&f, block, 1600);
    printf("%g", rms(block, 1600));
'''), '0')

    def test_mic_path_filters_before_vox_and_codec_and_warms_on_discarded_blocks(self):
        # The production process_audio_buffer: paused blocks are dropped untouched; discarded
        # startup blocks run through the filter (flushing stale state) but are not recorded.
        stubs = r'''
#include <stdbool.h>
#define CONFIG_OMI_ENABLE_T5838_AAD 1
#define CONFIG_OMI_MIC_HIGHPASS 1
#define BYTES_PER_SAMPLE 2
#define CHANNELS 2
#define MAX_FRAMES 1600
#define __ASSERT_NO_MSG(x) assert(x)
#define LOG_ERR(...) (void) 0
typedef void (*mix_handler)(int16_t *);
static int mem_slab, freed, manual_pause_requested, manual_paused, mic_discard_blocks;
static int atomic_get(int *p) { return *p; }
static void atomic_dec(int *p) { (*p)--; }
static void k_mem_slab_free(int *slab, void *b) { (void) slab; (void) b; freed++; }
static int16_t mono_buffer[MAX_FRAMES];
static struct mic_highpass rumble_filter;
static double vox_rms = -1, codec_rms = -1;
static int recorded;
static void aad_track_silence(const int16_t *buf, size_t n) { vox_rms = rms(buf, (int) n); }
static void record(int16_t *buf) { codec_rms = rms(buf, 1600); recorded++; }
static volatile mix_handler callback_func = record;
static int16_t stereo[3200];
static void stereo_tone(double hz, double amp) {
    tone(hz, amp); for (int i = 0; i < 1600; i++) stereo[2 * i] = stereo[2 * i + 1] = block[i];
}
static inline void
''' + function(SRC / 'mic.c', 'interleaved_stereo_to_mono(') + function(
            SRC / 'mic.c', 'static void process_audio_buffer(void *buffer, uint32_t size)\n{')
        out = self.run_c(r'''
    stereo_tone(1000, 8000);
    manual_paused = 1; process_audio_buffer(stereo, sizeof(stereo));
    assert(recorded == 0 && vox_rms < 0 && freed == 1);
    struct mic_highpass untouched = {0};
    for (int s = 0; s < 2; s++) assert(rumble_filter.z[s][0] == untouched.z[s][0]);
    manual_paused = 0;
    stereo_tone(40, 20000);
    mic_discard_blocks = 5;
    for (int k = 0; k < 5; k++) process_audio_buffer(stereo, sizeof(stereo));
    assert(recorded == 0 && vox_rms < 0 && mic_discard_blocks == 0 && freed == 6);
    process_audio_buffer(stereo, sizeof(stereo));             /* settled: no start-up transient */
    printf("%g %g ", vox_rms, codec_rms);
    stereo_tone(1000, 8000);
    for (int k = 0; k < 2; k++) process_audio_buffer(stereo, sizeof(stereo));
    printf("%g %g %d", vox_rms, codec_rms, recorded);
''', stubs)
        rumble_vox, rumble_codec, speech_vox, speech_codec, recorded = out.split()
        self.assertLess(float(rumble_codec), 14142 * 10 ** (-28 / 20))   # 40 Hz at -31.8 dB
        self.assertEqual(rumble_vox, rumble_codec)                       # VOX sees the same PCM
        self.assertAlmostEqual(float(speech_codec), 8000 / math.sqrt(2), delta=60)
        self.assertEqual(speech_vox, speech_codec)
        self.assertEqual(recorded, '3')


if __name__ == '__main__':
    unittest.main()
