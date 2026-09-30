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


class RawResetTimesTests(unittest.TestCase):
    def test_threshold_boundary_and_block_end_timestamps(self):
        block = V.BLOCK
        pcm = array('h', [400] * block + [399] * block + [-400] * block)
        self.assertEqual(V.raw_reset_times(pcm, threshold=400), [0.1, 0.3])


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
