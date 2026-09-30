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
__attribute__((unused)) static void tone(double hz, double amp) {
    /* 1600 samples at 16 kHz hold whole cycles of 100 Hz and 1 kHz: phase stays continuous. */
    for (int i = 0; i < 1600; i++) block[i] = (int16_t) (amp * sin(2 * PI * hz * i / 16000.0));
}
__attribute__((unused)) static uint32_t settle(struct vox_filter *f) {
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
